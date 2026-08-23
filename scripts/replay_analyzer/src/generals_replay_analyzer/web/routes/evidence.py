"""Report-scoped evidence inspector pages."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    EvidenceQueryDTO,
    ReportQueryPort,
    ReportTier,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import accepts_html, replay_library_shell, template_response
from generals_replay_analyzer.web.viewmodels.report import evidence_detail_view

router = APIRouter(tags=["evidence"])


def _public_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("invalid public ID")
    return value


@router.get("/evidence/{tier}/{evidence_public_id}", summary="Report evidence detail")
# TheSuperHackers @feature Leex 23/08/2026 Require exact report and tier scope for every evidence deep link. (#TBD)
def evidence_detail(
    request: Request,
    tier: str,
    evidence_public_id: str,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
        )
    if (
        tier not in {"observed", "derived", "inferred"}
        or set(request.query_params) != {"report_id"}
        or len(request.query_params.getlist("report_id")) != 1
    ):
        return problem_response(
            422, title="Invalid evidence query", code="invalid_evidence_query", detail="Evidence query is invalid"
        )
    try:
        query = EvidenceQueryDTO(
            report_public_id=_public_id(request.query_params["report_id"]),
            evidence_public_id=_public_id(evidence_public_id),
            expected_tier=cast(ReportTier, tier),
        )
    except (ValueError, ValidationError):
        return problem_response(
            422, title="Invalid evidence query", code="invalid_evidence_query", detail="Evidence query is invalid"
        )
    if not isinstance(port, ReportQueryPort):
        raise PublicProblem(status=503, code="report_adapter_pending", detail="Replay evidence is unavailable")
    detail = port.get_evidence(query)
    if detail.query != query:
        raise PublicProblem(status=409, code="evidence_identity_mismatch", detail="Evidence identity is inconsistent")
    view = evidence_detail_view(detail)
    return template_response(
        request,
        "evidence/detail.html",
        replay_library_shell(AvailabilityDTO(state="available")),
        context={"evidence": view},
    )
