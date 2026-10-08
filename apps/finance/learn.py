"""Regex compiler: ask the local model for regexes from emails the built-in ones missed.

Flow: sync records every email the built-in regexes could not read (`parse_misses`).
`propose()` takes up to 10 of them, masks them, asks the model for regexes, checks each
regex against those same emails, and stores the survivors as `proposed`. Nothing is used
until the user approves it on the Parsers page; approved regexes run after the built-in
ones, so they can only fill gaps.

Every regex, built-in or learned, is a row in `learned_parsers` with a scorecard: `matches`
goes up each time the regex reads an email (`hit`), and rules are always loaded
most-matches-first. There is no fixed order, so the patterns must not overlap.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from hub.plugin import AppContext
from hub.services.ai import AIError, ExtractionError

from . import amazon
from .amazon import AmazonRule, amazon_items
from .parsers import BUILTIN_SPECS, Rule, collapse, make_builder, mask, to_paise

SAMPLES = 10
MAX_PATTERN = 600
INSTRUMENTS = ("upi", "credit_card", "debit_card", "netbanking", "atm")


# -- the miss log -------------------------------------------------------------------------
def record_miss(conn: sqlite3.Connection, kind: str, gmail_id: str, outcome: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO parse_misses(kind, gmail_id, outcome) VALUES (?, ?, ?)", (kind, gmail_id, outcome)
    )


def clear_miss(conn: sqlite3.Connection, kind: str, gmail_id: str) -> None:
    conn.execute("DELETE FROM parse_misses WHERE kind = ? AND gmail_id = ?", (kind, gmail_id))


# -- safe patterns --------------------------------------------------------------------------
# Python's re has no timeout, and a model-written pattern runs on every email, so reject the
# classic catastrophic shape (a quantified group that itself holds a quantifier) and long patterns.
_NESTED = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)[+*{]")


def compile_pattern(pattern: str | None, needs: set[str], multiline: bool = False) -> re.Pattern[str] | None:
    """Compile a model-written pattern, or None if unsafe, invalid or missing a named group."""
    if not pattern or len(pattern) > MAX_PATTERN or _NESTED.search(pattern):
        return None
    try:
        rx = re.compile(pattern, re.I | (re.M if multiline else 0))
    except re.error:
        return None
    return rx if needs <= set(rx.groupindex) else None


@dataclass
class BankRule:
    id: int
    name: str
    rx: re.Pattern[str]
    direction: str
    instrument: str
    builder: str = "generic"
    builtin: bool = False

    def as_parser(self) -> Rule:
        # Learned rules carry a prefix so the Review page shows where a reading came from.
        name = self.name if self.builtin else f"learned:{self.name}"
        return name, self.rx, make_builder(self.builder, self.direction, self.instrument)


def ensure_seeded(conn: sqlite3.Connection, kind: str) -> None:
    """Make sure the built-in regexes are rows in the table. Cheap when they already are."""
    seeds = (
        [(n, p, None, d, i, b) for n, p, b, d, i in BUILTIN_SPECS]
        if kind == "bank"
        else [(n, p, None, None, None, "generic") for n, p in amazon.BUILTIN_TOTALS]
    )
    have = conn.execute("SELECT COUNT(*) FROM learned_parsers WHERE kind = ? AND builtin = 1", (kind,)).fetchone()[0]
    if have == len(seeds):
        return
    conn.executemany(
        "INSERT OR IGNORE INTO learned_parsers(kind, name, pattern, item_pattern, direction, instrument,"
        " builder, builtin, status) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active')",
        [(kind, n, p, ip, d, i or None, b) for n, p, ip, d, i, b in seeds],
    )


def _row_rules(conn: sqlite3.Connection, kind: str, status: str) -> list[sqlite3.Row]:
    ensure_seeded(conn, kind)
    # The scorecard decides the order: most matches first (id keeps ties in the original order).
    return conn.execute(
        "SELECT * FROM learned_parsers WHERE kind = ? AND status = ? ORDER BY matches DESC, id", (kind, status)
    ).fetchall()


def _compile(row: sqlite3.Row, column: str, needs: set[str], multiline: bool = False) -> re.Pattern[str] | None:
    if row["builtin"]:  # our own pattern: trusted
        return re.compile(row[column], re.I | (re.M if multiline else 0)) if row[column] else None
    return compile_pattern(row[column], needs, multiline)


def bank_rules(conn: sqlite3.Connection, status: str = "active") -> list[BankRule]:
    out = []
    for r in _row_rules(conn, "bank", status):
        rx = _compile(r, "pattern", {"amount"})
        if rx and r["direction"]:
            out.append(BankRule(
                r["id"], r["name"], rx, r["direction"], r["instrument"] or "", r["builder"], bool(r["builtin"])))
    return out


def amazon_rules(conn: sqlite3.Connection, status: str = "active") -> list[AmazonRule]:
    out = []
    for r in _row_rules(conn, "amazon", status):
        total = _compile(r, "pattern", {"total"})
        items = _compile(r, "item_pattern", {"title"}, multiline=True)
        if total or items:
            out.append(AmazonRule(r["id"], r["name"], total, items, bool(r["builtin"])))
    return out


def hit(conn: sqlite3.Connection, kind: str, name: str) -> None:
    """Score a match: the regex just read an email."""
    conn.execute(
        "UPDATE learned_parsers SET matches = matches + 1, last_matched_at = datetime('now')"
        " WHERE kind = ? AND name = ?",
        (kind, name.removeprefix("learned:")),
    )


def unique_name(conn: sqlite3.Connection, kind: str, name: str) -> str:
    taken = {r[0] for r in conn.execute("SELECT name FROM learned_parsers WHERE kind = ?", (kind,))}
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{name}_{n}"
    return candidate


# -- asking the model -----------------------------------------------------------------------
class BankProposal(BaseModel):
    name: str = Field(description="Short snake_case name for this email family, e.g. upi_debit_v3")
    pattern: str = Field(description="Python regex, case-insensitive, run on the whitespace-collapsed body")
    direction: Literal["debit", "credit"]
    instrument: Literal["upi", "credit_card", "debit_card", "netbanking", "atm"]


class BankProposals(BaseModel):
    parsers: list[BankProposal] = Field(default_factory=list, max_length=3)


class AmazonProposal(BaseModel):
    name: str = Field(description="Short snake_case name for this layout")
    total_pattern: str | None = Field(default=None, description="Regex with a named group `total`")
    item_pattern: str | None = Field(default=None, description="Regex with named groups `title`, optional `qty`, `price`")


BANK_SYSTEM = (
    "You write Python regular expressions for Indian bank (HDFC) alert emails. You get several emails "
    "that existing parsers could not read. Digit runs of 8+ are masked as XXXX1234 in what you see; in the "
    "real emails they are digits, so use \\d, never X, for numbers. Write up to 3 patterns, one per distinct "
    "email wording, each matching as many of the emails as possible. Rules: the pattern runs "
    "case-insensitively on the body with all whitespace collapsed to single spaces. Use named groups: "
    "(?P<amount>...) captures only the number (digits, commas, dot) after Rs./INR/₹; optional "
    "(?P<date>...), (?P<acct>...) last 4 digits, (?P<merchant>...), (?P<vpa>...). Anchor on wording "
    "around the values, not on the values. Never nest quantifiers like (a+)+. Set direction and instrument "
    "for the family. Skip emails that are not about money moving."
)

AMAZON_SYSTEM = (
    "You write Python regular expressions for Amazon.in order emails. You get several emails that the "
    "existing parser could not fully read. Provide total_pattern with a named group (?P<total>...) that "
    "captures only the order total number (digits, commas, dot), not subtotals; and item_pattern run "
    "line-by-line-aware (multiline mode, ^ and $ match at line ends) with (?P<title>...) per item and "
    "optionally (?P<qty>\\d+) and (?P<price>...). Use patterns that work across all the emails. Never "
    "nest quantifiers like (a+)+. Use null when no reliable pattern exists."
)


def _sample_block(i: int, subject: str, body: str, limit: int, keep_lines: bool) -> str:
    text = amazon.collapse_keep_lines(body) if keep_lines else collapse(body)
    return f"--- email {i} ---\nSubject: {subject}\n{mask(text)[:limit]}"


def pick_samples(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    table = "emails" if kind == "bank" else "order_emails"
    return conn.execute(
        f"SELECT e.* FROM parse_misses m JOIN {table} e ON e.gmail_id = m.gmail_id"
        " WHERE m.kind = ? AND m.outcome != 'ai_ignored' ORDER BY e.received_at DESC LIMIT ?",
        (kind, SAMPLES),
    ).fetchall()


def _known_amount(conn: sqlite3.Connection, gmail_id: str) -> int | None:
    row = conn.execute("SELECT amount_paise FROM transactions WHERE email_id = ?", (gmail_id,)).fetchone()
    return row[0] if row else None


@dataclass
class Check:
    """How a candidate regex fared on the sample emails."""

    hits: int = 0
    preview: str | None = None
    missed: list[int] = field(default_factory=list)  # 1-based numbers of emails it did not read
    wrong: list[str] = field(default_factory=list)  # emails it read differently from the earlier reading


def _bank_check(rule: BankRule, samples: list[sqlite3.Row], conn: sqlite3.Connection) -> Check:
    """How many samples the rule reads (agreeing with the model's earlier reading, if any)."""
    out = Check()
    _name, rx, build = rule.as_parser()
    for i, s in enumerate(samples, 1):
        text = collapse(s["body"])
        m = rx.search(text)
        p = None
        if m:
            try:
                p = build(m, text)
            except ValueError:
                p = None
        if p is None or p.amount_paise <= 0:
            out.missed.append(i)
            continue
        known = _known_amount(conn, s["gmail_id"])
        if known is not None and known != p.amount_paise:
            out.wrong.append(f"email {i}: your amount was {p.amount_paise / 100:g} but it is {known / 100:g}")
            continue
        out.hits += 1
        out.preview = out.preview or json.dumps(
            {"subject": s["subject"], "amount": p.amount_paise / 100, "direction": p.direction,
             "instrument": p.instrument, "account": p.account_mask, "counterparty": p.counterparty_raw,
             "date": p.occurred_on.isoformat() if p.occurred_on else None})
    return out


def _amazon_check(rule: AmazonRule, samples: list[sqlite3.Row]) -> Check:
    out = Check()
    for i, s in enumerate(samples, 1):
        total = rule.total.search(collapse(s["body"])) if rule.total else None
        items = amazon_items(rule, s["body"])
        try:
            paise = to_paise(total.group("total")) if total else None
        except ValueError:
            paise = None
        if not (paise or items):
            out.missed.append(i)
            continue
        out.hits += 1
        out.preview = out.preview or json.dumps(
            {"subject": s["subject"], "total": paise / 100 if paise else None, "items": [it.title for it in items][:5]})
    return out


def _snippet(sample: sqlite3.Row, kind: str) -> str:
    """The part of an unread email worth showing the model: around the first amount, else the start."""
    text = mask(amazon.collapse_keep_lines(sample["body"]) if kind == "amazon" else collapse(sample["body"]))
    m = re.search(r"(?:rs\.?|inr|₹)\s*\d", text, re.I)
    start = max(0, (m.start() if m else 0) - 100)
    return text[start : start + 260].replace("\n", " / ")


@dataclass
class Verdict:
    ok: bool
    notes: list[str]
    row: tuple | None = None  # what to insert when ok


def _evaluate(
    kind: str, cand: tuple, samples: list[sqlite3.Row], conn: sqlite3.Connection, need: int
) -> Verdict:
    """Test one proposed regex against the samples. Failures say exactly what to fix."""
    name, pattern, item_pattern, direction, instrument = cand
    name = re.sub(r"\W+", "_", (name or "").strip().lower()).strip("_")[:40] or kind
    if kind == "bank":
        if not pattern:
            return Verdict(False, [f"'{name}': the pattern is empty."])
        if instrument not in INSTRUMENTS:
            return Verdict(False, [f"'{name}': instrument must be one of {', '.join(INSTRUMENTS)}."])
        rx = compile_pattern(pattern, {"amount"})
        if rx is None:
            return Verdict(False, [f"'{name}': {_why_not_compiled(pattern, 'amount')}"])
        check = _bank_check(BankRule(0, name, rx, direction, instrument), samples, conn)
    else:
        total = compile_pattern(pattern, {"total"})
        items = compile_pattern(item_pattern, {"title"}, multiline=True)
        if not (total or items):
            why = []
            if pattern:
                why.append(f"total_pattern: {_why_not_compiled(pattern, 'total')}")
            if item_pattern:
                why.append(f"item_pattern: {_why_not_compiled(item_pattern, 'title')}")
            return Verdict(False, [f"'{name}': " + ("; ".join(why) or "give a total_pattern and/or an item_pattern.")])
        check = _amazon_check(AmazonRule(0, name, total, items), samples)
        pattern, item_pattern = (pattern if total else ""), (item_pattern if items else None)
    if check.hits >= need:
        return Verdict(True, [], (name, pattern, item_pattern, direction, instrument, check.hits, check.preview))
    notes = [f"'{name}' read only {check.hits} of {len(samples)} emails (it must read at least {need})."]
    notes += check.wrong
    for i in check.missed[:3]:
        notes.append(f"it did not match email {i}, which contains: \"{_snippet(samples[i - 1], kind)}\"")
    if len(check.missed) > 3:
        notes.append(f"... and {len(check.missed) - 3} more emails did not match.")
    return Verdict(False, notes)


def _why_not_compiled(pattern: str, group: str) -> str:
    if len(pattern) > MAX_PATTERN:
        return f"the pattern is longer than {MAX_PATTERN} characters; make it shorter."
    if _NESTED.search(pattern):
        return "it nests quantifiers like (a+)+, which is not allowed; rewrite without them."
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return f"it is not a valid Python regex ({e})."
    return f"it has no named group (?P<{group}>...); the group {group} is required (found: {sorted(rx.groupindex) or 'none'})."


MAX_REPLIES = 20
HISTORY_CHARS = 60_000  # keep the conversation within a local model's context


def _trim(messages: list[dict]) -> list[dict]:
    """Drop the oldest answer/feedback rounds (never the samples) when the history gets long."""
    msgs = list(messages)
    while len(msgs) > 3 and sum(len(m["content"]) for m in msgs) > HISTORY_CHARS:
        del msgs[1:3]
    return msgs


def _record(conn: sqlite3.Connection, nid: int, **cols: object) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE regex_negotiations SET {sets} WHERE id = ?", (*cols.values(), nid))


def negotiate(ctx: AppContext, kind: str) -> str:
    """Ask the model for regexes from up to 10 missed emails, and keep going until one holds up.

    After every reply each proposal is tested against the same emails. If none passes, the exact
    failures go back to the model and it answers again, up to MAX_REPLIES model replies. Passing
    regexes are saved as proposals (they still need approval). Returns a one-line result; the
    full conversation is kept in regex_negotiations for the Parsers page and dashboard.
    """
    if kind not in ("bank", "amazon"):
        raise ValueError(kind)
    with ctx.db() as conn:
        if conn.execute("SELECT 1 FROM learned_parsers WHERE kind = ? AND status = 'proposed'", (kind,)).fetchone():
            return "Approve or reject the pending proposals first."
        samples = pick_samples(conn, kind)
        if not samples:
            return "No missed emails to learn from."
        conn.execute(  # a run that died with the process can't still be going: only one job runs at a time
            "UPDATE regex_negotiations SET status = 'failed', last_error = 'Interrupted (the hub restarted).',"
            " finished_at = datetime('now') WHERE kind = ? AND status = 'running'", (kind,))
        nid = conn.execute(
            "INSERT INTO regex_negotiations(kind, samples) VALUES (?, ?)", (kind, len(samples))).lastrowid
    blocks = "\n\n".join(
        _sample_block(i, s["subject"], s["body"], 3000 if kind == "amazon" else 2500, kind == "amazon")
        for i, s in enumerate(samples, 1)
    )
    schema, system, tokens = (
        (BankProposals, BANK_SYSTEM, 900) if kind == "bank" else (AmazonProposal, AMAZON_SYSTEM, 700)
    )
    need = min(2, len(samples))
    messages: list[dict] = [{"role": "user", "content": blocks}]
    feedback = ""
    for reply in range(1, MAX_REPLIES + 1):
        try:
            got = ctx.ai.extract(_trim(messages), schema=schema, system=system, max_tokens=tokens)
            answer = got.model_dump_json()
            candidates = (
                [(p.name, p.pattern, None, p.direction, p.instrument) for p in got.parsers]
                if kind == "bank"
                else [(got.name, got.total_pattern, got.item_pattern, None, None)]
            )
            verdicts = []
            with ctx.db() as conn:
                verdicts = [_evaluate(kind, c, samples, conn, need) for c in candidates]
                saved = 0
                for v in verdicts:
                    if v.ok:
                        name, pattern, item_pattern, direction, instrument, hits, preview = v.row
                        conn.execute(
                            "INSERT INTO learned_parsers(kind, name, pattern, item_pattern, direction, instrument,"
                            " samples, matched, preview) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (kind, unique_name(conn, kind, name), pattern, item_pattern, direction, instrument,
                             len(samples), hits, preview),
                        )
                        saved += 1
            if not candidates:
                feedback = "You returned no patterns. Write at least one pattern."
            else:
                feedback = "\n".join(f"- {n}" for v in verdicts if not v.ok for n in v.notes)
        except ExtractionError as e:  # the model's answer wasn't usable JSON, even after one retry
            answer, saved = "", 0
            feedback = f"Your reply could not be read as the required JSON ({e}). Answer again in that format."
        except AIError as e:
            with ctx.db() as conn:
                _record(conn, nid, status="unavailable", replies=reply - 1, last_error=str(e),
                        transcript=json.dumps(messages), finished_at=_now())
            return f"Local model not available: {e}"
        if saved:
            messages.append({"role": "assistant", "content": answer})
            with ctx.db() as conn:
                _record(conn, nid, status="succeeded", replies=reply, last_error=None,
                        transcript=json.dumps(messages), finished_at=_now())
                conn.execute(  # the problem is solved: older failures for this source no longer matter
                    "UPDATE regex_negotiations SET dismissed = 1 WHERE kind = ? AND id != ?"
                    " AND status IN ('failed', 'unavailable')", (kind, nid))
            ctx.log.info("regex compiler (%s): %d proposal(s) after %d repl%s", kind, saved, reply,
                         "y" if reply == 1 else "ies")
            return f"{saved} regex proposal(s) ready to review, tested on {len(samples)} emails ({reply} model repl{'y' if reply == 1 else 'ies'})."
        if answer:
            messages.append({"role": "assistant", "content": answer})
        messages.append({
            "role": "user",
            "content": "None of that worked:\n" + feedback + "\nFix these problems and answer again in the same format.",
        })
        with ctx.db() as conn:
            _record(conn, nid, replies=reply, last_error=feedback, transcript=json.dumps(messages))
    with ctx.db() as conn:
        _record(conn, nid, status="failed", replies=MAX_REPLIES, transcript=json.dumps(messages),
                finished_at=_now())
    ctx.log.warning("regex compiler (%s): gave up after %d replies", kind, MAX_REPLIES)
    return f"The model could not produce a working regex in {MAX_REPLIES} replies."


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")  # same clock as SQLite's datetime('now')


