"""Counterparty normalisation and recipient matching.

A transaction's counterparty_key is what recipients are matched on: the
lower-cased UPI id when there is one, otherwise a cleaned merchant name.
Tagging a key re-tags every past transaction with it, and future ones pick
it up during sync.
"""

from __future__ import annotations

import re
import sqlite3

VPA_RE = re.compile(r"[\w.\-]+@[\w.\-]+")
_LEADING = {"pos", "upi", "ecom", "neft", "imps", "rtgs", "to", "from", "by", "towards", "ach", "nach"}
_TRAILING = {"in", "ind", "india", "pvt", "ltd", "private", "limited", "llp"}


def counterparty_key(raw: str | None, instrument: str | None = None) -> tuple[str | None, str | None]:
    """-> (key, kind) where kind is 'upi' or 'merchant'; (None, None) if nothing usable."""
    if instrument == "atm":
        return "atm", "merchant"
    if not raw:
        return None, None
    m = VPA_RE.search(raw)
    if m:
        return m.group(0).lower(), "upi"
    tokens = re.sub(r"[^a-z0-9& ]+", " ", raw.lower()).split()
    while tokens and (tokens[0] in _LEADING or tokens[0].isdigit()):
        tokens.pop(0)
    while tokens and (tokens[-1] in _TRAILING or tokens[-1].isdigit()):
        tokens.pop()
    key = " ".join(tokens)
    return (key, "merchant") if key else (None, None)


def lookup(conn: sqlite3.Connection, key: str | None) -> sqlite3.Row | None:
    """The recipient (id, category_id) owning this key, if any."""
    if not key:
        return None
    return conn.execute(
        "SELECT r.id, r.category_id FROM recipient_keys k JOIN recipients r ON r.id = k.recipient_id"
        " WHERE k.key = ?",
        (key,),
    ).fetchone()


def retag_key(conn: sqlite3.Connection, key: str) -> int:
    """Re-apply whatever recipient owns `key` to every transaction with that key."""
    owner = lookup(conn, key)
    rid = owner["id"] if owner else None
    cat = owner["category_id"] if owner else None
    cur = conn.execute(
        "UPDATE transactions SET recipient_id = ?,"
        " category_id = CASE WHEN category_manual = 1 THEN category_id ELSE ? END"
        " WHERE counterparty_key = ?",
        (rid, cat, key),
    )
    return cur.rowcount


def retag_recipient(conn: sqlite3.Connection, recipient_id: int) -> int:
    """After a recipient's category changes, push it to its (non-manual) transactions."""
    cur = conn.execute(
        "UPDATE transactions SET category_id = (SELECT category_id FROM recipients WHERE id = ?)"
        " WHERE recipient_id = ? AND category_manual = 0",
        (recipient_id, recipient_id),
    )
    return cur.rowcount


def find_or_create_recipient(conn: sqlite3.Connection, name: str, category_id: int | None) -> int:
    row = conn.execute("SELECT id, category_id FROM recipients WHERE name = ?", (name,)).fetchone()
    if row:
        if category_id is not None and category_id != row["category_id"]:
            conn.execute("UPDATE recipients SET category_id = ? WHERE id = ?", (category_id, row["id"]))
            retag_recipient(conn, row["id"])
        return row["id"]
    return conn.execute(
        "INSERT INTO recipients(name, category_id) VALUES (?, ?)", (name, category_id)
    ).lastrowid


def assign_key(conn: sqlite3.Connection, key: str, kind: str, recipient_id: int) -> int:
    """Give `key` to a recipient (moving it if another owned it) and back-fill transactions."""
    conn.execute(
        "INSERT INTO recipient_keys(key, kind, recipient_id) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET recipient_id = excluded.recipient_id, kind = excluded.kind",
        (key, kind, recipient_id),
    )
    return retag_key(conn, key)


def remove_key(conn: sqlite3.Connection, key_id: int) -> None:
    row = conn.execute("SELECT key FROM recipient_keys WHERE id = ?", (key_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM recipient_keys WHERE id = ?", (key_id,))
        retag_key(conn, row["key"])
