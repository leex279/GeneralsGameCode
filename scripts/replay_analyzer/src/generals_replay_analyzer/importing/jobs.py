"""Transactional durable jobs with dependency, lease, retry, and restart semantics."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Select, select, update
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
    return value.astimezone(UTC)


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


# TheSuperHackers @feature Leex 21/08/2026 Make replay analysis stages lease-safe and restartable in SQLite. (#TBD)
class JobCoordinator:
    """Own every job-state transition in a fresh, bounded transaction."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=5),
        retry_max_delay: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_duration <= timedelta(0) or retry_base_delay < timedelta(0) or retry_max_delay < retry_base_delay:
            raise ValueError("job lease and retry durations are invalid")
        self._session_factory = session_factory
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay

    def now(self) -> datetime:
        return _aware_utc(self._clock())

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
        # TheSuperHackers @bugfix Leex 22/08/2026 Coalesce graph-local job races without aborting the caller transaction. (#TBD)
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
                available_at=self.now(),
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
        existing = session.get(JobDependency, (job_id, depends_on_job_id))
        if existing is not None:
            return
        if self._reachable(session, depends_on_job_id, job_id):
            raise DependencyCycleError("dependency edge would create an indirect cycle")
        session.add(JobDependency(job_id=job_id, depends_on_job_id=depends_on_job_id, created_at=self.now()))
        session.flush()

    def _reachable(self, session: Session, start_id: int, target_id: int) -> bool:
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
                session.scalars(
                    select(JobDependency.depends_on_job_id).where(JobDependency.job_id == current)
                )
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
        tolerated_failures = {
            stage: frozenset(dependency_stages)
            for stage, dependency_stages in (terminal_failure_stages or {}).items()
        }
        with self._session_factory.begin() as session:
            now = self.now()
            self._reclaim_expired(session, now)
            self._project_dependency_failures(session, now, tolerated_failures)
            for candidate in session.scalars(self._candidate_query(stages, now)):
                dependencies = list(
                    session.execute(
                        select(Job.stage, Job.status, Job.retryable)
                        .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                        .where(JobDependency.job_id == candidate.id)
                    )
                )
                allowed = tolerated_failures.get(candidate.stage, frozenset())
                # TheSuperHackers @bugfix Leex 22/08/2026 Admit only settled nonretryable failures as terminal evidence. (#TBD)
                if any(
                    status != "succeeded"
                    and not (status == "failed" and not retryable and stage in allowed)
                    for stage, status, retryable in dependencies
                ):
                    continue
                result = session.execute(
                    update(Job)
                    .where(
                        Job.id == candidate.id,
                        Job.status == "pending",
                        Job.available_at <= now,
                    )
                    .values(
                        status="running",
                        lease_owner=owner,
                        lease_expires_at=now + self._lease_duration,
                        started_at=candidate.started_at or now,
                        attempt_count=Job.attempt_count + 1,
                        completed_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if getattr(result, "rowcount", 0) != 1:
                    continue
                session.flush()
                claimed = session.get(Job, candidate.id)
                assert claimed is not None
                session.refresh(claimed)
                return _snapshot(session, claimed)
        return None

    @staticmethod
    def _candidate_query(stages: Collection[str], now: datetime) -> Select[tuple[Job]]:
        return (
            select(Job)
            .where(Job.status == "pending", Job.available_at <= now, Job.stage.in_(stages))
            .order_by(Job.priority.desc(), Job.available_at, Job.id)
        )

    def _retry_at(self, now: datetime, attempt_count: int) -> datetime:
        multiplier: int = 2 ** max(0, attempt_count - 1)
        delay: timedelta = min(self._retry_base_delay * multiplier, self._retry_max_delay)
        return now + delay

    def _reclaim_expired(self, session: Session, now: datetime) -> None:
        expired = list(
            session.scalars(
                select(Job).where(
                    Job.status == "running",
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at <= now,
                )
            )
        )
        for row in expired:
            row.lease_owner = None
            row.lease_expires_at = None
            row.error_code = "lease_expired"
            row.error_message = "worker lease expired before completion"
            row.error_details_json = {"attempt_count": row.attempt_count}
            if row.retryable and row.attempt_count < row.max_attempts:
                row.status = "pending"
                row.available_at = self._retry_at(now, row.attempt_count)
            else:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
        session.flush()

    def _project_dependency_failures(
        self,
        session: Session,
        now: datetime,
        terminal_failure_stages: Mapping[str, Collection[str]],
    ) -> None:
        changed = True
        while changed:
            changed = False
            pending = list(session.scalars(select(Job).where(Job.status == "pending")))
            for row in pending:
                failed_dependencies = list(
                    session.execute(
                        select(Job.public_id, Job.stage)
                        .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                        # TheSuperHackers @bugfix Leex 22/08/2026 Project only settled dependency failures. (#TBD)
                        .where(
                            JobDependency.job_id == row.id,
                            Job.status == "failed",
                            Job.retryable.is_(False),
                        )
                        .order_by(Job.id)
                    )
                )
                allowed = frozenset(terminal_failure_stages.get(row.stage, ()))
                required_failure = next(
                    (
                        public_id
                        for public_id, dependency_stage in failed_dependencies
                        if dependency_stage not in allowed
                    ),
                    None,
                )
                if required_failure is None:
                    continue
                row.status = "failed"
                row.retryable = False
                row.error_code = "dependency_failed"
                row.error_message = "a required dependency failed"
                row.error_details_json = {"dependency_public_id": required_failure}
                row.lease_owner = None
                row.lease_expires_at = None
                row.completed_at = now
                changed = True
            if changed:
                session.flush()

    def succeed(self, job_public_id: str, worker_id: str, output_json: Mapping[str, Any]) -> JobSnapshot:
        with self._session_factory.begin() as session:
            row = self._owned_running(session, job_public_id, worker_id)
            row.status = "succeeded"
            row.output_json = dict(output_json)
            row.error_code = None
            row.error_message = None
            row.error_details_json = None
            row.retryable = False
            row.lease_owner = None
            row.lease_expires_at = None
            row.completed_at = self.now()
            session.flush()
            return _snapshot(session, row)

    def fail(self, job_public_id: str, worker_id: str, failure: StageFailure) -> JobSnapshot:
        with self._session_factory.begin() as session:
            row = self._owned_running(session, job_public_id, worker_id)
            now = self.now()
            row.error_code = failure.code
            row.error_message = failure.message
            row.error_details_json = dict(failure.details) if failure.details is not None else {}
            row.retryable = failure.retryable
            row.lease_owner = None
            row.lease_expires_at = None
            if failure.retryable and row.attempt_count < row.max_attempts:
                row.status = "pending"
                row.available_at = self._retry_at(now, row.attempt_count)
            else:
                row.status = "failed"
                # TheSuperHackers @bugfix Leex 22/08/2026 Settle exhausted retryable work into a stable terminal state. (#TBD)
                row.retryable = False
                row.completed_at = now
            session.flush()
            return _snapshot(session, row)

    def retry(self, job_public_id: str) -> JobSnapshot:
        with self._session_factory.begin() as session:
            row = session.scalar(select(Job).where(Job.public_id == job_public_id))
            if row is None:
                raise JobStateError("unknown_job", "unknown job public ID")
            # TheSuperHackers @bugfix Leex 22/08/2026 Preserve another worker's live lease during explicit retry. (#TBD)
            if row.status == "running":
                raise JobStateError("running_job", "running jobs cannot be retried")
            if row.status == "succeeded":
                raise JobStateError("successful_job", "successful jobs cannot be retried")
            if row.status not in {"pending", "failed"}:
                raise JobStateError("invalid_job_state", "only pending or failed jobs can be retried")
            if not row.retryable:
                raise JobStateError("nonretryable_job", "nonretryable jobs cannot be retried")
            if row.attempt_count >= row.max_attempts:
                raise JobStateError("exhausted_job", "job attempts are exhausted")
            now = self.now()
            row.status = "pending"
            row.available_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            row.completed_at = None
            row.error_code = None
            row.error_message = None
            row.error_details_json = None
            self._restore_dependency_descendants(session, row.id, now)
            session.flush()
            return _snapshot(session, row)

    def _restore_dependency_descendants(self, session: Session, root_id: int, now: datetime) -> None:
        pending = deque([root_id])
        visited = {root_id}
        while pending:
            dependency_id = pending.popleft()
            children = list(
                session.scalars(
                    select(Job)
                    .join(JobDependency, Job.id == JobDependency.job_id)
                    .where(JobDependency.depends_on_job_id == dependency_id)
                )
            )
            for child in children:
                if child.id not in visited:
                    visited.add(child.id)
                    pending.append(child.id)
                if child.status == "failed" and child.error_code == "dependency_failed":
                    child.status = "pending"
                    child.retryable = True
                    child.available_at = now
                    child.completed_at = None
                    child.error_code = None
                    child.error_message = None
                    child.error_details_json = None
                    child.lease_owner = None
                    child.lease_expires_at = None

    def snapshot(self, job_public_id: str) -> JobSnapshot:
        with self._session_factory() as session:
            row = self._require_row(session, job_public_id)
            return _snapshot(session, row)

    @staticmethod
    def _require_row(session: Session, public_id: str) -> Job:
        row = session.scalar(select(Job).where(Job.public_id == public_id))
        if row is None:
            raise JobStateError("unknown_job", "unknown job public ID")
        return row

    @staticmethod
    def _owned_running(session: Session, public_id: str, worker_id: str) -> Job:
        row = session.scalar(select(Job).where(Job.public_id == public_id))
        if row is None:
            raise JobStateError("unknown_job", "unknown job public ID")
        if row.status != "running" or row.lease_owner != worker_id:
            raise JobStateError("lease_mismatch", "job is not running under this worker lease")
        return row
