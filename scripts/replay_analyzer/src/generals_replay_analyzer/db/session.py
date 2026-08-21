"""File-backed SQLite engine and session construction."""

from __future__ import annotations

from pathlib import Path
from sqlite3 import Connection as SQLiteConnection
from typing import Any

from sqlalchemy import URL, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

SQLITE_BUSY_TIMEOUT_MS = 5_000


def _validated_database_path(database_path: Path) -> Path:
    raw = str(database_path)
    if not database_path.is_absolute():
        raise ValueError("database path must be absolute")
    if raw == ":memory:" or raw.startswith("file:") or "://" in raw or "?" in raw:
        raise ValueError("database path must identify a plain file")
    if not database_path.parent.is_dir():
        raise ValueError("database parent directory must already exist")
    if database_path.exists() and not database_path.is_file():
        raise ValueError("database path must not be a directory or special file")
    return database_path.resolve(strict=False)


def _configure_sqlite_connection(dbapi_connection: SQLiteConnection, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        journal_mode = str(cursor.fetchone()[0]).lower()
        cursor.execute("PRAGMA foreign_keys")
        foreign_keys = int(cursor.fetchone()[0])
        cursor.execute("PRAGMA busy_timeout")
        busy_timeout = int(cursor.fetchone()[0])
    finally:
        cursor.close()
    if foreign_keys != 1 or busy_timeout != SQLITE_BUSY_TIMEOUT_MS or journal_mode != "wal":
        raise RuntimeError("SQLite connection policy could not be established")


# TheSuperHackers @feature Leex 21/08/2026 Enforce WAL, foreign keys, and bounded contention per analyzer engine. (#TBD)
def create_database_engine(database_path: Path) -> Engine:
    """Create a pooled SQLAlchemy engine for one existing-parent file path."""
    validated_path = _validated_database_path(Path(database_path))
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(validated_path)),
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create nonexpiring application sessions bound to one configured engine."""
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
