"""Translate accepted Analytics job contracts into view-safe Web values."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

from generals_replay_analyzer.importing import (
    CancelJobCommandDTO as AnalyticsCancelJobCommandDTO,
)
from generals_replay_analyzer.importing import (
    CancellationReasonCode,
    JobLifecycleError,
    JobLifecycleErrorCode,
    JobOperationsPort,
)
from generals_replay_analyzer.importing import (
    JobDetailDTO as AnalyticsJobDetailDTO,
)
from generals_replay_analyzer.importing import (
    JobLogChunkDTO as AnalyticsJobLogChunkDTO,
)
from generals_replay_analyzer.importing import (
    JobLogQueryDTO as AnalyticsJobLogQueryDTO,
)
from generals_replay_analyzer.importing import (
    JobPageDTO as AnalyticsJobPageDTO,
)
from generals_replay_analyzer.importing import (
    JobQueryDTO as AnalyticsJobQueryDTO,
)
from generals_replay_analyzer.importing import (
    JobState as AnalyticsJobState,
)
from generals_replay_analyzer.importing import (
    JobSummaryDTO as AnalyticsJobSummaryDTO,
)
from generals_replay_analyzer.importing import (
    RetryJobCommandDTO as AnalyticsRetryJobCommandDTO,
)
from generals_replay_analyzer.watching import WatchFolderStatusRecord
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    CancelJobCommandDTO,
    JobDetailDTO,
    JobErrorSummaryDTO,
    JobLogChunkDTO,
    JobLogQueryDTO,
    JobLogReferenceDTO,
    JobMutationDTO,
    JobPageDTO,
    JobProgressDTO,
    JobQueryDTO,
    JobSummaryDTO,
    RetryJobCommandDTO,
    WatchFolderStatusDTO,
)

_NOT_FOUND = frozenset({JobLifecycleErrorCode.UNKNOWN_JOB, JobLifecycleErrorCode.UNKNOWN_LOG})
_CONFLICT = frozenset({JobLifecycleErrorCode.REVISION_CONFLICT})
_UNAVAILABLE = frozenset(
    {
        JobLifecycleErrorCode.LIFECYCLE_BUSY,
        JobLifecycleErrorCode.LIFECYCLE_CONFLICT,
        JobLifecycleErrorCode.LOG_SINK_UNAVAILABLE,
    }
)


def _problem(error: JobLifecycleError) -> PublicProblem:
    status = 404 if error.code in _NOT_FOUND else 409 if error.code in _CONFLICT else 503 if error.code in _UNAVAILABLE else 422
    return PublicProblem(
        status=status,
        code=error.code.value,
        detail="The requested job operation could not be completed",
    )


def _summary(value: AnalyticsJobSummaryDTO) -> JobSummaryDTO:
    progress = None
    if value.progress is not None:
        progress = JobProgressDTO(
            completed=value.progress.completed,
            total=value.progress.total,
            unit=value.progress.unit,
        )
    error = None
    if value.error is not None:
        error = JobErrorSummaryDTO(
            code=value.error.code.value,
            message=value.error.message,
            retryable=value.error.retryable,
        )
    return JobSummaryDTO(
        job_public_id=value.public_id,
        replay_public_id=value.replay_public_id,
        stage=value.stage,
        component_version=value.component_version,
        state=value.state.value,
        revision=value.revision,
        attempt_count=value.attempt_count,
        max_attempts=value.max_attempts,
        retryable=value.retryable,
        cancel_requested=value.cancel_requested,
        created_at_utc=value.created_at,
        started_at_utc=value.started_at,
        completed_at_utc=value.completed_at,
        progress=progress,
        error=error,
    )


# TheSuperHackers @feature Leex 22/08/2026 Reconcile accepted lifecycle DTOs without exposing worker capabilities. (#TBD)
class AnalyticsJobsAdapter:
    def __init__(
        self,
        operations: JobOperationsPort,
        *,
        watch_status_reader: Callable[[], tuple[WatchFolderStatusRecord, ...]] | None = None,
    ) -> None:
        self._operations = operations
        self._watch_status_reader = watch_status_reader or (lambda: ())

    def list_jobs(self, query: JobQueryDTO) -> JobPageDTO:
        try:
            page = self._operations.list_jobs(
                AnalyticsJobQueryDTO(
                    states=tuple(AnalyticsJobState(state) for state in query.states),
                    replay_public_id=query.replay_public_id,
                    stage=query.stage,
                    limit=query.limit,
                    after_public_id=query.after_job_public_id,
                )
            )
        except JobLifecycleError as error:
            raise _problem(error) from error
        return self._page(query, page)

    def _page(self, query: JobQueryDTO, page: AnalyticsJobPageDTO) -> JobPageDTO:
        items = tuple(_summary(item) for item in page.items)
        active = any(item.state in {"pending", "running"} for item in items)
        watched_folders = tuple(
            WatchFolderStatusDTO(
                root_public_id=status.root_public_id,
                label=status.label,
                state=status.state,
                last_scan_at_utc=status.last_scan_at_utc,
                reason_code=status.reason_code,
            )
            for status in self._watch_status_reader()
        )
        return JobPageDTO(
            query=query,
            items=items,
            next_job_public_id=page.next_public_id,
            watched_folders=watched_folders,
            poll_after_seconds=2 if active else None,
            availability=AvailabilityDTO(state="available"),
        )

    def get_job(self, job_public_id: str) -> JobDetailDTO:
        try:
            detail = self._operations.get_job(job_public_id)
        except JobLifecycleError as error:
            raise _problem(error) from error
        return self._detail(detail)

    @staticmethod
    def _detail(detail: AnalyticsJobDetailDTO) -> JobDetailDTO:
        logs = tuple(
            JobLogReferenceDTO(
                log_public_id=log.public_id,
                label=cast(Literal["stdout", "stderr", "supervisor"], log.label),
                byte_count=log.byte_count,
                created_at_utc=log.created_at,
            )
            for log in detail.logs
        )
        return JobDetailDTO(
            summary=_summary(detail.summary),
            dependency_job_public_ids=detail.dependency_public_ids,
            logs=logs,
            availability=AvailabilityDTO(state="available"),
        )

    def retry_job(self, command: RetryJobCommandDTO) -> JobMutationDTO:
        try:
            mutation = self._operations.retry_job(
                AnalyticsRetryJobCommandDTO(command.job_public_id, command.expected_revision)
            )
            detail = self._operations.get_job(mutation.public_id)
        except JobLifecycleError as error:
            raise _problem(error) from error
        return JobMutationDTO(detail=self._detail(detail), result_code="retried")

    def cancel_job(self, command: CancelJobCommandDTO) -> JobMutationDTO:
        try:
            mutation = self._operations.cancel_job(
                AnalyticsCancelJobCommandDTO(
                    command.job_public_id,
                    command.expected_revision,
                    "local_web_operator",
                    CancellationReasonCode.USER_REQUEST,
                )
            )
            detail = self._operations.get_job(mutation.public_id)
        except JobLifecycleError as error:
            raise _problem(error) from error
        result_code: Literal["cancelled", "cancel_requested"] = (
            "cancelled" if mutation.state is AnalyticsJobState.CANCELLED else "cancel_requested"
        )
        return JobMutationDTO(detail=self._detail(detail), result_code=result_code)

    def read_job_log(self, query: JobLogQueryDTO) -> JobLogChunkDTO:
        try:
            chunk = self._operations.read_log(
                AnalyticsJobLogQueryDTO(
                    query.job_public_id,
                    query.log_public_id,
                    query.offset,
                    min(query.limit, 65_536),
                )
            )
        except JobLifecycleError as error:
            raise _problem(error) from error
        return self._log(chunk)

    @staticmethod
    def _log(chunk: AnalyticsJobLogChunkDTO) -> JobLogChunkDTO:
        return JobLogChunkDTO(state=chunk.state, text=chunk.content, next_offset=chunk.next_offset)
