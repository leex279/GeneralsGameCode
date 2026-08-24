"""Same-origin replay-video production commands."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse, Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import VideoCastRequestDTO, WebApplicationPort
from generals_replay_analyzer.web.presentation.shell import feature_shell, template_response
from generals_replay_analyzer.web.viewmodels.video import video_detail_view

router = APIRouter(tags=["video"])


def _public_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("invalid public ID")
    return value


@router.post("/replays/{replay_public_id}/reports/{report_public_id}/video", summary="Queue commented replay cast")
# TheSuperHackers @feature Leex 24/08/2026 Queue same-origin replay casts without granting Web execution access. (#TBD)
async def queue_video_cast(
    request: Request,
    replay_public_id: str,
    report_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    form = await request.form()
    if any(name not in {"_csrf", "diagnostic_preview"} for name in form) or len(form.getlist("_csrf")) != 1:
        return problem_response(422, title="Invalid video request", code="invalid_video_request", detail="Video request is invalid")
    token = form.get("_csrf")
    if not isinstance(token, str) or not request.app.state.form_csrf_token_registry.consume(
        request.cookies.get("_csrf"), token, request.url.path
    ):
        raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")
    preview = form.getlist("diagnostic_preview") == ["true"]
    if form.getlist("diagnostic_preview") not in ([], ["true"]):
        return problem_response(422, title="Invalid video request", code="invalid_video_request", detail="Video request is invalid")
    try:
        command = VideoCastRequestDTO(
            replay_public_id=_public_id(replay_public_id),
            report_public_id=_public_id(report_public_id),
            diagnostic_preview=preview,
        )
    except (ValueError, ValidationError):
        return problem_response(422, title="Invalid video request", code="invalid_video_request", detail="Video request is invalid")
    submission = port.submit_video_cast(command)
    return RedirectResponse(f"/video/{submission.job_public_id}", status_code=303)


@router.get("/video/{job_public_id}", summary="Commented replay cast")
def video_detail(
    request: Request,
    job_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        detail = port.get_job(_public_id(job_public_id))
    except ValueError:
        return problem_response(422, title="Invalid video ID", code="invalid_video_id", detail="Video ID is invalid")
    if detail.summary.stage != "render_video":
        return problem_response(404, title="Video not found", code="video_not_found", detail="Replay cast was not found")
    return template_response(
        request,
        "video/detail.html",
        feature_shell(page_title="Replay cast | Generals Replay Analyzer", current_path="/jobs", availability=detail.availability),
        context={"video": video_detail_view(detail)},
    )
