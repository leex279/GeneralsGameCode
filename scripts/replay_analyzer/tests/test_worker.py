"""External replay-analysis worker orchestration tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import cast

import pytest

import generals_replay_analyzer.worker as worker_module
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.engine import runner as engine_runner_module
from generals_replay_analyzer.importing import (
    JobClaimSelectorDTO,
    JobLifecycleError,
    JobLifecycleService,
    OwnedExecutionSettlementDTO,
    StageExecutionOutcomeDTO,
    WorkerCancellationDTO,
    WorkerLeaseDTO,
)
from generals_replay_analyzer.importing.job_contracts import (
    DEFAULT_JOB_CLAIM_SELECTOR,
)
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec
from generals_replay_analyzer.worker import (
    EventWaiter,
    SubprocessSupervisor,
    SubprocessSupervisorFactory,
    WorkerRuntime,
    validate_worker_options,
)

WORKER = "123e4567-e89b-42d3-a456-426614174030"
JOB = "123e4567-e89b-42d3-a456-426614174031"
EXECUTION = "123e4567-e89b-42d3-a456-426614174032"
RESULT = "123e4567-e89b-42d3-a456-426614174033"


def lease() -> WorkerLeaseDTO:
    return WorkerLeaseDTO(JOB, EXECUTION, "parse", 1, 3, "private-token", datetime.now(UTC) + timedelta(seconds=30))


@dataclass
class FakeControl:
    stages: tuple[str, ...] = ("parse",)
    next_claim: WorkerLeaseDTO | None = field(default_factory=lease)
    cancellation_values: list[WorkerCancellationDTO] = field(
        default_factory=lambda: [WorkerCancellationDTO(False, None)]
    )
    claims: int = 0
    selectors: list[JobClaimSelectorDTO] = field(default_factory=list)
    heartbeats: int = 0
    successes: list[str] = field(default_factory=list)
    failures: list[StageExecutionOutcomeDTO] = field(default_factory=list)
    cancelled: list[OwnedExecutionSettlementDTO] = field(default_factory=list)
    released: list[OwnedExecutionSettlementDTO] = field(default_factory=list)
    heartbeat_error: JobLifecycleError | None = None

    def registered_stages(self) -> tuple[str, ...]:
        return self.stages

    def claim_next(
        self,
        _worker_public_id: str,
        _lease_seconds: int,
        selector: JobClaimSelectorDTO = DEFAULT_JOB_CLAIM_SELECTOR,
    ) -> WorkerLeaseDTO | None:
        self.claims += 1
        self.selectors.append(selector)
        value, self.next_claim = self.next_claim, None
        return value

    def heartbeat(self, _worker_public_id: str, claim: WorkerLeaseDTO, _lease_seconds: int) -> WorkerLeaseDTO:
        self.heartbeats += 1
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return claim

    def cancellation(self, _worker_public_id: str, _claim: WorkerLeaseDTO) -> WorkerCancellationDTO:
        if len(self.cancellation_values) > 1:
            return self.cancellation_values.pop(0)
        return self.cancellation_values[0]

    def settle_success(self, _worker_public_id: str, _claim: WorkerLeaseDTO, result_public_id: str) -> None:
        self.successes.append(result_public_id)

    def settle_failure(
        self, _worker_public_id: str, _claim: WorkerLeaseDTO, outcome: StageExecutionOutcomeDTO
    ) -> None:
        self.failures.append(outcome)

    def settle_cancelled(
        self, _worker_public_id: str, _claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None:
        self.cancelled.append(settlement)

    def release_after_shutdown(
        self, _worker_public_id: str, _claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None:
        self.released.append(settlement)


@dataclass
class FakeSupervisor:
    outcomes: list[StageExecutionOutcomeDTO | None]
    terminated: int = 0
    tree_settled: bool = True

    def poll(self) -> StageExecutionOutcomeDTO | None:
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]

    def terminate(self) -> OwnedExecutionSettlementDTO:
        self.terminated += 1
        return OwnedExecutionSettlementDTO(EXECUTION, self.tree_settled)


@dataclass
class FakeSupervisorFactory:
    supervisor: FakeSupervisor
    starts: list[tuple[str, str]] = field(default_factory=list)

    def start(self, job_public_id: str, execution_public_id: str) -> FakeSupervisor:
        self.starts.append((job_public_id, execution_public_id))
        return self.supervisor


@dataclass
class FakeWaiter:
    stop_after: int | None = None
    calls: int = 0

    def wait(self, _seconds: int) -> bool:
        self.calls += 1
        return self.stop_after is not None and self.calls >= self.stop_after


def runtime(
    control: FakeControl,
    supervisor: FakeSupervisor,
    waiter: FakeWaiter | None = None,
) -> WorkerRuntime:
    return WorkerRuntime(
        control=control,
        supervisors=FakeSupervisorFactory(supervisor),
        waiter=waiter or FakeWaiter(),
        worker_public_id=WORKER,
        poll_seconds=1,
        lease_seconds=30,
    )


@pytest.mark.parametrize("poll,lease", [(0, 30), (301, 903), (1, 14), (1, 3601), (6, 15)])
def test_worker_runtime_rejects_unsafe_poll_and_lease_values(poll: int, lease: int) -> None:
    with pytest.raises(ValueError):
        validate_worker_options(poll, lease)


def test_worker_does_not_claim_when_no_handler_is_registered() -> None:
    control = FakeControl(stages=())
    worker = runtime(control, FakeSupervisor([]))

    assert worker.run_once() is False
    assert control.claims == 0


def test_one_shot_worker_forwards_an_exact_selector_without_idle_wait() -> None:
    """Catch the analyze command claiming another replay or sleeping after its exact queue drains."""
    control = FakeControl(next_claim=None)
    waiter = FakeWaiter()
    worker = runtime(control, FakeSupervisor([]), waiter)
    selector = JobClaimSelectorDTO(
        "123e4567-e89b-42d3-a456-426614174099",
        ("derive_features", "render_report"),
    )

    assert worker.run_once(selector) is False
    assert control.selectors == [selector]
    assert waiter.calls == 0


def test_one_shot_worker_rejects_a_noncontract_selector_before_watcher_or_claim() -> None:
    """Catch arbitrary selector-shaped values bypassing validation on an idle worker."""
    control = FakeControl(stages=())
    worker = runtime(control, FakeSupervisor([]))

    with pytest.raises(TypeError, match="exact JobClaimSelectorDTO"):
        worker.run_once(cast(JobClaimSelectorDTO, object()))
    assert control.claims == 0


def test_worker_heartbeats_and_settles_one_successful_owned_execution() -> None:
    control = FakeControl()
    supervisor = FakeSupervisor([None, StageExecutionOutcomeDTO("succeeded", RESULT, None, None, False)])

    assert runtime(control, supervisor).run_once() is True
    assert control.heartbeats == 1
    assert control.successes == [RESULT]


def test_cancellation_before_launch_settles_without_starting_a_supervisor() -> None:
    control = FakeControl(cancellation_values=[WorkerCancellationDTO(True, "user_request")])
    supervisor = FakeSupervisor([])
    worker = runtime(control, supervisor)

    assert worker.run_once() is True
    assert worker.supervisors.starts == []
    assert control.cancelled == [OwnedExecutionSettlementDTO(EXECUTION, True)]


def test_running_cancellation_terminates_only_owned_supervisor_before_settlement() -> None:
    control = FakeControl(
        cancellation_values=[WorkerCancellationDTO(False, None), WorkerCancellationDTO(True, "user_request")]
    )
    supervisor = FakeSupervisor([None], tree_settled=False)

    assert runtime(control, supervisor).run_once() is True
    assert supervisor.terminated == 1
    assert control.cancelled == [OwnedExecutionSettlementDTO(EXECUTION, False)]


def test_lost_lease_terminates_owned_work_without_settling_stale_claim() -> None:
    control = FakeControl(heartbeat_error=JobLifecycleError("lease_mismatch", "lease changed"))
    supervisor = FakeSupervisor([None], tree_settled=False)

    with pytest.raises(RuntimeError, match="owned_child_settlement_failed"):
        runtime(control, supervisor).run_once()
    assert supervisor.terminated == 1
    assert control.successes == [] and control.failures == [] and control.cancelled == []


def test_cooperative_shutdown_settles_owned_child_before_releasing_claim() -> None:
    control = FakeControl()
    supervisor = FakeSupervisor([None])

    assert runtime(control, supervisor, FakeWaiter(stop_after=1)).run_once() is True
    assert supervisor.terminated == 1
    assert control.released == [OwnedExecutionSettlementDTO(EXECUTION, True)]


def test_failure_outcome_is_delegated_without_worker_retry_state_machine() -> None:
    outcome = StageExecutionOutcomeDTO("retryable_failure", None, "temporary_failure", "try later", True)
    control = FakeControl()

    assert runtime(control, FakeSupervisor([outcome])).run_once() is True
    assert control.failures == [outcome]
    assert control.claims == 1


def test_external_worker_owns_watcher_scan_before_idle_claim_poll() -> None:
    class FakeWatcher:
        calls = 0

        def scan_once(self) -> tuple[str, ...]:
            self.calls += 1
            return ()

    control = FakeControl(next_claim=None)
    watcher = FakeWatcher()
    worker = WorkerRuntime(
        control=control,
        supervisors=FakeSupervisorFactory(FakeSupervisor([])),
        waiter=FakeWaiter(stop_after=1),
        worker_public_id=WORKER,
        poll_seconds=1,
        lease_seconds=30,
        watcher=watcher,
    )

    assert worker.run_once() is False
    assert watcher.calls == 1


def test_worker_with_no_registered_stages_uses_interruptible_idle_wait() -> None:
    control = FakeControl(stages=(), next_claim=None)
    waiter = FakeWaiter(stop_after=1)
    worker = runtime(control, FakeSupervisor([]), waiter)

    assert worker.run_once() is False
    assert waiter.calls == 1
    assert worker.stop_requested is True


def test_worker_keeps_scanning_watched_roots_during_owned_execution() -> None:
    class FakeWatcher:
        calls = 0

        def scan_once(self) -> tuple[str, ...]:
            self.calls += 1
            return ()

    watcher = FakeWatcher()
    control = FakeControl()
    worker = WorkerRuntime(
        control=control,
        supervisors=FakeSupervisorFactory(
            FakeSupervisor([None, StageExecutionOutcomeDTO("failed", None, "failed", "failed", False)])
        ),
        waiter=FakeWaiter(),
        worker_public_id=WORKER,
        poll_seconds=1,
        lease_seconds=30,
        watcher=watcher,
    )

    assert worker.run_once() is True
    assert watcher.calls >= 2


def test_slow_watcher_scan_never_blocks_an_active_lease_heartbeat() -> None:
    scan_started = Event()
    release_scan = Event()
    heartbeat_seen = Event()

    class SlowWatcher:
        def scan_once(self) -> tuple[str, ...]:
            scan_started.set()
            assert release_scan.wait(5), "test did not release the slow watched-root scan"
            return ()

    class SignallingControl(FakeControl):
        def heartbeat(self, worker_public_id: str, claim: WorkerLeaseDTO, lease_seconds: int) -> WorkerLeaseDTO:
            heartbeat_seen.set()
            return super().heartbeat(worker_public_id, claim, lease_seconds)

    worker = WorkerRuntime(
        control=SignallingControl(),
        supervisors=FakeSupervisorFactory(
            FakeSupervisor([None, StageExecutionOutcomeDTO("failed", None, "failed", "failed", False)])
        ),
        waiter=FakeWaiter(),
        worker_public_id=WORKER,
        poll_seconds=1,
        lease_seconds=30,
        watcher=SlowWatcher(),
    )
    worker_thread = Thread(target=worker.run_once)
    worker_thread.start()
    try:
        assert scan_started.wait(1)
        assert heartbeat_seen.wait(1), "a synchronous watcher scan blocked the lease heartbeat"
    finally:
        release_scan.set()
        worker_thread.join(5)
    assert not worker_thread.is_alive()


def test_worker_shutdown_waits_for_inflight_watcher_scan_to_settle() -> None:
    scan_started = Event()
    release_scan = Event()
    shutdown_done = Event()
    shutdown_errors: list[BaseException] = []

    class SlowWatcher:
        def scan_once(self) -> tuple[str, ...]:
            scan_started.set()
            assert release_scan.wait(5), "test did not release the slow watched-root scan"
            return ()

    worker = WorkerRuntime(
        control=FakeControl(next_claim=None),
        supervisors=FakeSupervisorFactory(FakeSupervisor([])),
        waiter=FakeWaiter(stop_after=1),
        worker_public_id=WORKER,
        poll_seconds=1,
        lease_seconds=30,
        watcher=SlowWatcher(),
    )
    assert worker.run_once() is False
    assert scan_started.wait(1)

    def shutdown() -> None:
        try:
            worker.shutdown()
        except (AttributeError, OSError, RuntimeError) as error:
            shutdown_errors.append(error)
        finally:
            shutdown_done.set()

    shutdown_thread = Thread(target=shutdown)
    shutdown_thread.start()
    try:
        assert not shutdown_done.wait(0.1), "shutdown returned before the watched scan settled"
    finally:
        release_scan.set()
        shutdown_thread.join(5)

    assert shutdown_errors == []
    assert shutdown_done.is_set()


def test_watcher_io_failure_does_not_stop_durable_job_polling() -> None:
    class BrokenWatcher:
        def scan_once(self) -> tuple[str, ...]:
            raise OSError("private watched-root path must not escape")

    control = FakeControl(next_claim=None)
    worker = WorkerRuntime(
        control=control,
        supervisors=FakeSupervisorFactory(FakeSupervisor([])),
        waiter=FakeWaiter(stop_after=1),
        worker_public_id=WORKER,
        poll_seconds=1,
        lease_seconds=30,
        watcher=BrokenWatcher(),
    )

    assert worker.run_once() is False
    assert control.claims == 1


def test_two_file_backed_workers_claim_once_recover_expiry_and_exhaust_retries(tmp_path: Path) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.current = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

        def __call__(self) -> datetime:
            return self.current

        def advance(self, seconds: int) -> None:
            self.current += timedelta(seconds=seconds)

    database = tmp_path / "jobs.sqlite3"
    upgrade_database(database)
    engine = create_database_engine(database)
    sessions = create_session_factory(engine)
    clock = MutableClock()

    def service() -> JobLifecycleService:
        return JobLifecycleService(
            sessions,
            registered_stages=("parse",),
            clock=clock,
            retry_base_delay=timedelta(0),
            retry_max_delay=timedelta(0),
        )

    execution_job_id = JobCoordinator(sessions, clock=clock).create_job(
        JobSpec(
            stage="parse",
            component_version="parse-v1",
            idempotency_key="worker-runtime-execution",
            input_json={"private": "resolved only inside the executor"},
            max_attempts=1,
            retryable=False,
        )
    ).public_id

    class CountingFactory:
        def __init__(self) -> None:
            self.starts = 0
            self._lock = Lock()

        def start(self, _job_public_id: str, _execution_public_id: str) -> FakeSupervisor:
            with self._lock:
                self.starts += 1
            return FakeSupervisor([StageExecutionOutcomeDTO("failed", None, "stage_failed", "failed", False)])

    execution_factory = CountingFactory()
    execution_runtimes = (
        WorkerRuntime(
            control=service(),
            supervisors=execution_factory,
            waiter=FakeWaiter(stop_after=1),
            worker_public_id="123e4567-e89b-42d3-a456-426614174052",
            poll_seconds=1,
            lease_seconds=30,
        ),
        WorkerRuntime(
            control=service(),
            supervisors=execution_factory,
            waiter=FakeWaiter(stop_after=1),
            worker_public_id="123e4567-e89b-42d3-a456-426614174053",
            poll_seconds=1,
            lease_seconds=30,
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(lambda worker: worker.run_once(), execution_runtimes))
    assert execution_factory.starts == 1
    assert service().get_job(execution_job_id).summary.state.value == "failed"

    job_id = JobCoordinator(sessions, clock=clock).create_job(
        JobSpec(
            stage="parse",
            component_version="parse-v1",
            idempotency_key="worker-file-backed",
            input_json={"private": "never-crosses-worker"},
            max_attempts=3,
            retryable=True,
        )
    ).public_id

    first_service = service()
    second_service = service()
    first_worker = "123e4567-e89b-42d3-a456-426614174050"
    second_worker = "123e4567-e89b-42d3-a456-426614174051"
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(
            pool.map(
                lambda pair: pair[0].claim_next(pair[1], 30),
                ((first_service, first_worker), (second_service, second_worker)),
            )
        )
    winner_claim = next(claim for claim in claims if claim is not None)
    assert sum(claim is not None for claim in claims) == 1
    winner = first_worker if claims[0] is not None else second_worker
    loser = second_worker if winner == first_worker else first_worker
    with pytest.raises(JobLifecycleError, match="lease_mismatch"):
        second_service.heartbeat(loser, winner_claim, 30)

    clock.advance(31)
    recovered = service().claim_next(loser, 30)
    assert recovered is not None and recovered.job_public_id == job_id and recovered.attempt_count == 2
    service().heartbeat(loser, recovered, 30)
    service().settle_failure(
        loser,
        recovered,
        StageExecutionOutcomeDTO("retryable_failure", None, "temporary_failure", "retry", True),
    )

    restarted = service()
    final_claim = restarted.claim_next(first_worker, 30)
    assert final_claim is not None and final_claim.attempt_count == 3
    restarted.settle_failure(
        first_worker,
        final_claim,
        StageExecutionOutcomeDTO("retryable_failure", None, "temporary_failure", "retry", True),
    )
    assert restarted.get_job(job_id).summary.state.value == "failed"
    engine.dispose()


def test_event_waiter_is_interruptible() -> None:
    waiter = EventWaiter()
    assert waiter.wait(0) is False
    waiter.request_stop()
    assert waiter.wait(1) is True


class _Process:
    def __init__(
        self,
        *,
        running: bool,
        stdout: str = "",
        wait_failures: int = 0,
        return_code: int = 0,
    ) -> None:
        self.pid = 1234
        self.running = running
        self.stdout = stdout
        self.wait_failures = wait_failures
        self.return_code = return_code
        self.kills = 0

    def poll(self) -> int | None:
        return None if self.running else self.return_code

    def communicate(self) -> tuple[str, str]:
        return self.stdout, ""

    def wait(self, timeout: int) -> int:
        assert timeout in {5, 10, 15}
        if self.wait_failures:
            self.wait_failures -= 1
            raise subprocess.TimeoutExpired("owned", timeout)
        self.running = False
        return self.return_code

    def kill(self) -> None:
        self.kills += 1
        self.running = False


def test_subprocess_supervisor_decodes_one_outcome_and_fails_closed_on_invalid_protocol() -> None:
    document = json.dumps(
        {
            "status": "succeeded",
            "result_public_id": RESULT,
            "error_code": None,
            "error_message": None,
            "retryable": False,
        }
    )
    valid = SubprocessSupervisor(_Process(running=False, stdout=document), EXECUTION)  # type: ignore[arg-type]
    invalid = SubprocessSupervisor(_Process(running=False, stdout="not-json"), EXECUTION)  # type: ignore[arg-type]

    assert valid.poll() == StageExecutionOutcomeDTO("succeeded", RESULT, None, None, False)
    assert valid.poll() == StageExecutionOutcomeDTO("succeeded", RESULT, None, None, False)
    failed = invalid.poll()
    assert failed is not None and failed.error_code == "supervisor_protocol_failed"


def test_subprocess_supervisor_rejects_nonzero_child_even_with_valid_stdout() -> None:
    document = json.dumps(
        {
            "status": "succeeded",
            "result_public_id": RESULT,
            "error_code": None,
            "error_message": None,
            "retryable": False,
        }
    )
    supervisor = SubprocessSupervisor(
        _Process(running=False, stdout=document, return_code=9),  # type: ignore[arg-type]
        EXECUTION,
    )

    outcome = supervisor.poll()

    assert outcome is not None and outcome.error_code == "supervisor_protocol_failed"
    assert supervisor.terminate() == OwnedExecutionSettlementDTO(EXECUTION, False)


def test_windows_supervisor_requires_successful_taskkill_tree_result(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _Process(running=True)

    def failed_taskkill(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[object]:
        process.running = False
        return subprocess.CompletedProcess([], 1)

    monkeypatch.setattr(worker_module.os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", failed_taskkill)

    settlement = SubprocessSupervisor(process, EXECUTION).terminate()  # type: ignore[arg-type]

    assert settlement == OwnedExecutionSettlementDTO(EXECUTION, False)


def test_windows_supervisor_leader_only_fallback_is_never_tree_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _Process(running=True)

    def broken_taskkill(*_args: object, **_kwargs: object) -> object:
        raise OSError("taskkill unavailable")

    monkeypatch.setattr(worker_module.os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", broken_taskkill)

    settlement = SubprocessSupervisor(process, EXECUTION).terminate()  # type: ignore[arg-type]

    assert settlement == OwnedExecutionSettlementDTO(EXECUTION, False)
    assert process.kills == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill supervisor contract")
def test_subprocess_supervisor_terminates_only_its_owned_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _Process(running=True)
    commands: list[list[str]] = []

    def terminate(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
        commands.append(command)
        process.running = False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", terminate)
    supervisor = SubprocessSupervisor(process, EXECUTION)  # type: ignore[arg-type]

    assert supervisor.poll() is None
    assert supervisor.terminate() == OwnedExecutionSettlementDTO(EXECUTION, True)
    assert commands == [["taskkill", "/PID", "1234", "/T", "/F"]]


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill supervisor contract")
def test_subprocess_supervisor_escalates_owned_process_and_verifies_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(running=True, wait_failures=1)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0))
    supervisor = SubprocessSupervisor(process, EXECUTION)  # type: ignore[arg-type]

    assert supervisor.terminate() == OwnedExecutionSettlementDTO(EXECUTION, False)
    assert process.kills == 1


def test_posix_supervisor_requires_the_private_settlement_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_calls: list[tuple[int, int]] = []

    def forbid_process_group_lookup(_pid: int) -> int:
        raise AssertionError("owned new-session PGID must be the recorded child PID")

    def signal_owned_group(process_group_id: int, requested_signal: int) -> None:
        signal_calls.append((process_group_id, requested_signal))

    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.setattr(worker_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(worker_module.os, "getpgid", forbid_process_group_lookup, raising=False)
    monkeypatch.setattr(worker_module.os, "killpg", signal_owned_group, raising=False)
    settled = _Process(
        running=True,
        return_code=worker_module._POSIX_TREE_SETTLED_EXIT_CODE,
    )
    uncertain = _Process(running=True, return_code=1)
    escalating = _Process(running=True, wait_failures=1)

    assert SubprocessSupervisor(settled, EXECUTION).terminate().tree_settled is True  # type: ignore[arg-type]
    assert SubprocessSupervisor(uncertain, EXECUTION).terminate().tree_settled is False  # type: ignore[arg-type]
    assert SubprocessSupervisor(escalating, EXECUTION).terminate().tree_settled is False  # type: ignore[arg-type]
    assert signal_calls == [
        (settled.pid, signal.SIGTERM),
        (uncertain.pid, signal.SIGTERM),
        (escalating.pid, signal.SIGTERM),
        (escalating.pid, signal.SIGKILL),
    ]


def test_posix_stage_raw_sigterm_never_acknowledges_tree_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_handler = object()
    signal_calls: list[tuple[object, object]] = []
    installed_handler: list[object] = []

    def register(signal_number: object, handler: object) -> object:
        signal_calls.append((signal_number, handler))
        if len(signal_calls) == 1:
            installed_handler.append(handler)
            return previous_handler
        return object()

    class Executor:
        def execute(self, _job_public_id: str, _execution_public_id: str) -> StageExecutionOutcomeDTO:
            handler = installed_handler[0]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            raise AssertionError("SIGTERM handler must unwind the blocking stage")

    class Service:
        def stage_executor_port(self) -> Executor:
            return Executor()

    class Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.setattr(worker_module.signal, "signal", register)
    monkeypatch.setattr(worker_module, "_worker_service", lambda: (Service(), engine, object()))

    with pytest.raises(worker_module._StageTerminationRequested):
        worker_module._execute_stage(JOB, EXECUTION)
    assert signal_calls[0][0] == signal.SIGTERM
    assert signal_calls[1] == (signal.SIGTERM, previous_handler)
    assert engine.disposed is True


def test_posix_stage_acknowledges_only_the_engine_settled_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_calls: list[tuple[object, object]] = []

    def register(signal_number: object, handler: object) -> object:
        signal_calls.append((signal_number, handler))
        return signal.SIG_DFL

    class Executor:
        def execute(self, _job_public_id: str, _execution_public_id: str) -> StageExecutionOutcomeDTO:
            raise engine_runner_module.ProcessTreeSettledInterruption

    class Service:
        def stage_executor_port(self) -> Executor:
            return Executor()

    class Engine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.setattr(worker_module.signal, "signal", register)
    monkeypatch.setattr(worker_module, "_worker_service", lambda: (Service(), Engine(), object()))

    assert worker_module._execute_stage(JOB, EXECUTION) == worker_module._POSIX_TREE_SETTLED_EXIT_CODE
    assert signal_calls == [(signal.SIGTERM, worker_module._raise_stage_termination), (signal.SIGTERM, signal.SIG_DFL)]


def test_posix_stage_cleanup_failure_never_returns_the_settlement_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_handler: list[object] = []
    cleanup_error = OSError("injected runner cleanup failure")

    def register(_signal_number: object, handler: object) -> object:
        if not installed_handler:
            installed_handler.append(handler)
        return signal.SIG_DFL

    class Executor:
        def execute(self, _job_public_id: str, _execution_public_id: str) -> StageExecutionOutcomeDTO:
            handler = installed_handler[0]
            assert callable(handler)
            try:
                handler(signal.SIGTERM, None)
            except worker_module._StageTerminationRequested as termination:
                raise termination from cleanup_error
            raise AssertionError("SIGTERM handler must unwind the blocking stage")

    class Service:
        def stage_executor_port(self) -> Executor:
            return Executor()

    class Engine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.setattr(worker_module.signal, "signal", register)
    monkeypatch.setattr(worker_module, "_worker_service", lambda: (Service(), Engine(), object()))

    with pytest.raises(worker_module._StageTerminationRequested) as raised:
        worker_module._execute_stage(JOB, EXECUTION)

    assert raised.value.__cause__ is cleanup_error


def test_posix_handler_restore_failure_never_returns_the_settlement_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore_error = OSError("injected handler restore failure")

    def register(_signal_number: object, handler: object) -> object:
        if handler is worker_module._raise_stage_termination:
            return signal.SIG_DFL
        raise restore_error

    class Executor:
        def execute(self, _job_public_id: str, _execution_public_id: str) -> StageExecutionOutcomeDTO:
            raise engine_runner_module.ProcessTreeSettledInterruption

    class Service:
        def stage_executor_port(self) -> Executor:
            return Executor()

    class Engine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.setattr(worker_module.signal, "signal", register)
    monkeypatch.setattr(worker_module, "_worker_service", lambda: (Service(), Engine(), object()))

    with pytest.raises(OSError) as raised:
        worker_module._execute_stage(JOB, EXECUTION)

    assert raised.value is restore_error


def test_stage_emits_no_outcome_before_engine_disposal_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disposal_error = OSError("injected engine disposal failure")

    class Executor:
        def execute(self, _job_public_id: str, _execution_public_id: str) -> StageExecutionOutcomeDTO:
            return StageExecutionOutcomeDTO("succeeded", RESULT, None, None, False)

    class Service:
        def stage_executor_port(self) -> Executor:
            return Executor()

    class Engine:
        def dispose(self) -> None:
            raise disposal_error

    monkeypatch.setattr(worker_module, "_worker_service", lambda: (Service(), Engine(), object()))

    with pytest.raises(OSError) as raised:
        worker_module._execute_stage(JOB, EXECUTION)

    assert raised.value is disposal_error
    assert capsys.readouterr().out == ""


def test_stage_emits_no_outcome_before_signal_handler_restoration_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    restore_error = OSError("injected handler restore failure")

    def register(_signal_number: object, handler: object) -> object:
        if handler is worker_module._raise_stage_termination:
            return signal.SIG_DFL
        raise restore_error

    class Executor:
        def execute(self, _job_public_id: str, _execution_public_id: str) -> StageExecutionOutcomeDTO:
            return StageExecutionOutcomeDTO("succeeded", RESULT, None, None, False)

    class Service:
        def stage_executor_port(self) -> Executor:
            return Executor()

    class Engine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.setattr(worker_module.signal, "signal", register)
    monkeypatch.setattr(worker_module, "_worker_service", lambda: (Service(), Engine(), object()))

    with pytest.raises(OSError) as raised:
        worker_module._execute_stage(JOB, EXECUTION)

    assert raised.value is restore_error
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name == "nt", reason="requires real POSIX sessions and signals")
def test_posix_sigterm_settles_the_nested_runner_group_before_parent_reports_success(tmp_path: Path) -> None:
    inner_pid_path = tmp_path / "inner.pid"
    helper_path = tmp_path / "nested_stage_helper.py"
    stdout_path = tmp_path / "engine.stdout"
    stderr_path = tmp_path / "engine.stderr"
    helper_path.write_text(
        f"""
