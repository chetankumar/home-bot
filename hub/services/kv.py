"""Namespaced key/value settings per app, stored as JSON in hub.db."""

from __future__ import annotations

import json
from typing import Any

from hub.services.db import Database

_MISSING = object()


class KV:
    def __init__(self, db: Database, app_id: str):
        self._db = db
        self.app_id = app_id

    def get(self, key: str, default: Any = None) -> Any:
        with self._db() as conn:
            row = conn.execute(
                "SELECT value FROM kv WHERE app_id = ? AND key = ?", (self.app_id, key)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO kv(app_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(app_id, key) DO UPDATE SET value = excluded.value,"
                " updated_at = datetime('now')",
                (self.app_id, key, json.dumps(value)),
            )

    def delete(self, key: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM kv WHERE app_id = ? AND key = ?", (self.app_id, key))

    def all(self) -> dict[str, Any]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM kv WHERE app_id = ? ORDER BY key", (self.app_id,)
            ).fetchall()
        return {k: json.loads(v) for k, v in rows}
