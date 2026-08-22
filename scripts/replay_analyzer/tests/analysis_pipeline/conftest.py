"""Database fixtures for durable analysis planning."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database


@pytest.fixture
def clock() -> datetime:
    return datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    settings = AnalyzerSettings(data_root=tmp_path / "planner-data")
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()
