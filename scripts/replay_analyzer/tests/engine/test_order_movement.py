"""Static and real-engine coverage for post-resolution orders and end-update samples."""

import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest

from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.telemetry.model import (
    CompleteRecord,
    EntitySampleRecord,
    ObjectDestroyedRecord,
    OrderIssuedRecord,
)
from generals_replay_analyzer.telemetry.order_coverage import SUPPORTED_ORDER_COVERAGE
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_ID = "a23e4567-e89b-12d3-a456-426614174000"


def _runtime_environment(repository_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    dependency_directories = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependency_directories), environment["PATH"]])
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
        pytest.fail(f"modern Zero Hour timed out during order/movement telemetry: {error}")


def _base_command(runtime_executable: Path, replay: Path) -> list[str]:
    return [str(runtime_executable), "-headless", "-noaudio", "-replay", str(replay)]


def _write_crc_free_replay(source: Path, destination: Path) -> int:
    """Create disposable mechanics-only evidence without altering the checksum-pinned replay."""
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


def _telemetry_command(runtime_executable: Path, replay: Path, trace: Path, interval: int) -> list[str]:
    return [
        *_base_command(runtime_executable, replay),
        "-telemetry",
        str(trace.resolve()),
        "-telemetry-run-id",
        RUN_ID,
        "-telemetry-movement-frames",
        str(interval),
    ]


def _outcome_command(command: list[str], path: Path) -> list[str]:
    return [*command, "-replay-outcome", str(path.resolve())]


def _read_outcome(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _deterministic_console(stdout: str) -> str:
    return "\n".join(line for line in stdout.splitlines() if not line.startswith("Elapsed Time:"))


def _normalized_non_sample_records(path: Path) -> bytes:
    result = bytearray()
    for raw_line in path.read_bytes().splitlines():
        record = cast(dict[str, object], json.loads(raw_line))
        if record["event_type"] == "entity_sample":
            continue
        record.pop("sequence")
        payload = cast(dict[str, object], record["payload"])
        if record["event_type"] == "manifest":
            settings = cast(dict[str, object], payload["exporter_settings"])
            settings["movement_sample_frames"] = "normalized"
        elif record["event_type"] == "complete":
            counts = cast(dict[str, object], payload["event_counts"])
            counts.pop("entity_sample", None)
            payload["trace_sha256"] = "normalized"
        result.extend(json.dumps(record, separators=(",", ":")).encode("utf-8"))
        result.extend(b"\n")
    return bytes(result)


def test_order_export_uses_one_post_resolution_dispatch_seam(repository_root: Path) -> None:
    dispatch = (repository_root / "Core/GameEngine/Source/GameLogic/System/GameLogicDispatch.cpp").read_text(
        encoding="utf-8"
    )

    assert dispatch.count("ReplayMovementSampler::observeResolvedOrder") == 1
    switch_index = dispatch.index("switch( msgType )")
    switch_end_index = dispatch.index("\n\t}\n\n#if RETAIL_COMPATIBLE_AIGROUP", switch_index)
    hook_index = dispatch.index("ReplayMovementSampler::observeResolvedOrder")
    assert hook_index > switch_end_index


def test_sampler_is_modern_analyzer_only_and_runs_at_end_of_logic_update(repository_root: Path) -> None:
    header = repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayMovementSampler.h"
    source = repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayMovementSampler.cpp"
    game_logic = (repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp").read_text(
        encoding="utf-8"
    )

    assert header.is_file() and source.is_file()
    assert "defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)" in header.read_text(encoding="utf-8")
    sampler = source.read_text(encoding="utf-8")
    assert "std::sort(objectIds.begin(), objectIds.end())" in sampler
    assert "std::map<ObjectID" in sampler
    sample_state = sampler.split("struct SampleState", maxsplit=1)[1].split("};", maxsplit=1)[0]
    assert "Object *" not in sample_state
    prune_state = sampler.split("void pruneDeadState()", maxsplit=1)[1].split("\n}\n\nvoid", maxsplit=1)[0]
    assert "s_currentOrders.begin()" in prune_state
    assert "s_forcedSamples.begin()" in prune_state
    end_update = game_logic.split("TheVictoryConditions->UPDATE();", maxsplit=1)[1].split("m_frame++;", maxsplit=1)[0]
    assert "ReplayMovementSampler::sampleEndOfFrame();" in end_update


def test_order_coverage_is_explicitly_closed_and_excludes_diagnostics(repository_root: Path) -> None:
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayMovementSampler.cpp"
    ).read_text(encoding="utf-8")

    assert '\\\"coverage\\\":\\\"closed_supported_subset\\\"' in source
    table_match = re.search(r"s_supportedOrders\[\] = \{(?P<table>.*?)\n\t\};", source, re.DOTALL)
    assert table_match is not None
    parsed = re.findall(
        r'GameMessage::(MSG_[A-Z0-9_]+), "(MSG_[A-Z0-9_]+)", ORDER_TARGET_(NONE|OBJECT|LOCATION), (-?\d+)',
        table_match.group("table"),
    )
    expected = [
        (name, name, target_kind.upper(), -1 if target_argument_index is None else target_argument_index)
        for _, name, target_kind, target_argument_index in SUPPORTED_ORDER_COVERAGE
    ]
    assert [(enum_name, string_name, kind, int(index)) for enum_name, string_name, kind, index in parsed] == expected
    assert "MSG_LOGIC_CRC" not in source
    assert "MSG_SET_REPLAY_CAMERA" not in source
    assert "MSG_DEBUG_" not in source


