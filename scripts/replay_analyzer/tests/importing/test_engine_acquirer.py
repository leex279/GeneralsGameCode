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
from generals_replay_analyzer.importing import engine_acquirer as engine_acquirer_module
from generals_replay_analyzer.importing.engine_acquirer import (
    EngineTelemetryAcquirer,
    _stage_declared_map,
    _stage_replay_for_engine,
)


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

    assert len(captured) == 1
    staged_replay, config = captured[0]
    assert staged_replay.parent == config.replay_user_data_root / "Replays"
    assert staged_replay.name == "replay-" + hashlib.sha256(b"replay bytes").hexdigest()[:16] + ".rep"
    assert staged_replay.read_bytes() == replay.read_bytes()
    assert config == EngineRunConfig(
        executable=executable,
        data_root=settings.data_root,
        movement_sample_frames=30,
        replay_user_data_root=config.replay_user_data_root,
    )
    assert config.replay_user_data_root.parent.parent == settings.data_root
    assert artifact.runner_status == "success"
    assert artifact.replay_quality == "complete"
    assert artifact.strategy_analysis_scope == "full"
    assert artifact.trace_path == run_dir / "trace.ndjson"
    assert artifact.engine_executable_sha256 == hashlib.sha256(b"engine executable bytes").hexdigest()


def test_acquire_rejects_replay_changed_during_first_stage_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch source mutation during streaming before the engine sees a mixed replay snapshot."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    replay = tmp_path / "match.rep"
    replay.write_bytes(b"replay bytes")
    calls: list[Path] = []

    stream_descriptor = engine_acquirer_module._stream_descriptor

    def mutate_after_copy(source_descriptor: int, destination_descriptor: int | None = None) -> tuple[int, str]:
        result = stream_descriptor(source_descriptor, destination_descriptor)
        if destination_descriptor is not None:
            replay.write_bytes(b"changed during staging")
        return result

    monkeypatch.setattr(engine_acquirer_module, "_stream_descriptor", mutate_after_copy)

    def exporter(path: Path, _config: EngineRunConfig) -> EngineRunResult:
        calls.append(path)
        raise AssertionError("corrupted staged replay must not launch")

    with pytest.raises(ValueError, match="replay source changed"):
        EngineTelemetryAcquirer(_settings(tmp_path, executable), exporter=exporter).acquire(
            replay,
            hashlib.sha256(b"replay bytes").hexdigest(),
        )

    assert calls == []


def test_stage_declared_map_copies_one_safe_leaf(tmp_path: Path) -> None:
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    source_root = tmp_path / "retail-data"
    source_map = source_root / "Maps" / "[RANK] Sand Scorpion"
    source_map.mkdir(parents=True)
    (source_map / "map.ini").write_bytes(b"map")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_executable=executable,
        engine_user_data_directory=source_root,
    )
    isolated = settings.data_root / "engine-user-data" / "run"

    _stage_declared_map(settings, "userdata/maps/[RANK] Sand Scorpion", isolated)

    assert (isolated / "Maps" / "[RANK] Sand Scorpion" / "map.ini").read_bytes() == b"map"


def test_stage_declared_map_ignores_official_map_identity_without_source_data(tmp_path: Path) -> None:
    """Catch official maps being rejected merely because custom-map staging is unavailable."""
    settings = AnalyzerSettings(_env_file=None, data_root=tmp_path / "product-data")
    isolated = settings.data_root / "engine-user-data" / "run"

    _stage_declared_map(settings, "Maps/Alpine Assault/Alpine Assault.map", isolated)

    assert not isolated.exists()


def test_stage_declared_map_does_not_copy_global_map_cache(tmp_path: Path) -> None:
    """Catch broad Maps-directory copying that imports the retail generated cache."""
    source_root = tmp_path / "retail-data"
    source_map = source_root / "Maps" / "Tournament Desert"
    source_map.mkdir(parents=True)
    (source_root / "Maps" / "MapCache.ini").write_bytes(b"generated cache")
    (source_map / "Tournament Desert.map").write_bytes(b"map")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_user_data_directory=source_root,
    )
    isolated = settings.data_root / "engine-user-data" / "run"

    _stage_declared_map(settings, "userdata/maps/Tournament Desert", isolated)

    assert not (isolated / "Maps" / "MapCache.ini").exists()


def test_stage_declared_map_rejects_multilink_source_file(tmp_path: Path) -> None:
    """Catch a map payload whose bytes can be mutated through an external hard-link alias."""
    source_root = tmp_path / "retail-data"
    source_map = source_root / "Maps" / "Linked Map"
    source_map.mkdir(parents=True)
    payload = source_map / "Linked Map.map"
    payload.write_bytes(b"map")
    try:
        (tmp_path / "alias.map").hardlink_to(payload)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_user_data_directory=source_root,
    )

    with pytest.raises(ValueError, match="single-link"):
        _stage_declared_map(settings, "userdata/maps/Linked Map", settings.data_root / "isolated")


