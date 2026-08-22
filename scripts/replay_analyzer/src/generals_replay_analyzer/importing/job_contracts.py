"""ORM-free durable job contracts shared by workers and application adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
        if not self.stage or self.attempt_count < 1 or self.max_attempts < self.attempt_count or not self.lease_token:
            raise ValueError("worker lease fields are invalid")


@dataclass(frozen=True)
class WorkerCancellationDTO:
    requested: bool
    reason_code: str | None


@dataclass(frozen=True)
class JobProgressDTO:
    completed: int
    total: int
    unit: str
    updated_at: datetime

    def __post_init__(self) -> None:
        _utc(self.updated_at, "updated_at")
        if self.completed < 0 or self.total <= 0 or self.completed > self.total or not self.unit.strip():
            raise ValueError("job progress is invalid")
        if len(self.unit) > 64:
            raise ValueError("job progress unit is too long")


@dataclass(frozen=True)
class JobErrorSummaryDTO:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class JobEventSnapshotDTO:
    public_id: str
    revision: int
    event_kind: str
    state: JobState
    attempt_count: int
    reason_code: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class JobLogReferenceDTO:
    public_id: str
    job_public_id: str
    attempt_count: int
    label: str
    sequence: int
    byte_count: int
    created_at: datetime


@dataclass(frozen=True)
class JobLogQueryDTO:
    job_public_id: str
    log_public_id: str
    offset: int = 0
    limit: int = 65_536

    def __post_init__(self) -> None:
        _uuid(self.job_public_id, "job_public_id")
        _uuid(self.log_public_id, "log_public_id")
        if self.offset < 0 or self.limit < 1 or self.limit > 65_536:
            raise ValueError("log read bounds are invalid")


@dataclass(frozen=True)
class JobLogChunkDTO:
    state: Literal["available", "rotated", "unavailable"]
    content: str
    next_offset: int | None


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


@dataclass(frozen=True)
class JobDetailDTO:
    summary: JobSummaryDTO
    dependency_public_ids: tuple[str, ...]
    events: tuple[JobEventSnapshotDTO, ...]
    logs: tuple[JobLogReferenceDTO, ...]


@dataclass(frozen=True)
class JobQueryDTO:
    states: tuple[JobState, ...] = ()
    replay_public_id: str | None = None
    stage: str | None = None
    limit: int = 50
    after_public_id: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 200:
            raise ValueError("job page limit is invalid")


@dataclass(frozen=True)
class JobPageDTO:
    items: tuple[JobSummaryDTO, ...]
    next_public_id: str | None


@dataclass(frozen=True)
class RetryJobCommandDTO:
    job_public_id: str
    expected_revision: int


@dataclass(frozen=True)
class CancelJobCommandDTO:
    job_public_id: str
    expected_revision: int
    requested_by: str
    reason_code: str


@dataclass(frozen=True)
class JobMutationDTO:
    public_id: str
    state: JobState
    revision: int
    attempt_count: int


@dataclass(frozen=True)
class StageExecutionOutcomeDTO:
    status: Literal["succeeded", "retryable_failure", "failed"]
    result_public_id: str | None
    error_code: str | None
    error_message: str | None
    retryable: bool


@dataclass(frozen=True)
class OwnedExecutionSettlementDTO:
    execution_public_id: str
    tree_settled: bool


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
