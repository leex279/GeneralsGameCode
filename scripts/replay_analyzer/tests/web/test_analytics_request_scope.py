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

import generals_replay_analyzer.comparison.service as comparison_module
import generals_replay_analyzer.db as database_module
import generals_replay_analyzer.report.query as report_query_module
import generals_replay_analyzer.web.routes.jobs as jobs_routes
from generals_replay_analyzer.config import AnalyzerSettings, load_runtime_configuration
from generals_replay_analyzer.configuration import ConfigurationStore, SettingChange
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.importing import JobLifecycleService, JobSummaryDTO
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec
from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory
from generals_replay_analyzer.web.ports import (
    ApplySettingsCommandDTO,
    ComparisonQueryPort,
    DiagnosticsCommandPort,
    PlayerHistoryPort,
    PlayerIdentityWorkflowPort,
    PlayerIndexQueryDTO,
    SettingChangeDTO,
    SettingsCommandPort,
    SettingsPreviewCommandDTO,
    SettingsQueryPort,
)


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


def test_production_factory_exposes_player_and_comparison_ports(tmp_path: Path) -> None:
    """Keep the installed player workspaces backed by the accepted Analytics services."""

    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    factory = AnalyticsPortFactory(
        settings,
        _Readiness(),
        configuration_root=tmp_path / "external-configuration",
    )

    with factory() as port:
        assert isinstance(port, PlayerHistoryPort)
        assert isinstance(port, PlayerIdentityWorkflowPort)
        assert isinstance(port, ComparisonQueryPort)
        page = port.list_players(PlayerIndexQueryDTO(active_only=False))

    assert page.items == ()


def test_production_factory_shares_validated_report_cache_across_request_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caches: list[object] = []
    original_init = report_query_module.ReportQueryService.__init__

    def recording_init(self: object, *args: object, **kwargs: object) -> None:
        caches.append(kwargs.get("report_graph_cache"))
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(report_query_module.ReportQueryService, "__init__", recording_init)
    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    factory = AnalyticsPortFactory(settings, _Readiness())

    with factory():
        pass
    with factory():
        pass

    assert len(caches) == 2
    assert caches[0] is caches[1]


def test_production_factory_binds_one_comparison_minimum_sample_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[int] = []
    original = comparison_module.ReplayComparisonService

    class RecordingComparisonService(original):
        def __init__(self, *args: object, minimum_sample_size: int = 3, **kwargs: object) -> None:
            captured.append(minimum_sample_size)
            super().__init__(*args, minimum_sample_size=minimum_sample_size, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comparison_module, "ReplayComparisonService", RecordingComparisonService)
    settings = AnalyzerSettings.model_validate(
        {
            "data_root": tmp_path / "product-data",
            "minimum_longitudinal_sample_size": 1,
        }
    )
    settings.ensure_directories()
    upgrade_database(settings.database_path)

    with AnalyticsPortFactory(
        settings,
        _Readiness(),
        configuration_root=tmp_path / "external-configuration",
    )():
        pass

    assert captured == [1]


def test_production_factory_keeps_one_settings_startup_identity(tmp_path: Path) -> None:
    """A persisted change must require restart across later request scopes."""

    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    factory = AnalyticsPortFactory(
        settings,
        _Readiness(),
        configuration_root=tmp_path / "external-configuration",
    )

    with factory() as port:
        assert isinstance(port, SettingsQueryPort)
        assert isinstance(port, SettingsCommandPort)
        assert isinstance(port, DiagnosticsCommandPort)
        before = port.get_settings()
        preview = port.preview_settings(
            SettingsPreviewCommandDTO(
                expected_revision=before.revision,
                changes=(SettingChangeDTO(key="movement_sample_frames", value=30),),
            )
        )
        mutation = port.apply_settings(
            ApplySettingsCommandDTO(
                expected_revision=before.revision,
                changes=(SettingChangeDTO(key="movement_sample_frames", value=30),),
                expected_impact_digest=preview.impact_digest,
                confirm_invalidating_change=True,
            )
        )

    assert mutation.snapshot.restart_required is True
    with factory() as port:
        after = port.get_settings()
    assert after.revision == before.revision + 1
    assert after.restart_required is True


def test_fresh_production_factory_uses_same_persisted_runtime_snapshot(tmp_path: Path) -> None:
    """The page, adapter, and Analytics services must share one post-restart configuration identity."""

    configuration_root = tmp_path / "external-configuration"
    ConfigurationStore(configuration_root=configuration_root, environment={}).apply(
        expected_revision=0,
        changes=(
            SettingChange("minimum_longitudinal_sample_size", 19),
            SettingChange("movement_sample_frames", 75),
            SettingChange("import_mode", "reference"),
            SettingChange("ollama_model", "qwen3.6:8b"),
            SettingChange("ollama_url", "http://[::1]:22434"),
        ),
    )
    runtime = load_runtime_configuration(
        configuration_root=configuration_root,
        environment={},
        values={"data_root": tmp_path / "product-data"},
    )
    runtime.settings.ensure_directories()
    upgrade_database(runtime.settings.database_path)
    factory = AnalyticsPortFactory(runtime, _Readiness())

    with factory() as port:
        snapshot = port.get_settings()

    assert snapshot.effective_settings_digest == runtime.snapshot.effective_settings_digest
    assert snapshot.restart_required is False
    assert factory.runtime_settings is runtime.settings
    assert factory.runtime_settings.minimum_longitudinal_sample_size == 19
    assert factory.runtime_settings.movement_sample_frames == 75
    assert factory.runtime_settings.import_mode == "reference"
    assert factory.runtime_settings.ollama_model == "qwen3.6:8b"
    assert factory.runtime_settings.ollama_url == "http://[::1]:22434"


