"""Dashboard maths. Pure functions over a connection and a fixed 'today'.

"Spend" is debits in categories that count as spend (uncategorised debits
count; Transfers don't). Credits and refunds are shown but not netted off.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date

SPEND_FILTER = (
    "t.direction = 'debit' AND (t.category_id IS NULL OR"
    " t.category_id IN (SELECT id FROM categories WHERE counts_as_spend = 1))"
)


def month_bounds(year: int, month: int) -> tuple[str, str]:
    """[start, end) as ISO date strings, comparable with local occurred_at."""
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


def parse_ym(ym: str | None, today: date) -> tuple[int, int]:
    if ym:
        try:
            y, m = (int(x) for x in ym.split("-"))
            if 1 <= m <= 12:
                return y, m
        except ValueError:
            pass
    return today.year, today.month


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def spent(conn: sqlite3.Connection, year: int, month: int) -> int:
    start, end = month_bounds(year, month)
    row = conn.execute(
        f"SELECT COALESCE(SUM(t.amount_paise), 0) FROM transactions t"
        f" WHERE {SPEND_FILTER} AND t.occurred_at >= ? AND t.occurred_at < ?",
        (start, end),
    ).fetchone()
    return int(row[0])


def credits(conn: sqlite3.Connection, year: int, month: int) -> int:
    start, end = month_bounds(year, month)
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM transactions"
        " WHERE direction = 'credit' AND occurred_at >= ? AND occurred_at < ?",
        (start, end),
    ).fetchone()
    return int(row[0])


@dataclass
class Burn:
    spent: int  # paise
    days_elapsed: int
    days_in_month: int
    daily_rate: int  # paise/day
    projected: int  # paise at month end
    budget: int | None
    remaining: int | None
    projected_over: int | None  # paise projected above budget (negative = under)


def burn(conn: sqlite3.Connection, year: int, month: int, today: date, budget: int | None) -> Burn:
    total = spent(conn, year, month)
    days_in_month = calendar.monthrange(year, month)[1]
    if (year, month) == (today.year, today.month):
        days = today.day
    elif (year, month) < (today.year, today.month):
        days = days_in_month
    else:
        days = 0
    rate = round(total / days) if days else 0
    projected = total if days == days_in_month else rate * days_in_month
    return Burn(
        spent=total,
        days_elapsed=days,
        days_in_month=days_in_month,
        daily_rate=rate,
        projected=projected,
        budget=budget,
        remaining=(budget - total) if budget is not None else None,
        projected_over=(projected - budget) if budget is not None else None,
    )


def by_category(conn: sqlite3.Connection, year: int, month: int) -> list[tuple[str, int]]:
    start, end = month_bounds(year, month)
    rows = conn.execute(
        f"SELECT COALESCE(c.name, 'Uncategorised') AS name, SUM(t.amount_paise) AS total"
        f" FROM transactions t LEFT JOIN categories c ON c.id = t.category_id"
        f" WHERE {SPEND_FILTER} AND t.occurred_at >= ? AND t.occurred_at < ?"
        f" GROUP BY name ORDER BY total DESC",
        (start, end),
    ).fetchall()
    return [(r["name"], r["total"]) for r in rows]


def top_recipients(conn: sqlite3.Connection, year: int, month: int, limit: int = 8):
    start, end = month_bounds(year, month)
    rows = conn.execute(
        f"SELECT COALESCE(r.name, t.counterparty_raw, t.counterparty_key, '(unknown)') AS name,"
        f" r.id IS NOT NULL AS tagged, SUM(t.amount_paise) AS total, COUNT(*) AS n"
        f" FROM transactions t LEFT JOIN recipients r ON r.id = t.recipient_id"
        f" WHERE {SPEND_FILTER} AND t.occurred_at >= ? AND t.occurred_at < ?"
        f" GROUP BY COALESCE('r' || r.id, 'k' || t.counterparty_key, 'raw' || t.counterparty_raw)"
        f" ORDER BY total DESC LIMIT ?",
        (start, end, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def untagged(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Counterparty keys you've paid but not yet named, biggest total spend first."""
    sql = (
        "SELECT t.counterparty_key AS key, MAX(t.counterparty_raw) AS raw,"
        " CASE WHEN t.counterparty_key LIKE '%@%' THEN 'upi' ELSE 'merchant' END AS kind,"
        " SUM(CASE WHEN t.direction = 'debit' THEN t.amount_paise ELSE 0 END) AS total,"
        " COUNT(*) AS n, MAX(t.occurred_at) AS last_seen"
        " FROM transactions t"
        " WHERE t.recipient_id IS NULL AND t.counterparty_key IS NOT NULL"
        " AND t.counterparty_key NOT IN (SELECT key FROM recipient_keys)"
        " GROUP BY t.counterparty_key HAVING total > 0 ORDER BY total DESC, n DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def untagged_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT counterparty_key) FROM transactions"
        " WHERE direction = 'debit' AND recipient_id IS NULL AND counterparty_key IS NOT NULL"
        " AND counterparty_key NOT IN (SELECT key FROM recipient_keys)"
    ).fetchone()[0]


def trailing_average(conn: sqlite3.Connection, today: date, months: int = 3) -> int | None:
    """Average spend over up to `months` full prior months we have complete data for.

    A month counts as complete only if email history starts on or before its
    first day, so a partial first backfill never produces a hint.
    """
    row = conn.execute("SELECT MIN(received_at) FROM emails").fetchone()
    if not row or not row[0]:
        return None
    history_start = row[0][:10]
    totals = []
    for i in range(1, months + 1):
        y, m = shift_month(today.year, today.month, -i)
        if history_start > date(y, m, 1).isoformat():
            break
        totals.append(spent(conn, y, m))
    return round(sum(totals) / len(totals)) if totals else None


def inr(paise: int | None, decimals: bool = True) -> str:
    """Indian digit grouping: 1234567.8 -> ₹12,34,567.80."""
    if paise is None:
        return "—"
    sign = "-" if paise < 0 else ""
    paise = abs(int(paise))
    rupees, frac = divmod(paise, 100)
    if not decimals and frac >= 50:
        rupees += 1
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}₹{s}" + (f".{frac:02d}" if decimals else "")
