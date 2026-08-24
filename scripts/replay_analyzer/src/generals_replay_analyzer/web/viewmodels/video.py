"""Safe presentation values for one commented replay cast."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.web.ports import JobDetailDTO


class VideoDetailViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job: JobDetailDTO
    status_label: str
    video_download_url: str | None
    manifest_download_url: str | None


def video_detail_view(job: JobDetailDTO) -> VideoDetailViewModel:
    """Keep private render paths out of the video production page."""
    complete = job.summary.stage == "render_video" and job.summary.state == "succeeded"
    job_id = job.summary.job_public_id
    return VideoDetailViewModel(
        job=job,
        status_label="Verified cast ready" if complete else "Cast production in progress",
        video_download_url=f"/video/{job_id}/download" if complete else None,
        manifest_download_url=f"/video/{job_id}/manifest" if complete else None,
    )
