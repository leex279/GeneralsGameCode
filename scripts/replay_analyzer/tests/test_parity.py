"""Engine-free parity checks for authoritative C++ replay parse dumps."""

import json
import struct
from pathlib import Path

import pytest
from fixture_builder import command_bytes, replay_header_bytes

from generals_replay_analyzer.parser import parse_replay

GAME_OPTIONS = "US=1;M=00Maps/Test;MC=1;MS=2;SD=3;C=100;SR=0;SC=10000;O=N;S=O:O:O:O:O:O:O:O:;"
CPP_SAMPLE_PATH = Path(__file__).parent / "fixtures" / "parity" / "tiny_cpp.ndjson"


def _write_replay(tmp_path: Path, *, real_value: float = 1.5, message_type: int = 1003) -> Path:
    """Write one command containing every supported argument representation."""
    command = command_bytes(
        30,
        message_type,
        1,
        [(argument_type, 1) for argument_type in range(11)],
        [
            struct.pack("<i", -7),
            struct.pack("<f", real_value),
            b"\x01",
            struct.pack("<i", -2),
            struct.pack("<i", 3),
            struct.pack("<I", 0x89ABCDEF),
            struct.pack("<fff", 1.0, -2.5, 0.0),
            struct.pack("<ii", -4, 5),
            struct.pack("<iiii", -1, -2, 3, 4),
            struct.pack("<I", 0xFFFFFFFF),
            "Å".encode("utf-16-le"),
        ],
    )
    replay = tmp_path / "tiny.rep"
    replay.write_bytes(replay_header_bytes() + struct.pack("<iiii", 1, 5, 0, 0) + command)
    return replay


def _valid_records(*, cpp_real_value: float = 1.5, real_bits: str = "0x3FC00000") -> list[dict[str, object]]:
    """Return hand-checked C++ records for the tiny replay fixture."""
    return [
        {
            "record": "header",
            "filename": "tiny.rep",
            "for_playback": True,
            "start_time": -10,
            "end_time": 20,
            "frame_count": 30,
            "desync_game": False,
            "quit_early": False,
            "player_disconnects": [False] * 8,
            "replay_name": "Replay",
            "system_time": {
                "year": 2026,
                "month": 8,
                "day_of_week": 2,
                "day": 19,
                "hour": 12,
                "minute": 30,
                "second": 0,
                "milliseconds": 7,
            },
            "version_string": "1.04",
            "version_time_string": "build",
            "version_number": 0x104,
            "exe_crc": 0xAABBCCDD,
            "ini_crc": 0x11223344,
            "game_options": GAME_OPTIONS,
            "local_player_index": 0,
            "header_end_offset": 172,
        },
        {
            "record": "message_catalog",
            "messages": [{"message_type": 1003, "message_name": "MSG_DESTROY_SELECTED_GROUP"}],
        },
        {
            "record": "setup",
            "difficulty": 1,
            "original_game_mode": 5,
            "rank_points": 0,
            "max_fps": 0,
            "start_offset": 172,
            "end_offset": 188,
        },
        {
            "record": "command",
            "frame": 30,
            "start_offset": 192,
            "end_offset": 286,
            "message_type": 1003,
            "message_name": "MSG_DESTROY_SELECTED_GROUP",
            "player_index": 1,
            "arguments": [
                {"type": 0, "type_name": "INTEGER", "value": -7, "raw_scalar_bits": "0xFFFFFFF9"},
                {"type": 1, "type_name": "REAL", "value": cpp_real_value, "raw_scalar_bits": real_bits},
                {"type": 2, "type_name": "BOOLEAN", "value": True, "raw_scalar_bits": "0x01"},
                {"type": 3, "type_name": "OBJECT_ID", "value": -2, "raw_scalar_bits": "0xFFFFFFFE"},
                {"type": 4, "type_name": "DRAWABLE_ID", "value": 3, "raw_scalar_bits": "0x00000003"},
                {"type": 5, "type_name": "TEAM_ID", "value": 0x89ABCDEF, "raw_scalar_bits": "0x89ABCDEF"},
                {
                    "type": 6,
                    "type_name": "LOCATION",
                    "value": {"x": 1.0, "y": -2.5, "z": 0.0},
                    "raw_scalar_bits": ["0x3F800000", "0xC0200000", "0x00000000"],
                },
                {
                    "type": 7,
                    "type_name": "PIXEL",
                    "value": {"x": -4, "y": 5},
                    "raw_scalar_bits": ["0xFFFFFFFC", "0x00000005"],
                },
                {
                    "type": 8,
                    "type_name": "PIXEL_REGION",
                    "value": {"lo": {"x": -1, "y": -2}, "hi": {"x": 3, "y": 4}},
                    "raw_scalar_bits": ["0xFFFFFFFF", "0xFFFFFFFE", "0x00000003", "0x00000004"],
                },
                {"type": 9, "type_name": "TIMESTAMP", "value": 0xFFFFFFFF, "raw_scalar_bits": "0xFFFFFFFF"},
                {"type": 10, "type_name": "WIDE_CHAR", "value": "Å", "raw_scalar_bits": "0x00C5"},
            ],
        },
        {"record": "complete", "end_offset": 286, "complete": True},
    ]


