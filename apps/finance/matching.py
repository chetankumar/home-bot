"""Match Amazon orders to the bank transactions that paid for them.

The strongest signal is *timing*: the bank's alert email and Amazon's order email reach
your inbox within minutes of each other. The amount is a second signal, and an order
whose email gives no total can still match on timing plus a plausible amount.

Passes, from most to least certain; each only considers what earlier passes left:

1. same amount and within `window_minutes` of each other        -> exact
2. same amount, a unique pairing within the date window           -> exact
3. same amount, several candidates: closest in time/date first    -> ambiguous (check it)
4. within `window_minutes` and a plausible amount (unique pair)   -> ambiguous (check it)
5. 2-3 charges that add up to the order total                     -> split
6. a charge equal to some of the order's item prices (a shipment) -> split

Anything else stays unmatched; link it by hand. Cancelled orders and transactions that
are already linked (including by hand) are never touched. Every match stores a short
note saying why, shown on the Orders page.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations

DAYS_BEFORE = 1  # a charge can land the day before the email is received (time zones)
DAYS_AFTER = 14  # shipments (and so charges) can trail the order
WINDOW_MINUTES = 30  # bank alert and order email are "close" within this many minutes
SLACK_PAISE = 150_00  # delivery fees / coupons: plausible charge within max(this, 25%) of the items
SLACK_PCT = 0.25
MAX_SPLIT = 3
MAX_SPLIT_POOL = 12
MAX_SUBSET_ITEMS = 8

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


@dataclass
class Txn:
    id: int
    at: datetime
    amount: int
    precise: bool  # `at` is when the bank alert email arrived (not just the day)


@dataclass
class Order:
    id: int
    at: datetime  # when the order email arrived
    total: int | None
    prices: list[int | None] = field(default_factory=list)
    estimated: bool = False  # the total is the sum of item prices, not stated by the email

    @property
    def item_sum(self) -> int | None:
        if not self.prices or any(p is None for p in self.prices):
            return None
        return sum(self.prices)  # type: ignore[arg-type]


def _inr(paise: int) -> str:
    return f"₹{paise / 100:,.0f}" if paise % 100 == 0 else f"₹{paise / 100:,.2f}"


def candidate_transactions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unmatched debits that look like Amazon charges."""
    return conn.execute(
        "SELECT t.id, t.occurred_at, t.amount_paise, e.received_at AS email_at FROM transactions t"
        " LEFT JOIN recipients r ON r.id = t.recipient_id"
        " LEFT JOIN emails e ON e.gmail_id = t.email_id"
        f" WHERE t.direction = 'debit' AND t.order_id IS NULL AND t.instrument != 'atm' AND {AMAZON_SQL}"
        " ORDER BY COALESCE(e.received_at, t.occurred_at), t.id"
    ).fetchall()


def _load_txns(conn: sqlite3.Connection) -> dict[int, Txn]:
    out = {}
    for r in candidate_transactions(conn):
        out[r["id"]] = Txn(
            r["id"], datetime.fromisoformat(r["email_at"] or r["occurred_at"]), r["amount_paise"], bool(r["email_at"])
        )
    return out


def _load_orders(conn: sqlite3.Connection) -> dict[int, Order]:
    """Orders that can still be matched: not cancelled, nothing linked yet."""
    rows = conn.execute(
        "SELECT o.id, o.ordered_at, o.total_paise, o.total_source FROM orders o"
        " WHERE o.status != 'cancelled'"
        " AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.order_id = o.id)"
        " ORDER BY o.ordered_at, o.id"
    ).fetchall()
    prices: dict[int, list[int | None]] = {}
    for i in conn.execute("SELECT order_id, price_paise FROM order_items ORDER BY id"):
        prices.setdefault(i["order_id"], []).append(i["price_paise"])
    return {
        r["id"]: Order(
            r["id"], datetime.fromisoformat(r["ordered_at"]), r["total_paise"], prices.get(r["id"], []),
            estimated=r["total_source"] == "items",
        )
        for r in rows
    }


def open_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Orders that can still be matched (kept for the Orders page)."""
    return conn.execute(
        "SELECT o.id, o.ordered_at, o.total_paise FROM orders o"
        " WHERE o.status != 'cancelled' AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.order_id = o.id)"
        " ORDER BY o.ordered_at, o.id"
    ).fetchall()


def _link(conn: sqlite3.Connection, order_id: int, txn_id: int, how: str, note: str) -> None:
    conn.execute(
        "UPDATE transactions SET order_id = ?, order_match = ?, order_match_note = ? WHERE id = ?",
        (order_id, how, note, txn_id),
    )


def _minutes(o: Order, t: Txn) -> float:
    return abs((t.at - o.at).total_seconds()) / 60


def _near(o: Order, t: Txn, window: int) -> bool:
    return t.precise and _minutes(o, t) <= window


def _days(o: Order, t: Txn) -> int:
    return (t.at.date() - o.at.date()).days


def _in_days(o: Order, t: Txn) -> bool:
    return -DAYS_BEFORE <= _days(o, t) <= DAYS_AFTER


def _apart(o: Order, t: Txn) -> str:
    if t.precise and _minutes(o, t) < 120:
        return f"{_minutes(o, t):.0f} min apart"
    d = abs(_days(o, t))
    return "same day" if d == 0 else f"{d} day{'s' if d != 1 else ''} apart"


def _plausible(o: Order, t: Txn, slack: int) -> tuple[bool, str]:
    """Is this charge believable for the order when the amount isn't an exact match?"""
    ref = o.total if o.total is not None else o.item_sum
    if ref is None:
        return True, ""  # nothing to compare with: timing alone decides
    diff = t.amount - ref
    if abs(diff) <= max(slack, SLACK_PCT * ref):
        what = "item prices" if o.estimated or o.total is None else "order total"
        return True, f"{_inr(abs(diff))} {'above' if diff > 0 else 'below'} the {what}" if diff else ""
    return False, ""


