"""SQLite path and per-connection policy tests."""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.session import SQLITE_BUSY_TIMEOUT_MS


@pytest.mark.parametrize(
    "target",
    [
        Path("relative.sqlite3"),
        Path(":memory:"),
        Path("file:library.sqlite3?mode=rwc"),
        Path("C:/file://library.sqlite3"),
    ],
)
def test_database_engine_rejects_non_file_targets(target: Path) -> None:
    """Catch accidental checkout-relative, in-memory, and URI SQLite targets."""
    with pytest.raises(ValueError):
        create_database_engine(target)


def test_database_engine_rejects_directory_and_missing_parent(tmp_path: Path) -> None:
    """Catch implicit directory creation and attempts to open directories as databases."""
    with pytest.raises(ValueError):
        create_database_engine(tmp_path)
    with pytest.raises(ValueError):
        create_database_engine(tmp_path / "missing" / "library.sqlite3")


def test_each_connection_enforces_sqlite_policy(database_path: Path) -> None:
    """Catch connection-pool paths that omit FK, WAL, or bounded contention configuration."""
    engine = create_database_engine(database_path)
    try:
        for _ in range(2):
            with engine.connect() as connection:
                assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
                assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
                assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == SQLITE_BUSY_TIMEOUT_MS
            engine.dispose()
    finally:
        engine.dispose()


def test_schema_reopens_and_foreign_keys_reject_invalid_writes(database_path: Path) -> None:
    """Catch nonpersistent schemas and sessions that bypass SQLite foreign keys."""
    upgrade_database(database_path)
    first_engine = create_database_engine(database_path)
    factory = create_session_factory(first_engine)
    assert factory.kw["expire_on_commit"] is False
    try:
        with first_engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO sources "
                    "(public_id, replay_id, source_kind, original_locator, original_filename, discovered_at, provenance_json) "
                    "VALUES (:public_id, 999, 'file', 'x', 'x.rep', CURRENT_TIMESTAMP, '{}')"
                ),
                {"public_id": "00000000-0000-4000-8000-000000000001"},
            )
    finally:
        first_engine.dispose()

    second_engine = create_database_engine(database_path)
    try:
        with second_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0001_replay_analyzer_v2"
            )
    finally:
        second_engine.dispose()
