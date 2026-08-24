"""Immutable replay report pages and typed timeline chart data."""

from __future__ import annotations

import re
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import JSONResponse, RedirectResponse, Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    FixedReportQueryDTO,
    LatestReportQueryDTO,
    ReportQueryPort,
    TimelineChartQueryDTO,
    TimelineFamily,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import accepts_html, replay_library_shell, template_response
from generals_replay_analyzer.web.viewmodels.report import replay_report_view

router = APIRouter(tags=["reports"])
_JSON_MEDIA_RANGE_PRECEDENCE = {"*/*": 0, "application/*": 1, "application/json": 2}
_QVALUE = re.compile(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)\Z")


def _public_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("invalid public ID")
    return value


def _report_port(port: WebApplicationPort) -> ReportQueryPort:
    if not isinstance(port, ReportQueryPort):
        raise PublicProblem(status=503, code="report_adapter_pending", detail="Replay reports are unavailable")
    return cast(ReportQueryPort, port)


def _accepts_json(value: str | None) -> bool:
    if value is None or not value.strip():
        return True
    qualities_by_precedence: dict[int, list[float]] = {}
    for candidate in value.split(","):
        media_type, *parameters = (item.strip() for item in candidate.split(";"))
        precedence = _JSON_MEDIA_RANGE_PRECEDENCE.get(media_type.casefold())
        if precedence is None:
            continue
        quality = 1.0
        invalid_quality = False
        qvalue_seen = False
        for parameter in parameters:
            name, separator, raw_quality = parameter.partition("=")
            if name.strip().casefold() != "q" or not separator:
                invalid_quality = True
                continue
            normalized = raw_quality.strip()
            if qvalue_seen or _QVALUE.fullmatch(normalized) is None:
                invalid_quality = True
                continue
            qvalue_seen = True
            quality = float(normalized)
        qualities_by_precedence.setdefault(precedence, []).append(0.0 if invalid_quality else quality)
    if not qualities_by_precedence:
        return False
    most_specific = max(qualities_by_precedence)
    return min(qualities_by_precedence[most_specific]) > 0.0


@router.get("/replays/{replay_public_id}", summary="Latest replay report")
def latest_report(
    request: Request,
    replay_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
        )
    if (
        any(name != "replay_player_id" for name in request.query_params)
        or len(request.query_params.getlist("replay_player_id")) > 1
    ):
        return problem_response(
            422, title="Invalid report query", code="invalid_report_query", detail="Report query is invalid"
        )
    try:
        player_id = request.query_params.get("replay_player_id") or None
        query = LatestReportQueryDTO(
            replay_public_id=_public_id(replay_public_id),
            replay_player_public_id=None if player_id is None else _public_id(player_id),
        )
    except (ValueError, ValidationError):
        return problem_response(
            422, title="Invalid report query", code="invalid_report_query", detail="Report query is invalid"
        )
    resolution = _report_port(port).resolve_latest(query)
    if resolution.state == "available":
        assert resolution.fixed_report is not None
        assert resolution.version is not None
        if (
            resolution.fixed_report.replay_public_id != query.replay_public_id
            or resolution.version.replay_player_public_id != query.replay_player_public_id
        ):
            raise PublicProblem(
                status=409,
                code="report_identity_mismatch",
                detail="Resolved report identity is outside the requested scope",
            )
        return RedirectResponse(
            f"/replays/{resolution.fixed_report.replay_public_id}/reports/{resolution.fixed_report.report_public_id}",
            status_code=307,
        )
    availability = AvailabilityDTO(state="unavailable", reason_codes=(resolution.reason_code or "report_unavailable",))
    return template_response(
        request,
        "replays/detail.html",
        replay_library_shell(availability),
        context={"resolution": resolution, "report_view": None},
    )


@router.get("/replays/{replay_public_id}/reports/{report_public_id}", summary="Fixed replay report")
# TheSuperHackers @feature Leex 23/08/2026 Bind report HTML and timeline data to one immutable public report identity. (#TBD)
def fixed_report(
    request: Request,
    replay_public_id: str,
    report_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
        )
    try:
        query = FixedReportQueryDTO(
            replay_public_id=_public_id(replay_public_id),
            report_public_id=_public_id(report_public_id),
        )
    except (ValueError, ValidationError):
        return problem_response(422, title="Invalid report ID", code="invalid_report_id", detail="Report ID is invalid")
    report_port = _report_port(port)
    report = report_port.get_report(query)
    if report.fixed_report != query:
        raise PublicProblem(
            status=409,
            code="report_identity_mismatch",
            detail="Report identity is outside the requested scope",
        )
    timeline = report_port.timeline_chart(
        TimelineChartQueryDTO(replay_public_id=query.replay_public_id, report_public_id=query.report_public_id)
    )
    try:
        view = replay_report_view(report, timeline)
    except ValueError as error:
        raise PublicProblem(
            status=409, code="report_identity_mismatch", detail="Report identity is inconsistent"
        ) from error
    video_csrf = request.app.state.form_csrf_token_registry.issue(
        f"/replays/{query.replay_public_id}/reports/{query.report_public_id}/video", request.cookies.get("_csrf")
    )
    response = template_response(
        request,
        "replays/detail.html",
        replay_library_shell(report.availability),
        context={
            "resolution": None,
            "report_view": view,
            "video_csrf": video_csrf,
        },
    )
    response.set_cookie("_csrf", video_csrf.cookie_value, max_age=600, httponly=True, samesite="strict", path="/")
    return response


@router.get(
    "/api/replays/{replay_public_id}/reports/{report_public_id}/charts/timeline",
    summary="Fixed report timeline chart",
)
def timeline_chart(
    replay_public_id: str,
    report_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not _accepts_json(request.headers.get("accept")):
        return problem_response(
            406,
            title="Not Acceptable",
            code="not_acceptable",
            detail="This route provides only application/json",
        )
    try:
        query = TimelineChartQueryDTO(
            replay_public_id=_public_id(replay_public_id),
            report_public_id=_public_id(report_public_id),
            players=tuple(request.query_params.getlist("player")),
            families=cast(tuple[TimelineFamily, ...], tuple(request.query_params.getlist("family"))),
        )
    except (ValueError, ValidationError):
        return problem_response(
            422, title="Invalid timeline query", code="invalid_timeline_query", detail="Timeline query is invalid"
        )
    unknown = set(request.query_params).difference({"player", "family"})
    if unknown:
        return problem_response(
            422, title="Invalid timeline query", code="invalid_timeline_query", detail="Timeline query is invalid"
        )
    chart = _report_port(port).timeline_chart(query)
    if chart.query != query:
        raise PublicProblem(status=409, code="timeline_identity_mismatch", detail="Timeline identity is inconsistent")
    return JSONResponse(chart.model_dump(mode="json"))
