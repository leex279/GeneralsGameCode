"""Behavioral tests for complete and truncated replay command streams."""

import struct
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from fixture_builder import command_bytes, replay_header_bytes

from generals_replay_analyzer.commands import GameMessageArgumentDataType, ReplayCommand
from generals_replay_analyzer.errors import TruncatedReplayError
from generals_replay_analyzer.model import ReplaySetup
from generals_replay_analyzer.parser import ParsedReplay, parse_replay

PINNED_REPLAY_PATH = Path(__file__).parent / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"


def test_parse_replay_decodes_recorder_setup_before_the_first_command() -> None:
    """Reject treating RecorderClass's four post-header Int fields as a synthetic command."""
    parsed = parse_replay(PINNED_REPLAY_PATH)

    assert parsed.completion_status == "complete"
    assert parsed.setup == ReplaySetup(1, 5, 0, 0, 326, 342)
    assert parsed.commands[0].frame == 31
    assert parsed.commands[0].message_type == 1003
    assert parsed.command_stream_offset == 342
    assert parsed.commands[0].start_offset == parsed.command_stream_offset


@pytest.mark.parametrize("setup_bytes", [b"", b"\0" * 4, b"\0" * 12])
def test_parse_replay_rejects_truncated_recorder_setup_before_command_stream(
    tmp_path: Path, setup_bytes: bytes
) -> None:
    """Reject accepting a partial Recorder setup as though it were a complete empty command stream."""
    path = tmp_path / "truncated-setup.rep"
    path.write_bytes(replay_header_bytes() + setup_bytes)

    with pytest.raises(TruncatedReplayError) as raised:
        parse_replay(path)

    assert raised.value.code == "truncated_replay"
    assert raised.value.offset == len(replay_header_bytes()) + len(setup_bytes)


def _write_replay(tmp_path: Path, suffix: bytes) -> Path:
    path = tmp_path / "stream.rep"
    path.write_bytes(replay_header_bytes() + struct.pack("<iiii", 1, 5, 0, 0) + suffix)
    return path


def test_parse_replay_returns_complete_commands_with_header_relative_offsets(tmp_path: Path) -> None:
    """Reject parsing command offsets relative to a payload slice instead of replay bytes."""
    first = command_bytes(30, 77, 1, [(0, 1)], [struct.pack("<i", 5)])
    second = command_bytes(60, 78, 2, [], [])
    command_stream_offset = len(replay_header_bytes()) + 16

    parsed = parse_replay(_write_replay(tmp_path, first + second))

    assert isinstance(parsed, ParsedReplay)
    assert parsed.completion_status == "complete"
    assert parsed.end_offset == command_stream_offset + len(first) + len(second)
    assert [(command.frame, command.start_offset, command.end_offset) for command in parsed.commands] == [
        (30, command_stream_offset, command_stream_offset + len(first)),
        (60, command_stream_offset + len(first), parsed.end_offset),
    ]
    assert parsed.commands[0].arguments[0].argument_type is GameMessageArgumentDataType.INTEGER
    assert parsed.warnings == ()


@pytest.mark.parametrize(
    ("suffix", "expected_message"),
    [
        (b"\x01", "next frame"),
        (b"\x01\x02", "next frame"),
        (b"\x01\x02\x03", "next frame"),
        (struct.pack("<I", 30) + b"\x4d\0", "truncated command at offset"),
        (struct.pack("<Ii", 30, 77) + b"\x01", "truncated command at offset"),
        (command_bytes(30, 77, 1, [], [])[:-1], "truncated command at offset"),
        (command_bytes(30, 77, 1, [(0, 1)], [struct.pack("<i", 5)])[:-2], "truncated command at offset"),
    ],
)
def test_parse_replay_marks_incomplete_command_streams_truncated_without_resynchronizing(
    tmp_path: Path, suffix: bytes, expected_message: str
) -> None:
    """Reject accepting partial command data or scanning later bytes for a plausible frame."""
    command_stream_offset = len(replay_header_bytes()) + 16

    parsed = parse_replay(_write_replay(tmp_path, suffix))

    assert parsed.completion_status == "truncated"
    assert parsed.commands == ()
    assert parsed.end_offset == command_stream_offset
    assert len(parsed.warnings) == 1
    assert expected_message in parsed.warnings[0].message


def test_parse_replay_reports_a_neutral_command_warning_for_a_later_partial_type_run(tmp_path: Path) -> None:
    """Reject calling a second metadata run a payload after the first run has already been read."""
    partial_second_run = struct.pack("<IiiB", 30, 77, 1, 2) + b"\0\x01\x01"

    parsed = parse_replay(_write_replay(tmp_path, partial_second_run))

    assert parsed.completion_status == "truncated"
    assert parsed.warnings[0].message == f"truncated command at offset {len(replay_header_bytes()) + 32}"


def test_parse_replay_keeps_prior_command_when_later_command_is_truncated(tmp_path: Path) -> None:
    """Reject losing already trustworthy commands when the subsequent command ends early."""
    complete = command_bytes(30, 777, 1, [], [])
    partial = command_bytes(60, 78, 2, [(0, 1)], [struct.pack("<i", 5)])[:-1]
    command_stream_offset = len(replay_header_bytes()) + 16

    parsed = parse_replay(_write_replay(tmp_path, complete + partial))

    assert parsed.completion_status == "truncated"
    assert parsed.commands == (
        ReplayCommand(30, 1, 777, None, (), command_stream_offset, command_stream_offset + len(complete)),
    )
    assert parsed.end_offset == command_stream_offset + len(complete)


def test_parsed_replay_is_frozen(tmp_path: Path) -> None:
    """Reject mutable parse results whose completion state can diverge from their stream evidence."""
    parsed = parse_replay(_write_replay(tmp_path, b""))

    with pytest.raises(FrozenInstanceError):
        parsed.end_offset = 0  # type: ignore[misc]
