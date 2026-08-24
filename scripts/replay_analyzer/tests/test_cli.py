"""Behavioral contract tests for the deterministic replay inspection CLI."""

import argparse
import json
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fixture_builder import command_bytes, replay_header_bytes

from generals_replay_analyzer import __version__
from generals_replay_analyzer import cli as cli_module
from generals_replay_analyzer.analysis_pipeline.command import (
    AnalysisCommandResult,
    AnalysisJobStatusDTO,
    AnalysisReportStatusDTO,
)
from generals_replay_analyzer.analysis_pipeline.planner import AnalysisPlanningError
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
    assert output["timebase"] == {
        "logic_frames_per_second": 60,
        "observed_frames_per_second": pytest.approx(56_003 / 940),
        "source": "replay_header_wall_clock",
    }
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
    timed_command = next(command for command in commands if command["frame"] > 0)
    assert timed_command["seconds"] == pytest.approx(timed_command["frame"] / 60)


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


def test_importing_cli_keeps_optional_web_stack_lazy() -> None:
    """Catch inspect/parser commands importing the web server stack as a module side effect."""
    script = (
        "import sys; import generals_replay_analyzer.cli; "
        "assert 'fastapi' not in sys.modules; assert 'uvicorn' not in sys.modules"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_web_command_parses_default_loopback_bind_without_starting_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catch a default bind or port drifting away from the local product contract."""
    captured: dict[str, object] = {}

    def fake_run_web(arguments: argparse.Namespace) -> int:
        captured["host"] = arguments.host
        captured["port"] = arguments.port
        return 0

    monkeypatch.setattr(cli_module, "_run_web", fake_run_web)

    assert main(["web"]) == 0
    assert captured == {"host": "127.0.0.1", "port": 8765}


def test_web_command_accepts_ipv6_loopback_and_passes_an_already_created_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch Uvicorn receiving an import string or a non-loopback rewritten bind."""
    application = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_web_application", lambda: application)

    def fake_serve(app: object, host: str, port: int) -> None:
        captured.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(cli_module, "_serve_web", fake_serve)

    assert main(["web", "--host", "::1", "--port", "9876"]) == 0
    assert captured == {"app": application, "host": "::1", "port": 9876}


@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "::", "example.test"])
def test_web_command_rejects_every_nonliteral_bind_before_composition(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    """Catch remote or ambiguous hosts reaching application construction or Uvicorn."""
    monkeypatch.setattr(
        cli_module,
        "_web_application",
        lambda: pytest.fail("invalid host reached web composition"),
    )
    monkeypatch.setattr(cli_module, "_serve_web", lambda *_args: pytest.fail("invalid host reached Uvicorn"))

    assert main(["web", "--host", host]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "literal loopback" in captured.err
    assert "Traceback" not in captured.err


def test_import_service_uses_the_single_production_bootstrap_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch import/jobs bypassing backup, lock, integrity, and schema-identity bootstrap."""
    from generals_replay_analyzer import db
    from generals_replay_analyzer.web import bootstrap as web_bootstrap

    prepared: list[Path] = []

    class FakeCoordinator:
        def prepare(self, settings: Any) -> None:
            prepared.append(settings.data_root)
            settings.ensure_directories()

    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(tmp_path / "product"))
    monkeypatch.setattr(
        db,
        "upgrade_database",
        lambda *_args, **_kwargs: pytest.fail("legacy direct migration owner was called"),
    )
    monkeypatch.setattr(web_bootstrap, "create_production_bootstrapper", lambda _readiness: FakeCoordinator())

    _service, engine, _request_telemetry = cli_module._import_service()
    try:
        assert prepared == [(tmp_path / "product").resolve()]
    finally:
        engine.dispose()


@pytest.mark.parametrize("configured", [False, True])
def test_import_composition_matches_the_configured_telemetry_policy(
    configured: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the public import composition diverging from its worker's telemetry capability."""
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(tmp_path / "product"))
    monkeypatch.delenv("GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE", raising=False)
    if configured:
        executable = tmp_path / "generalszh.exe"
        executable.write_bytes(b"engine")
        monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE", str(executable))

    composition = cli_module._import_service()
    service, engine = composition[0], composition[1]
    try:
        registered = service.worker_control_port().registered_stages()
        assert ("telemetry" in registered) is configured
        assert service._telemetry_acquirer_version == (
            "engine-telemetry-v1" if configured else "none"
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("configured", [False, True])
def test_public_import_persists_the_configured_telemetry_intent(
    configured: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    """Catch the public import command silently requesting parser-only processing."""
    from sqlalchemy import select

    from generals_replay_analyzer.config import AnalyzerSettings
    from generals_replay_analyzer.db import create_database_engine, create_session_factory
    from generals_replay_analyzer.db.models import Job

    data_root = tmp_path / "product"
    replay = tmp_path / "league.rep"
    replay.write_bytes(b"replay bytes")
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))
    monkeypatch.delenv("GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE", raising=False)
    if configured:
        executable = tmp_path / "generalszh.exe"
        executable.write_bytes(b"engine")
        monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE", str(executable))

    assert main(["import", str(replay), "--json"]) == 0
    _json_output(capsys)
    settings = AnalyzerSettings.model_validate({})
    query_engine = create_database_engine(settings.database_path)
    try:
        with create_session_factory(query_engine)() as session:
            discovery = session.scalar(select(Job).where(Job.stage == "discover"))
            assert discovery is not None
            assert discovery.input_json["request_telemetry"] is configured
    finally:
        query_engine.dispose()


def test_worker_command_uses_external_runtime_defaults_without_uvicorn_or_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class FakeRuntime:
        def run_forever(self) -> None:
            calls.append((-1, -1))

        def shutdown(self) -> None:
            calls.append((-3, -3))

    class FakeEngine:
        def dispose(self) -> None:
            calls.append((-2, -2))

    def compose(poll_seconds: int, lease_seconds: int) -> tuple[FakeRuntime, FakeEngine]:
        calls.append((poll_seconds, lease_seconds))
        return FakeRuntime(), FakeEngine()

    monkeypatch.setattr(cli_module, "_worker_application", compose)
    monkeypatch.setattr(cli_module, "_serve_web", lambda *_args: pytest.fail("worker started Uvicorn"))

    assert main(["worker"]) == 0
    assert calls == [(1, 120), (-1, -1), (-3, -3), (-2, -2)]


def test_worker_command_validates_cross_field_bounds_before_composition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_worker_application",
        lambda *_args: pytest.fail("invalid worker options reached composition"),
        raising=False,
    )

    assert main(["worker", "--poll-seconds", "6", "--lease-seconds", "15"]) == 2
    captured = capsys.readouterr()
    assert "three poll intervals" in captured.err
    assert "Traceback" not in captured.err


