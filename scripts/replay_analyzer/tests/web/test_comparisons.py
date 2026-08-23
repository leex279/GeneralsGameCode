"""Five-mode comparison presentation contracts."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.ports import ComparisonDTO, ComparisonResolutionDTO, ComparisonSelectionDTO
from generals_replay_analyzer.web.routes.comparisons import router
from generals_replay_analyzer.web.viewmodels.comparisons import comparison_mode_options

from .test_comparison_determinism import _comparison, _fixed_url, _query


def test_all_five_comparison_modes_remain_visible_when_services_are_unavailable() -> None:
    """Catch unavailable production adapters silently removing a promised comparison mode."""
    options = comparison_mode_options()

    assert tuple((item.value, item.label) for item in options) == (
        ("players", "Players"),
        ("matches", "Matches"),
        ("openings", "Openings"),
        ("strategies", "Strategies"),
        ("time_periods", "Time Periods"),
    )


def test_comparison_selector_renders_all_modes_and_baseline_without_an_adapter() -> None:
    """Catch unavailable analytics hiding controls or substituting fixture comparison values."""
    app = FastAPI()
    app.state.form_csrf_token_registry = object()
    app.include_router(router)
    app.dependency_overrides[application_port] = object

    with TestClient(app) as client:
        response = client.get("/compare", headers={"accept": "text/html"})

    assert response.status_code == 200
    for label in ("Players", "Matches", "Openings", "Strategies", "Time Periods"):
        assert label in response.text
    assert "Player vs Segment Baseline" in response.text
    assert "comparison_selection_incomplete" in response.text
    assert "No aligned delta" not in response.text


class _ComparisonPort:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.result = _comparison()
        self.unavailable = unavailable
        self.selections: list[ComparisonSelectionDTO] = []
        self.queries = []

    def resolve(self, selection: ComparisonSelectionDTO) -> ComparisonResolutionDTO:
        self.selections.append(selection)
        if self.unavailable:
            return ComparisonResolutionDTO(
                state="unavailable",
                reason_codes=("minimum_sample_not_met",),
            )
        return ComparisonResolutionDTO(state="resolved", fixed_query=self.result.query)

    def compare(self, query: object) -> ComparisonDTO:
        assert query == self.result.query
        self.queries.append(query)
        return self.result


def _client(port: object) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port
    return TestClient(app)


def test_complete_selection_resolves_once_then_redirects_to_individually_bound_query() -> None:
    """Catch a resolved selector rendering mutable latest values or resolving twice."""
    port = _ComparisonPort()
    with _client(port) as client:
        response = client.get(
            f"/compare?kind=players&left_public_id={_query().left.player_public_id}"
            f"&right_public_id={_query().right.player_public_id}"
            "&metric_definition_id=economy.collection_rate",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == _fixed_url(_query())
    assert len(port.selections) == 1 and port.queries == []


def test_unavailable_complete_selection_keeps_mode_and_reason_visible() -> None:
    """Catch insufficient samples becoming blank output or a fabricated comparison."""
    port = _ComparisonPort(unavailable=True)
    query = _query()
    with _client(port) as client:
        response = client.get(
            f"/compare?kind=players&left_public_id={query.left.player_public_id}"
            f"&right_public_id={query.right.player_public_id}",
            headers={"accept": "text/html"},
        )

    assert response.status_code == 200
    assert "minimum_sample_not_met" in response.text
    assert "Players" in response.text
    assert port.queries == []


def test_fixed_result_and_json_use_compare_only_and_support_etag_304() -> None:
    """Catch a fixed comparison endpoint falling back to current identity or returning unstable bytes."""
    port = _ComparisonPort()
    html_url = _fixed_url(_query())
    api_url = _fixed_url(_query(), api=True)
    with _client(port) as client:
        html = client.get(html_url, headers={"accept": "text/html"})
        data = client.get(api_url, headers={"accept": "application/json"})
        cached = client.get(api_url, headers={"if-none-match": data.headers["etag"]})

    assert html.status_code == 200 and "Comparison Evidence" in html.text
    assert "What this comparison means" in html.text
    assert '<details class="technical-evidence comparison-method">' in html.text
    assert html.text.index("What this comparison means") < html.text.index("Immutable bindings")
    assert "Collection Rate" in html.text and "Sample 8" in html.text
    assert "Charts are optional" in html.text
    assert 'src="/static/vendor/echarts.min.js"' in html.text
    assert 'src="/static/js/compare.js"' in html.text
    assert data.status_code == 200 and data.json()["version"]["schema_version"] == "replay-comparison-v1"
    assert cached.status_code == 304 and cached.content == b""
    assert port.selections == [] and len(port.queries) == 3


def test_comparison_routes_reject_unknown_fields_and_unacceptable_html() -> None:
    """Catch browser-supplied generic state widening the fixed-query or media contract."""
    with _client(_ComparisonPort()) as client:
        invalid = client.get("/compare?kind=players&sql=select", headers={"accept": "text/html"})
        unacceptable = client.get("/compare", headers={"accept": "application/json"})
        fixed_invalid = client.get("/compare/result?fixed_query=e30", headers={"accept": "text/html"})

    assert invalid.status_code == 422
    assert unacceptable.status_code == 406
    assert fixed_invalid.status_code == 422
