"""Real-engine behavior tests for the passive telemetry trace envelope."""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from generals_replay_analyzer.telemetry.model import CompleteRecord, ManifestRecord
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_ID = "123e4567-e89b-12d3-a456-426614174000"


def _runtime_environment(repository_root: Path) -> dict[str, str]:
    """Expose build dependencies while the hardlinked executable resolves retail game data beside itself."""
    environment = os.environ.copy()
    dependency_directories = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependency_directories), environment["PATH"]])
    return environment


@pytest.fixture(scope="module")
def zero_hour_runtime_executable(zero_hour_executable: Path) -> Iterator[Path]:
    """Run the build with retail data without copying or replacing any installed executable."""
    override = os.environ.get("GENERALS_REPLAY_ANALYZER_GAME_DIR")
    game_directory = (
        Path(override)
        if override
        else Path(r"C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour")
    )
    if not game_directory.is_dir():
        pytest.skip(f"Zero Hour game data directory is absent: {game_directory}")

    runtime_executable = game_directory / f"generalszh_replay_analyzer_{os.getpid()}_{uuid4().hex}.exe"
    if runtime_executable.exists():
        pytest.fail(f"refusing to replace existing runtime executable: {runtime_executable}")
    os.link(zero_hour_executable, runtime_executable)
    try:
        yield runtime_executable
    finally:
        if runtime_executable.exists():
            if not os.path.samefile(zero_hour_executable, runtime_executable):
                pytest.fail(f"refusing to remove a runtime path that is no longer the test hardlink: {runtime_executable}")
            runtime_executable.unlink()


def _run_engine(
    command: list[str],
    game_directory: Path,
    repository_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Launch the real executable and make an engine hang an explicit test failure."""
    try:
        return subprocess.run(
            command,
            cwd=game_directory,
            env=_runtime_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"modern Zero Hour timed out during telemetry integration: {error}")


def _base_command(runtime_executable: Path, pinned_replay: Path) -> list[str]:
    return [
        str(runtime_executable),
        "-headless",
        "-noaudio",
        "-replay",
        str(pinned_replay),
    ]


def test_headless_replay_writes_a_valid_passive_telemetry_envelope(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch a writer that omits, fabricates, or corrupts the real replay's terminal evidence envelope."""
    trace_path = (tmp_path / "telemetry.ndjson").resolve()
    base_command = _base_command(zero_hour_runtime_executable, pinned_replay)
    baseline = _run_engine(base_command, zero_hour_runtime_executable.parent, repository_root)
    completed = _run_engine(
        [
            *base_command,
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
            "-telemetry-movement-frames",
            "15",
        ],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode == baseline.returncode
    assert completed.stdout == baseline.stdout
    assert completed.stderr == baseline.stderr
    assert trace_path.is_file(), "telemetry-enabled playback did not create its configured trace"

    records = tuple(iter_validated_trace(trace_path))
    assert len(records) == 2
    manifest, complete = records
    assert isinstance(manifest, ManifestRecord)
    assert isinstance(complete, CompleteRecord)
    assert manifest.run_id == UUID(RUN_ID)
    assert manifest.sequence == 0
    assert manifest.frame == 0
    assert manifest.logic_time_seconds == 0.0
    assert manifest.payload.exporter_settings == {"movement_sample_frames": 15}
    assert manifest.payload.engine_build
    assert manifest.payload.replay_version
    assert manifest.payload.map_identity
    assert complete.sequence == 1
    assert complete.payload.final_frame == complete.frame
    assert complete.logic_time_seconds == complete.frame / 30.0
    assert complete.payload.command_count > 0
    assert complete.payload.event_counts == {"manifest": 1, "complete": 1}
    assert complete.payload.crc_mismatch == (completed.returncode != 0)
    assert complete.payload.replay_truncated is False
    assert complete.payload.clean_shutdown == (completed.returncode == 0)
    assert complete.payload.writer_error is None
    assert complete.payload.map_assets == []


@pytest.mark.parametrize(
    ("arguments", "diagnostic"),
    [
        (["-telemetry", "relative.ndjson", "-telemetry-run-id", RUN_ID], "absolute"),
        (["-telemetry", "{trace}", "-telemetry-run-id", "not-a-uuid"], "UUID"),
        (["-telemetry", "{trace}", "-telemetry-run-id", RUN_ID, "-telemetry-movement-frames", "0"], "positive"),
        (["-telemetry", "{trace}"], "run ID"),
        (["-telemetry-run-id", RUN_ID], "requires -telemetry"),
        (["-telemetry-movement-frames", "15"], "requires -telemetry"),
        (["-telemetry", "{trace}", "-telemetry-run-id", RUN_ID, "-jobs", "2"], "sequential"),
    ],
)
def test_invalid_telemetry_settings_fail_before_replay_playback(
    arguments: list[str],
    diagnostic: str,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch startup validation that permits an unusable telemetry run to reach GameLogic playback."""
    trace_path = (tmp_path / "invalid.ndjson").resolve()
    resolved_arguments = [str(trace_path) if argument == "{trace}" else argument for argument in arguments]
    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, pinned_replay), *resolved_arguments],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode != 0
    assert "Simulating Replay" not in completed.stdout
    assert diagnostic in completed.stderr
    assert not trace_path.exists()


def test_telemetry_requires_one_headless_replay_before_playback(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch activation outside the single headless replay mode that owns one trace lifecycle."""
    trace_path = (tmp_path / "invalid-combination.ndjson").resolve()
    telemetry_arguments = ["-telemetry", str(trace_path), "-telemetry-run-id", RUN_ID]
    commands = (
        [str(zero_hour_runtime_executable), "-headless", "-noaudio", *telemetry_arguments],
        [str(zero_hour_runtime_executable), "-noaudio", "-replay", str(pinned_replay), *telemetry_arguments],
        [
            *_base_command(zero_hour_runtime_executable, pinned_replay),
            "-replay",
            str(pinned_replay),
            *telemetry_arguments,
        ],
    )

    for command in commands:
        completed = _run_engine(command, zero_hour_runtime_executable.parent, repository_root)
        assert completed.returncode != 0
        assert "Simulating Replay" not in completed.stdout
        assert "exactly one headless replay" in completed.stderr
        assert not trace_path.exists()


def test_writer_open_failure_is_diagnostic_only_for_replay_execution(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch a sink-open failure feeding back into replay output, CRC handling, or exit status."""
    trace_path = (tmp_path / "missing-parent" / "telemetry.ndjson").resolve()
    base_command = _base_command(zero_hour_runtime_executable, pinned_replay)
    baseline = _run_engine(base_command, zero_hour_runtime_executable.parent, repository_root)
    failed_sink = _run_engine(
        [*base_command, "-telemetry", str(trace_path), "-telemetry-run-id", RUN_ID],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert failed_sink.returncode == baseline.returncode
    assert failed_sink.stdout == baseline.stdout
    assert "ReplayTelemetry" in failed_sink.stderr
    assert str(trace_path) in failed_sink.stderr
    assert not trace_path.exists()
