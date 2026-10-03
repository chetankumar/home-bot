"""Sync pipeline: fetch -> store raw -> parse -> tag.

Re-running is always safe: emails are keyed by Gmail id and transactions by
email id. A single bad email never fails the whole sync; it stays
'unparsed' for the review page.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from hub.plugin import AppContext
from hub.services.ai import AIError, ExtractionError

from . import tagging
from .extract import ai_parse
from .parsers import IGNORE, Parsed, parse_email

JOB_ID = "sync"
DEFAULT_SENDERS = ["alerts@hdfcbank.net", "alerts@hdfcbank.bank.in"]


def local_iso(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).replace(tzinfo=None).isoformat(timespec="seconds")


def senders(ctx: AppContext) -> list[str]:
    return ctx.kv.get("senders") or list(DEFAULT_SENDERS)


BACKFILL_KEY = "backfill_from"  # one-shot: an ISO date set from Settings, cleared after a sync


def backfill_start(ctx: AppContext) -> datetime | None:
    """The date a pending backfill reaches back to (start of that day, local), if any."""
    raw = ctx.kv.get(BACKFILL_KEY)
    try:
        d = date.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=ctx.tz) if d else None


def sync_since(conn: sqlite3.Connection, tz: ZoneInfo, now: datetime) -> datetime:
    """First run: the 1st of this month (local). Later: newest email minus a day."""
    row = conn.execute("SELECT MAX(received_at) FROM emails").fetchone()
    if row and row[0]:
        return datetime.fromisoformat(row[0]).replace(tzinfo=tz) - timedelta(days=1)
    local = now.astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def gmail_query(sender_list: list[str], since: datetime) -> str:
    return f"from:({' OR '.join(sender_list)}) after:{int(since.timestamp())}"


@dataclass
class AIState:
    """Turns the model off for the rest of a run after the first connection failure,
    so a stopped local model server costs one timeout, not one per email."""

    enabled: bool = True
    error: str | None = None


def run_sync(ctx: AppContext, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(ctx.tz)
    stats: dict[str, Any] = {"fetched": 0, "new": 0, "parsed": 0, "ignored": 0, "unparsed": 0}
    try:
        backfill = backfill_start(ctx)
        with ctx.db() as conn:
            since = sync_since(conn, ctx.tz, now)
        if backfill and backfill < since:
            since = backfill  # reach further back once; already-stored emails are skipped by id
            stats["backfill_from"] = backfill.date().isoformat()
        ids = ctx.gmail.search(gmail_query(senders(ctx), since))
        stats["fetched"] = len(ids)
        with ctx.db() as conn:
            known = {r[0] for r in conn.execute("SELECT gmail_id FROM emails")}
        new_ids = [i for i in ids if i not in known]
        ai = AIState()
        for gid in reversed(new_ids):  # Gmail lists newest first; store oldest first
            msg = ctx.gmail.get_message(gid)
            with ctx.db() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO emails(gmail_id, received_at, sender, subject, body, status)"
                    " VALUES (?, ?, ?, ?, ?, 'unparsed')",
                    (msg.id, local_iso(msg.received_at, ctx.tz), msg.sender, msg.subject, msg.body),
                )
                if cur.rowcount == 0:
                    continue
                stats["new"] += 1
                stats[process_email(ctx, conn, msg.id, ai)] += 1
        if ai.error:
            stats["ai_error"] = ai.error
        stats["orders"] = amazon_step(ctx, now)
        if backfill:
            ctx.kv.delete(BACKFILL_KEY)  # done; the next sync goes back to its normal window
    except Exception as e:
        ctx.kv.set("last_sync", {**stats, "at": now.isoformat(timespec="seconds"), "error": str(e)})
        raise
    ctx.kv.set("last_sync", {**stats, "at": now.isoformat(timespec="seconds")})
    ctx.log.info("sync done: %s", stats)
    return stats


def amazon_step(ctx: AppContext, now: datetime) -> dict[str, Any]:
    """Scan Amazon order emails and match them to payments. Never fails the bank sync."""
    from .orders import sync_orders  # imported here: orders.py imports from this module

    try:
        return sync_orders(ctx, now)
    except Exception as e:
        ctx.log.exception("amazon order scan failed")
        return {"error": str(e)}


def process_email(
    ctx: AppContext, conn: sqlite3.Connection, gmail_id: str, ai: AIState | None
) -> str:
    """Parse one stored email and record the outcome. Returns its new status.

    A failure while handling this email is contained: its changes are undone, it is kept
    as 'unparsed' with the error for the Review page, and the sync carries on.
    """
    conn.execute("SAVEPOINT process_email")
    try:
        status = _process_email(ctx, conn, gmail_id, ai)
    except Exception as e:
        conn.execute("ROLLBACK TO process_email")
        ctx.log.exception("could not read email %s", gmail_id)
        conn.execute(
            "UPDATE emails SET status = 'unparsed', parser = NULL, error = ? WHERE gmail_id = ?",
            (f"{type(e).__name__}: {e}", gmail_id),
        )
        status = "unparsed"
    conn.execute("RELEASE process_email")
    return status


def _process_email(
    ctx: AppContext, conn: sqlite3.Connection, gmail_id: str, ai: AIState | None
) -> str:
    row = conn.execute("SELECT * FROM emails WHERE gmail_id = ?", (gmail_id,)).fetchone()
    result: Parsed | str | None = parse_email(row["subject"], row["body"])
    source, error = "regex", None
    if result is None and ai is not None and ai.enabled:
        source = "ai"
        try:
            result = ai_parse(ctx, row["subject"], row["body"])
        except ExtractionError as e:
            error = f"local model: {e}"
        except AIError as e:
            error = ai.error = f"local model unavailable: {e}"
            ai.enabled = False
        except Exception as e:  # never let one email sink the run
            error = f"local model: {e}"

    if result == IGNORE:
        status, parser = "ignored", source
    elif isinstance(result, Parsed):
        insert_transaction(conn, gmail_id, row["received_at"], result, source)
        status, parser = "parsed", result.parser
    else:
        status, parser = "unparsed", None
    conn.execute(
        "UPDATE emails SET status = ?, parser = ?, error = ? WHERE gmail_id = ?",
        (status, parser, error, gmail_id),
    )
    return status


def occurred_at(received_at: str | None, occurred_on: date | None) -> str:
    """Use the email's receive time unless the alert names a different day."""
    if occurred_on and (not received_at or received_at[:10] != occurred_on.isoformat()):
        return f"{occurred_on.isoformat()}T00:00:00"
    return received_at or datetime.now().isoformat(timespec="seconds")


