"""Behavioral contract tests for the deterministic replay inspection CLI."""

import json
import struct
from pathlib import Path

from fixture_builder import command_bytes, replay_header_bytes

from generals_replay_analyzer import __version__
from generals_replay_analyzer.cli import main
from generals_replay_analyzer.provenance import sha256_file

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"


def _json_output(capsys: object) -> dict[str, object]:
    """Read the CLI's complete one-document stdout contract."""
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_inspect_json_reports_observed_complete_replay_without_commands(capsys: object) -> None:
    """Reject inspection output that omits byte evidence, parser context, or header facts."""
    assert main(["inspect", str(FIXTURE_PATH), "--format", "json"]) == 0

    output = _json_output(capsys)

    assert output["evidence_tier"] == "observed"
    assert output["sha256"] == sha256_file(FIXTURE_PATH)
    assert output["parser_version"] == __version__
    assert output["warnings"] == []
    assert output["header"]["magic"] == "GENREP"
    assert output["setup"] == {
        "difficulty": 1,
        "original_game_mode": 5,
        "rank_points": 0,
        "max_fps": 0,
        "start_offset": 326,
        "end_offset": 342,
    }
    assert output["command_stream_offset"] == 342
    assert output["command_stream_offset"] == output["setup"]["end_offset"]
    assert output["command_stream_offset"] > output["header"]["header_end_offset"]
    assert output["command_count"] > 0
    assert output["completion_status"] == "complete"
    assert "commands" not in output


def test_inspect_commands_uses_the_documented_complete_command_record(capsys: object) -> None:
    """Reject a flag that returns an unstable partial command representation."""
    assert main(["inspect", str(FIXTURE_PATH), "--format", "json", "--commands"]) == 0

    output = _json_output(capsys)

    commands = output["commands"]
    assert isinstance(commands, list)
    assert len(commands) == output["command_count"]
    assert set(commands[0]) == {
        "arguments",
        "end_offset",
        "frame",
        "message_name",
        "message_type",
        "player_index",
        "seconds",
        "start_offset",
    }
    command_with_argument = next(command for command in commands if command["arguments"])
    assert set(command_with_argument["arguments"][0]) == {"raw_bytes_hex", "type", "type_name", "value"}


def test_inspect_malformed_replay_returns_typed_error_without_traceback(tmp_path: Path, capsys: object) -> None:
    """Reject malformed input escaping as a traceback or an untyped parser failure."""
    malformed = tmp_path / "malformed.rep"
    malformed.write_bytes(b"not a replay")

    assert main(["inspect", str(malformed), "--format", "json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[invalid_magic] offset 0" in captured.err
    assert "Traceback" not in captured.err


def test_inspect_json_includes_parser_truncation_warning(tmp_path: Path, capsys: object) -> None:
    """Reject CLI output that loses a non-fatal parser warning while serializing JSON."""
    truncated = tmp_path / "truncated.rep"
    truncated.write_bytes(
        replay_header_bytes() + struct.pack("<iiii", 1, 5, 0, 0) + command_bytes(30, 1001, 0, [], []) + b"\x01"
    )

    assert main(["inspect", str(truncated), "--format", "json"]) == 0

    output = _json_output(capsys)

    assert output["completion_status"] == "truncated"
    assert output["warnings"] == [
        {
            "code": "truncated_replay",
            "message": "truncated next frame field",
            "token": str(output["setup"]["end_offset"] + 13),
        }
    ]
