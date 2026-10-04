"""Regex compiler: ask the local model for regexes from emails the built-in ones missed.

Flow: sync records every email the built-in regexes could not read (`parse_misses`).
`propose()` takes up to 10 of them, masks them, asks the model for regexes, checks each
regex against those same emails, and stores the survivors as `proposed`. Nothing is used
until the user approves it on the Parsers page; approved regexes run after the built-in
ones, so they can only fill gaps.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from hub.plugin import AppContext
from hub.services.ai import AIError

from . import amazon
from .parsers import Builder, Parsed, _base, clean_name, collapse, mask, to_paise

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

    def as_parser(self) -> tuple[str, re.Pattern[str], Builder]:
        direction, instrument = self.direction, self.instrument

        def build(m: re.Match[str], text: str) -> Parsed:
            g = m.groupdict()
            cp = " ".join(x for x in (g.get("vpa"), clean_name(g.get("merchant"))) if x) or None
            return _base(m, direction, instrument, cp, text)

        return f"learned:{self.name}", self.rx, build


@dataclass
class AmazonRule:
    id: int
    name: str
    total: re.Pattern[str] | None
    items: re.Pattern[str] | None


def _row_rules(conn: sqlite3.Connection, kind: str, status: str = "active") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM learned_parsers WHERE kind = ? AND status = ? ORDER BY id", (kind, status)
    ).fetchall()


def bank_rules(conn: sqlite3.Connection, status: str = "active") -> list[BankRule]:
    out = []
    for r in _row_rules(conn, "bank", status):
        rx = compile_pattern(r["pattern"], {"amount"})
        if rx and r["direction"] and r["instrument"]:
            out.append(BankRule(r["id"], r["name"], rx, r["direction"], r["instrument"]))
    return out


def amazon_rules(conn: sqlite3.Connection, status: str = "active") -> list[AmazonRule]:
    out = []
    for r in _row_rules(conn, "amazon", status):
        total = compile_pattern(r["pattern"], {"total"})
        items = compile_pattern(r["item_pattern"], {"title"}, multiline=True)
        if total or items:
            out.append(AmazonRule(r["id"], r["name"], total, items))
    return out


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


def _bank_check(rule: BankRule, samples: list[sqlite3.Row], conn: sqlite3.Connection) -> tuple[int, str | None]:
    """How many samples the rule reads (agreeing with the model's earlier reading, if any)."""
    hits, preview = 0, None
    _name, rx, build = rule.as_parser()
    for s in samples:
        text = collapse(s["body"])
        m = rx.search(text)
        if not m:
            continue
        try:
            p = build(m, text)
        except ValueError:
            continue
        known = _known_amount(conn, s["gmail_id"])
        if p.amount_paise <= 0 or (known is not None and known != p.amount_paise):
            continue
        hits += 1
        preview = preview or json.dumps(
            {"subject": s["subject"], "amount": p.amount_paise / 100, "direction": p.direction,
             "instrument": p.instrument, "account": p.account_mask, "counterparty": p.counterparty_raw,
             "date": p.occurred_on.isoformat() if p.occurred_on else None})
    return hits, preview


def _amazon_check(rule: AmazonRule, samples: list[sqlite3.Row]) -> tuple[int, str | None]:
    hits, preview = 0, None
    for s in samples:
        total = rule.total.search(collapse(s["body"])) if rule.total else None
        items = list(amazon_items(rule, s["body"]))
        try:
            paise = to_paise(total.group("total")) if total else None
        except ValueError:
            paise = None
        if paise or items:
            hits += 1
            preview = preview or json.dumps(
                {"subject": s["subject"], "total": paise / 100 if paise else None, "items": [i.title for i in items][:5]})
    return hits, preview


def amazon_items(rule: AmazonRule, body: str) -> list[amazon.OrderItem]:
    out: list[amazon.OrderItem] = []
    if not rule.items:
        return out
    for m in rule.items.finditer(body):
        g = m.groupdict()
        title = clean_name(g.get("title"))
        if not title or len(title) < 3:
            continue
        try:
            price = to_paise(g["price"]) if g.get("price") else None
            qty = int(g["qty"]) if g.get("qty") else 1
        except ValueError:
            price, qty = None, 1
        out.append(amazon.OrderItem(title[:300], max(qty, 1), price))
    return out[:30]


def propose(ctx: AppContext, kind: str) -> str:
    """Ask the model for regexes from up to 10 missed emails. Returns a one-line result."""
    if kind not in ("bank", "amazon"):
        raise ValueError(kind)
    with ctx.db() as conn:
        if conn.execute("SELECT 1 FROM learned_parsers WHERE kind = ? AND status = 'proposed'", (kind,)).fetchone():
            return "Approve or reject the pending proposals first."
        samples = pick_samples(conn, kind)
    if not samples:
        return "No missed emails to learn from."
    blocks = "\n\n".join(
        _sample_block(i, s["subject"], s["body"], 3000 if kind == "amazon" else 2500, kind == "amazon")
        for i, s in enumerate(samples, 1)
    )
    try:
        if kind == "bank":
            got = ctx.ai.extract(blocks, schema=BankProposals, system=BANK_SYSTEM, max_tokens=900)
            candidates = [(p.name, p.pattern, None, p.direction, p.instrument) for p in got.parsers]
        else:
            got = ctx.ai.extract(blocks, schema=AmazonProposal, system=AMAZON_SYSTEM, max_tokens=700)
            candidates = [(got.name, got.total_pattern, got.item_pattern, None, None)]
    except AIError as e:
        return f"Local model not available: {e}"
    need = min(2, len(samples))
    saved, rejected = 0, 0
    with ctx.db() as conn:
        for name, pattern, item_pattern, direction, instrument in candidates:
            name = re.sub(r"\W+", "_", name.strip().lower()).strip("_")[:40] or kind
            if kind == "bank":
                rx = compile_pattern(pattern, {"amount"})
                if rx is None or instrument not in INSTRUMENTS:
                    rejected += 1
                    continue
                hits, preview = _bank_check(BankRule(0, name, rx, direction, instrument), samples, conn)
            else:
                total = compile_pattern(pattern, {"total"})
                items = compile_pattern(item_pattern, {"title"}, multiline=True)
                if not (total or items):
                    rejected += 1
                    continue
                hits, preview = _amazon_check(AmazonRule(0, name, total, items), samples)
                pattern = pattern if total else ""
                item_pattern = item_pattern if items else None
            if hits < need:
                rejected += 1
                continue
            conn.execute(
                "INSERT INTO learned_parsers(kind, name, pattern, item_pattern, direction, instrument,"
                " samples, matched, preview) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, name, pattern, item_pattern, direction, instrument, len(samples), hits, preview),
            )
            saved += 1
    ctx.log.info("regex compiler (%s): %d proposed, %d rejected from %d emails", kind, saved, rejected, len(samples))
    if not saved:
        return f"The model's regexes didn't hold up on the {len(samples)} sample emails ({rejected} rejected)."
    return f"{saved} regex proposal(s) ready to review, tested on {len(samples)} emails."


def clear_covered(conn: sqlite3.Connection, kind: str) -> int:
    """Drop misses that the regexes (now including approved ones) read, e.g. after an approval.

    Emails the model already handled are not re-processed, so this is how they leave the log.
    """
    from .parsers import parse_email

    table = "emails" if kind == "bank" else "order_emails"
    extra = [r.as_parser() for r in bank_rules(conn)] if kind == "bank" else []
    rules = amazon_rules(conn) if kind == "amazon" else []
    cleared = 0
    for r in conn.execute(
        f"SELECT e.gmail_id, e.subject, e.body FROM parse_misses m JOIN {table} e ON e.gmail_id = m.gmail_id"
        " WHERE m.kind = ?", (kind,)
    ).fetchall():
        if kind == "bank":
            covered = parse_email(r["subject"], r["body"], extra) is not None
        else:
            o = amazon.parse_email(r["subject"], r["body"], rules)
            covered = isinstance(o, amazon.ParsedOrder) and o.parser == "body" and o.total_paise is not None
        if covered:
            clear_miss(conn, kind, r["gmail_id"])
            cleared += 1
    return cleared
