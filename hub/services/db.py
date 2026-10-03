"""SQLite: one file per app plus hub.db, with a plain .sql migration runner."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATION_RE = re.compile(r"^(\d+)_[\w-]+\.sql$")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def migration_files(migrations_dir: Path) -> list[Path]:
    """NNN_name.sql files, ordered by their numeric prefix (not lexically)."""
    if not migrations_dir.is_dir():
        return []
    found = []
    for p in migrations_dir.iterdir():
        m = MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    numbers = [n for n, _ in found]
    if len(numbers) != len(set(numbers)):
        raise ValueError(f"duplicate migration numbers in {migrations_dir}")
    return [p for _, p in sorted(found)]


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """Apply unapplied migrations in order; returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations ("
        " name TEXT PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    done = {r[0] for r in conn.execute("SELECT name FROM _migrations")}
    applied = []
    for path in migration_files(migrations_dir):
        if path.name in done:
            continue
        script = path.read_text()
        # executescript commits first; wrap in an explicit transaction so a
        # failing migration leaves nothing half-applied.
        try:
            conn.executescript(
                "BEGIN;\n"
                + script
                + f"\n;INSERT INTO _migrations(name) VALUES ('{path.name}');\nCOMMIT;"
            )
        except Exception:
            conn.rollback()
            raise
        applied.append(path.name)
    return applied


class Database:
    """A connection factory for one SQLite file."""

    def __init__(self, path: Path, migrations_dir: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.applied: list[str] = []
        if migrations_dir is not None:
            conn = connect(self.path)
            try:
                self.applied = apply_migrations(conn, migrations_dir)
            finally:
                conn.close()

    @contextmanager
    def __call__(self) -> Iterator[sqlite3.Connection]:
        """`with db() as conn:` commits on success, rolls back on error, always closes."""
        conn = connect(self.path)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    connect = __call__

    def migrations(self) -> list[str]:
        """Names of every migration applied to this file, oldest first."""
        with self() as conn:
            try:
                return [r[0] for r in conn.execute("SELECT name FROM _migrations ORDER BY rowid")]
            except sqlite3.OperationalError:  # no migrations folder, so no table
                return []


# Apps get the same thing; the alias documents intent in type hints.
AppDB = Database

HUB_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def hub_database(data_dir: Path) -> Database:
    return Database(data_dir / "hub.db", HUB_MIGRATIONS)
