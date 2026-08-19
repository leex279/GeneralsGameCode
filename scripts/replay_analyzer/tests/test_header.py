"""Behavioral tests for the Zero Hour replay header contract."""

import json
import struct
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from generals_replay_analyzer.errors import (
    InvalidGameOptionsError,
    InvalidMagicError,
    InvalidStringLengthError,
    TruncatedReplayError,
)
from generals_replay_analyzer.header import parse_replay_header
from generals_replay_analyzer.model import ReplayFlags, ReplayHeader, ReplaySlot

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "zero_hour_1_04"
FIXTURE_PATH = FIXTURE_DIRECTORY / "leex279_vs_fox27.rep"
EXPECTED_PATH = FIXTURE_DIRECTORY / "leex279_vs_fox27.expected.json"


def _nul_ascii(value: str) -> bytes:
    return value.encode("ascii") + b"\0"


def _nul_utf16(value: str) -> bytes:
    return value.encode("utf-16-le") + b"\0\0"


def _valid_options(slots: str = "HAlice,ABCD,1234,TT,1,2,3,0,0:O:O:O:O:O:O:O:") -> str:
    return f"US=1;M=00Maps/Test;MC=1;MS=2;SD=3;C=100;SR=0;SC=10000;O=N;S={slots};"


def _header_bytes(
    *,
    magic: bytes = b"GENREP",
    flags: bytes | None = None,
    replay_name: bytes | None = None,
    options: str | None = None,
    slots: str | None = None,
) -> bytes:
    header_flags = bytes([0, 1] + [0] * 8) if flags is None else flags
    encoded_name = _nul_utf16("Replay") if replay_name is None else replay_name
    if options is None:
        game_options = _valid_options() if slots is None else _valid_options(slots)
    else:
        game_options = options
    return b"".join(
        (
            magic,
            struct.pack("<iiI", -10, 20, 30),
            header_flags,
            encoded_name,
            struct.pack("<8H", 2026, 8, 2, 19, 12, 30, 0, 7),
            _nul_utf16("1.04"),
            _nul_utf16("build"),
            struct.pack("<III", 0x104, 0xAABBCCDD, 0x11223344),
            _nul_ascii(game_options),
            _nul_ascii("0"),
        )
    )


def test_pinned_replay_header_matches_the_checked_in_source_grounded_json() -> None:
    """Reject field-order drift or fixture values inferred from its filename."""
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))["header"]

    header = parse_replay_header(FIXTURE_PATH.read_bytes())

    assert header.to_dict() == expected
    assert [slot.name for slot in header.slots if slot.kind == "human"] == ["leex279", "FOX27"]
    assert "3133811" not in {slot.name for slot in header.slots if slot.name is not None}
    assert "e80b96708aa4254945941fd5f81489bb" not in {
        slot.name for slot in header.slots if slot.name is not None
    }


def test_header_models_are_immutable_value_records() -> None:
    """Reject mutable metadata records that could detach fields from replay bytes."""
    header = parse_replay_header(_header_bytes())

    assert isinstance(header, ReplayHeader)
    assert isinstance(header.flags, ReplayFlags)
    assert isinstance(header.slots[0], ReplaySlot)
    assert isinstance(header.warnings, tuple)
    with pytest.raises(FrozenInstanceError):
        header.frame_count = 99  # type: ignore[misc]


def test_header_parses_explicit_slot_kinds_without_arithmetic_player_mapping() -> None:
    """Reject deriving a player identity from a command-like slot number."""
    header = parse_replay_header(
        _header_bytes(slots="HAlice,ABCD,1234,TT,1,2,3,0,0:CM,2,3,4,1:O:X:O:X:O:X:")
    )

    assert [(slot.index, slot.kind, slot.name, slot.ai_difficulty) for slot in header.slots] == [
        (0, "human", "Alice", None),
        (1, "ai", None, "medium"),
        (2, "open", None, None),
        (3, "closed", None, None),
        (4, "open", None, None),
        (5, "closed", None, None),
        (6, "open", None, None),
        (7, "closed", None, None),
    ]
    assert header.local_player_index == 0


def test_header_rejects_unknown_game_options_tokens() -> None:
    """Reject source-incompatible extension tokens as ParseAsciiStringToGameInfo does."""
    options = _valid_options() + "FUTURE=enabled;"

    with pytest.raises(InvalidGameOptionsError) as raised:
        parse_replay_header(_header_bytes(options=options))

    assert raised.value.code == "invalid_game_options"


def test_header_rejects_non_genrep_magic_at_the_start() -> None:
    """Reject accepting a file whose six-byte replay signature differs from the engine's."""
    with pytest.raises(InvalidMagicError) as raised:
        parse_replay_header(_header_bytes(magic=b"BADREP"))

    assert raised.value.code == "invalid_magic"
    assert raised.value.offset == 0


@pytest.mark.parametrize("payload", [b"A\x00" * 1025, b"\xff\xff" * 1025])
def test_header_rejects_oversized_or_unterminated_unicode_name_fields(payload: bytes) -> None:
    """Reject unbounded NUL scans, including bytes that a length-prefixed reader would misread."""
    with pytest.raises(InvalidStringLengthError) as raised:
        parse_replay_header(_header_bytes(replay_name=payload))

    assert raised.value.code == "invalid_string_length"
    assert raised.value.offset == 28


def test_header_rejects_truncated_flags_without_advancing_past_the_field() -> None:
    """Reject treating a partial two-flags-plus-eight-disconnects region as a header."""
    with pytest.raises(TruncatedReplayError) as raised:
        parse_replay_header(b"GENREP" + struct.pack("<iiI", -10, 20, 30) + b"\0")

    assert raised.value.code == "truncated_replay"
    assert raised.value.offset == 19


def test_header_rejects_truncated_slot_list() -> None:
    """Reject a game-options list that omits required Zero Hour slot entries."""
    with pytest.raises(InvalidGameOptionsError) as raised:
        parse_replay_header(_header_bytes(options=_valid_options(slots="HAlice,ABCD,1234,TT,1,2,3,0,0:")))

    assert raised.value.code == "invalid_game_options"


@pytest.mark.parametrize(
    "options",
    [
        "M=00Maps/Test;MC=1;MS=2;SD=3;C=100;S=O:O:O:O:O:O:O:",
        _valid_options(slots="Qbad:O:O:O:O:O:O:O:"),
        _valid_options(slots="HAlice,ABCD,1234,TT,1,2,3,0:O:O:O:O:O:O:O:"),
    ],
)
def test_header_rejects_malformed_game_options_grammar(options: str) -> None:
    """Reject incomplete required fields and malformed H/C/O/X slot grammar."""
    with pytest.raises(InvalidGameOptionsError) as raised:
        parse_replay_header(_header_bytes(options=options))

    assert raised.value.code == "invalid_game_options"
