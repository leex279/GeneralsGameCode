"""Production Jobs request session and transaction ownership tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import generals_replay_analyzer.db as database_module
import generals_replay_analyzer.web.routes.jobs as jobs_routes
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.importing import JobLifecycleService, JobSummaryDTO
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec
from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory


class _Readiness:
    def schema_revision(self) -> str:
        return "0005"


class _Bootstrapper:
    def prepare(self, _settings: object) -> None:
        return None


class _Csrf:
    def accepts(self, token: str | None) -> bool:
        return token == "accepted-token"


@dataclass
class _SessionCounts:
    opened: int = 0
    committed: int = 0
    rolled_back: int = 0
    closed: int = 0


def _counting_session_factory(
    original: Callable[[Engine], sessionmaker[Session]],
    counts: _SessionCounts,
) -> Callable[[Engine], Callable[[], Session]]:
    def factory(engine: Engine) -> Callable[[], Session]:
        base = original(engine)

        def open_session() -> Session:
            counts.opened += 1
            session = base()
            real_commit = session.commit
            real_rollback = session.rollback
            real_close = session.close

            def commit() -> None:
                counts.committed += 1
                real_commit()

            def rollback() -> None:
                counts.rolled_back += 1
                real_rollback()

            def close() -> None:
                counts.closed += 1
                real_close()

            session.commit = commit  # type: ignore[method-assign]
            session.rollback = rollback  # type: ignore[method-assign]
            session.close = close  # type: ignore[method-assign]
            return session

        return open_session

    return factory


def _settings_and_pending_job(tmp_path: Path) -> tuple[AnalyzerSettings, str]:
    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    try:
        session_factory = create_session_factory(engine)
        snapshot = JobCoordinator(session_factory, clock=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=UTC)).create_job(
            JobSpec("parse", "parse-v1", "request-scope-job", {})
        )
        return settings, snapshot.public_id
    finally:
        engine.dispose()


def _durable_job(settings: AnalyzerSettings, job_public_id: str) -> JobSummaryDTO:
    engine = create_database_engine(settings.database_path)
    try:
        lifecycle = JobLifecycleService(
            create_session_factory(engine),
            registered_stages=(),
            clock=lambda: datetime(2026, 8, 22, 12, 1, tzinfo=UTC),
        )
        return lifecycle.get_job(job_public_id).summary
    finally:
        engine.dispose()


def test_production_cancel_request_uses_one_session_and_commits_returned_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_public_id = _settings_and_pending_job(tmp_path)
    counts = _SessionCounts()
    original = database_module.create_session_factory
    monkeypatch.setattr(database_module, "create_session_factory", _counting_session_factory(original, counts))
    app = create_app(
        settings,
        port_factory=AnalyticsPortFactory(settings, _Readiness()),
        bootstrapper=_Bootstrapper(),
        csrf_validator=_Csrf(),
    )

    with TestClient(app) as client:
        response = client.post(
            f"/jobs/{job_public_id}/cancel",
            data={"expected_revision": "0"},
            headers={
                "host": "localhost",
                "origin": "http://localhost",
                "x-csrf-token": "accepted-token",
            },
        )

    assert response.status_code == 200, response.text
    assert "State: cancelled" in response.text
    assert counts == _SessionCounts(opened=1, committed=1, closed=1)
    durable = _durable_job(settings, job_public_id)
    assert durable.state.value == "cancelled" and durable.revision == 1


def test_production_cancel_rolls_back_when_returned_detail_cannot_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_public_id = _settings_and_pending_job(tmp_path)
    counts = _SessionCounts()
    original = database_module.create_session_factory
    monkeypatch.setattr(database_module, "create_session_factory", _counting_session_factory(original, counts))
    monkeypatch.setattr(jobs_routes, "template_response", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failed")))
    app = create_app(
        settings,
        port_factory=AnalyticsPortFactory(settings, _Readiness()),
        bootstrapper=_Bootstrapper(),
        csrf_validator=_Csrf(),
    )

    with TestClient(app) as client, pytest.raises(RuntimeError, match="render failed"):
        client.post(
            f"/jobs/{job_public_id}/cancel",
            data={"expected_revision": "0"},
            headers={
                "host": "localhost",
                "origin": "http://localhost",
                "x-csrf-token": "accepted-token",
            },
        )

    assert counts == _SessionCounts(opened=1, rolled_back=1, closed=1)
    durable = _durable_job(settings, job_public_id)
    assert durable.state.value == "pending" and durable.revision == 0
