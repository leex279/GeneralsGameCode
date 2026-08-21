"""Real-engine behavior tests for the passive telemetry trace envelope."""

import ctypes
import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import pytest

from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.telemetry.model import CompleteRecord, ManifestRecord, PlayersInitializedRecord
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


def _run_engine(
    command: list[str],
    game_directory: Path,
    repository_root: Path,
    environment_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Launch the real executable and make an engine hang an explicit test failure."""
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


def _resume_suspended_process(process: subprocess.Popen[str]) -> int:
    """Resume a Windows child through the native handle that subprocess exposes privately."""
    process_handle = process._handle  # type: ignore[attr-defined]
    return int(ctypes.windll.ntdll.NtResumeProcess(int(process_handle)))


def _write_crc_free_replay(source: Path, destination: Path) -> int:
    """Preserve real replay commands while removing comparisons that intentionally fail on the modern build."""
    parsed = parse_replay(source)
    source_bytes = source.read_bytes()
    records = [source_bytes[: parsed.command_stream_offset]]
    final_frame = 0
    for command in parsed.commands:
        if command.message_name == "MSG_LOGIC_CRC":
            continue
        records.append(source_bytes[command.start_offset : command.end_offset])
        final_frame = command.frame
    destination.write_bytes(b"".join(records))
    return final_frame


def test_release_analyzer_honors_noaudio_before_replay_startup(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch a Release analyzer that ignores -noaudio even when playback happens not to fault in Miles."""
    trace_path = (tmp_path / "noaudio-proof.ndjson").resolve()
    completed = _run_engine(
        [
            *_base_command(zero_hour_runtime_executable, pinned_replay),
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
        ],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode != 3221225477, (
        f"Release analyzer entered the retail Miles access violation despite -noaudio: "
        f"{completed.stdout}{completed.stderr}"
    )
    manifest = json.loads(trace_path.read_bytes().splitlines()[0])
    assert manifest["schema_version"] == 2
    assert manifest["payload"]["exporter_settings"]["audio_enabled"] is False
    assert isinstance(next(iter(iter_validated_trace(trace_path))), ManifestRecord)


def _write_zero_command_replay(source: Path, destination: Path) -> None:
    """Preserve the real decoded header and setup while ending before the first command frame."""
    parsed = parse_replay(source)
    destination.write_bytes(source.read_bytes()[: parsed.command_stream_offset])


def _header_string_spans(source: bytes) -> dict[str, tuple[int, int]]:
    """Locate the five Recorder NUL strings without trusting malformed replacement bytes."""
    offset = 6 + 12 + 10
    spans: dict[str, tuple[int, int]] = {}

    def take_utf16(name: str) -> None:
        nonlocal offset
        start = offset
        while source[offset : offset + 2] != b"\0\0":
            offset += 2
        offset += 2
        spans[name] = (start, offset)

    def take_ascii(name: str) -> None:
        nonlocal offset
        start = offset
        offset = source.index(b"\0", offset) + 1
        spans[name] = (start, offset)

    take_utf16("replay_name")
    offset += 16
    take_utf16("version_string")
    take_utf16("version_time_string")
    offset += 12
    take_ascii("game_options")
    take_ascii("local_player_index")
    return spans


def _replace_header_field(source: bytes, span: tuple[int, int], replacement: bytes) -> bytes:
    return source[: span[0]] + replacement + source[span[1] :]


def _max_valid_game_options(source: bytes, span: tuple[int, int]) -> bytes:
    options = source[span[0] : span[1] - 1]
    extra = b"A" * (1023 - len(options))
    padded = options.replace(b"Hleex279,", b"Hleex279" + extra + b",", 1)
    assert len(padded) == 1023
    return padded + b"\0"


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
    assert len(records) >= 3
    manifest, players, complete = records[0], records[1], records[-1]
    assert isinstance(manifest, ManifestRecord)
    assert isinstance(players, PlayersInitializedRecord)
    assert isinstance(complete, CompleteRecord)
    assert manifest.run_id == UUID(RUN_ID)
    assert manifest.sequence == 0
    assert manifest.frame == 0
    assert manifest.logic_time_seconds == 0.0
    assert manifest.schema_version == 2
    assert manifest.payload.exporter_settings["movement_sample_frames"] == 15
    assert manifest.payload.exporter_settings["audio_enabled"] is False
    order_coverage = manifest.payload.exporter_settings["order_coverage"]
    assert isinstance(order_coverage, dict)
    assert order_coverage["coverage"] == "closed_supported_subset"
    assert manifest.payload.engine_build
    assert manifest.payload.replay_version
    assert manifest.payload.map_identity
    assert players.sequence == 1
    assert players.frame == 0
    assert players.payload.players is None
    assert players.payload.slots is not None
    assert [slot.slot_index for slot in players.payload.slots] == list(range(8))
    assert players.payload.game_data_catalog == manifest.payload.game_data_catalog
    assert complete.sequence == len(records) - 1
    assert complete.payload.final_frame == complete.frame
    assert complete.logic_time_seconds == complete.frame / 30.0
    assert complete.payload.command_count > 0
    expected_event_counts: dict[str, int] = {}
    for record in records:
        expected_event_counts[record.event_type] = expected_event_counts.get(record.event_type, 0) + 1
    assert complete.payload.event_counts == expected_event_counts
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
        (["-telemetry", "{trace}", "-telemetry-run-id", RUN_ID, "-telemetry-movement-frames", "3601"], "at most 3600"),
        (
            [
                "-telemetry",
                "{trace}",
                "-telemetry-run-id",
                RUN_ID,
                "-telemetry-movement-frames",
                "999999999999999999999999999999999999",
            ],
            "positive",
        ),
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


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "relative",
        "duplicate",
        "existing",
        "telemetry_alias",
        "spelling_alias",
        "trailing_dot_alias",
        "trailing_space",
    ],
)
def test_invalid_replay_outcome_settings_fail_before_playback(
    case: str,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Keep the independent outcome channel opt-in, absolute, exclusive, and collision-safe."""
    outcome_path = (tmp_path / "outcome.json").resolve()
    second_path = (tmp_path / "second-outcome.json").resolve()
    trace_path = (tmp_path / "outcome-alias.ndjson").resolve()
    arguments = ["-replay-outcome"]
    if case == "relative":
        arguments.append("relative.json")
    elif case == "duplicate":
        arguments.extend([str(outcome_path), "-replay-outcome", str(second_path)])
    elif case == "existing":
        outcome_path.write_bytes(b"caller-owned\n")
        arguments.append(str(outcome_path))
    elif case == "telemetry_alias":
        arguments = [
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
            "-replay-outcome",
            str(trace_path),
        ]
    elif case == "spelling_alias":
        alternate_spelling = f"{trace_path.parent}\\.\\{trace_path.name}"
        arguments = [
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
            "-replay-outcome",
            alternate_spelling,
        ]
    elif case == "trailing_dot_alias":
        trace_path = (tmp_path / "Evidence.NDJSON").resolve()
        arguments = [
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
            "-replay-outcome",
            f"{trace_path.parent}\\{trace_path.name.lower()}.",
        ]
    elif case == "trailing_space":
        arguments.append(f"{outcome_path} ")

    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, pinned_replay), *arguments],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode != 0
    assert "Simulating Replay" not in completed.stdout
    assert "Replay outcome:" in completed.stderr
    if case == "existing":
        assert outcome_path.read_bytes() == b"caller-owned\n"
    else:
        assert not outcome_path.exists()
    assert not second_path.exists()
    assert not trace_path.exists()


@pytest.mark.parametrize(
    "unsafe_name",
    ["evidence.ndjson.", "evidence.ndjson ", "evidence.ndjson:stream", "CON.ndjson"],
    ids=["trailing-dot", "trailing-space", "alternate-data-stream", "reserved-device"],
)
def test_telemetry_rejects_unsafe_win32_final_components_before_playback(
    unsafe_name: str,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    trace_path = tmp_path / unsafe_name
    completed = _run_engine(
        [
            *_base_command(zero_hour_runtime_executable, pinned_replay),
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
        ],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode != 0
    assert "Simulating Replay" not in completed.stdout
    assert "unsafe Win32 final component" in completed.stderr
    assert not trace_path.exists()


def test_case_insensitive_replay_spelling_and_canonical_outputs_still_work(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    replay = tmp_path / "MixedCase.REP"
    shutil.copyfile(pinned_replay, replay)
    lowercase_replay = replay.with_name(replay.name.lower())
    outcome = (tmp_path / "canonical-outcome.json").resolve()
    completed = _run_engine(
        [
            *_base_command(zero_hour_runtime_executable, lowercase_replay),
            "-replay-outcome",
            str(outcome),
        ],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode != 0
    assert json.loads(outcome.read_text(encoding="utf-8"))["playback_started"] is True
    assert replay.read_bytes() == pinned_replay.read_bytes()


def test_analyzer_paths_share_one_final_component_and_identity_policy(repository_root: Path) -> None:
    source = (repository_root / "Core/GameEngine/Source/Common/CommandLine.cpp").read_text(encoding="utf-8")

    assert 'strchr("<>:\\\"|?*", character)' in source
    assert '"CON", "PRN", "AUX", "NUL", "CLOCK$"' in source
    assert '"COM"' in source and '"LPT"' in source
    assert source.count("hasSafeWin32FinalComponent(TheGlobalData->m_simulateReplays[0].str())") == 2
    assert "hasSafeWin32FinalComponent(s_telemetryTracePath.str())" in source
    assert "hasSafeWin32FinalComponent(s_replayOutcomePath.str())" in source
    assert "GetFinalPathNameByHandleA" in source
    assert "_stricmp(leftIdentity.str(), rightIdentity.str()) == 0" in source


def test_outcome_attempt_brackets_all_replay_input_and_ready_seams(repository_root: Path) -> None:
    recorder = (repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp").read_text(encoding="utf-8")
    playback = recorder.split("Bool RecorderClass::playbackFile(AsciiString filename)", maxsplit=1)[1].split(
        "UnicodeString RecorderClass::readUnicodeString", maxsplit=1
    )[0]

    assert playback.index("clearGameData();") < playback.index("ReplayOutcome::beginAttempt();")
    assert playback.index("ReplayOutcome::beginAttempt();") < playback.index("readReplayHeader( header )")
    assert playback.index("m_mode = RECORDERMODETYPE_PLAYBACK;") < playback.index(
        "ReplayOutcome::observePlaybackStarted();"
    )
    assert "genrepBytesRead == sizeof(s_genrep) - 1" in recorder
    assert "REPLAY_OUTCOME_INPUT_UNAVAILABLE" in recorder
    assert "REPLAY_OUTCOME_INVALID_REPLAY_HEADER" in recorder
    assert "REPLAY_OUTCOME_TRUNCATED_INPUT" in recorder


def test_every_header_string_and_false_return_has_explicit_startup_status(repository_root: Path) -> None:
    header = (
        repository_root / "GeneralsMD/Code/GameEngine/Include/Common/Recorder.h"
    ).read_text(encoding="utf-8")
    recorder = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp"
    ).read_text(encoding="utf-8")
    outcome_header = (
        repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayOutcome.h"
    ).read_text(encoding="utf-8")
    read_header = recorder.split("Bool RecorderClass::readReplayHeader", maxsplit=1)[1].split(
        "Bool RecorderClass::simulateReplay", maxsplit=1
    )[0]
    playback = recorder.split("Bool RecorderClass::playbackFile", maxsplit=1)[1].split(
        "UnicodeString RecorderClass::readUnicodeString", maxsplit=1
    )[0]

    assert "enum ReplayStringReadStatus" in header
    assert read_header.count("readUnicodeString(stringStatus)") == 3
    assert read_header.count("readAsciiString(stringStatus)") == 2
    assert "return FALSE;" not in read_header
    assert playback.count("return FALSE;") == 2
    assert "ReplayOutcome::finishStartupFailure(REPLAY_OUTCOME_TRUNCATED_INPUT);" in playback
    assert "REPLAY_OUTCOME_STARTUP_FAILED" not in outcome_header


def test_fixed_width_header_scalars_are_committed_only_after_complete_reads(repository_root: Path) -> None:
    recorder = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp"
    ).read_text(encoding="utf-8")
    read_header = recorder.split("Bool RecorderClass::readReplayHeader", maxsplit=1)[1].split(
        "Bool RecorderClass::simulateReplay", maxsplit=1
    )[0]
    playback = recorder.split("Bool RecorderClass::playbackFile", maxsplit=1)[1].split(
        "UnicodeString RecorderClass::readUnicodeString", maxsplit=1
    )[0]
    fixed_validation = read_header.index("if (!fixedHeaderComplete)")
    system_time_validation = read_header.index("if (timeValueBytesRead != sizeof(timeValue))")
    version_validation = read_header.index("if (versionNumberBytesRead != sizeof(versionNumber)")

    assert "replay_time_t startTime = 0;" in read_header
    assert "replay_time_t endTime = 0;" in read_header
    assert "m_file->read(&header." not in read_header
    for assignment in (
        "header.startTime = startTime;",
        "header.endTime = endTime;",
        "header.frameCount = frameCount;",
        "header.desyncGame = desyncGame;",
        "header.quitEarly = quitEarly;",
        "header.playerDiscons[i] = playerDiscons[i];",
    ):
        assert read_header.index(assignment) > fixed_validation
    assert read_header.index("header.timeVal = timeValue;") > system_time_validation
    for assignment in (
        "header.versionNumber = versionNumber;",
        "header.exeCRC = exeCRC;",
        "header.iniCRC = iniCRC;",
    ):
        assert read_header.index(assignment) > version_validation
    assert "Int originalGameMode = GAME_NONE;" in playback
    assert "Int difficulty = 0;" in playback
    assert "Int rankPoints = 0;" in playback
    assert "Int maxFPS = 0;" in playback
    assert "m_file->read(&m_originalGameMode" not in playback
    assert playback.index("m_originalGameMode = originalGameMode;") > playback.index("if (setupComplete)")


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


@pytest.mark.parametrize("alias_kind", ["exact", "hardlink", "symlink"])
def test_existing_trace_alias_is_rejected_before_replay_without_mutating_input(
    alias_kind: str,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch a telemetry destination that can truncate the replay through the same path or file identity."""
    replay_copy = tmp_path / "disposable-input.rep"
    shutil.copyfile(pinned_replay, replay_copy)
    original_bytes = replay_copy.read_bytes()
    trace_path = replay_copy
    if alias_kind == "hardlink":
        trace_path = tmp_path / "input-hardlink.ndjson"
        os.link(replay_copy, trace_path)
    elif alias_kind == "symlink":
        trace_path = tmp_path / "input-symlink.ndjson"
        try:
            trace_path.symlink_to(replay_copy)
        except OSError as error:
            pytest.skip(f"symlink creation is unavailable on this Windows host: {error}")

    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, replay_copy), "-telemetry", str(trace_path), "-telemetry-run-id", RUN_ID],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode != 0
    assert "Simulating Replay" not in completed.stdout
    assert "must not already exist" in completed.stderr
    assert replay_copy.read_bytes() == original_bytes


