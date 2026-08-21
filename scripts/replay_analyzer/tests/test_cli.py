"""Behavioral contract tests for the deterministic replay inspection CLI."""

import json
import struct
from pathlib import Path
from typing import cast

import pytest
from fixture_builder import command_bytes, replay_header_bytes

from generals_replay_analyzer import __version__
from generals_replay_analyzer import cli as cli_module
from generals_replay_analyzer.cli import main
from generals_replay_analyzer.engine.config import EngineRunConfig, EngineRunConfigurationError
from generals_replay_analyzer.engine.result import (
    EngineRunResult,
    EngineRunStatus,
    ReplayQuality,
    StrategyAnalysisScope,
)
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


def _cli_result(run_dir: Path, status: EngineRunStatus) -> EngineRunResult:
    run_dir.mkdir(parents=True)
    stdout = run_dir / "stdout.log"
    stderr = run_dir / "stderr.log"
    trace = run_dir / "trace.ndjson"
    outcome = run_dir / "replay-outcome.json"
    catalog = run_dir / f"game-data-catalog-v1-{'a' * 64}.json"
    map_asset = run_dir / "map-assets-v1" / ("b" * 64) / "manifest.json"
    for path in (stdout, stderr, trace, outcome, catalog, map_asset):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    partial = status is EngineRunStatus.VALID_CRC_MISMATCH
    return EngineRunResult(
        run_id="123e4567-e89b-42d3-a456-426614174000",
        run_dir=run_dir,
        trace_path=trace,
        catalog_path=catalog,
        map_assets=(map_asset,),
        outcome_path=outcome,
        stdout_path=stdout,
        stderr_path=stderr,
        exit_code=1 if partial else 0,
        status=status,
        duration_seconds=1.5,
        replay_quality=ReplayQuality.PARTIAL if partial else ReplayQuality.ENGINE_VERIFIED,
        strategy_analysis_scope=(
            StrategyAnalysisScope.OBSERVED_BOUNDARY_ONLY if partial else StrategyAnalysisScope.FULL_MATCH
        ),
        diagnostics=(),
    )


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        (EngineRunStatus.SUCCESS, 0),
        (EngineRunStatus.VALID_CRC_MISMATCH, 3),
        (EngineRunStatus.REPLAY_TRUNCATED, 3),
        (EngineRunStatus.TRUNCATED_INPUT, 4),
    ],
)
def test_export_telemetry_cli_resolves_inputs_and_emits_stable_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    status: EngineRunStatus,
    expected_exit: int,
) -> None:
    """Catch the public command bypassing strict config or emitting ad hoc non-JSON status."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    replay = tmp_path / "fixture.rep"
    replay.write_bytes(b"GENREP")
    captured: dict[str, object] = {}

    def fake_export(replay_path: Path, config: EngineRunConfig) -> EngineRunResult:
        captured.update({"replay": replay_path, "config": config})
        return _cli_result(tmp_path / f"run-{status.value}", status)

    monkeypatch.setattr(cli_module, "export_telemetry", fake_export)
    monkeypatch.chdir(tmp_path)

    assert main(
        [
            "export-telemetry",
            replay.name,
            "--engine",
            executable.name,
            "--data-root",
            "data",
            "--timeout",
            "77",
            "--movement-sample-frames",
            "30",
        ]
    ) == expected_exit

    output = _json_output(capsys)
    assert output["status"] == status.value
    assert output["trace_path"].endswith("trace.ndjson")
    assert captured["replay"] == replay.resolve()
    config = cast(EngineRunConfig, captured["config"])
    assert config.executable == executable.resolve()
    assert config.data_root == (tmp_path / "data").resolve()
    assert config.timeout_seconds == 77
    assert config.movement_sample_frames == 30


def test_export_telemetry_cli_returns_typed_json_for_invalid_request(tmp_path: Path, capsys: object) -> None:
    """Catch configuration failures escaping as a traceback or unstable plain-text diagnostic."""
    replay = tmp_path / "fixture.rep"
    replay.write_bytes(b"GENREP")

    assert main(["export-telemetry", str(replay), "--engine", str(tmp_path / "missing.exe")]) == 2

    output = _json_output(capsys)
    assert output["status"] == "request_invalid"
    assert output["trace_path"] is None
    assert output["map_assets"] == []
    assert output["diagnostics"][0]["code"] == "request_invalid"


def test_export_telemetry_cli_does_not_silently_resolve_parent_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    replay = tmp_path / "fixture.rep"
    replay.write_bytes(b"GENREP")
    monkeypatch.chdir(tmp_path)

    def strict_export(replay_path: Path, _config: EngineRunConfig) -> EngineRunResult:
        assert ".." in replay_path.parts
        raise EngineRunConfigurationError("replay input path contains an unsafe Windows path component: ..")

    monkeypatch.setattr(cli_module, "export_telemetry", strict_export)
    aliased_replay = Path("unused") / ".." / replay.name

    assert main(["export-telemetry", str(aliased_replay), "--engine", executable.name]) == 2
    assert _json_output(capsys)["status"] == "request_invalid"