def test_sampler_does_not_mutate_simulation_or_use_randomness(repository_root: Path) -> None:
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayMovementSampler.cpp"
    ).read_text(encoding="utf-8")

    forbidden = ("LogicRandom", "ClientRandom", "rand(", "setPosition(", "setOrientation(", "aiMove", "groupMove")
    assert not [token for token in forbidden if token in source]


def test_trace_and_independent_outcome_count_the_same_preinitialization_command_seam(
    repository_root: Path,
) -> None:
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    ).read_text(encoding="utf-8")
    observer = source.split("void ReplayTelemetry::observeExecutedCommand()", maxsplit=1)[1].split(
        "void ReplayTelemetry::deferFinish", maxsplit=1
    )[0]

    assert "ReplayOutcome::observeExecutedCommand();" in observer
    assert "if (s_output != nullptr)" in observer
    assert "s_initialized" not in observer


def test_natural_pinned_replay_retains_frame_108_crc_boundary(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Use the natural replay only for its checksum boundary, never strategy or outcome claims."""
    original_hash = hashlib.sha256(pinned_replay.read_bytes()).hexdigest()
    trace = tmp_path / "natural-order-movement.ndjson"
    baseline_outcome = tmp_path / "natural-baseline-outcome.json"
    telemetry_outcome = tmp_path / "natural-telemetry-outcome.json"
    baseline = _run_engine(
        _outcome_command(_base_command(zero_hour_runtime_executable, pinned_replay), baseline_outcome),
        zero_hour_runtime_executable.parent,
        repository_root,
    )
    completed = _run_engine(
        _outcome_command(
            _telemetry_command(zero_hour_runtime_executable, pinned_replay, trace, 15), telemetry_outcome
        ),
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    records = tuple(iter_validated_trace(trace))
    terminal = records[-1]
    assert isinstance(terminal, CompleteRecord)
    assert completed.returncode == baseline.returncode != 0
    assert _deterministic_console(completed.stdout) == _deterministic_console(baseline.stdout)
    assert completed.stderr == baseline.stderr
    assert _read_outcome(telemetry_outcome) == _read_outcome(baseline_outcome)
    outcome = _read_outcome(baseline_outcome)
    assert outcome == {
        "playback_started": True,
        "final_frame": 108,
        "command_count": terminal.payload.command_count,
        "terminal_reason": "crc_mismatch",
        "crc_mismatch": True,
        "crc_mismatch_frame": 105,
    }
    assert terminal.frame == 108
    assert terminal.payload.crc_mismatch is True
    assert terminal.payload.crc_mismatch_frame == 105
    assert hashlib.sha256(pinned_replay.read_bytes()).hexdigest() == original_hash


def test_crc_free_mechanics_trace_is_deterministic_and_only_interval_changes_sample_density(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Treat the CRC-stripped derivative only as order/sampling mechanics and density evidence."""
    original_hash = hashlib.sha256(pinned_replay.read_bytes()).hexdigest()
    derivative = tmp_path / "mechanics-only-crc-free.rep"
    final_command_frame = _write_crc_free_replay(pinned_replay, derivative)
    derivative_hash = hashlib.sha256(derivative.read_bytes()).hexdigest()
    trace_15_a = tmp_path / "movement-15-a.ndjson"
    trace_15_b = tmp_path / "movement-15-b.ndjson"
    trace_30 = tmp_path / "movement-30.ndjson"
    baseline_outcome = tmp_path / "mechanics-baseline-outcome.json"
    outcome_15_a = tmp_path / "mechanics-15-a-outcome.json"
    outcome_15_b = tmp_path / "mechanics-15-b-outcome.json"
    outcome_30 = tmp_path / "mechanics-30-outcome.json"
    baseline = _run_engine(
        _outcome_command(_base_command(zero_hour_runtime_executable, derivative), baseline_outcome),
        zero_hour_runtime_executable.parent,
        repository_root,
    )
    completed_15_a = _run_engine(
        _outcome_command(_telemetry_command(zero_hour_runtime_executable, derivative, trace_15_a, 15), outcome_15_a),
        zero_hour_runtime_executable.parent,
        repository_root,
    )
    completed_15_b = _run_engine(
        _outcome_command(_telemetry_command(zero_hour_runtime_executable, derivative, trace_15_b, 15), outcome_15_b),
        zero_hour_runtime_executable.parent,
        repository_root,
    )
    assert completed_15_a.returncode == completed_15_b.returncode == baseline.returncode
    assert (
        _deterministic_console(completed_15_a.stdout)
        == _deterministic_console(completed_15_b.stdout)
        == _deterministic_console(baseline.stdout)
    )
    assert completed_15_a.stderr == completed_15_b.stderr == baseline.stderr
    assert _read_outcome(outcome_15_a) == _read_outcome(outcome_15_b) == _read_outcome(baseline_outcome)
    assert trace_15_a.read_bytes() == trace_15_b.read_bytes()
    trace_15_b.unlink()

    completed_30 = _run_engine(
        _outcome_command(_telemetry_command(zero_hour_runtime_executable, derivative, trace_30, 30), outcome_30),
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed_15_a.returncode == completed_30.returncode == baseline.returncode
    assert (
        _deterministic_console(completed_15_a.stdout)
        == _deterministic_console(completed_30.stdout)
        == _deterministic_console(baseline.stdout)
    )
    assert completed_15_a.stderr == completed_30.stderr == baseline.stderr
    assert _read_outcome(outcome_15_a) == _read_outcome(outcome_30) == _read_outcome(baseline_outcome)
    records_15 = tuple(iter_validated_trace(trace_15_a))
    records_30 = tuple(iter_validated_trace(trace_30))
    complete_15 = records_15[-1]
    complete_30 = records_30[-1]
    assert isinstance(complete_15, CompleteRecord) and isinstance(complete_30, CompleteRecord)
    assert baseline.returncode == 0
    assert complete_15.frame == complete_30.frame == final_command_frame + 1
    assert complete_15.payload.crc_mismatch is complete_30.payload.crc_mismatch is False
    assert complete_15.payload.command_count == complete_30.payload.command_count
    assert _read_outcome(baseline_outcome) == {
        "playback_started": True,
        "final_frame": final_command_frame + 1,
        "command_count": complete_15.payload.command_count,
        "terminal_reason": "clean_completion",
        "crc_mismatch": False,
        "crc_mismatch_frame": None,
    }

    orders = [record for record in records_15 if isinstance(record, OrderIssuedRecord)]
    samples_15 = [record for record in records_15 if isinstance(record, EntitySampleRecord)]
    samples_30 = [record for record in records_30 if isinstance(record, EntitySampleRecord)]
    assert orders, "full mechanics playback did not reach any closed supported post-resolution order"
    assert len(samples_15) > len(samples_30) > 0
    assert _normalized_non_sample_records(trace_15_a) == _normalized_non_sample_records(trace_30)

    frames_by_object: dict[int, list[EntitySampleRecord]] = defaultdict(list)
    for sample in samples_15:
        frames_by_object[sample.payload.object_id].append(sample)
    destroyed_frames = {
        record.payload.object_id: record.frame
        for record in records_15
        if isinstance(record, ObjectDestroyedRecord)
    }
    bounded_gaps: list[int] = []
    for object_samples in frames_by_object.values():
        for previous, current in pairwise(object_samples):
            if (
                previous.payload.is_engine_moving
                and previous.payload.is_mobile
                and not previous.payload.is_structure
                and not previous.payload.is_disabled
            ):
                gap = current.frame - previous.frame
                bounded_gaps.append(gap)
                assert gap <= 15
        last = object_samples[-1]
        if (
            last.payload.is_engine_moving
            and last.payload.is_mobile
            and not last.payload.is_structure
            and not last.payload.is_disabled
        ):
            tail_gap = destroyed_frames.get(last.payload.object_id, complete_15.frame) - last.frame
            bounded_gaps.append(tail_gap)
            assert tail_gap <= 15
    print(
        f"Task7 density evidence: interval15={len(samples_15)} interval30={len(samples_30)} "
        f"max_moving_gap15={max(bounded_gaps, default=0)}"
    )

    assert hashlib.sha256(pinned_replay.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256(derivative.read_bytes()).hexdigest() == derivative_hash