def test_worker_command_refuses_incompatible_schema_without_migrating_or_tracing_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    from generals_replay_analyzer.web.bootstrap import IncompatibleSchemaError

    monkeypatch.setattr(
        cli_module,
        "_worker_application",
        lambda *_args: (_ for _ in ()).throw(IncompatibleSchemaError("worker schema identity is incompatible")),
    )

    assert main(["worker"]) == 2
    captured = capsys.readouterr()
    assert "incompatible_worker_schema" in captured.err
    assert "Traceback" not in captured.err


def test_worker_command_reports_unsettled_owned_child_without_private_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    from generals_replay_analyzer.worker import OwnedChildSettlementError

    class Runtime:
        def run_forever(self) -> None:
            raise OwnedChildSettlementError("owned_child_settlement_failed")

        def shutdown(self) -> None:
            pass

    class Engine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "_worker_application", lambda *_args: (Runtime(), Engine()))

    assert main(["worker"]) == 2
    captured = capsys.readouterr()
    assert "owned_child_settlement_failed" in captured.err
    assert "Traceback" not in captured.err


@dataclass
class _AnalyzeApplication:
    result: AnalysisCommandResult | None = None
    error: Exception | None = None
    calls: list[tuple[str, bool, bool]] | None = None
    closed: bool = False
    close_error: Exception | None = None

    def run(self, replay_public_id: str, *, execute: bool, allow_ollama: bool) -> AnalysisCommandResult:
        if self.calls is not None:
            self.calls.append((replay_public_id, execute, allow_ollama))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _analysis_result(status: str, *, allow_ollama: bool = False) -> AnalysisCommandResult:
    return AnalysisCommandResult(
        status,  # type: ignore[arg-type]
        "123e4567-e89b-42d3-a456-426614174120",
        allow_ollama,
        0,
        (
            AnalysisJobStatusDTO(
                "123e4567-e89b-42d3-a456-426614174121",
                "render_report",
                "succeeded" if status == "succeeded" else "pending",
                "a" * 64,
                "123e4567-e89b-42d3-a456-426614174122" if status == "succeeded" else None,
            ),
        ),
        (
            AnalysisReportStatusDTO(
                "123e4567-e89b-42d3-a456-426614174123",
                "b" * 64,
                "c" * 64,
                "123e4567-e89b-42d3-a456-426614174124",
                "d" * 64,
                "123e4567-e89b-42d3-a456-426614174125",
                "e" * 64,
            ),
        )
        if status == "succeeded"
        else (),
    )