def _write_dump(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    dump = tmp_path / "cpp.ndjson"
    dump.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return dump


def test_cpp_dump_matches_every_python_replay_field_and_raw_argument_bit(tmp_path: Path) -> None:
    """Reject a parity checker that ignores any serialized command argument representation."""
    from generals_replay_analyzer.parity import compare_replay, load_cpp_dump

    parsed = parse_replay(_write_replay(tmp_path))
    cpp_dump = load_cpp_dump(CPP_SAMPLE_PATH)

    assert compare_replay(parsed, cpp_dump) is None


def test_parity_reports_the_first_mismatch_with_offset_path_and_values(tmp_path: Path) -> None:
    """Reject aggregate or context-free mismatch diagnostics that cannot locate the replay evidence."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    records[3]["frame"] = 31
    parsed = parse_replay(_write_replay(tmp_path))
    cpp_dump = load_cpp_dump(_write_dump(tmp_path, records))

    mismatch = compare_replay(parsed, cpp_dump)

    assert mismatch == ParityMismatch(188, "commands[0].frame", 30, 31)
    assert str(mismatch) == "replay parity mismatch at byte 188: commands[0].frame: Python=30, C++=31"


def test_parity_validates_cpp_payload_start_four_bytes_after_python_frame_start(tmp_path: Path) -> None:
    """Reject a C++ diagnostic payload start that is not immediately after the serialized frame field."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    records[3]["start_offset"] = 193

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(188, "commands[0].start_offset", 192, 193)


@pytest.mark.parametrize(
    ("field", "python_value", "cpp_value"),
    [
        ("start_time", -10, -11),
        ("end_time", 20, 21),
        ("frame_count", 30, 31),
        ("desync_game", False, True),
        ("quit_early", False, True),
        ("player_disconnects", [False] * 8, [True] + [False] * 7),
        ("replay_name", "Replay", "Other"),
        (
            "system_time",
            {
                "year": 2026,
                "month": 8,
                "day_of_week": 2,
                "day": 19,
                "hour": 12,
                "minute": 30,
                "second": 0,
                "milliseconds": 7,
            },
            {
                "year": 2025,
                "month": 8,
                "day_of_week": 2,
                "day": 19,
                "hour": 12,
                "minute": 30,
                "second": 0,
                "milliseconds": 7,
            },
        ),
        ("version_string", "1.04", "1.03"),
        ("version_time_string", "build", "other-build"),
        ("version_number", 0x104, 0x103),
        ("exe_crc", 0xAABBCCDD, 0),
        ("ini_crc", 0x11223344, 0),
        ("game_options", GAME_OPTIONS, GAME_OPTIONS.replace("US=1", "US=2")),
        ("local_player_index", 0, 1),
        ("header_end_offset", 172, 171),
    ],
)
def test_parity_checks_every_cplusplus_header_value(
    tmp_path: Path, field: str, python_value: object, cpp_value: object
) -> None:
    """Reject accepting a C++ header that diverges from the replay's serialized Python header."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    records[0][field] = cpp_value

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(0, f"header.{field}", python_value, cpp_value)


@pytest.mark.parametrize(
    ("field", "python_value", "cpp_value"),
    [
        ("difficulty", 1, 2),
        ("original_game_mode", 5, 6),
        ("rank_points", 0, 1),
        ("max_fps", 0, 30),
        ("start_offset", 172, 171),
        ("end_offset", 188, 187),
    ],
)
def test_parity_checks_every_setup_value_and_boundary(
    tmp_path: Path, field: str, python_value: object, cpp_value: object
) -> None:
    """Reject shifting or changing the four Recorder setup integers before the command stream."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    records[2][field] = cpp_value

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(172, f"setup.{field}", python_value, cpp_value)


def test_parity_checks_command_count_before_indexing_commands(tmp_path: Path) -> None:
    """Reject indexing past a shortened engine dump instead of reporting its command-count mismatch."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    del records[3]

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(188, "command_count", 1, 0)


@pytest.mark.parametrize(
    ("field", "python_value", "cpp_value"),
    [
        ("end_offset", 286, 285),
        ("player_index", 1, 2),
    ],
)
def test_parity_checks_every_command_identity_and_boundary(
    tmp_path: Path, field: str, python_value: object, cpp_value: object
) -> None:
    """Reject accepting a command whose identity or replay-relative boundaries diverge."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    records[3][field] = cpp_value

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(188, f"commands[0].{field}", python_value, cpp_value)


def test_parity_accepts_cpp_nine_digit_real_decimal_when_float32_bits_match(tmp_path: Path) -> None:
    """Reject treating the observer's nine-digit decimal rendering as a value mismatch when f32 bits agree."""
    from generals_replay_analyzer.parity import compare_replay, load_cpp_dump

    parsed = parse_replay(_write_replay(tmp_path, real_value=2483.817626953125))
    cpp_dump = load_cpp_dump(
        _write_dump(
            tmp_path,
            _valid_records(cpp_real_value=2483.81763, real_bits="0x451B3D15"),
        )
    )

    assert compare_replay(parsed, cpp_dump) is None


def test_parity_checks_numeric_message_type_against_an_internally_valid_cpp_mapping(tmp_path: Path) -> None:
    """Reject a different numeric engine message even when its catalog name is internally consistent."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    catalog = records[1]["messages"]
    assert isinstance(catalog, list)
    catalog.append({"message_type": 1004, "message_name": "MSG_REMOVE_FROM_SELECTED_GROUP"})
    records[3]["message_type"] = 1004
    records[3]["message_name"] = "MSG_REMOVE_FROM_SELECTED_GROUP"

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(188, "commands[0].message_type", 1003, 1004)


def test_parity_checks_symbolic_message_name_against_the_cpp_catalog(tmp_path: Path) -> None:
    """Reject a different compiled symbolic label for the same numeric message id."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    catalog = records[1]["messages"]
    assert isinstance(catalog, list)
    entry = catalog[0]
    assert isinstance(entry, dict)
    entry["message_name"] = "MSG_REMOVE_FROM_SELECTED_GROUP"
    records[3]["message_name"] = "MSG_REMOVE_FROM_SELECTED_GROUP"

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(
        188,
        "commands[0].message_name",
        "MSG_DESTROY_SELECTED_GROUP",
        "MSG_REMOVE_FROM_SELECTED_GROUP",
    )


def test_parity_accepts_uncataloged_numeric_command_only_as_unknown_message(tmp_path: Path) -> None:
    """Reject dropping a future numeric command when both parsers preserve it as symbolically unknown."""
    from generals_replay_analyzer.parity import compare_replay, load_cpp_dump

    records = _valid_records()
    records[3]["message_type"] = 777
    records[3]["message_name"] = "UnknownMessage"

    parsed = parse_replay(_write_replay(tmp_path, message_type=777))
    cpp_dump = load_cpp_dump(_write_dump(tmp_path, records))

    assert compare_replay(parsed, cpp_dump) is None


@pytest.mark.parametrize("argument_index", list(range(11)))
def test_parity_checks_each_argument_type(tmp_path: Path, argument_index: int) -> None:
    """Reject accepting any argument when C++ reports a different serialized type number."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    arguments = records[3]["arguments"]
    assert isinstance(arguments, list)
    argument = arguments[argument_index]
    assert isinstance(argument, dict)
    cpp_type = 5 if argument_index == 0 else 0
    cpp_type_name = "TEAM_ID" if argument_index == 0 else "INTEGER"
    argument["type"] = cpp_type
    argument["type_name"] = cpp_type_name
    argument["value"] = 0
    argument["raw_scalar_bits"] = "0x00000000"

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(
        188,
        f"commands[0].arguments[{argument_index}].type",
        argument_index,
        cpp_type,
    )


def test_parity_checks_argument_count_before_indexing_arguments(tmp_path: Path) -> None:
    """Reject indexing past a shortened C++ argument list instead of reporting its count mismatch."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    arguments = records[3]["arguments"]
    assert isinstance(arguments, list)
    del arguments[-1]

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(188, "commands[0].argument_count", 11, 10)


def test_parity_checks_argument_symbolic_type_name(tmp_path: Path) -> None:
    """Reject a symbolic argument label that disagrees with the serialized type number."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    records = _valid_records()
    arguments = records[3]["arguments"]
    assert isinstance(arguments, list)
    argument = arguments[3]
    assert isinstance(argument, dict)
    argument["type_name"] = "INTEGER"

    with pytest.raises(
        CppDumpValidationError,
        match=r"command.arguments\[3\].type_name does not match type 3",
    ):
        load_cpp_dump(_write_dump(tmp_path, records))


@pytest.mark.parametrize(
    ("argument_index", "python_value", "cpp_value"),
    [
        (0, -7, -8),
        (1, 1.5, 1.25),
        (2, True, False),
        (3, -2, -3),
        (4, 3, 4),
        (5, 0x89ABCDEF, 1),
        (6, {"x": 1.0, "y": -2.5, "z": 0.0}, {"x": 2.0, "y": -2.5, "z": 0.0}),
        (7, {"x": -4, "y": 5}, {"x": -3, "y": 5}),
        (8, {"lo": {"x": -1, "y": -2}, "hi": {"x": 3, "y": 4}}, {"lo": {"x": 0, "y": -2}, "hi": {"x": 3, "y": 4}}),
        (9, 0xFFFFFFFF, 0),
        (10, "Å", "B"),
    ],
)
def test_parity_checks_each_argument_value(
    tmp_path: Path, argument_index: int, python_value: object, cpp_value: object
) -> None:
    """Reject accepting any typed argument whose decoded C++ value diverges."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    arguments = records[3]["arguments"]
    assert isinstance(arguments, list)
    argument = arguments[argument_index]
    assert isinstance(argument, dict)
    argument["value"] = cpp_value

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(
        188,
        f"commands[0].arguments[{argument_index}].value",
        python_value,
        cpp_value,
    )


@pytest.mark.parametrize(
    ("argument_index", "python_bits", "cpp_bits"),
    [
        (0, "0xFFFFFFF9", "0x00000000"),
        (1, "0x3FC00000", "0x00000000"),
        (2, "0x01", "0x00"),
        (3, "0xFFFFFFFE", "0x00000000"),
        (4, "0x00000003", "0x00000000"),
        (5, "0x89ABCDEF", "0x00000000"),
        (6, ["0x3F800000", "0xC0200000", "0x00000000"], ["0x00000000"] * 3),
        (7, ["0xFFFFFFFC", "0x00000005"], ["0x00000000"] * 2),
        (8, ["0xFFFFFFFF", "0xFFFFFFFE", "0x00000003", "0x00000004"], ["0x00000000"] * 4),
        (9, "0xFFFFFFFF", "0x00000000"),
        (10, "0x00C5", "0x0000"),
    ],
)
def test_parity_checks_each_argument_raw_scalar_bit_pattern(
    tmp_path: Path, argument_index: int, python_bits: object, cpp_bits: object
) -> None:
    """Reject decimal equality when the engine's authoritative serialized scalar bits diverge."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    arguments = records[3]["arguments"]
    assert isinstance(arguments, list)
    argument = arguments[argument_index]
    assert isinstance(argument, dict)
    argument["raw_scalar_bits"] = cpp_bits

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(
        188,
        f"commands[0].arguments[{argument_index}].raw_scalar_bits",
        python_bits,
        cpp_bits,
    )


def test_parity_checks_final_stream_offset_and_python_completion(tmp_path: Path) -> None:
    """Reject a terminal record that does not end at the Python parser's trustworthy replay boundary."""
    from generals_replay_analyzer.parity import ParityMismatch, compare_replay, load_cpp_dump

    records = _valid_records()
    records[-1]["end_offset"] = 285

    mismatch = compare_replay(
        parse_replay(_write_replay(tmp_path)),
        load_cpp_dump(_write_dump(tmp_path, records)),
    )

    assert mismatch == ParityMismatch(286, "complete.end_offset", 286, 285)


@pytest.mark.parametrize(
    ("mutate", "expected_message"),
    [
        (lambda records: records.__setitem__(1, records[2]), "record 2 must be message_catalog"),
        (lambda records: records.__setitem__(2, records[0]), "record 3 must be setup"),
        (lambda records: records.pop(), "terminal record must be complete"),
        (lambda records: records.append({"record": "command"}), "terminal record must be complete"),
        (lambda records: records[-1].__setitem__("complete", False), "authoritative dump is partial"),
        (lambda records: records[0].pop("game_options"), "header must contain exactly"),
        (lambda records: records[3].__setitem__("arguments", "not-a-list"), "command.arguments must be a list"),
    ],
)
def test_cpp_dump_rejects_malformed_order_missing_data_shapes_and_partial_output(
    tmp_path: Path, mutate: object, expected_message: str
) -> None:
    """Reject malformed or partial engine evidence before it can be treated as authoritative parity."""
    from collections.abc import Callable
    from typing import cast

    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    records = _valid_records()
    cast(Callable[[list[dict[str, object]]], object], mutate)(records)

    with pytest.raises(CppDumpValidationError, match=expected_message):
        load_cpp_dump(_write_dump(tmp_path, records))


def test_cpp_dump_rejects_invalid_json_with_its_line_number(tmp_path: Path) -> None:
    """Reject a syntactically invalid NDJSON line with the exact evidence line in the diagnostic."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    dump = tmp_path / "invalid.ndjson"
    dump.write_text('{"record":"header"}\n{not json}\n', encoding="utf-8")

    with pytest.raises(CppDumpValidationError, match="invalid JSON on line 2"):
        load_cpp_dump(dump)


def test_cpp_dump_rejects_duplicate_root_json_object_members(tmp_path: Path) -> None:
    """Reject a repeated root member even when both JSON values are identical."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    duplicate = CPP_SAMPLE_PATH.read_text(encoding="utf-8").replace(
        '{"record":"header"',
        '{"record":"header","record":"header"',
        1,
    )
    dump = tmp_path / "duplicate-root.ndjson"
    dump.write_text(duplicate, encoding="utf-8")

    with pytest.raises(CppDumpValidationError, match="duplicate JSON object member 'record' on line 1"):
        load_cpp_dump(dump)


