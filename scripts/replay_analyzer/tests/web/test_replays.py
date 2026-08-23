"""Replay-library route contracts, written before the feature route exists."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    PipelineStateDTO,
    ReplayLibraryItemDTO,
    ReplayLibraryPageDTO,
    ReplayLibraryQueryDTO,
    ReplayPlayerDisplayDTO,
    ReplayProvenanceDTO,
    TerminalQualityDTO,
)

from .conftest import RecordingBootstrapper

REPLAY_ID = "123e4567-e89b-42d3-a456-426614174000"
PLAYER_ID = "123e4567-e89b-42d3-a456-426614174001"
MAP_ID = "123e4567-e89b-42d3-a456-426614174002"
SOURCE_ID = "123e4567-e89b-42d3-a456-426614174003"
EVIDENCE_ID = "123e4567-e89b-42d3-a456-426614174004"


class _ReplayPort:
    def __init__(self, *, availability: str = "partial") -> None:
        self.queries: list[ReplayLibraryQueryDTO] = []
        self._availability = availability

    def list_replays(self, query: ReplayLibraryQueryDTO) -> ReplayLibraryPageDTO:
        self.queries.append(query)
        availability = AvailabilityDTO(state=self._availability, reason_codes=("fixture_reason",))
        return ReplayLibraryPageDTO(
            query=query,
            page=query.page,
            page_size=query.page_size,
            total_items=3,
            availability=availability,
            items=(
                ReplayLibraryItemDTO(
                    replay_public_id=REPLAY_ID,
                    content_sha256="a" * 64,
                    display_filename="honest-match.rep",
                    players=(
                        ReplayPlayerDisplayDTO(display_name="leex279", slot=1, faction="USA", result="win"),
                        ReplayPlayerDisplayDTO(display_name="FOX27", slot=2, faction="GLA", result="loss"),
                    ),
                    map_public_id=MAP_ID,
                    map_display_name="Tournament Desert",
                    patch="1.04",
                    result="win",
                    lifecycle_state="engine_verified",
                    pipeline=PipelineStateDTO(stage="features", state="running", attempt=1, progress=0.5),
                    terminal_quality=TerminalQualityDTO(
                        lifecycle="engine_verified", engine_run_status="valid", strategy_analysis_scope="observed"
                    ),
                    availability=availability,
                    provenance=ReplayProvenanceDTO(
                        source_public_id=SOURCE_ID,
                        source_kind="strata",
                        strata_match_token="3133811",
                        strata_user_token="source-token",
                        availability=availability,
                        evidence_tier="observed",
                        evidence_public_id=EVIDENCE_ID,
                    ),
                    observed_at_utc=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
                ),
            ),
        )


class _ReplayPortFactory:
    def __init__(self, port: _ReplayPort) -> None:
        self.port = port

    @contextmanager
    def __call__(self) -> Iterator[_ReplayPort]:
        yield self.port


def _client(port: _ReplayPort) -> TestClient:
    return TestClient(
        create_app(object(), port_factory=_ReplayPortFactory(port), bootstrapper=RecordingBootstrapper())
    )

def test_replay_library_route_is_registered_from_a_web_only_module() -> None:
    """Removing the replay route must make the public library unavailable."""
    from generals_replay_analyzer.web.routes.replays import router

    assert router.prefix == ""


def test_library_maps_every_url_filter_to_an_immutable_query_and_keeps_a_canonical_url() -> None:
    port = _ReplayPort()
    query = (
        "?strategy_id=opening-rush&source_kind=strata&date_to_utc=2026-08-31T00%3A00%3A00Z"
        "&page_size=25&player_public_id=" + PLAYER_ID + "&page=2&search=  fox  &faction=GLA"
        "&matchup=USA-v-GLA&map_public_id=" + MAP_ID
        + "&result=loss&date_from_utc=2026-08-01T00%3A00%3A00Z&patch=1.04&evidence_tier=observed"
        "&lifecycle_state=engine_verified"
    )

    with _client(port) as client:
        response = client.get("/replays" + query, headers={"host": "localhost", "accept": "text/html"})

    assert response.status_code == 200
    assert port.queries == [
        ReplayLibraryQueryDTO(
            page=2,
            page_size=25,
            search="fox",
            player_public_id=PLAYER_ID,
            faction="GLA",
            matchup="USA-v-GLA",
            map_public_id=MAP_ID,
            result="loss",
            patch="1.04",
            strategy_id="opening-rush",
            evidence_tier="observed",
            lifecycle_state="engine_verified",
            source_kind="strata",
            date_from_utc=datetime(2026, 8, 1, tzinfo=UTC),
            date_to_utc=datetime(2026, 8, 31, tzinfo=UTC),
        )
    ]
    assert (
        'href="/replays?page=2&amp;page_size=25&amp;search=fox&amp;player_public_id='
        + PLAYER_ID
        + "&amp;faction=GLA&amp;matchup=USA-v-GLA&amp;map_public_id="
        + MAP_ID
        + "&amp;result=loss&amp;patch=1.04&amp;strategy_id=opening-rush&amp;evidence_tier=observed"
        "&amp;lifecycle_state=engine_verified&amp;source_kind=strata&amp;date_from_utc=2026-08-01T00%3A00%3A00Z"
        "&amp;date_to_utc=2026-08-31T00%3A00%3A00Z\""
    ) in response.text


def test_library_maps_a_closed_analysis_status_filter_without_replacing_terminal_quality() -> None:
    port = _ReplayPort()

    with _client(port) as client:
        response = client.get("/replays?analysis_status=engine_verified", headers={"host": "localhost"})

    assert response.status_code == 200
    assert port.queries[-1].analysis_status == "engine_verified"
    assert "Analysis status" in response.text
    assert "Terminal quality: engine_verified" in response.text


@pytest.mark.parametrize("query", ["?page=1&page=2", "?analysis_status=bogus", "?unknown_filter=value"])
def test_library_rejects_repeated_unknown_and_invalid_scalar_filters_before_port_call(query: str) -> None:
    port = _ReplayPort()

    with _client(port) as client:
        response = client.get("/replays" + query, headers={"host": "localhost"})

    assert response.status_code in {400, 422}
    assert port.queries == []


def test_library_renders_html_table_only_for_an_hx_request_and_never_flattens_states() -> None:
    port = _ReplayPort()

    with _client(port) as client:
        response = client.get("/replays", headers={"host": "localhost", "hx-request": "true"})

    assert response.status_code == 200
    assert "<!doctype" not in response.text.casefold()
    assert "Pipeline: features / running" in response.text
    assert "Availability: partial. Reason: fixture_reason" in response.text
    assert "Terminal quality: engine_verified" in response.text
    assert "Evidence tier: observed" in response.text


def test_library_pagination_preserves_all_active_filters_in_deterministic_urls() -> None:
    port = _ReplayPort()

    with _client(port) as client:
        response = client.get(
            "/replays?page=2&page_size=1&faction=GLA&source_kind=strata",
            headers={"host": "localhost"},
        )

    assert response.status_code == 200
    assert 'href="/replays?page=1&amp;page_size=1&amp;faction=GLA&amp;source_kind=strata"' in response.text
    assert 'href="/replays?page=3&amp;page_size=1&amp;faction=GLA&amp;source_kind=strata"' in response.text


def test_library_displays_player_identity_separately_from_labelled_provenance() -> None:
    port = _ReplayPort()

    with _client(port) as client:
        response = client.get("/replays", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "leex279" in response.text and "FOX27" in response.text
    assert "Provenance disclosure" in response.text
    assert "Strata match token: 3133811" in response.text
    assert "Strata source token: source-token" in response.text
    assert 'data-replay-public-id="' + REPLAY_ID + '"' in response.text
    assert "content_sha256" not in response.text


def test_library_unavailability_is_textual_and_returns_a_controlled_non_html_problem() -> None:
    port = _ReplayPort(availability="unavailable")

    with _client(port) as client:
        unavailable = client.get("/replays", headers={"host": "localhost"})
        rejected = client.get("/replays", headers={"host": "localhost", "accept": "application/json"})

    assert "Availability: unavailable. Reason: fixture_reason" in unavailable.text
    assert "No replay library snapshot is available." in unavailable.text
    assert rejected.status_code == 406
    assert rejected.json()["code"] == "not_acceptable"
