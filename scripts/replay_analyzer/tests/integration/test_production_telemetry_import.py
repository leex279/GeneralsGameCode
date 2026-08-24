"""Opt-in proof that production composition imports a real engine trace."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.composition import (
    ENGINE_TELEMETRY_ACQUIRER_VERSION,
    configured_engine_telemetry_acquirer,
    create_production_import_service,
)
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    Job,
    ManagedAsset,
    Replay,
    ReplayQualityIssue,
    Report,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.importing import ImportRequest, ImportService
from generals_replay_analyzer.importing.engine_acquirer import EngineTelemetryAcquirer
from generals_replay_analyzer.llm.provider import (
    CancellationSignal,
    JSONValue,
    OllamaClientConfig,
    ProviderError,
    TransportResponse,
)
from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.report.model import ReportDocument, ReportValue
from generals_replay_analyzer.report.query import ReportQueryService
from generals_replay_analyzer.storage import ContentAddressedStore
from generals_replay_analyzer.video.resolver import VideoRequestResolver

_OPT_IN = "GENERALS_REPLAY_ANALYZER_RUN_PRODUCTION_TELEMETRY_INTEGRATION"
_EXECUTABLE = "GENERALS_REPLAY_ANALYZER_EXE"
_RUNTIME_DIRECTORY = "GENERALS_REPLAY_ANALYZER_GAME_DIR"
_USER_DATA_DIRECTORY = "GENERALS_REPLAY_ANALYZER_GAME_USER_DATA"
_PINNED_REPLAY = Path(__file__).parents[1] / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"
_EXPECTED_FINAL_FRAME = 56004
_EXPECTED_COMMAND_COUNT = 3992


class _UnavailableTransport:
    """Keep the optional LLM branch local while the durable graph drains."""

    def __init__(self, config: OllamaClientConfig) -> None:
        self.client_config = config

    async def request(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        *,
        client_config: OllamaClientConfig,
        cancellation: CancellationSignal | None,
    ) -> TransportResponse:
        del method, path, payload, client_config, cancellation
        raise ProviderError("disabled for production telemetry ingestion proof")

    async def aclose(self) -> None:
        return None


def _configured_runtime() -> tuple[Path, Path, Path]:
    """Require an explicit opt-in and both immutable engine launch inputs."""
    if os.environ.get(_OPT_IN) != "1":
        pytest.skip(f"set {_OPT_IN}=1 to launch the real Zero Hour telemetry integration")
    executable_raw = os.environ.get(_EXECUTABLE)
    runtime_raw = os.environ.get(_RUNTIME_DIRECTORY)
    user_data_raw = os.environ.get(_USER_DATA_DIRECTORY)
    if not executable_raw or not runtime_raw or not user_data_raw:
        pytest.fail(f"{_OPT_IN}=1 requires {_EXECUTABLE}, {_RUNTIME_DIRECTORY}, and {_USER_DATA_DIRECTORY}")
    executable = Path(executable_raw).resolve(strict=False)
    runtime = Path(runtime_raw).resolve(strict=False)
    user_data = Path(user_data_raw).resolve(strict=False)
    if not executable.is_file() or not runtime.is_dir() or not user_data.is_dir():
        pytest.fail("production telemetry integration inputs must be an executable file and runtime directory")
    return executable, runtime, user_data


def _drain(service: ImportService) -> None:
    """Run the public worker port until the single clean-root graph has settled."""
    for _ in range(32):
        if not service.run_available("production-telemetry-import", limit=1):
            return
    raise AssertionError("production telemetry import graph did not drain")


def _report_values(document: ReportDocument) -> Iterable[ReportValue]:
    return (*document.observed, *document.derived, *document.inferred)


def test_production_composition_exposes_the_real_engine_adapter_when_configured(tmp_path: Path) -> None:
    """Catch production composition regressing to parser-only telemetry before the opt-in run starts."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    settings = AnalyzerSettings.model_validate(
        {"data_root": tmp_path / "composition", "engine_executable": executable}
    )

    acquirer = configured_engine_telemetry_acquirer(settings)

    assert isinstance(acquirer, EngineTelemetryAcquirer)
    assert acquirer.settings is settings


