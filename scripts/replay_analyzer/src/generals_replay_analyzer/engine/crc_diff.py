"""Compare paired retail and modern per-frame CRC diagnostic logs."""

import re
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Literal


class CRCDiffError(ValueError):
    """Raised when one diagnostic input cannot be compared safely."""


@dataclass(frozen=True)
class CRCDifference:
    """The first exact line where paired CRC logs stop agreeing."""

    frame: int
    line_number: int
    component: str
    reason: Literal[
        "line_mismatch",
        "retail_line_missing",
        "modern_line_missing",
        "retail_frame_missing",
        "modern_frame_missing",
    ]
    retail_line: str | None
    modern_line: str | None


_FRAME_NAME = re.compile(r"^DebugFrame_(\d{6})\.txt$")
_LINE_PREFIX = re.compile(r"^\d+:\d{5} ")
_COMPONENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^CRC at start of frame \d+ is 0x[0-9A-Fa-f]{8}$"), "game_logic_start"),
    (re.compile(r"^CRC after objects for frame \d+ is 0x[0-9A-Fa-f]{8}$"), "objects"),
    (re.compile(r"^RandomSeed: "), "random_seed"),
    (re.compile(r"^CRC after partition manager for frame \d+ is 0x[0-9A-Fa-f]{8}$"), "partition_manager"),
    (re.compile(r"^CRC after module factory for frame \d+ is 0x[0-9A-Fa-f]{8}$"), "module_factory"),
    (re.compile(r"^CRC after PlayerList for frame \d+ is 0x[0-9A-Fa-f]{8}$"), "player_list"),
    (re.compile(r"^CRC after AI(?: |$)"), "ai"),
    (re.compile(r"^CRC for frame \d+ is 0x[0-9A-Fa-f]{8}$"), "game_logic_final"),
)


def _frame_logs(directory: Path, side: str) -> dict[int, Path]:
    if not directory.is_dir():
        raise CRCDiffError(f"{side} CRC directory does not exist: {directory}")
    logs: dict[int, Path] = {}
    for path in directory.iterdir():
        match = _FRAME_NAME.fullmatch(path.name)
        if match is not None and path.is_file():
            logs[int(match.group(1))] = path
    if not logs:
        raise CRCDiffError(f"{side} CRC directory contains no DebugFrame logs: {directory}")
    return logs


def _read_lines(path: Path, side: str) -> list[str]:
    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise CRCDiffError(f"cannot read {side} CRC log '{path}': {error}") from error
    if not lines:
        raise CRCDiffError(f"{side} CRC log is empty: {path}")
    return lines


def _component(line: str) -> str:
    message = _LINE_PREFIX.sub("", line, count=1)
    for pattern, component in _COMPONENT_PATTERNS:
        if pattern.search(message):
            return component
    return message


def first_crc_difference(retail_directory: Path, modern_directory: Path) -> CRCDifference | None:
    """Return the earliest deterministic difference between paired logs."""
    # TheSuperHackers @feature Leex 24/08/2026 Diff normalized text lines in numeric frame order without changing diagnostic bytes. (#TBD)
    retail_logs = _frame_logs(retail_directory, "retail")
    modern_logs = _frame_logs(modern_directory, "modern")
    for frame in sorted(set(retail_logs) | set(modern_logs)):
        retail_path = retail_logs.get(frame)
        modern_path = modern_logs.get(frame)
        if retail_path is None:
            assert modern_path is not None
            modern_line = _read_lines(modern_path, "modern")[0]
            return CRCDifference(
                frame, 1, _component(modern_line), "retail_frame_missing", None, modern_line
            )
        if modern_path is None:
            retail_line = _read_lines(retail_path, "retail")[0]
            return CRCDifference(
                frame, 1, _component(retail_line), "modern_frame_missing", retail_line, None
            )
        retail_lines = _read_lines(retail_path, "retail")
        modern_lines = _read_lines(modern_path, "modern")
        for line_number, (retail_line, modern_line) in enumerate(
            zip_longest(retail_lines, modern_lines),
            start=1,
        ):
            if retail_line == modern_line:
                continue
            line = retail_line if retail_line is not None else modern_line
            assert line is not None
            if retail_line is None:
                reason: Literal[
                    "line_mismatch", "retail_line_missing", "modern_line_missing"
                ] = "retail_line_missing"
            elif modern_line is None:
                reason = "modern_line_missing"
            else:
                reason = "line_mismatch"
            return CRCDifference(
                frame,
                line_number,
                _component(line),
                reason,
                retail_line,
                modern_line,
            )
    return None
