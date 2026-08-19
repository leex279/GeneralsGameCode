"""Source-grounded reader for Zero Hour's ``RecorderClass::readReplayHeader``."""

import struct
from typing import BinaryIO, cast

from .binary import BinaryReader
from .errors import (
    InvalidLocalPlayerIndexError,
    InvalidMagicError,
    InvalidStringEncodingError,
    InvalidStringLengthError,
    TruncatedReplayError,
)
from .game_options import parse_game_options
from .model import ReplayFlags, ReplayHeader

MAGIC = b"GENREP"
MAX_NUL_ASCII_BYTES = 1024
MAX_NUL_UTF16_CODE_UNITS = 1024
MAX_SLOTS = 8


# TheSuperHackers @feature Leex 19/08/2026 Parse the Recorder header exactly through the local-slot terminator.
def parse_replay_header(source: bytes | BinaryIO) -> ReplayHeader:
    """Parse a Zero Hour replay header and stop before its first command or post-header field."""
    reader = BinaryReader(source)
    magic_offset = reader.offset
    if reader.read_exact(len(MAGIC)) != MAGIC:
        raise InvalidMagicError("invalid_magic", magic_offset, "expected GENREP replay signature")

    start_time = reader.read_i32()
    end_time = reader.read_i32()
    frame_count = reader.read_u32()
    flags_offset = reader.offset
    flags = ReplayFlags(
        desync_game=_read_bool(reader),
        quit_early=_read_bool(reader),
        player_disconnects=tuple(_read_bool(reader) for _ in range(MAX_SLOTS)),
    )
    if reader.offset != flags_offset + 10:
        raise TruncatedReplayError("truncated_replay", flags_offset, "replay flags are incomplete")

    replay_name = _read_nul_utf16le(reader)
    system_time = cast(tuple[int, int, int, int, int, int, int, int], struct.unpack("<8H", reader.read_exact(16)))
    version_string = _read_nul_utf16le(reader)
    version_time_string = _read_nul_utf16le(reader)
    version_number = reader.read_u32()
    exe_crc = reader.read_u32()
    ini_crc = reader.read_u32()
    game_options_offset = reader.offset
    game_options = _read_nul_ascii(reader)
    parsed_options = parse_game_options(game_options, game_options_offset)
    local_player_index_offset = reader.offset
    local_player_index_text = _read_nul_ascii(reader)
    local_player_index = _parse_local_player_index(local_player_index_text, local_player_index_offset)

    return ReplayHeader(
        magic=MAGIC.decode("ascii"),
        start_time=start_time,
        end_time=end_time,
        frame_count=frame_count,
        flags=flags,
        replay_name=replay_name,
        system_time=system_time,
        version_string=version_string,
        version_time_string=version_time_string,
        version_number=version_number,
        exe_crc=exe_crc,
        ini_crc=ini_crc,
        game_options=game_options,
        local_player_index=local_player_index,
        header_end_offset=reader.offset,
        map=parsed_options.map,
        map_contents_mask=parsed_options.map_contents_mask,
        map_crc=parsed_options.map_crc,
        map_size=parsed_options.map_size,
        seed=parsed_options.seed,
        crc_interval=parsed_options.crc_interval,
        use_stats=parsed_options.use_stats,
        superweapon_restriction=parsed_options.superweapon_restriction,
        starting_cash=parsed_options.starting_cash,
        old_factions_only=parsed_options.old_factions_only,
        slots=parsed_options.slots,
        warnings=parsed_options.warnings,
    )


def _read_bool(reader: BinaryReader) -> bool:
    """Read the one-byte Bool representation used by the recorded Zero Hour header."""
    return reader.read_u8() != 0


def _read_nul_utf16le(reader: BinaryReader) -> str:
    """Read a bounded UTF-16LE string terminated by a zero code unit."""
    field_offset = reader.offset
    payload = bytearray()
    for _ in range(MAX_NUL_UTF16_CODE_UNITS):
        code_unit = reader.read_exact(2)
        if code_unit == b"\0\0":
            try:
                return bytes(payload).decode("utf-16-le")
            except UnicodeDecodeError as error:
                raise InvalidStringEncodingError(
                    "invalid_string_encoding", field_offset, f"invalid UTF-16LE replay string: {error.reason}"
                ) from error
        payload.extend(code_unit)
    raise InvalidStringLengthError(
        "invalid_string_length",
        field_offset,
        f"UTF-16LE replay string exceeds {MAX_NUL_UTF16_CODE_UNITS} code units without a terminator",
    )


def _read_nul_ascii(reader: BinaryReader) -> str:
    """Read a bounded ASCII string terminated by a zero byte."""
    field_offset = reader.offset
    payload = bytearray()
    for _ in range(MAX_NUL_ASCII_BYTES):
        byte = reader.read_exact(1)
        if byte == b"\0":
            try:
                return bytes(payload).decode("ascii")
            except UnicodeDecodeError as error:
                raise InvalidStringEncodingError(
                    "invalid_string_encoding", field_offset, f"invalid ASCII replay string: {error.reason}"
                ) from error
        payload.extend(byte)
    raise InvalidStringLengthError(
        "invalid_string_length",
        field_offset,
        f"ASCII replay string exceeds {MAX_NUL_ASCII_BYTES} bytes without a terminator",
    )


def _parse_local_player_index(value: str, offset: int) -> int:
    """Validate the local GameInfo slot index against the engine's eight slots."""
    try:
        index = int(value, 10)
    except ValueError as error:
        raise InvalidLocalPlayerIndexError("invalid_local_player_index", offset, f"invalid local slot '{value}'") from error
    if index < -1 or index >= MAX_SLOTS:
        raise InvalidLocalPlayerIndexError("invalid_local_player_index", offset, f"local slot {index} is outside [-1, 7]")
    return index
