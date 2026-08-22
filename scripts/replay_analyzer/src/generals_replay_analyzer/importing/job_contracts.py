"""ORM-free durable job contracts shared by workers and application adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Literal, Protocol
from uuid import UUID

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# TheSuperHackers @feature Leex 22/08/2026 Freeze the ORM-free lifecycle vocabulary shared by Web and external workers. (#TBD)
class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobEventKind(StrEnum):
    CLAIMED = "claimed"
    PROGRESS = "progress"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_REQUESTED = "retry_requested"
    LEASE_EXPIRED = "lease_expired"
    WORKER_SHUTDOWN = "worker_shutdown"
    DEPENDENCY_FAILED = "dependency_failed"
    DEPENDENCY_CANCELLED = "dependency_cancelled"
    RESULT_REUSED = "result_reused"


class CancellationReasonCode(StrEnum):
    USER_REQUEST = "user_request"


class PublicJobReasonCode(StrEnum):
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    DEPENDENCY_CANCELLED = "dependency_cancelled"
    DEPENDENCY_FAILED = "dependency_failed"
    LEASE_EXPIRED = "lease_expired"
    MIGRATION_EXHAUSTED_PENDING = "migration_exhausted_pending"
    MIGRATION_RECOVERED_RUNNING = "migration_recovered_running"
    OWNED_CHILD_SETTLEMENT_FAILED = "owned_child_settlement_failed"
    RESULT_IDENTITY_MISMATCH = "result_identity_mismatch"
    STAGE_FAILED = "stage_failed"
    USER_CANCELLED = "user_cancelled"
    USER_REQUEST = "user_request"
    WORKER_SHUTDOWN = "worker_shutdown"


class JobLifecycleErrorCode(StrEnum):
    CANCELLATION_NOT_REQUESTED = "cancellation_not_requested"
    CANCELLATION_REQUESTED = "cancellation_requested"
    EXECUTION_MISMATCH = "execution_mismatch"
    EXHAUSTED_JOB = "exhausted_job"
    INELIGIBLE_RETRY = "ineligible_retry"
    INVALID_JOB_STATE = "invalid_job_state"
    LEASE_MISMATCH = "lease_mismatch"
    LIFECYCLE_BUSY = "lifecycle_busy"
    LIFECYCLE_CONFLICT = "lifecycle_conflict"
    LOG_ASSET_CONFLICT = "log_asset_conflict"
    LOG_OWNERSHIP_MISMATCH = "log_ownership_mismatch"
    LOG_SEQUENCE_CONFLICT = "log_sequence_conflict"
    LOG_SINK_UNAVAILABLE = "log_sink_unavailable"
    NONRETRYABLE_JOB = "nonretryable_job"
    OWNED_EXECUTION_MISMATCH = "owned_execution_mismatch"
    PROGRESS_REGRESSED = "progress_regressed"
    RESULT_CONFLICT = "result_conflict"
    REVISION_CONFLICT = "revision_conflict"
    RUNNING_JOB = "running_job"
    SUCCESSFUL_JOB = "successful_job"
    TERMINAL_JOB = "terminal_job"
    UNKNOWN_CURSOR = "unknown_cursor"
    UNKNOWN_JOB = "unknown_job"
    UNKNOWN_LOG = "unknown_log"
    UNKNOWN_RESULT = "unknown_result"


@dataclass(frozen=True)
class JobLifecycleFailure:
    """Immutable stable failure value carried by the raised lifecycle exception."""

    code: JobLifecycleErrorCode
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", JobLifecycleErrorCode(self.code))
        if not self.message or len(self.message) > 512:
            raise ValueError("job lifecycle error message is invalid")


class JobLifecycleError(ValueError):
    """ORM-free exception wrapper; traceback state remains mutable as Python requires."""

    def __init__(self, code: JobLifecycleErrorCode | str, message: str) -> None:
        self.failure = JobLifecycleFailure(JobLifecycleErrorCode(code), message)
        super().__init__(self.failure.code.value, self.failure.message)

    @property
    def code(self) -> JobLifecycleErrorCode:
        return self.failure.code

    @property
    def message(self) -> str:
        return self.failure.message

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


def _uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a lowercase hyphenated UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{field_name} must be a lowercase hyphenated UUID")


def _utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or value.astimezone(UTC).utcoffset() != value.utcoffset():
        raise ValueError(f"{field_name} must be an aware UTC datetime")


def _code(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded lower-case stable code")


def _text(value: str, field_name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")


def _integer(value: int, field_name: str, *, minimum: int = 0) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an exact integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _boolean(value: bool, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be an exact boolean")


@dataclass(frozen=True)
class WorkerLeaseDTO:
    job_public_id: str
    execution_public_id: str
    stage: str
    attempt_count: int
    max_attempts: int
    lease_token: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.job_public_id, "job_public_id")
        _uuid(self.execution_public_id, "execution_public_id")
        _utc(self.lease_expires_at, "lease_expires_at")
        _integer(self.attempt_count, "attempt_count", minimum=1)
        _integer(self.max_attempts, "max_attempts", minimum=1)
        if (
            not self.stage
            or self.stage != self.stage.strip()
            or len(self.stage) > 64
            or self.attempt_count < 1
            or self.max_attempts < self.attempt_count
            or not self.lease_token
        ):
            raise ValueError("worker lease fields are invalid")


@dataclass(frozen=True)
class WorkerCancellationDTO:
    requested: bool
    reason_code: CancellationReasonCode | None

    def __post_init__(self) -> None:
        _boolean(self.requested, "requested")
        if self.reason_code is not None:
            object.__setattr__(self, "reason_code", CancellationReasonCode(self.reason_code))
        if self.requested != (self.reason_code is not None):
            raise ValueError("worker cancellation fields are inconsistent")


@dataclass(frozen=True)
class JobProgressDTO:
    completed: int
    total: int
    unit: str
    updated_at: datetime

    def __post_init__(self) -> None:
        _utc(self.updated_at, "updated_at")
        _integer(self.completed, "completed")
        _integer(self.total, "total", minimum=1)
        if self.completed < 0 or self.total <= 0 or self.completed > self.total:
            raise ValueError("job progress is invalid")
        _text(self.unit, "progress unit", 64)


@dataclass(frozen=True)
class JobErrorSummaryDTO:
    code: PublicJobReasonCode
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", PublicJobReasonCode(self.code))
        _text(self.message, "job error message", 512)
        _boolean(self.retryable, "retryable")


@dataclass(frozen=True)
class JobEventSnapshotDTO:
    public_id: str
    revision: int
    event_kind: JobEventKind
    state: JobState
    attempt_count: int
    reason_code: PublicJobReasonCode | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.public_id, "event public_id")
        _integer(self.revision, "revision")
        _integer(self.attempt_count, "attempt_count")
        if self.revision < 0 or self.attempt_count < 0:
            raise ValueError("job event counters are invalid")
        object.__setattr__(self, "event_kind", JobEventKind(self.event_kind))
        if self.reason_code is not None:
            object.__setattr__(self, "reason_code", PublicJobReasonCode(self.reason_code))
        _utc(self.occurred_at, "occurred_at")


@dataclass(frozen=True)
class JobLogReferenceDTO:
    public_id: str
    job_public_id: str
    attempt_count: int
    label: str
    sequence: int
    byte_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.public_id, "log public_id")
        _uuid(self.job_public_id, "job_public_id")
        _integer(self.attempt_count, "attempt_count")
        _integer(self.sequence, "sequence")
        _integer(self.byte_count, "byte_count")
        if self.attempt_count < 0 or self.label not in {"stdout", "stderr", "supervisor"}:
            raise ValueError("log stream identity is invalid")
        if self.sequence < 0 or self.byte_count < 0:
            raise ValueError("log counters are invalid")
        _utc(self.created_at, "created_at")


@dataclass(frozen=True)
class JobLogQueryDTO:
    job_public_id: str
    log_public_id: str
    offset: int = 0
    limit: int = 65_536

    def __post_init__(self) -> None:
        _uuid(self.job_public_id, "job_public_id")
        _uuid(self.log_public_id, "log_public_id")
        _integer(self.offset, "offset")
        _integer(self.limit, "limit", minimum=1)
        if self.offset < 0 or self.limit < 4 or self.limit > 65_536:
            raise ValueError("log read bounds are invalid")


@dataclass(frozen=True)
class JobLogChunkDTO:
    state: Literal["available", "rotated", "unavailable"]
    content: str
    next_offset: int | None

    def __post_init__(self) -> None:
        if self.state not in {"available", "rotated", "unavailable"}:
            raise ValueError("log chunk state is invalid")
        if len(self.content.encode("utf-8")) > 65_536:
            raise ValueError("log chunk exceeds the public byte bound")
        if self.next_offset is not None:
            _integer(self.next_offset, "next_offset")


@dataclass(frozen=True)
class JobSummaryDTO:
    public_id: str
    replay_public_id: str | None
    stage: str
    component_version: str
    state: JobState
    revision: int
    attempt_count: int
    max_attempts: int
    retryable: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested: bool
    progress: JobProgressDTO | None
    error: JobErrorSummaryDTO | None
    result_public_id: str | None

    def __post_init__(self) -> None:
        _uuid(self.public_id, "job public_id")
        if self.replay_public_id is not None:
            _uuid(self.replay_public_id, "replay_public_id")
        if self.result_public_id is not None:
            _uuid(self.result_public_id, "result_public_id")
        _text(self.stage, "stage", 64)
        _text(self.component_version, "component_version", 255)
        object.__setattr__(self, "state", JobState(self.state))
        _integer(self.revision, "revision")
        _integer(self.attempt_count, "attempt_count")
        _integer(self.max_attempts, "max_attempts", minimum=1)
        _boolean(self.retryable, "retryable")
        _boolean(self.cancel_requested, "cancel_requested")
        if self.revision < 0 or self.attempt_count < 0 or self.max_attempts < self.attempt_count:
            raise ValueError("job summary counters are invalid")
        _utc(self.created_at, "created_at")
        for name, value in (("started_at", self.started_at), ("completed_at", self.completed_at)):
            if value is not None:
                _utc(value, name)


@dataclass(frozen=True)
class JobDetailDTO:
    summary: JobSummaryDTO
    dependency_public_ids: tuple[str, ...]
    events: tuple[JobEventSnapshotDTO, ...]
    logs: tuple[JobLogReferenceDTO, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.dependency_public_ids, tuple) or not isinstance(self.events, tuple) or not isinstance(self.logs, tuple):
            raise TypeError("job detail collections must be immutable tuples")
        for value in self.dependency_public_ids:
            _uuid(value, "dependency public_id")


@dataclass(frozen=True)
class JobQueryDTO:
    states: tuple[JobState, ...] = ()
    replay_public_id: str | None = None
    stage: str | None = None
    limit: int = 50
    after_public_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.states, tuple):
            raise TypeError("job query states must be an immutable tuple")
        if any(not isinstance(value, JobState) for value in self.states):
            raise ValueError("job query state is invalid")
        if self.replay_public_id is not None:
            _uuid(self.replay_public_id, "replay_public_id")
        if self.after_public_id is not None:
            _uuid(self.after_public_id, "after_public_id")
        if self.stage is not None:
            _text(self.stage, "stage", 64)
        _integer(self.limit, "limit", minimum=1)
        if self.limit < 1 or self.limit > 200:
            raise ValueError("job page limit is invalid")


@dataclass(frozen=True)
class JobPageDTO:
    items: tuple[JobSummaryDTO, ...]
    next_public_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise TypeError("job page items must be an immutable tuple")
        if self.next_public_id is not None:
            _uuid(self.next_public_id, "next_public_id")


@dataclass(frozen=True)
class RetryJobCommandDTO:
    job_public_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        _uuid(self.job_public_id, "job_public_id")
        _integer(self.expected_revision, "expected_revision")
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be nonnegative")


@dataclass(frozen=True)
class CancelJobCommandDTO:
    job_public_id: str
    expected_revision: int
    requested_by: str
    reason_code: CancellationReasonCode

    def __post_init__(self) -> None:
        _uuid(self.job_public_id, "job_public_id")
        _integer(self.expected_revision, "expected_revision")
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be nonnegative")
        _text(self.requested_by, "requested_by", 255)
        object.__setattr__(self, "reason_code", CancellationReasonCode(self.reason_code))


@dataclass(frozen=True)
class JobMutationDTO:
    public_id: str
    state: JobState
    revision: int
    attempt_count: int

    def __post_init__(self) -> None:
        _uuid(self.public_id, "job public_id")
        object.__setattr__(self, "state", JobState(self.state))
        _integer(self.revision, "revision")
        _integer(self.attempt_count, "attempt_count")
        if self.revision < 0 or self.attempt_count < 0:
            raise ValueError("job mutation counters are invalid")


@dataclass(frozen=True)
class StageExecutionOutcomeDTO:
    status: Literal["succeeded", "retryable_failure", "failed"]
    result_public_id: str | None
    error_code: str | None
    error_message: str | None
    retryable: bool

    def __post_init__(self) -> None:
        _boolean(self.retryable, "retryable")
        if self.status not in {"succeeded", "retryable_failure", "failed"}:
            raise ValueError("stage outcome status is invalid")
        if self.status == "succeeded":
            if self.result_public_id is None or self.error_code is not None or self.error_message is not None or self.retryable:
                raise ValueError("successful stage outcome fields are inconsistent")
            _uuid(self.result_public_id, "result_public_id")
            return
        if self.result_public_id is not None or self.error_code is None or self.error_message is None:
            raise ValueError("failed stage outcome fields are inconsistent")
        _code(self.error_code, "error_code")
        _text(self.error_message, "error_message", 512)
        if (self.status == "retryable_failure") != self.retryable:
            raise ValueError("stage retryability is inconsistent")


@dataclass(frozen=True)
class OwnedExecutionSettlementDTO:
    execution_public_id: str
    tree_settled: bool

    def __post_init__(self) -> None:
        _uuid(self.execution_public_id, "execution_public_id")
        _boolean(self.tree_settled, "tree_settled")


class WorkerControlPort(Protocol):
    def registered_stages(self) -> tuple[str, ...]: ...

    def claim_next(self, worker_public_id: str, lease_seconds: int) -> WorkerLeaseDTO | None: ...

    def heartbeat(
        self, worker_public_id: str, claim: WorkerLeaseDTO, lease_seconds: int
    ) -> WorkerLeaseDTO: ...

    def cancellation(self, worker_public_id: str, claim: WorkerLeaseDTO) -> WorkerCancellationDTO: ...

    def report_progress(
        self, worker_public_id: str, claim: WorkerLeaseDTO, progress: JobProgressDTO
    ) -> None: ...

    def settle_success(self, worker_public_id: str, claim: WorkerLeaseDTO, result_public_id: str) -> None: ...

    def settle_failure(
        self, worker_public_id: str, claim: WorkerLeaseDTO, outcome: StageExecutionOutcomeDTO
    ) -> None: ...

    def settle_cancelled(
        self, worker_public_id: str, claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None: ...

    def release_after_shutdown(
        self, worker_public_id: str, claim: WorkerLeaseDTO, settlement: OwnedExecutionSettlementDTO
    ) -> None: ...


class StageExecutorPort(Protocol):
    def execute(self, job_public_id: str, execution_public_id: str) -> StageExecutionOutcomeDTO: ...


class JobOperationsPort(Protocol):
    def list_jobs(self, query: JobQueryDTO) -> JobPageDTO: ...

    def get_job(self, job_public_id: str) -> JobDetailDTO: ...

    def retry_job(self, command: RetryJobCommandDTO) -> JobMutationDTO: ...

    def cancel_job(self, command: CancelJobCommandDTO) -> JobMutationDTO: ...

    def read_log(self, query: JobLogQueryDTO) -> JobLogChunkDTO: ...
