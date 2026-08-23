"""Deterministic command-line inspection for observed Zero Hour replay bytes."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from . import __version__
from .binary import Coord3D, ICoord2D, IRegion2D
from .commands import ReplayArgument, ReplayCommand
from .contracts import MessageCatalogValidationError
from .errors import ReplayParseError
from .parser import ParsedReplay, parse_replay
from .provenance import sha256_file

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from .analysis_pipeline.command import AnalysisCommandResult, AnalysisCommandService
    from .config import RuntimeConfiguration
    from .engine.config import EngineRunConfig
    from .engine.result import EngineRunResult, EngineRunStatus
    from .importing import ImportService, JobClaimSelectorDTO, WorkerControlPort
    from .worker import WorkerRuntime


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
    import_command = subcommands.add_parser("import", help="enqueue a bounded replay file or folder snapshot")
    import_command.add_argument("path", type=Path)
    import_command.add_argument("--recursive", action="store_true")
    import_command.add_argument("--reference-only", action="store_true")
    import_command.add_argument("--json", action="store_true", dest="json_output")
    jobs = subcommands.add_parser("jobs", help="manage durable replay-analysis jobs")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    retry = job_commands.add_parser("retry", help="retry one eligible durable job")
    retry.add_argument("job_id")
    web = subcommands.add_parser("web", help="serve the local replay library")
    web.add_argument("--host", default="127.0.0.1", help="literal loopback bind (default: 127.0.0.1)")
    web.add_argument("--port", type=int, default=8765, help="loopback TCP port (default: 8765)")
    worker = subcommands.add_parser("worker", help="run the external replay-analysis worker")
    worker.add_argument("--poll-seconds", type=int, default=1, help="interruptible idle poll interval (default: 1)")
    worker.add_argument("--lease-seconds", type=int, default=120, help="durable job lease duration (default: 120)")
    analyze = subcommands.add_parser("analyze", help="plan or execute one replay analysis")
    analyze.add_argument("replay_public_id")
    analyze.add_argument("--execute", action="store_true", help="execute only safe analytics jobs for this replay")
    analyze.add_argument("--allow-ollama", action="store_true", help="opt into local Ollama interpretation")
    analyze.add_argument("--json", action="store_true", dest="json_output")
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


# TheSuperHackers @feature Leex 21/08/2026 Initialize resumable replay intake only at an explicit CLI boundary. (#TBD)
def _runtime_configuration(
    *,
    configuration_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    values: Mapping[str, object] | None = None,
) -> RuntimeConfiguration:
    """Load one process-lifetime settings/store identity for every CLI composition root."""

    from .config import load_runtime_configuration

    return load_runtime_configuration(
        configuration_root=configuration_root,
        environment=environment,
        values=values,
        version_identities=(("analyzer", __version__),),
    )


def _import_service() -> tuple[ImportService, Engine]:
    """Initialize managed paths and the packaged database only at the CLI application boundary."""
    from .db import create_database_engine, create_session_factory
    from .importing import ImportService
    from .storage import ContentAddressedStore
    from .web.bootstrap import BootstrapReadinessState, create_production_bootstrapper

    settings = _runtime_configuration().settings
    readiness = BootstrapReadinessState()
    create_production_bootstrapper(readiness).prepare(settings)
    engine = create_database_engine(settings.database_path)
    session_factory = create_session_factory(engine)
    return (
        ImportService(
            session_factory,
            settings,
            ContentAddressedStore(settings.managed_replay_directory),
            ContentAddressedStore(settings.cache_directory / "artifacts"),
            parser=parse_replay,
            clock=lambda: datetime.now(UTC),
            parser_version=__version__,
            telemetry_acquirer_version="none",
        ),
        engine,
    )


def _run_import(arguments: argparse.Namespace) -> int:
    from .importing import ImportRequest

    service, engine = _import_service()
    try:
        submission = service.submit(
            ImportRequest(
                _absolute_cli_path(arguments.path),
                recursive=arguments.recursive,
                reference_only=True if arguments.reference_only else None,
            )
        )
        if arguments.json_output:
            _write_json_document(asdict(submission))
        else:
            print(
                f"discovery {submission.discovery_job.public_id}: {submission.discovery_job.status}, "
                f"{submission.accepted_path_count} accepted, {submission.rejected_path_count} rejected"
            )
    finally:
        engine.dispose()
    return 0


def _run_jobs(arguments: argparse.Namespace) -> int:
    from .importing.jobs import JobStateError

    service, engine = _import_service()
    try:
        result = service.retry(arguments.job_id)
    except JobStateError as error:
        print(f"replay-analyzer: error: [{error.code}] {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    _write_json_document(asdict(result))
    return 0


# TheSuperHackers @feature Leex 22/08/2026 Compose the loopback web scaffold without importing it for other commands. (#TBD)
def _web_application() -> Any:
    from .web.app import create_app
    from .web.bootstrap import BootstrapReadinessState, create_production_bootstrapper
    from .web.dependencies import AnalyticsPortFactory

    runtime = _runtime_configuration()
    settings = runtime.settings
    readiness = BootstrapReadinessState()
    return create_app(
        settings,
        port_factory=AnalyticsPortFactory(runtime, readiness),
        bootstrapper=create_production_bootstrapper(readiness),
    )


def _serve_web(app: Any, host: str, port: int) -> None:
    import uvicorn

    logging.getLogger(__name__).info(
        "Serving replay analyzer host=%s port=%d analytics_adapter=unavailable",
        host,
        port,
    )
    uvicorn.run(app, host=host, port=port)


def _run_web(arguments: argparse.Namespace) -> int:
    from .web.app import validate_loopback_host, validate_port

    host = validate_loopback_host(arguments.host)
    port = validate_port(arguments.port)
    app = _web_application()
    _serve_web(app, host, port)
    return 0


def _worker_application(poll_seconds: int, lease_seconds: int) -> tuple[Any, Any]:
    """Import worker and database dependencies only for the explicit external command."""
    from .worker import compose_worker_runtime

    return compose_worker_runtime(poll_seconds, lease_seconds)


# TheSuperHackers @feature Leex 22/08/2026 Launch durable analysis outside Uvicorn without taking migration ownership. (#TBD)
def _run_worker(arguments: argparse.Namespace) -> int:
    from .web.bootstrap import IncompatibleSchemaError
    from .worker import OwnedChildSettlementError, validate_worker_options

    poll_seconds, lease_seconds = validate_worker_options(arguments.poll_seconds, arguments.lease_seconds)
    try:
        runtime, engine = _worker_application(poll_seconds, lease_seconds)
    except IncompatibleSchemaError:
        print(
            "replay-analyzer: error: [incompatible_worker_schema] worker schema identity is incompatible",
            file=sys.stderr,
        )
        return 2
    logging.getLogger(__name__).info(
        "Starting external replay-analysis worker poll_seconds=%d lease_seconds=%d",
        poll_seconds,
        lease_seconds,
    )
    try:
        try:
            runtime.run_forever()
        except OwnedChildSettlementError:
            print(
                "replay-analyzer: error: [owned_child_settlement_failed] owned execution tree did not settle",
                file=sys.stderr,
            )
            return 2
    finally:
        runtime.shutdown()
        engine.dispose()
    return 0


class _LazyAnalysisRuntime:
    """Acquire worker/subprocess capability only for an explicit execution claim."""

    watcher = None

    def __init__(self, control: WorkerControlPort) -> None:
        self.control = control
        self._runtime: WorkerRuntime | None = None

    def run_once(self, selector: JobClaimSelectorDTO) -> bool:
        if self._runtime is None:
            from uuid import uuid4

            from .worker import EventWaiter, SubprocessSupervisorFactory, WorkerRuntime

            self._runtime = WorkerRuntime(
                control=self.control,
                supervisors=SubprocessSupervisorFactory(),
                waiter=EventWaiter(),
                worker_public_id=str(uuid4()),
                poll_seconds=1,
                lease_seconds=120,
            )
        return self._runtime.run_once(selector)

    def shutdown(self) -> None:
        if self._runtime is not None:
            self._runtime.shutdown()


class _AnalyzeApplication:
    """Own the foreground command runtime and its database engine."""

    def __init__(self, command: AnalysisCommandService, runtime: _LazyAnalysisRuntime, engine: Engine) -> None:
        self._command = command
        self._runtime = runtime
        self._engine = engine

    def run(self, replay_public_id: str, *, execute: bool, allow_ollama: bool) -> AnalysisCommandResult:
        return self._command.run(replay_public_id, execute=execute, allow_ollama=allow_ollama)

    def close(self) -> None:
        try:
            self._runtime.shutdown()
        finally:
            self._engine.dispose()


def _analyze_application() -> _AnalyzeApplication:
    """Compose planning and foreground execution from the sole production stage root."""
    from .analysis_pipeline.command import AnalysisCommandService
    from .analysis_pipeline.composition import create_production_import_service
    from .analysis_pipeline.planner import AnalysisPlanner
    from .db import create_database_engine, create_session_factory
    from .storage import ContentAddressedStore
    from .web.bootstrap import BootstrapReadinessState, create_production_bootstrapper

    settings = _runtime_configuration().settings
    create_production_bootstrapper(BootstrapReadinessState()).prepare(settings)
    engine = create_database_engine(settings.database_path)
    session_factory = create_session_factory(engine)
    clock = lambda: datetime.now(UTC)
    service = create_production_import_service(
        session_factory,
        settings,
        ContentAddressedStore(settings.managed_replay_directory),
        ContentAddressedStore(settings.cache_directory / "artifacts"),
        parser=parse_replay,
        telemetry_acquirer=None,
        clock=clock,
        parser_version=__version__,
        telemetry_acquirer_version="none",
    )
    planner = AnalysisPlanner(session_factory, clock=clock)
    # TheSuperHackers @feature Leex 23/08/2026 Defer foreground worker capability until explicit execution. (#TBD)
    runtime = _LazyAnalysisRuntime(service.worker_control_port())
    return _AnalyzeApplication(AnalysisCommandService(session_factory, planner, runtime), runtime, engine)


def _analysis_exit_code(status: str) -> int:
    if status in {"planned", "succeeded"}:
        return 0
    if status in {"awaiting_observations", "incomplete", "claim_limit_reached"}:
        return 3
    return 5


def _write_analysis_result(result: AnalysisCommandResult, *, json_output: bool) -> None:
    if json_output:
        _write_json_document(asdict(result))
        return
    print(
        f"replay {result.replay_public_id}: {result.status}; "
        f"claims={result.claims_executed}; ollama={'enabled' if result.allow_ollama else 'disabled'}"
    )
    for job in result.jobs:
        print(f"job {job.public_id} {job.stage} {job.status} {job.job_identity_digest}")
    for report in result.reports:
        print(f"report {report.public_id} {report.input_digest} {report.cache_key}")


# TheSuperHackers @feature Leex 22/08/2026 Expose bounded replay-scoped analysis with explicit local-model consent. (#TBD)
def _run_analyze(arguments: argparse.Namespace) -> int:
    from uuid import UUID

    from .analysis_pipeline.planner import AnalysisPlanningError
    from .importing import JobLifecycleError
    from .worker import OwnedChildSettlementError

    try:
        if str(UUID(arguments.replay_public_id)) != arguments.replay_public_id:
            raise ValueError("replay public ID is not canonical")
    except (AttributeError, TypeError, ValueError):
        print("replay-analyzer: error: [invalid_analysis_request] replay analysis request is invalid", file=sys.stderr)
        return 2

    try:
        application = _analyze_application()
    except Exception:  # noqa: BLE001 - public CLI boundary must never expose private tracebacks.
        print("replay-analyzer: error: [analysis_initialization_failed] analysis is unavailable", file=sys.stderr)
        return 5
    result: AnalysisCommandResult | None = None
    failure: tuple[int, str] | None = None
    try:
        result = application.run(
            arguments.replay_public_id,
            execute=arguments.execute,
            allow_ollama=arguments.allow_ollama,
        )
    except OwnedChildSettlementError:
        failure = (5, "replay-analyzer: error: [analysis_execution_failed] analysis execution did not settle")
    except JobLifecycleError:
        failure = (5, "replay-analyzer: error: [analysis_execution_failed] analysis execution failed")
    except AnalysisPlanningError as error:
        if error.code == "unknown_replay":
            failure = (2, "replay-analyzer: error: [invalid_analysis_request] replay analysis request is invalid")
        else:
            failure = (5, "replay-analyzer: error: [analysis_execution_failed] analysis execution failed")
    except Exception:  # noqa: BLE001 - public CLI boundary translates unknown application faults.
        failure = (5, "replay-analyzer: error: [analysis_execution_failed] analysis execution failed")
    try:
        application.close()
    except Exception:  # noqa: BLE001 - cleanup failures must become a stable public exit.
        print("replay-analyzer: error: [analysis_cleanup_failed] analysis cleanup failed", file=sys.stderr)
        return 5
    if failure is not None:
        print(failure[1], file=sys.stderr)
        return failure[0]
    assert result is not None
    _write_analysis_result(result, json_output=arguments.json_output)
    return _analysis_exit_code(result.status)


# TheSuperHackers @feature Leex 19/08/2026 Expose deterministic observed replay inspection without LLM or network calls. (#TBD)
def main(argv: Sequence[str] | None = None) -> int:
    """Run the inspection CLI and return a deterministic process status for replay failures."""
    arguments = _parser().parse_args(argv)
    if arguments.command == "export-telemetry":
        return _run_export(arguments)
    if arguments.command == "import":
        try:
            return _run_import(arguments)
        except (OSError, ValueError) as error:
            print(f"replay-analyzer: error: [invalid_import] {error}", file=sys.stderr)
            return 2
    if arguments.command == "jobs":
        return _run_jobs(arguments)
    if arguments.command == "web":
        try:
            return _run_web(arguments)
        except ValueError as error:
            print(f"replay-analyzer: error: [invalid_web_bind] {error}", file=sys.stderr)
            return 2
    if arguments.command == "worker":
        try:
            return _run_worker(arguments)
        except ValueError as error:
            print(f"replay-analyzer: error: [invalid_worker_options] {error}", file=sys.stderr)
            return 2
    if arguments.command == "analyze":
        return _run_analyze(arguments)
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
