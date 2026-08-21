"""Behavioral contracts for the isolated authoritative-engine telemetry runner."""

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest
from telemetry.test_telemetry_v2_contract import _write_catalog, _write_v2_trace

from generals_replay_analyzer.engine import runner as runner_module
from generals_replay_analyzer.engine.config import EngineRunConfig, EngineRunConfigurationError
from generals_replay_analyzer.engine.outcome import ReplayOutcomeValidationError, load_replay_outcome
from generals_replay_analyzer.engine.result import EngineRunStatus, StrategyAnalysisScope
from generals_replay_analyzer.engine.runner import (
    ProcessExecution,
    ProcessLaunchRequest,
    export_telemetry,
)
from generals_replay_analyzer.telemetry.model import CompleteRecord
from generals_replay_analyzer.telemetry.reader import iter_validated_trace


class FakeLauncher:
    """Replace only the external engine process while retaining real artifact validation."""

    def __init__(
        self,
        publish: Callable[[ProcessLaunchRequest], None] | None = None,
        *,
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.publish = publish
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.requests: list[ProcessLaunchRequest] = []

    def __call__(self, request: ProcessLaunchRequest) -> ProcessExecution:
        self.requests.append(request)
        if self.publish is not None:
            self.publish(request)
        return ProcessExecution(
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            duration_seconds=1.25,
            process_tree_terminated=self.timed_out,
            termination_method="fake_process_tree" if self.timed_out else None,
        )


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = (tmp_path / "runtime" / "generalszh.exe").resolve()
    executable.parent.mkdir()
    executable.write_bytes(b"test-engine")
    replay = (tmp_path / "fixture.rep").resolve()
    replay.write_bytes(b"GENREP-test-fixture")
    short_token = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:8]
    return executable, replay, (tmp_path.parents[1] / f"g9-{short_token}").resolve()


def _config(executable: Path, data_root: Path, **changes: object) -> EngineRunConfig:
    values: dict[str, object] = {
        "executable": executable,
        "timeout_seconds": 60,
        "movement_sample_frames": 15,
        "data_root": data_root,
    }
    values.update(changes)
    return EngineRunConfig(**values)  # type: ignore[arg-type]


def _argument_path(request: ProcessLaunchRequest, option: str) -> Path:
    index = request.argv.index(option)
    return Path(request.argv[index + 1])


def _rewrite_trace_terminal(
    trace: Path,
    run_id: str,
    *,
    terminal_reason: str,
    final_frame: int,
    command_count: int,
    crc_mismatch_frame: int | None,
    writer_error: str | None = None,
) -> None:
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record["run_id"] = run_id
    outcome = records[-2]
    complete = records[-1]
    for record in (outcome, complete):
        record["frame"] = final_frame
        record["logic_time_seconds"] = final_frame / 30.0
        record["payload"]["terminal_reason"] = terminal_reason
        record["payload"]["crc_mismatch"] = crc_mismatch_frame is not None
        record["payload"]["crc_mismatch_frame"] = crc_mismatch_frame
        record["payload"]["clean_shutdown"] = terminal_reason == "clean_completion"
    complete["payload"]["final_frame"] = final_frame
    complete["payload"]["command_count"] = command_count
    complete["payload"]["replay_truncated"] = terminal_reason == "replay_truncated"
    complete["payload"]["writer_error"] = writer_error
    prior = b"".join(
        json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records[:-1]
    )
    complete["payload"]["trace_sha256"] = hashlib.sha256(prior).hexdigest()
    trace.write_bytes(prior + json.dumps(complete, separators=(",", ":")).encode("utf-8") + b"\n")


