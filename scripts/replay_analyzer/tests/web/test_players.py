"""Version-bound player index and profile routes."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DefinitionBindingDTO,
    DistributionIntervalDTO,
    EmbeddedAliasDTO,
    FixedReportReferenceDTO,
    PlayerIndexPageDTO,
    PlayerIndexQueryDTO,
    PlayerInsightDTO,
    PlayerProfileDTO,
    PlayerProfileQueryDTO,
    PlayerProfileResolutionDTO,
    PlayerProfileSelectionDTO,
    ProviderIdentityDTO,
    PublicEvidenceReferenceDTO,
    ReplayHistoryItemDTO,
    StrataProvenanceDTO,
    TerminalQualityDTO,
)
from generals_replay_analyzer.web.routes.players import _fixed_profile_url, router
from generals_replay_analyzer.web.viewmodels.players import player_index_url, profile_json_url

from .test_player_profile_json import DIGEST, PLAYER_ID, _profile

REPORT_ID = "123e4567-e89b-42d3-a456-426614174340"
REPLAY_ID = "123e4567-e89b-42d3-a456-426614174341"
REPLAY_PLAYER_ID = "123e4567-e89b-42d3-a456-426614174342"
EVIDENCE_ID = "123e4567-e89b-42d3-a456-426614174343"
ALIAS_ID = "123e4567-e89b-42d3-a456-426614174344"
PROVIDER_ALIAS_ID = "123e4567-e89b-42d3-a456-426614174345"
ATTACHMENT_ID = "123e4567-e89b-42d3-a456-426614174346"
SOURCE_ID = "123e4567-e89b-42d3-a456-426614174347"
INSIGHT_KINDS = (
    "recurring_opening",
    "timing_distribution",
    "transition_preference",
    "spatial_habit",
    "personal_baseline_deviation",
    "opponent_associated_difference",
    "trend",
    "change_point_candidate",
    "consistency",
)


def test_player_index_query_normalizes_native_filters() -> None:
    """Catch repeated whitespace and invalid pagination leaking into the query service."""
    query = PlayerIndexQueryDTO(
        page=2,
        page_size=25,
        search="  Leex279  ",
        faction=" China ",
        active_only=False,
        sort="match_count",
    )

    assert query.model_dump(mode="json") == {
        "page": 2,
        "page_size": 25,
        "search": "Leex279",
        "faction": "China",
        "opponent_faction": None,
        "map_public_id": None,
        "patch": None,
        "active_only": False,
        "sort": "match_count",
    }


def test_player_index_url_is_stable_and_uses_public_filters_only() -> None:
    """Catch pagination links dropping filters or carrying mutable/private selectors."""
    query = PlayerIndexQueryDTO(page=2, search="Leex279", faction="China", sort="match_count")

    assert player_index_url(query) == "/players?page=2&search=Leex279&faction=China&sort=match_count"


def _definition(index: int) -> DefinitionBindingDTO:
    return DefinitionBindingDTO(
        definition_kind="feature",
        definition_id=f"player.pattern.{index}",
        definition_version="1.0",
        unit="frames",
        scope_type="player",
        window_policy_version="whole-match-v1",
        faction_comparability="same_faction_only",
    )


def _full_profile() -> PlayerProfileDTO:
    base = _profile()
    fixed_report = FixedReportReferenceDTO(
        replay_public_id=REPLAY_ID,
        replay_player_public_id=REPLAY_PLAYER_ID,
        report_public_id=REPORT_ID,
        document_schema_version="replay-report-document-v1",
        report_version="replay-report-v1",
        display_policy_version="replay-report-display-v1",
        input_digest=DIGEST,
    )
    insights = tuple(
        PlayerInsightDTO(
            insight_kind=kind,
            result_public_id=f"123e4567-e89b-42d3-a456-4266141743{50 + index:02x}",
            definition=_definition(index),
            label=kind.replace("_", " ").title(),
            raw_value=0.0 if index == 0 else float(index),
            unit="frames",
            frame_start=0,
            frame_end=300,
            sample_count=10,
            missing_count=1,
            interval=DistributionIntervalDTO(
                lower=0.0,
                upper=10.0,
                confidence_level=0.95,
                method="bootstrap",
                algorithm_version="bootstrap-v1",
            ),
            quality_exclusion_codes=("desynced_excluded",),
            availability=AvailabilityDTO(state="available"),
            evidence=(PublicEvidenceReferenceDTO(evidence_public_id=EVIDENCE_ID, tier="derived"),),
        )
        for index, kind in enumerate(INSIGHT_KINDS)
    )
    query = base.query.model_copy(update={"report_public_ids": (REPORT_ID,)})
    return PlayerProfileDTO.model_validate(
        {
            **base.model_dump(),
            "query": query.model_dump(),
            "version": {
                **base.version.model_dump(),
                "fixed_reports": (fixed_report.model_dump(),),
            },
            "embedded_aliases": (
                EmbeddedAliasDTO(
                    alias_public_id=ALIAS_ID,
                    namespace="embedded_replay_name",
                    original_name="leex279",
                    normalized_name="leex279",
                ).model_dump(),
            ),
            "provider_identities": (
                ProviderIdentityDTO(
                    alias_public_id=PROVIDER_ALIAS_ID,
                    provider_namespace="community",
                    external_subject="Leex profile",
                    attachment_operation_public_id=ATTACHMENT_ID,
                    label="manually_attached_provider_identity",
                ).model_dump(),
            ),
            "strata_provenance": (
                StrataProvenanceDTO(
                    source_public_id=SOURCE_ID,
                    replay_public_id=REPLAY_ID,
                    strata_match_id="3133811",
                    strata_source_user_token="e80b96708aa4254945941fd5f81489bb",
                    label="provenance_not_identity",
                    availability=AvailabilityDTO(state="available"),
                ).model_dump(),
            ),
            "replay_history": (
                ReplayHistoryItemDTO(
                    replay_public_id=REPLAY_ID,
                    replay_player_public_id=REPLAY_PLAYER_ID,
                    observed_name="leex279",
                    faction="China",
                    opponent_factions=("GLA",),
                    opponent_player_public_ids=(),
                    map_display_name="Tournament Desert",
                    patch="1.04",
                    result="won",
                    terminal_quality=TerminalQualityDTO(lifecycle="engine_verified"),
                    fixed_report=fixed_report,
                    availability=AvailabilityDTO(state="available"),
                ).model_dump(),
            ),
            "history_total_items": 1,
            "insights": tuple(item.model_dump() for item in insights),
            "availability": AvailabilityDTO(state="available").model_dump(),
        }
    )


class _PlayerPort:
    def __init__(self, profile: PlayerProfileDTO | None = None, *, unavailable: bool = False) -> None:
        self.profile = profile or _full_profile()
        self.unavailable = unavailable
        self.index_queries: list[PlayerIndexQueryDTO] = []
        self.selections: list[PlayerProfileSelectionDTO] = []
        self.profile_queries: list[PlayerProfileQueryDTO] = []

    def list_players(self, query: PlayerIndexQueryDTO) -> PlayerIndexPageDTO:
        self.index_queries.append(query)
        return PlayerIndexPageDTO(
            query=query,
            items=(self.profile.player,),
            page=query.page,
            page_size=query.page_size,
            total_items=1,
            availability=AvailabilityDTO(state="available"),
        )

    def resolve_profile(self, selection: PlayerProfileSelectionDTO) -> PlayerProfileResolutionDTO:
        self.selections.append(selection)
        if self.unavailable:
            return PlayerProfileResolutionDTO(state="unavailable", reason_codes=("minimum_sample_not_met",))
        return PlayerProfileResolutionDTO(state="resolved", fixed_query=self.profile.query)

    def get_profile(self, query: PlayerProfileQueryDTO) -> PlayerProfileDTO:
        self.profile_queries.append(query)
        return self.profile


async def _public_problem(_request: Request, error: Exception) -> Response:
    assert isinstance(error, PublicProblem)
    return problem_response(error.status, title="Request rejected", code=error.code, detail=error.detail)


def _client(port: object) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(PublicProblem, _public_problem)
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port
    return TestClient(app)


def test_player_index_maps_native_filters_and_preserves_port_order() -> None:
    """Catch web code normalizing identities, sorting results, or dropping filter state itself."""
    port = _PlayerPort()
    with _client(port) as client:
        response = client.get(
            "/players?page=2&page_size=25&search=Leex279&faction=China&active_only=false&sort=match_count",
            headers={"accept": "text/html"},
        )

    assert response.status_code == 200
    assert port.index_queries == [
        PlayerIndexQueryDTO(
            page=2,
            page_size=25,
            search="Leex279",
            faction="China",
            active_only=False,
            sort="match_count",
        )
    ]
    assert "Leex279" in response.text and "active" in response.text


def test_profile_selection_redirects_once_then_fixed_request_renders_all_evidence_families() -> None:
    """Catch current identity/report resolution leaking into a fixed profile read."""
    port = _PlayerPort()
    with _client(port) as client:
        resolution = client.get(f"/players/{PLAYER_ID}", headers={"accept": "text/html"}, follow_redirects=False)
        fixed = client.get(resolution.headers["location"], headers={"accept": "text/html"})

    assert resolution.status_code == 303
    assert resolution.headers["location"] == _fixed_profile_url(port.profile.query)
    assert len(port.selections) == 1 and port.profile_queries == [port.profile.query]
    assert fixed.status_code == 200
    for kind in INSIGHT_KINDS:
        assert kind in fixed.text
    assert "Sample / Missing" in fixed.text and "10 / 1" in fixed.text
    assert "desynced_excluded" in fixed.text and "bootstrap" in fixed.text
    assert "leex279" in fixed.text
    assert "manually_attached_provider_identity" in fixed.text
    assert "provenance_not_identity" in fixed.text
    assert "3133811" in fixed.text and "e80b96708aa4254945941fd5f81489bb" in fixed.text
    assert "/players/3133811" not in fixed.text and "/players/e80b96708aa4254945941fd5f81489bb" not in fixed.text
    assert 'src="/static/vendor/echarts.min.js"' in fixed.text
    assert 'src="/static/js/compare.js"' in fixed.text
    assert "Charts are optional" in fixed.text
    assert f"/api/players/{PLAYER_ID}/profile?" in fixed.text
    json_url = profile_json_url(port.profile)
    assert "report_public_id=" in json_url and "report_public=" not in json_url
    assert REPORT_ID in fixed.text
    assert fixed.text.index("Recent analyzed matches") < fixed.text.index("Identity and data")
    assert 'class="player-insight-grid"' in fixed.text
    assert '<details class="workspace-panel technical-evidence profile-identity">' in fixed.text


def test_one_match_profile_explains_the_sample_requirement_once_without_empty_rows() -> None:
    profile = _full_profile().model_copy(
        update={
            "insights": (),
            "availability": AvailabilityDTO(
                state="partial",
                reason_codes=("minimum_sample_not_met",),
            ),
        }
    )
    port = _PlayerPort(profile)

    with _client(port) as client:
        resolution = client.get(f"/players/{PLAYER_ID}", headers={"accept": "text/html"}, follow_redirects=False)
        fixed = client.get(resolution.headers["location"], headers={"accept": "text/html"})

    assert fixed.status_code == 200
    assert fixed.text.count("More analyzed matches are needed") == 1
    assert "minimum_sample_not_met" not in fixed.text.split("Identity and data", 1)[0]
    assert "Unavailable:</td>" not in fixed.text


def test_unavailable_profile_and_invalid_queries_fail_closed_without_fixed_reads() -> None:
    """Catch partial bindings or unsupported history becoming a current best-effort profile."""
    port = _PlayerPort(unavailable=True)
    with _client(port) as client:
        unavailable = client.get(f"/players/{PLAYER_ID}", headers={"accept": "text/html"})
        partial = client.get(
            f"/players/{PLAYER_ID}?expected_identity_revision=3",
            headers={"accept": "text/html"},
        )
        unknown = client.get("/players?sql=select", headers={"accept": "text/html"})
        unacceptable = client.get("/players", headers={"accept": "application/json"})

    assert unavailable.status_code == 503 and unavailable.json()["code"] == "minimum_sample_not_met"
    assert partial.status_code == 422
    assert unknown.status_code == 422
    assert unacceptable.status_code == 406
    assert port.profile_queries == []


def test_player_routes_report_missing_capability_and_cross_scope_profile() -> None:
    """Catch a missing adapter or cross-player snapshot being presented as valid evidence."""
    with _client(object()) as client:
        missing = client.get("/players", headers={"accept": "text/html"})

    other = _full_profile().model_copy(
        update={"player": _full_profile().player.model_copy(update={"player_public_id": "123e4567-e89b-42d3-a456-426614174399"})}
    )
    port = _PlayerPort(other)
    with _client(port) as client:
        mismatch = client.get(_fixed_profile_url(other.query), headers={"accept": "text/html"})

    assert missing.status_code == 503 and missing.json()["code"] == "player_history_adapter_pending"
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "player_profile_identity_mismatch"