def test_cpp_dump_rejects_duplicate_nested_json_object_members(tmp_path: Path) -> None:
    """Reject repeated members inside nested argument/header structures, not only at record roots."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    duplicate = CPP_SAMPLE_PATH.read_text(encoding="utf-8").replace(
        '"system_time":{"year":2026',
        '"system_time":{"year":2026,"year":2026',
        1,
    )
    dump = tmp_path / "duplicate-nested.ndjson"
    dump.write_text(duplicate, encoding="utf-8")

    with pytest.raises(CppDumpValidationError, match="duplicate JSON object member 'year' on line 1"):
        load_cpp_dump(dump)


def test_cpp_dump_rejects_unhashable_real_value_as_a_schema_error(tmp_path: Path) -> None:
    """Reject an invalid object-valued Real with a typed dump diagnostic instead of leaking TypeError."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    records = _valid_records()
    arguments = records[3]["arguments"]
    assert isinstance(arguments, list)
    argument = arguments[1]
    assert isinstance(argument, dict)
    argument["value"] = {"not": "a real"}

    with pytest.raises(CppDumpValidationError, match=r"command.arguments\[1\].value must be a JSON number"):
        load_cpp_dump(_write_dump(tmp_path, records))


def test_cpp_dump_rejects_numeric_real_that_overflows_to_infinity(tmp_path: Path) -> None:
    """Reject standard-JSON numeric overflow; non-finite observer values must use explicit string tags."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    overflow = CPP_SAMPLE_PATH.read_text(encoding="utf-8").replace('"value":1.5', '"value":1e400', 1)
    dump = tmp_path / "overflow-real.ndjson"
    dump.write_text(overflow, encoding="utf-8")

    with pytest.raises(CppDumpValidationError, match=r"command.arguments\[1\].value must be finite"):
        load_cpp_dump(dump)


@pytest.mark.parametrize(
    ("duplicate_field", "duplicate_value", "expected_message"),
    [
        ("message_type", 1003, "duplicate message_catalog id 1003"),
        ("message_name", "MSG_DESTROY_SELECTED_GROUP", "duplicate message_catalog name MSG_DESTROY_SELECTED_GROUP"),
    ],
)
def test_cpp_dump_rejects_duplicate_catalog_ids_and_names(
    tmp_path: Path, duplicate_field: str, duplicate_value: object, expected_message: str
) -> None:
    """Reject ambiguous compiled catalog mappings before symbolic command validation."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    records = _valid_records()
    catalog = records[1]["messages"]
    assert isinstance(catalog, list)
    duplicate = {"message_type": 1004, "message_name": "MSG_REMOVE_FROM_SELECTED_GROUP"}
    duplicate[duplicate_field] = duplicate_value
    catalog.append(duplicate)

    with pytest.raises(CppDumpValidationError, match=expected_message):
        load_cpp_dump(_write_dump(tmp_path, records))


