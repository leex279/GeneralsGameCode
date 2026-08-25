"""Analytics-owned secure job lifecycle and immutable worker evidence."""

from __future__ import annotations

import codecs
import hashlib
import re
import secrets
import stat
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
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
from generals_replay_analyzer.storage import ContentAddressedStore, StoredContent

from .job_contracts import (
    DEFAULT_JOB_CLAIM_SELECTOR,
    CancelJobCommandDTO,
    CancellationReasonCode,
    JobClaimSelectorDTO,
    JobDetailDTO,
    JobErrorSummaryDTO,
    JobEventKind,
    JobEventSnapshotDTO,
    JobLifecycleError,
    JobLifecycleErrorCode,
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
    PublicJobReasonCode,
    RetryJobCommandDTO,
    StageExecutionOutcomeDTO,
    WorkerCancellationDTO,
    WorkerLeaseDTO,
)

_MAX_LOG_READ = 65_536
_BASE_LOG_REDACTION_OVERLAP = 4_096
_MAX_REDACTION_VALUE_BYTES = 16_384
# TheSuperHackers @fix Leex 22/08/2026 Authenticate bounded log pages and durable OSC checkpoints after restart. (#TBD)
_LOG_INTEGRITY_VERSION = "sha256-merkle-v1"
_LOG_INTEGRITY_CHUNK_SIZE = 4_096
_LOG_HASH_HEX_SIZE = 64
_LOG_MAX_TREE_DEPTH = 63
_LOG_LEAF_DOMAIN = b"job-log-merkle-v1:leaf\x00"
_LOG_PADDING_DOMAIN = b"job-log-merkle-v1:padding\x00"
_LOG_NODE_DOMAIN = b"job-log-merkle-v1:node\x00"
_LOG_ROOT_DOMAIN = b"job-log-merkle-v1:root\x00"
_LOWER_HEX_BYTES = frozenset(b"0123456789abcdef")
_ANSI_PATTERN = re.compile(r"(?:\x1b\[[0-?]*[ -/]*[@-~])|(?:\x1b\][^\x07]*(?:\x07|\x1b\\))")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_COMMAND_CREDENTIALS = re.compile(
    r"(?i)(--(?:token|password|secret|api[-_]?key)|(?:token|password|secret|api[-_]?key)=)(?:\s+|)[^\s]+"
)
_PATH_PATTERN = re.compile(r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s:'\"]+[\\/])*[^\s:'\"]+")
_TRACEBACK_TERMINAL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):")
_TRACEBACK_CHAIN = (
    "During handling of the above exception",
    "The above exception was the direct cause",
)
JobStateError = JobLifecycleError
_PUBLIC_ERROR_MESSAGES: Mapping[PublicJobReasonCode, str] = {
    PublicJobReasonCode.ATTEMPTS_EXHAUSTED: "Job attempt budget is exhausted.",
    PublicJobReasonCode.DEPENDENCY_CANCELLED: "A required dependency was cancelled.",
    PublicJobReasonCode.DEPENDENCY_FAILED: "A required dependency failed.",
    PublicJobReasonCode.LEASE_EXPIRED: "Worker ownership expired before completion.",
    PublicJobReasonCode.MIGRATION_EXHAUSTED_PENDING: "Legacy pending work had exhausted its attempts.",
    PublicJobReasonCode.MIGRATION_RECOVERED_RUNNING: "Legacy running work was recovered without a capability.",
    PublicJobReasonCode.OWNED_CHILD_SETTLEMENT_FAILED: "Owned child-tree settlement could not be proven.",
    PublicJobReasonCode.RESULT_IDENTITY_MISMATCH: "Durable result identity did not match the job.",
    PublicJobReasonCode.STAGE_FAILED: "Stage execution failed.",
    PublicJobReasonCode.USER_CANCELLED: "Job was cancelled by an operator.",
    PublicJobReasonCode.USER_REQUEST: "Cancellation was requested by an operator.",
    PublicJobReasonCode.WORKER_SHUTDOWN: "Worker shutdown interrupted the job.",
}


def _domain_error(code: JobLifecycleErrorCode | str, message: str) -> JobLifecycleError:
    return JobLifecycleError(JobLifecycleErrorCode(code), message)


def _is_busy(error: OperationalError) -> bool:
    message = str(error).casefold()
    return "database is locked" in message or "database is busy" in message


def _public_reason(value: str | None) -> PublicJobReasonCode:
    if value is None:
        return PublicJobReasonCode.STAGE_FAILED
    try:
        return PublicJobReasonCode(value)
    except (TypeError, ValueError):
        return PublicJobReasonCode.STAGE_FAILED


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _state(value: str) -> JobState:
    return JobState(value)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_seconds(value: int) -> timedelta:
    if type(value) is not int or value < 1 or value > 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    return timedelta(seconds=value)


def _advance_osc_state(state: tuple[bool, bool], data: bytes) -> tuple[bool, bool]:
    """Advance the closed OSC parser used by publication checkpoints and page reads."""
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