def propose(ctx: AppContext, kind: str) -> str:
    """Kept for callers that want the result inline; same as negotiate."""
    return negotiate(ctx, kind)


def clear_covered(conn: sqlite3.Connection, kind: str) -> int:
    """Drop misses that the regexes (now including approved ones) read, e.g. after an approval.

    Emails the model already handled are not re-processed, so this is how they leave the log.
    """
    from .parsers import parse_email

    table = "emails" if kind == "bank" else "order_emails"
    bank = [r.as_parser() for r in bank_rules(conn)] if kind == "bank" else []
    rules = amazon_rules(conn) if kind == "amazon" else []
    cleared = 0
    for r in conn.execute(
        f"SELECT e.gmail_id, e.subject, e.body FROM parse_misses m JOIN {table} e ON e.gmail_id = m.gmail_id"
        " WHERE m.kind = ?", (kind,)
    ).fetchall():
        if kind == "bank":
            covered = parse_email(r["subject"], r["body"], bank) is not None
        else:
            o = amazon.parse_email(r["subject"], r["body"], rules)
            covered = isinstance(o, amazon.ParsedOrder) and o.parser == "body" and o.total_paise is not None
        if covered:
            clear_miss(conn, kind, r["gmail_id"])
            cleared += 1
    return cleared