def test_cpp_dump_exposes_compiled_catalog_entries_in_numeric_order(tmp_path: Path) -> None:
    """Reject reconstructing the catalog from Python source instead of the engine-emitted record."""
    from generals_replay_analyzer.parity import cpp_message_catalog_entries, load_cpp_dump

    records = _valid_records()
    catalog = records[1]["messages"]
    assert isinstance(catalog, list)
    catalog.append({"message_type": 1004, "message_name": "MSG_REMOVE_FROM_SELECTED_GROUP"})

    cpp_dump = load_cpp_dump(_write_dump(tmp_path, records))

    assert cpp_message_catalog_entries(cpp_dump) == (
        (1003, "MSG_DESTROY_SELECTED_GROUP"),
        (1004, "MSG_REMOVE_FROM_SELECTED_GROUP"),
    )


def test_cpp_dump_rejects_catalog_entries_out_of_numeric_order(tmp_path: Path) -> None:
    """Reject a reordered catalog that cannot be treated as the compiled enum walk."""
    from generals_replay_analyzer.parity import CppDumpValidationError, load_cpp_dump

    records = _valid_records()
    catalog = records[1]["messages"]
    assert isinstance(catalog, list)
    catalog.append({"message_type": 1, "message_name": "MSG_FRAME_TICK"})

    with pytest.raises(CppDumpValidationError, match="message_catalog ids must be strictly increasing"):
        load_cpp_dump(_write_dump(tmp_path, records))


