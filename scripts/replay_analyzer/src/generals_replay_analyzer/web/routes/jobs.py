"""Durable job query and revisioned command routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    CancelJobCommandDTO,
    JobLogQueryDTO,
    JobQueryDTO,
    RetryJobCommandDTO,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import accepts_html, jobs_shell, template_response
from generals_replay_analyzer.web.viewmodels.jobs import job_page_view

router = APIRouter(tags=["jobs"])
_QUERY_FIELDS = frozenset({"state", "stage", "replay_public_id", "limit", "after_job_public_id"})
_STATE_ORDER = ("pending", "running", "succeeded", "failed", "cancelled")


def _public_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("invalid public ID")
    return value


def _job_query(request: Request) -> JobQueryDTO:
    if any(name not in _QUERY_FIELDS for name in request.query_params):
        raise ValueError("unknown job query field")
    scalar_names = _QUERY_FIELDS.difference({"state"})
    if any(len(request.query_params.getlist(name)) > 1 for name in scalar_names):
        raise ValueError("repeated scalar job query field")
    values: dict[str, object] = {"states": tuple(request.query_params.getlist("state"))}
    for name in scalar_names:
        value = request.query_params.get(name)
        if value not in {None, ""}:
            values[name] = value
    query = JobQueryDTO.model_validate(values)
    # TheSuperHackers @fix Leex 22/08/2026 Canonicalize equivalent state filters in one closed public order. (#TBD)
    normalized_states = tuple(state for state in _STATE_ORDER if state in query.states)
    return query.model_copy(update={"states": normalized_states})


@router.get("/jobs", summary="Analysis jobs")
# TheSuperHackers @feature Leex 22/08/2026 Render cursor-bounded durable jobs through immutable application snapshots. (#TBD)
def jobs(request: Request, port: Annotated[WebApplicationPort, Depends(application_port, scope="function")]) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html")
    try:
        query = _job_query(request)
    except (ValueError, ValidationError):
        return problem_response(422, title="Invalid job filters", code="invalid_job_query", detail="Job filters are invalid")
    page = port.list_jobs(query)
    view = job_page_view(page)
    template = "jobs/_rows.html" if request.headers.get("hx-request", "").casefold() == "true" else "jobs/index.html"
    return template_response(request, template, jobs_shell(page.availability), context={"jobs": view})


@router.get("/jobs/{job_public_id}", summary="Analysis job detail")
def job_detail(
    request: Request,
    job_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        public_id = _public_id(job_public_id)
    except ValueError:
        return problem_response(422, title="Invalid job ID", code="invalid_job_id", detail="Job ID is invalid")
    detail = port.get_job(public_id)
    return _detail_response(request, detail)


def _detail_response(request: Request, detail: object, mutation: object | None = None) -> Response:
    issued_token = request.app.state.form_csrf_token_registry.issue()
    context = {"job": detail, "csrf_token": issued_token.hidden_value, "mutation": mutation}
    response = template_response(request, "jobs/detail.html", jobs_shell(detail.availability), context=context)  # type: ignore[attr-defined]
    response.set_cookie("_csrf", issued_token.cookie_value, max_age=600, httponly=True, samesite="strict", path="/")
    return response


async def _expected_revision(request: Request) -> int:
    form = await request.form()
    allowed = {"expected_revision", "_csrf"}
    if any(name not in allowed for name in form) or len(form.getlist("expected_revision")) != 1:
        raise ValueError("invalid revision form")
    tokens = form.getlist("_csrf")
    if len(tokens) > 1:
        raise ValueError("invalid revision form")
    if request.headers.get("x-csrf-token") is None:
        token = tokens[0] if tokens and isinstance(tokens[0], str) else None
        if not request.app.state.form_csrf_token_registry.consume(request.cookies.get("_csrf"), token):
            raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")
    value = form.get("expected_revision")
    if not isinstance(value, str) or not value.isdecimal():
        raise ValueError("invalid revision")
    revision = int(value)
    if revision > 2_147_483_647:
        raise ValueError("invalid revision")
    return revision


@router.post("/jobs/{job_public_id}/retry", summary="Retry analysis job")
async def retry_job(
    request: Request,
    job_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        command = RetryJobCommandDTO(job_public_id=_public_id(job_public_id), expected_revision=await _expected_revision(request))
    except (ValueError, ValidationError):
        return problem_response(422, title="Invalid retry request", code="invalid_job_revision", detail="Job retry request is invalid")
    mutation = port.retry_job(command)
    return _detail_response(request, mutation.detail, mutation)


@router.post("/jobs/{job_public_id}/cancel", summary="Cancel analysis job")
async def cancel_job(
    request: Request,
    job_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        command = CancelJobCommandDTO(job_public_id=_public_id(job_public_id), expected_revision=await _expected_revision(request))
    except (ValueError, ValidationError):
        return problem_response(422, title="Invalid cancel request", code="invalid_job_revision", detail="Job cancel request is invalid")
    mutation = port.cancel_job(command)
    return _detail_response(request, mutation.detail, mutation)


@router.get("/jobs/{job_public_id}/logs/{log_public_id}", summary="Analysis job log")
def job_log(
    request: Request,
    job_public_id: str,
    log_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
    offset: int = 0,
) -> Response:
    try:
        query = JobLogQueryDTO(
            job_public_id=_public_id(job_public_id),
            log_public_id=_public_id(log_public_id),
            offset=offset,
            limit=65_536,
        )
    except (ValueError, ValidationError):
        return problem_response(422, title="Invalid log request", code="invalid_job_log_query", detail="Job log request is invalid")
    chunk = port.read_job_log(query)
    return template_response(request, "jobs/_log.html", jobs_shell(), context={"chunk": chunk})
