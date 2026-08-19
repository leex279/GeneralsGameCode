"""Deterministic command-line inspection for observed Zero Hour replay bytes."""

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .binary import Coord3D, ICoord2D, IRegion2D
from .commands import ReplayArgument, ReplayCommand
from .contracts import MessageCatalogValidationError
from .errors import ReplayParseError
from .parser import ParsedReplay, parse_replay
from .provenance import sha256_file


def _parser() -> argparse.ArgumentParser:
    """Build the compact public CLI parser without network or model dependencies."""
    parser = argparse.ArgumentParser(prog="replay-analyzer")
    subcommands = parser.add_subparsers(dest="command", required=True)
    inspect = subcommands.add_parser("inspect", help="inspect replay bytes")
    inspect.add_argument("file", type=Path)
    inspect.add_argument("--format", choices=("human", "json"), default="human")
    inspect.add_argument("--commands", action="store_true", help="include complete decoded command records")
    return parser


def _json_value(value: object) -> object:
    """Convert replay-native values to deterministic JSON values without losing their shape."""
    if isinstance(value, Coord3D):
        return {"x": _json_value(value.x), "y": _json_value(value.y), "z": _json_value(value.z)}
    if isinstance(value, ICoord2D):
        return {"x": value.x, "y": value.y}
    if isinstance(value, IRegion2D):
        return {"lo": _json_value(value.lo), "hi": _json_value(value.hi)}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _argument_dict(argument: ReplayArgument) -> dict[str, object]:
    """Return the exact JSON command-argument contract used only with --commands."""
    return {
        "type": argument.argument_type.value,
        "type_name": argument.argument_type.name,
        "value": _json_value(argument.value),
        "raw_bytes_hex": argument.raw_bytes.hex().upper(),
    }


def _command_dict(command: ReplayCommand) -> dict[str, object]:
    """Return the complete decoded command record, including binary evidence boundaries."""
    return {
        "frame": command.frame,
        "seconds": command.seconds,
        "player_index": command.player_index,
        "message_type": command.message_type,
        "message_name": command.message_name,
        "arguments": [_argument_dict(argument) for argument in command.arguments],
        "start_offset": command.start_offset,
        "end_offset": command.end_offset,
    }


def _inspection_document(path: Path, parsed: ParsedReplay, include_commands: bool) -> dict[str, object]:
    """Return the stable observed-evidence JSON contract for a successfully parsed replay."""
    document: dict[str, object] = {
        "evidence_tier": "observed",
        "sha256": sha256_file(path),
        "parser_version": __version__,
        "warnings": [warning.to_dict() for warning in parsed.warnings],
        "header": parsed.header.to_dict(),
        "setup": parsed.setup.to_dict(),
        "command_count": len(parsed.commands),
        "completion_status": parsed.completion_status,
    }
    if include_commands:
        document["commands"] = [_command_dict(command) for command in parsed.commands]
    return document


def _write_human_summary(path: Path, parsed: ParsedReplay, output: TextIO) -> None:
    """Write a compact human summary while retaining complete facts in JSON mode."""
    print(
        f"{path.name}: {len(parsed.commands)} commands, {parsed.completion_status}, {len(parsed.warnings)} warnings",
        file=output,
    )


# TheSuperHackers @feature Leex 19/08/2026 Expose deterministic observed replay inspection without LLM or network calls. (#TBD)
def main(argv: Sequence[str] | None = None) -> int:
    """Run the inspection CLI and return a deterministic process status for replay failures."""
    arguments = _parser().parse_args(argv)
    if arguments.command != "inspect":
        return 2
    try:
        parsed = parse_replay(arguments.file)
        if arguments.format == "json":
            print(
                json.dumps(
                    _inspection_document(arguments.file, parsed, arguments.commands),
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            _write_human_summary(arguments.file, parsed, sys.stdout)
    except ReplayParseError as error:
        print(f"replay-analyzer: error: {error}", file=sys.stderr)
        return 2
    except MessageCatalogValidationError as error:
        print(f"replay-analyzer: error: [invalid_message_catalog] {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"replay-analyzer: error: [io_error] {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