def test_generated_message_catalog_is_written_only_from_cpp_entries(tmp_path: Path) -> None:
    """Reject a catalog generator that scrapes headers or changes the compiled id/name order."""
    from generals_replay_analyzer.contracts import load_message_catalog
    from generals_replay_analyzer.parity import load_cpp_dump, write_generated_message_catalog

    records = _valid_records()
    catalog = records[1]["messages"]
    assert isinstance(catalog, list)
    catalog.append({"message_type": 1004, "message_name": "MSG_REMOVE_FROM_SELECTED_GROUP"})
    cpp_dump = load_cpp_dump(_write_dump(tmp_path, records))
    destination = tmp_path / "catalog.json"

    write_generated_message_catalog(
        cpp_dump,
        destination,
        engine_build="modern-win32-release",
        generated_at_utc="2026-08-19T12:00:00Z",
    )

    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document == {
        "schema_version": 1,
        "game": "Command & Conquer: Generals Zero Hour",
        "patch": "1.04",
        "engine_build": "modern-win32-release",
        "source_header_path": "Core/GameEngine/Include/Common/MessageStream.h",
        "generated_at_utc": "2026-08-19T12:00:00Z",
        "generated": True,
        "generation_note": "Generated from ReplayParseDump message_catalog; do not hand-edit.",
        "message_types": [
            {"id": 1003, "name": "MSG_DESTROY_SELECTED_GROUP"},
            {"id": 1004, "name": "MSG_REMOVE_FROM_SELECTED_GROUP"},
        ],
    }
    assert tuple(load_message_catalog(destination).names_by_id.items()) == (
        (1003, "MSG_DESTROY_SELECTED_GROUP"),
        (1004, "MSG_REMOVE_FROM_SELECTED_GROUP"),
    )
