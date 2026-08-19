"""Minimal source-grounded replay byte fixtures for command-parser tests."""

import struct
from collections.abc import Iterable


def _nul_ascii(value: str) -> bytes:
    return value.encode("ascii") + b"\0"


def _nul_utf16(value: str) -> bytes:
    return value.encode("utf-16-le") + b"\0\0"


def replay_header_bytes() -> bytes:
    """Return a valid, smallest practical Zero Hour header before command bytes."""
    options = "US=1;M=00Maps/Test;MC=1;MS=2;SD=3;C=100;SR=0;SC=10000;O=N;S=O:O:O:O:O:O:O:O:;"
    return b"".join(
        (
            b"GENREP",
            struct.pack("<iiI", -10, 20, 30),
            bytes([0, 0] + [0] * 8),
            _nul_utf16("Replay"),
            struct.pack("<8H", 2026, 8, 2, 19, 12, 30, 0, 7),
            _nul_utf16("1.04"),
            _nul_utf16("build"),
            struct.pack("<III", 0x104, 0xAABBCCDD, 0x11223344),
            _nul_ascii(options),
            _nul_ascii("0"),
        )
    )


def command_bytes(
    frame: int,
    message_type: int,
    player_index: int,
    type_runs: Iterable[tuple[int, int]],
    payloads: Iterable[bytes],
) -> bytes:
    """Encode one RecorderClass::writeToFile command without deriving payload widths."""
    runs = tuple(type_runs)
    return b"".join(
        (
            struct.pack("<IiiB", frame, message_type, player_index, len(runs)),
            b"".join(struct.pack("<BB", argument_type, count) for argument_type, count in runs),
            *payloads,
        )
    )