@pytest.mark.engine
def test_configured_production_import_persists_complete_replay_telemetry_and_report_horizon(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch fixture telemetry, early termination, or claims beyond the clean engine completion record."""
    executable, runtime_directory, user_data_directory = _configured_runtime()
    for test_control in (_OPT_IN, _EXECUTABLE, _RUNTIME_DIRECTORY, _USER_DATA_DIRECTORY):
        monkeypatch.delenv(test_control, raising=False)
    assert _PINNED_REPLAY.is_file()
    settings = AnalyzerSettings.model_validate(
        {
            # Keep the runner's immutable transaction names below the retail engine's ANSI MAX_PATH.
            "data_root": tmp_path / "p",
            "engine_executable": executable,
            "engine_runtime_directory": runtime_directory,
            "engine_user_data_directory": user_data_directory,
        }
    )
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    request.addfinalizer(engine.dispose)
    session_factory: sessionmaker[Session] = create_session_factory(engine)
    replay_store = ContentAddressedStore(settings.managed_replay_directory)
    artifact_store = ContentAddressedStore(settings.cache_directory / "artifacts")
    acquirer = configured_engine_telemetry_acquirer(settings)
    assert acquirer is not None
    service = create_production_import_service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parse_replay,
        telemetry_acquirer=acquirer,
        clock=lambda: datetime.now(UTC),
        parser_version="zero-hour-1.04-v1",
        telemetry_acquirer_version=ENGINE_TELEMETRY_ACQUIRER_VERSION,
        transport_factory=_UnavailableTransport,
    )

    service.submit(ImportRequest(_PINNED_REPLAY, request_telemetry=True))
    _drain(service)

    with session_factory() as session:
        replay = session.scalar(select(Replay))
        telemetry = session.scalar(select(TelemetryRun))
        report = session.scalar(select(Report).where(Report.replay_player_id.is_(None)))
        assert replay is not None and telemetry is not None and report is not None
        jobs = tuple(session.scalars(select(Job).order_by(Job.id)))
        issues = tuple(
            session.scalars(
                select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == telemetry.id)
            )
        )
        telemetry_assets = tuple(
            asset for asset in session.scalars(select(ManagedAsset)) if asset.kind.startswith("telemetry_")
        )
        outcome = session.scalar(
            select(TelemetryEvent).where(
                TelemetryEvent.telemetry_run_id == telemetry.id,
                TelemetryEvent.event_type == "match_outcome",
            )
        )
        persisted_document = ReportQueryService._document(report.report_json)

    assert {job.stage for job in jobs} >= {
        "parse",
        "telemetry",
        "import_observations",
        "reconcile_identities",
        "derive_features",
        "assess_strategies",
        "render_report",
    }
    assert all(job.status == "succeeded" for job in jobs if job.stage != "analyze_llm")
    assert telemetry.status == "succeeded"
    assert telemetry.runner_status == "success"
    assert isinstance(telemetry.settings_json, dict)
    assert telemetry.settings_json.get("replay_quality") == "complete"
    assert telemetry.strategy_analysis_scope == "full"
    assert telemetry.final_frame == _EXPECTED_FINAL_FRAME
    assert telemetry.command_count == _EXPECTED_COMMAND_COUNT
    assert telemetry.trace_sha256 is not None
    assert all(issue.issue_code != "crc_mismatch" for issue in issues)
    assert outcome is not None
    assert outcome.frame == _EXPECTED_FINAL_FRAME
    assert outcome.payload_json == {
        "status": "decided",
        "source": "victory_conditions",
        "winner_player_indices": [2],
        "loser_player_indices": [3],
        "engine_player_indices": [0, 1, 2, 3, 4],
        "terminal_reason": "clean_completion",
        "quit_early": False,
        "replay_header_desync": False,
        "replay_header_disconnected_slots": [],
        "crc_mismatch": False,
        "crc_mismatch_frame": None,
        "clean_shutdown": True,
    }
    assert telemetry_assets
    assert telemetry.trace_sha256 in {asset.sha256 for asset in telemetry_assets}
    for asset in telemetry_assets:
        verified = artifact_store.verify(asset.sha256)
        assert verified.sha256 == asset.sha256
        assert verified.size == asset.size_bytes

    resolver = VideoRequestResolver(session_factory, settings)
    video_request = resolver.resolve(
        {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "report_public_id": report.public_id,
            "evidence_horizon": "complete",
            "diagnostic_preview": False,
            "logic_frames_per_second": 30,
        }
    )
    assert video_request.authority.evidence_horizon.frame_end == telemetry.final_frame
    for value in _report_values(video_request.report.selected.document):
        frame_window = value.frame_window
        assert frame_window is None or frame_window[1] <= telemetry.final_frame
    for value in _report_values(persisted_document):
        frame_window = value.frame_window
        assert frame_window is None or frame_window[1] <= _EXPECTED_FINAL_FRAME
