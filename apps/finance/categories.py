"""User-managed categories: naming, matching, CRUD, and applying them to transactions.

Pure database logic with no AI. categorise.py asks the model *which* category;
everything that changes data lives here so the rules are enforced in one place.
"""

from __future__ import annotations

import re
import sqlite3

from . import tagging

MAX_NAME = 40
RESERVED = {"uncategorised", "uncategorized", "none", "n/a"}


class CategoryError(ValueError):
    """A category name or operation the user (or model) can't be allowed."""


def normalise_name(raw: str | None) -> str:
    """Tidy a proposed name; raise CategoryError if it can't be a category."""
    name = re.sub(r"\s+", " ", (raw or "")).strip(" .,:;-\"'")
    if not name:
        raise CategoryError("category name is empty")
    if len(name) > MAX_NAME:
        raise CategoryError(f"category name is longer than {MAX_NAME} characters")
    if name.casefold() in RESERVED:
        raise CategoryError(f"'{name}' is not a usable category name")
    if name.islower() or name.isupper():
        name = name.title()
    return name


def _stem(name: str) -> str:
    """Casefold and drop a plural, so 'Grocery' and 'Groceries' compare equal.

    Deliberately not fuzzy: a looser match would merge different words
    ('Shopping' / 'Shipping'). A harmless near-duplicate can be merged by hand.
    """
    words = []
    for w in re.sub(r"[^a-z0-9& ]+", " ", name.casefold()).split():
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        words.append(w)
    return " ".join(words)


def find(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """The existing category a name refers to: exact (any case), else same word ignoring plurals."""
    rows = conn.execute("SELECT * FROM categories").fetchall()
    key = name.strip().casefold()
    for r in rows:
        if r["name"].casefold() == key:
            return r
    stem = _stem(name)
    return next((r for r in rows if _stem(r["name"]) == stem), None)


def get(conn: sqlite3.Connection, category_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()


def listing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM categories ORDER BY counts_as_spend DESC, name").fetchall()


def create(
    conn: sqlite3.Connection,
    name: str,
    counts_as_spend: bool = True,
    description: str | None = None,
    created_by: str = "user",
) -> tuple[int, bool]:
    """Create a category, or return the existing one. -> (id, created)."""
    name = normalise_name(name)
    existing = find(conn, name)
    if existing:
        return existing["id"], False
    cur = conn.execute(
        "INSERT INTO categories(name, counts_as_spend, description, created_by) VALUES (?, ?, ?, ?)",
        (name, 1 if counts_as_spend else 0, (description or "").strip() or None, created_by),
    )
    return cur.lastrowid, True


def update(
    conn: sqlite3.Connection,
    category_id: int,
    name: str,
    description: str | None,
    counts_as_spend: bool,
) -> None:
    name = normalise_name(name)
    clash = conn.execute(
        "SELECT id FROM categories WHERE name = ? COLLATE NOCASE AND id != ?", (name, category_id)
    ).fetchone()
    if clash:
        raise CategoryError(f"a category called '{name}' already exists; merge them instead")
    conn.execute(
        "UPDATE categories SET name = ?, description = ?, counts_as_spend = ? WHERE id = ?",
        (name, (description or "").strip() or None, 1 if counts_as_spend else 0, category_id),
    )


def merge(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Move everything from `source` into `target`, then delete `source`."""
    if source_id == target_id:
        raise CategoryError("pick a different category to merge into")
    if not get(conn, source_id) or not get(conn, target_id):
        raise CategoryError("no such category")
    conn.execute("UPDATE transactions SET category_id = ? WHERE category_id = ?", (target_id, source_id))
    conn.execute("UPDATE recipients SET category_id = ? WHERE category_id = ?", (target_id, source_id))
    conn.execute("DELETE FROM categories WHERE id = ?", (source_id,))


def delete(conn: sqlite3.Connection, category_id: int) -> None:
    """Delete a category. Its transactions and recipients become uncategorised.

    The foreign keys null out category_id; rows that were hand-categorised go back to
    following their recipient so they aren't stuck as 'manual' with no category.
    """
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.execute("UPDATE transactions SET category_manual = 0 WHERE category_id IS NULL AND category_manual = 1")


def stats(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per-category usage: transaction count and total debit paise."""
    rows = conn.execute(
        "SELECT category_id, COUNT(*) AS n,"
        " COALESCE(SUM(CASE WHEN direction = 'debit' THEN amount_paise END), 0) AS total"
        " FROM transactions WHERE category_id IS NOT NULL GROUP BY category_id"
    ).fetchall()
    return {r["category_id"]: {"n": r["n"], "total": r["total"]} for r in rows}


# -- applying a category to data -------------------------------------------------------


def assign_to_transaction(
    conn: sqlite3.Connection, txn_id: int, category_id: int, narration: str | None
) -> None:
    conn.execute(
        "UPDATE transactions SET category_id = ?, category_manual = 1, narration = ? WHERE id = ?",
        (category_id, (narration or "").strip() or None, txn_id),
    )


def teach_recipient(conn: sqlite3.Connection, txn_id: int, category_id: int) -> str | None:
    """Give the transaction's recipient this category if it has none yet.

    Returns the recipient's name when it was taught, else None. A recipient that
    already has a category is never changed, and hand-set transactions are skipped
    by retag_recipient.
    """
    row = conn.execute(
        "SELECT r.id, r.name, r.category_id FROM transactions t JOIN recipients r ON r.id = t.recipient_id"
        " WHERE t.id = ?",
        (txn_id,),
    ).fetchone()
    if not row or row["category_id"] is not None:
        return None
    conn.execute("UPDATE recipients SET category_id = ? WHERE id = ?", (category_id, row["id"]))
    tagging.retag_recipient(conn, row["id"])
    return row["name"]
