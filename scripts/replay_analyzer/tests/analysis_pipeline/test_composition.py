"""Single production registration seam for replay analysis."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline import composition
from generals_replay_analyzer.analysis_pipeline.composition import create_production_import_service
from generals_replay_analyzer.analysis_pipeline.handlers import PRODUCTION_EXTRACTOR_NAMES
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.storage import ContentAddressedStore
from generals_replay_analyzer.strategy.service import StrategyAssessmentService


def test_configured_engine_telemetry_acquirer_is_present_only_when_engine_is_configured(
    tmp_path: Path,
) -> None:
    """Catch production composition enabling engine launch without an explicit executable."""
    unconfigured = AnalyzerSettings(data_root=tmp_path / "unconfigured")
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    configured = AnalyzerSettings(
        data_root=tmp_path / "configured",
        engine_executable=executable,
    )

    assert composition.configured_engine_telemetry_acquirer(unconfigured) is None
    acquirer = composition.configured_engine_telemetry_acquirer(configured)
    assert acquirer is not None
    assert acquirer.settings is configured


def test_public_factory_registers_the_exact_production_stage_set(
    session_factory: sessionmaker[Session], clock: datetime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AnalyzerSettings(data_root=tmp_path / "composition")
    settings.ensure_directories()
    strategy_arguments: dict[str, object] = {}

    def strategy_service(
        factory: sessionmaker[Session], **kwargs: object
    ) -> StrategyAssessmentService:
        strategy_arguments.update(kwargs)
        return StrategyAssessmentService(factory, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(composition, "StrategyAssessmentService", strategy_service)
    service = create_production_import_service(
        session_factory,
        settings,
        ContentAddressedStore(settings.managed_replay_directory),
        ContentAddressedStore(settings.cache_directory / "artifacts"),
        parser=lambda _path: (_ for _ in ()).throw(AssertionError("parser is not used")),
        telemetry_acquirer=None,
        clock=lambda: clock,
        parser_version="parser-v1",
        telemetry_acquirer_version="telemetry-v1",
    )

    assert service.worker_control_port().registered_stages() == (
        "analyze_llm",
        "assess_strategies",
        "derive_features",
        "discover",
        "hash",
        "import_observations",
        "manage_copy",
        "parse",
        "reconcile_identities",
        "render_report",
    )
    assert PRODUCTION_EXTRACTOR_NAMES == (
        "activity",
        "build",
        "combat",
        "economy",
        "production",
        "spatial",
    )
    assert strategy_arguments == {"data_root": settings.data_root}
