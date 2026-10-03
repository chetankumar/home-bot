"""Read queries shared by the page routers."""

from __future__ import annotations

import sqlite3


def txn_rows(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[dict]:
    rows = conn.execute(
        "SELECT t.*, c.name AS category, r.name AS recipient, c.counts_as_spend"
        " FROM transactions t LEFT JOIN categories c ON c.id = t.category_id"
        " LEFT JOIN recipients r ON r.id = t.recipient_id"
        f" WHERE {where} ORDER BY t.occurred_at DESC, t.id DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def recipient_names(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM recipients ORDER BY name").fetchall()
