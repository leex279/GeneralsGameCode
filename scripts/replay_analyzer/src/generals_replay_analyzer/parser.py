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
                _truncation_message(error, next_frame_offset),
            )
        commands.append(command)
        trustworthy_offset = command.end_offset

    return ParsedReplay(header, tuple(commands), tuple(warnings), trustworthy_offset, "complete")


def _truncation_message(error: TruncatedReplayError, command_start: int) -> str:
    """Describe whether a failed command read reached metadata or a typed payload."""
    if error.offset <= command_start + 12:
        return f"truncated command type-run count at offset {error.offset}"
    if error.offset <= command_start + 14:
        return f"truncated command type run at offset {error.offset}"
    return f"truncated command payload at offset {error.offset}"


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
