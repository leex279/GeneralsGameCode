"""Behavioral tests for source-grounded command argument decoding."""

import struct
from dataclasses import FrozenInstanceError

import pytest
from fixture_builder import command_bytes

from generals_replay_analyzer.binary import Coord3D, ICoord2D, IRegion2D
from generals_replay_analyzer.commands import (
    GameMessageArgumentDataType,
    ReplayArgument,
    ReplayCommand,
    parse_command,
)
from generals_replay_analyzer.errors import UnsupportedArgumentTypeError


@pytest.mark.parametrize(
    ("argument_type", "payload", "expected_value"),
    [
        (0, struct.pack("<i", -12), -12),
        (1, struct.pack("<f", 1.5), 1.5),
        (2, b"\x01", True),
        (3, struct.pack("<i", 123), 123),
        (4, struct.pack("<i", -124), -124),
        (5, struct.pack("<I", 0xFEEDBEEF), 0xFEEDBEEF),
        (6, struct.pack("<fff", 1.0, 2.0, 3.0), Coord3D(1.0, 2.0, 3.0)),
        (7, struct.pack("<ii", -5, 8), ICoord2D(-5, 8)),
        (8, struct.pack("<iiii", 1, 2, 3, 4), IRegion2D(ICoord2D(1, 2), ICoord2D(3, 4))),
        (9, struct.pack("<I", 123456), 123456),
        (10, "Z".encode("utf-16-le"), "Z"),
        (10, b"\x00\xd8", "\ud800"),
    ],
)
def test_command_decodes_every_engine_argument_type(
    argument_type: int, payload: bytes, expected_value: object
) -> None:
    """Reject payload-width or type-order drift from RecorderClass::readArgument."""
    encoded = command_bytes(90, 1000, 2, [(argument_type, 1)], [payload])

    command = parse_command(encoded)

    assert command.frame == 90
    assert command.seconds == 3.0
    assert command.player_index == 2
    assert command.message_type == 1000
    assert command.arguments == (
        ReplayArgument(GameMessageArgumentDataType(argument_type), expected_value, payload),
    )
    assert command.start_offset == 0
    assert command.end_offset == len(encoded)


def test_command_accepts_zero_type_runs_without_inventing_arguments() -> None:
    """Reject treating a valid zero-run command as malformed or as an unknown type."""
    command = parse_command(command_bytes(0, 777, -1, [], []))

    assert command.arguments == ()
    assert command.message_name is None


def test_command_preserves_argument_order_across_repeated_type_runs() -> None:
    """Reject merging separated same-type runs and moving their payloads."""
    encoded = command_bytes(
        30,
        77,
        1,
        [(0, 1), (1, 1), (0, 1)],
        [struct.pack("<i", 10), struct.pack("<f", 2.5), struct.pack("<i", 20)],
    )

    command = parse_command(encoded)

    assert [(argument.argument_type, argument.value) for argument in command.arguments] == [
        (GameMessageArgumentDataType.INTEGER, 10),
        (GameMessageArgumentDataType.REAL, 2.5),
        (GameMessageArgumentDataType.INTEGER, 20),
    ]


def test_command_preserves_unknown_message_number_when_argument_widths_are_known() -> None:
    """Reject dropping a structurally valid command merely because its message enum is new."""
    command = parse_command(command_bytes(1, 987654, 3, [(9, 1)], [struct.pack("<I", 7)]))

    assert command.message_type == 987654
    assert command.message_name is None
    assert command.arguments[0].value == 7


def test_command_rejects_unknown_argument_type_before_guessing_its_payload_width() -> None:
    """Reject interpreting future argument bytes as a known width and losing stream alignment."""
    encoded = command_bytes(1, 77, 3, [(11, 1)], [b"\0\0\0\0"])

    with pytest.raises(UnsupportedArgumentTypeError) as raised:
        parse_command(encoded)

    assert raised.value.code == "unsupported_argument_type"
    assert raised.value.offset == 13


def test_command_models_are_frozen_records() -> None:
    """Reject mutable command records whose offsets can detach from their raw evidence."""
    command = parse_command(command_bytes(1, 77, 3, [], []))

    assert isinstance(command, ReplayCommand)
    with pytest.raises(FrozenInstanceError):
        command.frame = 2  # type: ignore[misc]