def match_orders(
    conn: sqlite3.Connection, window_minutes: int = WINDOW_MINUTES, slack_paise: int = SLACK_PAISE
) -> Result:
    result = Result()
    orders, txns = _load_orders(conn), _load_txns(conn)

    def take(o: Order, t: Txn, how: str, note: str) -> None:
        _link(conn, o.id, t.id, how, note)
        orders.pop(o.id, None)
        txns.pop(t.id, None)
        setattr(result, how, getattr(result, how) + 1)

    # -- same amount ---------------------------------------------------------------------
    def equal_pairs() -> list[tuple[Order, Txn]]:
        return [
            (o, t)
            for o in orders.values()
            for t in txns.values()
            if o.total is not None and t.amount == o.total and (_near(o, t, window_minutes) or _in_days(o, t))
        ]

    # 1. equal amount and minutes apart: the closest pairs first
    for o, t in sorted((p for p in equal_pairs() if _near(*p, window_minutes)),
                       key=lambda p: (_minutes(*p), p[0].id, p[1].id)):
        if o.id in orders and t.id in txns:
            take(o, t, "exact", f"same amount, {_apart(o, t)}")

    # 2. equal amount, unique within the date window; repeat as each match frees others
    progress = True
    while progress:
        progress = False
        pairs = equal_pairs()
        for o, t in pairs:
            if sum(1 for p in pairs if p[0].id == o.id) == 1 and sum(1 for p in pairs if p[1].id == t.id) == 1:
                take(o, t, "exact", f"same amount, {_apart(o, t)}")
                progress = True
                break

    # 3. equal amount shared by several candidates: closest first, flagged for a look
    for o, t in sorted(equal_pairs(), key=lambda p: (abs(_days(*p)), p[0].at, p[0].id, p[1].id)):
        if o.id in orders and t.id in txns:
            take(o, t, "ambiguous", f"same amount as another order; closest, {_apart(o, t)}")

    # -- timing with a plausible amount (no total needed) ----------------------------------------
    def timing_pairs() -> list[tuple[Order, Txn, str]]:
        out = []
        for o in orders.values():
            for t in txns.values():
                if _near(o, t, window_minutes):
                    ok, why = _plausible(o, t, slack_paise)
                    if ok:
                        out.append((o, t, why))
        return out

    progress = True
    while progress:  # only a pairing that is unique both ways is trusted
        progress = False
        pairs = timing_pairs()
        for o, t, why in pairs:
            if sum(1 for p in pairs if p[0].id == o.id) == 1 and sum(1 for p in pairs if p[1].id == t.id) == 1:
                take(o, t, "ambiguous", f"{_apart(o, t)}" + (f", {why}" if why else ", amount not compared"))
                progress = True
                break

    # -- several charges adding up to the order total --------------------------------------------
    for oid in sorted(orders, key=lambda i: orders[i].at):
        o = orders[oid]
        if o.total is None:
            continue
        pool = [t for t in txns.values() if _in_days(o, t)][:MAX_SPLIT_POOL]
        best = None
        for size in range(2, MAX_SPLIT + 1):
            for combo in combinations(pool, size):
                if sum(t.amount for t in combo) == o.total:
                    spread = (combo[-1].at - combo[0].at).total_seconds()
                    if best is None or spread < best[0]:
                        best = (spread, combo)
            if best:
                break
        if best:
            for t in best[1]:
                _link(conn, oid, t.id, "split", f"one of {len(best[1])} charges adding up to the order total")
                txns.pop(t.id, None)
            orders.pop(oid)
            result.split += 1

    # -- a charge equal to some of the order's items (a shipment) --------------------------------
    for oid in sorted(orders, key=lambda i: orders[i].at):
        o = orders[oid]
        prices = o.prices
        if len(prices) < 2 or len(prices) > MAX_SUBSET_ITEMS or any(p is None for p in prices):
            continue
        free = set(range(len(prices)))
        linked_any = False
        for t in sorted((t for t in txns.values() if _in_days(o, t)), key=lambda t: (abs(_days(o, t)), t.at, t.id)):
            hit = next((c for n in range(1, len(free) + 1) for c in combinations(sorted(free), n)
                        if sum(prices[i] for i in c) == t.amount), None)
            if hit:
                _link(conn, oid, t.id, "split", f"a shipment of {len(hit)} of the order's {len(prices)} items")
                txns.pop(t.id, None)
                free -= set(hit)
                linked_any = True
        if linked_any:
            orders.pop(oid)
            result.split += 1
    return result


def link_manually(conn: sqlite3.Connection, order_id: int, txn_id: int) -> None:
    conn.execute(
        "UPDATE transactions SET order_id = ?, order_match = 'manual', order_match_note = 'linked by you'"
        " WHERE id = ? AND direction = 'debit'",
        (order_id, txn_id),
    )


def unlink(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute(
        "UPDATE transactions SET order_id = NULL, order_match = NULL, order_match_note = NULL WHERE order_id = ?",
        (order_id,),
    )


def window_settings(config: dict) -> dict:
    """Matching knobs from [apps.finance] in hub.toml."""
    return {
        "window_minutes": int(config.get("amazon_match_minutes", WINDOW_MINUTES)),
        "slack_paise": round(float(config.get("amazon_amount_slack", SLACK_PAISE / 100)) * 100),
    }
