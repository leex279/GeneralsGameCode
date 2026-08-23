"""Contract tests for adapting validated engine runs into telemetry artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.engine.config import EngineRunConfig
from generals_replay_analyzer.engine.result import (
    EngineRunResult,
    EngineRunStatus,
    ReplayQuality,
    RunDiagnostic,
    StrategyAnalysisScope,
)
from generals_replay_analyzer.importing.engine_acquirer import EngineTelemetryAcquirer


def _settings(tmp_path: Path, executable: Path) -> AnalyzerSettings:
    return AnalyzerSettings(_env_file=None, data_root=tmp_path / "product-data", engine_executable=executable, movement_sample_frames=30)


def _engine_result(
    run_dir: Path,
    status: EngineRunStatus,
    *,
    diagnostics: tuple[RunDiagnostic, ...] = (),
) -> EngineRunResult:
    trace = run_dir / "trace.ndjson"
    catalog = run_dir / ("game-data-catalog-v1-" + "a" * 64 + ".json")
    map_asset = run_dir / "map-assets-v1" / ("b" * 64) / "manifest.json"
    outcome = run_dir / "replay-outcome.json"
    stdout = run_dir / "stdout.log"
    stderr = run_dir / "stderr.log"
    partial = status in {
        EngineRunStatus.VALID_CRC_MISMATCH,
        EngineRunStatus.REPLAY_TRUNCATED,
        EngineRunStatus.INTERRUPTED,
    }
    failed = not partial and status is not EngineRunStatus.SUCCESS
    return EngineRunResult(
        run_id="123e4567-e89b-42d3-a456-426614174000",
        run_dir=run_dir,
        trace_path=None if failed else trace,
        catalog_path=None if failed else catalog,
        map_assets=() if failed else (map_asset,),
        outcome_path=None if failed else outcome,
        stdout_path=None if failed else stdout,
        stderr_path=None if failed else stderr,
        exit_code=0 if status is EngineRunStatus.SUCCESS else 1,
        status=status,
        duration_seconds=1.5,
        replay_quality=(
            ReplayQuality.ENGINE_VERIFIED
            if status is EngineRunStatus.SUCCESS
            else ReplayQuality.PARTIAL
            if partial
            else ReplayQuality.FAILED
        ),
        strategy_analysis_scope=(
            StrategyAnalysisScope.FULL_MATCH
            if status is EngineRunStatus.SUCCESS
            else StrategyAnalysisScope.OBSERVED_BOUNDARY_ONLY
            if partial
            else StrategyAnalysisScope.NONE
        ),
        diagnostics=diagnostics,
    )


def test_acquire_rejects_changed_replay_before_launch(tmp_path: Path) -> None:
    """Catch removal of the content binding that prevents an exporter launching changed replay bytes."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    replay = tmp_path / "match.rep"
    replay.write_bytes(b"actual replay bytes")
    calls: list[tuple[Path, EngineRunConfig]] = []

    def exporter(path: Path, config: EngineRunConfig) -> EngineRunResult:
        calls.append((path, config))
        raise AssertionError("the exporter must not run for changed content")

    acquirer = EngineTelemetryAcquirer(_settings(tmp_path, executable), exporter=exporter)

    with pytest.raises(ValueError, match="replay SHA-256"):
        acquirer.acquire(replay, "0" * 64)

    assert calls == []


