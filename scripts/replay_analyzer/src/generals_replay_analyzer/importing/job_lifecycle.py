"""Analytics-owned secure job lifecycle and immutable worker evidence."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections import deque
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
    Job,
    JobDependency,
    JobEvent,
    JobLogSnapshot,
    JobStageResult,
    ManagedAsset,
    Replay,
)
from generals_replay_analyzer.storage import ContentAddressedStore, ContentStorageError

from .job_contracts import (
    CancelJobCommandDTO,
    JobDetailDTO,
    JobErrorSummaryDTO,
    JobEventSnapshotDTO,
    JobLogChunkDTO,
    JobLogQueryDTO,
    JobLogReferenceDTO,
    JobMutationDTO,
    JobPageDTO,
    JobProgressDTO,
    JobQueryDTO,
    JobState,
    JobSummaryDTO,
    OwnedExecutionSettlementDTO,
    RetryJobCommandDTO,
    StageExecutionOutcomeDTO,
    WorkerCancellationDTO,
    WorkerLeaseDTO,
)
from .jobs import JobStateError

_MAX_LOG_READ = 65_536
_ANSI_PATTERN = re.compile(r"(?:\x1b\[[0-?]*[ -/]*[@-~])|(?:\x1b\][^\x07]*(?:\x07|\x1b\\))")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_COMMAND_CREDENTIALS = re.compile(
    r"(?i)(--(?:token|password|secret|api[-_]?key)|(?:token|password|secret|api[-_]?key)=)(?:\s+|)[^\s]+"
)
_PATH_PATTERN = re.compile(r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s:'\"]+[\\/])*[^\s:'\"]+")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _state(value: str) -> JobState:
    return JobState(value)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_seconds(value: int) -> timedelta:
    if isinstance(value, bool) or value < 1 or value > 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    return timedelta(seconds=value)


# TheSuperHackers @feature Leex 22/08/2026 Centralize every capability-owned job transition in one durable service. (#0)
class JobLifecycleService:
    """Implement worker control and revisioned job operations over short SQLite transactions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        registered_stages: Collection[str],
        clock: Callable[[], datetime],
        terminal_failure_stages: Mapping[str, Collection[str]] | None = None,
        retry_base_delay: timedelta = timedelta(seconds=5),
        retry_max_delay: timedelta = timedelta(minutes=5),
        log_store: ContentAddressedStore | None = None,
        log_relative_root: str = "job-logs",
        redaction_values: Collection[str] = (),
    ) -> None:
        stages = tuple(sorted(set(registered_stages)))
        if any(not stage.strip() for stage in stages):
            raise ValueError("registered stages must be nonempty")
        if retry_base_delay < timedelta(0) or retry_max_delay < retry_base_delay:
            raise ValueError("retry delays are invalid")
        if not log_relative_root or Path(log_relative_root).is_absolute() or ".." in Path(log_relative_root).parts:
            raise ValueError("log relative root must be product-relative")
        self._session_factory = session_factory
        self._stages = stages
        self._clock = clock
        self._terminal_failure_stages = {
            stage: frozenset(values) for stage, values in (terminal_failure_stages or {}).items()
        }
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._log_store = log_store
        self._log_relative_root = log_relative_root.replace("\\", "/").strip("/")
        self._redaction_values = tuple(sorted({value for value in redaction_values if value}, key=len, reverse=True))

    def registered_stages(self) -> tuple[str, ...]:
        return self._stages

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("job clock must return an aware UTC datetime")
        utc = value.astimezone(UTC)
        if utc.utcoffset() != value.utcoffset():
            raise ValueError("job clock must return UTC")
        return utc

    def _retry_at(self, now: datetime, attempt_count: int) -> datetime:
        multiplier: int = 2 ** max(0, attempt_count - 1)
        candidate: timedelta = self._retry_base_delay * multiplier
        delay: timedelta = min(candidate, self._retry_max_delay)
        return now + delay

    def claim_next(self, worker_public_id: str, lease_seconds: int) -> WorkerLeaseDTO | None:
        worker = worker_public_id.strip()
        if not worker:
            raise ValueError("worker_public_id must be nonempty")
        duration = _validate_seconds(lease_seconds)
        if not self._stages:
            return None
        with self._session_factory.begin() as session:
            now = self.now()
            self._recover(session, now)
            self._project_dependency_terminals(session, now)
            for candidate in session.scalars(self._candidate_query(now)):
                if not self._dependencies_satisfied(session, candidate):
                    continue
                token = secrets.token_urlsafe(32)
                execution_public_id = str(uuid4())
                next_revision = candidate.revision + 1
                result = session.execute(
                    update(Job)
                    .where(Job.id == candidate.id, Job.status == "pending", Job.available_at <= now)
                    .values(
                        status="running",
                        attempt_count=Job.attempt_count + 1,
                        lease_owner=worker,
                        lease_token_sha256=_token_digest(token),
                        lease_execution_public_id=execution_public_id,
                        last_heartbeat_at=now,
                        lease_expires_at=now + duration,
                        started_at=candidate.started_at or now,
                        completed_at=None,
                        progress_completed=None,
                        progress_total=None,
                        progress_unit=None,
                        progress_updated_at=None,
                        error_code=None,
                        error_message=None,
                        error_details_json=None,
                        revision=next_revision,
                    )
                    .execution_options(synchronize_session=False)
                )
                if getattr(result, "rowcount", 0) != 1:
                    continue
                row = session.get(Job, candidate.id)
                assert row is not None
                session.refresh(row)
                self._event(session, row, "claimed", None, now)
                return WorkerLeaseDTO(
                    row.public_id,
                    execution_public_id,
                    row.stage,
                    row.attempt_count,
                    row.max_attempts,
                    token,
                    now + duration,
                )
        return None

    def _candidate_query(self, now: datetime) -> Select[tuple[Job]]:
        return (
            select(Job)
            .where(Job.status == "pending", Job.available_at <= now, Job.stage.in_(self._stages))
            .order_by(Job.priority.desc(), Job.available_at, Job.id)
        )

    def _dependencies_satisfied(self, session: Session, row: Job) -> bool:
        dependencies = list(
            session.execute(
                select(Job.stage, Job.status, Job.retryable)
                .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                .where(JobDependency.job_id == row.id)
            )
        )
        allowed = self._terminal_failure_stages.get(row.stage, frozenset())
        return not any(
            status != "succeeded"
            and not (status == "failed" and not retryable and stage in allowed)
            for stage, status, retryable in dependencies
        )

    def _recover(self, session: Session, now: datetime) -> None:
        expired = list(
            session.scalars(
                select(Job)
                .where(Job.status == "running", Job.lease_expires_at.is_not(None), Job.lease_expires_at <= now)
                .order_by(Job.id)
            )
        )
        for row in expired:
            durable = session.scalar(select(JobStageResult).where(JobStageResult.job_id == row.id))
            if durable is not None and row.cancel_requested_at is None:
                self._clear_lease(row)
                row.status = "succeeded"
                row.output_json = cast(dict[str, Any], durable.output_json)
                row.retryable = False
                row.completed_at = now
                row.error_code = None
                row.error_message = None
                row.error_details_json = None
                row.revision += 1
                self._event(session, row, "result_reused", None, now)
                continue
            self._clear_lease(row)
            row.progress_completed = None
            row.progress_total = None
            row.progress_unit = None
            row.progress_updated_at = None
            row.revision += 1
            if row.cancel_requested_at is not None:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
                row.error_code = "owned_child_settlement_failed"
                row.error_message = "lease expired before owned child settlement was proven"
                row.error_details_json = {}
                self._event(session, row, "failed", row.error_code, now)
            elif row.retryable and row.attempt_count < row.max_attempts:
                row.status = "pending"
                row.available_at = self._retry_at(now, row.attempt_count)
                row.completed_at = None
                row.error_code = "lease_expired"
                row.error_message = "worker lease expired before completion"
                row.error_details_json = {}
                self._event(session, row, "lease_expired", row.error_code, now)
            else:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
                row.error_code = "lease_expired"
                row.error_message = "worker lease expired with no retry budget"
                row.error_details_json = {}
                self._event(session, row, "failed", row.error_code, now)
        session.flush()

    def _project_dependency_terminals(self, session: Session, now: datetime) -> None:
        changed = True
        while changed:
            changed = False
            for row in session.scalars(select(Job).where(Job.status == "pending").order_by(Job.id)):
                dependencies = list(
                    session.execute(
                        select(Job.public_id, Job.stage, Job.status, Job.retryable)
                        .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                        .where(JobDependency.job_id == row.id)
                        .order_by(Job.id)
                    )
                )
                cancelled = next((public_id for public_id, _, status, _ in dependencies if status == "cancelled"), None)
                allowed = self._terminal_failure_stages.get(row.stage, frozenset())
                failed = next(
                    (
                        public_id
                        for public_id, stage, status, retryable in dependencies
                        if status == "failed" and not retryable and stage not in allowed
                    ),
                    None,
                )
                reason = "dependency_cancelled" if cancelled is not None else "dependency_failed" if failed else None
                dependency_id = cancelled or failed
                if reason is None:
                    continue
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
                row.error_code = reason
                row.error_message = "a required dependency reached a terminal state"
                row.error_details_json = {"dependency_public_id": dependency_id}
                row.revision += 1
                self._event(session, row, reason, reason, now)
                changed = True
            if changed:
                session.flush()

    def heartbeat(self, worker_public_id: str, claim: WorkerLeaseDTO, lease_seconds: int) -> WorkerLeaseDTO:
        duration = _validate_seconds(lease_seconds)
        with self._session_factory.begin() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            row.last_heartbeat_at = now
            row.lease_expires_at = now + duration
            session.flush()
            return replace_lease_expiry(claim, now + duration)

    def cancellation(self, worker_public_id: str, claim: WorkerLeaseDTO) -> WorkerCancellationDTO:
        with self._session_factory() as session:
            row = self._owned(session, worker_public_id, claim, self.now())
            return WorkerCancellationDTO(row.cancel_requested_at is not None, row.cancel_reason_code)

    def report_progress(
        self, worker_public_id: str, claim: WorkerLeaseDTO, progress: JobProgressDTO
    ) -> None:
        with self._session_factory.begin() as session:
            row = self._owned(session, worker_public_id, claim, self.now())
            current = self._progress(row)
            if current == progress:
                return
            if current is not None and (
                progress.completed < current.completed
                or progress.total < current.total
                or progress.unit != current.unit
            ):
                raise JobStateError("progress_regressed", "progress must be monotonic within one attempt")
            row.progress_completed = progress.completed
            row.progress_total = progress.total
            row.progress_unit = progress.unit
            row.progress_updated_at = progress.updated_at
            row.revision += 1
            self._event(session, row, "progress", None, self.now())

    def persist_stage_result(self, claim: WorkerLeaseDTO, output: Mapping[str, Any]) -> str:
        with self._session_factory.begin() as session:
            row = self._claim_identity(session, claim, self.now())
            return self._persist_result(session, row, output)

    def persist_stage_result_for_execution(
        self, job_public_id: str, execution_public_id: str, output: Mapping[str, Any]
    ) -> str:
        """Persist executor output after rechecking the exact live execution identity."""
        with self._session_factory.begin() as session:
            row = self._execution(session, job_public_id, execution_public_id, self.now())
            return self._persist_result(session, row, output)

    def _persist_result(self, session: Session, row: Job, output: Mapping[str, Any]) -> str:
        existing = session.scalar(select(JobStageResult).where(JobStageResult.job_id == row.id))
        if existing is not None:
            if existing.idempotency_key != row.idempotency_key or existing.output_json != dict(output):
                raise JobStateError("result_conflict", "job already has a different durable result")
            return existing.public_id
        result = JobStageResult(
            public_id=str(uuid4()),
            job_id=row.id,
            stage=row.stage,
            component_version=row.component_version,
            idempotency_key=row.idempotency_key,
            output_json=dict(output),
            created_at=self.now(),
        )
        session.add(result)
        session.flush()
        return result.public_id

    def assert_execution(self, job_public_id: str, execution_public_id: str) -> None:
        with self._session_factory() as session:
            self._execution(session, job_public_id, execution_public_id, self.now())

    @staticmethod
    def _execution(
        session: Session, job_public_id: str, execution_public_id: str, now: datetime
    ) -> Job:
        row = session.scalar(select(Job).where(Job.public_id == job_public_id))
        if (
            row is None
            or row.status != "running"
            or row.lease_execution_public_id != execution_public_id
            or row.lease_expires_at is None
            or _aware_utc(row.lease_expires_at) <= now
        ):
            raise JobStateError("execution_mismatch", "stage execution is stale, expired, or mismatched")
        if row.cancel_requested_at is not None:
            raise JobStateError("cancellation_requested", "stage execution cannot launch after cancellation")
        return row

    def settle_success(self, worker_public_id: str, claim: WorkerLeaseDTO, result_public_id: str) -> None:
        with self._session_factory.begin() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            if row.cancel_requested_at is not None:
                raise JobStateError("cancellation_requested", "success cannot settle after cancellation wins")
            result = session.scalar(
                select(JobStageResult).where(
                    JobStageResult.public_id == result_public_id,
                    JobStageResult.job_id == row.id,
                    JobStageResult.idempotency_key == row.idempotency_key,
                )
            )
            if result is None:
                raise JobStateError("unknown_result", "durable stage result is unavailable")
            self._clear_lease(row)
            row.status = "succeeded"
            row.output_json = cast(dict[str, Any], result.output_json)
            row.retryable = False
            row.completed_at = now
            row.error_code = None
            row.error_message = None
            row.error_details_json = None
            row.revision += 1
            self._event(session, row, "succeeded", None, now)

    def settle_failure(
        self,
        worker_public_id: str,
        claim: WorkerLeaseDTO,
        outcome: StageExecutionOutcomeDTO,
        *,
        safe_details: Mapping[str, Any] | None = None,
    ) -> None:
        if outcome.status not in {"retryable_failure", "failed"}:
            raise ValueError("failure settlement requires a failure outcome")
        if not outcome.error_code or not outcome.error_message:
            raise ValueError("failure outcome requires safe error fields")
        with self._session_factory.begin() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            self._clear_lease(row)
            row.progress_completed = None
            row.progress_total = None
            row.progress_unit = None
            row.progress_updated_at = None
            row.error_code = outcome.error_code
            row.error_message = self._redact(outcome.error_message)
            row.error_details_json = dict(safe_details or {})
            retry = outcome.status == "retryable_failure" and outcome.retryable
            if retry and row.cancel_requested_at is None and row.attempt_count < row.max_attempts:
                row.status = "pending"
                row.retryable = True
                row.available_at = self._retry_at(now, row.attempt_count)
                row.completed_at = None
            else:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
            row.revision += 1
            self._event(session, row, "failed", row.error_code, now)

    def settle_cancelled(
        self, worker_public_id: str, claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None:
        with self._session_factory.begin() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            self._validate_settlement(claim, settlement)
            if row.cancel_requested_at is None:
                raise JobStateError("cancellation_not_requested", "owned execution cannot settle cancelled without a request")
            self._clear_lease(row)
            row.progress_completed = None
            row.progress_total = None
            row.progress_unit = None
            row.progress_updated_at = None
            row.retryable = False
            row.completed_at = now
            row.revision += 1
            if settlement.tree_settled:
                row.status = "cancelled"
                row.error_code = "user_cancelled"
                row.error_message = "job cancelled after owned execution tree settled"
                row.error_details_json = {}
                self._event(session, row, "cancelled", row.cancel_reason_code, now)
            else:
                row.status = "failed"
                row.error_code = "owned_child_settlement_failed"
                row.error_message = "owned execution tree settlement was uncertain"
                row.error_details_json = {}
                self._event(session, row, "failed", row.error_code, now)

    def release_after_shutdown(
        self, worker_public_id: str, claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None:
        with self._session_factory.begin() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            self._validate_settlement(claim, settlement)
            self._clear_lease(row)
            row.progress_completed = None
            row.progress_total = None
            row.progress_unit = None
            row.progress_updated_at = None
            row.revision += 1
            if not settlement.tree_settled:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
                row.error_code = "owned_child_settlement_failed"
                row.error_message = "owned execution tree settlement was uncertain"
                row.error_details_json = {}
                self._event(session, row, "failed", row.error_code, now)
            elif row.cancel_requested_at is not None:
                row.status = "cancelled"
                row.retryable = False
                row.completed_at = now
                row.error_code = "user_cancelled"
                row.error_message = "job cancelled during worker shutdown"
                row.error_details_json = {}
                self._event(session, row, "cancelled", row.cancel_reason_code, now)
            elif row.retryable and row.attempt_count < row.max_attempts:
                row.status = "pending"
                row.available_at = self._retry_at(now, row.attempt_count)
                row.completed_at = None
                row.error_code = "worker_shutdown"
                row.error_message = "worker shut down after owned execution tree settled"
                row.error_details_json = {}
                self._event(session, row, "worker_shutdown", row.error_code, now)
            else:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
                row.error_code = "worker_shutdown"
                row.error_message = "worker shutdown exhausted retry budget"
                row.error_details_json = {}
                self._event(session, row, "failed", row.error_code, now)

    def cancel_job(self, command: CancelJobCommandDTO) -> JobMutationDTO:
        actor = command.requested_by.strip()
        reason = command.reason_code.strip()
        if not actor or not reason:
            raise ValueError("cancellation actor and reason must be nonempty")
        with self._session_factory.begin() as session:
            now = self.now()
            row = self._require(session, command.job_public_id)
            self._revision(row, command.expected_revision)
            if row.status == "pending":
                row.status = "cancelled"
                row.retryable = False
                row.completed_at = now
                row.cancel_requested_at = now
                row.cancel_requested_by = actor
                row.cancel_reason_code = reason
                row.error_code = "user_cancelled"
                row.error_message = "job cancelled before claim"
                row.error_details_json = {}
                row.revision += 1
                self._event(session, row, "cancelled", reason, now)
                self._cancel_descendants(session, row.id, now)
            elif row.status == "running" and row.cancel_requested_at is None:
                row.cancel_requested_at = now
                row.cancel_requested_by = actor
                row.cancel_reason_code = reason
                row.revision += 1
                self._event(session, row, "cancel_requested", reason, now)
            else:
                raise JobStateError("terminal_job", "job cannot accept cancellation in its current state")
            session.flush()
            return self._mutation(row)

    def _cancel_descendants(self, session: Session, root_id: int, now: datetime) -> None:
        pending = deque([root_id])
        visited = {root_id}
        while pending:
            dependency_id = pending.popleft()
            children = list(
                session.scalars(
                    select(Job)
                    .join(JobDependency, Job.id == JobDependency.job_id)
                    .where(JobDependency.depends_on_job_id == dependency_id)
                    .order_by(Job.id)
                )
            )
            for child in children:
                if child.id not in visited:
                    visited.add(child.id)
                    pending.append(child.id)
                if child.status != "pending":
                    continue
                child.status = "failed"
                child.retryable = False
                child.completed_at = now
                child.error_code = "dependency_cancelled"
                child.error_message = "a required dependency was cancelled"
                child.error_details_json = {}
                child.revision += 1
                self._event(session, child, "dependency_cancelled", child.error_code, now)

    def retry_job(self, command: RetryJobCommandDTO) -> JobMutationDTO:
        with self._session_factory.begin() as session:
            row = self._require(session, command.job_public_id)
            self._revision(row, command.expected_revision)
            if row.status != "failed":
                raise JobStateError("ineligible_retry", "only failed work can be explicitly retried")
            if not row.retryable or row.attempt_count >= row.max_attempts:
                raise JobStateError("ineligible_retry", "job is not retryable or has exhausted attempts")
            now = self.now()
            row.status = "pending"
            row.available_at = now
            row.completed_at = None
            row.error_code = None
            row.error_message = None
            row.error_details_json = None
            row.revision += 1
            self._event(session, row, "retry_requested", None, now)
            return self._mutation(row)

    def retry_legacy(self, job_public_id: str) -> None:
        """Preserve Task 3's unrevisioned CLI facade while keeping transitions centralized here."""
        with self._session_factory.begin() as session:
            row = self._require(session, job_public_id)
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
            row.completed_at = None
            row.error_code = None
            row.error_message = None
            row.error_details_json = None
            row.revision += 1
            self._event(session, row, "retry_requested", None, now)
            self._restore_dependency_descendants(session, row.id, now)

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
                    child.revision += 1
                    self._event(session, child, "retry_requested", None, now)

    def list_jobs(self, query: JobQueryDTO) -> JobPageDTO:
        with self._session_factory() as session:
            statement = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
            if query.states:
                statement = statement.where(Job.status.in_(tuple(value.value for value in query.states)))
            if query.stage is not None:
                statement = statement.where(Job.stage == query.stage)
            if query.replay_public_id is not None:
                statement = statement.join(Replay, Replay.id == Job.replay_id).where(
                    Replay.public_id == query.replay_public_id
                )
            if query.after_public_id is not None:
                cursor = session.scalar(select(Job).where(Job.public_id == query.after_public_id))
                if cursor is None:
                    raise JobStateError("unknown_cursor", "job page cursor is unavailable")
                statement = statement.where(Job.id < cursor.id)
            rows = list(session.scalars(statement.limit(query.limit + 1)))
            next_public_id = rows[query.limit - 1].public_id if len(rows) > query.limit else None
            return JobPageDTO(tuple(self._summary(session, row) for row in rows[: query.limit]), next_public_id)

    def get_job(self, job_public_id: str) -> JobDetailDTO:
        with self._session_factory() as session:
            row = self._require(session, job_public_id)
            dependencies = tuple(
                session.scalars(
                    select(Job.public_id)
                    .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                    .where(JobDependency.job_id == row.id)
                    .order_by(Job.id)
                )
            )
            events = tuple(
                self._event_dto(event)
                for event in session.scalars(
                    select(JobEvent).where(JobEvent.job_id == row.id).order_by(JobEvent.revision)
                )
            )
            logs = tuple(
                self._log_dto(row.public_id, value)
                for value in session.scalars(
                    select(JobLogSnapshot)
                    .where(JobLogSnapshot.job_id == row.id)
                    .order_by(JobLogSnapshot.attempt_count, JobLogSnapshot.label, JobLogSnapshot.sequence)
                )
            )
            return JobDetailDTO(self._summary(session, row), dependencies, events, logs)

    def publish_log(
        self,
        worker_public_id: str,
        claim: WorkerLeaseDTO,
        label: str,
        sequence: int,
        raw_output: bytes,
    ) -> JobLogReferenceDTO:
        if self._log_store is None:
            raise JobStateError("log_sink_unavailable", "managed job log sink is unavailable")
        if label not in {"stdout", "stderr", "supervisor"} or sequence < 0:
            raise ValueError("log stream identity is invalid")
        if not isinstance(raw_output, bytes):
            raise TypeError("raw supervisor output must be bytes")
        now = self.now()
        with self._session_factory() as session:
            self._owned(session, worker_public_id, claim, now)
        redacted = self._redact(raw_output.decode("utf-8", errors="replace")).encode("utf-8")
        stored = self._log_store.store_bytes(redacted)
        with self._session_factory.begin() as session:
            row = self._owned(session, worker_public_id, claim, now)
            asset = session.scalar(select(ManagedAsset).where(ManagedAsset.sha256 == stored.sha256))
            if asset is None:
                asset = ManagedAsset(
                    public_id=str(uuid4()),
                    sha256=stored.sha256,
                    kind="job_log_snapshot",
                    relative_path=f"{self._log_relative_root}/{stored.sha256[:2]}/{stored.sha256}.txt",
                    size_bytes=stored.size,
                    media_type="text/plain",
                    created_at=now,
                )
                session.add(asset)
                session.flush()
            snapshot = JobLogSnapshot(
                public_id=str(uuid4()),
                job_id=row.id,
                attempt_count=row.attempt_count,
                label=label,
                sequence=sequence,
                managed_asset_id=asset.id,
                media_type="text/plain",
                byte_count=stored.size,
                redaction_version="job-log-redaction-v1",
                created_at=now,
            )
            session.add(snapshot)
            session.flush()
            return self._log_dto(row.public_id, snapshot)

    def read_log(self, query: JobLogQueryDTO) -> JobLogChunkDTO:
        if self._log_store is None:
            return JobLogChunkDTO("unavailable", "", None)
        with self._session_factory() as session:
            row = self._require(session, query.job_public_id)
            snapshot = session.scalar(
                select(JobLogSnapshot).where(JobLogSnapshot.public_id == query.log_public_id)
            )
            if snapshot is None:
                raise JobStateError("unknown_log", "job log is unavailable")
            if snapshot.job_id != row.id:
                raise JobStateError("log_ownership_mismatch", "log does not belong to the requested job")
            asset = session.get(ManagedAsset, snapshot.managed_asset_id)
            if asset is None:
                return JobLogChunkDTO("unavailable", "", None)
            digest = asset.sha256
        try:
            stored = self._log_store.verify(digest)
            data = stored.path.read_bytes()
        except (ContentStorageError, OSError):
            return JobLogChunkDTO("unavailable", "", None)
        if query.offset >= len(data):
            return JobLogChunkDTO("rotated", "", None)
        limit = min(query.limit, _MAX_LOG_READ)
        end = min(len(data), query.offset + limit)
        content = self._redact(data[query.offset:end].decode("utf-8", errors="replace"))
        return JobLogChunkDTO("available", content, end if end < len(data) else None)

    def _redact(self, value: str) -> str:
        redacted = _ANSI_PATTERN.sub("", value)
        redacted = _URL_CREDENTIALS.sub(r"\1[credentials]@", redacted)
        redacted = _COMMAND_CREDENTIALS.sub(r"\1 [redacted]", redacted)
        for secret in self._redaction_values:
            redacted = re.sub(re.escape(secret), "[redacted]", redacted, flags=re.IGNORECASE)
        redacted = _PATH_PATTERN.sub("[path]", redacted)
        safe_lines = []
        for line in redacted.splitlines(keepends=True):
            stripped = line.lstrip()
            if stripped.startswith(("Traceback (", "File ")):
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):", stripped):
                continue
            safe_lines.append(line)
        return "".join(safe_lines)

    def _owned(self, session: Session, worker: str, claim: WorkerLeaseDTO, now: datetime) -> Job:
        row = self._claim_identity(session, claim, now)
        if row.lease_owner != worker:
            raise JobStateError("lease_mismatch", "worker does not own this job lease")
        return row

    def _claim_identity(self, session: Session, claim: WorkerLeaseDTO, now: datetime) -> Job:
        row = session.scalar(select(Job).where(Job.public_id == claim.job_public_id))
        if (
            row is None
            or row.status != "running"
            or row.attempt_count != claim.attempt_count
            or row.lease_execution_public_id != claim.execution_public_id
            or row.lease_token_sha256 != _token_digest(claim.lease_token)
            or row.lease_expires_at is None
            or _aware_utc(row.lease_expires_at) <= now
        ):
            raise JobStateError("lease_mismatch", "job lease capability is stale, expired, or mismatched")
        return row

    @staticmethod
    def _validate_settlement(claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO) -> None:
        if settlement.execution_public_id != claim.execution_public_id:
            raise JobStateError("owned_execution_mismatch", "settlement does not identify the owned execution")

    @staticmethod
    def _clear_lease(row: Job) -> None:
        row.lease_owner = None
        row.lease_token_sha256 = None
        row.lease_execution_public_id = None
        row.last_heartbeat_at = None
        row.lease_expires_at = None

    @staticmethod
    def _revision(row: Job, expected: int) -> None:
        if row.revision != expected:
            raise JobStateError("revision_conflict", "job lifecycle revision changed")

    @staticmethod
    def _require(session: Session, public_id: str) -> Job:
        row = session.scalar(select(Job).where(Job.public_id == public_id))
        if row is None:
            raise JobStateError("unknown_job", "unknown job public ID")
        return row

    @staticmethod
    def _event(session: Session, row: Job, kind: str, reason: str | None, now: datetime) -> None:
        session.add(
            JobEvent(
                public_id=str(uuid4()),
                job_id=row.id,
                revision=row.revision,
                event_kind=kind,
                state=row.status,
                attempt_count=row.attempt_count,
                reason_code=reason,
                occurred_at=now,
            )
        )

    @staticmethod
    def _progress(row: Job) -> JobProgressDTO | None:
        if row.progress_completed is None:
            return None
        assert row.progress_total is not None and row.progress_unit is not None and row.progress_updated_at is not None
        return JobProgressDTO(
            row.progress_completed,
            row.progress_total,
            row.progress_unit,
            _aware_utc(row.progress_updated_at),
        )

    def _summary(self, session: Session, row: Job) -> JobSummaryDTO:
        replay_public_id = None
        if row.replay_id is not None:
            replay_public_id = session.scalar(select(Replay.public_id).where(Replay.id == row.replay_id))
        result_public_id = session.scalar(
            select(JobStageResult.public_id).where(JobStageResult.job_id == row.id)
        )
        error = None
        if row.error_code is not None:
            error = JobErrorSummaryDTO(
                row.error_code,
                self._redact(row.error_message or "Job failed."),
                bool(row.retryable),
            )
        return JobSummaryDTO(
            row.public_id,
            replay_public_id,
            row.stage,
            row.component_version,
            _state(row.status),
            row.revision,
            row.attempt_count,
            row.max_attempts,
            bool(row.retryable),
            _aware_utc(row.created_at),
            _aware_utc(row.started_at) if row.started_at is not None else None,
            _aware_utc(row.completed_at) if row.completed_at is not None else None,
            row.cancel_requested_at is not None,
            self._progress(row),
            error,
            result_public_id,
        )

    @staticmethod
    def _event_dto(row: JobEvent) -> JobEventSnapshotDTO:
        return JobEventSnapshotDTO(
            row.public_id,
            row.revision,
            row.event_kind,
            _state(row.state),
            row.attempt_count,
            row.reason_code,
            _aware_utc(row.occurred_at),
        )

    @staticmethod
    def _log_dto(job_public_id: str, row: JobLogSnapshot) -> JobLogReferenceDTO:
        return JobLogReferenceDTO(
            row.public_id,
            job_public_id,
            row.attempt_count,
            row.label,
            row.sequence,
            row.byte_count,
            _aware_utc(row.created_at),
        )

    @staticmethod
    def _mutation(row: Job) -> JobMutationDTO:
        return JobMutationDTO(row.public_id, _state(row.status), row.revision, row.attempt_count)


def replace_lease_expiry(claim: WorkerLeaseDTO, expiry: datetime) -> WorkerLeaseDTO:
    return WorkerLeaseDTO(
        claim.job_public_id,
        claim.execution_public_id,
        claim.stage,
        claim.attempt_count,
        claim.max_attempts,
        claim.lease_token,
        expiry,
    )
