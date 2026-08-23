"""Real-engine behavior for sampled scouting visibility telemetry."""

import hashlib
import json
import os
import shutil
import subprocess
import winreg
from collections.abc import Mapping
from ctypes import create_unicode_buffer, windll
from pathlib import Path
from typing import cast

import pytest

from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.telemetry.model import (
    CompleteRecord,
    ObjectCreatedRecord,
    ObjectDestroyedRecord,
    ObjectVisibilityChangedRecord,
    VisibilitySamplingSummaryRecord,
)
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_ID = "a23e4567-e89b-12d3-a456-426614174040"


def _runtime_environment(repository_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    dependency_directories = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join(
        [*(str(path.resolve()) for path in dependency_directories), environment["PATH"]]
    )
    return environment


def _run_engine(
    command: list[str],
    game_directory: Path,
    repository_root: Path,
    environment_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = _runtime_environment(repository_root)
    if environment_overrides is not None:
        environment.update(environment_overrides)
    try:
        return subprocess.run(
            command,
            cwd=game_directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"modern Zero Hour timed out during visibility telemetry: {error}")


def _write_crc_free_replay(source: Path, destination: Path) -> int:
    parsed = parse_replay(source)
    source_bytes = source.read_bytes()
    records = [source_bytes[: parsed.command_stream_offset]]
    final_command_frame = 0
    for command in parsed.commands:
        if command.message_name == "MSG_LOGIC_CRC":
            continue
        records.append(source_bytes[command.start_offset : command.end_offset])
        final_command_frame = command.frame
    destination.write_bytes(b"".join(records))
    return final_command_frame


def _default_user_data_root() -> Path:
    documents = create_unicode_buffer(32_768)
    result = windll.shell32.SHGetFolderPathW(None, 5, None, 0, documents)
    assert result == 0 and documents.value
    registry_path = r"SOFTWARE\Electronic Arts\EA Games\Command and Conquer Generals Zero Hour"
    leaf_name: str | None = None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in (winreg.KEY_READ | winreg.KEY_WOW64_32KEY, winreg.KEY_READ):
            try:
                with winreg.OpenKey(hive, registry_path, 0, access) as key:
                    value, _kind = winreg.QueryValueEx(key, "UserDataLeafName")
            except OSError:
                continue
            if isinstance(value, str) and value:
                leaf_name = value
                break
        if leaf_name is not None:
            break
    return (Path(documents.value) / (leaf_name or "Command and Conquer Generals Zero Hour Data")).resolve()


def _stage_pinned_map(destination: Path) -> Path:
    source = _default_user_data_root() / "Maps" / "[RANK] Sand Scorpion"
    assert source.is_dir()
    maps = destination / "Maps"
    maps.mkdir(parents=True)
    shutil.copytree(source, maps / source.name)
    return destination


def _base_command(runtime_executable: Path, replay: Path, user_data_root: Path) -> list[str]:
    return [
        str(runtime_executable),
        "-headless",
        "-noaudio",
        "-replay",
        str(replay),
        "-replay-user-data-root",
        str(user_data_root),
    ]


def _telemetry_command(
    runtime_executable: Path, replay: Path, trace: Path, user_data_root: Path
) -> list[str]:
    return [
        *_base_command(runtime_executable, replay, user_data_root),
        "-telemetry",
        str(trace.resolve()),
        "-telemetry-run-id",
        RUN_ID,
    ]


def _outcome_command(command: list[str], path: Path) -> list[str]:
    return [*command, "-replay-outcome", str(path.resolve())]


def _read_outcome(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _deterministic_console(stdout: str) -> str:
    return "\n".join(line for line in stdout.splitlines() if not line.startswith("Elapsed Time:"))


def _assert_sampling_cycles(summaries: list[VisibilitySamplingSummaryRecord]) -> None:
    previous: VisibilitySamplingSummaryRecord | None = None
    for summary in summaries:
        payload = summary.payload
        assert payload.cursor_end - payload.cursor_start == payload.sampled_pair_count
        assert payload.sampled_pair_count <= payload.maximum_pairs_per_pass == 8192
        assert payload.cursor_end <= payload.eligible_pair_count
        assert payload.cycle_complete == (payload.cursor_end == payload.eligible_pair_count)
        if previous is None:
            assert payload.sampling_cycle_id == payload.cursor_start == 0
        elif payload.sampling_cycle_id == previous.payload.sampling_cycle_id:
            assert not previous.payload.cycle_complete
            assert payload.cursor_start == previous.payload.cursor_end
            assert payload.eligible_pair_count == previous.payload.eligible_pair_count
        else:
            assert previous.payload.cycle_complete
            assert payload.sampling_cycle_id == previous.payload.sampling_cycle_id + 1
            assert payload.cursor_start == 0
        previous = summary


def test_visibility_sampling_is_repeatable_ordered_and_simulation_neutral(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    original_hash = hashlib.sha256(pinned_replay.read_bytes()).hexdigest()
    derivative = tmp_path / "visibility-mechanics.rep"
    final_command_frame = _write_crc_free_replay(pinned_replay, derivative)
    user_data_root = _stage_pinned_map(tmp_path / "isolated-user-data")
    trace_a = tmp_path / "visibility-a.ndjson"
    trace_b = tmp_path / "visibility-b.ndjson"
    baseline_outcome = tmp_path / "baseline-outcome.json"
    outcome_a = tmp_path / "visibility-a-outcome.json"
    outcome_b = tmp_path / "visibility-b-outcome.json"

    baseline = _run_engine(
        _outcome_command(
            _base_command(zero_hour_runtime_executable, derivative, user_data_root), baseline_outcome
        ),
        zero_hour_runtime_executable.parent,
        repository_root,
    )
    first = _run_engine(
        _outcome_command(
            _telemetry_command(zero_hour_runtime_executable, derivative, trace_a, user_data_root), outcome_a
        ),
        zero_hour_runtime_executable.parent,
        repository_root,
    )
    second = _run_engine(
        _outcome_command(
            _telemetry_command(zero_hour_runtime_executable, derivative, trace_b, user_data_root), outcome_b
        ),
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert first.returncode == second.returncode == baseline.returncode == 0
    assert _deterministic_console(first.stdout) == _deterministic_console(second.stdout) == _deterministic_console(
        baseline.stdout
    )
    assert first.stderr == second.stderr == baseline.stderr
    assert _read_outcome(outcome_a) == _read_outcome(outcome_b) == _read_outcome(baseline_outcome)
    assert trace_a.read_bytes() == trace_b.read_bytes()

    records = tuple(iter_validated_trace(trace_a))
    complete = records[-1]
    assert isinstance(complete, CompleteRecord)
    assert complete.frame == final_command_frame + 1
    summaries = [record for record in records if isinstance(record, VisibilitySamplingSummaryRecord)]
    transitions = [record for record in records if isinstance(record, ObjectVisibilityChangedRecord)]
    assert [record.frame for record in summaries] == list(range(15, complete.frame + 1, 15))
    _assert_sampling_cycles(summaries)
    assert transitions

    live_objects: set[int] = set()
    visibility_state: dict[tuple[int, int], str] = {}
    first_clear: set[tuple[int, int]] = set()
    ordered_by_frame: dict[int, list[tuple[int, int]]] = {}
    for record in records:
        if isinstance(record, ObjectCreatedRecord):
            live_objects.add(record.payload.object_id)
        elif isinstance(record, ObjectDestroyedRecord):
            live_objects.discard(record.payload.object_id)
        elif isinstance(record, ObjectVisibilityChangedRecord):
            key = (record.payload.player_index, record.payload.object_id)
            assert record.payload.object_id in live_objects
            assert record.payload.previous_status == visibility_state.get(key, "unseen")
            if record.payload.first_observed_clear:
                assert record.payload.status == "clear"
                assert key not in first_clear
                first_clear.add(key)
            visibility_state[key] = record.payload.status
            ordered_by_frame.setdefault(record.frame, []).append(key)
    assert all(keys == sorted(keys) for keys in ordered_by_frame.values())
    assert first_clear
    assert hashlib.sha256(pinned_replay.read_bytes()).hexdigest() == original_hash
