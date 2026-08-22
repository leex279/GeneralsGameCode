"""Secure durable lifecycle behavior for external workers and the Jobs UI."""

from __future__ import annotations

import hashlib
import inspect
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock, get_ident
from types import ModuleType
from typing import Any, Self
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import generals_replay_analyzer.importing.job_contracts as contracts
from generals_replay_analyzer.db.models import Job, JobEvent, JobLogSnapshot, JobStageResult, ManagedAsset
from generals_replay_analyzer.importing.job_contracts import (
    CancelJobCommandDTO,
    JobLogQueryDTO,
    JobProgressDTO,
    JobQueryDTO,
    JobState,
    OwnedExecutionSettlementDTO,
    RetryJobCommandDTO,
    StageExecutionOutcomeDTO,
    WorkerLeaseDTO,
)
from generals_replay_analyzer.importing.job_lifecycle import JobLifecycleService
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec, JobStateError
from generals_replay_analyzer.storage import ContentAddressedStore, StoredContent

from .conftest import MutableClock

_LOG_INTEGRITY_VERSION = "sha256-merkle-v1"
_LOG_INTEGRITY_CHUNK_SIZE = 4_096
_LOG_LEAF_DOMAIN = b"job-log-merkle-v1:leaf\x00"
_LOG_PADDING_DOMAIN = b"job-log-merkle-v1:padding\x00"
_LOG_NODE_DOMAIN = b"job-log-merkle-v1:node\x00"
_LOG_ROOT_DOMAIN = b"job-log-merkle-v1:root\x00"


def _job(session_factory: sessionmaker[Session], clock: MutableClock, stage: str = "parse", **values: object) -> str:
    coordinator = JobCoordinator(session_factory, clock=clock)
    spec = JobSpec(
        stage=stage,
        component_version="component-v1",
        idempotency_key=str(values.pop("idempotency_key", f"{stage}-identity")),
        input_json={"private_input": "must-never-cross-contract"},
        max_attempts=int(values.pop("max_attempts", 3)),
        retryable=bool(values.pop("retryable", True)),
    )
    assert not values
    return coordinator.create_job(spec).public_id


def _service(
    session_factory: sessionmaker[Session],
    clock: MutableClock,
    tmp_path: Path,
    stages: tuple[str, ...] = ("parse",),
) -> JobLifecycleService:
    return JobLifecycleService(
        session_factory,
        registered_stages=stages,
        clock=clock,
        retry_base_delay=timedelta(0),
        retry_max_delay=timedelta(0),
        log_store=ContentAddressedStore(tmp_path / "managed-logs"),
        log_data_root=tmp_path,
        redaction_values=(str(tmp_path), "TOP_SECRET"),
    )


def _claim(service: JobLifecycleService, worker: str = "00000000-0000-4000-8000-000000000901") -> WorkerLeaseDTO:
    claim = service.claim_next(worker, 30)
    assert claim is not None
    return claim


@contextmanager
def _barrier_job_reads(session_factory: sessionmaker[Session]) -> Iterator[None]:
    """Make two real SQLite writers observe the same pre-mutation job snapshot."""
    engine = session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    barrier = Barrier(2)
    lock = Lock()
    seen_threads: set[int] = set()

    def synchronize_first_job_read(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "FROM jobs" not in statement:
            return
        thread_id = get_ident()
        with lock:
            if thread_id in seen_threads:
                return
            seen_threads.add(thread_id)
        try:
            barrier.wait(timeout=1)
        except BrokenBarrierError:
            pass

    event.listen(engine, "before_cursor_execute", synchronize_first_job_read)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_first_job_read)


def _capture(call: Any) -> object:
    try:
        return call()
    except Exception as error:  # noqa: BLE001 - tests assert the public exception boundary, including raw leaks.
        return error


def _advance_osc_state(state: tuple[bool, bool], data: bytes) -> tuple[bool, bool]:
    """Derive the literal OSC checkpoint fixture state independently of the reader."""
    in_osc, pending_escape = state
    for value in data:
        if in_osc:
            if value == 0x07 or (pending_escape and value == ord("\\")):
                in_osc = False
                pending_escape = False
            else:
                pending_escape = value == 0x1B
        elif pending_escape:
            in_osc = value == ord("]")
            pending_escape = False
        elif value == 0x1B:
            pending_escape = True
    return in_osc, pending_escape


