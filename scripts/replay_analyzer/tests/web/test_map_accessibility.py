"""Semantic and keyboard-first contract for fixed map evidence pages."""

from html.parser import HTMLParser
from pathlib import Path

from web.test_map_scene import (
    ENTITY_A,
    EVIDENCE_ID,
    MAP_ID,
    PLAYER_A,
    REPLAY_ID,
    REPORT_ID,
    _scene_client,
    _ScenePort,
)


class _MapHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1_count = 0
        self.ids: set[str] = set()
        self.labels: set[str] = set()
        self.inline_handlers: list[str] = []
        self.inline_styles = 0
        self.scripts_without_src = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "h1":
            self.h1_count += 1
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag == "label" and values.get("for"):
            self.labels.add(values["for"] or "")
        self.inline_handlers.extend(name for name, _value in attrs if name.startswith("on"))
        if "style" in values:
            self.inline_styles += 1
        if tag == "script" and not values.get("src"):
            self.scripts_without_src += 1


def test_fixed_map_page_keeps_essential_evidence_semantic_without_javascript() -> None:
    """Removing a semantic section would make the chart the only evidence surface."""
    response = _scene_client(_ScenePort()).get(f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    parser = _MapHTML()
    parser.feed(response.text)
    assert parser.h1_count == 1
    assert {"frame-start", "frame-end", "coordinate-display", "sample-budget"} <= parser.labels
    assert parser.inline_handlers == []
    assert parser.inline_styles == 0
    assert parser.scripts_without_src == 0
    for phrase in (
        "Authoritative map status",
        "Raw map bounds",
        "Coordinate transforms",
        "Starts",
        "Resources",
        "Structures",
        "Observed sample summary",
        "Orders",
        "Validated route segments and gaps",
        "Engagements",
        "Casualties",
        "Observed sample presence share",
        "event-forced-stratified-v1",
        "Original samples",
        "Mandatory samples",
        "Returned samples",
    ):
        assert phrase in response.text
    assert "The chart is an enhancement" in response.text
    assert "Map evidence at a glance" in response.text
    assert 'class="workspace-panel spatial-briefing"' in response.text
    assert response.text.index("Map evidence at a glance") < response.text.index("Authoritative map status")
    assert f"/api/maps/{MAP_ID}/rasters/" in response.text
    assert "/static/vendor/echarts.min.js" in response.text
    assert "/static/js/map.js" in response.text


def test_player_centric_control_is_not_enabled_without_an_accepted_transform() -> None:
    response = _scene_client(_ScenePort()).get(f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map")

    assert 'value="player_centric" disabled' in response.text
    assert "unresolved_player_transform" in response.text


def test_fixed_map_rejects_non_html_and_invalid_filters_without_falling_forward() -> None:
    port = _ScenePort()
    client = _scene_client(port)

    assert client.get(
        f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map", headers={"accept": "application/json"}
    ).status_code == 406
    assert client.get(
        f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map?frame_start=10&frame_end=1"
    ).status_code == 422
    assert client.get(
        f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map?path=sidecar"
    ).status_code == 422
    assert port.scene_queries == []


def test_fixed_map_preserves_filters_uses_labels_and_resolves_evidence_links() -> None:
    response = _scene_client(_ScenePort()).get(
        f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map"
        f"?frame_start=0&frame_end=10&player={PLAYER_A}&entity={ENTITY_A}"
        "&family=samples&surface=ground&coordinate=raw"
    )

    assert response.status_code == 200
    assert f'name="player" value="{PLAYER_A}" checked' in response.text
    assert f'name="entity" value="{ENTITY_A}" checked' in response.text
    assert 'name="family" value="samples" checked' in response.text
    assert '<option value="ground" selected>' in response.text
    assert "Player 1" in response.text
    assert "Entity 1" in response.text
    assert f'/evidence/observed/{EVIDENCE_ID}?report_id={REPORT_ID}' in response.text
    assert 'id="map-chart-status"' in response.text
    assert 'data-raw-minimum-x="0.0"' in response.text


def test_map_javascript_uses_supplied_positions_and_one_chart_instance() -> None:
    source = Path("src/generals_replay_analyzer/web/static/js/map.js").read_text(encoding="utf-8")

    assert source.count("echarts.init(") == 1
    assert "item.position.map_normalized" in source
    assert "item.position.player_centric" in source
    assert "rawMinimumX" in source
    assert "map-chart-status" in source
    assert 'setAttribute("data-map-state", "unavailable")' not in source
    assert "scene.visibility_transitions" in source
    assert "scene.engine_heuristic_overlays" in source
    assert 'name: "Sampled shroud status"' in source
    assert 'name: "Engine AI threat heuristic"' in source
    assert 'name: "Engine AI cash-value heuristic"' in source


def test_map_chart_has_bounded_responsive_geometry_and_accessible_time_sliders() -> None:
    response = _scene_client(_ScenePort()).get(
        f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map?frame_start=10&frame_end=20"
    )
    stylesheet = Path("src/generals_replay_analyzer/web/static/css/map.css").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert 'href="/static/css/map.css"' in response.text
    assert 'id="frame-start-slider" type="range"' in response.text
    assert 'id="frame-end-slider" type="range"' in response.text
    assert "aspect-ratio:" in stylesheet
    assert "min-height:" in stylesheet
    assert "max-height:" in stylesheet


def test_map_evidence_tables_have_labelled_keyboard_scroll_regions() -> None:
    response = _scene_client(_ScenePort()).get(f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map")
    stylesheet = Path("src/generals_replay_analyzer/web/static/css/app.css").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert '<div class="wide-table-scroll" role="region" aria-label="Map evidence tables" tabindex="0">' in response.text
    assert ".wide-table-scroll { max-width: 100%; overflow-x: auto;" in stylesheet


def test_scouting_and_opted_in_heuristics_have_equivalent_evidence_tables() -> None:
    response = _scene_client(_ScenePort()).get(
        f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map?frame_start=0&frame_end=1800&heuristics=true"
    )

    assert response.status_code == 200
    body = response.text
    assert "First observed clear" in body
    assert "Visibility sampling coverage" in body
    assert "incomplete" in body
    assert "Engine AI threat heuristic" in body
    assert "Engine AI cash-value heuristic" in body
    assert 'name="heuristics" value="true" checked' in body
    assert f"/evidence/observed/{EVIDENCE_ID}?report_id={REPORT_ID}" in body
    heuristic_table = body.split("<caption>Engine heuristic cell samples</caption>", maxsplit=1)[1].split(
        "</table>", maxsplit=1
    )[0]
    assert "map control" not in heuristic_table.casefold()
    assert "territory" not in heuristic_table.casefold()


def test_map_javascript_reports_missing_echarts_instead_of_leaving_loading_status() -> None:
    source = Path("src/generals_replay_analyzer/web/static/js/map.js").read_text(encoding="utf-8")

    assert "if (!window.echarts)" in source
    assert "Interactive chart unavailable" in source
    assert source.index("Interactive chart unavailable") < source.index("echarts.init(")
