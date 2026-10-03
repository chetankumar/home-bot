"""Turn a narration ("weekly vegetables from the market") into a category.

The local model sees the narration, a masked description of the transaction and
your existing categories (with their descriptions), and either picks one or
proposes a new name. The model's `is_new` flag is not trusted: the name is always
re-resolved against your categories here, so a "new" category that already exists
is a match, never a duplicate. New categories are only ever *proposed*; creating one
is a separate, explicit step (category_routes.py).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator

from hub.plugin import AppContext
from hub.services.ai import AIError

from . import categories
from .parsers import mask

MAX_NARRATION = 500

SYSTEM = (
    "You file a household's spending into categories. The user describes a spend in their own "
    "words; choose the category it belongs to.\n"
    "- Prefer one of the existing categories, using its exact name. Read each category's "
    "description.\n"
    "- Only if no existing category reasonably fits, propose a NEW category: a short, general "
    "name of one or two words (for example 'Pets', not 'Dog food'), and set is_new to true.\n"
    "- counts_as_spend is false only for moving the user's own money (transfers between their "
    "accounts, credit-card bill payments). It is true for everything else.\n"
    "- Reply with the category name only in the category field."
)


class CategoryChoice(BaseModel):
    category: str = Field(description="Exact name of an existing category, or a new short name")
    is_new: bool = Field(description="True only if this is not one of the existing categories")
    counts_as_spend: bool = Field(default=True, description="False only for moving your own money")

    @field_validator("category")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("category must not be blank")
        return v


@dataclass
class Matched:
    category_id: int
    name: str


@dataclass
class Proposal:
    name: str
    counts_as_spend: bool


@dataclass
class Failed:
    reason: str


Decision = Matched | Proposal | Failed


def clean_narration(raw: str | None) -> str:
    return " ".join((raw or "").split())[:MAX_NARRATION]


def transaction_context(conn: sqlite3.Connection, txn_id: int) -> str:
    t = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if t is None:
        return ""
    rupees = t["amount_paise"] / 100
    parts = [
        f"{t['direction']} of Rs {rupees:,.2f} on {t['occurred_at'][:10]} via {t['instrument'].replace('_', ' ')}",
    ]
    if t["counterparty_raw"]:
        parts.append(f"counterparty: {mask(t['counterparty_raw'])}")
    return "; ".join(parts)


def recipient_context(conn: sqlite3.Connection, key: str, name: str) -> str:
    rows = conn.execute(
        "SELECT counterparty_raw, amount_paise, direction FROM transactions"
        " WHERE counterparty_key = ? ORDER BY occurred_at DESC LIMIT 5",
        (key,),
    ).fetchall()
    raw = next((r["counterparty_raw"] for r in rows if r["counterparty_raw"]), key)
    spends = ", ".join(f"Rs {r['amount_paise'] / 100:,.0f}" for r in rows if r["direction"] == "debit")
    return f"recipient: {name} ({mask(raw)}); recent payments: {spends or 'none'}"


def _category_lines(conn: sqlite3.Connection) -> str:
    lines = []
    for c in categories.listing(conn):
        note = "" if c["counts_as_spend"] else " [not spending: moves your own money]"
        desc = f": {c['description']}" if c["description"] else ""
        lines.append(f"- {c['name']}{desc}{note}")
    return "\n".join(lines)


def decide(ctx: AppContext, conn: sqlite3.Connection, narration: str, context: str) -> Decision:
    """Ask the local model for a category. Never raises for model problems: returns Failed."""
    narration = clean_narration(narration)
    if not narration:
        return Failed("Write a short description of the spend first.")
    prompt = (
        f"Existing categories:\n{_category_lines(conn)}\n\n"
        f"Transaction: {context}\n"
        f"The user says: \"{narration}\"\n\n"
        "Which category does this belong to?"
    )
    try:
        choice = ctx.ai.extract(prompt, schema=CategoryChoice, system=SYSTEM, max_tokens=200)
    except AIError as e:
        ctx.log.info("categorise: model unavailable: %s", e)
        return Failed(f"The local model couldn't be used ({e}). Pick a category manually.")
    try:
        name = categories.normalise_name(choice.category)
    except categories.CategoryError as e:
        return Failed(f"The model suggested an unusable category ({e}). Pick one manually.")
    existing = categories.find(conn, name)
    if existing:
        return Matched(existing["id"], existing["name"])
    return Proposal(name, choice.counts_as_spend)
