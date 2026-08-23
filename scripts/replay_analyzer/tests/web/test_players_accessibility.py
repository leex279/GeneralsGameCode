"""Text-first and keyboard-accessible player/comparison surfaces."""

from __future__ import annotations

from html.parser import HTMLParser

from fastapi import FastAPI
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    PlayerIndexPageDTO,
    PlayerIndexQueryDTO,
    PlayerProfileDTO,
    PlayerProfileQueryDTO,
    PlayerProfileResolutionDTO,
    PlayerProfileSelectionDTO,
    PlayerSummaryDTO,
)
from generals_replay_analyzer.web.routes.comparisons import router as comparison_router
from generals_replay_analyzer.web.routes.players import router as player_router

PLAYER_ID = "123e4567-e89b-42d3-a456-426614174330"


class _OutlineParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1_count = 0
        self.labels = 0
        self.captions = 0
        self.skip_links = 0
        self.inline_scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "h1":
            self.h1_count += 1
        elif tag == "label":
            self.labels += 1
        elif tag == "caption":
            self.captions += 1
        elif tag == "a" and values.get("href") == "#main-content":
            self.skip_links += 1
        elif tag == "script" and "src" not in values:
            self.inline_scripts += 1


class _PlayerPort:
    def list_players(self, query: PlayerIndexQueryDTO) -> PlayerIndexPageDTO:
        return PlayerIndexPageDTO(
            query=query,
            items=(
                PlayerSummaryDTO(
                    player_public_id=PLAYER_ID,
                    display_name="Leex279",
                    identity_revision=2,
                    state="active",
                    match_count=10,
                    availability=AvailabilityDTO(state="available"),
                ),
            ),
            page=query.page,
            page_size=query.page_size,
            total_items=1,
            availability=AvailabilityDTO(state="available"),
        )

    def resolve_profile(self, selection: PlayerProfileSelectionDTO) -> PlayerProfileResolutionDTO:
        raise AssertionError(selection)

    def get_profile(self, query: PlayerProfileQueryDTO) -> PlayerProfileDTO:
        raise AssertionError(query)


def _parse(text: str) -> _OutlineParser:
    parser = _OutlineParser()
    parser.feed(text)
    return parser


def test_players_index_has_one_heading_labelled_filters_caption_and_skip_link() -> None:
    """Catch the player directory becoming a visual-only card wall or losing native controls."""
    app = FastAPI()
    app.include_router(player_router)
    app.dependency_overrides[application_port] = _PlayerPort

    with TestClient(app) as client:
        response = client.get("/players", headers={"accept": "text/html"})

    parsed = _parse(response.text)
    assert response.status_code == 200
    assert parsed.h1_count == 1
    assert parsed.labels >= 6
    assert parsed.captions >= 1
    assert parsed.skip_links == 1
    assert parsed.inline_scripts == 0
    assert "active" in response.text and "10" in response.text


def test_compare_selector_keeps_keyboard_controls_and_textual_state_alternatives() -> None:
    """Catch the comparison screen making mode/state facts pointer-, canvas-, or color-only."""
    app = FastAPI()
    app.include_router(comparison_router)
    app.dependency_overrides[application_port] = object

    with TestClient(app) as client:
        response = client.get("/compare", headers={"accept": "text/html"})

    parsed = _parse(response.text)
    assert response.status_code == 200
    assert parsed.h1_count == 1
    assert parsed.labels >= 10
    assert parsed.skip_links == 1
    assert parsed.inline_scripts == 0
    for state in ("Comparable", "Partial", "Not Comparable", "Unavailable"):
        assert state in response.text
    assert "[start inclusive, end exclusive)" in response.text
