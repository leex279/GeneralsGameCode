"""Strict comparison of Python replay parses with modern-engine NDJSON evidence."""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .binary import Coord3D, ICoord2D, IRegion2D
from .commands import GameMessageArgumentDataType, ReplayArgument
from .parser import ParsedReplay

_HEADER_KEYS = frozenset(
    {
        "record",
        "filename",
        "for_playback",
        "start_time",
        "end_time",
        "frame_count",
        "desync_game",
        "quit_early",
        "player_disconnects",
        "replay_name",
        "system_time",
        "version_string",
        "version_time_string",
        "version_number",
        "exe_crc",
        "ini_crc",
        "game_options",
        "local_player_index",
        "header_end_offset",
    }
)
_SYSTEM_TIME_KEYS = frozenset(
    {"year", "month", "day_of_week", "day", "hour", "minute", "second", "milliseconds"}
)
_SETUP_KEYS = frozenset(
    {"record", "difficulty", "original_game_mode", "rank_points", "max_fps", "start_offset", "end_offset"}
)
_COMMAND_KEYS = frozenset(
    {
        "record",
        "frame",
        "start_offset",
        "end_offset",
        "message_type",
        "message_name",
        "player_index",
        "arguments",
    }
)
_ARGUMENT_KEYS = frozenset({"type", "type_name", "value", "raw_scalar_bits"})
_COMPLETE_KEYS = frozenset({"record", "end_offset", "complete"})
_CATALOG_KEYS = frozenset({"record", "messages"})
_CATALOG_ENTRY_KEYS = frozenset({"message_type", "message_name"})
_MESSAGE_NAME = re.compile(r"MSG_[A-Z0-9_]+$")
_HEX_BITS = re.compile(r"0x[0-9A-F]+$")
_ARGUMENT_TYPE_NAMES = frozenset(argument_type.name for argument_type in GameMessageArgumentDataType if argument_type.value < 11)


class CppDumpValidationError(ValueError):
    """Raised when engine NDJSON is not complete, ordered, and schema-valid."""


@dataclass(frozen=True)
class CppReplayDump:
    """One authoritative engine dump split into its ordered record kinds."""

    header: dict[str, object]
    catalog: dict[str, object]
    setup: dict[str, object]
    commands: tuple[dict[str, object], ...]
    complete: dict[str, object]


@dataclass(frozen=True)
class ParityMismatch:
    """The first replay-relative field where Python and C++ disagree."""

    offset: int
    field_path: str
    python_value: object
    cpp_value: object

    def __str__(self) -> str:
        """Render all mismatch evidence in one deterministic diagnostic."""
        return (
            f"replay parity mismatch at byte {self.offset}: {self.field_path}: "
            f"Python={self.python_value!r}, C++={self.cpp_value!r}"
        )


