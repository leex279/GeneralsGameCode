"""Actionable opponent scouting workspace."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Protocol, cast, runtime_checkable
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from generals_replay_analyzer.report.model import FrozenReportMapping
from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    DerivedLongitudinalEvidenceDTO,
    EvidenceDetailDTO,
    EvidenceQueryDTO,
    PlayerHistoryPort,
    PlayerIndexQueryDTO,
    PlayerProfileSelectionDTO,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import accepts_html, feature_shell, template_response
from generals_replay_analyzer.web.viewmodels.scouting import scouting_workspace_view

router = APIRouter(tags=["scouting"])


@runtime_checkable
class _EvidenceReader(Protocol):
    def get_evidence(self, query: EvidenceQueryDTO) -> EvidenceDetailDTO: ...


def _history_port(port: WebApplicationPort) -> PlayerHistoryPort:
    if not isinstance(port, PlayerHistoryPort):
        raise PublicProblem(status=503, code="player_history_adapter_pending", detail="Opponent scouting is unavailable")
    return cast(PlayerHistoryPort, port)


# TheSuperHackers @feature Leex 24/08/2026 Add a first-class opponent scouting workspace backed by fixed player evidence. (#TBD)
@router.get("/scouting", summary="Opponent scouting workspace")
def scouting_workspace(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406,
            title="Not Acceptable",
            code="not_acceptable",
            detail="This route provides only text/html",
        )
    if set(request.query_params).difference({"player"}) or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return problem_response(
            422,
            title="Invalid scouting query",
            code="invalid_scouting_query",
            detail="Scouting query is invalid",
        )
    player_public_id = request.query_params.get("player")
    if player_public_id is not None:
        try:
            if str(UUID(player_public_id)) != player_public_id:
                raise ValueError("noncanonical player ID")
        except (ValueError, AttributeError):
            return problem_response(
                422,
                title="Invalid scouting query",
                code="invalid_scouting_query",
                detail="Scouting query is invalid",
            )
    history = _history_port(port)
    page = history.list_players(
        PlayerIndexQueryDTO(page_size=100, active_only=True, sort="match_count")
    )
    selected_id = player_public_id or (page.items[0].player_public_id if page.items else None)
    profile = None
    unavailable_reason = None
    opening_statistics: dict[str, Mapping[str, object]] = {}
    opening_evidence_report_ids: dict[str, str] = {}
    if selected_id is not None:
        resolution = history.resolve_profile(PlayerProfileSelectionDTO(player_public_id=selected_id, page_size=100))
        if resolution.state == "resolved":
            assert resolution.fixed_query is not None
            if resolution.fixed_query.player_public_id != selected_id:
                raise PublicProblem(
                    status=409,
                    code="player_profile_identity_mismatch",
                    detail="Player profile identity is inconsistent",
                )
            profile = history.get_profile(resolution.fixed_query)
            if profile.query != resolution.fixed_query or profile.player.player_public_id != selected_id:
                raise PublicProblem(
                    status=409,
                    code="player_profile_identity_mismatch",
                    detail="Player profile identity is inconsistent",
                )
            if profile.version.fixed_reports and isinstance(port, _EvidenceReader):
                for insight in profile.insights:
                    if insight.insight_kind != "recurring_opening":
                        continue
                    # TheSuperHackers @bugfix Leex 25/08/2026 Skip unavailable or empty recurring-opening evidence lookups to prevent scouting request timeouts. (#TBD)
                    if (
                        insight.availability.state not in {"available", "partial"}
                        or insight.raw_value is None
                        or insight.sample_count <= 0
                    ):
                        continue
                    for evidence in insight.evidence:
                        for report in profile.version.fixed_reports:
                            try:
                                detail = port.get_evidence(
                                    EvidenceQueryDTO(
                                        report_public_id=report.report_public_id,
                                        evidence_public_id=evidence.evidence_public_id,
                                        expected_tier=evidence.tier,
                                    )
                                )
                            except PublicProblem:
                                continue
                            source = detail.source
                            statistics = (
                                source.statistics if isinstance(source, DerivedLongitudinalEvidenceDTO) else None
                            )
                            if isinstance(statistics, FrozenReportMapping):
                                statistics = dict(statistics)
                            if (
                                isinstance(source, DerivedLongitudinalEvidenceDTO)
                                and source.result_public_id == insight.result_public_id
                                and isinstance(statistics, Mapping)
                            ):
                                opening_statistics[insight.result_public_id] = statistics
                                opening_evidence_report_ids[insight.result_public_id] = report.report_public_id
                                break
                        if insight.result_public_id in opening_statistics:
                            break
        else:
            unavailable_reason = resolution.reason_codes[0] if resolution.reason_codes else "player_profile_unavailable"
    view = scouting_workspace_view(
        page.items,
        profile,
        selected_player_public_id=selected_id,
        unavailable_reason=unavailable_reason,
        opening_statistics=opening_statistics,
        opening_evidence_report_ids=opening_evidence_report_ids,
    )
    shell = feature_shell(
        page_title="Opponent scouting | Generals Replay Analyzer",
        current_path="/scouting",
        availability=page.availability,
    )
    return template_response(
        request,
        "scouting/index.html",
        shell,
        context={"view": view},
    )
