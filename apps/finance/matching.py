"""Match Amazon orders to the bank transactions that paid for them.

Amazon charges per shipment, usually the order total, a day or few after the order.
Matching is by exact amount inside a date window, in three passes, from most to least
certain:

1. exact: an order and a transaction that are each other's only candidate.
2. ambiguous: several orders/transactions share an amount (two Rs 499 orders); the
   closest dates are paired and flagged so you can check them.
3. split: an order with no single-charge match paid as 2-3 charges that add up to it.

Anything else stays unmatched. Orders the user cancelled, and transactions already
linked (including by hand), are never touched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from itertools import combinations

DAYS_BEFORE = 1  # a charge can land the day before the email is received (time zones)
DAYS_AFTER = 14  # shipments (and so charges) can trail the order
MAX_SPLIT = 3
MAX_SPLIT_POOL = 12

AMAZON_SQL = (
    "(LOWER(COALESCE(t.counterparty_raw, '')) LIKE '%amazon%'"
    " OR LOWER(COALESCE(t.counterparty_raw, '')) LIKE '%amzn%'"
    " OR LOWER(COALESCE(t.counterparty_key, '')) LIKE '%amazon%'"
    " OR LOWER(COALESCE(t.counterparty_key, '')) LIKE '%amzn%'"
    " OR LOWER(COALESCE(r.name, '')) LIKE '%amazon%')"
)


@dataclass
class Result:
    exact: int = 0
    ambiguous: int = 0
    split: int = 0

    @property
    def total(self) -> int:
        return self.exact + self.ambiguous + self.split


def _day(iso: str) -> date:
    return date.fromisoformat(iso[:10])


def _in_window(order_day: date, txn_day: date) -> bool:
    return -DAYS_BEFORE <= (txn_day - order_day).days <= DAYS_AFTER


def candidate_transactions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unmatched debits that look like Amazon charges."""
    return conn.execute(
        "SELECT t.id, t.occurred_at, t.amount_paise FROM transactions t"
        " LEFT JOIN recipients r ON r.id = t.recipient_id"
        f" WHERE t.direction = 'debit' AND t.order_id IS NULL AND t.instrument != 'atm' AND {AMAZON_SQL}"
        " ORDER BY t.occurred_at, t.id"
    ).fetchall()


def open_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Orders that can still be matched: has a total, not cancelled, no charge linked yet."""
    return conn.execute(
        "SELECT o.id, o.ordered_at, o.total_paise FROM orders o"
        " WHERE o.total_paise IS NOT NULL AND o.status != 'cancelled'"
        " AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.order_id = o.id)"
        " ORDER BY o.ordered_at, o.id"
    ).fetchall()


def _link(conn: sqlite3.Connection, order_id: int, txn_id: int, how: str) -> None:
    conn.execute("UPDATE transactions SET order_id = ?, order_match = ? WHERE id = ?", (order_id, how, txn_id))


def match_orders(conn: sqlite3.Connection) -> Result:
    result = Result()
    orders = {o["id"]: o for o in open_orders(conn)}
    txns = {t["id"]: t for t in candidate_transactions(conn)}

    # every (order, txn) pair with the same amount inside the window
    pairs: dict[tuple[int, int], int] = {}
    for oid, o in orders.items():
        od = _day(o["ordered_at"])
        for tid, t in txns.items():
            if t["amount_paise"] == o["total_paise"] and _in_window(od, _day(t["occurred_at"])):
                pairs[(oid, tid)] = abs((_day(t["occurred_at"]) - od).days)

    def take(oid: int, tid: int, how: str) -> None:
        _link(conn, oid, tid, how)
        orders.pop(oid, None)
        txns.pop(tid, None)
        for key in [k for k in pairs if k[0] == oid or k[1] == tid]:
            del pairs[key]
        setattr(result, how, getattr(result, how) + 1)

    # 1. mutual unique candidates, repeated since each match can make others unique
    progress = True
    while progress:
        progress = False
        for oid, tid in sorted(pairs):
            if sum(1 for k in pairs if k[0] == oid) == 1 and sum(1 for k in pairs if k[1] == tid) == 1:
                take(oid, tid, "exact")
                progress = True
                break

    # 2. ambiguous: closest date first, earliest order breaks ties
    for (oid, tid), _ in sorted(pairs.items(), key=lambda kv: (kv[1], orders[kv[0][0]]["ordered_at"], kv[0])):
        if oid in orders and tid in txns:
            take(oid, tid, "ambiguous")

    # 3. split: 2-3 leftover charges that add up to an order
    for oid in sorted(orders, key=lambda i: orders[i]["ordered_at"]):
        o = orders[oid]
        od = _day(o["ordered_at"])
        pool = [t for t in txns.values() if _in_window(od, _day(t["occurred_at"]))][:MAX_SPLIT_POOL]
        best = None
        for size in range(2, MAX_SPLIT + 1):
            for combo in combinations(pool, size):
                if sum(t["amount_paise"] for t in combo) == o["total_paise"]:
                    spread = (_day(combo[-1]["occurred_at"]) - _day(combo[0]["occurred_at"])).days
                    if best is None or spread < best[0]:
                        best = (spread, combo)
            if best:
                break
        if best:
            for t in best[1]:
                _link(conn, oid, t["id"], "split")
                txns.pop(t["id"], None)
            result.split += 1
    return result


def link_manually(conn: sqlite3.Connection, order_id: int, txn_id: int) -> None:
    conn.execute(
        "UPDATE transactions SET order_id = ?, order_match = 'manual' WHERE id = ? AND direction = 'debit'",
        (order_id, txn_id),
    )


def unlink(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute("UPDATE transactions SET order_id = NULL, order_match = NULL WHERE order_id = ?", (order_id,))