def test_production_factory_compares_page_to_exact_frozen_runtime_snapshot(tmp_path: Path) -> None:
    """A store mutation between runtime load and factory construction must require restart."""

    configuration_root = tmp_path / "external-configuration"
    writer = ConfigurationStore(configuration_root=configuration_root, environment={})
    writer.apply(
        expected_revision=0,
        changes=(SettingChange("movement_sample_frames", 75),),
    )
    runtime = load_runtime_configuration(
        configuration_root=configuration_root,
        environment={},
        values={"data_root": tmp_path / "product-data"},
    )
    runtime.settings.ensure_directories()
    upgrade_database(runtime.settings.database_path)
    runtime.store.apply(
        expected_revision=runtime.snapshot.revision,
        changes=(SettingChange("movement_sample_frames", 90),),
    )

    factory = AnalyticsPortFactory(runtime, _Readiness())
    with factory() as port:
        page = port.get_settings()

    assert factory.runtime_settings.movement_sample_frames == 75
    assert page.revision == runtime.snapshot.revision + 1
    assert page.effective_settings_digest != runtime.snapshot.effective_settings_digest
    assert page.restart_required is True


def test_legacy_factory_preserves_explicit_safe_settings_as_read_only_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit legacy safe fields must remain higher priority than process environment."""

    environment_values = {
        "GENERALS_REPLAY_ANALYZER_IMPORT_MODE": "copy",
        "GENERALS_REPLAY_ANALYZER_MINIMUM_LONGITUDINAL_SAMPLE_SIZE": "7",
        "GENERALS_REPLAY_ANALYZER_MOVEMENT_SAMPLE_FRAMES": "30",
        "GENERALS_REPLAY_ANALYZER_OLLAMA_MODEL": "environment:1",
        "GENERALS_REPLAY_ANALYZER_OLLAMA_URL": "http://127.0.0.1:11434",
    }
    for name, value in environment_values.items():
        monkeypatch.setenv(name, value)
    settings = AnalyzerSettings.model_validate(
        {
            "data_root": tmp_path / "product-data",
            "import_mode": "reference",
            "minimum_longitudinal_sample_size": 23,
            "movement_sample_frames": 105,
            "ollama_model": "qwen3.6:8b",
            "ollama_url": "http://[::1]:33434",
        }
    )
    settings.ensure_directories()
    upgrade_database(settings.database_path)

    factory = AnalyticsPortFactory(
        settings,
        _Readiness(),
        configuration_root=tmp_path / "external-configuration",
    )
    with factory() as port:
        page = port.get_settings()

    expected = {
        "import_mode": "reference",
        "minimum_longitudinal_sample_size": 23,
        "movement_sample_frames": 105,
        "ollama_model": "qwen3.6:8b",
        "ollama_url": "http://[::1]:33434",
    }
    assert {key: getattr(factory.runtime_settings, key) for key in expected} == expected
    by_key = {value.key: value for value in page.values}
    assert {key: by_key[key].value for key in expected} == expected
    assert all(by_key[key].editable is False for key in expected)
    assert all(
        by_key[key].unavailable_reason_code == "settings_overridden_by_composition"
        for key in expected
    )


def test_legacy_factory_does_not_promote_implicit_safe_defaults_to_composition(tmp_path: Path) -> None:
    """Implicit legacy defaults must leave persisted safe settings effective and editable."""

    configuration_root = tmp_path / "external-configuration"
    expected = {
        "import_mode": "reference",
        "minimum_longitudinal_sample_size": 29,
        "movement_sample_frames": 120,
        "ollama_model": "qwen3.6:8b",
        "ollama_url": "http://[::1]:22434",
    }
    ConfigurationStore(configuration_root=configuration_root, environment={}).apply(
        expected_revision=0,
        changes=tuple(SettingChange(key, value) for key, value in expected.items()),
    )
    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)

    factory = AnalyticsPortFactory(settings, _Readiness(), configuration_root=configuration_root)
    with factory() as port:
        page = port.get_settings()

    assert {key: getattr(factory.runtime_settings, key) for key in expected} == expected
    by_key = {value.key: value for value in page.values}
    assert {key: by_key[key].value for key in expected} == expected
    assert all(by_key[key].source == "persisted" and by_key[key].editable for key in expected)