def test_analyze_defaults_to_plan_only_and_ollama_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the safe default starting execution or silently opting into a model."""
    calls: list[tuple[str, bool, bool]] = []
    application = _AnalyzeApplication(_analysis_result("planned"), calls=calls)
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120"]) == 0
    assert calls == [("123e4567-e89b-42d3-a456-426614174120", False, False)]
    assert application.closed is True


def test_analyze_json_is_path_free_and_preserves_opt_in_and_report_digests(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    """Catch machine output leaking locators or dropping the durable Ollama opt-in identity."""
    calls: list[tuple[str, bool, bool]] = []
    application = _AnalyzeApplication(_analysis_result("succeeded", allow_ollama=True), calls=calls)
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(
        [
            "analyze",
            "123e4567-e89b-42d3-a456-426614174120",
            "--execute",
            "--allow-ollama",
            "--json",
        ]
    ) == 0

    output = _json_output(capsys)
    assert calls == [("123e4567-e89b-42d3-a456-426614174120", True, True)]
    assert output["status"] == "succeeded"
    assert output["allow_ollama"] is True
    assert output["reports"][0]["structured_asset_sha256"] == "d" * 64
    serialized = json.dumps(output)
    assert "\\" not in serialized and "://" not in serialized and "relative_path" not in serialized


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("awaiting_observations", 3),
        ("incomplete", 3),
        ("claim_limit_reached", 3),
        ("failed", 5),
    ],
)
def test_analyze_returns_stable_nonzero_statuses(
    status: str,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _AnalyzeApplication(_analysis_result(status))
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120", "--json"]) == exit_code
    assert application.closed is True


def test_analyze_rejects_invalid_replay_identity_before_composition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_analyze_application",
        lambda: pytest.fail("invalid public identity reached application composition"),
    )

    assert main(["analyze", "NOT-A-REPLAY"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid_analysis_request" in captured.err
    assert "Traceback" not in captured.err


def test_analyze_rejects_unknown_replay_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    application = _AnalyzeApplication(error=AnalysisPlanningError("unknown_replay", "unknown replay public ID"))
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174999"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid_analysis_request" in captured.err
    assert "Traceback" not in captured.err
    assert application.closed is True


def test_analyze_maps_persisted_graph_corruption_to_exit_five(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    """Catch internal evidence corruption being mislabeled as a user request error."""
    application = _AnalyzeApplication(
        error=AnalysisPlanningError("invalid_observation_graph", "C:\\private\\corrupt graph")
    )
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120"]) == 5
    captured = capsys.readouterr()
    assert "analysis_execution_failed" in captured.err
    assert "private" not in captured.err and "Traceback" not in captured.err


def test_analyze_execution_failure_is_exit_five_without_private_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    """Catch foreground worker faults escaping as tracebacks or request-validation failures."""
    application = _AnalyzeApplication(error=RuntimeError("C:\\private\\worker failure"))
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120", "--execute"]) == 5
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "analysis_execution_failed" in captured.err
    assert "private" not in captured.err and "Traceback" not in captured.err
    assert application.closed is True


def test_analyze_unexpected_application_exception_is_stable_exit_five(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    class UnexpectedAnalysisFailure(Exception):
        pass

    application = _AnalyzeApplication(error=UnexpectedAnalysisFailure("private"))
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120"]) == 5
    captured = capsys.readouterr()
    assert "analysis_execution_failed" in captured.err
    assert "private" not in captured.err and "Traceback" not in captured.err


@pytest.mark.parametrize("error", [TypeError("private graph type"), ValueError("private graph value")])
def test_analyze_internal_type_or_value_fault_is_exit_five(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    """Catch persisted graph/data faults being mislabeled as invalid user input."""
    application = _AnalyzeApplication(error=error)
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120"]) == 5
    captured = capsys.readouterr()
    assert "analysis_execution_failed" in captured.err
    assert "private" not in captured.err and "Traceback" not in captured.err
    assert application.closed is True


def test_analyze_initialization_failure_is_path_free_exit_five(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_analyze_application",
        lambda: (_ for _ in ()).throw(OSError("C:\\private\\database")),
    )

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120"]) == 5
    captured = capsys.readouterr()
    assert "analysis_initialization_failed" in captured.err
    assert "private" not in captured.err


def test_analyze_maps_owned_settlement_and_lifecycle_failures_to_exit_five(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    from generals_replay_analyzer.importing import JobLifecycleError
    from generals_replay_analyzer.worker import OwnedChildSettlementError

    for error in (
        OwnedChildSettlementError("unsettled"),
        JobLifecycleError("lifecycle_conflict", "private lifecycle state"),
    ):
        application = _AnalyzeApplication(error=error)
        monkeypatch.setattr(cli_module, "_analyze_application", lambda application=application: application)
        assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120", "--execute"]) == 5
        captured = capsys.readouterr()
        assert "analysis_execution_failed" in captured.err
        assert "private" not in captured.err
        assert application.closed is True


def test_analyze_human_output_contains_only_public_status_and_digest_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    application = _AnalyzeApplication(_analysis_result("succeeded"))
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120"]) == 0
    captured = capsys.readouterr()
    assert "render_report succeeded" in captured.out
    assert "report 123e4567-e89b-42d3-a456-426614174123" in captured.out
    assert "\\" not in captured.out and "://" not in captured.out


def test_analyze_parent_and_stage_child_share_the_exact_production_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the foreground command drifting to a second handler registration list."""
    from generals_replay_analyzer import worker as worker_module

    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(tmp_path / "product"))
    application = cli_module._analyze_application()
    service, child_engine, _settings = worker_module._worker_service()
    try:
        assert application._runtime.control.registered_stages() == service.worker_control_port().registered_stages()
        assert application._runtime.watcher is None
        assert application._runtime.control.registered_stages() == (
            "analyze_llm",
            "assess_strategies",
            "derive_features",
            "discover",
            "hash",
            "import_observations",
            "manage_copy",
            "parse",
            "reconcile_identities",
            "render_report",
        )
    finally:
        application.close()
        child_engine.dispose()


