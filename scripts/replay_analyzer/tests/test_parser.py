"""Behavioral tests for complete and truncated replay command streams."""

import struct
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from fixture_builder import command_bytes, replay_header_bytes

from generals_replay_analyzer.commands import GameMessageArgumentDataType, ReplayCommand
from generals_replay_analyzer.parser import ParsedReplay, parse_replay


def _write_replay(tmp_path: Path, suffix: bytes) -> Path:
    path = tmp_path / "stream.rep"
    path.write_bytes(replay_header_bytes() + suffix)
    return path


def test_parse_replay_returns_complete_commands_with_header_relative_offsets(tmp_path: Path) -> None:
    """Reject parsing command offsets relative to a payload slice instead of replay bytes."""
    first = command_bytes(30, 77, 1, [(0, 1)], [struct.pack("<i", 5)])
    second = command_bytes(60, 78, 2, [], [])
    header_size = len(replay_header_bytes())

    parsed = parse_replay(_write_replay(tmp_path, first + second))

    assert isinstance(parsed, ParsedReplay)
    assert parsed.completion_status == "complete"
    assert parsed.end_offset == header_size + len(first) + len(second)
    assert [(command.frame, command.start_offset, command.end_offset) for command in parsed.commands] == [
        (30, header_size, header_size + len(first)),
        (60, header_size + len(first), parsed.end_offset),
    ]
    assert parsed.commands[0].arguments[0].argument_type is GameMessageArgumentDataType.INTEGER
    assert parsed.warnings == ()


@pytest.mark.parametrize(
    ("suffix", "expected_message"),
    [
        (b"\x01", "next frame"),
        (b"\x01\x02", "next frame"),
        (b"\x01\x02\x03", "next frame"),
        (command_bytes(30, 77, 1, [], [])[:-1], "type-run count"),
        (command_bytes(30, 77, 1, [(0, 1)], [struct.pack("<i", 5)])[:-2], "payload"),
    ],
)
def test_parse_replay_marks_incomplete_command_streams_truncated_without_resynchronizing(
    tmp_path: Path, suffix: bytes, expected_message: str
) -> None:
    """Reject accepting partial command data or scanning later bytes for a plausible frame."""
    header_size = len(replay_header_bytes())

    parsed = parse_replay(_write_replay(tmp_path, suffix))

    assert parsed.completion_status == "truncated"
    assert parsed.commands == ()
    assert parsed.end_offset == header_size
    assert len(parsed.warnings) == 1
    assert expected_message in parsed.warnings[0].message


def test_parse_replay_keeps_prior_command_when_later_command_is_truncated(tmp_path: Path) -> None:
    """Reject losing already trustworthy commands when the subsequent command ends early."""
    complete = command_bytes(30, 77, 1, [], [])
    partial = command_bytes(60, 78, 2, [(0, 1)], [struct.pack("<i", 5)])[:-1]
    header_size = len(replay_header_bytes())

    parsed = parse_replay(_write_replay(tmp_path, complete + partial))

    assert parsed.completion_status == "truncated"
    assert parsed.commands == (ReplayCommand(30, 1, 77, None, (), header_size, header_size + len(complete)),)
    assert parsed.end_offset == header_size + len(complete)


def test_parsed_replay_is_frozen(tmp_path: Path) -> None:
    """Reject mutable parse results whose completion state can diverge from their stream evidence."""
    parsed = parse_replay(_write_replay(tmp_path, b""))

    with pytest.raises(FrozenInstanceError):
        parsed.end_offset = 0  # type: ignore[misc]
