"""Isolated no-shell launcher and evidence validator for headless replay telemetry."""

import ctypes
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast
from uuid import UUID, uuid4

from generals_replay_analyzer.engine.config import (
    EngineRunConfig,
    EngineRunConfigurationError,
    require_no_reparse_components,
    require_regular_input,
)
from generals_replay_analyzer.engine.outcome import (
    ReplayOutcome,
    ReplayOutcomeValidationError,
    load_replay_outcome,
)
from generals_replay_analyzer.engine.result import (
    EngineRunResult,
    EngineRunStatus,
    ReplayQuality,
    RunDiagnostic,
    StrategyAnalysisScope,
)
from generals_replay_analyzer.telemetry.map_asset import ASSET_NAMES
from generals_replay_analyzer.telemetry.model import CompleteRecord, ManifestRecord
from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

ANSI_MAX_PATH = 260
_MAX_DIAGNOSTIC_BYTES = 4 * 1024 * 1024
_CATALOG_PREFIX = "game-data-catalog-v1-"
_CATALOG_SUFFIX = ".json"


@dataclass(frozen=True)
class ProcessLaunchRequest:
    """Complete no-shell process boundary supplied to an injected launcher."""

    run_id: str
    run_dir: Path
    argv: tuple[str, ...]
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    stdout_handle: BinaryIO
    stderr_handle: BinaryIO
    timeout_seconds: int
    shell: Literal[False] = False


@dataclass(frozen=True)
class ProcessExecution:
    """Process facts returned only after normal exit or complete tree termination."""

    exit_code: int
    timed_out: bool
    duration_seconds: float
    process_tree_terminated: bool
    termination_method: str | None


class ProcessLauncher(Protocol):
    """Injected external-process seam used by unit tests and the production launcher."""

    def __call__(self, request: ProcessLaunchRequest) -> ProcessExecution: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _input_identity_changed(path: Path, label: str, expected_sha256: str, expected_size: int) -> bool:
    try:
        current = require_regular_input(path, label)
        return current.stat().st_size != expected_size or _sha256_file(current) != expected_sha256
    except (EngineRunConfigurationError, OSError):
        return True


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _require_plain_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise EngineRunConfigurationError(f"{label} cannot be inspected: {path}: {error}") from error
    if path.is_symlink() or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise EngineRunConfigurationError(f"{label} must be an ordinary non-reparse directory: {path}")
    return info


