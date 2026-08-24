from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from generals_replay_analyzer.web.ports import AvailabilityDTO, JobDetailDTO, JobSummaryDTO
from generals_replay_analyzer.web.viewmodels.video import video_detail_view


def test_video_detail_exposes_downloads_only_after_success() -> None:
    job_id = str(uuid4())
    detail = JobDetailDTO(
        summary=JobSummaryDTO(
            job_public_id=job_id, stage="render_video", component_version="1", state="succeeded", revision=1,
            attempt_count=1, max_attempts=3, retryable=False, cancel_requested=False, created_at_utc=datetime.now(UTC),
        ),
        availability=AvailabilityDTO(state="available"),
    )

    view = video_detail_view(detail)

    assert view.status_label == "Verified cast ready"
    assert view.video_download_url == f"/video/{job_id}/download"
    assert view.manifest_download_url == f"/video/{job_id}/manifest"
