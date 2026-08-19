"""Source-grounded Zero Hour Recorder command decoding."""

from dataclasses import dataclass
from enum import IntEnum
from typing import TypeAlias

from .binary import BinaryReader, Coord3D, ICoord2D, IRegion2D
from .errors import UnsupportedArgumentTypeError


class GameMessageArgumentDataType(IntEnum):
    """Values serialized by ``GameMessageArgumentDataType`` in declaration order."""

    INTEGER = 0
    REAL = 1
    BOOLEAN = 2
    OBJECT_ID = 3
    DRAWABLE_ID = 4
    TEAM_ID = 5
    LOCATION = 6
    PIXEL = 7
    PIXEL_REGION = 8
    TIMESTAMP = 9
    WIDE_CHAR = 10
    UNKNOWN = 11


ReplayArgumentValue: TypeAlias = int | float | bool | str | Coord3D | ICoord2D | IRegion2D


@dataclass(frozen=True)
class ReplayArgument:
    """One typed command payload with the exact bytes recorded by the engine."""

    argument_type: GameMessageArgumentDataType
    value: ReplayArgumentValue
    raw_bytes: bytes

    @property
    def type(self) -> GameMessageArgumentDataType:
        """Return the serialized engine type under the concise consumer-facing name."""
        return self.argument_type


@dataclass(frozen=True)
class ReplayCommand:
    """One fully decoded Recorder command with replay-relative evidence boundaries."""

    frame: int
    player_index: int
    message_type: int
    message_name: str | None
    arguments: tuple[ReplayArgument, ...]
    start_offset: int
    end_offset: int

    @property
    def seconds(self) -> float:
        """Expose replay time only as the fixed 30 Hz engine-frame conversion."""
        return self.frame / 30.0


_MESSAGE_NAMES = {
    0: "MSG_INVALID",
    1: "MSG_FRAME_TICK",
    1000: "MSG_BEGIN_NETWORK_MESSAGES",
    1999: "MSG_END_NETWORK_MESSAGES",
}


# TheSuperHackers @feature Leex 19/08/2026 Decode Recorder command runs without inventing stream boundaries.
def parse_command(source: bytes | BinaryReader) -> ReplayCommand:
    """Decode one complete command from ``source`` and leave a supplied reader at its next frame."""
    reader = BinaryReader(source) if isinstance(source, bytes) else source
    command_start = reader.offset
    frame = reader.read_u32()
    message_type = reader.read_i32()
    player_index = reader.read_i32()
    type_run_count = reader.read_u8()

    type_runs: list[tuple[GameMessageArgumentDataType, int]] = []
    for _ in range(type_run_count):
        type_offset = reader.offset
        raw_type = reader.read_u8()
        count = reader.read_u8()
        try:
            argument_type = GameMessageArgumentDataType(raw_type)
        except ValueError as error:
            raise UnsupportedArgumentTypeError(
                "unsupported_argument_type",
                type_offset,
                f"unsupported GameMessageArgumentDataType {raw_type}",
            ) from error
        if argument_type is GameMessageArgumentDataType.UNKNOWN:
            raise UnsupportedArgumentTypeError(
                "unsupported_argument_type",
                type_offset,
                "ARGUMENTDATATYPE_UNKNOWN cannot be decoded without a payload width",
            )
        type_runs.append((argument_type, count))

    arguments: list[ReplayArgument] = []
    for argument_type, count in type_runs:
        for _ in range(count):
            arguments.append(_read_argument(reader, argument_type))

    return ReplayCommand(
        frame=frame,
        player_index=player_index,
        message_type=message_type,
        message_name=_MESSAGE_NAMES.get(message_type),
        arguments=tuple(arguments),
        start_offset=command_start,
        end_offset=reader.offset,
    )


def _read_argument(reader: BinaryReader, argument_type: GameMessageArgumentDataType) -> ReplayArgument:
    """Read one payload through the engine width associated with an already validated type."""
    payload_start = reader.offset
    value: ReplayArgumentValue
    if argument_type is GameMessageArgumentDataType.INTEGER:
        value = reader.read_i32()
    elif argument_type is GameMessageArgumentDataType.REAL:
        value = reader.read_f32()
    elif argument_type is GameMessageArgumentDataType.BOOLEAN:
        value = reader.read_u8() != 0
    elif argument_type is GameMessageArgumentDataType.OBJECT_ID or argument_type is GameMessageArgumentDataType.DRAWABLE_ID:
        value = reader.read_i32()
    elif argument_type is GameMessageArgumentDataType.TEAM_ID:
        value = reader.read_u32()
    elif argument_type is GameMessageArgumentDataType.LOCATION:
        value = reader.read_coord3d()
    elif argument_type is GameMessageArgumentDataType.PIXEL:
        value = reader.read_icoord2d()
    elif argument_type is GameMessageArgumentDataType.PIXEL_REGION:
        value = reader.read_iregion2d()
    elif argument_type is GameMessageArgumentDataType.TIMESTAMP:
        value = reader.read_u32()
    elif argument_type is GameMessageArgumentDataType.WIDE_CHAR:
        value = reader.read_exact(2).decode("utf-16-le")
    else:
        raise UnsupportedArgumentTypeError(
            "unsupported_argument_type",
            payload_start,
            f"unsupported GameMessageArgumentDataType {argument_type.value}",
        )
    return ReplayArgument(argument_type, value, reader.slice(payload_start, reader.offset))
