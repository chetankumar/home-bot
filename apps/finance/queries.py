"""Read queries shared by the page routers."""

from __future__ import annotations

import sqlite3


def order_summary(conn: sqlite3.Connection, order_id: int | None, limit: int = 3, width: int = 60) -> str:
    """'Item one, Item two +1 more' for an order, or '' if there is none."""
    if not order_id:
        return ""
    items = conn.execute("SELECT title FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
    titles = [(i["title"][: width - 1] + "…") if len(i["title"]) > width else i["title"] for i in items[:limit]]
    more = len(items) - limit
    return ", ".join(titles) + (f" +{more} more" if more > 0 else "")


def txn_rows(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[dict]:
    rows = conn.execute(
        "SELECT t.*, c.name AS category, r.name AS recipient, c.counts_as_spend,"
        " o.order_number AS order_number, o.status AS order_status"
        " FROM transactions t LEFT JOIN categories c ON c.id = t.category_id"
        " LEFT JOIN recipients r ON r.id = t.recipient_id"
        " LEFT JOIN orders o ON o.id = t.order_id"
        f" WHERE {where} ORDER BY t.occurred_at DESC, t.id DESC",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["order_summary"] = order_summary(conn, d["order_id"])
        out.append(d)
    return out


def recipient_names(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM recipients ORDER BY name").fetchall()
