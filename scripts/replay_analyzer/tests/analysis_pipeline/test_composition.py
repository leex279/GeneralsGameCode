"""Single production registration seam for replay analysis."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.composition import create_production_import_service
from generals_replay_analyzer.analysis_pipeline.handlers import PRODUCTION_EXTRACTOR_NAMES
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.storage import ContentAddressedStore


def test_public_factory_registers_the_exact_production_stage_set(
    session_factory: sessionmaker[Session], clock: datetime, tmp_path: Path
) -> None:
    settings = AnalyzerSettings(data_root=tmp_path / "composition")
    settings.ensure_directories()
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