def _authenticated_log_container(content: bytes) -> tuple[bytes, str]:
    """Build the canonical externally specified fixture container with literal expected hashes."""
    chunk_count = max(1, (len(content) + _LOG_INTEGRITY_CHUNK_SIZE - 1) // _LOG_INTEGRITY_CHUNK_SIZE)
    capacity = 1 << (chunk_count - 1).bit_length()
    nodes = [b""] * (2 * capacity - 1)
    states: list[int] = []
    state = (False, False)
    for index in range(chunk_count):
        start = index * _LOG_INTEGRITY_CHUNK_SIZE
        chunk = content[start : start + _LOG_INTEGRITY_CHUNK_SIZE]
        state_code = int(state[0]) | (int(state[1]) << 1)
        states.append(state_code)
        nodes[capacity - 1 + index] = hashlib.sha256(
            _LOG_LEAF_DOMAIN
            + index.to_bytes(8, "big")
            + bytes((state_code,))
            + len(chunk).to_bytes(4, "big")
            + chunk
        ).digest()
        state = _advance_osc_state(state, chunk)
    for index in range(chunk_count, capacity):
        nodes[capacity - 1 + index] = hashlib.sha256(
            _LOG_PADDING_DOMAIN + index.to_bytes(8, "big")
        ).digest()
    for index in range(capacity - 2, -1, -1):
        nodes[index] = hashlib.sha256(
            _LOG_NODE_DOMAIN + nodes[index * 2 + 1] + nodes[index * 2 + 2]
        ).digest()
    root = hashlib.sha256(
        _LOG_ROOT_DOMAIN
        + len(content).to_bytes(8, "big")
        + _LOG_INTEGRITY_CHUNK_SIZE.to_bytes(4, "big")
        + chunk_count.to_bytes(8, "big")
        + nodes[0]
    ).hexdigest()
    footer = bytes(ord("0") + value for value in states) + b"".join(
        node.hex().encode("ascii") for node in nodes
    )
    return content + footer, root


def _register_authenticated_log(
    session_factory: sessionmaker[Session],
    clock: MutableClock,
    tmp_path: Path,
    job_public_id: str,
    content: bytes,
    *,
    label: str = "supervisor",
    sequence: int = 0,
) -> tuple[str, StoredContent]:
    container, root = _authenticated_log_container(content)
    stored = ContentAddressedStore(tmp_path / "managed-logs").store_bytes(container)
    log_public_id = str(uuid4())
    with session_factory.begin() as session:
        job = session.scalar(select(Job).where(Job.public_id == job_public_id))
        assert job is not None
        asset = ManagedAsset(
            public_id=str(uuid4()),
            sha256=stored.sha256,
            kind="job_log_snapshot",
            relative_path=stored.path.relative_to(tmp_path).as_posix(),
            size_bytes=stored.size,
            media_type="text/plain",
            created_at=clock.current,
        )
        session.add(asset)
        session.flush()
        session.add(
            JobLogSnapshot(
                public_id=log_public_id,
                job_id=job.id,
                attempt_count=job.attempt_count,
                label=label,
                sequence=sequence,
                managed_asset_id=asset.id,
                media_type="text/plain",
                byte_count=len(content),
                redaction_version="job-log-redaction-v1",
                integrity_version=_LOG_INTEGRITY_VERSION,
                integrity_root_sha256=root,
                integrity_chunk_size=_LOG_INTEGRITY_CHUNK_SIZE,
                created_at=clock.current,
            )
        )
    return log_public_id, stored


def _mutate_byte_and_restore_mtime(path: Path, offset: int) -> None:
    before = path.stat()
    with path.open("r+b") as stream:
        stream.seek(offset)
        original = stream.read(1)
        assert original
        stream.seek(offset)
        stream.write(b"X" if original != b"X" else b"Y")
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


class _CoordinatedPath:
    """Schedule a cleanup race while preserving real filesystem behavior."""

    def __init__(self, path: Path, cleanup_entered: Event, winner_registered: Event) -> None:
        self._path = path
        self._cleanup_entered = cleanup_entered
        self._winner_registered = winner_registered

    def __fspath__(self) -> str:
        return str(self._path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._path, name)

    def resolve(self, *, strict: bool = False) -> Path:
        self._wait_for_winner()
        return self._path.resolve(strict=strict)

    def unlink(self, *, missing_ok: bool = False) -> None:
        self._wait_for_winner()
        self._path.unlink(missing_ok=missing_ok)

    def _wait_for_winner(self) -> None:
        self._cleanup_entered.set()
        if not self._winner_registered.wait(timeout=5):
            raise AssertionError("racing publisher did not reach durable registration")


class _CleanupRaceStore:
    def __init__(self, delegate: ContentAddressedStore, cleanup_entered: Event, winner_registered: Event) -> None:
        self.root = delegate.root
        self._delegate = delegate
        self._cleanup_entered = cleanup_entered
        self._winner_registered = winner_registered
        self._lock = Lock()
        self._calls = 0

    def store_bytes(self, data: bytes) -> StoredContent:
        stored = self._delegate.store_bytes(data)
        with self._lock:
            self._calls += 1
            first = self._calls == 1
        if not first:
            return stored
        return StoredContent(
            stored.sha256,
            _CoordinatedPath(stored.path, self._cleanup_entered, self._winner_registered),  # type: ignore[arg-type]
            stored.size,
            stored.created,
        )

    def verify(self, sha256: str) -> StoredContent:
        return self._delegate.verify(sha256)


class _BlockingStore:
    def __init__(self, delegate: ContentAddressedStore, entered: Event, release: Event) -> None:
        self.root = delegate.root
        self._delegate = delegate
        self._entered = entered
        self._release = release

    def store_bytes(self, data: bytes) -> StoredContent:
        self._entered.set()
        if not self._release.wait(timeout=5):
            raise AssertionError("test did not release the blocking content store")
        return self._delegate.store_bytes(data)

    def verify(self, sha256: str) -> StoredContent:
        return self._delegate.verify(sha256)


class _CountingReader:
    def __init__(self, delegate: Any, counter: list[int]) -> None:
        self._delegate = delegate
        self._counter = counter

    def __enter__(self) -> Self:
        self._delegate.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._delegate.__exit__(*args)

    def read(self, size: int = -1) -> bytes:
        data = self._delegate.read(size)
        self._counter[0] += len(data)
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def test_contract_module_is_orm_free_immutable_and_contains_no_privileged_job_views() -> None:
    """Catch a public DTO importing persistence/runtime layers or leaking raw job implementation fields."""
    import generals_replay_analyzer.importing.job_contracts as contracts

    imported_modules = {
        value.__name__
        for value in vars(contracts).values()
        if isinstance(value, ModuleType)
    }
    assert not any(
        name.startswith(("sqlalchemy", "generals_replay_analyzer.db", "generals_replay_analyzer.web", "pathlib"))
        for name in imported_modules
    )
    assert tuple(JobState) == (
        JobState.PENDING,
        JobState.RUNNING,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    )
    forbidden = {"input_json", "output_json", "path", "pid", "process", "owner", "lease_token_sha256"}
    for value in vars(contracts).values():
        if inspect.isclass(value) and hasattr(value, "__dataclass_fields__"):
            assert value.__dataclass_params__.frozen is True
            assert forbidden.isdisjoint(field.name for field in fields(value))


def test_two_services_atomically_claim_once_and_persist_only_the_token_digest(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch a claim race, duplicate attempt increment, raw token persistence, or unregistered-stage admission."""
    job_public_id = _job(session_factory, clock)
    first = _service(session_factory, clock, tmp_path)
    second = _service(session_factory, clock, tmp_path)
    workers = (
        "00000000-0000-4000-8000-000000000911",
        "00000000-0000-4000-8000-000000000912",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(first.claim_next, workers[0], 30),
            executor.submit(second.claim_next, workers[1], 30),
        )
        claims = tuple(future.result() for future in futures)
    claim = next(value for value in claims if value is not None)
    assert sum(value is not None for value in claims) == 1
    assert claim.job_public_id == job_public_id
    assert claim.attempt_count == 1
    assert len(claim.lease_token) >= 43
    with session_factory() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_public_id))
        assert row is not None
        assert row.attempt_count == 1
        assert row.lease_token_sha256 == hashlib.sha256(claim.lease_token.encode("utf-8")).hexdigest()
        assert claim.lease_token not in "|".join(
            str(value)
            for value in (
                row.lease_owner,
                row.lease_token_sha256,
                row.lease_execution_public_id,
                row.input_json,
                row.error_details_json,
            )
        )
    assert second.claim_next(workers[1], 30) is None
    _job(session_factory, clock, "derive_features", idempotency_key="unregistered")
    assert first.claim_next(workers[0], 30) is None


def test_heartbeat_progress_and_capability_checks_preserve_attempt_and_heartbeat_revision(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch partial lease validation, heartbeat token churn, or nonmonotonic progress within one attempt."""
    _job(session_factory, clock)
    service = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000921"
    claim = _claim(service, worker)
    detail = service.get_job(claim.job_public_id)
    initial_revision = detail.summary.revision
    clock.advance(seconds=10)
    extended = service.heartbeat(worker, claim, 60)
    assert extended.lease_expires_at == clock.current + timedelta(seconds=60)
    after_heartbeat = service.get_job(claim.job_public_id)
    assert after_heartbeat.summary.revision == initial_revision
    assert after_heartbeat.summary.attempt_count == 1

    service.report_progress(worker, extended, JobProgressDTO(2, 5, "items", clock.current))
    progressed = service.get_job(claim.job_public_id)
    assert progressed.summary.progress == JobProgressDTO(2, 5, "items", clock.current)
    assert progressed.summary.revision == initial_revision + 1
    service.report_progress(worker, extended, JobProgressDTO(2, 5, "items", clock.current))
    assert service.get_job(claim.job_public_id).summary.revision == progressed.summary.revision
    with pytest.raises(JobStateError, match="progress_regressed"):
        service.report_progress(worker, extended, JobProgressDTO(1, 5, "items", clock.current))
    with pytest.raises(JobStateError, match="progress_regressed"):
        service.report_progress(
            worker,
            extended,
            JobProgressDTO(3, 5, "items", clock.current - timedelta(microseconds=1)),
        )

    wrong_claims = (
        replace(extended, lease_token="wrong"),
        replace(extended, attempt_count=2),
        replace(extended, execution_public_id="00000000-0000-4000-8000-000000000999"),
    )
    for wrong in wrong_claims:
        with pytest.raises(JobStateError, match="lease_mismatch"):
            service.heartbeat(worker, wrong, 30)
    with pytest.raises(JobStateError, match="lease_mismatch"):
        service.heartbeat("00000000-0000-4000-8000-000000000922", extended, 30)


def test_pending_and_running_cancellation_preserve_lease_until_exact_owned_tree_settlement(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch process-signalling cancellation, success-after-cancel, stale mutation, or uncertain cancellation success."""
    pending_id = _job(session_factory, clock, idempotency_key="pending-cancel")
    service = _service(session_factory, clock, tmp_path)
    pending = service.get_job(pending_id)
    mutation = service.cancel_job(
        CancelJobCommandDTO(pending_id, pending.summary.revision, "operator", "user_request")
    )
    assert mutation.state is JobState.CANCELLED
    assert mutation.attempt_count == 0
    with pytest.raises(JobStateError, match="revision_conflict"):
        service.cancel_job(CancelJobCommandDTO(pending_id, pending.summary.revision, "operator", "user_request"))

    running_id = _job(session_factory, clock, idempotency_key="running-cancel")
    claim = _claim(service)
    assert claim.job_public_id == running_id
    running = service.get_job(running_id)
    requested = service.cancel_job(
        CancelJobCommandDTO(running_id, running.summary.revision, "operator", "user_request")
    )
    assert requested.state is JobState.RUNNING
    assert service.cancellation("00000000-0000-4000-8000-000000000901", claim).requested is True
    with pytest.raises(JobStateError, match="cancellation_requested"):
        service.settle_success("00000000-0000-4000-8000-000000000901", claim, "opaque-result")
    with pytest.raises(JobStateError, match="owned_execution_mismatch"):
        service.settle_cancelled(
            "00000000-0000-4000-8000-000000000901",
            claim,
            OwnedExecutionSettlementDTO(
                "00000000-0000-4000-8000-000000000999", True
            ),
        )
    service.settle_cancelled(
        "00000000-0000-4000-8000-000000000901",
        claim,
        OwnedExecutionSettlementDTO(claim.execution_public_id, True),
    )
    assert service.get_job(running_id).summary.state is JobState.CANCELLED

    uncertain_id = _job(session_factory, clock, idempotency_key="uncertain-cancel")
    uncertain_claim = _claim(service)
    uncertain_detail = service.get_job(uncertain_id)
    service.cancel_job(
        CancelJobCommandDTO(uncertain_id, uncertain_detail.summary.revision, "operator", "user_request")
    )
    service.settle_cancelled(
        "00000000-0000-4000-8000-000000000901",
        uncertain_claim,
        OwnedExecutionSettlementDTO(uncertain_claim.execution_public_id, False),
    )
    failed = service.get_job(uncertain_id).summary
    assert failed.state is JobState.FAILED
    assert failed.error is not None and failed.error.code == "owned_child_settlement_failed"


def test_generic_failure_cannot_drop_a_cancellation_requested_owned_tree(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch generic handler failure clearing ownership without affirmative child-tree settlement."""
    job_id = _job(session_factory, clock, idempotency_key="cancelled-generic-failure")
    service = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000901"
    claim = _claim(service, worker)
    detail = service.get_job(job_id)
    service.cancel_job(CancelJobCommandDTO(job_id, detail.summary.revision, "operator", "user_request"))

    with pytest.raises(Exception) as caught:
        service.settle_failure(
            worker,
            claim,
            StageExecutionOutcomeDTO("failed", None, "private_failure", "private failure", False),
        )
    assert isinstance(caught.value, contracts.JobLifecycleError)
    assert caught.value.code.value == "cancellation_requested"
    with session_factory() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_id))
        assert row is not None and row.status == "running"
        assert row.lease_execution_public_id == claim.execution_public_id
        assert row.cancel_requested_at is not None


def test_two_service_cancel_success_race_has_one_domain_winner_and_retains_owned_tree(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch SQLITE_BUSY_SNAPSHOT or a lost lease in the cancel-versus-success race."""
    job_id = _job(session_factory, clock, idempotency_key="cancel-success-race")
    first = _service(session_factory, clock, tmp_path)
    second = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000951"
    claim = _claim(first, worker)
    result_id = first.persist_stage_result(claim, {"stable": "output"})
    revision = first.get_job(job_id).summary.revision
    calls = (
        lambda: first.cancel_job(CancelJobCommandDTO(job_id, revision, "operator", "user_request")),
        lambda: second.settle_success(worker, claim, result_id),
    )
    with _barrier_job_reads(session_factory), ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(_capture, calls))

    domain_error = getattr(contracts, "JobLifecycleError", JobStateError)
    assert not any(isinstance(value, OperationalError) for value in outcomes), outcomes
    assert all(value is None or isinstance(value, (contracts.JobMutationDTO, domain_error)) for value in outcomes), outcomes
    with session_factory() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_id))
        assert row is not None and row.status in {"running", "succeeded"}
        if row.status == "running":
            assert row.cancel_requested_at is not None
            assert row.lease_execution_public_id == claim.execution_public_id


def test_two_service_heartbeat_recovery_and_retry_races_return_only_domain_results(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch raw SQLite snapshot errors during heartbeat/recovery and optimistic retry races."""
    _job(session_factory, clock, idempotency_key="heartbeat-recovery-race", max_attempts=3)
    owner = "00000000-0000-4000-8000-000000000952"
    claim_service = _service(session_factory, clock, tmp_path)
    claim = _claim(claim_service, owner)
    heartbeat_clock = MutableClock(clock.current + timedelta(seconds=29))
    recovery_clock = MutableClock(clock.current + timedelta(seconds=31))
    heartbeat_service = _service(session_factory, heartbeat_clock, tmp_path)
    recovery_service = _service(session_factory, recovery_clock, tmp_path)
    calls = (
        lambda: heartbeat_service.heartbeat(owner, claim, 30),
        lambda: recovery_service.claim_next("00000000-0000-4000-8000-000000000953", 30),
    )
    with _barrier_job_reads(session_factory), ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(_capture, calls))
    domain_error = getattr(contracts, "JobLifecycleError", JobStateError)
    assert not any(isinstance(value, OperationalError) for value in outcomes), outcomes
    assert all(value is None or isinstance(value, (WorkerLeaseDTO, domain_error)) for value in outcomes), outcomes

    retry_id = _job(session_factory, clock, idempotency_key="retry-race")
    with session_factory.begin() as session:
        row = session.scalar(select(Job).where(Job.public_id == retry_id))
        assert row is not None
        row.status = "failed"
        row.attempt_count = 1
        row.retryable = True
        row.completed_at = clock.current
    retry_revision = claim_service.get_job(retry_id).summary.revision
    retry_calls = (
        lambda: claim_service.retry_job(RetryJobCommandDTO(retry_id, retry_revision)),
        lambda: recovery_service.retry_job(RetryJobCommandDTO(retry_id, retry_revision)),
    )
    with _barrier_job_reads(session_factory), ThreadPoolExecutor(max_workers=2) as executor:
        retry_outcomes = tuple(executor.map(_capture, retry_calls))
    assert not any(isinstance(value, OperationalError) for value in retry_outcomes), retry_outcomes
    assert sum(isinstance(value, contracts.JobMutationDTO) for value in retry_outcomes) == 1
    assert sum(isinstance(value, domain_error) for value in retry_outcomes) == 1, retry_outcomes


def test_sqlite_writer_contention_is_bounded_and_mapped_to_public_domain_error(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch the configured SQLite busy timeout escaping as a driver OperationalError."""
    _job(session_factory, clock, idempotency_key="busy-writer")
    engine = session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    holder = engine.raw_connection()
    try:
        holder.execute("BEGIN IMMEDIATE")
        service = _service(session_factory, clock, tmp_path)
        with pytest.raises(contracts.JobLifecycleError) as caught:
            service.claim_next("00000000-0000-4000-8000-000000000955", 30)
        assert caught.value.code is contracts.JobLifecycleErrorCode.LIFECYCLE_BUSY
    finally:
        holder.rollback()
        holder.close()


def test_expiry_shutdown_retry_and_dependency_cancellation_are_distinct(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch stale-owner settlement, attempt reset, shutdown overclaim, or collapsed dependency terminal reasons."""
    parent_id = _job(session_factory, clock, idempotency_key="expiry-parent", max_attempts=2)
    child_id = _job(session_factory, clock, "report", idempotency_key="expiry-child")
    coordinator = JobCoordinator(session_factory, clock=clock)
    coordinator.add_dependency(child_id, parent_id)
    service = _service(session_factory, clock, tmp_path, ("parse", "report"))
    old_claim = _claim(service)
    clock.advance(seconds=31)
    new_claim = service.claim_next("00000000-0000-4000-8000-000000000932", 30)
    assert new_claim is not None and new_claim.job_public_id == parent_id and new_claim.attempt_count == 2
    with pytest.raises(JobStateError, match="lease_mismatch"):
        service.settle_failure(
            "00000000-0000-4000-8000-000000000901",
            old_claim,
            StageExecutionOutcomeDTO("failed", None, "stale", "stale", False),
        )
    service.release_after_shutdown(
        "00000000-0000-4000-8000-000000000932",
        new_claim,
        OwnedExecutionSettlementDTO(new_claim.execution_public_id, True),
    )
    exhausted = service.get_job(parent_id).summary
    assert exhausted.state is JobState.FAILED and exhausted.error is not None
    assert exhausted.error.code == "worker_shutdown"

    cancelled_parent = _job(session_factory, clock, idempotency_key="cancel-parent")
    cancelled_child = _job(session_factory, clock, "report", idempotency_key="cancel-child")
    coordinator.add_dependency(cancelled_child, cancelled_parent)
    detail = service.get_job(cancelled_parent)
    service.cancel_job(
        CancelJobCommandDTO(cancelled_parent, detail.summary.revision, "operator", "user_request")
    )
    descendant = service.get_job(cancelled_child).summary
    assert descendant.state is JobState.FAILED
    assert descendant.error is not None and descendant.error.code == "dependency_cancelled"


def test_shutdown_release_covers_retry_cancel_and_uncertain_owned_tree_outcomes(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Prove shutdown never releases ownership without an exact affirmative settlement."""
    service = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000956"

    retry_id = _job(session_factory, clock, idempotency_key="shutdown-retry")
    retry_claim = _claim(service, worker)
    service.release_after_shutdown(
        worker,
        retry_claim,
        OwnedExecutionSettlementDTO(retry_claim.execution_public_id, True),
    )
    retry = service.get_job(retry_id).summary
    assert retry.state is JobState.PENDING and retry.error is not None
    assert retry.error.code is contracts.PublicJobReasonCode.WORKER_SHUTDOWN
    service.cancel_job(CancelJobCommandDTO(retry_id, retry.revision, "operator", "user_request"))

    cancel_id = _job(session_factory, clock, idempotency_key="shutdown-cancel")
    cancel_claim = _claim(service, worker)
    cancel_detail = service.get_job(cancel_id)
    service.cancel_job(CancelJobCommandDTO(cancel_id, cancel_detail.summary.revision, "operator", "user_request"))
    service.release_after_shutdown(
        worker,
        cancel_claim,
        OwnedExecutionSettlementDTO(cancel_claim.execution_public_id, True),
    )
    assert service.get_job(cancel_id).summary.state is JobState.CANCELLED

    uncertain_id = _job(session_factory, clock, idempotency_key="shutdown-uncertain")
    uncertain_claim = _claim(service, worker)
    service.release_after_shutdown(
        worker,
        uncertain_claim,
        OwnedExecutionSettlementDTO(uncertain_claim.execution_public_id, False),
    )
    uncertain = service.get_job(uncertain_id).summary
    assert uncertain.state is JobState.FAILED and uncertain.error is not None
    assert uncertain.error.code is contracts.PublicJobReasonCode.OWNED_CHILD_SETTLEMENT_FAILED


def test_exhausted_pending_job_is_terminalized_before_claim_without_exceeding_attempt_check(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch claim incrementing an accepted pending attempt at its maximum into an integrity failure."""
    job_id = _job(session_factory, clock, idempotency_key="exhausted-pending", max_attempts=1)
    with session_factory.begin() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_id))
        assert row is not None
        row.attempt_count = 1
    service = _service(session_factory, clock, tmp_path)
    assert service.claim_next("00000000-0000-4000-8000-000000000954", 30) is None
    detail = service.get_job(job_id)
    assert detail.summary.state is JobState.FAILED
    assert detail.summary.error is not None and detail.summary.error.code == "attempts_exhausted"


def test_durable_result_is_reused_after_settlement_loss_without_another_attempt(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch handler re-execution after an immutable result committed but job settlement was lost."""
    job_id = _job(session_factory, clock, idempotency_key="crash-safe-result")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    result_id = service.persist_stage_result(claim, {"stable": "output"})
    with session_factory() as session:
        result = session.scalar(select(JobStageResult).where(JobStageResult.public_id == result_id))
        assert result is not None and result.output_json == {"stable": "output"}
    clock.advance(seconds=31)
    assert service.claim_next("00000000-0000-4000-8000-000000000942", 30) is None
    settled = service.get_job(job_id).summary
    assert settled.state is JobState.SUCCEEDED
    assert settled.attempt_count == 1
    assert settled.result_public_id == result_id


def test_cancellation_wins_over_durable_result_recovery_after_lease_expiry(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch crash-result recovery settling success after a cancellation request has won."""
    job_id = _job(session_factory, clock, idempotency_key="cancelled-crash-safe-result")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    result_id = service.persist_stage_result(claim, {"stable": "output"})
    detail = service.get_job(job_id)
    service.cancel_job(CancelJobCommandDTO(job_id, detail.summary.revision, "operator", "user_request"))

    clock.advance(seconds=31)
    assert service.claim_next("00000000-0000-4000-8000-000000000943", 30) is None
    settled = service.get_job(job_id).summary
    assert settled.state is JobState.FAILED
    assert settled.error is not None and settled.error.code == "owned_child_settlement_failed"
    assert settled.result_public_id == result_id


def test_events_and_logs_are_path_free_immutable_redacted_bounded_and_ownership_checked(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch unsafe event fields, locator exposure, unbounded reads, or raw supervisor output publication."""
    first_id = _job(session_factory, clock, idempotency_key="logs-first")
    second_id = _job(session_factory, clock, idempotency_key="logs-second")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    payload = (
        f"\x1b[31mfailed at {tmp_path} https://user:pass@example.invalid --token TOP_SECRET\x1b[0m\n"
        "Traceback (most recent call last):\n  File secret.py, line 1\nValueError: TOP_SECRET\n"
        + ("x" * 70000)
    ).encode("utf-8")
    reference = service.publish_log(
        "00000000-0000-4000-8000-000000000901", claim, "stderr", 0, payload
    )
    assert reference.job_public_id == first_id
    assert not hasattr(reference, "locator")
    chunk = service.read_log(JobLogQueryDTO(first_id, reference.public_id, 0, 65536))
    assert chunk.state == "available"
    assert len(chunk.content.encode("utf-8")) <= 65536
    assert chunk.next_offset is not None
    assert "\x1b" not in chunk.content
    assert str(tmp_path) not in chunk.content
    assert "user:pass" not in chunk.content
    assert "TOP_SECRET" not in chunk.content
    assert "Traceback" not in chunk.content
    with pytest.raises(JobStateError, match="log_ownership_mismatch"):
        service.read_log(JobLogQueryDTO(second_id, reference.public_id, 0, 1024))
    with session_factory() as session:
        event = session.scalar(
            select(JobEvent).where(
                JobEvent.job_id == select(Job.id).where(Job.public_id == first_id).scalar_subquery()
            )
        )
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == reference.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        assert event is not None
        assert event.reason_code is None
        assert snapshot is not None and asset is not None
        assert not Path(asset.relative_path).is_absolute()


def test_authenticated_log_pages_survive_restart_and_reject_same_size_requested_chunk_tamper(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch returning mutated requested bytes under the immutable snapshot identity after restart."""
    _job(session_factory, clock, idempotency_key="authenticated-requested-chunk")
    worker = "00000000-0000-4000-8000-000000000901"
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service, worker)
    payload = b"safe-line\n" * 2_000
    reference = service.publish_log(worker, claim, "stdout", 0, payload)
    with session_factory() as session:
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == reference.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        assert snapshot is not None and asset is not None
        assert snapshot.integrity_version == _LOG_INTEGRITY_VERSION
        assert snapshot.integrity_chunk_size == _LOG_INTEGRITY_CHUNK_SIZE
        assert len(snapshot.integrity_root_sha256) == 64
        assert snapshot.byte_count == len(payload)
        assert asset.size_bytes > snapshot.byte_count
        managed_path = tmp_path / asset.relative_path

    offset = 5_000
    limit = 200
    restarted = _service(session_factory, clock, tmp_path)
    exact = restarted.read_log(JobLogQueryDTO(claim.job_public_id, reference.public_id, offset, limit))
    assert exact.state == "available"
    assert exact.content == payload[offset : offset + limit].decode("utf-8")

    _mutate_byte_and_restore_mtime(managed_path, offset + 20)
    unavailable = _service(session_factory, clock, tmp_path).read_log(
        JobLogQueryDTO(claim.job_public_id, reference.public_id, offset, limit)
    )
    assert unavailable.state == "unavailable"
    assert unavailable.content == ""
    assert unavailable.next_offset is None


def test_unrelated_chunk_corruption_preserves_an_exact_good_page_and_rejects_the_bad_page(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Freeze lazy per-page integrity: unrelated damage cannot forge or poison exact returned bytes."""
    _job(session_factory, clock, idempotency_key="authenticated-unrelated-chunk")
    worker = "00000000-0000-4000-8000-000000000901"
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service, worker)
    payload = b"safe-line\n" * 2_000
    reference = service.publish_log(worker, claim, "stderr", 0, payload)
    with session_factory() as session:
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == reference.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        assert snapshot is not None and asset is not None
        managed_path = tmp_path / asset.relative_path
    corrupt_offset = _LOG_INTEGRITY_CHUNK_SIZE * 2 + 100
    _mutate_byte_and_restore_mtime(managed_path, corrupt_offset)

    restarted = _service(session_factory, clock, tmp_path)
    good = restarted.read_log(JobLogQueryDTO(claim.job_public_id, reference.public_id, 0, 200))
    assert good.state == "available"
    assert good.content == payload[:200].decode("utf-8")
    bad = restarted.read_log(JobLogQueryDTO(claim.job_public_id, reference.public_id, corrupt_offset, 200))
    assert bad.state == "unavailable"
    assert bad.content == ""


@pytest.mark.parametrize("forgery", ("checkpoint", "proof", "root"))
def test_authenticated_log_rejects_forged_checkpoint_proof_or_persisted_root(
    forgery: str,
    session_factory: sessionmaker[Session],
    clock: MutableClock,
    tmp_path: Path,
) -> None:
    """Catch accepting attacker-selected redaction state or Merkle evidence for an otherwise valid page."""
    _job(session_factory, clock, idempotency_key=f"authenticated-{forgery}")
    worker = "00000000-0000-4000-8000-000000000901"
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service, worker)
    payload = b"safe-line\n" * 2_000
    reference = service.publish_log(worker, claim, "supervisor", 0, payload)
    with session_factory.begin() as session:
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == reference.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        job = session.scalar(select(Job).where(Job.public_id == claim.job_public_id))
        assert snapshot is not None and asset is not None and job is not None
        managed_path = tmp_path / asset.relative_path
        if forgery == "root":
            forged_id = str(uuid4())
            session.add(
                JobLogSnapshot(
                    public_id=forged_id,
                    job_id=job.id,
                    attempt_count=snapshot.attempt_count,
                    label=snapshot.label,
                    sequence=1,
                    managed_asset_id=asset.id,
                    media_type=snapshot.media_type,
                    byte_count=snapshot.byte_count,
                    redaction_version=snapshot.redaction_version,
                    integrity_version=snapshot.integrity_version,
                    integrity_root_sha256="0" * 64,
                    integrity_chunk_size=snapshot.integrity_chunk_size,
                    created_at=clock.current,
                )
            )
            log_public_id = forged_id
        else:
            log_public_id = reference.public_id
            chunk_count = max(1, (snapshot.byte_count + snapshot.integrity_chunk_size - 1) // snapshot.integrity_chunk_size)
            capacity = 1 << (chunk_count - 1).bit_length()
            if forgery == "checkpoint":
                tamper_offset = snapshot.byte_count
            else:
                nodes_start = snapshot.byte_count + chunk_count
                first_leaf_sibling_index = capacity
                tamper_offset = nodes_start + first_leaf_sibling_index * 64
            _mutate_byte_and_restore_mtime(managed_path, tamper_offset)

    page = _service(session_factory, clock, tmp_path).read_log(
        JobLogQueryDTO(claim.job_public_id, log_public_id, 0, 200)
    )
    assert page.state == "unavailable"
    assert page.content == ""


def test_public_errors_and_event_reasons_are_closed_and_private_handler_codes_stay_private(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch arbitrary cancellation/handler codes crossing the ORM-free Web boundary."""
    job_id = _job(session_factory, clock, idempotency_key="closed-public-reasons")
    with pytest.raises(ValueError):
        CancelJobCommandDTO(job_id, 0, "operator", "private arbitrary reason")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    service.settle_failure(
        "00000000-0000-4000-8000-000000000901",
        claim,
        StageExecutionOutcomeDTO(
            "failed",
            None,
            "private_handler_code",
            r"private failure at C:\secret\worker.log",
            False,
        ),
    )
    detail = service.get_job(job_id)
    assert detail.summary.error is not None
    assert detail.summary.error.code == "stage_failed"
    assert "private_handler_code" not in repr(detail)
    assert "secret" not in repr(detail).casefold()
    assert detail.events[-1].reason_code == "stage_failed"


def test_contracts_validate_public_ids_states_outcomes_and_export_frozen_domain_errors(
    clock: MutableClock,
) -> None:
    """Catch malformed DTOs and an ORM-coupled private exception escaping the public lifecycle port."""
    assert JobStateError is contracts.JobLifecycleError
    assert contracts.JobLifecycleFailure.__dataclass_params__.frozen is True
    failure = contracts.JobLifecycleError("unknown_job", "unknown job")
    assert failure.failure == contracts.JobLifecycleFailure(contracts.JobLifecycleErrorCode.UNKNOWN_JOB, "unknown job")
    with pytest.raises(ValueError):
        RetryJobCommandDTO("NOT-A-UUID", 0)
    with pytest.raises(ValueError):
        OwnedExecutionSettlementDTO("NOT-A-UUID", True)
    for invalid_tree_state in ("true", 1, 0, None):
        with pytest.raises(TypeError):
            OwnedExecutionSettlementDTO(
                "00000000-0000-4000-8000-000000000002",
                invalid_tree_state,  # type: ignore[arg-type]
            )
    with pytest.raises(ValueError):
        StageExecutionOutcomeDTO("succeeded", None, "unexpected", "unexpected", False)
    with pytest.raises(ValueError):
        StageExecutionOutcomeDTO("unknown", None, "stable_code", "safe message", False)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        contracts.JobLogChunkDTO("unknown", "", None)  # type: ignore[arg-type]
    assert tuple(value.value for value in contracts.JobEventKind) == (
        "claimed",
        "progress",
        "cancel_requested",
        "cancelled",
        "succeeded",
        "failed",
        "retry_requested",
        "lease_expired",
        "worker_shutdown",
        "dependency_failed",
        "dependency_cancelled",
        "result_reused",
    )
    with pytest.raises(TypeError):
        JobQueryDTO(states=[JobState.PENDING])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        JobProgressDTO(0, 1, " items ", clock.current)
    with pytest.raises(ValueError):
        WorkerLeaseDTO(
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            " parse ",
            1,
            1,
            "token",
            clock.current,
        )
    with pytest.raises(ValueError):
        contracts.WorkerCancellationDTO(True, None)
    with pytest.raises(ValueError):
        contracts.JobLogChunkDTO("available", "x" * 65_537, None)
    with pytest.raises(ValueError):
        JobQueryDTO(limit=0)
    with pytest.raises(ValueError):
        JobLogQueryDTO(
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            -1,
            1,
        )
    for unsafe_utf8_limit in (1, 2, 3):
        with pytest.raises(ValueError):
            JobLogQueryDTO(
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
                0,
                unsafe_utf8_limit,
            )
    summary = contracts.JobSummaryDTO(
        "00000000-0000-4000-8000-000000000001",
        None,
        "parse",
        "component-v1",
        JobState.PENDING,
        0,
        0,
        1,
        True,
        clock.current,
        None,
        None,
        False,
        None,
        None,
        None,
    )
    with pytest.raises(ValueError):
        replace(summary, max_attempts=0)
    for field_name, invalid_integer in (
        ("revision", True),
        ("attempt_count", 0.0),
        ("max_attempts", 1.0),
    ):
        with pytest.raises(TypeError):
            replace(summary, **{field_name: invalid_integer})
    with pytest.raises(TypeError):
        JobProgressDTO(True, 1, "items", clock.current)
    with pytest.raises(TypeError):
        JobLogQueryDTO(
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            False,
            1,
        )


def test_worker_public_identifier_is_an_exact_lowercase_uuid(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch accepting a display name or normalized variant as a public worker identity."""
    _job(session_factory, clock, idempotency_key="worker-identity")
    service = _service(session_factory, clock, tmp_path)
    for invalid_worker in (
        "worker-a",
        " 00000000-0000-4000-8000-000000000901 ",
        "00000000-0000-4000-8000-00000000090A",
    ):
        with pytest.raises(ValueError, match="worker_public_id"):
            service.claim_next(invalid_worker, 30)
    assert service.claim_next("00000000-0000-4000-8000-000000000901", 30) is not None


def test_log_registration_uses_real_cas_path_validates_existing_asset_and_sequences_atomically(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch fabricated locators, unsafe digest reuse, and non-domain concurrent sequence conflicts."""
    _job(session_factory, clock, idempotency_key="real-log-registration")
    first = _service(session_factory, clock, tmp_path)
    second = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000901"
    claim = _claim(first, worker)
    reference = first.publish_log(worker, claim, "stdout", 0, b"first log")
    with session_factory() as session:
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == reference.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        assert asset is not None
        expected = (tmp_path / "managed-logs" / asset.sha256[:2] / asset.sha256).relative_to(tmp_path).as_posix()
        assert asset.relative_path == expected

    race_calls = (
        lambda: first.publish_log(worker, claim, "stderr", 0, b"concurrent log"),
        lambda: second.publish_log(worker, claim, "stderr", 0, b"concurrent log"),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(_capture, race_calls))
    assert sum(isinstance(value, contracts.JobLogReferenceDTO) for value in outcomes) == 1
    assert sum(isinstance(value, contracts.JobLifecycleError) for value in outcomes) == 1

    conflict_id = _job(session_factory, clock, idempotency_key="log-role-conflict")
    conflict_claim = first.claim_next(worker, 30)
    assert conflict_claim is not None and conflict_claim.job_public_id == conflict_id
    wrong_role_container, _root = _authenticated_log_container(b"wrong role")
    stored = ContentAddressedStore(tmp_path / "managed-logs").store_bytes(wrong_role_container)
    with session_factory.begin() as session:
        session.add(
            ManagedAsset(
                public_id="00000000-0000-4000-8000-000000000997",
                sha256=stored.sha256,
                kind="telemetry",
                relative_path=stored.path.relative_to(tmp_path).as_posix(),
                size_bytes=stored.size,
                media_type="application/json",
                created_at=clock.current,
            )
        )
    with pytest.raises(Exception) as caught:
        first.publish_log(worker, conflict_claim, "stderr", 0, b"wrong role")
    assert isinstance(caught.value, contracts.JobLifecycleError)


def test_failed_log_publisher_cannot_delete_a_racing_publishers_registered_blob(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch unlinking a digest after another publisher durably registered the same real CAS object."""
    _job(session_factory, clock, idempotency_key="cleanup-race")
    cleanup_entered = Event()
    winner_registered = Event()
    delegate = ContentAddressedStore(tmp_path / "managed-logs")
    coordinated_store = _CleanupRaceStore(delegate, cleanup_entered, winner_registered)
    service_args = {
        "registered_stages": ("parse",),
        "clock": clock,
        "retry_base_delay": timedelta(0),
        "retry_max_delay": timedelta(0),
        "log_store": coordinated_store,
        "log_data_root": tmp_path,
    }
    first = JobLifecycleService(session_factory, **service_args)  # type: ignore[arg-type]
    second = JobLifecycleService(session_factory, **service_args)  # type: ignore[arg-type]
    worker = "00000000-0000-4000-8000-000000000901"
    claim = _claim(first, worker)
    payload = b"same immutable bytes"

    with ThreadPoolExecutor(max_workers=2) as executor:
        loser_future = executor.submit(
            _capture,
            lambda: first.publish_log(worker, claim, "stderr", 2, payload),
        )
        assert cleanup_entered.wait(timeout=3)
        winner_future = executor.submit(second.publish_log, worker, claim, "stderr", 0, payload)
        try:
            winner = winner_future.result(timeout=3)
        finally:
            winner_registered.set()
        loser = loser_future.result(timeout=3)

    assert isinstance(winner, contracts.JobLogReferenceDTO)
    assert isinstance(loser, contracts.JobLifecycleError)
    expected_container, _root = _authenticated_log_container(payload)
    stored = delegate.verify(hashlib.sha256(expected_container).hexdigest())
    assert stored.path.is_file()
    with session_factory() as session:
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == winner.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        assert asset is not None and asset.sha256 == stored.sha256


def test_slow_log_normalization_and_cas_write_do_not_block_unrelated_lifecycle_mutations(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch holding the global SQLite writer reservation across redaction, file write, or fsync."""
    first_id = _job(session_factory, clock, idempotency_key="slow-log")
    second_id = _job(session_factory, clock, idempotency_key="unrelated-heartbeat")
    pending_id = _job(session_factory, clock, idempotency_key="unrelated-cancel")
    normal = _service(session_factory, clock, tmp_path)
    publisher_claim = _claim(normal, "00000000-0000-4000-8000-000000000901")
    heartbeat_claim = _claim(normal, "00000000-0000-4000-8000-000000000902")
    assert publisher_claim.job_public_id == first_id
    assert heartbeat_claim.job_public_id == second_id
    entered = Event()
    release = Event()
    blocking_store = _BlockingStore(ContentAddressedStore(tmp_path / "managed-logs"), entered, release)
    publisher = JobLifecycleService(
        session_factory,
        registered_stages=("parse",),
        clock=clock,
        retry_base_delay=timedelta(0),
        retry_max_delay=timedelta(0),
        log_store=blocking_store,  # type: ignore[arg-type]
        log_data_root=tmp_path,
    )
    mutator = _service(session_factory, clock, tmp_path)
    pending = mutator.get_job(pending_id)

    with ThreadPoolExecutor(max_workers=3) as executor:
        publish_future = executor.submit(
            publisher.publish_log,
            "00000000-0000-4000-8000-000000000901",
            publisher_claim,
            "stdout",
            0,
            b"slow publication",
        )
        assert entered.wait(timeout=2)
        heartbeat_future = executor.submit(
            mutator.heartbeat,
            "00000000-0000-4000-8000-000000000902",
            heartbeat_claim,
            60,
        )
        cancel_future = executor.submit(
            mutator.cancel_job,
            CancelJobCommandDTO(pending_id, pending.summary.revision, "operator", "user_request"),
        )
        try:
            heartbeat = heartbeat_future.result(timeout=1)
            cancelled = cancel_future.result(timeout=1)
        finally:
            release.set()
        assert publish_future.result(timeout=3).job_public_id == first_id

    assert heartbeat.lease_expires_at == clock.current + timedelta(seconds=60)
    assert cancelled.state is JobState.CANCELLED


def test_log_read_streams_bounded_redacted_utf8_and_strips_complete_traceback_blocks(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch whole-file reads, post-redaction byte overflow, or leaked chained traceback locals."""
    _job(session_factory, clock, idempotency_key="bounded-log-read")
    service = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000901"
    claim = _claim(service, worker)
    payload = (
        "before\nTraceback (most recent call last):\n"
        "  File worker.py, line 1, in run\n"
        "    local_variable = 'must-disappear'\n"
        "ValueError: first\n\n"
        "The above exception was the direct cause of the following exception:\n\n"
        "Traceback (most recent call last):\n"
        "  File supervisor.py, line 2, in main\n"
        "    child_secret = 'also-disappear'\n"
        "RuntimeError: final\n"
        "after\n"
        + ("é" * 70_000)
    ).encode("utf-8")
    reference = service.publish_log(worker, claim, "supervisor", 0, payload)

    def reject_whole_file_read(_path: Path) -> bytes:
        raise AssertionError("job log reads must not materialize the whole managed object")

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file_read)
    chunk = service.read_log(JobLogQueryDTO(claim.job_public_id, reference.public_id, 0, 65_536))
    assert chunk.state == "available"
    assert len(chunk.content.encode("utf-8")) <= 65_536
    assert "must-disappear" not in chunk.content
    assert "also-disappear" not in chunk.content
    assert "direct cause" not in chunk.content

    rotated = service.read_log(
        JobLogQueryDTO(claim.job_public_id, reference.public_id, reference.byte_count, 1024)
    )
    assert rotated.state == "rotated"
    with session_factory() as session:
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == reference.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        assert asset is not None
        managed_path = tmp_path / asset.relative_path
    managed_path.unlink()
    unavailable = service.read_log(JobLogQueryDTO(claim.job_public_id, reference.public_id, 0, 1024))
    assert unavailable.state == "unavailable"


def test_log_pagination_consumes_complete_utf8_and_redacts_patterns_across_chunk_boundaries(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch split code points, duplicated replacement characters, or slice-local defense-in-depth redaction."""
    job_id = _job(session_factory, clock, idempotency_key="boundary-log-read")
    raw = bytearray(b"safe-start\n")

    def crossing(value: bytes) -> None:
        raw.extend(b" " * ((7 - (len(raw) % 9)) % 9))
        raw.extend(value)
        raw.extend(b"\n")

    crossing(b"\x1b[31mred\x1b[0m")
    crossing(b"TOP_SECRET")
    crossing(b"https://user:pass@example.invalid/private")
    crossing(b"--token TOP_SECRET")
    crossing(str(tmp_path).encode("utf-8"))
    crossing(
        b"Traceback (most recent call last):\n"
        b"  File private_worker.py, line 1, in run\n"
        b"    local_secret = 'TOP_SECRET'\n"
        b"RuntimeError: private\n"
    )
    crossing("😀".encode())
    raw.extend(b"safe-end\n")
    log_public_id, _stored = _register_authenticated_log(
        session_factory,
        clock,
        tmp_path,
        job_id,
        bytes(raw),
    )
    service = _service(session_factory, clock, tmp_path)
    chunks: list[str] = []
    offset: int | None = 0
    previous = 0
    for _ in range(200):
        assert offset is not None
        chunk = service.read_log(JobLogQueryDTO(job_id, log_public_id, offset, 13))
        assert chunk.state == "available"
        assert len(chunk.content.encode("utf-8")) <= 13
        chunks.append(chunk.content)
        consumed_to = len(raw) if chunk.next_offset is None else chunk.next_offset
        bytes(raw[previous:consumed_to]).decode("utf-8", errors="strict")
        assert consumed_to > previous
        previous = consumed_to
        offset = chunk.next_offset
        if offset is None:
            break
    else:
        pytest.fail("log pagination did not terminate")

    reconstructed = "".join(chunks)
    assert "safe-start" in reconstructed and "safe-end" in reconstructed
    assert reconstructed.count("😀") == 1
    assert "�" not in reconstructed
    assert "\x1b" not in reconstructed
    assert "TOP_SECRET" not in reconstructed
    assert "user:pass" not in reconstructed
    assert str(tmp_path) not in reconstructed
    assert "Traceback" not in reconstructed
    assert "local_secret" not in reconstructed


def test_four_byte_minimum_log_page_reconstructs_every_utf8_scalar_once(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch continuation advancing past a scalar that could not fit in the requested public page."""
    _job(session_factory, clock, idempotency_key="minimum-utf8-page")
    service = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000901"
    claim = _claim(service, worker)
    expected = "a😀b😀c"
    reference = service.publish_log(worker, claim, "stdout", 0, expected.encode())
    chunks: list[str] = []
    offset: int | None = 0
    for _ in range(10):
        assert offset is not None
        page = service.read_log(JobLogQueryDTO(claim.job_public_id, reference.public_id, offset, 4))
        assert page.state == "available"
        chunks.append(page.content)
        offset = page.next_offset
        if offset is None:
            break
    else:
        pytest.fail("minimum-width UTF-8 pagination did not terminate")
    assert "".join(chunks) == expected


def test_read_redaction_bounds_configured_secrets_and_unbounded_sensitive_lines(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch fixed-overlap leakage from long secrets, credentials, paths, commands, or OSC control strings."""
    job_id = _job(session_factory, clock, idempotency_key="long-boundary-redaction")
    secret = "s" * 12_000
    sensitive_lines = (
        secret,
        "https://user:" + ("q" * 12_000) + "@example.invalid/private",
        "--token " + ("t" * 12_000),
        "C:\\" + ("p" * 12_000) + "\\private.log",
        "\x1b]0;" + (("o" * 1_000 + "\n") * 30) + "\x07",
    )
    safe_lines = tuple(f"safe-{index}" for index in range(len(sensitive_lines) + 1))
    raw_parts: list[str] = []
    for index, sensitive in enumerate(sensitive_lines):
        raw_parts.extend((safe_lines[index], sensitive))
    raw_parts.append(safe_lines[-1])
    raw = ("\n".join(raw_parts) + "\n").encode()
    store = ContentAddressedStore(tmp_path / "managed-logs")
    log_public_id, _stored = _register_authenticated_log(
        session_factory,
        clock,
        tmp_path,
        job_id,
        raw,
        label="stderr",
    )
    service = JobLifecycleService(
        session_factory,
        registered_stages=("parse",),
        clock=clock,
        log_store=store,
        log_data_root=tmp_path,
        redaction_values=(secret,),
    )
    with pytest.raises(ValueError, match="redaction"):
        JobLifecycleService(
            session_factory,
            registered_stages=("parse",),
            clock=clock,
            redaction_values=("x" * 16_385,),
        )

    chunks: list[str] = []
    offset: int | None = 0
    for _ in range(100):
        assert offset is not None
        page = service.read_log(JobLogQueryDTO(job_id, log_public_id, offset, 1_024))
        assert page.state == "available"
        assert len(page.content.encode()) <= 1_024
        chunks.append(page.content)
        offset = page.next_offset
        if offset is None:
            break
    else:
        pytest.fail("long sensitive log pagination did not terminate")

    reconstructed = "".join(chunks)
    positions = [reconstructed.index(value) for value in safe_lines]
    assert positions == sorted(positions)
    assert all(reconstructed.count(value) == 1 for value in safe_lines)
    for leaked_run in ("s" * 128, "q" * 128, "t" * 128, "p" * 128, "o" * 128):
        assert leaked_run not in reconstructed
    assert "\x1b" not in reconstructed


def test_authenticated_osc_checkpoints_survive_restart_cache_pressure_and_concurrent_random_pages(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch direct-offset multiline OSC leakage after restart, eviction pressure, or concurrent reads."""
    job_id = _job(session_factory, clock, idempotency_key="durable-osc-checkpoints")
    safe_after = b"safe-after-one\nsafe-after-two\n"
    osc_body = (b"PRIVATE-OSC-PAYLOAD-" + b"x" * 79 + b"\n") * 400
    raw = b"safe-before\n\x1b]0;" + osc_body + b"\x07" + safe_after
    log_public_id, _stored = _register_authenticated_log(
        session_factory,
        clock,
        tmp_path,
        job_id,
        raw,
        label="stderr",
    )
    osc_start = raw.index(b"PRIVATE-OSC-PAYLOAD")
    offsets = tuple(osc_start + 32 + ((index * 1_021) % 2_052) * 4 for index in range(2_052))
    assert max(offsets) + 4 < raw.index(b"\x07", osc_start)
    service = _service(session_factory, clock, tmp_path)

    def read_hidden(offset: int) -> contracts.JobLogChunkDTO:
        return service.read_log(JobLogQueryDTO(job_id, log_public_id, offset, 4))

    with ThreadPoolExecutor(max_workers=16) as executor:
        pages = tuple(executor.map(read_hidden, offsets))
    assert all(page.state == "available" and page.content == "" for page in pages)

    direct_offset = osc_start + len(osc_body) // 2
    restarted = _service(session_factory, clock, tmp_path)
    direct = restarted.read_log(JobLogQueryDTO(job_id, log_public_id, direct_offset, 65_536))
    assert direct.state == "available"
    assert direct.content == safe_after.decode("utf-8")
    assert "PRIVATE-OSC-PAYLOAD" not in direct.content
    assert "x" * 32 not in direct.content
    assert "\x1b" not in direct.content


def test_far_offset_log_read_has_constant_bounded_file_io(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch full-object hashing or traceback-state rescans from byte zero for every range request."""
    job_id = _job(session_factory, clock, idempotency_key="far-offset-budget")
    raw = b"safe-line\n" * 20_000
    log_public_id, stored = _register_authenticated_log(
        session_factory,
        clock,
        tmp_path,
        job_id,
        raw,
    )
    real_open = Path.open
    bytes_read = [0]

    def counted_open(path: Path, *args: object, **kwargs: object) -> Any:
        opened = real_open(path, *args, **kwargs)
        if path.resolve(strict=False) == stored.path:
            return _CountingReader(opened, bytes_read)
        return opened

    monkeypatch.setattr(Path, "open", counted_open)
    service = _service(session_factory, clock, tmp_path)
    page = service.read_log(JobLogQueryDTO(job_id, log_public_id, 150_000, 256))
    assert page.state == "available"
    assert page.content.startswith("safe-line\n")
    assert page.next_offset is not None and page.next_offset > 150_000
    assert bytes_read[0] <= 20_000


def test_stale_log_publisher_is_rejected_before_any_managed_bytes_are_written(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch a rejected publisher leaving an orphaned blob in the Analytics-managed log store."""
    _job(session_factory, clock, idempotency_key="stale-log-publisher")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    log_root = tmp_path / "managed-logs"
    before = tuple(path.relative_to(log_root) for path in log_root.rglob("*") if path.is_file())

    with pytest.raises(JobStateError, match="lease_mismatch"):
        service.publish_log(
            "00000000-0000-4000-8000-000000000999",
            claim,
            "stderr",
            0,
            b"must-not-be-published",
        )

    after = tuple(path.relative_to(log_root) for path in log_root.rglob("*") if path.is_file())
    assert after == before


def test_revisioned_retry_and_listing_expose_only_safe_snapshots(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch nonoptimistic retry, retrying terminal success, or a public list surface with privileged payloads."""
    job_id = _job(session_factory, clock, idempotency_key="retryable-failure")
    service = _service(session_factory, clock, tmp_path)
    with session_factory.begin() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_id))
        assert row is not None
        row.status = "failed"
        row.attempt_count = 1
        row.retryable = True
        row.completed_at = clock.current
        row.error_code = "temporary"
        row.error_message = "safe summary"
        row.error_details_json = {}
    detail = service.get_job(job_id)
    retried = service.retry_job(RetryJobCommandDTO(job_id, detail.summary.revision))
    assert retried.state is JobState.PENDING
    assert retried.attempt_count == 1
    with pytest.raises(JobStateError, match="revision_conflict"):
        service.retry_job(RetryJobCommandDTO(job_id, detail.summary.revision))
    page = service.list_jobs(JobQueryDTO(states=(JobState.PENDING,), limit=10))
    assert page.items[0].public_id == job_id
    assert not hasattr(page.items[0], "lease_token")
    assert not hasattr(page.items[0], "input_json")


def test_job_pagination_uses_the_created_at_and_id_composite_seek_key(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch duplicate/omitted rows when migrated creation timestamps are not insertion ordered."""
    identifiers = (
        _job(session_factory, clock, idempotency_key="cursor-newest"),
        _job(session_factory, clock, idempotency_key="cursor-oldest"),
        _job(session_factory, clock, idempotency_key="cursor-middle"),
    )
    with session_factory.begin() as session:
        rows = {
            row.public_id: row
            for row in session.scalars(select(Job).where(Job.public_id.in_(identifiers)))
        }
        rows[identifiers[0]].created_at = clock.current + timedelta(days=3)
        rows[identifiers[1]].created_at = clock.current + timedelta(days=1)
        rows[identifiers[2]].created_at = clock.current + timedelta(days=2)
    service = _service(session_factory, clock, tmp_path)
    first = service.list_jobs(JobQueryDTO(limit=2))
    second = service.list_jobs(JobQueryDTO(limit=2, after_public_id=first.next_public_id))
    assert tuple(item.public_id for item in first.items) == (identifiers[0], identifiers[2])
    assert tuple(item.public_id for item in second.items) == (identifiers[1],)


def test_mismatched_durable_result_identity_is_never_reused_or_settled(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch job-id-only result recovery or settlement across stage/component identity."""
    job_id = _job(session_factory, clock, idempotency_key="exact-result-identity")
    service = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000901"
    claim = _claim(service, worker)
    with session_factory.begin() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_id))
        assert row is not None
        result = JobStageResult(
            public_id="00000000-0000-4000-8000-000000000998",
            job_id=row.id,
            stage="wrong-stage",
            component_version="wrong-component",
            idempotency_key=row.idempotency_key,
            output_json={"unsafe": True},
            created_at=clock.current,
        )
        session.add(result)
    with pytest.raises(Exception) as caught:
        service.settle_success(worker, claim, "00000000-0000-4000-8000-000000000998")
    assert isinstance(caught.value, contracts.JobLifecycleError)
    clock.advance(seconds=31)
    assert service.claim_next("00000000-0000-4000-8000-000000000999", 30) is None
    detail = service.get_job(job_id)
    assert detail.summary.state is JobState.FAILED
    assert detail.summary.error is not None and detail.summary.error.code == "result_identity_mismatch"
