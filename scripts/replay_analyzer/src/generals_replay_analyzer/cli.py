"""Deterministic command-line inspection for observed Zero Hour replay bytes."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from . import __version__
from .binary import Coord3D, ICoord2D, IRegion2D
from .commands import ReplayArgument, ReplayCommand
from .contracts import MessageCatalogValidationError
from .errors import ReplayParseError
from .parser import ParsedReplay, parse_replay
from .provenance import sha256_file

if TYPE_CHECKING:
    from .engine.config import EngineRunConfig
    from .engine.result import EngineRunResult, EngineRunStatus


def _parser() -> argparse.ArgumentParser:
    """Build the compact public CLI parser without network or model dependencies."""
    parser = argparse.ArgumentParser(prog="replay-analyzer")
    subcommands = parser.add_subparsers(dest="command", required=True)
    inspect = subcommands.add_parser("inspect", help="inspect replay bytes")
    inspect.add_argument("file", type=Path)
    inspect.add_argument("--format", choices=("human", "json"), default="human")
    inspect.add_argument("--commands", action="store_true", help="include complete decoded command records")
    export = subcommands.add_parser("export-telemetry", help="run authoritative headless replay telemetry")
    export.add_argument("replay", type=Path, help="Zero Hour replay input")
    export.add_argument("--engine", type=Path, required=True, help="generalszh.exe runtime path")
    export.add_argument("--data-root", type=Path, help="isolated product data root")
    export.add_argument("--timeout", type=int, default=900, help="engine timeout in seconds (default: 900)")
    export.add_argument(
        "--movement-sample-frames",
        type=int,
        default=15,
        help="maximum moving-entity sample interval (default: 15)",
    )
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
        "command_stream_offset": parsed.command_stream_offset,
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


def _write_json_document(document: dict[str, object]) -> None:
    """Write one stable machine-readable CLI document."""
    print(json.dumps(document, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


# TheSuperHackers @feature Leex 21/08/2026 Expose isolated authoritative telemetry without loading it for inspection. (#TBD)
def export_telemetry(replay: Path, config: EngineRunConfig) -> EngineRunResult:
    """Keep optional engine validation dependencies outside the inspection CLI import path."""
    from .engine.runner import export_telemetry as run

    return run(replay, config)


def _export_exit_code(status: EngineRunStatus) -> int:
    if status.value == "success":
        return 0
    if status.value in {"valid_crc_mismatch", "replay_truncated", "interrupted"}:
        return 3
    if status.value in {"input_unavailable", "invalid_replay_header", "truncated_input"}:
        return 4
    if status.value in {"timeout", "nonzero_engine_failure", "launch_failure", "input_changed"}:
        return 5
    return 6


def _invalid_export_request(error: Exception) -> dict[str, object]:
    return {
        "run_id": None,
        "run_dir": None,
        "status": "request_invalid",
        "exit_code": None,
        "duration_seconds": 0.0,
        "replay_quality": "failed",
        "strategy_analysis_scope": "none",
        "trace_path": None,
        "catalog_path": None,
        "map_assets": [],
        "outcome_path": None,
        "stdout_path": None,
        "stderr_path": None,
        "diagnostics": [{"code": "request_invalid", "message": str(error)}],
    }


def _absolute_cli_path(path: Path) -> Path:
    """Make a relative convenience path absolute without resolving aliases for the strict boundary."""
    return path if path.is_absolute() else Path.cwd() / path


def _run_export(arguments: argparse.Namespace) -> int:
    """Resolve CLI convenience paths before entering the strict library boundary."""
    from .engine.config import EngineRunConfig, EngineRunConfigurationError

    try:
        replay = _absolute_cli_path(arguments.replay)
        executable = _absolute_cli_path(arguments.engine)
        options: dict[str, object] = {
            "executable": executable,
            "timeout_seconds": arguments.timeout,
            "movement_sample_frames": arguments.movement_sample_frames,
        }
        if arguments.data_root is not None:
            options["data_root"] = _absolute_cli_path(arguments.data_root)
        config = EngineRunConfig(**options)  # type: ignore[arg-type]
        result = export_telemetry(replay, config)
    except (EngineRunConfigurationError, OSError) as error:
        _write_json_document(_invalid_export_request(error))
        return 2
    _write_json_document(result.to_public_dict())
    return _export_exit_code(result.status)


# TheSuperHackers @feature Leex 19/08/2026 Expose deterministic observed replay inspection without LLM or network calls. (#TBD)
def main(argv: Sequence[str] | None = None) -> int:
    """Run the inspection CLI and return a deterministic process status for replay failures."""
    arguments = _parser().parse_args(argv)
    if arguments.command == "export-telemetry":
        return _run_export(arguments)
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
