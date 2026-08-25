"""Canonical fixed player-profile JSON behavior."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    PlayerIndexPageDTO,
    PlayerIndexQueryDTO,
    PlayerProfileDTO,
    PlayerProfileQueryDTO,
    PlayerProfileResolutionDTO,
    PlayerProfileSelectionDTO,
    PlayerProfileVersionDTO,
    PlayerSummaryDTO,
)
from generals_replay_analyzer.web.routes.players import router
from generals_replay_analyzer.web.viewmodels.players import profile_json_url

PLAYER_ID = "123e4567-e89b-42d3-a456-426614174300"


def test_profile_rejects_engine_verified_count_above_imported_history() -> None:
    profile = _profile()
    with pytest.raises(ValidationError, match="cannot exceed total history"):
        PlayerProfileDTO.model_validate(
            profile.model_copy(update={"engine_verified_history_count": profile.history_total_items + 1}).model_dump()
        )
PROFILE_ID = "123e4567-e89b-42d3-a456-426614174301"
DIGEST = "a" * 64


def _profile() -> PlayerProfileDTO:
    query = PlayerProfileQueryDTO(
        player_public_id=PLAYER_ID,
        expected_identity_revision=3,
        longitudinal_run_ids=(),
        report_public_ids=(),
        definition_binding_digest=DIGEST,
        profile_input_digest=DIGEST,
    )
    return PlayerProfileDTO(
        version=PlayerProfileVersionDTO(
            schema_version="replay-player-profile-v1",
            display_policy_version="replay-player-profile-display-v1",
            profile_public_id=PROFILE_ID,
            player_public_id=PLAYER_ID,
            identity_revision=3,
            longitudinal=(),
            fixed_reports=(),
            definition_bindings=(),
            input_digest=DIGEST,
        ),
        query=query,
        player=PlayerSummaryDTO(
            player_public_id=PLAYER_ID,
            display_name="Leex279",
            identity_revision=3,
            state="active",
            match_count=0,
            availability=AvailabilityDTO(state="partial", reason_codes=("minimum_sample_not_met",)),
        ),
        embedded_aliases=(),
        provider_identities=(),
        strata_provenance=(),
        replay_history=(),
        history_page=1,
        history_page_size=25,
        history_total_items=0,
        insights=(),
        availability=AvailabilityDTO(state="partial", reason_codes=("minimum_sample_not_met",)),
    )


class _ProfilePort:
    def __init__(self, profile: PlayerProfileDTO) -> None:
        self.profile = profile
        self.queries: list[PlayerProfileQueryDTO] = []

    def list_players(self, query: PlayerIndexQueryDTO) -> PlayerIndexPageDTO:
        raise AssertionError(query)

    def resolve_profile(self, selection: PlayerProfileSelectionDTO) -> PlayerProfileResolutionDTO:
        raise AssertionError(selection)

    def get_profile(self, query: PlayerProfileQueryDTO) -> PlayerProfileDTO:
        self.queries.append(query)
        return self.profile


def test_fixed_profile_json_is_byte_stable_etagged_and_never_reresolves() -> None:
    """Catch an immutable profile endpoint doing a latest lookup or emitting clock-dependent bytes."""
    profile = _profile()
    port = _ProfilePort(profile)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port
    url = profile_json_url(profile)

    with TestClient(app) as client:
        first = client.get(url, headers={"accept": "application/json"})
        second = client.get(url, headers={"accept": "application/json"})
        cached = client.get(url, headers={"if-none-match": first.headers["etag"]})

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.headers["etag"] == second.headers["etag"]
    assert cached.status_code == 304 and cached.content == b""
    assert len(port.queries) == 3
    assert all(query == profile.query for query in port.queries)
    assert b"minimum_sample_not_met" in first.content
    assert b"generated_at" not in first.content


def test_profile_etag_changes_with_semantic_identity_content() -> None:
    """Catch ETags ignoring an immutable player identity change."""
    original = _profile()
    changed = PlayerProfileDTO.model_validate(
        {
            **original.model_dump(),
            "player": {**original.player.model_dump(), "display_name": "Leex279 tournament"},
        }
    )

    def etag(profile: PlayerProfileDTO) -> str:
        port = _ProfilePort(profile)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[application_port] = lambda: port
        with TestClient(app) as client:
            return client.get(profile_json_url(profile)).headers["etag"]

    assert etag(original) != etag(changed)


def test_profile_contract_rejects_cross_player_and_cross_report_bindings() -> None:
    """Catch a fixed profile accepting longitudinal/report identities outside its immutable query."""
    profile = _profile()
    with pytest.raises(ValidationError, match="report bindings"):
        PlayerProfileDTO.model_validate(
            {
                **profile.model_dump(),
                "query": {**profile.query.model_dump(), "report_public_ids": (PROFILE_ID,)},
            }
        )

    with pytest.raises(ValidationError, match="profile identity"):
        PlayerProfileDTO.model_validate(
            {
                **profile.model_dump(),
                "query": {
                    **profile.query.model_dump(),
                    "player_public_id": "123e4567-e89b-42d3-a456-426614174399",
                },
            }
        )
