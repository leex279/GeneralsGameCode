"""External durable-job worker and owned stage supervisor."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock, Thread
from types import FrameType
from typing import NoReturn, Protocol
from uuid import uuid4

from generals_replay_analyzer.engine.runner import ProcessTreeSettledInterruption
from generals_replay_analyzer.importing import (
    JobLifecycleError,
    OwnedExecutionSettlementDTO,
    StageExecutionOutcomeDTO,
    WorkerControlPort,
)
from generals_replay_analyzer.importing.job_contracts import (
    DEFAULT_JOB_CLAIM_SELECTOR,
    JobClaimSelectorDTO,
)

LOGGER = logging.getLogger(__name__)
_POSIX_TREE_SETTLED_EXIT_CODE = 75


class _StageTerminationRequested(BaseException):
    """Unwind a POSIX stage through its runner-owned child-group cleanup."""


def _raise_stage_termination(_signal_number: int, _frame: FrameType | None) -> NoReturn:
    raise _StageTerminationRequested("owned stage termination requested")


@contextmanager
def _posix_stage_termination_handler() -> Iterator[None]:
    if os.name == "nt":
        yield
        return
    previous = signal.signal(signal.SIGTERM, _raise_stage_termination)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class OwnedChildSettlementError(RuntimeError):
    """The worker could not prove that its private execution tree stopped."""


def validate_worker_options(poll_seconds: int, lease_seconds: int) -> tuple[int, int]:
    if type(poll_seconds) is not int or not 1 <= poll_seconds <= 300:
        raise ValueError("poll-seconds must be between 1 and 300")
    if type(lease_seconds) is not int or not 15 <= lease_seconds <= 3600:
        raise ValueError("lease-seconds must be between 15 and 3600")
    if lease_seconds < poll_seconds * 3:
        raise ValueError("lease-seconds must be at least three poll intervals")
    return poll_seconds, lease_seconds


class InterruptibleWaiter(Protocol):
    def wait(self, seconds: int) -> bool: ...


class OwnedSupervisor(Protocol):
    def poll(self) -> StageExecutionOutcomeDTO | None: ...

    def terminate(self) -> OwnedExecutionSettlementDTO: ...


class SupervisorFactory(Protocol):
    def start(self, job_public_id: str, execution_public_id: str) -> OwnedSupervisor: ...


class WatchSchedulerPort(Protocol):
    def scan_once(self) -> tuple[str, ...]: ...


# TheSuperHackers @fix Leex 22/08/2026 Bound discovery work away from lease-heartbeat timing. (#TBD)
class _BackgroundWatchScanner:
    """Bound watched-root discovery to one daemon thread without delaying leases."""

    def __init__(self, watcher: WatchSchedulerPort) -> None:
        self._watcher = watcher
        self._lock = Lock()
        self._thread: Thread | None = None
        self._closed = False

    def request_scan(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._scan_once,
                name="replay-watched-root-scan",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread is not None:
            thread.join()

    def _scan_once(self) -> None:
        try:
            self._watcher.scan_once()
        except (OSError, RuntimeError, ValueError):
            LOGGER.warning("watched replay scan failed code=watched_scheduler_failed")


class EventWaiter:
    def __init__(self) -> None:
        self._stop = Event()

    def request_stop(self, *_ignored: object) -> None:
        self._stop.set()

    def wait(self, seconds: int) -> bool:
        return self._stop.wait(seconds)


# TheSuperHackers @feature Leex 22/08/2026 Keep claiming, heartbeat, cancellation, and settlement outside FastAPI. (#TBD)
class WorkerRuntime:
    """Orchestrate one claimed job at a time through owner-checked lifecycle ports."""

    def __init__(
        self,
        *,
        control: WorkerControlPort,
        supervisors: SupervisorFactory,
        waiter: InterruptibleWaiter,
        worker_public_id: str,
        poll_seconds: int,
        lease_seconds: int,
        watcher: WatchSchedulerPort | None = None,
    ) -> None:
        validate_worker_options(poll_seconds, lease_seconds)
        from uuid import UUID

        if str(UUID(worker_public_id)) != worker_public_id:
            raise ValueError("worker_public_id must be a lowercase hyphenated UUID")
        self.control = control
        self.supervisors = supervisors
        self.waiter = waiter
        self.worker_public_id = worker_public_id
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.watcher = watcher
        self._watch_scanner = _BackgroundWatchScanner(watcher) if watcher is not None else None
        self.stop_requested = False

    def _scan_watcher(self) -> None:
        if self._watch_scanner is None:
            return
        self._watch_scanner.request_scan()

    def run_once(self, selector: JobClaimSelectorDTO = DEFAULT_JOB_CLAIM_SELECTOR) -> bool:
        if type(selector) is not JobClaimSelectorDTO:
            raise TypeError("selector must be an exact JobClaimSelectorDTO")
        self._scan_watcher()
        if not self.control.registered_stages():
            if selector != DEFAULT_JOB_CLAIM_SELECTOR:
                return False
            self.stop_requested = self.waiter.wait(self.poll_seconds)
            return False
        claim = self.control.claim_next(self.worker_public_id, self.lease_seconds, selector)
        if claim is None:
            if selector != DEFAULT_JOB_CLAIM_SELECTOR:
                return False
            self.stop_requested = self.waiter.wait(self.poll_seconds)
            return False
        cancellation = self.control.cancellation(self.worker_public_id, claim)
        if cancellation.requested:
            self.control.settle_cancelled(
                self.worker_public_id,
                claim,
                OwnedExecutionSettlementDTO(claim.execution_public_id, True),
            )
            return True

        supervisor = self.supervisors.start(claim.job_public_id, claim.execution_public_id)
        while True:
            cancellation = self.control.cancellation(self.worker_public_id, claim)
            if cancellation.requested:
                self.control.settle_cancelled(self.worker_public_id, claim, supervisor.terminate())
                return True
            outcome = supervisor.poll()
            if outcome is not None:
                if outcome.status == "succeeded":
                    assert outcome.result_public_id is not None
                    self.control.settle_success(self.worker_public_id, claim, outcome.result_public_id)
                else:
                    self.control.settle_failure(self.worker_public_id, claim, outcome)
                return True
            if self.waiter.wait(self.poll_seconds):
                self.stop_requested = True
                settlement = supervisor.terminate()
                self.control.release_after_shutdown(self.worker_public_id, claim, settlement)
                return True
            self._scan_watcher()
            try:
                claim = self.control.heartbeat(self.worker_public_id, claim, self.lease_seconds)
            except JobLifecycleError:
                settlement = supervisor.terminate()
                if not settlement.tree_settled:
                    self.stop_requested = True
                    raise OwnedChildSettlementError("owned_child_settlement_failed")
                return True

    def run_forever(self) -> None:
        while not self.stop_requested:
            self.run_once()

    def shutdown(self) -> None:
        """Stop accepting discovery work and settle the one in-flight scan."""
        if self._watch_scanner is not None:
            self._watch_scanner.close()


_PRIVATE_STDERR_TAIL_BYTES = 2048
_PRIVATE_STDOUT_COUNT_BYTES = 1_048_576
_PROTOCOL_DRAIN_TIMEOUT_SECONDS = 1.0


# TheSuperHackers @fix Leex 26/08/2026 Retain bounded private child protocol diagnostics without expanding public failure text. (#TBD)
def _private_protocol_diagnostics(exit_code: int, stdout: str, stderr: str) -> dict[str, object]:
    stderr_bytes = stderr.encode("utf-8", errors="replace")
    tail = stderr_bytes[-_PRIVATE_STDERR_TAIL_BYTES:]
    stderr_tail = tail.decode("utf-8", errors="replace")
    while len(stderr_tail.encode("utf-8")) > _PRIVATE_STDERR_TAIL_BYTES:
        stderr_tail = stderr_tail[1:]
    return {
        "exit_code": exit_code,
        "stdout_bytes": min(len(stdout.encode("utf-8", errors="replace")), _PRIVATE_STDOUT_COUNT_BYTES),
        "stderr_tail": stderr_tail,
    }


class SubprocessSupervisor:
    """Own exactly one private stage subprocess and its process group."""

    def __init__(self, process: subprocess.Popen[str], execution_public_id: str) -> None:
        self._process = process
        self._execution_public_id = execution_public_id
        self._outcome: StageExecutionOutcomeDTO | None = None
        self._protocol_valid: bool | None = None

    def poll(self) -> StageExecutionOutcomeDTO | None:
        if self._outcome is not None:
            return self._outcome
        exit_code = self._process.poll()
        if exit_code is None:
            return None
        # TheSuperHackers @fix Leex 26/08/2026 Bound protocol-pipe draining after child exit so inherited handles cannot stall worker settlement. (#TBD)
        try:
            stdout, stderr = self._process.communicate(timeout=_PROTOCOL_DRAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        except (OSError, subprocess.SubprocessError, ValueError):
            stdout, stderr = "", ""
        try:
            # TheSuperHackers @fix Leex 22/08/2026 Reject crashed supervisors even when stdout resembles a valid result. (#TBD)
            if exit_code != 0:
                raise ValueError("owned stage supervisor exited unsuccessfully")
            document = json.loads(stdout)
            self._outcome = StageExecutionOutcomeDTO(**document)
            self._protocol_valid = True
        except (TypeError, ValueError, json.JSONDecodeError):
            self._protocol_valid = False
            self._outcome = StageExecutionOutcomeDTO(
                "retryable_failure",
                None,
                "supervisor_protocol_failed",
                "owned stage supervisor returned no valid outcome",
                True,
                _private_protocol_diagnostics(exit_code, stdout, stderr),
            )
        return self._outcome

    def terminate(self) -> OwnedExecutionSettlementDTO:
        was_running = self._process.poll() is None
        if not was_running:
            self.poll()
            return OwnedExecutionSettlementDTO(
                self._execution_public_id,
                self._protocol_valid is True,
            )
        tree_settled = False
        if was_running:
            try:
                if os.name == "nt":
                    taskkill = subprocess.run(
                        ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                    # TheSuperHackers @fix Leex 22/08/2026 Trust Windows tree settlement only when taskkill confirms /T success. (#TBD)
                    if taskkill.returncode != 0:
                        raise subprocess.SubprocessError("taskkill tree termination failed")
                else:  # pragma: no cover - exercised on POSIX hosts
                    # TheSuperHackers @fix Leex 22/08/2026 Signal only the recorded owned session-leader PGID. (#TBD)
                    os.killpg(self._process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
                exit_code = self._process.wait(timeout=5)
                tree_settled = os.name == "nt" or exit_code == _POSIX_TREE_SETTLED_EXIT_CODE
            except (OSError, subprocess.SubprocessError):
                try:
                    if os.name != "nt":  # pragma: no cover - exercised on POSIX hosts
                        os.killpg(self._process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
                    self._process.kill()
                    self._process.wait(timeout=10)
                    tree_settled = False
                except (OSError, subprocess.SubprocessError):
                    return OwnedExecutionSettlementDTO(self._execution_public_id, False)
        return OwnedExecutionSettlementDTO(
            self._execution_public_id,
            tree_settled and self._process.poll() is not None,
        )


class SubprocessSupervisorFactory:
    def start(self, job_public_id: str, execution_public_id: str) -> SubprocessSupervisor:
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            [sys.executable, "-m", "generals_replay_analyzer.worker", "_execute", job_public_id, execution_public_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        return SubprocessSupervisor(process, execution_public_id)


def _worker_service(*, configuration_root: Path | None = None) -> tuple[object, object, object]:
    """Compose Analytics ports after exact schema verification; never migrate."""
    from datetime import UTC, datetime

    from generals_replay_analyzer import __version__
    from generals_replay_analyzer.analysis_pipeline.composition import (
        ENGINE_TELEMETRY_ACQUIRER_VERSION,
        configured_engine_telemetry_acquirer,
        create_production_import_service,
    )
    from generals_replay_analyzer.config import load_runtime_configuration
    from generals_replay_analyzer.db import create_database_engine, create_session_factory
    from generals_replay_analyzer.parser import parse_replay
    from generals_replay_analyzer.storage import ContentAddressedStore
    from generals_replay_analyzer.web.bootstrap import PackageMigrationAdapter, SchemaIdentityStore

    settings = load_runtime_configuration(
        configuration_root=configuration_root,
        version_identities=(("analyzer", __version__),),
    ).settings
    migrations = PackageMigrationAdapter()
    current = migrations.current_revision(settings.database_path)
    head = migrations.head_revision(settings.database_path)
    if current is None or current != head:
        from generals_replay_analyzer.web.bootstrap import IncompatibleSchemaError

        raise IncompatibleSchemaError("worker schema identity is incompatible")
    SchemaIdentityStore().verify_worker_schema(settings.data_root, current)
    engine = create_database_engine(settings.database_path)
    # TheSuperHackers @fix Leex 23/08/2026 Bind configured engine telemetry to the launch-capable worker process. (#TBD)
    telemetry_acquirer = configured_engine_telemetry_acquirer(settings)
    service = create_production_import_service(
        create_session_factory(engine),
        settings,
        ContentAddressedStore(settings.managed_replay_directory),
        ContentAddressedStore(settings.cache_directory / "artifacts"),
        parser=parse_replay,
        telemetry_acquirer=telemetry_acquirer,
        clock=lambda: datetime.now(UTC),
        parser_version=__version__,
        telemetry_acquirer_version=(
            ENGINE_TELEMETRY_ACQUIRER_VERSION if telemetry_acquirer is not None else "none"
        ),
    )
    return service, engine, settings


def compose_worker_runtime(poll_seconds: int, lease_seconds: int) -> tuple[WorkerRuntime, object]:
    from generals_replay_analyzer.watching import (
        FileWatchStatusStore,
        WatchedRootRegistry,
        create_analytics_watched_import_adapter,
    )
    from generals_replay_analyzer.watching.adapters import RegistryWatchedRootAdapter
    from generals_replay_analyzer.watching.service import WatchScheduler

    service, engine, settings = _worker_service()
    waiter = EventWaiter()
    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        value = getattr(signal, signal_name, None)
        if value is not None:
            signal.signal(value, waiter.request_stop)
    registry = WatchedRootRegistry(settings.data_root)  # type: ignore[attr-defined]
    roots = RegistryWatchedRootAdapter(registry, settings.watched_folders)  # type: ignore[attr-defined]
    status_store = FileWatchStatusStore(settings.data_root)  # type: ignore[attr-defined]
    watcher = WatchScheduler(
        roots,
        create_analytics_watched_import_adapter(
            registry,
            service,  # type: ignore[arg-type]
            # TheSuperHackers @fix Leex 23/08/2026 Persist configured telemetry intent without silently downgrading watched imports. (#TBD)
            request_telemetry=settings.engine_executable is not None,  # type: ignore[attr-defined]
        ),
        status_store=status_store,
    )
    runtime = WorkerRuntime(
        control=service.worker_control_port(),  # type: ignore[attr-defined]
        supervisors=SubprocessSupervisorFactory(),
        waiter=waiter,
        worker_public_id=str(uuid4()),
        poll_seconds=poll_seconds,
        lease_seconds=lease_seconds,
        watcher=watcher,
    )
    return runtime, engine


def _execute_stage(job_public_id: str, execution_public_id: str) -> int:
    # TheSuperHackers @fix Leex 22/08/2026 Publish success only after disposal and signal restoration both finish. (#TBD)
    outcome_document: str | None = None
    try:
        with _posix_stage_termination_handler():
            engine: object | None = None
            try:
                service, engine, _settings = _worker_service()
                outcome = service.stage_executor_port().execute(  # type: ignore[attr-defined]
                    job_public_id,
                    execution_public_id,
                )
                outcome_document = json.dumps(asdict(outcome), separators=(",", ":"), sort_keys=True)
            finally:
                if engine is not None:
                    engine.dispose()  # type: ignore[attr-defined]
    except ProcessTreeSettledInterruption:
        return _POSIX_TREE_SETTLED_EXIT_CODE
    if outcome_document is None:
        raise RuntimeError("owned stage returned no outcome")
    print(outcome_document)
    return 0


def _module_main(arguments: list[str]) -> int:
    if len(arguments) == 3 and arguments[0] == "_execute":
        return _execute_stage(arguments[1], arguments[2])
    return 2


if __name__ == "__main__":
    raise SystemExit(_module_main(sys.argv[1:]))
