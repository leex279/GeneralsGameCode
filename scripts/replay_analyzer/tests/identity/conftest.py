"""File-backed identity database fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database


@pytest.fixture
def identity_database_path(tmp_path: Path) -> Path:
    """Return an external temporary file-backed database path."""
    return tmp_path / "identity.sqlite3"


@pytest.fixture
def identity_engine(identity_database_path: Path) -> Iterator[Engine]:
    """Return an engine upgraded through the current packaged migration head."""
    upgrade_database(identity_database_path)
    engine = create_database_engine(identity_database_path)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def identity_session_factory(identity_engine: Engine) -> sessionmaker[Session]:
    """Return independent application sessions for identity transactions."""
    return create_session_factory(identity_engine)
