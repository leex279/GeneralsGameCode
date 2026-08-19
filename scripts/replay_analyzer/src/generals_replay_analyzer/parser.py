"""Replay-file command stream parser with explicit clean/truncated completion."""

from dataclasses import dataclass
from pathlib import Path

from .binary import BinaryReader
from .commands import ReplayCommand, parse_command
from .errors import TruncatedReplayError
from .header import parse_replay_header
from .model import ParseWarning, ReplayHeader


@dataclass(frozen=True)
class ParsedReplay:
    """Header plus all complete commands and the trustworthy terminal replay offset."""

    header: ReplayHeader
    commands: tuple[ReplayCommand, ...]
    warnings: tuple[ParseWarning, ...]
    end_offset: int
    completion_status: str


# TheSuperHackers @feature Leex 19/08/2026 Preserve valid commands while classifying terminal recorder truncation.
def parse_replay(path: Path) -> ParsedReplay:
    """Parse ``path`` through its command stream without scanning after an incomplete command."""
    data = path.read_bytes()
    header = parse_replay_header(data)
    reader = BinaryReader(data)
    reader.read_exact(header.header_end_offset)
    commands: list[ReplayCommand] = []
    warnings = list(header.warnings)
    trustworthy_offset = header.header_end_offset

    while reader.offset < len(data):
        next_frame_offset = reader.offset
        if len(data) - next_frame_offset < 4:
            return _truncated_result(
                header,
                commands,
                warnings,
                trustworthy_offset,
                next_frame_offset,
                "truncated next frame field",
            )
        try:
            command = parse_command(reader)
        except TruncatedReplayError as error:
            return _truncated_result(
                header,
                commands,
                warnings,
                trustworthy_offset,
                error.offset,
                f"truncated command at offset {error.offset}",
            )
        commands.append(command)
        trustworthy_offset = command.end_offset

    return ParsedReplay(header, tuple(commands), tuple(warnings), trustworthy_offset, "complete")


def _truncated_result(
    header: ReplayHeader,
    commands: list[ReplayCommand],
    warnings: list[ParseWarning],
    trustworthy_offset: int,
    error_offset: int,
    message: str,
) -> ParsedReplay:
    """Return the complete prefix only; recorder streams cannot safely be resynchronized."""
    warnings.append(ParseWarning("truncated_replay", message, str(error_offset)))
    return ParsedReplay(header, tuple(commands), tuple(warnings), trustworthy_offset, "truncated")