def _log_integrity_shape(public_size: int) -> tuple[int, int, int, int] | None:
    """Return the canonical bounded tree shape without allocating from persisted metadata."""
    if type(public_size) is not int or public_size < 0 or public_size >= 1 << 64:
        return None
    chunk_count = max(1, (public_size + _LOG_INTEGRITY_CHUNK_SIZE - 1) // _LOG_INTEGRITY_CHUNK_SIZE)
    capacity = 1 << (chunk_count - 1).bit_length()
    if capacity.bit_length() - 1 > _LOG_MAX_TREE_DEPTH:
        return None
    node_count = 2 * capacity - 1
    container_size = public_size + chunk_count + node_count * _LOG_HASH_HEX_SIZE
    return chunk_count, capacity, node_count, container_size


def _authenticated_log_container(content: bytes) -> tuple[bytes, str]:
    """Package public log bytes with canonical OSC checkpoints and a complete Merkle tree."""
    shape = _log_integrity_shape(len(content))
    if shape is None:  # pragma: no cover - an in-memory bytes value cannot reach the descriptor bound.
        raise ValueError("log content exceeds the integrity descriptor bound")
    chunk_count, capacity, _node_count, _container_size = shape
    nodes = [b""] * (2 * capacity - 1)
    checkpoints: list[int] = []
    state = (False, False)
    for index in range(chunk_count):
        start = index * _LOG_INTEGRITY_CHUNK_SIZE
        chunk = content[start : start + _LOG_INTEGRITY_CHUNK_SIZE]
        state_code = int(state[0]) | (int(state[1]) << 1)
        checkpoints.append(state_code)
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
    footer = bytes(ord("0") + value for value in checkpoints) + b"".join(
        node.hex().encode("ascii") for node in nodes
    )
    return content + footer, root


# TheSuperHackers @feature Leex 22/08/2026 Centralize every capability-owned job transition in one durable service. (#TBD)
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
        log_data_root: Path | None = None,
        redaction_values: Collection[str] = (),
        _stage_lease_seconds: Mapping[str, int] | None = None,
        _allow_legacy_worker_identifiers: bool = False,
    ) -> None:
        stages = tuple(sorted(set(registered_stages)))
        if any(not stage.strip() for stage in stages):
            raise ValueError("registered stages must be nonempty")
        if retry_base_delay < timedelta(0) or retry_max_delay < retry_base_delay:
            raise ValueError("retry delays are invalid")
        resolved_log_root = None if log_data_root is None else Path(log_data_root).resolve(strict=False)
        if log_store is not None:
            if resolved_log_root is None:
                raise ValueError("log_data_root is required with a managed log store")
            try:
                log_store.root.relative_to(resolved_log_root)
            except ValueError as error:
                raise ValueError("managed log store must be below log_data_root") from error
        self._session_factory = session_factory
        self._stages = stages
        self._clock = clock
        self._terminal_failure_stages = {
            stage: frozenset(values) for stage, values in (terminal_failure_stages or {}).items()
        }
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        # TheSuperHackers @fix Leex 25/08/2026 Honor bounded per-stage leases for long telemetry and rendering jobs. (#TBD)
        self._stage_lease_seconds = {
            stage: int(_validate_seconds(seconds).total_seconds())
            for stage, seconds in (_stage_lease_seconds or {}).items()
            if stage in stages
        }
        self._log_store = log_store
        self._log_data_root = resolved_log_root
        normalized_redactions: set[str] = set()
        maximum_redaction_bytes = 0
        for value in redaction_values:
            if not isinstance(value, str) or not value:
                raise ValueError("log redaction values must be nonempty strings")
            value_bytes = len(value.encode("utf-8"))
            if value_bytes > _MAX_REDACTION_VALUE_BYTES:
                raise ValueError("log redaction value exceeds the operational byte bound")
            normalized_redactions.add(value)
            maximum_redaction_bytes = max(maximum_redaction_bytes, value_bytes)
        self._redaction_values = tuple(sorted(normalized_redactions, key=len, reverse=True))
        self._log_redaction_overlap = max(
            _BASE_LOG_REDACTION_OVERLAP,
            maximum_redaction_bytes + 4,
        )
        self._allow_legacy_worker_identifiers = _allow_legacy_worker_identifiers

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

    @contextmanager
    def _writer(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            session.commit()
        except JobLifecycleError:
            session.rollback()
            raise
        except OperationalError as error:
            session.rollback()
            if _is_busy(error):
                raise _domain_error("lifecycle_busy", "job lifecycle writer is busy") from error
            raise _domain_error("lifecycle_conflict", "job lifecycle database operation failed") from error
        except IntegrityError as error:
            session.rollback()
            raise _domain_error("lifecycle_conflict", "job lifecycle database invariant changed") from error
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _retry_at(self, now: datetime, attempt_count: int) -> datetime:
        multiplier: int = 2 ** max(0, attempt_count - 1)
        candidate: timedelta = self._retry_base_delay * multiplier
        delay: timedelta = min(candidate, self._retry_max_delay)
        return now + delay

    def claim_next(
        self,
        worker_public_id: str,
        lease_seconds: int,
        selector: JobClaimSelectorDTO = DEFAULT_JOB_CLAIM_SELECTOR,
    ) -> WorkerLeaseDTO | None:
        worker = self._worker_identifier(worker_public_id)
        default_duration = _validate_seconds(lease_seconds)
        if type(selector) is not JobClaimSelectorDTO:
            raise TypeError("selector must be an exact JobClaimSelectorDTO")
        if not self._stages:
            return None
        with self._writer() as session:
            now = self.now()
            self._recover(session, now)
            self._project_dependency_terminals(session, now)
            selected_stages = (
                self._stages
                if not selector.stages
                else tuple(stage for stage in selector.stages if stage in self._stages)
            )
            selected_replay_id: int | None = None
            if selector.replay_public_id is not None:
                selected_replay_id = session.scalar(
                    select(Replay.id).where(Replay.public_id == selector.replay_public_id)
                )
                if selected_replay_id is None:
                    return None
            candidate_query = (
                self._candidate_query(now)
                if selector == DEFAULT_JOB_CLAIM_SELECTOR
                else self._candidate_query(
                    now,
                    replay_id=selected_replay_id,
                    selected_stages=selected_stages,
                    selected_public_ids=selector.job_public_ids or None,
                )
            )
            for candidate in session.scalars(candidate_query):
                if not self._dependencies_satisfied(session, candidate):
                    continue
                duration = timedelta(
                    seconds=self._stage_lease_seconds.get(
                        candidate.stage,
                        int(default_duration.total_seconds()),
                    )
                )
                token = secrets.token_urlsafe(32)
                execution_public_id = str(uuid4())
                next_revision = candidate.revision + 1
                claim_conditions: list[Any] = [
                    Job.id == candidate.id,
                    Job.status == "pending",
                    Job.available_at <= now,
                    Job.attempt_count < Job.max_attempts,
                    Job.revision == candidate.revision,
                ]
                if selector.replay_public_id is not None:
                    claim_conditions.append(Job.replay_id == selected_replay_id)
                if selector != DEFAULT_JOB_CLAIM_SELECTOR:
                    claim_conditions.append(Job.stage.in_(selected_stages))
                if selector.job_public_ids:
                    claim_conditions.append(Job.public_id.in_(selector.job_public_ids))
                result = session.execute(
                    update(Job)
                    .where(*claim_conditions)
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

    def _candidate_query(
        self,
        now: datetime,
        *,
        replay_id: int | None = None,
        selected_stages: tuple[str, ...] | None = None,
        selected_public_ids: tuple[str, ...] | None = None,
    ) -> Select[tuple[Job]]:
        stages = self._stages if selected_stages is None else selected_stages
        query = (
            select(Job)
            .where(
                Job.status == "pending",
                Job.available_at <= now,
                Job.attempt_count < Job.max_attempts,
                Job.stage.in_(stages),
            )
            .order_by(Job.priority.desc(), Job.available_at, Job.id)
        )
        if replay_id is not None:
            query = query.where(Job.replay_id == replay_id)
        if selected_public_ids is not None:
            query = query.where(Job.public_id.in_(selected_public_ids))
        return query

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
        exhausted = list(
            session.scalars(
                select(Job)
                .where(Job.status == "pending", Job.attempt_count >= Job.max_attempts)
                .order_by(Job.id)
            )
        )
        for row in exhausted:
            row.status = "failed"
            row.retryable = False
            row.completed_at = now
            row.error_code = PublicJobReasonCode.ATTEMPTS_EXHAUSTED.value
            row.error_message = "job reached its attempt limit before claim"
            row.error_details_json = {}
            row.revision += 1
            self._event(session, row, "failed", PublicJobReasonCode.ATTEMPTS_EXHAUSTED, now)
        expired = list(
            session.scalars(
                select(Job)
                .where(Job.status == "running", Job.lease_expires_at.is_not(None), Job.lease_expires_at <= now)
                .order_by(Job.id)
            )
        )
        for row in expired:
            durable = session.scalar(
                select(JobStageResult).where(
                    JobStageResult.job_id == row.id,
                    JobStageResult.idempotency_key == row.idempotency_key,
                    JobStageResult.stage == row.stage,
                    JobStageResult.component_version == row.component_version,
                )
            )
            any_durable = durable or session.scalar(
                select(JobStageResult).where(JobStageResult.job_id == row.id)
            )
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
                self._event(session, row, "failed", PublicJobReasonCode.OWNED_CHILD_SETTLEMENT_FAILED, now)
            elif any_durable is not None:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
                row.error_code = PublicJobReasonCode.RESULT_IDENTITY_MISMATCH.value
                row.error_message = "durable result identity does not match the owned job stage"
                row.error_details_json = {}
                self._event(session, row, "failed", PublicJobReasonCode.RESULT_IDENTITY_MISMATCH, now)
            elif row.retryable and row.attempt_count < row.max_attempts:
                row.status = "pending"
                row.available_at = self._retry_at(now, row.attempt_count)
                row.completed_at = None
                row.error_code = "lease_expired"
                row.error_message = "worker lease expired before completion"
                row.error_details_json = {}
                self._event(session, row, "lease_expired", PublicJobReasonCode.LEASE_EXPIRED, now)
            else:
                row.status = "failed"
                row.retryable = False
                row.completed_at = now
                row.error_code = "lease_expired"
                row.error_message = "worker lease expired with no retry budget"
                row.error_details_json = {}
                self._event(session, row, "failed", PublicJobReasonCode.LEASE_EXPIRED, now)
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
                self._event(session, row, reason, PublicJobReasonCode(reason), now)
                changed = True
            if changed:
                session.flush()

    def heartbeat(self, worker_public_id: str, claim: WorkerLeaseDTO, lease_seconds: int) -> WorkerLeaseDTO:
        duration = _validate_seconds(lease_seconds)
        with self._writer() as session:
            now = self.now()
            result = session.execute(
                update(Job)
                .where(*self._lease_conditions(worker_public_id, claim, now))
                .values(last_heartbeat_at=now, lease_expires_at=now + duration)
                .execution_options(synchronize_session=False)
            )
            self._require_cas(result, "lease_mismatch", "job lease changed before heartbeat")
            return replace_lease_expiry(claim, now + duration)

    def cancellation(self, worker_public_id: str, claim: WorkerLeaseDTO) -> WorkerCancellationDTO:
        with self._session_factory() as session:
            row = self._owned(session, worker_public_id, claim, self.now())
            reason = None if row.cancel_reason_code is None else CancellationReasonCode(row.cancel_reason_code)
            return WorkerCancellationDTO(row.cancel_requested_at is not None, reason)

    def report_progress(
        self, worker_public_id: str, claim: WorkerLeaseDTO, progress: JobProgressDTO
    ) -> None:
        with self._writer() as session:
            row = self._owned(session, worker_public_id, claim, self.now())
            current = self._progress(row)
            if current == progress:
                return
            if current is not None and (
                progress.completed < current.completed
                or progress.total < current.total
                or progress.unit != current.unit
                or progress.updated_at < current.updated_at
            ):
                raise JobStateError("progress_regressed", "progress must be monotonic within one attempt")
            now = self.now()
            result = session.execute(
                update(Job)
                .where(
                    *self._lease_conditions(worker_public_id, claim, now),
                    Job.revision == row.revision,
                )
                .values(
                    progress_completed=progress.completed,
                    progress_total=progress.total,
                    progress_unit=progress.unit,
                    progress_updated_at=progress.updated_at,
                    revision=row.revision + 1,
                )
                .execution_options(synchronize_session=False)
            )
            self._require_cas(result, "lease_mismatch", "job lease changed before progress publication")
            session.expire_all()
            row = self._require(session, claim.job_public_id)
            self._event(session, row, "progress", None, now)

    def persist_stage_result(self, claim: WorkerLeaseDTO, output: Mapping[str, Any]) -> str:
        with self._writer() as session:
            row = self._claim_identity(session, claim, self.now())
            return self._persist_result(session, row, output)

    def persist_stage_result_for_execution(
        self, job_public_id: str, execution_public_id: str, output: Mapping[str, Any]
    ) -> str:
        """Persist executor output after rechecking the exact live execution identity."""
        with self._writer() as session:
            row = self._execution(session, job_public_id, execution_public_id, self.now())
            return self._persist_result(session, row, output)

    def _persist_result(self, session: Session, row: Job, output: Mapping[str, Any]) -> str:
        existing = session.scalar(select(JobStageResult).where(JobStageResult.job_id == row.id))
        if existing is not None:
            if (
                existing.idempotency_key != row.idempotency_key
                or existing.stage != row.stage
                or existing.component_version != row.component_version
                or existing.output_json != dict(output)
            ):
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
        with self._writer() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            if row.cancel_requested_at is not None:
                raise JobStateError("cancellation_requested", "success cannot settle after cancellation wins")
            result = session.scalar(
                select(JobStageResult).where(
                    JobStageResult.public_id == result_public_id,
                    JobStageResult.job_id == row.id,
                    JobStageResult.idempotency_key == row.idempotency_key,
                    JobStageResult.stage == row.stage,
                    JobStageResult.component_version == row.component_version,
                )
            )
            if result is None:
                raise JobStateError("unknown_result", "durable stage result is unavailable")
            next_revision = row.revision + 1
            mutation = session.execute(
                update(Job)
                .where(
                    *self._lease_conditions(worker_public_id, claim, now),
                    Job.revision == row.revision,
                    Job.cancel_requested_at.is_(None),
                )
                .values(
                    status="succeeded",
                    output_json=cast(dict[str, Any], result.output_json),
                    retryable=False,
                    completed_at=now,
                    error_code=None,
                    error_message=None,
                    error_details_json=None,
                    revision=next_revision,
                    **self._cleared_lease_values(),
                )
                .execution_options(synchronize_session=False)
            )
            self._require_cas(mutation, "lease_mismatch", "job lease changed before success settlement")
            session.expire_all()
            row = self._require(session, claim.job_public_id)
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
        with self._writer() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            if row.cancel_requested_at is not None:
                raise JobStateError("cancellation_requested", "failure cannot release a cancellation-owned child tree")
            retry = outcome.status == "retryable_failure" and outcome.retryable
            values: dict[str, Any] = {
                **self._cleared_lease_values(),
                "progress_completed": None,
                "progress_total": None,
                "progress_unit": None,
                "progress_updated_at": None,
                "error_code": outcome.error_code,
                "error_message": self._redact(outcome.error_message),
                "error_details_json": dict(safe_details or {}),
                "revision": row.revision + 1,
            }
            if retry and row.cancel_requested_at is None and row.attempt_count < row.max_attempts:
                values.update(
                    status="pending",
                    retryable=True,
                    available_at=self._retry_at(now, row.attempt_count),
                    completed_at=None,
                )
            else:
                values.update(status="failed", retryable=False, completed_at=now)
            mutation = session.execute(
                update(Job)
                .where(
                    *self._lease_conditions(worker_public_id, claim, now),
                    Job.revision == row.revision,
                    Job.cancel_requested_at.is_(None),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            self._require_cas(mutation, "lease_mismatch", "job lease changed before failure settlement")
            session.expire_all()
            row = self._require(session, claim.job_public_id)
            self._event(session, row, "failed", PublicJobReasonCode.STAGE_FAILED, now)

    def settle_cancelled(
        self, worker_public_id: str, claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None:
        with self._writer() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            self._validate_settlement(claim, settlement)
            if row.cancel_requested_at is None:
                raise JobStateError("cancellation_not_requested", "owned execution cannot settle cancelled without a request")
            values: dict[str, Any] = {
                **self._cleared_lease_values(),
                "progress_completed": None,
                "progress_total": None,
                "progress_unit": None,
                "progress_updated_at": None,
                "retryable": False,
                "completed_at": now,
                "revision": row.revision + 1,
                "error_details_json": {},
            }
            if settlement.tree_settled:
                values.update(
                    status="cancelled",
                    error_code=PublicJobReasonCode.USER_CANCELLED.value,
                    error_message="job cancelled after owned execution tree settled",
                )
                event_reason = PublicJobReasonCode.USER_REQUEST
                event_kind = "cancelled"
            else:
                values.update(
                    status="failed",
                    error_code=PublicJobReasonCode.OWNED_CHILD_SETTLEMENT_FAILED.value,
                    error_message="owned execution tree settlement was uncertain",
                )
                event_reason = PublicJobReasonCode.OWNED_CHILD_SETTLEMENT_FAILED
                event_kind = "failed"
            mutation = session.execute(
                update(Job)
                .where(
                    *self._lease_conditions(worker_public_id, claim, now),
                    Job.revision == row.revision,
                    Job.cancel_requested_at.is_not(None),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            self._require_cas(mutation, "lease_mismatch", "job lease changed before cancellation settlement")
            session.expire_all()
            row = self._require(session, claim.job_public_id)
            self._event(session, row, event_kind, event_reason, now)

    def release_after_shutdown(
        self, worker_public_id: str, claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None:
        with self._writer() as session:
            now = self.now()
            row = self._owned(session, worker_public_id, claim, now)
            self._validate_settlement(claim, settlement)
            values: dict[str, Any] = {
                **self._cleared_lease_values(),
                "progress_completed": None,
                "progress_total": None,
                "progress_unit": None,
                "progress_updated_at": None,
                "revision": row.revision + 1,
                "error_details_json": {},
            }
            if not settlement.tree_settled:
                values.update(
                    status="failed",
                    retryable=False,
                    completed_at=now,
                    error_code=PublicJobReasonCode.OWNED_CHILD_SETTLEMENT_FAILED.value,
                    error_message="owned execution tree settlement was uncertain",
                )
                event_kind = "failed"
                event_reason = PublicJobReasonCode.OWNED_CHILD_SETTLEMENT_FAILED
            elif row.cancel_requested_at is not None:
                values.update(
                    status="cancelled",
                    retryable=False,
                    completed_at=now,
                    error_code=PublicJobReasonCode.USER_CANCELLED.value,
                    error_message="job cancelled during worker shutdown",
                )
                event_kind = "cancelled"
                event_reason = PublicJobReasonCode.USER_REQUEST
            elif row.retryable and row.attempt_count < row.max_attempts:
                values.update(
                    status="pending",
                    available_at=self._retry_at(now, row.attempt_count),
                    completed_at=None,
                    error_code=PublicJobReasonCode.WORKER_SHUTDOWN.value,
                    error_message="worker shut down after owned execution tree settled",
                )
                event_kind = "worker_shutdown"
                event_reason = PublicJobReasonCode.WORKER_SHUTDOWN
            else:
                values.update(
                    status="failed",
                    retryable=False,
                    completed_at=now,
                    error_code=PublicJobReasonCode.WORKER_SHUTDOWN.value,
                    error_message="worker shutdown exhausted retry budget",
                )
                event_kind = "failed"
                event_reason = PublicJobReasonCode.WORKER_SHUTDOWN
            mutation = session.execute(
                update(Job)
                .where(
                    *self._lease_conditions(worker_public_id, claim, now),
                    Job.revision == row.revision,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            self._require_cas(mutation, "lease_mismatch", "job lease changed before shutdown release")
            session.expire_all()
            row = self._require(session, claim.job_public_id)
            self._event(session, row, event_kind, event_reason, now)

    def cancel_job(self, command: CancelJobCommandDTO) -> JobMutationDTO:
        actor = command.requested_by.strip()
        reason = command.reason_code.value
        if not actor or not reason:
            raise ValueError("cancellation actor and reason must be nonempty")
        with self._writer() as session:
            now = self.now()
            row = self._require(session, command.job_public_id)
            self._revision(row, command.expected_revision)
            if row.status == "pending":
                mutation = session.execute(
                    update(Job)
                    .where(
                        Job.id == row.id,
                        Job.status == "pending",
                        Job.revision == command.expected_revision,
                        Job.cancel_requested_at.is_(None),
                    )
                    .values(
                        status="cancelled",
                        retryable=False,
                        completed_at=now,
                        cancel_requested_at=now,
                        cancel_requested_by=actor,
                        cancel_reason_code=reason,
                        error_code=PublicJobReasonCode.USER_CANCELLED.value,
                        error_message="job cancelled before claim",
                        error_details_json={},
                        revision=row.revision + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                self._require_cas(mutation, "revision_conflict", "job changed before pending cancellation")
                session.expire_all()
                row = self._require(session, command.job_public_id)
                self._event(session, row, "cancelled", PublicJobReasonCode.USER_REQUEST, now)
                self._cancel_descendants(session, row.id, now)
            elif row.status == "running" and row.cancel_requested_at is None:
                mutation = session.execute(
                    update(Job)
                    .where(
                        Job.id == row.id,
                        Job.status == "running",
                        Job.revision == command.expected_revision,
                        Job.cancel_requested_at.is_(None),
                    )
                    .values(
                        cancel_requested_at=now,
                        cancel_requested_by=actor,
                        cancel_reason_code=reason,
                        revision=row.revision + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                self._require_cas(mutation, "revision_conflict", "job changed before running cancellation")
                session.expire_all()
                row = self._require(session, command.job_public_id)
                self._event(session, row, "cancel_requested", PublicJobReasonCode.USER_REQUEST, now)
            else:
                raise JobStateError("terminal_job", "job cannot accept cancellation in its current state")
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
                self._event(session, child, "dependency_cancelled", PublicJobReasonCode.DEPENDENCY_CANCELLED, now)

    def retry_job(self, command: RetryJobCommandDTO) -> JobMutationDTO:
        with self._writer() as session:
            row = self._require(session, command.job_public_id)
            self._revision(row, command.expected_revision)
            if row.status != "failed":
                raise JobStateError("ineligible_retry", "only failed work can be explicitly retried")
            if not row.retryable or row.attempt_count >= row.max_attempts:
                raise JobStateError("ineligible_retry", "job is not retryable or has exhausted attempts")
            now = self.now()
            mutation = session.execute(
                update(Job)
                .where(
                    Job.id == row.id,
                    Job.status == "failed",
                    Job.revision == command.expected_revision,
                    Job.retryable.is_(True),
                    Job.attempt_count < Job.max_attempts,
                )
                .values(
                    status="pending",
                    available_at=now,
                    completed_at=None,
                    error_code=None,
                    error_message=None,
                    error_details_json=None,
                    revision=row.revision + 1,
                )
                .execution_options(synchronize_session=False)
            )
            self._require_cas(mutation, "revision_conflict", "job changed before retry")
            session.expire_all()
            row = self._require(session, command.job_public_id)
            self._event(session, row, "retry_requested", None, now)
            return self._mutation(row)

    def retry_legacy(self, job_public_id: str) -> None:
        """Preserve Task 3's unrevisioned CLI facade while keeping transitions centralized here."""
        with self._writer() as session:
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
            mutation = session.execute(
                update(Job)
                .where(
                    Job.id == row.id,
                    Job.status == row.status,
                    Job.revision == row.revision,
                    Job.retryable.is_(True),
                    Job.attempt_count < Job.max_attempts,
                )
                .values(
                    status="pending",
                    available_at=now,
                    completed_at=None,
                    error_code=None,
                    error_message=None,
                    error_details_json=None,
                    revision=row.revision + 1,
                )
                .execution_options(synchronize_session=False)
            )
            self._require_cas(mutation, "revision_conflict", "job changed before legacy retry")
            session.expire_all()
            row = self._require(session, job_public_id)
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
                statement = statement.where(
                    or_(
                        Job.created_at < cursor.created_at,
                        and_(Job.created_at == cursor.created_at, Job.id < cursor.id),
                    )
                )
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
        assert self._log_data_root is not None
        if label not in {"stdout", "stderr", "supervisor"} or sequence < 0:
            raise ValueError("log stream identity is invalid")
        if not isinstance(raw_output, bytes):
            raise TypeError("raw supervisor output must be bytes")
        stored = None
        try:
            with self._session_factory() as session:
                self._owned(session, worker_public_id, claim, self.now())
            redacted = self._redact(raw_output.decode("utf-8", errors="replace")).encode("utf-8")
            container, integrity_root = _authenticated_log_container(redacted)
            stored = self._log_store.store_bytes(container)
            stored_path = stored.path.resolve(strict=False)
            expected_path = (self._log_store.root / stored.sha256[:2] / stored.sha256).resolve(strict=False)
            if stored_path != expected_path:
                raise JobStateError("log_asset_conflict", "managed log store returned an unexpected locator")
            with self._writer() as session:
                now = self.now()
                row = self._owned(session, worker_public_id, claim, now)
                try:
                    stored_info = stored_path.lstat()
                except OSError as error:
                    raise JobStateError("log_asset_conflict", "managed log object disappeared before registration") from error
                if not stat.S_ISREG(stored_info.st_mode) or stored_path.is_symlink() or stored_info.st_size != stored.size:
                    raise JobStateError("log_asset_conflict", "managed log object changed before registration")
                last_sequence = session.scalar(
                    select(func.max(JobLogSnapshot.sequence)).where(
                        JobLogSnapshot.job_id == row.id,
                        JobLogSnapshot.attempt_count == row.attempt_count,
                        JobLogSnapshot.label == label,
                    )
                )
                expected_sequence = 0 if last_sequence is None else last_sequence + 1
                if sequence != expected_sequence:
                    raise JobStateError("log_sequence_conflict", "job log sequence is stale or nonmonotonic")
                relative_path = stored_path.relative_to(self._log_data_root).as_posix()
                asset = session.scalar(select(ManagedAsset).where(ManagedAsset.sha256 == stored.sha256))
                if asset is not None and (
                    asset.kind != "job_log_snapshot"
                    or asset.relative_path != relative_path
                    or asset.size_bytes != stored.size
                    or asset.media_type != "text/plain"
                ):
                    raise JobStateError("log_asset_conflict", "managed digest is registered for another asset role")
                if asset is None:
                    asset = ManagedAsset(
                        public_id=str(uuid4()),
                        sha256=stored.sha256,
                        kind="job_log_snapshot",
                        relative_path=relative_path,
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
                    byte_count=len(redacted),
                    redaction_version="job-log-redaction-v1",
                    integrity_version=_LOG_INTEGRITY_VERSION,
                    integrity_root_sha256=integrity_root,
                    integrity_chunk_size=_LOG_INTEGRITY_CHUNK_SIZE,
                    created_at=now,
                )
                session.add(snapshot)
                session.flush()
                reference = self._log_dto(row.public_id, snapshot)
            return reference
        except Exception:
            if stored is not None and stored.created:
                self._cleanup_unreferenced_log(stored)
            raise

    def _cleanup_unreferenced_log(self, stored: StoredContent) -> None:
        """Remove only an unregistered CAS object while serialized with registrations."""
        assert self._log_store is not None
        try:
            stored_path = stored.path.resolve(strict=False)
            expected_path = (self._log_store.root / stored.sha256[:2] / stored.sha256).resolve(strict=False)
            if stored_path != expected_path:
                return
            with self._writer() as session:
                referenced = session.scalar(
                    select(ManagedAsset.id).where(ManagedAsset.sha256 == stored.sha256).limit(1)
                )
                if referenced is None:
                    stored_path.unlink(missing_ok=True)
        except (JobLifecycleError, OSError):
            # An orphan is safer than deleting a digest whose registration could not be excluded.
            return

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
            asset_kind = asset.kind
            asset_relative_path = asset.relative_path
            asset_size = asset.size_bytes
            asset_media_type = asset.media_type
            public_size = snapshot.byte_count
            snapshot_media_type = snapshot.media_type
            redaction_version = snapshot.redaction_version
            integrity_version = snapshot.integrity_version
            integrity_root = snapshot.integrity_root_sha256
            integrity_chunk_size = snapshot.integrity_chunk_size
        if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            return JobLogChunkDTO("unavailable", "", None)
        stored_path = (self._log_store.root / digest[:2] / digest).resolve(strict=False)
        try:
            stored_info = stored_path.lstat()
        except OSError:
            return JobLogChunkDTO("unavailable", "", None)
        try:
            expected_path = stored_path.relative_to(self._log_data_root).as_posix() if self._log_data_root else ""
        except ValueError:
            return JobLogChunkDTO("unavailable", "", None)
        shape = _log_integrity_shape(public_size)
        if (
            not stat.S_ISREG(stored_info.st_mode)
            or stored_path.is_symlink()
            or asset_kind != "job_log_snapshot"
            or asset_relative_path != expected_path
            or asset_media_type != "text/plain"
            or snapshot_media_type != "text/plain"
            or redaction_version != "job-log-redaction-v1"
            or integrity_version != _LOG_INTEGRITY_VERSION
            or integrity_chunk_size != _LOG_INTEGRITY_CHUNK_SIZE
            or len(integrity_root) != 64
            or any(value not in "0123456789abcdef" for value in integrity_root)
            or shape is None
            or asset_size != stored_info.st_size
            or shape[3] != stored_info.st_size
        ):
            return JobLogChunkDTO("unavailable", "", None)
        if query.offset >= public_size:
            return JobLogChunkDTO("rotated", "", None)
        limit = min(query.limit, _MAX_LOG_READ)
        candidate_end = min(public_size, query.offset + max(4, limit))
        requested_context_start = max(0, query.offset - self._log_redaction_overlap)
        requested_context_end = min(
            public_size,
            candidate_end + self._log_redaction_overlap + 3,
        )
        try:
            window, window_start, stream_state = self._authenticated_log_window(
                stored_path,
                public_size=public_size,
                integrity_root=integrity_root,
                offset=query.offset,
                window_start=requested_context_start,
                window_end=requested_context_end,
            )
            candidate = window[
                query.offset - window_start : candidate_end - window_start
            ]
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            decoder.decode(candidate, final=candidate_end == public_size)
            pending, _state = decoder.getstate()
            end = candidate_end - len(pending)
            if end <= query.offset:
                return JobLogChunkDTO("unavailable", "", None)
            context_start = requested_context_start
            while (
                context_start < query.offset
                and window[context_start - window_start] & 0xC0 == 0x80
            ):
                context_start += 1
            context_end = min(public_size, end + self._log_redaction_overlap + 3)
            data = window[context_start - window_start : context_end - window_start]
            content = self._redacted_log_range(
                data,
                context_start=context_start,
                offset=query.offset,
                end=end,
                size=public_size,
                stream_state=stream_state,
            )
        except (OSError, OverflowError, UnicodeDecodeError, ValueError):
            return JobLogChunkDTO("unavailable", "", None)
        content = self._bounded_utf8(content, limit)
        return JobLogChunkDTO("available", content, end if end < public_size else None)

    @staticmethod
    def _authenticated_log_window(
        path: Path,
        *,
        public_size: int,
        integrity_root: str,
        offset: int,
        window_start: int,
        window_end: int,
    ) -> tuple[bytes, int, tuple[bool, bool]]:
        """Read only a bounded content window and its independently rooted authentication paths."""
        shape = _log_integrity_shape(public_size)
        if shape is None or not (0 <= window_start <= offset < window_end <= public_size):
            raise ValueError("invalid authenticated log window")
        chunk_count, capacity, _node_count, _container_size = shape
        first_chunk = window_start // _LOG_INTEGRITY_CHUNK_SIZE
        last_chunk = (window_end - 1) // _LOG_INTEGRITY_CHUNK_SIZE
        nodes_start = public_size + chunk_count
        node_cache: dict[int, bytes] = {}
        chunks: dict[int, bytes] = {}
        checkpoints: dict[int, int] = {}

        with path.open("rb") as source:

            def read_exact(position: int, length: int) -> bytes:
                source.seek(position)
                value = source.read(length)
                if len(value) != length:
                    raise ValueError("truncated authenticated log container")
                return value

            def read_node(index: int) -> bytes:
                cached = node_cache.get(index)
                if cached is not None:
                    return cached
                encoded = read_exact(nodes_start + index * _LOG_HASH_HEX_SIZE, _LOG_HASH_HEX_SIZE)
                if any(value not in _LOWER_HEX_BYTES for value in encoded):
                    raise ValueError("noncanonical authenticated log proof")
                decoded = bytes.fromhex(encoded.decode("ascii"))
                node_cache[index] = decoded
                return decoded

            tree_root = read_node(0)
            descriptor_root = hashlib.sha256(
                _LOG_ROOT_DOMAIN
                + public_size.to_bytes(8, "big")
                + _LOG_INTEGRITY_CHUNK_SIZE.to_bytes(4, "big")
                + chunk_count.to_bytes(8, "big")
                + tree_root
            ).hexdigest()
            if not secrets.compare_digest(descriptor_root, integrity_root):
                raise ValueError("authenticated log root mismatch")

            for chunk_index in range(first_chunk, last_chunk + 1):
                chunk_start = chunk_index * _LOG_INTEGRITY_CHUNK_SIZE
                chunk_length = min(_LOG_INTEGRITY_CHUNK_SIZE, public_size - chunk_start)
                chunk = read_exact(chunk_start, chunk_length)
                encoded_checkpoint = read_exact(public_size + chunk_index, 1)
                if encoded_checkpoint[0] < ord("0") or encoded_checkpoint[0] > ord("3"):
                    raise ValueError("invalid authenticated log checkpoint")
                checkpoint = encoded_checkpoint[0] - ord("0")
                chunks[chunk_index] = chunk
                checkpoints[chunk_index] = checkpoint
                computed = hashlib.sha256(
                    _LOG_LEAF_DOMAIN
                    + chunk_index.to_bytes(8, "big")
                    + bytes((checkpoint,))
                    + chunk_length.to_bytes(4, "big")
                    + chunk
                ).digest()
                node_index = capacity - 1 + chunk_index
                while node_index > 0:
                    if node_index % 2:
                        computed = hashlib.sha256(
                            _LOG_NODE_DOMAIN + computed + read_node(node_index + 1)
                        ).digest()
                    else:
                        computed = hashlib.sha256(
                            _LOG_NODE_DOMAIN + read_node(node_index - 1) + computed
                        ).digest()
                    node_index = (node_index - 1) // 2
                if not secrets.compare_digest(computed, tree_root):
                    raise ValueError("authenticated log page mismatch")

        authenticated_start = first_chunk * _LOG_INTEGRITY_CHUNK_SIZE
        authenticated = b"".join(chunks[index] for index in range(first_chunk, last_chunk + 1))
        offset_chunk = offset // _LOG_INTEGRITY_CHUNK_SIZE
        checkpoint = checkpoints[offset_chunk]
        state = (bool(checkpoint & 1), bool(checkpoint & 2))
        chunk_start = offset_chunk * _LOG_INTEGRITY_CHUNK_SIZE
        state = _advance_osc_state(state, chunks[offset_chunk][: offset - chunk_start])
        return authenticated, authenticated_start, state

    def _redacted_log_range(
        self,
        data: bytes,
        *,
        context_start: int,
        offset: int,
        end: int,
        size: int,
        stream_state: tuple[bool, bool],
    ) -> str:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text = decoder.decode(data, final=context_start + len(data) == size)
        pending, _state = decoder.getstate()
        if pending:
            data = data[: -len(pending)]
            text = data.decode("utf-8", errors="strict")
        mask = self._redaction_mask(
            text,
            context_start=context_start,
            context_end=context_start + len(data),
            size=size,
        )
        output: list[str] = []
        raw_position = context_start
        in_osc, pending_escape = stream_state
        for index, character in enumerate(text):
            character_end = raw_position + len(character.encode("utf-8"))
            if raw_position >= offset and character_end <= end:
                stream_hidden = False
                if in_osc:
                    stream_hidden = True
                    if character == "\x07" or (pending_escape and character == "\\"):
                        in_osc = False
                        pending_escape = False
                    else:
                        pending_escape = character == "\x1b"
                elif pending_escape:
                    stream_hidden = True
                    in_osc = character == "]"
                    pending_escape = False
                elif character == "\x1b":
                    stream_hidden = True
                    pending_escape = True
                if not mask[index] and not stream_hidden:
                    output.append(character)
            raw_position = character_end
            if raw_position >= end:
                break
        return "".join(output)

    def _redaction_mask(
        self,
        value: str,
        *,
        context_start: int,
        context_end: int,
        size: int,
    ) -> list[bool]:
        mask = [False] * len(value)

        def hide(start: int, end: int) -> None:
            mask[start:end] = [True] * (end - start)

        for pattern in (_ANSI_PATTERN, _URL_CREDENTIALS, _COMMAND_CREDENTIALS, _PATH_PATTERN):
            for match in pattern.finditer(value):
                hide(match.start(), match.end())
        for secret in self._redaction_values:
            for match in re.finditer(re.escape(secret), value, flags=re.IGNORECASE):
                hide(match.start(), match.end())

        if context_start > 0:
            first_newline = value.find("\n")
            hide(0, len(value) if first_newline < 0 else first_newline + 1)
        if context_end < size:
            last_newline = value.rfind("\n")
            hide(0 if last_newline < 0 else last_newline + 1, len(value))

        in_traceback = False
        position = 0
        for line in value.splitlines(keepends=True):
            stripped = line.lstrip()
            if stripped.startswith("Traceback (") or (
                not in_traceback
                and line[:1].isspace()
                and (stripped.startswith("File ") or "=" in stripped)
            ):
                in_traceback = True
                hide(position, position + len(line))
            elif in_traceback:
                hide(position, position + len(line))
                if _TRACEBACK_TERMINAL.match(stripped):
                    in_traceback = False
            elif stripped.startswith(_TRACEBACK_CHAIN) or stripped.startswith("File ") or _TRACEBACK_TERMINAL.match(stripped):
                hide(position, position + len(line))
            position += len(line)
        return mask

    def _redact(self, value: str) -> str:
        redacted = _ANSI_PATTERN.sub("", value)
        redacted = _URL_CREDENTIALS.sub(r"\1[credentials]@", redacted)
        redacted = _COMMAND_CREDENTIALS.sub(r"\1 [redacted]", redacted)
        for secret in self._redaction_values:
            redacted = re.sub(re.escape(secret), "[redacted]", redacted, flags=re.IGNORECASE)
        redacted = _PATH_PATTERN.sub("[path]", redacted)
        safe_lines = []
        in_traceback = False
        for line in redacted.splitlines(keepends=True):
            stripped = line.lstrip()
            if stripped.startswith("Traceback ("):
                in_traceback = True
                continue
            if in_traceback:
                if _TRACEBACK_TERMINAL.match(stripped):
                    in_traceback = False
                continue
            if stripped.startswith(_TRACEBACK_CHAIN):
                continue
            if stripped.startswith("File ") or _TRACEBACK_TERMINAL.match(stripped):
                continue
            safe_lines.append(line)
        return "".join(safe_lines)

    @staticmethod
    def _bounded_utf8(value: str, limit: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= limit:
            return value
        return encoded[:limit].decode("utf-8", errors="ignore")

    def _owned(self, session: Session, worker: str, claim: WorkerLeaseDTO, now: datetime) -> Job:
        worker = self._worker_identifier(worker)
        row = self._claim_identity(session, claim, now)
        if row.lease_owner != worker:
            raise JobStateError("lease_mismatch", "worker does not own this job lease")
        return row

    def _lease_conditions(
        self, worker: str, claim: WorkerLeaseDTO, now: datetime
    ) -> tuple[Any, ...]:
        worker = self._worker_identifier(worker)
        return (
            Job.public_id == claim.job_public_id,
            Job.status == "running",
            Job.lease_owner == worker,
            Job.attempt_count == claim.attempt_count,
            Job.lease_execution_public_id == claim.execution_public_id,
            Job.lease_token_sha256 == _token_digest(claim.lease_token),
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at > now,
        )

    def _worker_identifier(self, value: str) -> str:
        if self._allow_legacy_worker_identifiers:
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 255
                or any(character.isspace() or ord(character) < 32 for character in value)
            ):
                raise ValueError("legacy worker identifier is invalid")
            return value
        try:
            parsed = UUID(value)
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("worker_public_id must be a lowercase hyphenated UUID") from error
        if str(parsed) != value:
            raise ValueError("worker_public_id must be a lowercase hyphenated UUID")
        return value

    @staticmethod
    def _require_cas(result: Any, code: JobLifecycleErrorCode | str, message: str) -> None:
        if getattr(result, "rowcount", 0) != 1:
            raise _domain_error(code, message)

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
    def _cleared_lease_values() -> dict[str, None]:
        return {
            "lease_owner": None,
            "lease_token_sha256": None,
            "lease_execution_public_id": None,
            "last_heartbeat_at": None,
            "lease_expires_at": None,
        }

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
    def _event(
        session: Session,
        row: Job,
        kind: str,
        reason: PublicJobReasonCode | None,
        now: datetime,
    ) -> None:
        session.add(
            JobEvent(
                public_id=str(uuid4()),
                job_id=row.id,
                revision=row.revision,
                event_kind=kind,
                state=row.status,
                attempt_count=row.attempt_count,
                reason_code=None if reason is None else reason.value,
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
            select(JobStageResult.public_id).where(
                JobStageResult.job_id == row.id,
                JobStageResult.idempotency_key == row.idempotency_key,
                JobStageResult.stage == row.stage,
                JobStageResult.component_version == row.component_version,
            )
        )
        error = None
        if row.error_code is not None:
            public_reason = _public_reason(row.error_code)
            error = JobErrorSummaryDTO(
                public_reason,
                _PUBLIC_ERROR_MESSAGES[public_reason],
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
            JobEventKind(row.event_kind),
            _state(row.state),
            row.attempt_count,
            None if row.reason_code is None else PublicJobReasonCode(row.reason_code),
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