def insert_transaction(
    conn: sqlite3.Connection,
    email_id: str | None,
    received_at: str | None,
    p: Parsed,
    source: str,
) -> int | None:
    key, _kind = tagging.counterparty_key(p.counterparty_raw, p.instrument)
    owner = tagging.lookup(conn, key)
    cur = conn.execute(
        "INSERT OR IGNORE INTO transactions(email_id, occurred_at, amount_paise, direction,"
        " instrument, account_mask, counterparty_raw, counterparty_key, reference,"
        " recipient_id, category_id, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            email_id,
            occurred_at(received_at, p.occurred_on),
            p.amount_paise,
            p.direction,
            p.instrument,
            p.account_mask,
            p.counterparty_raw,
            key,
            p.reference,
            owner["id"] if owner else None,
            owner["category_id"] if owner else None,
            source,
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def reparse(ctx: AppContext, use_ai: bool = True) -> dict[str, int]:
    """Re-run parsers over unparsed and ignored emails (after parsers improve).

    Emails you ignored by hand on the review page are left alone."""
    stats = {"parsed": 0, "ignored": 0, "unparsed": 0}
    ai = AIState(enabled=use_ai)
    with ctx.db() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT gmail_id FROM emails WHERE status IN ('unparsed', 'ignored')"
                " AND COALESCE(parser, '') != 'manual' ORDER BY received_at"
            )
        ]
    for gid in ids:
        with ctx.db() as conn:
            # Only use the model for mail that was never classified as noise.
            was = conn.execute("SELECT status FROM emails WHERE gmail_id = ?", (gid,)).fetchone()[0]
            stats[process_email(ctx, conn, gid, ai if was == "unparsed" else None)] += 1
    return stats
