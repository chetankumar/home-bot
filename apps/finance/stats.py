"""Dashboard maths. Pure functions over a connection and a fixed 'today'.

"Spend" is debits in categories that count as spend (uncategorised debits
count; Transfers don't). Credits and refunds are shown but not netted off.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

# Re-exported for the dashboard route (stats.parse_mode) and tests; the maths itself lives in the host.
from hub.services.charts import (  # noqa: F401
    MODE_ONEOFFS,
    MODE_RUNRATE,
    MODES,
    ProgressStats,
    parse_mode,
    progress_stats,
)

SPEND_FILTER = (
    "t.direction = 'debit' AND (t.category_id IS NULL OR"
    " t.category_id IN (SELECT id FROM categories WHERE counts_as_spend = 1))"
)


# Forecast modes (the maths is the host's ctx.charts service). "runrate": average daily spend so far x
# days in the month. "oneoffs": big single payments (rent, an EMI) count once as already paid and only
# the everyday spend is extrapolated.
FORECAST_MODES = MODES
DEFAULT_ONEOFF_PAISE = 5000_00


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


def daily_spend(conn: sqlite3.Connection, year: int, month: int) -> dict[int, int]:
    """Spend per day of the month (paise), for days that had any."""
    start, end = month_bounds(year, month)
    rows = conn.execute(
        f"SELECT CAST(substr(t.occurred_at, 9, 2) AS INTEGER) AS day, SUM(t.amount_paise) AS total"
        f" FROM transactions t WHERE {SPEND_FILTER} AND t.occurred_at >= ? AND t.occurred_at < ?"
        f" GROUP BY day",
        (start, end),
    ).fetchall()
    return {r["day"]: int(r["total"]) for r in rows}


def big_payments(conn: sqlite3.Connection, year: int, month: int, threshold: int) -> list[dict]:
    """Single spends at or above `threshold` paise, oldest first."""
    start, end = month_bounds(year, month)
    rows = conn.execute(
        f"SELECT t.id, CAST(substr(t.occurred_at, 9, 2) AS INTEGER) AS day, t.amount_paise AS amount,"
        f" COALESCE(r.name, t.counterparty_raw, 'Payment') AS name"
        f" FROM transactions t LEFT JOIN recipients r ON r.id = t.recipient_id"
        f" WHERE {SPEND_FILTER} AND t.amount_paise >= ? AND t.occurred_at >= ? AND t.occurred_at < ?"
        f" ORDER BY t.occurred_at, t.id",
        (threshold, start, end),
    ).fetchall()
    return [dict(r) for r in rows]


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
    days_left: int = 0  # days after today in the month
    target_daily: int | None = None  # paise/day you can spend from now to finish exactly on budget
    cut_pct: int | None = None  # % the current daily rate must fall to hit target_daily (None = on track)
    mode: str = MODE_RUNRATE
    big_total: int = 0  # paise of big payments counted once (only in "oneoffs" mode)
    big_count: int = 0


def month_stats(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    today: date,
    budget: int | None,
    mode: str = MODE_RUNRATE,
    threshold: int = DEFAULT_ONEOFF_PAISE,
) -> ProgressStats:
    """The month's numbers, from the host's chart service, so the cards and the chart can't disagree."""
    big = big_payments(conn, year, month, threshold) if mode == MODE_ONEOFFS else []
    return progress_stats(
        daily_spend(conn, year, month), year=year, month=month, today=today, limit=budget, mode=mode, big=big
    )


def burn(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    today: date,
    budget: int | None,
    mode: str = MODE_RUNRATE,
    threshold: int = DEFAULT_ONEOFF_PAISE,
) -> Burn:
    p = month_stats(conn, year, month, today, budget, mode, threshold)
    return Burn(
        spent=p.spent,
        days_elapsed=p.days_elapsed,
        days_in_month=p.days_in_period,
        daily_rate=p.rate,
        projected=p.projected,
        budget=budget,
        remaining=p.remaining,
        projected_over=p.projected_over,
        days_left=p.days_left,
        target_daily=p.target_daily,
        cut_pct=p.cut_pct,
        mode=mode,
        big_total=p.big_total,
        big_count=len(p.big),
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