def test_configured_foreground_analyze_composition_registers_engine_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch foreground analysis omitting telemetry jobs configured for its worker child."""
    executable = tmp_path / "generalszh.exe"
    executable.write_bytes(b"engine")
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(tmp_path / "product"))
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE", str(executable))

    application = cli_module._analyze_application()
    try:
        assert "telemetry" in application._runtime.control.registered_stages()
    finally:
        application.close()


def test_analyze_application_disposes_engine_when_runtime_shutdown_fails() -> None:
    """Catch a foreground cleanup fault leaking the SQLite engine."""
    class Command:
        pass

    class Runtime:
        def shutdown(self) -> None:
            raise OSError("shutdown failed")

    class Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    application = cli_module._AnalyzeApplication(Command(), Runtime(), engine)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="shutdown failed"):
        application.close()
    assert engine.disposed is True


def test_analyze_cleanup_failure_is_stable_exit_five_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    """Catch disposal or runtime-shutdown errors escaping after a successful plan."""
    application = _AnalyzeApplication(
        _analysis_result("planned"),
        close_error=OSError("C:\\private\\close failure"),
    )
    monkeypatch.setattr(cli_module, "_analyze_application", lambda: application)

    assert main(["analyze", "123e4567-e89b-42d3-a456-426614174120"]) == 5
    captured = capsys.readouterr()
    assert "analysis_cleanup_failed" in captured.err
    assert "private" not in captured.err and "Traceback" not in captured.err
    assert application.closed is True


@pytest.mark.parametrize("forbidden_constructor", ["supervisor", "worker"])
def test_default_analyze_uses_no_execution_or_ollama_machinery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    forbidden_constructor: str,
) -> None:
    """Catch plan-only analysis acquiring execution or network capability as a composition side effect."""
    from generals_replay_analyzer.config import AnalyzerSettings
    from generals_replay_analyzer.db import create_database_engine, create_session_factory
    from generals_replay_analyzer.db.models import Replay
    from generals_replay_analyzer.llm.service import HttpxOllamaTransport
    from generals_replay_analyzer.web.bootstrap import BootstrapReadinessState, create_production_bootstrapper
    from generals_replay_analyzer.worker import SubprocessSupervisorFactory, WorkerRuntime

    data_root = tmp_path / "product"
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))
    settings = AnalyzerSettings.model_validate({})
    create_production_bootstrapper(BootstrapReadinessState()).prepare(settings)
    engine = create_database_engine(settings.database_path)
    sessions = create_session_factory(engine)
    replay_id = "123e4567-e89b-42d3-a456-426614174140"
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    with sessions.begin() as session:
        session.add(
            Replay(
                public_id=replay_id,
                sha256="f" * 64,
                replay_name="fixture.rep",
                version_string="1.04",
                version_number=1,
                frame_count=1,
                start_time=0,
                end_time=1,
                exe_crc=1,
                ini_crc=2,
                map_crc=3,
                map_name="fixture.map",
                seed=4,
                header_json={},
                lifecycle_state="parsed",
                created_at=now,
                updated_at=now,
            )
        )
    engine.dispose()
    forbidden_type = SubprocessSupervisorFactory if forbidden_constructor == "supervisor" else WorkerRuntime
    monkeypatch.setattr(
        forbidden_type,
        "__init__",
        lambda *_args, **_kwargs: pytest.fail(f"plan-only analysis constructed {forbidden_constructor} machinery"),
    )
    monkeypatch.setattr(
        HttpxOllamaTransport,
        "__init__",
        lambda *_args, **_kwargs: pytest.fail("plan-only analysis created an Ollama transport"),
    )

    assert main(["analyze", replay_id, "--json"]) == 3
    output = _json_output(capsys)
    assert output["status"] == "awaiting_observations"
    assert output["allow_ollama"] is False


def test_cli_runtime_configuration_activates_persisted_settings(tmp_path: Path) -> None:
    """Import, analyze, and Web CLI composition consume the same fresh persisted settings loader."""

    from generals_replay_analyzer.configuration import ConfigurationStore, SettingChange

    configuration_root = tmp_path / "external-configuration"
    ConfigurationStore(configuration_root=configuration_root, environment={}).apply(
        expected_revision=0,
        changes=(
            SettingChange("movement_sample_frames", 90),
            SettingChange("minimum_longitudinal_sample_size", 23),
        ),
    )

    runtime = cli_module._runtime_configuration(
        configuration_root=configuration_root,
        environment={},
        values={"data_root": tmp_path / "product-data"},
    )

    assert runtime.settings.movement_sample_frames == 90
    assert runtime.settings.minimum_longitudinal_sample_size == 23
