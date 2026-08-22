"""Production Analytics-to-Web job adapter contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from generals_replay_analyzer.importing import (
    JobDetailDTO as AnalyticsJobDetailDTO,
)
from generals_replay_analyzer.importing import (
    JobLifecycleError,
    PublicJobReasonCode,
)
from generals_replay_analyzer.importing import (
    JobLogChunkDTO as AnalyticsJobLogChunkDTO,
)
from generals_replay_analyzer.importing import (
    JobLogReferenceDTO as AnalyticsJobLogReferenceDTO,
)
from generals_replay_analyzer.importing import (
    JobMutationDTO as AnalyticsJobMutationDTO,
)
from generals_replay_analyzer.importing import (
    JobPageDTO as AnalyticsJobPageDTO,
)
from generals_replay_analyzer.importing import (
    JobProgressDTO as AnalyticsJobProgressDTO,
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
from generals_replay_analyzer.importing.job_contracts import (
    JobErrorSummaryDTO as AnalyticsJobErrorSummaryDTO,
)
from generals_replay_analyzer.watching import WatchFolderStatusRecord
from generals_replay_analyzer.web.adapters.analytics import AnalyticsJobsAdapter
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    CancelJobCommandDTO,
    JobLogQueryDTO,
    JobQueryDTO,
    RetryJobCommandDTO,
)

JOB = "123e4567-e89b-42d3-a456-426614174040"
REPLAY = "123e4567-e89b-42d3-a456-426614174041"
LOG = "123e4567-e89b-42d3-a456-426614174042"


def analytics_summary(state: AnalyticsJobState = AnalyticsJobState.RUNNING) -> AnalyticsJobSummaryDTO:
    return AnalyticsJobSummaryDTO(
        JOB,
        REPLAY,
        "parse",
        "parse-v1",
        state,
        2,
        1,
        3,
        True,
        datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 22, 12, 1, tzinfo=UTC),
        None,
        False,
        None,
        None,
        None,
    )


class FakeOperations:
    def __init__(self) -> None:
        self.summary = analytics_summary()
        self.queries: list[AnalyticsJobQueryDTO] = []
        self.cancelled: list[object] = []
        self.detail_reads = 0

    def list_jobs(self, query: AnalyticsJobQueryDTO) -> AnalyticsJobPageDTO:
        self.queries.append(query)
        return AnalyticsJobPageDTO((self.summary,), None)

    def get_job(self, _job_public_id: str) -> AnalyticsJobDetailDTO:
        self.detail_reads += 1
        return AnalyticsJobDetailDTO(self.summary, (), (), ())

    def retry_job(self, _command: object) -> AnalyticsJobMutationDTO:
        return AnalyticsJobMutationDTO(JOB, AnalyticsJobState.PENDING, 3, 1)

    def cancel_job(self, command: object) -> AnalyticsJobMutationDTO:
        self.cancelled.append(command)
        self.summary = analytics_summary(AnalyticsJobState.CANCELLED)
        return AnalyticsJobMutationDTO(JOB, AnalyticsJobState.CANCELLED, 3, 1)

    def read_log(self, _query: object) -> AnalyticsJobLogChunkDTO:
        return AnalyticsJobLogChunkDTO("available", "safe log", None)


def test_adapter_reconciles_cursor_names_and_maps_safe_job_values() -> None:
    operations = FakeOperations()
    adapter = AnalyticsJobsAdapter(operations)

    page = adapter.list_jobs(JobQueryDTO(states=("running",), replay_public_id=REPLAY, limit=20))

    assert operations.queries == [
        AnalyticsJobQueryDTO(states=(AnalyticsJobState.RUNNING,), replay_public_id=REPLAY, limit=20)
    ]
    assert page.items[0].job_public_id == JOB
    assert page.poll_after_seconds == 2
    assert not hasattr(page.items[0], "result_public_id")


def test_adapter_reads_path_free_worker_published_watched_folder_status() -> None:
    scanned = datetime(2026, 8, 22, 12, 5, tzinfo=UTC)
    status = WatchFolderStatusRecord(
        "123e4567-e89b-42d3-a456-426614174099",
        "Replay folder 1",
        "degraded",
        scanned,
        "watched_root_scan_failed",
    )
    adapter = AnalyticsJobsAdapter(FakeOperations(), watch_status_reader=lambda: (status,))

    page = adapter.list_jobs(JobQueryDTO())

    assert page.watched_folders[0].root_public_id == status.root_public_id
    assert page.watched_folders[0].label == "Replay folder 1"
    assert page.watched_folders[0].state == "degraded"
    assert page.watched_folders[0].last_scan_at_utc == scanned
    assert page.watched_folders[0].reason_code == "watched_root_scan_failed"


def test_browser_cancel_supplies_closed_internal_actor_and_reason() -> None:
    operations = FakeOperations()
    adapter = AnalyticsJobsAdapter(operations)

    mutation = adapter.cancel_job(CancelJobCommandDTO(job_public_id=JOB, expected_revision=2))

    command = operations.cancelled[0]
    assert command.requested_by == "local_web_operator"
    assert command.reason_code.value == "user_request"
    assert mutation.result_code == "cancelled"
    assert mutation.detail.summary.job_public_id == JOB
    assert operations.detail_reads == 1


def test_log_mapping_preserves_opaque_ids_and_bounded_analytics_read() -> None:
    adapter = AnalyticsJobsAdapter(FakeOperations())
    chunk = adapter.read_job_log(JobLogQueryDTO(job_public_id=JOB, log_public_id=LOG, offset=4, limit=65_536))
    assert chunk.text == "safe log"


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("unknown_job", 404),
        ("revision_conflict", 409),
        ("ineligible_retry", 422),
        ("lifecycle_busy", 503),
        ("log_sink_unavailable", 503),
    ],
)
def test_stable_lifecycle_errors_map_to_public_http_classes(error_code: str, expected_status: int) -> None:
    class BrokenOperations(FakeOperations):
        def get_job(self, _job_public_id: str) -> AnalyticsJobDetailDTO:
            raise JobLifecycleError(error_code, "private detail must not escape")

    with pytest.raises(PublicProblem) as caught:
        AnalyticsJobsAdapter(BrokenOperations()).get_job(JOB)
    assert caught.value.status == expected_status
    assert caught.value.code == error_code
    assert "private detail" not in caught.value.detail


def test_adapter_maps_progress_error_logs_and_revisioned_retry() -> None:
    log = AnalyticsJobLogReferenceDTO(
        LOG,
        JOB,
        1,
        "stdout",
        0,
        12,
        datetime(2026, 8, 22, 12, 3, tzinfo=UTC),
    )

    class DetailedOperations(FakeOperations):
        def get_job(self, _job_public_id: str) -> AnalyticsJobDetailDTO:
            return AnalyticsJobDetailDTO(self.summary, (REPLAY,), (), (log,))

    operations = DetailedOperations()
    operations.summary = replace(
        operations.summary,
        progress=AnalyticsJobProgressDTO(2, 5, "records", datetime(2026, 8, 22, 12, 2, tzinfo=UTC)),
        error=AnalyticsJobErrorSummaryDTO(PublicJobReasonCode.STAGE_FAILED, "safe failure", True),
    )
    adapter = AnalyticsJobsAdapter(operations)

    detail = adapter.get_job(JOB)
    mutation = adapter.retry_job(RetryJobCommandDTO(job_public_id=JOB, expected_revision=2))

    assert detail.summary.progress is not None and detail.summary.progress.completed == 2
    assert detail.summary.error is not None and detail.summary.error.code == "stage_failed"
    assert detail.logs[0].log_public_id == LOG
    assert detail.dependency_job_public_ids == (REPLAY,)
    assert mutation.result_code == "retried"
