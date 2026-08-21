"""File-backed fixtures for replay intake and durable job orchestration."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.storage import ContentAddressedStore

PINNED_REPLAY = Path(__file__).parents[1] / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"


@dataclass
class MutableClock:
    """Deterministic aware-UTC clock whose time can advance across lease tests."""

    current: datetime = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **values: float) -> None:
        self.current += timedelta(**values)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def settings(tmp_path: Path) -> AnalyzerSettings:
    product_root = tmp_path / "external-product-data"
    configured = AnalyzerSettings(data_root=product_root)
    configured.ensure_directories()
    return configured


@pytest.fixture
def session_factory(settings: AnalyzerSettings) -> sessionmaker[Session]:
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def database_engine(session_factory: sessionmaker[Session]) -> Engine:
    bind = session_factory.kw["bind"]
    assert isinstance(bind, Engine)
    return bind


@pytest.fixture
def replay_store(settings: AnalyzerSettings) -> ContentAddressedStore:
    return ContentAddressedStore(settings.managed_replay_directory)


@pytest.fixture
def artifact_store(settings: AnalyzerSettings) -> ContentAddressedStore:
    return ContentAddressedStore(settings.cache_directory / "artifacts")


@pytest.fixture
def replay_file(tmp_path: Path) -> Path:
    target = tmp_path / "match_3133811_user_ABCDEF_replay.rep"
    shutil.copyfile(PINNED_REPLAY, target)
    return target