def test_stage_declared_map_rejects_file_replaced_between_capture_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch opening attacker-selected bytes after the source manifest was captured."""
    source_root = tmp_path / "retail-data"
    source_map = source_root / "Maps" / "Race Map"
    source_map.mkdir(parents=True)
    payload = source_map / "Race Map.map"
    payload.write_bytes(b"trusted")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_user_data_directory=source_root,
    )
    isolated = settings.data_root / "engine-user-data" / "run"

    def replace_after_capture(event: str) -> None:
        if event == "map_before_file_open":
            payload.unlink()
            payload.write_bytes(b"external attacker bytes")

    monkeypatch.setattr(engine_acquirer_module, "_race_hook", replace_after_capture)

    with pytest.raises(ValueError, match="source changed"):
        _stage_declared_map(settings, "userdata/maps/Race Map", isolated)

    assert not (isolated / "Maps" / "Race Map").exists()


def test_stage_declared_map_revalidates_complete_source_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch source-directory additions after individual files were copied."""
    source_root = tmp_path / "retail-data"
    source_map = source_root / "Maps" / "Mutable Map"
    source_map.mkdir(parents=True)
    (source_map / "Mutable Map.map").write_bytes(b"map")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_user_data_directory=source_root,
    )
    isolated = settings.data_root / "engine-user-data" / "run"

    def mutate_after_copy(event: str) -> None:
        if event == "map_after_copy":
            (source_map / "late.ini").write_bytes(b"late")

    monkeypatch.setattr(engine_acquirer_module, "_race_hook", mutate_after_copy)

    with pytest.raises(ValueError, match="source changed"):
        _stage_declared_map(settings, "userdata/maps/Mutable Map", isolated)

    assert not (isolated / "Maps" / "Mutable Map").exists()


def test_stage_declared_map_reuses_only_an_exact_destination_manifest(tmp_path: Path) -> None:
    """Catch reuse of a destination containing bytes outside the captured source manifest."""
    source_root = tmp_path / "retail-data"
    source_map = source_root / "Maps" / "Exact Map"
    source_map.mkdir(parents=True)
    (source_map / "Exact Map.map").write_bytes(b"map")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_user_data_directory=source_root,
    )
    isolated = settings.data_root / "engine-user-data" / "run"
    _stage_declared_map(settings, "userdata/maps/Exact Map", isolated)
    destination = isolated / "Maps" / "Exact Map"

    _stage_declared_map(settings, "userdata/maps/Exact Map", isolated)
    (destination / "unexpected.ini").write_bytes(b"unexpected")

    with pytest.raises(ValueError, match="manifest"):
        _stage_declared_map(settings, "userdata/maps/Exact Map", isolated)


def test_stage_declared_map_concurrent_publish_does_not_overwrite_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch a check-then-rename publication overwriting a concurrent directory."""
    source_root = tmp_path / "retail-data"
    source_map = source_root / "Maps" / "Concurrent Map"
    source_map.mkdir(parents=True)
    (source_map / "Concurrent Map.map").write_bytes(b"map")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_user_data_directory=source_root,
    )
    isolated = settings.data_root / "engine-user-data" / "run"
    destination = isolated / "Maps" / "Concurrent Map"

    def publish_competitor(event: str) -> None:
        if event == "map_before_publish":
            destination.mkdir(parents=True)
            (destination / "competitor.txt").write_bytes(b"owned by competitor")

    monkeypatch.setattr(engine_acquirer_module, "_race_hook", publish_competitor)

    with pytest.raises(ValueError, match="destination"):
        _stage_declared_map(settings, "userdata/maps/Concurrent Map", isolated)

    assert (destination / "competitor.txt").read_bytes() == b"owned by competitor"
    assert not (destination / "Concurrent Map.map").exists()
    assert not tuple((isolated / "Maps").glob(".Concurrent Map.*.tmp"))


def test_stage_replay_rejects_replacement_between_capture_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch replay staging reading bytes from a path replacement after content binding."""
    replay = tmp_path / "match.rep"
    replay.write_bytes(b"trusted replay")
    settings = AnalyzerSettings(_env_file=None, data_root=tmp_path / "product-data")
    expected_sha256 = hashlib.sha256(b"trusted replay").hexdigest()

    def replace_after_capture(event: str) -> None:
        if event == "replay_after_capture":
            replay.unlink()
            replay.write_bytes(b"external attacker replay")

    monkeypatch.setattr(engine_acquirer_module, "_race_hook", replace_after_capture)

    with pytest.raises(ValueError, match="replay source changed"):
        _stage_replay_for_engine(settings, replay, expected_sha256)


@pytest.mark.parametrize("identity", ("userdata/maps/../escape", "userdata/maps/a/b", "C:/escape"))
def test_stage_declared_map_rejects_unsafe_identity(tmp_path: Path, identity: str) -> None:
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    settings = AnalyzerSettings(_env_file=None, data_root=tmp_path / "product-data", engine_executable=executable)

    with pytest.raises(ValueError, match="map identity"):
        _stage_declared_map(settings, identity, settings.data_root / "isolated")


def test_stage_declared_map_rejects_missing_source(tmp_path: Path) -> None:
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=tmp_path / "product-data",
        engine_executable=executable,
        engine_user_data_directory=tmp_path / "retail-data",
    )

    with pytest.raises(ValueError, match="ordinary"):
        _stage_declared_map(settings, "userdata/maps/Missing Map", settings.data_root / "isolated")


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
