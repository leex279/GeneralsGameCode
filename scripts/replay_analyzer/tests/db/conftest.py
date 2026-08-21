"""File-backed database fixtures for persistence contract tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from generals_replay_analyzer.db import create_database_engine, downgrade_database, upgrade_database


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """Return a fresh product database path whose parent already exists."""
    return tmp_path / "library.sqlite3"


@pytest.fixture
def migrated_engine(database_path: Path) -> Iterator[Engine]:
    """Upgrade a file database and dispose its engine after each test."""
    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def empty_again(database_path: Path) -> Iterator[Path]:
    """Offer a migrated database and return it to Alembic base afterwards."""
    upgrade_database(database_path)
    try:
        yield database_path
    finally:
        downgrade_database(database_path)
