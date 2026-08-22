"""Deterministic view values for durable job pages."""

from __future__ import annotations

from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.web.ports import JobPageDTO, JobQueryDTO


class JobPageViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page: JobPageDTO
    canonical_url: str
    next_url: str | None


def job_query_url(query: JobQueryDTO) -> str:
    values: list[tuple[str, str]] = []
    values.extend(("state", state) for state in query.states)
    if query.stage is not None:
        values.append(("stage", query.stage))
    if query.replay_public_id is not None:
        values.append(("replay_public_id", query.replay_public_id))
    values.append(("limit", str(query.limit)))
    if query.after_job_public_id is not None:
        values.append(("after_job_public_id", query.after_job_public_id))
    return "/jobs?" + urlencode(values)


def job_page_view(page: JobPageDTO) -> JobPageViewModel:
    next_url = None
    if page.next_job_public_id is not None:
        next_url = job_query_url(page.query.model_copy(update={"after_job_public_id": page.next_job_public_id}))
    return JobPageViewModel(page=page, canonical_url=job_query_url(page.query), next_url=next_url)
