"""Fetch Amazon order emails, keep the orders and items, and match them to payments.

Runs as a step of the Finance sync (see sync.run_sync). Like the bank sync it is safe
to re-run: emails are keyed by Gmail id and orders by order number.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from hub.plugin import AppContext

from . import amazon, learn, matching
from .sync import backfill_start, local_iso

JOB_ID = "orders"
DEFAULT_SENDERS = ["auto-confirm@amazon.in"]
DEFAULT_BACKFILL_DAYS = 90


def senders(ctx: AppContext) -> list[str]:
    return ctx.kv.get("amazon_senders") or list(DEFAULT_SENDERS)


def since(ctx: AppContext, conn: sqlite3.Connection, now: datetime) -> datetime:
    """First run: `amazon_backfill_days` back (default 90). Later: newest email minus a day."""
    row = conn.execute("SELECT MAX(received_at) FROM order_emails").fetchone()
    if row and row[0]:
        start = datetime.fromisoformat(row[0]).replace(tzinfo=ctx.tz) - timedelta(days=1)
    else:
        days = int(ctx.config.get("amazon_backfill_days", DEFAULT_BACKFILL_DAYS))
        start = (now.astimezone(ctx.tz) - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    backfill = backfill_start(ctx)  # a pending Settings backfill reaches back for orders too
    return min(start, backfill) if backfill else start


def upsert_order(conn: sqlite3.Connection, parsed: amazon.ParsedOrder, received_at: str) -> int:
    """Create the order, or fold a later email (shipped, delivered, cancelled) into it."""
    row = conn.execute("SELECT * FROM orders WHERE order_number = ?", (parsed.order_number,)).fetchone()
    if row is None:
        oid = conn.execute(
            "INSERT INTO orders(order_number, ordered_at, total_paise, total_source, status)"
            " VALUES (?, ?, ?, ?, ?)",
            (parsed.order_number, received_at, parsed.total_paise, parsed.total_source, parsed.status),
        ).lastrowid
    else:
        oid = row["id"]
        total, source = row["total_paise"], row["total_source"]
        # A real total (from the email or the model) replaces an estimate, never the reverse.
        if parsed.total_paise is not None and (total is None or source == "items"):
            total, source = parsed.total_paise, parsed.total_source
        conn.execute(
            "UPDATE orders SET ordered_at = MIN(ordered_at, ?), total_paise = ?, total_source = ?,"
            " status = ? WHERE id = ?",
            (received_at, total, source, amazon.merge_status(row["status"], parsed.status), oid),
        )
    # Items: keep the fullest list seen. A shipped email naming one item must not
    # replace a confirmation that listed three.
    have = conn.execute("SELECT COUNT(*) FROM order_items WHERE order_id = ?", (oid,)).fetchone()[0]
    if parsed.items and len(parsed.items) > have:
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (oid,))
        conn.executemany(
            "INSERT INTO order_items(order_id, title, quantity, price_paise) VALUES (?, ?, ?, ?)",
            [(oid, i.title, i.quantity, i.price_paise) for i in parsed.items],
        )
    # No total anywhere? Estimate it from the item prices so amount matching can use it.
    row = conn.execute("SELECT total_paise FROM orders WHERE id = ?", (oid,)).fetchone()
    if row["total_paise"] is None:
        stored = [
            amazon.OrderItem(i["title"], i["quantity"], i["price_paise"])
            for i in conn.execute("SELECT * FROM order_items WHERE order_id = ?", (oid,))
        ]
        estimate = amazon.estimate_total(stored)
        if estimate is not None:
            conn.execute("UPDATE orders SET total_paise = ?, total_source = 'items' WHERE id = ?", (estimate, oid))
    if parsed.status == "cancelled":
        matching.unlink(conn, oid)  # a cancelled order shouldn't keep a payment
    return oid


def process_email(ctx: AppContext, conn: sqlite3.Connection, gmail_id: str, use_ai: bool = True) -> str:
    """Parse one stored email and record the outcome. Returns its new status.

    A failure while handling this email is contained: its changes are undone, it is kept
    as 'unparsed' with the error for the Orders page, and the scan carries on.
    """
    conn.execute("SAVEPOINT process_email")
    try:
        status = _process_email(ctx, conn, gmail_id, use_ai)
    except Exception as e:
        conn.execute("ROLLBACK TO process_email")
        ctx.log.exception("amazon: could not read email %s", gmail_id)
        conn.execute(
            "UPDATE order_emails SET status = 'unparsed', parser = NULL, error = ? WHERE gmail_id = ?",
            (f"{type(e).__name__}: {e}", gmail_id),
        )
        status = "unparsed"
    conn.execute("RELEASE process_email")
    return status


def _process_email(ctx: AppContext, conn: sqlite3.Connection, gmail_id: str, use_ai: bool) -> str:
    row = conn.execute("SELECT * FROM order_emails WHERE gmail_id = ?", (gmail_id,)).fetchone()
    parsed: Any = amazon.parse_email(row["subject"], row["body"], learn.amazon_rules(conn))
    parser = parsed.parser if isinstance(parsed, amazon.ParsedOrder) else None
    # The model fills in what the regexes couldn't read: unreadable emails, or an order
    # whose items came only from the subject line, or whose total is missing.
    weak = parsed is None or (
        isinstance(parsed, amazon.ParsedOrder)
        and (parsed.parser != "body" or parsed.total_paise is None)
        and parsed.status == "placed"
    )
    if weak:  # the built-in (and approved) regexes didn't fully read it: log it for the regex compiler
        learn.record_miss(conn, "amazon", gmail_id, "ai_skipped")
    else:
        learn.clear_miss(conn, "amazon", gmail_id)
    if use_ai and weak:
        parsed = amazon.ai_fill(ctx, row["subject"], row["body"], parsed if isinstance(parsed, amazon.ParsedOrder) else None)
        parser = parsed.parser if isinstance(parsed, amazon.ParsedOrder) else parser
        learn.record_miss(conn, "amazon", gmail_id, _amazon_outcome(parsed))
        ctx.log.info("regex miss (amazon) %s", gmail_id)
    if isinstance(parsed, amazon.ParsedOrder):
        upsert_order(conn, parsed, row["received_at"])
        status = "parsed"
    elif parsed == "ignore":
        status = "ignored"
    else:
        status = "unparsed"
    conn.execute(
        "UPDATE order_emails SET status = ?, parser = ?, error = NULL WHERE gmail_id = ?",
        (status, parser, gmail_id),
    )
    return status


def _amazon_outcome(parsed: Any) -> str:
    if isinstance(parsed, amazon.ParsedOrder):
        return "ai_parsed" if parsed.parser == "ai" else "ai_failed"
    return "ai_ignored" if parsed == "ignore" else "ai_failed"


# The full sync and the Orders-only job can overlap; scan one at a time.
_scan_lock = threading.Lock()


def sync_orders(ctx: AppContext, now: datetime) -> dict[str, int]:
    """Scan Amazon order emails and match them to payments; remember how it went."""
    with _scan_lock:
        try:
            stats = _scan(ctx, now)
        except Exception as e:
            ctx.kv.set("last_orders_sync", {"at": now.isoformat(timespec="seconds"), "error": str(e)})
            raise
    ctx.kv.set("last_orders_sync", {**stats, "at": now.isoformat(timespec="seconds")})
    ctx.log.info("orders sync done: %s", stats)
    return stats


def run_orders_sync(ctx: AppContext) -> dict[str, int]:
    """The scheduled / Sync-now job. Raises on failure so it lands in the job history."""
    return sync_orders(ctx, datetime.now(ctx.tz))


def _scan(ctx: AppContext, now: datetime) -> dict[str, int]:
    stats = {"fetched": 0, "new": 0, "parsed": 0, "ignored": 0, "unparsed": 0, "matched": 0}
    with ctx.db() as conn:
        start = since(ctx, conn, now)
    ids = ctx.gmail.search(f"from:({' OR '.join(senders(ctx))}) after:{int(start.timestamp())}")
    stats["fetched"] = len(ids)
    with ctx.db() as conn:
        known = {r[0] for r in conn.execute("SELECT gmail_id FROM order_emails")}
    for gid in reversed([i for i in ids if i not in known]):  # oldest first, so confirmations precede updates
        msg = ctx.gmail.get_message(gid)
        with ctx.db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO order_emails(gmail_id, received_at, sender, subject, body, status)"
                " VALUES (?, ?, ?, ?, ?, 'unparsed')",
                (msg.id, local_iso(msg.received_at, ctx.tz), msg.sender, msg.subject, msg.body),
            )
            if cur.rowcount:
                stats["new"] += 1
                stats[process_email(ctx, conn, msg.id)] += 1
    with ctx.db() as conn:
        stats["matched"] = matching.match_orders(conn, **matching.window_settings(ctx.config)).total
    return stats


def reparse(ctx: AppContext, use_ai: bool = True) -> dict[str, int]:
    """Re-run the parser over unparsed/ignored Amazon emails (and ones the regexes only half read),
    then re-match."""
    stats = {"parsed": 0, "ignored": 0, "unparsed": 0, "matched": 0}
    with ctx.db() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT gmail_id FROM order_emails WHERE status IN ('unparsed', 'ignored')"
            " OR gmail_id IN (SELECT gmail_id FROM parse_misses WHERE kind = 'amazon') ORDER BY received_at")]
    for gid in ids:
        with ctx.db() as conn:
            stats[process_email(ctx, conn, gid, use_ai)] += 1
    with ctx.db() as conn:
        stats["matched"] = matching.match_orders(conn, **matching.window_settings(ctx.config)).total
    return stats
