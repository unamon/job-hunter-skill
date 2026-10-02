"""DB engine + session factory + migration runner.

SQLAlchemy/SQLModel handles ORM operations. Migrations bypass it and use
raw `sqlite3.executescript()` because pysqlite's transactional DDL behavior
is unreliable for multi-statement migration files (CREATE TABLE followed by
CREATE INDEX on the same table can fail to see the table).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlmodel import Session

from .paths import Paths

MIGRATIONS_PACKAGE = "job_hunter.migrations"


def _engine_for(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(text("PRAGMA journal_mode = WAL"))
    return engine


def get_engine(paths: Paths) -> Engine:
    return _engine_for(paths.db_path)


@contextmanager
def session(paths: Paths) -> Iterator[Session]:
    eng = get_engine(paths)
    with Session(eng) as s:
        yield s


def _list_migrations() -> list[tuple[str, str]]:
    """Return (name, sql) tuples in lexical order."""
    out: list[tuple[str, str]] = []
    files = resources.files(MIGRATIONS_PACKAGE)
    for entry in sorted(p.name for p in files.iterdir() if p.name.endswith(".sql")):
        sql = (files / entry).read_text(encoding="utf-8")
        out.append((entry, sql))
    return out


def run_migrations(paths: Paths) -> list[str]:
    """Apply any unapplied migrations. Returns names. Idempotent.

    Uses raw sqlite3 + executescript() to avoid pysqlite DDL-transactional quirks.
    """
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(paths.db_path), isolation_level=None)  # autocommit
    applied_now: list[str] = []
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "name TEXT PRIMARY KEY, "
            "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        already = {row[0] for row in conn.execute("SELECT name FROM _migrations").fetchall()}
        for name, sql in _list_migrations():
            if name in already:
                continue
            conn.executescript(sql)
            conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
            applied_now.append(name)
    finally:
        conn.close()
    return applied_now