def _publish_outcome(
    path: Path,
    *,
    playback_started: bool = True,
    final_frame: int = 0,
    command_count: int = 0,
    terminal_reason: str = "clean_completion",
    crc_mismatch_frame: int | None = None,
) -> None:
    path.write_bytes(
        (
            json.dumps(
            {
                "schema_version": 1,
                "playback_started": playback_started,
                "final_frame": final_frame,
                "command_count": command_count,
                "terminal_reason": terminal_reason,
                "crc_mismatch": crc_mismatch_frame is not None,
                "crc_mismatch_frame": crc_mismatch_frame,
            },
            separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )


def _publish_valid_evidence(
    request: ProcessLaunchRequest,
    *,
    terminal_reason: str = "clean_completion",
    final_frame: int = 0,
    command_count: int = 0,
    crc_mismatch_frame: int | None = None,
    writer_error: str | None = None,
) -> None:
    trace = _argument_path(request, "-telemetry")
    catalog_reference = _write_catalog(trace.parent)
    _write_v2_trace(trace.parent, catalog_reference)
    _rewrite_trace_terminal(
        trace,
        request.run_id,
        terminal_reason=terminal_reason,
        final_frame=final_frame,
        command_count=command_count,
        crc_mismatch_frame=crc_mismatch_frame,
        writer_error=writer_error,
    )
    _publish_outcome(
        _argument_path(request, "-replay-outcome"),
        final_frame=final_frame,
        command_count=command_count,
        terminal_reason=terminal_reason,
        crc_mismatch_frame=crc_mismatch_frame,
    )


def test_success_uses_explicit_argv_runtime_cwd_and_validated_public_paths(tmp_path: Path) -> None:
    """Catch shell launch, retail-incompatible cwd, or public paths escaping strict validation."""
    executable, replay, data_root = _inputs(tmp_path)
    launcher = FakeLauncher(_publish_valid_evidence)

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=launcher,
        run_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.SUCCESS
    assert result.strategy_analysis_scope is StrategyAnalysisScope.FULL_MATCH
    assert result.trace_path == result.run_dir / "trace.ndjson"
    assert result.outcome_path == result.run_dir / "replay-outcome.json"
    assert result.catalog_path is not None and result.catalog_path.is_file()
    assert len(result.map_assets) == 6
    request = launcher.requests[0]
    assert request.cwd == executable.parent
    assert request.argv == (
        str(executable),
        "-headless",
        "-noaudio",
        "-replay",
        str(replay),
        "-telemetry",
        str(result.run_dir / "trace.ndjson"),
        "-telemetry-run-id",
        result.run_id,
        "-telemetry-movement-frames",
        "15",
        "-replay-outcome",
        str(result.run_dir / "replay-outcome.json"),
    )
    assert request.shell is False
    assert result.stdout_path.read_bytes() == b""
    assert result.stderr_path.read_bytes() == b""
    assert (result.run_dir / "request.json").is_file()
    assert (result.run_dir / "result.json").is_file()


def test_isolated_replay_user_data_root_is_validated_and_bound_to_request(tmp_path: Path) -> None:
    """Catch the runner omitting or ambiguously recording the engine's isolated user-map root."""
    executable, replay, data_root = _inputs(tmp_path)
    isolated_user_data = (tmp_path / "isolated-user-data").resolve()
    isolated_user_data.mkdir()
    launcher = FakeLauncher(_publish_valid_evidence)

    result = export_telemetry(
        replay,
        _config(executable, data_root, replay_user_data_root=isolated_user_data),
        launcher=launcher,
        run_id_factory=lambda: "133e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.SUCCESS
    request = launcher.requests[0]
    option_index = request.argv.index("-replay-user-data-root")
    assert request.argv[option_index + 1] == str(isolated_user_data)
    request_document = json.loads((result.run_dir / "request.json").read_text(encoding="utf-8"))
    assert request_document["config"]["replay_user_data_root"] == str(isolated_user_data)


def test_isolated_replay_user_data_root_must_be_an_existing_plain_directory(tmp_path: Path) -> None:
    """Catch launch configuration accepting a missing path or an ordinary file as the user-data root."""
    executable, _replay, data_root = _inputs(tmp_path)
    missing = (tmp_path / "missing-user-data").resolve()
    ordinary_file = (tmp_path / "not-a-directory").resolve()
    ordinary_file.write_bytes(b"not a directory")

    with pytest.raises(EngineRunConfigurationError, match="existing ordinary non-reparse directory"):
        _config(executable, data_root, replay_user_data_root=missing)
    with pytest.raises(EngineRunConfigurationError, match="existing ordinary non-reparse directory"):
        _config(executable, data_root, replay_user_data_root=ordinary_file)


def test_crc_mismatch_is_validated_partial_evidence_not_success(tmp_path: Path) -> None:
    """Catch an expected CRC stop being mislabeled as complete-match strategy evidence."""
    executable, replay, data_root = _inputs(tmp_path)
    launcher = FakeLauncher(
        lambda request: _publish_valid_evidence(
            request,
            terminal_reason="crc_mismatch",
            final_frame=108,
            command_count=16,
            crc_mismatch_frame=105,
        ),
        exit_code=1,
    )

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=launcher,
        run_id_factory=lambda: "223e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.VALID_CRC_MISMATCH
    assert result.strategy_analysis_scope is StrategyAnalysisScope.OBSERVED_BOUNDARY_ONLY
    assert result.trace_path is not None
    assert result.replay_quality == "partial"


def test_playback_truncation_is_distinct_validated_partial_evidence(tmp_path: Path) -> None:
    """Catch a playback-boundary trace collapsing into the pre-playback truncated-input status."""
    executable, replay, data_root = _inputs(tmp_path)
    launcher = FakeLauncher(
        lambda request: _publish_valid_evidence(
            request,
            terminal_reason="replay_truncated",
            final_frame=50,
            command_count=4,
        ),
        exit_code=1,
    )

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=launcher,
        run_id_factory=lambda: "243e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.REPLAY_TRUNCATED
    assert result.strategy_analysis_scope is StrategyAnalysisScope.OBSERVED_BOUNDARY_ONLY
    assert result.trace_path is not None


def test_timeout_requires_tree_termination_and_preserves_diagnostics(tmp_path: Path) -> None:
    """Catch a timed-out engine being reported before its process tree is terminated."""
    executable, replay, data_root = _inputs(tmp_path)
    launcher = FakeLauncher(timed_out=True)

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=launcher,
        run_id_factory=lambda: "323e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.TIMEOUT
    assert result.exit_code == 0
    assert result.trace_path is None
    assert result.run_dir.is_dir()
    result_document = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
    assert result_document["process"]["process_tree_terminated"] is True
    assert result_document["process"]["termination_method"] == "fake_process_tree"


def test_launch_exception_becomes_typed_failure_and_keeps_run_directory(tmp_path: Path) -> None:
    """Catch launcher exceptions escaping without an atomic diagnostic result."""
    executable, replay, data_root = _inputs(tmp_path)

    def fail(_request: ProcessLaunchRequest) -> NoReturn:
        raise OSError("CreateProcessW denied")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=fail,
        run_id_factory=lambda: "423e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.LAUNCH_FAILURE
    assert result.exit_code is None
    assert result.run_dir.is_dir()
    assert (result.run_dir / "result.json").is_file()
    assert any(diagnostic.code == "launch_failure" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    ("terminal_reason", "expected_status"),
    [
        ("input_unavailable", EngineRunStatus.INPUT_UNAVAILABLE),
        ("invalid_replay_header", EngineRunStatus.INVALID_REPLAY_HEADER),
        ("truncated_input", EngineRunStatus.TRUNCATED_INPUT),
    ],
)
def test_preplayback_outcome_precedes_missing_trace(
    tmp_path: Path, terminal_reason: str, expected_status: EngineRunStatus
) -> None:
    """Catch an authoritative startup reason being hidden behind the intentionally absent trace."""
    executable, replay, data_root = _inputs(tmp_path)

    def publish(request: ProcessLaunchRequest) -> None:
        _publish_outcome(
            _argument_path(request, "-replay-outcome"),
            playback_started=False,
            terminal_reason=terminal_reason,
        )

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(publish, exit_code=1),
        run_id_factory=lambda: "523e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is expected_status
    assert result.trace_path is None
    assert result.outcome_path is not None


def test_nonzero_exit_with_valid_clean_evidence_remains_engine_failure(tmp_path: Path) -> None:
    """Catch independently valid evidence erasing a contradictory nonzero engine process result."""
    executable, replay, data_root = _inputs(tmp_path)
    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(_publish_valid_evidence, exit_code=7),
        run_id_factory=lambda: "623e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.ENGINE_FAILURE
    assert result.trace_path is not None
    assert result.strategy_analysis_scope is StrategyAnalysisScope.NONE


@pytest.mark.parametrize(
    ("publisher", "exit_code", "expected"),
    [
        (None, 0, EngineRunStatus.MISSING_OUTCOME),
        (None, 9, EngineRunStatus.ENGINE_FAILURE),
        (lambda request: _publish_outcome(_argument_path(request, "-replay-outcome")), 0, EngineRunStatus.MISSING_TRACE),
    ],
)
def test_missing_evidence_has_source_grounded_precedence(
    tmp_path: Path,
    publisher: Callable[[ProcessLaunchRequest], None] | None,
    exit_code: int,
    expected: EngineRunStatus,
) -> None:
    executable, replay, data_root = _inputs(tmp_path)
    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(publisher, exit_code=exit_code),
        run_id_factory=lambda: "723e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is expected
    assert result.trace_path is None


def test_outcome_loader_is_versioned_closed_and_coherent(tmp_path: Path) -> None:
    """Catch unversioned, extended, or internally contradictory independent evidence."""
    path = tmp_path / "outcome.json"
    _publish_outcome(path, final_frame=108, command_count=16, terminal_reason="crc_mismatch", crc_mismatch_frame=105)
    outcome = load_replay_outcome(path)
    assert outcome.schema_version == 1
    assert outcome.final_frame == 108

    invalid_values = [
        {"schema_version": 2},
        {"unexpected": True},
        {"crc_mismatch": False},
        {"crc_mismatch_frame": None},
        {"playback_started": False},
    ]
    original = json.loads(path.read_text(encoding="utf-8"))
    for index, changes in enumerate(invalid_values):
        invalid_path = tmp_path / f"invalid-{index}.json"
        invalid_path.write_bytes((json.dumps({**original, **changes}) + "\n").encode("utf-8"))
        with pytest.raises(ReplayOutcomeValidationError):
            load_replay_outcome(invalid_path)


def test_outcome_loader_rejects_file_replacement_between_preflight_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a same-size publisher race substituting a different valid outcome inode."""
    path = tmp_path / "outcome.json"
    replacement = tmp_path / "replacement.json"
    _publish_outcome(path, final_frame=0)
    _publish_outcome(replacement, final_frame=1)
    assert path.stat().st_size == replacement.stat().st_size
    original_open = Path.open
    replaced = False

    def replace_before_open(candidate: Path, *args: object, **kwargs: object) -> object:
        nonlocal replaced
        if candidate == path and not replaced:
            replaced = True
            os.replace(replacement, path)
        return original_open(candidate, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", replace_before_open)

    with pytest.raises(ReplayOutcomeValidationError, match="changed before|identity"):
        load_replay_outcome(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
        ("movement_sample_frames", 0),
        ("movement_sample_frames", 3601),
        ("movement_sample_frames", 15.0),
    ],
)
def test_config_rejects_non_strict_or_out_of_range_values(tmp_path: Path, field: str, value: object) -> None:
    executable, _replay, data_root = _inputs(tmp_path)
    with pytest.raises(EngineRunConfigurationError):
        _config(executable, data_root, **{field: value})


def test_runner_refuses_relative_inputs_and_existing_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catch implicit cwd resolution or reuse of failed/stale run artifacts."""
    executable, replay, data_root = _inputs(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(EngineRunConfigurationError, match="absolute"):
        _config(Path("runtime/generalszh.exe"), data_root)

    run_id = "823e4567-e89b-42d3-a456-426614174000"
    existing = data_root / "runs" / run_id
    existing.mkdir(parents=True)
    marker = existing / "caller-owned.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(EngineRunConfigurationError, match="already exists"):
        export_telemetry(replay, _config(executable, data_root), launcher=FakeLauncher(), run_id_factory=lambda: run_id)
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_config_rejects_unsafe_windows_path_alias_components(tmp_path: Path) -> None:
    """Catch trailing-dot and reserved-name aliases before any product directory is created."""
    executable, _replay, data_root = _inputs(tmp_path)

    for unsafe_root in (data_root.with_name("unsafe-root."), data_root / "NUL" / "child"):
        with pytest.raises(EngineRunConfigurationError, match="unsafe Windows path component"):
            _config(executable, unsafe_root)
        assert not unsafe_root.exists()


def test_config_rejects_an_absolute_but_unresolved_parent_alias(tmp_path: Path) -> None:
    executable, _replay, data_root = _inputs(tmp_path)
    unresolved = data_root / "discarded-component" / ".."

    with pytest.raises(EngineRunConfigurationError, match="unsafe Windows path component|already resolved"):
        _config(executable, unresolved)


def test_runner_rejects_root_that_cannot_fit_worst_case_ansi_map_transaction(tmp_path: Path) -> None:
    """Catch Task 8 reaching map_write_failed because the runner skipped ANSI MAX_PATH preflight."""
    executable, replay, _data_root = _inputs(tmp_path)
    long_root = tmp_path.resolve()
    while len(str(long_root / "runs" / ("9" * 36))) < 230:
        long_root /= "path-segment-1234567890"

    with pytest.raises(EngineRunConfigurationError, match="ANSI MAX_PATH"):
        export_telemetry(
            replay,
            _config(executable, long_root),
            launcher=FakeLauncher(),
            run_id_factory=lambda: "923e4567-e89b-42d3-a456-426614174000",
        )
    assert not long_root.exists()


def test_runner_preflights_every_engine_bound_path_as_ansi_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, replay, data_root = _inputs(tmp_path)
    encoded_paths: list[Path] = []

    def oversized_replay(path: Path) -> bytes:
        encoded_paths.append(path)
        return b"x" * 260 if path == replay else str(path).encode("ascii")

    monkeypatch.setattr(runner_module, "_ansi_path_bytes", oversized_replay)

    with pytest.raises(EngineRunConfigurationError, match="ANSI MAX_PATH"):
        export_telemetry(
            replay,
            _config(executable, data_root),
            launcher=FakeLauncher(),
            run_id_factory=lambda: "a13e4567-e89b-42d3-a456-426614174000",
        )
    assert executable in encoded_paths
    assert executable.parent in encoded_paths
    assert replay in encoded_paths
    assert not data_root.exists()


def test_request_metadata_binds_input_hashes_and_original_replay_is_unchanged(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)
    original_hash = hashlib.sha256(replay.read_bytes()).hexdigest()
    launcher = FakeLauncher(_publish_valid_evidence)
    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=launcher,
        run_id_factory=lambda: "a23e4567-e89b-42d3-a456-426614174000",
    )

    request = json.loads((result.run_dir / "request.json").read_text(encoding="utf-8"))
    assert request["replay"] == {
        "path": str(replay),
        "sha256": original_hash,
        "size": len(replay.read_bytes()),
    }
    assert request["engine"]["path"] == str(executable)
    assert request["engine"]["sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert request["argv"] == list(launcher.requests[0].argv)
    assert hashlib.sha256(replay.read_bytes()).hexdigest() == original_hash


def test_writer_error_and_outcome_mismatch_never_expose_unvalidated_trace(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    writer_result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(lambda request: _publish_valid_evidence(request, writer_error="disk full")),
        run_id_factory=lambda: "b23e4567-e89b-42d3-a456-426614174000",
    )
    assert writer_result.status is EngineRunStatus.WRITER_ERROR
    assert writer_result.trace_path is None

    def mismatch(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        _publish_outcome(_argument_path(request, "-replay-outcome"), final_frame=1)

    mismatch_result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(mismatch),
        run_id_factory=lambda: "c23e4567-e89b-42d3-a456-426614174000",
    )
    assert mismatch_result.status is EngineRunStatus.OUTCOME_MISMATCH
    assert mismatch_result.trace_path is None


def test_corrupt_catalog_is_asset_invalid_and_failed_directory_is_preserved(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def corrupt(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        catalog = next(request.run_dir.glob("game-data-catalog-v1-*.json"))
        catalog.write_bytes(b"corrupt")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(corrupt),
        run_id_factory=lambda: "d23e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.ASSET_INVALID
    assert result.trace_path is None
    assert next(result.run_dir.glob("game-data-catalog-v1-*.json")).read_bytes() == b"corrupt"


def test_missing_catalog_is_asset_invalid_and_failed_directory_is_preserved(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def remove_catalog(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        next(request.run_dir.glob("game-data-catalog-v1-*.json")).unlink()

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(remove_catalog),
        run_id_factory=lambda: "d33e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.ASSET_INVALID
    assert result.trace_path is None
    assert result.run_dir.is_dir()


def test_unexpected_or_hardlinked_success_output_is_rejected_without_cleanup(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def publish_extra(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        (request.run_dir / "unexpected.bin").write_bytes(b"caller-owned")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(publish_extra),
        run_id_factory=lambda: "e23e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.UNSAFE_OUTPUT
    assert result.trace_path is None
    assert (result.run_dir / "unexpected.bin").read_bytes() == b"caller-owned"

    if os.name == "nt":
        def publish_hardlink(request: ProcessLaunchRequest) -> None:
            _publish_valid_evidence(request)
            os.link(request.stdout_path, request.run_dir / "stdout-alias.log")

        hardlink_result = export_telemetry(
            replay,
            _config(executable, data_root / "hardlink-data"),
            launcher=FakeLauncher(publish_hardlink),
            run_id_factory=lambda: "f23e4567-e89b-42d3-a456-426614174000",
        )
        assert hardlink_result.status is EngineRunStatus.UNSAFE_OUTPUT
        assert hardlink_result.stdout_path is None
        assert (hardlink_result.run_dir / "stdout.log").stat().st_nlink == 2


@pytest.mark.parametrize("poison_name", ["result.json", ".result.json.tmp"])
def test_result_metadata_collision_returns_typed_failure_without_deleting_poison(
    tmp_path: Path,
    poison_name: str,
) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def poison_result(request: ProcessLaunchRequest) -> None:
        (request.run_dir / poison_name).write_bytes(b"caller-owned")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(poison_result),
        run_id_factory=lambda: "e33e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.UNSAFE_OUTPUT
    assert (result.run_dir / poison_name).read_bytes() == b"caller-owned"
    assert len(tuple(result.run_dir.glob("runner-result*.json"))) == 1


def test_missing_runner_owned_log_is_not_exposed_as_a_public_diagnostic_path(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def remove_stdout(request: ProcessLaunchRequest) -> None:
        request.stdout_handle.close()
        request.stdout_path.unlink()

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(remove_stdout),
        run_id_factory=lambda: "e43e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.UNSAFE_OUTPUT
    assert result.stdout_path is None
    assert result.stderr_path is not None


def test_timeout_result_without_verified_tree_termination_is_a_launcher_failure(tmp_path: Path) -> None:
    """Catch an injected or future launcher returning while descendant processes may still be alive."""
    executable, replay, data_root = _inputs(tmp_path)

    def unsafe_timeout(_request: ProcessLaunchRequest) -> ProcessExecution:
        return ProcessExecution(1, True, 2.0, False, None)

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=unsafe_timeout,
        run_id_factory=lambda: "013e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.LAUNCH_FAILURE
    assert any("ProcessExecution" in diagnostic.message for diagnostic in result.diagnostics)


def test_invalid_completion_and_publish_race_are_invalid_trace(tmp_path: Path) -> None:
    """Catch digest-independent completion tampering or a stale publisher winning the fixed trace name."""
    executable, replay, data_root = _inputs(tmp_path)

    def invalid_completion(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        trace = _argument_path(request, "-telemetry")
        records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        records[-1]["payload"]["event_counts"]["manifest"] = 2
        trace.write_bytes(
            b"".join(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in records)
        )

    invalid = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(invalid_completion),
        run_id_factory=lambda: "113e4567-e89b-42d3-a456-426614174000",
    )
    assert invalid.status is EngineRunStatus.INVALID_TRACE
    assert invalid.trace_path is None

    def stale_publish(request: ProcessLaunchRequest) -> None:
        _argument_path(request, "-telemetry").write_bytes(b"stale caller bytes")
        _publish_outcome(_argument_path(request, "-replay-outcome"))

    raced = export_telemetry(
        replay,
        _config(executable, data_root / "race"),
        launcher=FakeLauncher(stale_publish),
        run_id_factory=lambda: "213e4567-e89b-42d3-a456-426614174000",
    )
    assert raced.status is EngineRunStatus.INVALID_TRACE
    assert (raced.run_dir / "trace.ndjson").read_bytes() == b"stale caller bytes"


def test_startup_outcome_rejects_contradictory_playback_artifacts(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def contradictory_startup(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        outcome = _argument_path(request, "-replay-outcome")
        outcome.unlink()
        _publish_outcome(outcome, playback_started=False, terminal_reason="invalid_replay_header")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(contradictory_startup, exit_code=1),
        run_id_factory=lambda: "293e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.OUTCOME_MISMATCH
    assert result.trace_path is None


def test_windows_launcher_source_marks_child_owned_before_fallible_handle_reset(repository_root: Path) -> None:
    """Guard the suspended-child cleanup branch and job-wide settlement API against regression."""
    source = (
        repository_root
        / "scripts/replay_analyzer/src/generals_replay_analyzer/engine/runner.py"
    ).read_text(encoding="utf-8")
    launcher = source.split("def _windows_process_launcher", maxsplit=1)[1]
    created_branch = launcher.split("created = kernel32.CreateProcessW", maxsplit=1)[1]
    assert created_branch.index("process_created = True") < created_branch.index(
        "os.set_handle_inheritable(handle, False)"
    )
    assert "QueryInformationJobObject" in launcher
    assert "wait_for_job_empty" in launcher
    assert "active_job_processes() != 0" in launcher


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcessW/Job Object contract")
def test_windows_launcher_terminates_suspended_child_when_handle_reset_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the reviewed failure window rather than relying only on source ordering."""
    marker = tmp_path / "child-resumed.txt"
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    original_set_inheritable = os.set_handle_inheritable

    def fail_first_reset(handle: int, inheritable: bool) -> None:
        if not inheritable:
            raise OSError("injected handle reset failure")
        original_set_inheritable(handle, inheritable)

    monkeypatch.setattr(runner_module.os, "set_handle_inheritable", fail_first_reset)
    with stdout_path.open("xb", buffering=0) as stdout_handle, stderr_path.open("xb", buffering=0) as stderr_handle:
        request = ProcessLaunchRequest(
            run_id="a23e4567-e89b-42d3-a456-426614174000",
            run_dir=tmp_path,
            argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('resumed')"),
            cwd=Path(sys.executable).parent,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
            timeout_seconds=10,
        )
        with pytest.raises(OSError, match="injected handle reset failure"):
            runner_module._windows_process_launcher(request)

    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcessW/Job Object contract")
def test_windows_launcher_timeout_settles_spawned_descendant_tree(tmp_path: Path) -> None:
    """Prove timeout does not return while a descendant remains able to finish work."""
    child_started = tmp_path / "child-started.txt"
    child_completed = tmp_path / "child-completed.txt"
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    child_script = (
        "from pathlib import Path; import time; "
        f"Path({str(child_started)!r}).write_text('started'); time.sleep(5); "
        f"Path({str(child_completed)!r}).write_text('completed')"
    )
    root_script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_script!r}]); time.sleep(30)"
    with stdout_path.open("xb", buffering=0) as stdout_handle, stderr_path.open("xb", buffering=0) as stderr_handle:
        request = ProcessLaunchRequest(
            run_id="b23e4567-e89b-42d3-a456-426614174000",
            run_dir=tmp_path,
            argv=(sys.executable, "-c", root_script),
            cwd=Path(sys.executable).parent,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
            timeout_seconds=2,
        )
        execution = runner_module._windows_process_launcher(request)

    assert execution.timed_out is True
    assert execution.process_tree_terminated is True
    assert execution.termination_method == "windows_job_object"
    assert child_started.is_file()
    time.sleep(0.25)
    assert not child_completed.exists()


@pytest.mark.parametrize("member", ["manifest.json", "height.f32.zlib"])
def test_missing_or_corrupt_map_member_is_asset_invalid(tmp_path: Path, member: str) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def break_map(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        target = next((request.run_dir / "map-assets-v1").glob(f"*/{member}"))
        if member == "manifest.json":
            target.write_bytes(b"not-json")
        else:
            target.unlink()

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(break_map),
        run_id_factory=lambda: "313e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.ASSET_INVALID
    assert result.map_assets == ()
    assert result.run_dir.is_dir()


def test_unexpected_nested_map_output_is_asset_invalid_and_preserved(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)

    def publish_extra_map_output(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        extra = request.run_dir / "map-assets-v1" / "unexpected-transaction"
        extra.mkdir()
        (extra / "partial.bin").write_bytes(b"preserve")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(publish_extra_map_output),
        run_id_factory=lambda: "393e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.ASSET_INVALID
    assert result.trace_path is None
    assert (result.run_dir / "map-assets-v1" / "unexpected-transaction" / "partial.bin").read_bytes() == b"preserve"


def test_runner_detects_replay_mutation_without_restoring_or_deleting_caller_bytes(tmp_path: Path) -> None:
    """Catch engine execution mutating the source replay while the runner reports trusted evidence."""
    executable, replay, data_root = _inputs(tmp_path)

    def mutate(request: ProcessLaunchRequest) -> None:
        _argument_path(request, "-replay").write_bytes(b"mutated-by-launcher")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(mutate),
        run_id_factory=lambda: "413e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.INPUT_CHANGED
    assert replay.read_bytes() == b"mutated-by-launcher"
    assert result.run_dir.is_dir()


def test_runner_detects_engine_mutation_without_restoring_or_deleting_caller_bytes(tmp_path: Path) -> None:
    """Catch the launched binary identity changing while its outputs are being produced."""
    executable, replay, data_root = _inputs(tmp_path)

    def mutate_engine(request: ProcessLaunchRequest) -> None:
        Path(request.argv[0]).write_bytes(b"mutated-engine")

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(mutate_engine),
        run_id_factory=lambda: "513e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.INPUT_CHANGED
    assert executable.read_bytes() == b"mutated-engine"
    assert result.run_dir.is_dir()


def test_runner_rejects_reparse_input_alias_when_host_supports_symlinks(tmp_path: Path) -> None:
    executable, replay, data_root = _inputs(tmp_path)
    alias = tmp_path / "replay-alias.rep"
    try:
        alias.symlink_to(replay)
    except OSError:
        pytest.skip("host does not grant unprivileged symlink creation")

    with pytest.raises(EngineRunConfigurationError, match="reparse|symlink"):
        export_telemetry(alias.absolute(), _config(executable, data_root), launcher=FakeLauncher())


def test_runner_rejects_a_hardlinked_trace_even_when_trace_bytes_validate(tmp_path: Path) -> None:
    """Catch an output identity alias that the telemetry reader's content validation cannot detect."""
    executable, replay, data_root = _inputs(tmp_path)

    def hardlink_trace(request: ProcessLaunchRequest) -> None:
        _publish_valid_evidence(request)
        trace = _argument_path(request, "-telemetry")
        outside = request.run_dir.parent / f"{request.run_id}-shared.ndjson"
        outside.write_bytes(trace.read_bytes())
        trace.unlink()
        os.link(outside, trace)

    result = export_telemetry(
        replay,
        _config(executable, data_root),
        launcher=FakeLauncher(hardlink_trace),
        run_id_factory=lambda: "613e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.UNSAFE_OUTPUT
    assert (result.run_dir / "trace.ndjson").stat().st_nlink == 2


def test_real_pinned_engine_runner_cross_binds_crc_trace_outcome_and_assets(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Prove the public runner against the real Steam-runtime hardlink and natural CRC boundary."""
    replay_hash = hashlib.sha256(pinned_replay.read_bytes()).hexdigest()
    user_replay_directory = Path.home() / "Documents" / "Command and Conquer Generals Zero Hour Data" / "Replays"
    before_user_replays = (
        sorted((str(path.relative_to(user_replay_directory)), path.stat().st_size, path.stat().st_mtime_ns)
               for path in user_replay_directory.rglob("*") if path.is_file())
        if user_replay_directory.is_dir()
        else []
    )
    short_token = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:8]
    data_root = (tmp_path.parents[1] / f"g9-real-{short_token}").resolve()

    result = export_telemetry(
        pinned_replay,
        _config(zero_hour_runtime_executable, data_root, timeout_seconds=120),
        run_id_factory=lambda: "713e4567-e89b-42d3-a456-426614174000",
    )

    assert result.status is EngineRunStatus.VALID_CRC_MISMATCH
    assert result.exit_code != 0
    assert result.trace_path is not None
    assert result.outcome_path is not None
    records = tuple(iter_validated_trace(result.trace_path))
    complete = records[-1]
    assert isinstance(complete, CompleteRecord)
    assert complete.payload.final_frame == 108
    assert complete.payload.command_count == 16
    assert complete.payload.crc_mismatch_frame == 105
    assert load_replay_outcome(result.outcome_path).model_dump() == {
        "schema_version": 1,
        "playback_started": True,
        "final_frame": 108,
        "command_count": 16,
        "terminal_reason": "crc_mismatch",
        "crc_mismatch": True,
        "crc_mismatch_frame": 105,
    }
    assert len(result.map_assets) == 6 and all(path.is_file() for path in result.map_assets)
    assert (result.run_dir / "request.json").is_file()
    assert (result.run_dir / "result.json").is_file()
    assert hashlib.sha256(pinned_replay.read_bytes()).hexdigest() == replay_hash
    after_user_replays = (
        sorted((str(path.relative_to(user_replay_directory)), path.stat().st_size, path.stat().st_mtime_ns)
               for path in user_replay_directory.rglob("*") if path.is_file())
        if user_replay_directory.is_dir()
        else []
    )
    assert after_user_replays == before_user_replays