# TheSuperHackers @feature Leex 19/08/2026 Load engine replay evidence for byte-exact parser parity checks. (#TBD)
def load_cpp_dump(path: Path) -> CppReplayDump:
    """Load a complete, ordered C++ NDJSON dump after strict shape validation."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise CppDumpValidationError("C++ replay dump is empty")
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise CppDumpValidationError(f"blank NDJSON record on line {line_number}")
        try:
            decoded = cast(object, json.loads(line, parse_constant=_reject_json_constant))
        except (json.JSONDecodeError, ValueError) as error:
            raise CppDumpValidationError(f"invalid JSON on line {line_number}: {error}") from error
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise CppDumpValidationError(f"NDJSON record on line {line_number} must be a JSON object")
        records.append(cast(dict[str, object], decoded))

    if records[-1].get("record") != "complete":
        raise CppDumpValidationError("terminal record must be complete")
    if records[0].get("record") != "header":
        raise CppDumpValidationError("record 1 must be header")
    if len(records) < 2 or records[1].get("record") != "message_catalog":
        raise CppDumpValidationError("record 2 must be message_catalog")
    if len(records) < 3 or records[2].get("record") != "setup":
        raise CppDumpValidationError("record 3 must be setup")
    for record_number, record in enumerate(records[3:-1], start=4):
        if record.get("record") != "command":
            raise CppDumpValidationError(f"record {record_number} must be command")

    _validate_header(records[0])
    catalog_names = _validate_catalog(records[1])
    _validate_setup(records[2])
    for record_number, record in enumerate(records[3:-1], start=4):
        _validate_command(record, record_number, catalog_names)
    _validate_complete(records[-1])
    return CppReplayDump(
        header=records[0],
        catalog=records[1],
        setup=records[2],
        commands=tuple(records[3:-1]),
        complete=records[-1],
    )


def _reject_json_constant(value: str) -> None:
    """Reject JavaScript NaN/Infinity tokens that are not valid JSON."""
    raise ValueError(f"invalid JSON constant {value}")


def _validate_header(record: dict[str, object]) -> None:
    """Validate the exact observer header schema and primitive types."""
    _require_exact_keys(record, _HEADER_KEYS, "header")
    for key in ("filename", "replay_name", "version_string", "version_time_string", "game_options"):
        _require_string(record[key], f"header.{key}")
    for key in ("for_playback", "desync_game", "quit_early"):
        _require_bool(record[key], f"header.{key}")
    for key in (
        "start_time",
        "end_time",
        "frame_count",
        "version_number",
        "exe_crc",
        "ini_crc",
        "local_player_index",
        "header_end_offset",
    ):
        _require_int(record[key], f"header.{key}")
    disconnects = record["player_disconnects"]
    if not isinstance(disconnects, list) or len(disconnects) != 8 or any(type(value) is not bool for value in disconnects):
        raise CppDumpValidationError("header.player_disconnects must contain exactly eight booleans")
    system_time = record["system_time"]
    if not isinstance(system_time, dict) or set(system_time) != _SYSTEM_TIME_KEYS:
        raise CppDumpValidationError("header.system_time must contain exactly the eight SYSTEMTIME fields")
    for key, value in system_time.items():
        _require_int(value, f"header.system_time.{key}")


def _validate_catalog(record: dict[str, object]) -> dict[int, str]:
    """Validate one-to-one compiled message id/name mappings and retain them for commands."""
    _require_exact_keys(record, _CATALOG_KEYS, "message_catalog")
    messages = record["messages"]
    if not isinstance(messages, list) or not messages:
        raise CppDumpValidationError("message_catalog.messages must be a non-empty list")
    names_by_id: dict[int, str] = {}
    ids_by_name: dict[str, int] = {}
    previous_id: int | None = None
    for index, entry in enumerate(messages):
        if not isinstance(entry, dict):
            raise CppDumpValidationError(f"message_catalog.messages[{index}] must be an object")
        entry = cast(dict[str, object], entry)
        _require_exact_keys(entry, _CATALOG_ENTRY_KEYS, f"message_catalog.messages[{index}]")
        message_id = entry["message_type"]
        message_name = entry["message_name"]
        _require_int(message_id, f"message_catalog.messages[{index}].message_type")
        _require_string(message_name, f"message_catalog.messages[{index}].message_name")
        message_id = cast(int, message_id)
        message_name = cast(str, message_name)
        if _MESSAGE_NAME.fullmatch(message_name) is None:
            raise CppDumpValidationError(f"message_catalog.messages[{index}].message_name must be MSG_*")
        if message_id in names_by_id:
            raise CppDumpValidationError(f"duplicate message_catalog id {message_id}")
        if message_name in ids_by_name:
            raise CppDumpValidationError(f"duplicate message_catalog name {message_name}")
        if previous_id is not None and message_id <= previous_id:
            raise CppDumpValidationError("message_catalog ids must be strictly increasing")
        names_by_id[message_id] = message_name
        ids_by_name[message_name] = message_id
        previous_id = message_id
    return names_by_id


# TheSuperHackers @feature Leex 19/08/2026 Source Python message metadata only from the compiled engine catalog. (#TBD)
def cpp_message_catalog_entries(cpp_dump: CppReplayDump) -> tuple[tuple[int, str], ...]:
    """Return the validated C++ catalog without re-reading or scraping engine headers."""
    messages = cast(list[dict[str, object]], cpp_dump.catalog["messages"])
    return tuple(
        (cast(int, entry["message_type"]), cast(str, entry["message_name"]))
        for entry in messages
    )


# TheSuperHackers @feature Leex 19/08/2026 Generate the Python message contract directly from engine NDJSON. (#TBD)
def write_generated_message_catalog(
    cpp_dump: CppReplayDump,
    destination: Path,
    *,
    engine_build: str,
    generated_at_utc: str,
) -> None:
    """Write the validated compiled message catalog in the analyzer's packaged contract schema."""
    document: dict[str, object] = {
        "schema_version": 1,
        "game": "Command & Conquer: Generals Zero Hour",
        "patch": "1.04",
        "engine_build": engine_build,
        "source_header_path": "Core/GameEngine/Include/Common/MessageStream.h",
        "generated_at_utc": generated_at_utc,
        "generated": True,
        "generation_note": "Generated from ReplayParseDump message_catalog; do not hand-edit.",
        "message_types": [
            {"id": message_id, "name": message_name}
            for message_id, message_name in cpp_message_catalog_entries(cpp_dump)
        ],
    }
    destination.write_text(json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _validate_setup(record: dict[str, object]) -> None:
    """Validate the four setup values and both source boundaries."""
    _require_exact_keys(record, _SETUP_KEYS, "setup")
    for key in _SETUP_KEYS - {"record"}:
        _require_int(record[key], f"setup.{key}")


def _validate_command(record: dict[str, object], record_number: int, catalog: dict[int, str]) -> None:
    """Validate one command record and every argument's type-specific JSON shape."""
    _require_exact_keys(record, _COMMAND_KEYS, "command")
    for key in ("frame", "start_offset", "end_offset", "message_type", "player_index"):
        _require_int(record[key], f"command.{key}")
    _require_string(record["message_name"], "command.message_name")
    message_type = cast(int, record["message_type"])
    message_name = cast(str, record["message_name"])
    if catalog.get(message_type) != message_name:
        raise CppDumpValidationError(
            f"command record {record_number} message mapping {message_type}:{message_name} is absent from message_catalog"
        )
    arguments = record["arguments"]
    if not isinstance(arguments, list):
        raise CppDumpValidationError("command.arguments must be a list")
    for argument_index, argument in enumerate(arguments):
        if not isinstance(argument, dict):
            raise CppDumpValidationError(f"command.arguments[{argument_index}] must be an object")
        _validate_argument(cast(dict[str, object], argument), argument_index)


def _validate_argument(argument: dict[str, object], index: int) -> None:
    """Validate one C++ argument using the serialized type as its schema discriminator."""
    path = f"command.arguments[{index}]"
    _require_exact_keys(argument, _ARGUMENT_KEYS, path)
    _require_int(argument["type"], f"{path}.type")
    raw_type = cast(int, argument["type"])
    try:
        argument_type = GameMessageArgumentDataType(raw_type)
    except ValueError as error:
        raise CppDumpValidationError(f"{path}.type is unsupported: {raw_type}") from error
    if argument_type is GameMessageArgumentDataType.UNKNOWN:
        raise CppDumpValidationError(f"{path}.type UNKNOWN cannot be authoritative")
    _require_string(argument["type_name"], f"{path}.type_name")
    if argument["type_name"] not in _ARGUMENT_TYPE_NAMES:
        raise CppDumpValidationError(f"{path}.type_name is not a recognized observer label")

    value = argument["value"]
    raw_bits = argument["raw_scalar_bits"]
    if argument_type in {
        GameMessageArgumentDataType.INTEGER,
        GameMessageArgumentDataType.OBJECT_ID,
        GameMessageArgumentDataType.DRAWABLE_ID,
        GameMessageArgumentDataType.TEAM_ID,
        GameMessageArgumentDataType.TIMESTAMP,
    }:
        _require_int(value, f"{path}.value")
        _require_hex_bits(raw_bits, 8, f"{path}.raw_scalar_bits")
    elif argument_type is GameMessageArgumentDataType.REAL:
        _require_real(value, f"{path}.value")
        _require_hex_bits(raw_bits, 8, f"{path}.raw_scalar_bits")
    elif argument_type is GameMessageArgumentDataType.BOOLEAN:
        _require_bool(value, f"{path}.value")
        _require_hex_bits(raw_bits, 2, f"{path}.raw_scalar_bits")
    elif argument_type is GameMessageArgumentDataType.LOCATION:
        _require_coord(value, f"{path}.value", real=True)
        _require_hex_list(raw_bits, 3, f"{path}.raw_scalar_bits")
    elif argument_type is GameMessageArgumentDataType.PIXEL:
        _require_coord(value, f"{path}.value", real=False)
        _require_hex_list(raw_bits, 2, f"{path}.raw_scalar_bits")
    elif argument_type is GameMessageArgumentDataType.PIXEL_REGION:
        if not isinstance(value, dict) or set(value) != {"lo", "hi"}:
            raise CppDumpValidationError(f"{path}.value must contain lo and hi pixel coordinates")
        _require_coord(value["lo"], f"{path}.value.lo", real=False)
        _require_coord(value["hi"], f"{path}.value.hi", real=False)
        _require_hex_list(raw_bits, 4, f"{path}.raw_scalar_bits")
    elif argument_type is GameMessageArgumentDataType.WIDE_CHAR:
        if not isinstance(value, str) or len(value) != 1:
            raise CppDumpValidationError(f"{path}.value must be one UTF-16 code unit")
        _require_hex_bits(raw_bits, 4, f"{path}.raw_scalar_bits")


def _validate_complete(record: dict[str, object]) -> None:
    """Require a terminal complete:true record for authoritative comparisons."""
    _require_exact_keys(record, _COMPLETE_KEYS, "complete")
    _require_int(record["end_offset"], "complete.end_offset")
    _require_bool(record["complete"], "complete.complete")
    if record["complete"] is not True:
        raise CppDumpValidationError("authoritative dump is partial (complete is false)")


def _require_exact_keys(record: dict[str, object], keys: frozenset[str], path: str) -> None:
    """Reject missing and unexpected schema members with one stable diagnostic."""
    if set(record) != keys:
        raise CppDumpValidationError(f"{path} must contain exactly: {', '.join(sorted(keys))}")


def _require_int(value: object, path: str) -> None:
    """Require a JSON integer without accepting booleans as Python ints."""
    if type(value) is not int:
        raise CppDumpValidationError(f"{path} must be an integer")


def _require_string(value: object, path: str) -> None:
    """Require a JSON string."""
    if not isinstance(value, str):
        raise CppDumpValidationError(f"{path} must be a string")


def _require_bool(value: object, path: str) -> None:
    """Require a JSON boolean."""
    if type(value) is not bool:
        raise CppDumpValidationError(f"{path} must be a boolean")


def _require_real(value: object, path: str) -> None:
    """Require a finite JSON number or one of the observer's non-finite string tags."""
    if type(value) in {int, float}:
        return
    if isinstance(value, str) and value in {"nan", "infinity", "-infinity"}:
        return
    raise CppDumpValidationError(f"{path} must be a JSON number or a non-finite Real tag")


def _require_coord(value: object, path: str, *, real: bool) -> None:
    """Require an x/y or x/y/z coordinate with type-appropriate components."""
    expected_keys = {"x", "y", "z"} if real else {"x", "y"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise CppDumpValidationError(f"{path} must contain exactly {', '.join(sorted(expected_keys))}")
    for key, component in value.items():
        if real:
            _require_real(component, f"{path}.{key}")
        else:
            _require_int(component, f"{path}.{key}")


def _require_hex_bits(value: object, digits: int, path: str) -> None:
    """Require the observer's uppercase 0x-prefixed fixed-width scalar representation."""
    if not isinstance(value, str) or len(value) != digits + 2 or _HEX_BITS.fullmatch(value) is None:
        raise CppDumpValidationError(f"{path} must be an uppercase {digits}-digit hex scalar")


def _require_hex_list(value: object, count: int, path: str) -> None:
    """Require a fixed number of 32-bit scalar bit patterns."""
    if not isinstance(value, list) or len(value) != count:
        raise CppDumpValidationError(f"{path} must contain {count} scalar bit patterns")
    for item in value:
        _require_hex_bits(item, 8, path)


def compare_replay(parsed: ParsedReplay, cpp_dump: CppReplayDump) -> ParityMismatch | None:
    """Return the first replay-relative mismatch, or ``None`` when both parses agree."""
    header = parsed.header
    system_time = header.system_time
    header_fields: tuple[tuple[str, object], ...] = (
        ("start_time", header.start_time),
        ("end_time", header.end_time),
        ("frame_count", header.frame_count),
        ("desync_game", header.flags.desync_game),
        ("quit_early", header.flags.quit_early),
        ("player_disconnects", list(header.flags.player_disconnects)),
        ("replay_name", header.replay_name),
        (
            "system_time",
            {
                "year": system_time[0],
                "month": system_time[1],
                "day_of_week": system_time[2],
                "day": system_time[3],
                "hour": system_time[4],
                "minute": system_time[5],
                "second": system_time[6],
                "milliseconds": system_time[7],
            },
        ),
        ("version_string", header.version_string),
        ("version_time_string", header.version_time_string),
        ("version_number", header.version_number),
        ("exe_crc", header.exe_crc),
        ("ini_crc", header.ini_crc),
        ("game_options", header.game_options),
        ("local_player_index", header.local_player_index),
        ("header_end_offset", header.header_end_offset),
    )
    mismatch = _compare_fields(0, "header", header_fields, cpp_dump.header)
    if mismatch is not None:
        return mismatch

    setup_fields = tuple(parsed.setup.to_dict().items())
    mismatch = _compare_fields(parsed.setup.start_offset, "setup", setup_fields, cpp_dump.setup)
    if mismatch is not None:
        return mismatch

    if parsed.completion_status != "complete":
        return ParityMismatch(parsed.end_offset, "completion_status", parsed.completion_status, "complete")
    if len(parsed.commands) != len(cpp_dump.commands):
        return ParityMismatch(
            parsed.command_stream_offset,
            "command_count",
            len(parsed.commands),
            len(cpp_dump.commands),
        )

    for index, (python_command, cpp_command) in enumerate(zip(parsed.commands, cpp_dump.commands, strict=True)):
        command_path = f"commands[{index}]"
        command_fields: tuple[tuple[str, object], ...] = (
            ("frame", python_command.frame),
            ("end_offset", python_command.end_offset),
            ("message_type", python_command.message_type),
            ("message_name", python_command.message_name),
            ("player_index", python_command.player_index),
        )
        mismatch = _compare_fields(python_command.start_offset, command_path, command_fields, cpp_command)
        if mismatch is not None:
            return mismatch

        cpp_arguments = cast(list[dict[str, object]], cpp_command["arguments"])
        if len(python_command.arguments) != len(cpp_arguments):
            return ParityMismatch(
                python_command.start_offset,
                f"{command_path}.argument_count",
                len(python_command.arguments),
                len(cpp_arguments),
            )
        for argument_index, (python_argument, cpp_argument) in enumerate(
            zip(python_command.arguments, cpp_arguments, strict=True)
        ):
            argument_path = f"{command_path}.arguments[{argument_index}]"
            argument_fields: tuple[tuple[str, object], ...] = (
                ("type", python_argument.argument_type.value),
                ("type_name", python_argument.argument_type.name),
                ("value", _argument_value(python_argument)),
                ("raw_scalar_bits", _argument_raw_scalar_bits(python_argument)),
            )
            mismatch = _compare_fields(
                python_command.start_offset,
                argument_path,
                argument_fields,
                cpp_argument,
            )
            if mismatch is not None:
                return mismatch

    cpp_end_offset = cpp_dump.complete["end_offset"]
    if parsed.end_offset != cpp_end_offset:
        return ParityMismatch(parsed.end_offset, "complete.end_offset", parsed.end_offset, cpp_end_offset)
    return None


def _compare_fields(
    offset: int,
    path: str,
    python_fields: tuple[tuple[str, object], ...],
    cpp_fields: dict[str, object],
) -> ParityMismatch | None:
    """Compare one ordered group while preserving the first differing path."""
    for field, python_value in python_fields:
        cpp_value = cpp_fields[field]
        if python_value != cpp_value:
            return ParityMismatch(offset, f"{path}.{field}", python_value, cpp_value)
    return None


def _argument_value(argument: ReplayArgument) -> object:
    """Return the JSON value shape emitted by ``ReplayParseDump`` for one argument."""
    value = argument.value
    if isinstance(value, Coord3D):
        return {"x": _real_value(value.x), "y": _real_value(value.y), "z": _real_value(value.z)}
    if isinstance(value, ICoord2D):
        return {"x": value.x, "y": value.y}
    if isinstance(value, IRegion2D):
        return {
            "lo": {"x": value.lo.x, "y": value.lo.y},
            "hi": {"x": value.hi.x, "y": value.hi.y},
        }
    if isinstance(value, float):
        return _real_value(value)
    return value


def _real_value(value: float) -> float | str:
    """Mirror the C++ observer's valid-JSON representation of non-finite Real values."""
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "-infinity" if value < 0 else "infinity"
    return float(f"{value:.9g}")


def _argument_raw_scalar_bits(argument: ReplayArgument) -> str | list[str]:
    """Format the exact little-endian argument bytes using the observer's scalar groups."""
    argument_type = argument.argument_type
    if argument_type is GameMessageArgumentDataType.BOOLEAN:
        return "0x01" if argument.value else "0x00"
    if argument_type is GameMessageArgumentDataType.WIDE_CHAR:
        return _hex_bits(argument.raw_bytes)
    if argument_type in {
        GameMessageArgumentDataType.LOCATION,
        GameMessageArgumentDataType.PIXEL,
        GameMessageArgumentDataType.PIXEL_REGION,
    }:
        return [_hex_bits(argument.raw_bytes[offset : offset + 4]) for offset in range(0, len(argument.raw_bytes), 4)]
    return _hex_bits(argument.raw_bytes)


def _hex_bits(raw_bytes: bytes) -> str:
    """Return uppercase unsigned hex at the scalar's serialized width."""
    return f"0x{int.from_bytes(raw_bytes, byteorder='little', signed=False):0{len(raw_bytes) * 2}X}"
