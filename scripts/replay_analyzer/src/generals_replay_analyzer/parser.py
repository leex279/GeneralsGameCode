"""Replay-file command stream parser with explicit clean/truncated completion."""

from dataclasses import dataclass
from pathlib import Path

from .binary import BinaryReader
from .commands import ReplayCommand, parse_command
from .errors import TruncatedReplayError
from .header import parse_replay_header
from .model import ParseWarning, ReplayHeader, ReplaySetup


@dataclass(frozen=True)
class ParsedReplay:
    """Header plus all complete commands and the trustworthy terminal replay offset."""

    header: ReplayHeader
    setup: ReplaySetup
    command_stream_offset: int
    commands: tuple[ReplayCommand, ...]
    warnings: tuple[ParseWarning, ...]
    end_offset: int
    completion_status: str


# TheSuperHackers @feature Leex 19/08/2026 Decode Recorder setup before preserving command-stream completion evidence. (#TBD)
def parse_replay(path: Path) -> ParsedReplay:
    """Parse ``path`` through its command stream without scanning after an incomplete command."""
    data = path.read_bytes()
    header = parse_replay_header(data)
    reader = BinaryReader(data)
    reader.read_exact(header.header_end_offset)
    setup = _parse_replay_setup(reader)
    command_stream_offset = reader.offset
    commands: list[ReplayCommand] = []
    warnings = list(header.warnings)
    trustworthy_offset = command_stream_offset

    while reader.offset < len(data):
        next_frame_offset = reader.offset
        if len(data) - next_frame_offset < 4:
            return _truncated_result(
                header,
                setup,
                command_stream_offset,
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
                setup,
                command_stream_offset,
                commands,
                warnings,
                trustworthy_offset,
                error.offset,
                f"truncated command at offset {error.offset}",
            )
        commands.append(command)
        trustworthy_offset = command.end_offset

    return ParsedReplay(header, setup, command_stream_offset, tuple(commands), tuple(warnings), trustworthy_offset, "complete")


def _parse_replay_setup(reader: BinaryReader) -> ReplaySetup:
    """Read the four Int values RecorderClass consumes before its first ``readNextFrame`` call."""
    start_offset = reader.offset
    return ReplaySetup(
        difficulty=reader.read_i32(),
        original_game_mode=reader.read_i32(),
        rank_points=reader.read_i32(),
        max_fps=reader.read_i32(),
        start_offset=start_offset,
        end_offset=reader.offset,
    )


def _truncated_result(
    header: ReplayHeader,
    setup: ReplaySetup,
    command_stream_offset: int,
    commands: list[ReplayCommand],
    warnings: list[ParseWarning],
    trustworthy_offset: int,
    error_offset: int,
    message: str,
) -> ParsedReplay:
    """Return the complete prefix only; recorder streams cannot safely be resynchronized."""
    warnings.append(ParseWarning("truncated_replay", message, str(error_offset)))
    return ParsedReplay(
        header,
        setup,
        command_stream_offset,
        tuple(commands),
        tuple(warnings),
        trustworthy_offset,
        "truncated",
    )