def test_clean_eof_completion_is_published_after_the_terminal_logic_update(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch completion emitted before terminal-frame commands, systems, and the frame increment execute."""
    clean_replay = tmp_path / "crc-free.rep"
    terminal_command_frame = _write_crc_free_replay(pinned_replay, clean_replay)
    trace_path = (tmp_path / "clean.ndjson").resolve()

    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, clean_replay), "-telemetry", str(trace_path), "-telemetry-run-id", RUN_ID],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    records = tuple(iter_validated_trace(trace_path))
    complete = records[-1]
    assert isinstance(complete, CompleteRecord)
    assert complete.payload.clean_shutdown is True
    assert complete.payload.crc_mismatch is False
    assert complete.payload.final_frame == terminal_command_frame + 1


def test_partial_replay_input_has_explicit_truncated_termination(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Distinguish a partial command from clean EOF and an intact-file interruption."""
    complete_replay = tmp_path / "crc-free-complete.rep"
    _write_crc_free_replay(pinned_replay, complete_replay)
    source = complete_replay.read_bytes()
    assert len(source) > 1
    truncated_replay = tmp_path / "crc-free-partial.rep"
    truncated_replay.write_bytes(source[:-1])
    trace_path = (tmp_path / "truncated.ndjson").resolve()

    completed = _run_engine(
        [
            *_base_command(zero_hour_runtime_executable, truncated_replay),
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
        ],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert trace_path.is_file(), completed.stdout[-2000:] + completed.stderr[-2000:]
    records = tuple(iter_validated_trace(trace_path))
    outcome = records[-2]
    complete = records[-1]
    assert outcome.payload.terminal_reason == "replay_truncated"
    assert complete.payload.terminal_reason == "replay_truncated"
    assert complete.payload.replay_truncated is True
    assert complete.payload.clean_shutdown is False
    assert complete.payload.crc_mismatch is False


def test_late_writer_failure_never_publishes_an_apparently_successful_trace(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch a completion write/flush/close failure publishing a strict-valid trace with writer_error null."""
    trace_path = (tmp_path / "late-failure.ndjson").resolve()
    base_command = _base_command(zero_hour_runtime_executable, pinned_replay)
    baseline = _run_engine(base_command, zero_hour_runtime_executable.parent, repository_root)
    failed_sink = _run_engine(
        [*base_command, "-telemetry", str(trace_path), "-telemetry-run-id", RUN_ID],
        zero_hour_runtime_executable.parent,
        repository_root,
        {"GENERALS_REPLAY_TELEMETRY_TEST_FAIL_AFTER_COMPLETE_WRITE": "1"},
    )

    assert failed_sink.returncode == baseline.returncode
    assert failed_sink.stdout == baseline.stdout
    assert "injected_late_failure" in failed_sink.stderr
    assert not trace_path.exists()
    assert not tuple(tmp_path.glob("late-failure.ndjson.tmp.*"))


def test_destination_created_during_playback_is_not_replaced_at_publish(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch transactional publication that overwrites a destination created after startup validation."""
    clean_replay = tmp_path / "crc-free-race.rep"
    _write_crc_free_replay(pinned_replay, clean_replay)
    trace_path = (tmp_path / "publish-race.ndjson").resolve()
    collision_bytes = b"created-after-validation\n"
    command = [
        *_base_command(zero_hour_runtime_executable, clean_replay),
        "-telemetry",
        str(trace_path),
        "-telemetry-run-id",
        RUN_ID,
    ]
    process = subprocess.Popen(
        command,
        cwd=zero_hour_runtime_executable.parent,
        env=_runtime_environment(repository_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    temporary_paths: tuple[Path, ...] = ()
    deadline = time.monotonic() + 30
    while process.poll() is None and time.monotonic() < deadline:
        temporary_paths = tuple(tmp_path.glob("publish-race.ndjson.tmp.*"))
        if temporary_paths:
            break
        time.sleep(0.005)
    if not temporary_paths:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        pytest.fail("telemetry writer never exposed its exclusive transaction file")

    trace_path.write_bytes(collision_bytes)
    stdout, stderr = process.communicate(timeout=120)

    assert process.returncode == 0, stdout[-2000:] + stderr[-2000:]
    assert trace_path.read_bytes() == collision_bytes
    assert "publish_failed" in stderr
    assert not tuple(tmp_path.glob("publish-race.ndjson.tmp.*"))


def test_temp_candidate_exhaustion_never_removes_unowned_files(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch cleanup that deletes the last pre-existing candidate after every exclusive create returns EEXIST."""
    trace_path = (tmp_path / "exhausted.ndjson").resolve()
    base_command = _base_command(zero_hour_runtime_executable, pinned_replay)
    baseline = _run_engine(base_command, zero_hour_runtime_executable.parent, repository_root)
    process = subprocess.Popen(
        [*base_command, "-telemetry", str(trace_path), "-telemetry-run-id", RUN_ID],
        cwd=zero_hour_runtime_executable.parent,
        env=_runtime_environment(repository_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=0x00000004,
    )
    resumed = False
    candidates = tuple(Path(f"{trace_path}.tmp.{process.pid}.{index}") for index in range(1, 101))
    sentinels = {candidate: f"unowned-{index}\n".encode() for index, candidate in enumerate(candidates, start=1)}
    try:
        for candidate, sentinel in sentinels.items():
            candidate.write_bytes(sentinel)
        resume_status = _resume_suspended_process(process)
        assert resume_status == 0
        resumed = True
        stdout, stderr = process.communicate(timeout=120)
    finally:
        if process.poll() is None:
            if not resumed:
                _resume_suspended_process(process)
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode == baseline.returncode
    assert stdout == baseline.stdout
    assert "open_failed" in stderr
    assert not trace_path.exists()
    assert {candidate: candidate.read_bytes() for candidate in candidates} == sentinels
    assert set(tmp_path.iterdir()) == set(candidates)


def test_replay_failure_before_initialized_phase_discards_pending_telemetry(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch first-frame EOF publishing a v2 trace before authoritative catalog/player initialization."""
    zero_command_replay = tmp_path / "zero-command.rep"
    _write_zero_command_replay(pinned_replay, zero_command_replay)
    trace_path = (tmp_path / "zero-command.ndjson").resolve()
    completed = _run_engine(
        [
            *_base_command(zero_hour_runtime_executable, zero_command_replay),
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            RUN_ID,
        ],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode != 0
    assert "Cannot open replay" in completed.stdout
    assert not trace_path.exists()
    assert not tuple(tmp_path.glob("zero-command.ndjson.tmp.*"))
    assert not tuple(tmp_path.glob("game-data-catalog-v1-*.json"))


def test_replay_outcome_settles_every_preplayback_failure_attempt(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    missing = tmp_path / "missing.rep"
    invalid_magic = tmp_path / "invalid-magic.rep"
    invalid_magic.write_bytes(b"NOTREP" + (b"\0" * 64))
    truncated_header = tmp_path / "truncated-header.rep"
    truncated_header.write_bytes(b"GENREP")
    later_startup_abort = tmp_path / "later-startup-abort.rep"
    _write_zero_command_replay(pinned_replay, later_startup_abort)
    cases = (
        ("missing", missing, "input_unavailable"),
        ("invalid-magic", invalid_magic, "invalid_replay_header"),
        ("truncated-header", truncated_header, "truncated_input"),
        ("later-startup-abort", later_startup_abort, "truncated_input"),
    )

    for label, replay, terminal_reason in cases:
        baseline = _run_engine(
            _base_command(zero_hour_runtime_executable, replay),
            zero_hour_runtime_executable.parent,
            repository_root,
        )
        outcome = (tmp_path / f"{label}-outcome.json").resolve()
        completed = _run_engine(
            [*_base_command(zero_hour_runtime_executable, replay), "-replay-outcome", str(outcome)],
            zero_hour_runtime_executable.parent,
            repository_root,
        )

        assert completed.returncode == baseline.returncode != 0
        assert completed.stdout == baseline.stdout
        assert completed.stderr == baseline.stderr
        assert json.loads(outcome.read_text(encoding="utf-8")) == {
            "playback_started": False,
            "final_frame": 0,
            "command_count": 0,
            "terminal_reason": terminal_reason,
            "crc_mismatch": False,
            "crc_mismatch_frame": None,
        }
        assert not tuple(tmp_path.glob(f"{label}-outcome.json.tmp.*"))


@pytest.mark.parametrize("timestamp_bytes", range(8), ids=lambda count: f"timestamp-bytes-{count}")
def test_short_fixed_replay_timestamps_settle_as_truncated_without_partial_facts(
    timestamp_bytes: int,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
) -> None:
    replay_bytes = b"GENREP" + (b"\xA5" * timestamp_bytes)
    replay = tmp_path / f"short-timestamp-{timestamp_bytes}.rep"
    replay.write_bytes(replay_bytes)
    outcome = (tmp_path / f"short-timestamp-{timestamp_bytes}.json").resolve()
    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, replay), "-replay-outcome", str(outcome)],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode not in {0, 3221225477}
    assert "Cannot open replay" in completed.stdout
    assert completed.stderr == ""
    assert json.loads(outcome.read_text(encoding="utf-8")) == {
        "playback_started": False,
        "final_frame": 0,
        "command_count": 0,
        "terminal_reason": "truncated_input",
        "crc_mismatch": False,
        "crc_mismatch_frame": None,
    }
    assert not tuple(tmp_path.glob(f"{outcome.name}.tmp.*"))
    assert replay.read_bytes() == replay_bytes


@pytest.mark.parametrize(
    "local_index",
    ["-1", "8", "7", "0junk"],
    ids=["negative-one", "past-max-slots", "closed-slot", "invalid-syntax"],
)
def test_malformed_local_replay_slot_fails_cleanly_with_one_outcome(
    local_index: str,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    source = pinned_replay.read_bytes()
    span = _header_string_spans(source)["local_player_index"]
    replay = tmp_path / f"local-{local_index.replace('-', 'minus')}.rep"
    replay.write_bytes(_replace_header_field(source, span, local_index.encode("ascii") + b"\0"))
    outcome = (tmp_path / f"local-{local_index.replace('-', 'minus')}.json").resolve()
    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, replay), "-replay-outcome", str(outcome)],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode not in {0, 3221225477}
    assert "Cannot open replay" in completed.stdout
    assert completed.stderr == ""
    assert json.loads(outcome.read_text(encoding="utf-8")) == {
        "playback_started": False,
        "final_frame": 0,
        "command_count": 0,
        "terminal_reason": "invalid_replay_header",
        "crc_mismatch": False,
        "crc_mismatch_frame": None,
    }
    assert not tuple(tmp_path.glob(f"{outcome.name}.tmp.*"))
    assert replay.read_bytes() == _replace_header_field(source, span, local_index.encode("ascii") + b"\0")


@pytest.mark.parametrize("field", ["replay_name", "game_options"], ids=["utf16", "ascii"])
def test_replay_header_accepts_the_maximum_valid_nul_string_payload(
    field: str,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    source = pinned_replay.read_bytes()
    span = _header_string_spans(source)[field]
    replacement = (
        ("U" * 1023).encode("utf-16-le") + b"\0\0"
        if field == "replay_name"
        else _max_valid_game_options(source, span)
    )
    replay = tmp_path / f"max-valid-{field}.rep"
    replay.write_bytes(_replace_header_field(source, span, replacement))
    outcome = (tmp_path / f"max-valid-{field}.json").resolve()
    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, replay), "-replay-outcome", str(outcome)],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    payload = json.loads(outcome.read_text(encoding="utf-8"))
    assert completed.returncode != 3221225477
    assert payload["playback_started"] is True
    assert payload["final_frame"] > 0
    assert payload["command_count"] > 0
    assert replay.read_bytes() == _replace_header_field(source, span, replacement)


@pytest.mark.parametrize(
    ("field", "replacement", "preserve_suffix", "terminal_reason"),
    [
        (
            "replay_name",
            ("U" * 1024).encode("utf-16-le") + b"\0\0",
            True,
            "invalid_replay_header",
        ),
        ("game_options", b"A" * 1024 + b"\0", True, "invalid_replay_header"),
        (
            "replay_name",
            ("U" * 1024).encode("utf-16-le"),
            False,
            "invalid_replay_header",
        ),
        ("game_options", b"A" * 1024, False, "invalid_replay_header"),
        ("replay_name", b"", False, "truncated_input"),
        ("game_options", b"", False, "truncated_input"),
        ("replay_name", "few".encode("utf-16-le"), False, "truncated_input"),
        ("game_options", b"few", False, "truncated_input"),
    ],
    ids=[
        "utf16-max-plus-one",
        "ascii-max-plus-one",
        "utf16-overlong-unterminated",
        "ascii-overlong-unterminated",
        "utf16-immediate-eof",
        "ascii-immediate-eof",
        "utf16-short-eof",
        "ascii-short-eof",
    ],
)
def test_malformed_header_strings_fail_bounded_with_source_grounded_reason(
    field: str,
    replacement: bytes,
    preserve_suffix: bool,
    terminal_reason: str,
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    source = pinned_replay.read_bytes()
    span = _header_string_spans(source)[field]
    replay = tmp_path / f"malformed-{field}-{terminal_reason}-{len(replacement)}.rep"
    if preserve_suffix:
        malformed = _replace_header_field(source, span, replacement)
    else:
        malformed = source[: span[0]] + replacement
    replay.write_bytes(malformed)
    outcome = (tmp_path / f"malformed-{field}-{terminal_reason}-{len(replacement)}.json").resolve()
    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, replay), "-replay-outcome", str(outcome)],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode not in {0, 3221225477}
    assert "Cannot open replay" in completed.stdout
    assert completed.stderr == ""
    assert json.loads(outcome.read_text(encoding="utf-8")) == {
        "playback_started": False,
        "final_frame": 0,
        "command_count": 0,
        "terminal_reason": terminal_reason,
        "crc_mismatch": False,
        "crc_mismatch_frame": None,
    }
    assert not tuple(tmp_path.glob(f"{outcome.name}.tmp.*"))
    assert replay.read_bytes() == malformed


def test_preplayback_outcome_writer_failure_is_passive_and_cleans_owned_temp(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
) -> None:
    replay = tmp_path / "missing.rep"
    baseline = _run_engine(
        _base_command(zero_hour_runtime_executable, replay),
        zero_hour_runtime_executable.parent,
        repository_root,
    )
    outcome = (tmp_path / "missing-parent" / "outcome.json").resolve()
    completed = _run_engine(
        [*_base_command(zero_hour_runtime_executable, replay), "-replay-outcome", str(outcome)],
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode == baseline.returncode
    assert completed.stdout == baseline.stdout
    assert baseline.stderr in completed.stderr
    assert "ReplayOutcome:" in completed.stderr
    assert not outcome.exists()
    assert not tuple(tmp_path.rglob("outcome.json.tmp.*"))


def test_outcome_publish_collision_preserves_caller_destination_and_replay(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    replay = tmp_path / "collision-mechanics.rep"
    _write_crc_free_replay(pinned_replay, replay)
    replay_hash = replay.read_bytes()
    outcome = (tmp_path / "late-collision.json").resolve()
    caller_bytes = b"caller-owned-after-validation\n"
    process = subprocess.Popen(
        [*_base_command(zero_hour_runtime_executable, replay), "-replay-outcome", str(outcome)],
        cwd=zero_hour_runtime_executable.parent,
        env=_runtime_environment(repository_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    first_line = process.stdout.readline()
    assert first_line.startswith("Simulating Replay")
    outcome.write_bytes(caller_bytes)
    stdout_tail, stderr = process.communicate(timeout=120)

    assert process.returncode == 0, first_line + stdout_tail + stderr
    assert outcome.read_bytes() == caller_bytes
    assert "could not exclusively publish replay outcome" in stderr
    assert not tuple(tmp_path.glob("late-collision.json.tmp.*"))
    assert replay.read_bytes() == replay_hash


def test_telemetry_writer_avoids_msvc_only_bounded_formatting() -> None:
    """Catch reintroduction of formatting calls that cannot compile in the supported MinGW analyzer build."""
    source = Path(__file__).resolve().parents[4] / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    assert "sprintf_s(" not in source.read_text(encoding="utf-8")


def test_telemetry_translation_units_explicitly_exclude_vc6() -> None:
    """Catch analyzer-only APIs relying on an indirect build-definition exclusion from the legacy compiler."""
    root = Path(__file__).resolve().parents[4]
    for relative_path in (
        "GeneralsMD/Code/GameEngine/Include/Common/ReplayOutcome.h",
        "GeneralsMD/Code/GameEngine/Include/Common/ReplayTelemetry.h",
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayOutcome.cpp",
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp",
    ):
        source = (root / relative_path).read_text(encoding="utf-8")
        assert "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)" in source