def _ensure_plain_directory(path: Path) -> None:
    """Create missing directory components without accepting an alias at any boundary."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    require_no_reparse_components(cursor, "data root")
    _require_plain_directory(cursor, "data root ancestor")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        _require_plain_directory(directory, "data root component")
    require_no_reparse_components(path, "data root")


def _canonical_run_id(value: object) -> str:
    if type(value) is not str:
        raise EngineRunConfigurationError("run ID factory must return a canonical lowercase UUID string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise EngineRunConfigurationError("run ID factory returned an unsafe run ID") from error
    if str(parsed) != value:
        raise EngineRunConfigurationError("run ID factory must return the canonical lowercase UUID spelling")
    return value


def _ansi_path_bytes(path: Path) -> bytes:
    """Encode one engine-bound path without active-code-page substitution or best-fit aliases."""
    if os.name != "nt":
        return os.fsencode(path)
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetACP.argtypes = []
    kernel32.GetACP.restype = wintypes.UINT
    kernel32.WideCharToMultiByte.argtypes = [
        wintypes.UINT,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.WideCharToMultiByte.restype = ctypes.c_int
    code_page = int(kernel32.GetACP())
    utf8 = code_page == 65001
    flags = 0x00000080 if utf8 else 0x00000400  # WC_ERR_INVALID_CHARS or WC_NO_BEST_FIT_CHARS
    used_default = wintypes.BOOL(False)
    used_default_pointer = None if utf8 else ctypes.byref(used_default)
    source = str(path)
    required = kernel32.WideCharToMultiByte(
        code_page, flags, source, len(source), None, 0, None, used_default_pointer
    )
    if required <= 0 or used_default.value:
        error = ctypes.get_last_error()
        detail = f"Win32 error {error}" if error else "active-code-page substitution"
        raise EngineRunConfigurationError(f"engine-bound path is not exactly ANSI encodable ({detail}): {path}")
    buffer = (ctypes.c_char * required)()
    used_default = wintypes.BOOL(False)
    used_default_pointer = None if utf8 else ctypes.byref(used_default)
    written = kernel32.WideCharToMultiByte(
        code_page, flags, source, len(source), buffer, required, None, used_default_pointer
    )
    if written != required or used_default.value:
        raise EngineRunConfigurationError(f"engine-bound path is not exactly ANSI encodable: {path}")
    return bytes(buffer[:written])


def _preflight_ansi_paths(
    run_dir: Path,
    executable: Path,
    replay: Path,
    replay_user_data_root: Path | None,
) -> None:
    """Fail before launch when any actual engine-bound ANSI path cannot fit exactly."""
    hash_name = "f" * 64
    transaction_suffix = ".tmp.4294967295.100"
    candidates = [
        ("engine executable", executable),
        ("engine working directory", executable.parent),
        ("replay input", replay),
        ("telemetry transaction", run_dir / "trace.ndjson.tmp.4294967295.100"),
        (
            "catalog transaction",
            run_dir / (f"{_CATALOG_PREFIX}{hash_name}{_CATALOG_SUFFIX}{transaction_suffix}"),
        ),
        (
            "map transaction",
            run_dir / "map-assets-v1" / f"{hash_name}{transaction_suffix}" / "pathing-amphibious.u8.zlib",
        ),
        ("outcome transaction", run_dir / "replay-outcome.json.tmp.4294967295.100"),
    ]
    if replay_user_data_root is not None:
        candidates.extend(
            (
                ("replay user-data root", replay_user_data_root),
                ("replay user map cache", replay_user_data_root / "Maps" / "MapCache.ini"),
            )
        )
    for label, candidate in candidates:
        byte_length = len(_ansi_path_bytes(candidate))
        if byte_length >= ANSI_MAX_PATH:
            raise EngineRunConfigurationError(
                f"{label} cannot fit below ANSI MAX_PATH ({byte_length} >= {ANSI_MAX_PATH} bytes): {candidate}"
            )


def _write_atomic_json(path: Path, document: dict[str, object]) -> str:
    """Publish canonical metadata atomically and without replacing caller-owned bytes."""
    raw = (json.dumps(document, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(f".{path.name}.tmp")
    owns_temporary = False
    try:
        with temporary.open("xb") as output:
            owns_temporary = True
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        temporary.unlink()
    except BaseException:
        if owns_temporary and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
        raise
    return hashlib.sha256(raw).hexdigest()


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        getattr(info, "st_file_attributes", 0),
    )


def _validated_runner_log(
    path: Path,
    expected_identity: tuple[int, int, int, int, int, int] | None,
) -> tuple[Path | None, str | None]:
    if expected_identity is None:
        return None, f"runner never acquired an exclusive handle for {path.name}"
    try:
        _require_plain_output_file(path, path.name)
        current = _file_identity(path.lstat())
    except (OSError, ValueError) as error:
        return None, str(error)
    if current[:4] != expected_identity[:4] or current[5] != expected_identity[5]:
        return None, f"runner-owned log identity changed after launch: {path.name}"
    return path, None


def _read_diagnostic(path: Path) -> str:
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_DIAGNOSTIC_BYTES + 1)
    except OSError:
        return ""
    if len(raw) > _MAX_DIAGNOSTIC_BYTES:
        raw = raw[:_MAX_DIAGNOSTIC_BYTES]
    return raw.decode("utf-8", errors="replace")


def _require_plain_output_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} cannot be inspected: {error}") from error
    if path.is_symlink() or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is not an ordinary non-reparse file")
    if info.st_nlink != 1:
        raise ValueError(f"{label} is a hardlinked output")


def _looks_like_catalog(name: str) -> bool:
    digest = name.removeprefix(_CATALOG_PREFIX).removesuffix(_CATALOG_SUFFIX)
    return (
        name.startswith(_CATALOG_PREFIX)
        and name.endswith(_CATALOG_SUFFIX)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _preflight_output_layout(run_dir: Path, request_sha256: str) -> tuple[Path, ...]:
    """Reject aliases, hardlinks, transactions, and files outside the runner/engine contract."""
    _require_plain_directory(run_dir, "run directory")
    allowed_fixed = {
        "request.json",
        "stdout.log",
        "stderr.log",
        "trace.ndjson",
        "replay-outcome.json",
        "map-assets-v1",
    }
    catalogs: list[Path] = []
    seen: set[str] = set()
    for entry in run_dir.iterdir():
        seen.add(entry.name)
        if _looks_like_catalog(entry.name):
            catalogs.append(entry)
            _require_plain_output_file(entry, "game-data catalog")
            continue
        if entry.name not in allowed_fixed:
            raise ValueError(f"unexpected run output: {entry.name}")
        if entry.name == "map-assets-v1":
            try:
                info = entry.lstat()
            except OSError as error:
                raise ValueError(f"map asset root cannot be inspected: {error}") from error
            if entry.is_symlink() or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("map asset root is not an ordinary non-reparse directory")
        else:
            _require_plain_output_file(entry, entry.name)
    if len(catalogs) > 1:
        raise ValueError("multiple game-data catalog outputs are present")
    missing_runner_files = {"request.json", "stdout.log", "stderr.log"} - seen
    if missing_runner_files:
        raise ValueError(f"runner-owned output is missing: {min(missing_runner_files)}")
    request_path = run_dir / "request.json"
    if not request_path.is_file() or _sha256_file(request_path) != request_sha256:
        raise ValueError("request metadata changed after launch")
    return tuple(catalogs)


def _quality_for(status: EngineRunStatus, validated_trace: bool) -> tuple[ReplayQuality, StrategyAnalysisScope]:
    if status is EngineRunStatus.SUCCESS:
        return ReplayQuality.ENGINE_VERIFIED, StrategyAnalysisScope.FULL_MATCH
    if status in {
        EngineRunStatus.VALID_CRC_MISMATCH,
        EngineRunStatus.REPLAY_TRUNCATED,
        EngineRunStatus.INTERRUPTED,
    } and validated_trace:
        return ReplayQuality.PARTIAL, StrategyAnalysisScope.OBSERVED_BOUNDARY_ONLY
    return ReplayQuality.FAILED, StrategyAnalysisScope.NONE


def _result_document(
    result: EngineRunResult,
    execution: ProcessExecution | None,
    request_sha256: str,
    started_at: str,
) -> dict[str, object]:
    document = result.to_public_dict()
    document.update(
        {
            "schema_version": 1,
            "type": "engine_run_result",
            "request_sha256": request_sha256,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "process": {
                "process_tree_terminated": execution.process_tree_terminated if execution is not None else False,
                "termination_method": execution.termination_method if execution is not None else None,
            },
        }
    )
    return document


def _finish_result(
    *,
    run_id: str,
    run_dir: Path,
    stdout_path: Path | None,
    stderr_path: Path | None,
    execution: ProcessExecution | None,
    status: EngineRunStatus,
    diagnostics: list[RunDiagnostic],
    request_sha256: str,
    started_at: str,
    trace_path: Path | None = None,
    catalog_path: Path | None = None,
    map_assets: tuple[Path, ...] = (),
    outcome_path: Path | None = None,
) -> EngineRunResult:
    validated_trace = trace_path is not None
    quality, strategy_scope = _quality_for(status, validated_trace)
    result = EngineRunResult(
        run_id=run_id,
        run_dir=run_dir,
        trace_path=trace_path,
        catalog_path=catalog_path,
        map_assets=map_assets,
        outcome_path=outcome_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        exit_code=execution.exit_code if execution is not None else None,
        status=status,
        duration_seconds=execution.duration_seconds if execution is not None else 0.0,
        replay_quality=quality,
        strategy_analysis_scope=strategy_scope,
        diagnostics=tuple(diagnostics),
    )
    document = _result_document(result, execution, request_sha256, started_at)
    try:
        _write_atomic_json(run_dir / "result.json", document)
    except OSError as primary_error:
        diagnostic = RunDiagnostic(
            "result_metadata_collision",
            f"fixed result metadata name was unavailable and a runner-owned fallback was used: {primary_error}",
        )
        result = replace(result, diagnostics=(*result.diagnostics, diagnostic))
        document = _result_document(result, execution, request_sha256, started_at)
        for index in range(100):
            fallback = run_dir / f"runner-result-{index}.json"
            try:
                _write_atomic_json(fallback, document)
                break
            except FileExistsError:
                continue
            except OSError as fallback_error:
                raise EngineRunConfigurationError(
                    f"could not atomically publish fallback result metadata: {fallback_error}"
                ) from fallback_error
        else:
            raise EngineRunConfigurationError(
                "could not atomically publish result metadata after 100 exclusive fallback attempts"
            ) from primary_error
    return result


def _cross_bind_outcome(outcome: ReplayOutcome, complete: CompleteRecord) -> str | None:
    trace_facts = (
        complete.payload.final_frame,
        complete.payload.command_count,
        complete.payload.terminal_reason,
        complete.payload.crc_mismatch,
        complete.payload.crc_mismatch_frame,
    )
    outcome_facts = (
        outcome.final_frame,
        outcome.command_count,
        outcome.terminal_reason,
        outcome.crc_mismatch,
        outcome.crc_mismatch_frame,
    )
    if trace_facts != outcome_facts:
        return "independent replay outcome differs from telemetry completion facts"
    return None


def _validated_asset_paths(run_dir: Path, manifest: ManifestRecord) -> tuple[Path, tuple[Path, ...]]:
    catalog_reference = manifest.payload.game_data_catalog
    map_reference = manifest.payload.map_asset
    if catalog_reference is None or map_reference is None:
        raise ValueError("validated v2 manifest did not expose mandatory assets")
    catalog_path = run_dir / catalog_reference.path
    _require_plain_output_file(catalog_path, "game-data catalog")
    map_manifest = run_dir / Path(*map_reference.path.split("/"))
    map_directory = map_manifest.parent
    map_root = run_dir / "map-assets-v1"
    map_outputs = tuple(map_root.iterdir())
    if len(map_outputs) != 1 or map_outputs[0] != map_directory:
        raise ValueError("map asset root contains an unexpected output or transaction")
    require_no_reparse_components(map_directory, "map asset directory")
    _require_plain_directory(map_directory, "map asset directory")
    map_paths = tuple(map_directory / name for name in sorted(ASSET_NAMES))
    for path in map_paths:
        _require_plain_output_file(path, f"map asset {path.name}")
    return catalog_path, map_paths


def _startup_status(outcome: ReplayOutcome) -> EngineRunStatus | None:
    return {
        "input_unavailable": EngineRunStatus.INPUT_UNAVAILABLE,
        "invalid_replay_header": EngineRunStatus.INVALID_REPLAY_HEADER,
        "truncated_input": EngineRunStatus.TRUNCATED_INPUT,
    }.get(outcome.terminal_reason)


# TheSuperHackers @feature Leex 21/08/2026 Isolate engine outputs and expose only cross-validated replay evidence. (#TBD)
def export_telemetry(
    replay: Path,
    config: EngineRunConfig,
    *,
    launcher: ProcessLauncher | None = None,
    run_id_factory: Callable[[], str] = lambda: str(uuid4()),
) -> EngineRunResult:
    """Launch one replay in a never-reused run directory and validate all evidence atomically."""
    if type(config) is not EngineRunConfig:
        raise EngineRunConfigurationError("config must be an EngineRunConfig")
    replay = require_regular_input(replay, "replay input")
    run_id = _canonical_run_id(run_id_factory())
    run_dir = config.data_root / "runs" / run_id
    _preflight_ansi_paths(run_dir, config.executable, replay, config.replay_user_data_root)
    _ensure_plain_directory(config.data_root)
    runs_root = config.data_root / "runs"
    _ensure_plain_directory(runs_root)
    try:
        run_dir.mkdir()
    except FileExistsError as error:
        raise EngineRunConfigurationError(f"run directory already exists and will not be reused: {run_dir}") from error
    _require_plain_directory(run_dir, "run directory")

    trace_path = run_dir / "trace.ndjson"
    outcome_path = run_dir / "replay-outcome.json"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    argv_parts = [
        str(config.executable),
        "-headless",
        "-noaudio",
        "-replay",
        str(replay),
    ]
    if config.replay_user_data_root is not None:
        argv_parts.extend(("-replay-user-data-root", str(config.replay_user_data_root)))
    argv_parts.extend((
        "-telemetry",
        str(trace_path),
        "-telemetry-run-id",
        run_id,
        "-telemetry-movement-frames",
        str(config.movement_sample_frames),
        "-replay-outcome",
        str(outcome_path),
    ))
    argv = tuple(argv_parts)
    replay_sha256 = _sha256_file(replay)
    replay_size = replay.stat().st_size
    executable_sha256 = _sha256_file(config.executable)
    executable_size = config.executable.stat().st_size
    started_at = _utc_now()
    request_document: dict[str, object] = {
        "schema_version": 1,
        "type": "engine_run_request",
        "run_id": run_id,
        "requested_at": started_at,
        "replay": {"path": str(replay), "sha256": replay_sha256, "size": replay_size},
        "engine": {
            "path": str(config.executable),
            "sha256": executable_sha256,
            "size": executable_size,
            "version": None,
        },
        "config": {
            "timeout_seconds": config.timeout_seconds,
            "movement_sample_frames": config.movement_sample_frames,
            "data_root": str(config.data_root),
            "replay_user_data_root": (
                str(config.replay_user_data_root) if config.replay_user_data_root is not None else None
            ),
        },
        "argv": list(argv),
        "cwd": str(config.executable.parent),
        "shell": False,
    }
    try:
        request_sha256 = _write_atomic_json(run_dir / "request.json", request_document)
    except OSError as error:
        raise EngineRunConfigurationError(f"could not atomically publish request metadata: {error}") from error
    selected_launcher = default_process_launcher if launcher is None else launcher
    stdout_identity: tuple[int, int, int, int, int, int] | None = None
    stderr_identity: tuple[int, int, int, int, int, int] | None = None
    try:
        with stdout_path.open("xb", buffering=0) as stdout_handle:
            stdout_identity = _file_identity(os.fstat(stdout_handle.fileno()))
            with stderr_path.open("xb", buffering=0) as stderr_handle:
                stderr_identity = _file_identity(os.fstat(stderr_handle.fileno()))
                launch_request = ProcessLaunchRequest(
                    run_id=run_id,
                    run_dir=run_dir,
                    argv=argv,
                    cwd=config.executable.parent,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    stdout_handle=stdout_handle,
                    stderr_handle=stderr_handle,
                    timeout_seconds=config.timeout_seconds,
                )
                execution = selected_launcher(launch_request)
        if (
            type(execution) is not ProcessExecution
            or type(execution.exit_code) is not int
            or type(execution.timed_out) is not bool
            or not math.isfinite(execution.duration_seconds)
            or execution.duration_seconds < 0
            or (execution.timed_out and not execution.process_tree_terminated)
        ):
            raise TypeError("process launcher returned an invalid ProcessExecution")
    except Exception as error:  # noqa: BLE001 - the injected process boundary must become a typed run result.
        launch_diagnostics = [
            RunDiagnostic("launch_failure", f"engine launch failed: {type(error).__name__}: {error}")
        ]
        public_stdout, stdout_error = _validated_runner_log(stdout_path, stdout_identity)
        public_stderr, stderr_error = _validated_runner_log(stderr_path, stderr_identity)
        for log_error in (stdout_error, stderr_error):
            if log_error is not None:
                launch_diagnostics.append(RunDiagnostic("unsafe_log", log_error))
        return _finish_result(
            run_id=run_id,
            run_dir=run_dir,
            stdout_path=public_stdout,
            stderr_path=public_stderr,
            execution=None,
            status=EngineRunStatus.LAUNCH_FAILURE,
            diagnostics=launch_diagnostics,
            request_sha256=request_sha256,
            started_at=started_at,
        )

    diagnostics: list[RunDiagnostic] = []
    public_stdout, stdout_error = _validated_runner_log(stdout_path, stdout_identity)
    public_stderr, stderr_error = _validated_runner_log(stderr_path, stderr_identity)
    log_errors = tuple(error for error in (stdout_error, stderr_error) if error is not None)
    if log_errors:
        diagnostics.extend(RunDiagnostic("unsafe_log", error) for error in log_errors)
        status = EngineRunStatus.TIMEOUT if execution.timed_out else EngineRunStatus.UNSAFE_OUTPUT
        if execution.timed_out:
            diagnostics.append(RunDiagnostic("timeout", f"engine exceeded {config.timeout_seconds} seconds"))
        return _finish_result(
            run_id=run_id,
            run_dir=run_dir,
            stdout_path=public_stdout,
            stderr_path=public_stderr,
            execution=execution,
            status=status,
            diagnostics=diagnostics,
            request_sha256=request_sha256,
            started_at=started_at,
        )
    assert public_stdout is not None and public_stderr is not None
    stdout_path = public_stdout
    stderr_path = public_stderr
    replay_changed = _input_identity_changed(replay, "replay input", replay_sha256, replay_size)
    engine_changed = _input_identity_changed(
        config.executable, "engine executable", executable_sha256, executable_size
    )
    if replay_changed or engine_changed:
        changed_inputs = " and ".join(
            label for label, changed in (("replay", replay_changed), ("engine", engine_changed)) if changed
        )
        diagnostics.append(RunDiagnostic("input_changed", f"{changed_inputs} bytes changed during engine execution"))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.INPUT_CHANGED, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at,
        )
    if execution.timed_out:
        diagnostics.append(RunDiagnostic("timeout", f"engine exceeded {config.timeout_seconds} seconds"))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.TIMEOUT, diagnostics=diagnostics, request_sha256=request_sha256, started_at=started_at,
        )

    try:
        catalog_candidates = _preflight_output_layout(run_dir, request_sha256)
    except (OSError, ValueError, EngineRunConfigurationError) as error:
        diagnostics.append(RunDiagnostic("unsafe_output", str(error)))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.UNSAFE_OUTPUT, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at,
        )

    if not outcome_path.is_file():
        stderr_text = _read_diagnostic(stderr_path)
        if "ReplayOutcome:" in stderr_text or "ReplayTelemetry:" in stderr_text:
            diagnostics.append(RunDiagnostic("writer_error", "engine reported an outcome or telemetry writer failure"))
            status = EngineRunStatus.WRITER_ERROR
        elif execution.exit_code != 0 and not trace_path.exists():
            diagnostics.append(RunDiagnostic("engine_failure", f"engine exited with code {execution.exit_code}"))
            status = EngineRunStatus.ENGINE_FAILURE
        else:
            diagnostics.append(RunDiagnostic("missing_outcome", "engine did not publish replay-outcome.json"))
            status = EngineRunStatus.MISSING_OUTCOME
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=status, diagnostics=diagnostics, request_sha256=request_sha256, started_at=started_at,
        )
    try:
        outcome = load_replay_outcome(outcome_path)
    except ReplayOutcomeValidationError as error:
        diagnostics.append(RunDiagnostic("invalid_outcome", str(error)))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.INVALID_OUTCOME, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at,
        )

    startup_status = _startup_status(outcome)
    if startup_status is not None:
        playback_artifacts = trace_path.exists() or bool(catalog_candidates) or (run_dir / "map-assets-v1").exists()
        if playback_artifacts:
            diagnostics.append(
                RunDiagnostic(
                    "outcome_mismatch",
                    "pre-playback outcome contradicts published telemetry, catalog, or map artifacts",
                )
            )
            return _finish_result(
                run_id=run_id,
                run_dir=run_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                execution=execution,
                status=EngineRunStatus.OUTCOME_MISMATCH,
                diagnostics=diagnostics,
                request_sha256=request_sha256,
                started_at=started_at,
                outcome_path=outcome_path,
            )
        diagnostics.append(RunDiagnostic(startup_status.value, f"engine reported {outcome.terminal_reason} before playback"))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=startup_status, diagnostics=diagnostics, request_sha256=request_sha256, started_at=started_at,
            outcome_path=outcome_path,
        )
    if not trace_path.is_file():
        diagnostics.append(RunDiagnostic("missing_trace", "playback started but engine did not publish trace.ndjson"))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.MISSING_TRACE, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at, outcome_path=outcome_path,
        )
    try:
        _require_plain_output_file(trace_path, "telemetry trace")
        records = tuple(iter_validated_trace(trace_path))
    except (TelemetryTraceValidationError, OSError, ValueError) as error:
        message = str(error)
        asset_markers = ("catalog", "map_asset", "map asset", "game-data")
        status = EngineRunStatus.ASSET_INVALID if any(marker in message for marker in asset_markers) else EngineRunStatus.INVALID_TRACE
        diagnostics.append(RunDiagnostic(status.value, message))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=status, diagnostics=diagnostics, request_sha256=request_sha256, started_at=started_at,
            outcome_path=outcome_path,
        )
    manifest = cast(ManifestRecord, records[0])
    complete = cast(CompleteRecord, records[-1])
    if str(manifest.run_id) != run_id:
        diagnostics.append(RunDiagnostic("invalid_trace", "trace run_id differs from isolated run request"))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.INVALID_TRACE, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at, outcome_path=outcome_path,
        )
    if manifest.payload.exporter_settings.get("movement_sample_frames") != config.movement_sample_frames:
        diagnostics.append(RunDiagnostic("invalid_trace", "trace movement interval differs from requested configuration"))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.INVALID_TRACE, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at, outcome_path=outcome_path,
        )
    if complete.payload.writer_error is not None:
        diagnostics.append(RunDiagnostic("writer_error", complete.payload.writer_error))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.WRITER_ERROR, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at, outcome_path=outcome_path,
        )
    mismatch = _cross_bind_outcome(outcome, complete)
    if mismatch is not None:
        diagnostics.append(RunDiagnostic("outcome_mismatch", mismatch))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.OUTCOME_MISMATCH, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at, outcome_path=outcome_path,
        )
    try:
        catalog_path, map_assets = _validated_asset_paths(run_dir, manifest)
    except (OSError, ValueError) as error:
        diagnostics.append(RunDiagnostic("asset_invalid", str(error)))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.ASSET_INVALID, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at, outcome_path=outcome_path,
        )
    if catalog_candidates != (catalog_path,):
        diagnostics.append(RunDiagnostic("asset_invalid", "validated catalog differs from the sole run output catalog"))
        return _finish_result(
            run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
            status=EngineRunStatus.ASSET_INVALID, diagnostics=diagnostics, request_sha256=request_sha256,
            started_at=started_at, outcome_path=outcome_path,
        )

    if outcome.terminal_reason == "crc_mismatch":
        status = EngineRunStatus.VALID_CRC_MISMATCH
    elif outcome.terminal_reason == "replay_truncated":
        status = EngineRunStatus.REPLAY_TRUNCATED
    elif outcome.terminal_reason == "interrupted":
        status = EngineRunStatus.INTERRUPTED
    elif execution.exit_code != 0:
        status = EngineRunStatus.ENGINE_FAILURE
        diagnostics.append(RunDiagnostic("engine_failure", f"engine exited with code {execution.exit_code}"))
    else:
        status = EngineRunStatus.SUCCESS
    return _finish_result(
        run_id=run_id, run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path, execution=execution,
        status=status, diagnostics=diagnostics, request_sha256=request_sha256, started_at=started_at,
        trace_path=trace_path, catalog_path=catalog_path, map_assets=map_assets, outcome_path=outcome_path,
    )


def _posix_process_launcher(request: ProcessLaunchRequest) -> ProcessExecution:
    started = time.monotonic()
    timed_out = False
    tree_terminated = False
    termination_method: str | None = None
    process = subprocess.Popen(
        list(request.argv),
        cwd=request.cwd,
        stdout=request.stdout_handle,
        stderr=request.stderr_handle,
        stdin=subprocess.DEVNULL,
        shell=False,
        start_new_session=True,
    )
    try:
        exit_code = process.wait(timeout=request.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        exit_code = process.wait()
        tree_terminated = True
        termination_method = "posix_process_group"
    return ProcessExecution(exit_code, timed_out, time.monotonic() - started, tree_terminated, termination_method)


# TheSuperHackers @feature Leex 21/08/2026 Contain every Windows engine child in a kill-on-close Job Object. (#TBD)
def _windows_process_launcher(request: ProcessLaunchRequest) -> ProcessExecution:
    """Create suspended, assign to a kill-on-close Job Object, then start the engine."""
    import msvcrt
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)), ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process_info = PROCESS_INFORMATION()
    process_created = False
    job_assigned = False
    stdin: BinaryIO | None = None
    inherited_handles: list[int] = []
    started = time.monotonic()

    def wait_for(handle: int, timeout_milliseconds: int, label: str) -> None:
        wait_result = kernel32.WaitForSingleObject(handle, timeout_milliseconds)
        if wait_result != 0x00000000:
            if wait_result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            raise OSError(f"{label} did not settle (WaitForSingleObject={int(wait_result)})")

    def active_job_processes() -> int:
        accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        returned_length = wintypes.DWORD()
        if not kernel32.QueryInformationJobObject(
            job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), ctypes.byref(returned_length)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses)

    def wait_for_job_empty() -> None:
        deadline = time.monotonic() + 10.0
        while active_job_processes() != 0:
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows Job Object did not reach zero active processes after termination")
            time.sleep(0.01)

    def terminate_and_wait_for_job() -> None:
        if not kernel32.TerminateJobObject(job, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        wait_for_job_empty()

    try:
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        stdin = open(os.devnull, "rb", buffering=0)  # noqa: SIM115 - closed in finally.
        inherited_handles = [
            msvcrt.get_osfhandle(stdin.fileno()),
            msvcrt.get_osfhandle(request.stdout_handle.fileno()),
            msvcrt.get_osfhandle(request.stderr_handle.fileno()),
        ]
        for handle in inherited_handles:
            os.set_handle_inheritable(handle, True)
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = inherited_handles
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(request.argv))
        created = kernel32.CreateProcessW(
            str(request.argv[0]), command_line, None, None, True,
            0x00000004 | 0x08000000,  # CREATE_SUSPENDED | CREATE_NO_WINDOW
            None, str(request.cwd), ctypes.byref(startup), ctypes.byref(process_info),
        )
        if not created:
            raise ctypes.WinError(ctypes.get_last_error())
        process_created = True
        for handle in inherited_handles:
            os.set_handle_inheritable(handle, False)
        if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
            raise ctypes.WinError(ctypes.get_last_error())
        job_assigned = True
        if kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        wait_result = kernel32.WaitForSingleObject(process_info.hProcess, request.timeout_seconds * 1000)
        timed_out = wait_result == 0x00000102
        tree_terminated = False
        termination_method: str | None = None
        if timed_out:
            terminate_and_wait_for_job()
            tree_terminated = True
            termination_method = "windows_job_object"
        elif wait_result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        elif active_job_processes() != 0:
            terminate_and_wait_for_job()
            tree_terminated = True
            termination_method = "windows_job_object_cleanup"
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ProcessExecution(
            int(exit_code.value), timed_out, time.monotonic() - started, tree_terminated, termination_method
        )
    except Exception:
        if process_created:
            if job_assigned:
                terminate_and_wait_for_job()
            else:
                # The process is still suspended here, so it cannot have spawned descendants.
                if not kernel32.TerminateProcess(process_info.hProcess, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                wait_for(process_info.hProcess, 0xFFFFFFFF, "suspended engine process")
        raise
    finally:
        for handle in inherited_handles:
            try:
                os.set_handle_inheritable(handle, False)
            except OSError:
                pass
        if stdin is not None:
            stdin.close()
        if process_info.hThread:
            kernel32.CloseHandle(process_info.hThread)
        if process_info.hProcess:
            kernel32.CloseHandle(process_info.hProcess)
        kernel32.CloseHandle(job)


def default_process_launcher(request: ProcessLaunchRequest) -> ProcessExecution:
    """Launch explicit argv without a shell and enforce whole-tree timeout termination."""
    if request.shell is not False:
        raise ValueError("shell launch is forbidden")
    if os.name == "nt":
        return _windows_process_launcher(request)
    return _posix_process_launcher(request)
