import sqlite3

import pytest

from hub.services.db import Database, apply_migrations, connect, migration_files


def test_migrations_apply_in_numeric_order(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    # Lexical order would run 10 before 2.
    (mig / "10_add_col.sql").write_text("ALTER TABLE t ADD COLUMN b TEXT;")
    (mig / "2_create.sql").write_text("CREATE TABLE t (a TEXT);")
    (mig / "1_first.sql").write_text("CREATE TABLE log (x);")
    (mig / "notes.txt").write_text("ignored")
    assert [p.name for p in migration_files(mig)] == ["1_first.sql", "2_create.sql", "10_add_col.sql"]
    db = Database(tmp_path / "x.db", mig)
    assert db.applied == ["1_first.sql", "2_create.sql", "10_add_col.sql"]
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    assert cols == ["a", "b"]


def test_migrations_are_idempotent(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_create.sql").write_text("CREATE TABLE t (a TEXT);")
    assert Database(tmp_path / "x.db", mig).applied == ["001_create.sql"]
    assert Database(tmp_path / "x.db", mig).applied == []
    (mig / "002_more.sql").write_text("CREATE TABLE u (a TEXT);")
    assert Database(tmp_path / "x.db", mig).applied == ["002_more.sql"]


def test_failed_migration_rolls_back(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_bad.sql").write_text("CREATE TABLE t (a TEXT); INSERT INTO nope VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        Database(tmp_path / "x.db", mig)
    conn = connect(tmp_path / "x.db")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 't'").fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM _migrations").fetchone()[0] == 0


def test_duplicate_numbers_rejected(tmp_path):
    mig = tmp_path / "m"
    mig.mkdir()
    (mig / "001_a.sql").write_text("")
    (mig / "1_b.sql").write_text("")
    with pytest.raises(ValueError):
        migration_files(mig)


def test_connection_context_commits_and_rolls_back(tmp_path):
    db = Database(tmp_path / "x.db")
    with db() as conn:
        conn.execute("CREATE TABLE t (a)")
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError):
        with db() as conn:
            conn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    assert apply_migrations(connect(tmp_path / "x.db"), tmp_path / "missing") == []
