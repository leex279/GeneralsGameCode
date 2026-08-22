"""Task 3 compatibility facade over the secure durable job lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import Job, JobDependency, Replay


class DependencyCycleError(ValueError):
    """A dependency edge would make the durable graph cyclic."""


class JobStateError(ValueError):
    """A requested transition is invalid for the durable job state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class StageFailure(Exception):
    """Typed handler failure with stable retry and diagnostic semantics."""

    code: str
    message: str
    retryable: bool
    details: Mapping[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class JobSpec:
    stage: str
    component_version: str
    idempotency_key: str
    input_json: Mapping[str, Any]
    replay_id: int | None = None
    priority: int = 100
    max_attempts: int = 3
    retryable: bool = True


@dataclass(frozen=True)
class JobSnapshot:
    internal_id: int
    public_id: str
    idempotency_key: str
    replay_public_id: str | None
    stage: str
    component_version: str
    status: str
    attempt_count: int
    max_attempts: int
    retryable: bool
    error_code: str | None
    input_json: Mapping[str, Any]
    output_json: Mapping[str, Any] | None
    error_details_json: Mapping[str, Any] | None


ClaimedJob = JobSnapshot


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("job clock must return an aware UTC datetime")
    utc = value.astimezone(UTC)
    if utc.utcoffset() != value.utcoffset():
        raise ValueError("job clock must return UTC")
    return utc


def _snapshot(session: Session, row: Job) -> JobSnapshot:
    replay_public_id = None
    if row.replay_id is not None:
        replay_public_id = session.scalar(select(Replay.public_id).where(Replay.id == row.replay_id))
    return JobSnapshot(
        internal_id=row.id,
        public_id=row.public_id,
        idempotency_key=row.idempotency_key,
        replay_public_id=replay_public_id,
        stage=row.stage,
        component_version=row.component_version,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        retryable=row.retryable,
        error_code=row.error_code,
        input_json=cast(Mapping[str, Any], row.input_json),
        output_json=cast(Mapping[str, Any] | None, row.output_json),
        error_details_json=cast(Mapping[str, Any] | None, row.error_details_json),
    )


# TheSuperHackers @refactor Leex 22/08/2026 Keep Task 3 callers on the single secure lifecycle implementation. (#0)
class JobCoordinator:
    """Retain the accepted Task 3 API while delegating all lifecycle transitions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=5),
        retry_max_delay: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_duration < timedelta(seconds=1) or lease_duration > timedelta(hours=1):
            raise ValueError("job lease and retry durations are invalid")
        if retry_base_delay < timedelta(0) or retry_max_delay < retry_base_delay:
            raise ValueError("job retry durations are invalid")
        self._session_factory = session_factory
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._claims: dict[tuple[str, str], object] = {}

    def now(self) -> datetime:
        return _aware_utc(self._clock())

    def _lifecycle(
        self,
        stages: Collection[str],
        terminal_failure_stages: Mapping[str, Collection[str]] | None = None,
    ) -> Any:
        from .job_lifecycle import JobLifecycleService

        return JobLifecycleService(
            self._session_factory,
            registered_stages=stages,
            clock=self._clock,
            terminal_failure_stages=terminal_failure_stages,
            retry_base_delay=self._retry_base_delay,
            retry_max_delay=self._retry_max_delay,
        )

    def create_job(self, spec: JobSpec) -> JobSnapshot:
        try:
            with self._session_factory.begin() as session:
                row = self.ensure_job(session, spec)
                snapshot = _snapshot(session, row)
            return snapshot
        except IntegrityError:
            with self._session_factory() as session:
                existing = session.scalar(select(Job).where(Job.idempotency_key == spec.idempotency_key))
                if existing is None:
                    raise
                return _snapshot(session, existing)

    def ensure_job(self, session: Session, spec: JobSpec) -> Job:
        if not spec.stage or not spec.component_version or not spec.idempotency_key:
            raise ValueError("job stage, version, and idempotency key must be nonempty")
        if spec.priority < 0 or spec.max_attempts < 1:
            raise ValueError("job priority and maximum attempts are invalid")
        now = self.now()
        session.execute(
            sqlite_insert(Job)
            .values(
                public_id=str(uuid4()),
                replay_id=spec.replay_id,
                stage=spec.stage,
                component_version=spec.component_version,
                idempotency_key=spec.idempotency_key,
                status="pending",
                priority=spec.priority,
                attempt_count=0,
                max_attempts=spec.max_attempts,
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                started_at=None,
                completed_at=None,
                input_json=dict(spec.input_json),
                output_json=None,
                error_code=None,
                error_message=None,
                error_details_json=None,
                retryable=spec.retryable,
                created_at=now,
                revision=0,
                lease_token_sha256=None,
                lease_execution_public_id=None,
                last_heartbeat_at=None,
                cancel_requested_at=None,
                cancel_requested_by=None,
                cancel_reason_code=None,
                progress_completed=None,
                progress_total=None,
                progress_unit=None,
                progress_updated_at=None,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )
        row = session.scalar(select(Job).where(Job.idempotency_key == spec.idempotency_key))
        if row is None:
            raise RuntimeError("job idempotency insert did not yield a durable row")
        return row

    def add_dependency(self, job_public_id: str, depends_on_public_id: str) -> None:
        with self._session_factory.begin() as session:
            job = self._require_row(session, job_public_id)
            dependency = self._require_row(session, depends_on_public_id)
            self.ensure_dependency(session, job.id, dependency.id)

    def ensure_dependency(self, session: Session, job_id: int, depends_on_job_id: int) -> None:
        if job_id == depends_on_job_id:
            raise DependencyCycleError("a job cannot depend on itself")
        if session.get(JobDependency, (job_id, depends_on_job_id)) is not None:
            return
        if self._reachable(session, depends_on_job_id, job_id):
            raise DependencyCycleError("dependency edge would create an indirect cycle")
        session.add(JobDependency(job_id=job_id, depends_on_job_id=depends_on_job_id, created_at=self.now()))
        session.flush()

    @staticmethod
    def _reachable(session: Session, start_id: int, target_id: int) -> bool:
        pending = [start_id]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(
                session.scalars(select(JobDependency.depends_on_job_id).where(JobDependency.job_id == current))
            )
        return False

    def claim(
        self,
        worker_id: str,
        registered_stages: Collection[str],
        *,
        terminal_failure_stages: Mapping[str, Collection[str]] | None = None,
    ) -> ClaimedJob | None:
        owner = worker_id.strip()
        if not owner:
            raise ValueError("worker_id must be nonempty")
        stages = frozenset(registered_stages)
        if not stages:
            return None
        lifecycle = self._lifecycle(stages, terminal_failure_stages)
        claim = lifecycle.claim_next(owner, int(self._lease_duration.total_seconds()))
        if claim is None:
            return None
        self._claims[(claim.job_public_id, owner)] = claim
        return self.snapshot(claim.job_public_id)

    def persist_result(self, job_public_id: str, worker_id: str, output_json: Mapping[str, Any]) -> str:
        claim = self._owned_claim(job_public_id, worker_id)
        lifecycle = self._lifecycle((claim.stage,))
        return cast(str, lifecycle.persist_stage_result(claim, output_json))

    def succeed(self, job_public_id: str, worker_id: str, output_json: Mapping[str, Any]) -> JobSnapshot:
        claim = self._owned_claim(job_public_id, worker_id)
        lifecycle = self._lifecycle((claim.stage,))
        result_public_id = lifecycle.persist_stage_result(claim, output_json)
        lifecycle.settle_success(worker_id, claim, result_public_id)
        self._claims.pop((job_public_id, worker_id), None)
        return self.snapshot(job_public_id)

    def fail(self, job_public_id: str, worker_id: str, failure: StageFailure) -> JobSnapshot:
        from .job_contracts import StageExecutionOutcomeDTO

        claim = self._owned_claim(job_public_id, worker_id)
        lifecycle = self._lifecycle((claim.stage,))
        lifecycle.settle_failure(
            worker_id,
            claim,
            StageExecutionOutcomeDTO(
                "retryable_failure" if failure.retryable else "failed",
                None,
                failure.code,
                failure.message,
                failure.retryable,
            ),
            safe_details=failure.details,
        )
        self._claims.pop((job_public_id, worker_id), None)
        return self.snapshot(job_public_id)

    def retry(self, job_public_id: str) -> JobSnapshot:
        lifecycle = self._lifecycle(())
        lifecycle.retry_legacy(job_public_id)
        return self.snapshot(job_public_id)

    def snapshot(self, job_public_id: str) -> JobSnapshot:
        with self._session_factory() as session:
            return _snapshot(session, self._require_row(session, job_public_id))

    def _owned_claim(self, job_public_id: str, worker_id: str) -> Any:
        claim = self._claims.get((job_public_id, worker_id))
        if claim is None:
            raise JobStateError("lease_mismatch", "job is not running under this worker lease")
        return claim

    @staticmethod
    def _require_row(session: Session, public_id: str) -> Job:
        row = session.scalar(select(Job).where(Job.public_id == public_id))
        if row is None:
            raise JobStateError("unknown_job", "unknown job public ID")
        return row