def test_acquire_maps_full_engine_success_and_binds_executable_identity(tmp_path: Path) -> None:
    """Catch config/provenance drift that would run valid telemetry under the wrong engine identity."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine executable bytes")
    replay = tmp_path / "match.rep"
    replay.write_bytes(b"replay bytes")
    captured: list[tuple[Path, EngineRunConfig]] = []
    run_dir = tmp_path / "engine-run"

    def exporter(path: Path, config: EngineRunConfig) -> EngineRunResult:
        captured.append((path, config))
        return _engine_result(run_dir, EngineRunStatus.SUCCESS)

    settings = _settings(tmp_path, executable)
    artifact = EngineTelemetryAcquirer(settings, exporter=exporter).acquire(
        replay,
        hashlib.sha256(b"replay bytes").hexdigest(),
    )

    assert captured == [
        (
            replay,
            EngineRunConfig(
                executable=executable,
                data_root=settings.data_root,
                movement_sample_frames=30,
            ),
        )
    ]
    assert artifact.runner_status == "success"
    assert artifact.replay_quality == "complete"
    assert artifact.strategy_analysis_scope == "full"
    assert artifact.trace_path == run_dir / "trace.ndjson"
    assert artifact.engine_executable_sha256 == hashlib.sha256(b"engine executable bytes").hexdigest()


@pytest.mark.parametrize(
    "status",
    (
        EngineRunStatus.VALID_CRC_MISMATCH,
        EngineRunStatus.REPLAY_TRUNCATED,
        EngineRunStatus.INTERRUPTED,
    ),
)
def test_acquire_retains_validated_partial_trace_with_terminal_status_diagnostic(
    tmp_path: Path,
    status: EngineRunStatus,
) -> None:
    """Catch downgrading a validated boundary trace to failure or overstating it as a full match."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    replay = tmp_path / "match.rep"
    replay.write_bytes(b"replay")

    def exporter(_path: Path, _config: EngineRunConfig) -> EngineRunResult:
        return _engine_result(tmp_path / "engine-run", status)

    artifact = EngineTelemetryAcquirer(_settings(tmp_path, executable), exporter=exporter).acquire(
        replay,
        hashlib.sha256(b"replay").hexdigest(),
    )

    assert artifact.runner_status == "success"
    assert artifact.replay_quality == "partial"
    assert artifact.strategy_analysis_scope == "observed_boundary_only"
    assert artifact.trace_path == tmp_path / "engine-run" / "trace.ndjson"
    assert artifact.diagnostics[-1].code == "engine_terminal_status"
    assert artifact.diagnostics[-1].message == status.value


def test_acquire_preserves_failed_runner_status_without_fabricating_evidence_paths(tmp_path: Path) -> None:
    """Catch failure translation that claims trace/catalog/map evidence the runner did not validate."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    replay = tmp_path / "match.rep"
    replay.write_bytes(b"replay")

    def exporter(_path: Path, _config: EngineRunConfig) -> EngineRunResult:
        return _engine_result(tmp_path / "engine-run", EngineRunStatus.ENGINE_FAILURE)

    artifact = EngineTelemetryAcquirer(_settings(tmp_path, executable), exporter=exporter).acquire(
        replay,
        hashlib.sha256(b"replay").hexdigest(),
    )

    assert artifact.runner_status == "nonzero_engine_failure"
    assert artifact.replay_quality == "failed"
    assert artifact.strategy_analysis_scope == "none"
    assert artifact.trace_path is None
    assert artifact.catalog_path is None
    assert artifact.map_asset_paths == ()


def test_acquire_redacts_caller_paths_from_runner_diagnostics(tmp_path: Path) -> None:
    """Catch diagnostics leaking replay or engine-run filesystem paths across the import boundary."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    replay = tmp_path / "inputs" / "match.rep"
    replay.parent.mkdir()
    replay.write_bytes(b"replay")
    run_dir = tmp_path / "engine-runs" / "run-1"
    diagnostic = RunDiagnostic("engine_failure", f"{replay} failed near {run_dir / 'trace.ndjson'}")

    def exporter(_path: Path, _config: EngineRunConfig) -> EngineRunResult:
        return _engine_result(run_dir, EngineRunStatus.SUCCESS, diagnostics=(diagnostic,))

    artifact = EngineTelemetryAcquirer(_settings(tmp_path, executable), exporter=exporter).acquire(
        replay,
        hashlib.sha256(b"replay").hexdigest(),
    )

    message = artifact.diagnostics[0].message
    assert str(replay) not in message
    assert str(run_dir) not in message
    assert "[replay]" in message
    assert "[engine-artifact]" in message