import os
import sys
import time
from pathlib import Path

from generals_replay_analyzer import worker
from generals_replay_analyzer.engine.runner import ProcessLaunchRequest, _posix_process_launcher

inner_pid_path = Path({str(inner_pid_path)!r})
stdout_path = Path({str(stdout_path)!r})
stderr_path = Path({str(stderr_path)!r})

class Executor:
    def execute(self, _job_public_id, _execution_public_id):
        inner_code = (
            "import os,time; from pathlib import Path; "
            f"Path({str(inner_pid_path)!r}).write_text(str(os.getpid()), encoding='ascii'); time.sleep(60)"
        )
        with stdout_path.open("xb", buffering=0) as stdout_handle, stderr_path.open("xb", buffering=0) as stderr_handle:
            request = ProcessLaunchRequest(
                run_id={EXECUTION!r},
                run_dir=stdout_path.parent,
                argv=(sys.executable, "-c", inner_code),
                cwd=stdout_path.parent,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
                timeout_seconds=60,
            )
            return _posix_process_launcher(request)

class Service:
    def stage_executor_port(self):
        return Executor()

class Engine:
    def dispose(self):
        pass

worker._worker_service = lambda: (Service(), Engine(), object())
raise SystemExit(worker._execute_stage({JOB!r}, {EXECUTION!r}))
""",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(helper_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    inner_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not inner_pid_path.exists():
            time.sleep(0.01)
        assert inner_pid_path.exists(), "nested runner child did not start"
        inner_pid = int(inner_pid_path.read_text(encoding="ascii"))

        settlement = SubprocessSupervisor(process, EXECUTION).terminate()

        assert settlement == OwnedExecutionSettlementDTO(EXECUTION, True)
        with pytest.raises(ProcessLookupError):
            os.kill(inner_pid, 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        if inner_pid is not None:
            try:
                os.killpg(inner_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_supervisor_factory_launches_only_private_public_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    process = _Process(running=True)

    def popen(command: list[str], **kwargs: object) -> _Process:
        captured.update({"command": command, **kwargs})
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    supervisor = SubprocessSupervisorFactory().start(JOB, EXECUTION)

    assert isinstance(supervisor, SubprocessSupervisor)
    assert captured["command"][-3:] == ["_execute", JOB, EXECUTION]  # type: ignore[index]
    assert "stdin" in captured and "stdout" in captured


def test_worker_composition_verifies_bootstrapped_schema_without_migrating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from generals_replay_analyzer.config import AnalyzerSettings
    from generals_replay_analyzer.web.bootstrap import BootstrapReadinessState, create_production_bootstrapper

    data_root = tmp_path / "product"
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))
    settings = AnalyzerSettings.model_validate({})
    create_production_bootstrapper(BootstrapReadinessState()).prepare(settings)
    registered_signals: list[object] = []
    monkeypatch.setattr(worker_module.signal, "signal", lambda signal_number, _handler: registered_signals.append(signal_number))

    service, engine, composed_settings = worker_module._worker_service()
    assert composed_settings.database_path == settings.database_path
    assert service.worker_control_port().registered_stages() == (
        "analyze_llm",
        "assess_strategies",
        "derive_features",
        "discover",
        "hash",
        "import_observations",
        "manage_copy",
        "parse",
        "render_report",
    )
    engine.dispose()
    runtime_value, runtime_engine = worker_module.compose_worker_runtime(1, 120)
    assert runtime_value.watcher is not None
    assert set(registered_signals) == {
        getattr(worker_module.signal, name)
        for name in ("SIGINT", "SIGTERM", "SIGBREAK")
        if hasattr(worker_module.signal, name)
    }
    runtime_engine.dispose()


def test_worker_fresh_composition_activates_persisted_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The external worker must restart with the same persisted settings as Web and CLI."""

    from generals_replay_analyzer.config import load_runtime_configuration
    from generals_replay_analyzer.configuration import ConfigurationStore, SettingChange
    from generals_replay_analyzer.web.bootstrap import BootstrapReadinessState, create_production_bootstrapper

    configuration_root = tmp_path / "external-configuration"
    data_root = tmp_path / "product-data"
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))
    ConfigurationStore(configuration_root=configuration_root, environment={}).apply(
        expected_revision=0,
        changes=(
            SettingChange("movement_sample_frames", 120),
            SettingChange("minimum_longitudinal_sample_size", 29),
            SettingChange("import_mode", "reference"),
        ),
    )
    runtime = load_runtime_configuration(configuration_root=configuration_root)
    create_production_bootstrapper(BootstrapReadinessState()).prepare(runtime.settings)

    service, engine, settings = worker_module._worker_service(configuration_root=configuration_root)
    try:
        assert settings.movement_sample_frames == 120
        assert settings.minimum_longitudinal_sample_size == 29
        assert settings.import_mode == "reference"
        assert service.worker_control_port().registered_stages()
    finally:
        engine.dispose()


def test_private_stage_entrypoint_prints_only_outcome_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    outcome = StageExecutionOutcomeDTO("succeeded", RESULT, None, None, False)

    class Executor:
        def execute(self, job_public_id: str, execution_public_id: str) -> StageExecutionOutcomeDTO:
            assert (job_public_id, execution_public_id) == (JOB, EXECUTION)
            return outcome

    class Service:
        def stage_executor_port(self) -> Executor:
            return Executor()

    class Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(worker_module, "_worker_service", lambda: (Service(), engine, object()))

    assert worker_module._module_main(["_execute", JOB, EXECUTION]) == 0
    assert json.loads(capsys.readouterr().out)["result_public_id"] == RESULT
    assert engine.disposed is True
    assert worker_module._module_main([]) == 2
