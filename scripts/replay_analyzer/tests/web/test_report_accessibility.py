"""Semantic, keyboard, and no-JavaScript contracts for report evidence pages."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from generals_replay_analyzer.web.ports import EvidenceDetailDTO, EvidenceQueryDTO

from .test_evidence import EVIDENCE_ID, REPLAY_ID, REPORT_ID, VARIANTS, _EvidencePort
from .test_report import _client, _report, _ReportPort
from .test_report_charts import _ChartPort


class _SemanticParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _render_report(*, timeline_available: bool) -> str:
    port = _ChartPort(_report()) if timeline_available else _ReportPort(_report())
    with _client(port) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )
    assert response.status_code == 200
    return response.text


def test_fixed_report_has_one_heading_landmarks_and_external_scripts_only() -> None:
    """Catch report enhancements weakening the shell's keyboard and CSP-safe structure."""
    html = _render_report(timeline_available=True)
    parser = _SemanticParser()
    parser.feed(html)

    assert sum(tag == "h1" for tag, _attrs in parser.tags) == 1
    assert any(tag == "main" and attrs.get("id") == "main-content" for tag, attrs in parser.tags)
    assert any(tag == "nav" and attrs.get("aria-label") == "Jump to report section" for tag, attrs in parser.tags)
    scripts = [attrs for tag, attrs in parser.tags if tag == "script"]
    assert scripts and all(attrs.get("src", "").startswith("/static/") for attrs in scripts)
    assert not any(name.casefold().startswith("on") for _tag, attrs in parser.tags for name in attrs)


def test_available_timeline_has_keyboard_filters_axis_toggle_and_native_frame_table() -> None:
    """Catch an ECharts-only timeline that becomes unusable without pointer input or JavaScript."""
    html = _render_report(timeline_available=True)
    parser = _SemanticParser()
    parser.feed(html)

    labels = {attrs.get("for") for tag, attrs in parser.tags if tag == "label"}
    assert any(tag == "form" and attrs.get("aria-label") == "Timeline controls" for tag, attrs in parser.tags)
    assert {"timeline-axis-frame", "timeline-axis-seconds"} <= labels
    assert any(value and value.startswith("timeline-player-") for value in labels)
    assert any(value and value.startswith("timeline-family-") for value in labels)
    assert any(tag == "caption" for tag, _attrs in parser.tags)
    assert any(tag == "div" and "data-report-timeline" in attrs for tag, attrs in parser.tags)
    assert any(tag == "details" and "timeline-event-log" in attrs.get("class", "") for tag, attrs in parser.tags)
    assert "Full event log" in html
    assert "Timeline data in authoritative replay frames" in html
    assert "<td>7</td>" in html
    assert "<noscript>" in html


def test_unavailable_timeline_has_reason_and_no_empty_chart_placeholder() -> None:
    """Catch telemetry absence leaving an unexplained or blank visualization frame."""
    html = _render_report(timeline_available=False)
    parser = _SemanticParser()
    parser.feed(html)

    assert "Timeline unavailable" in html
    assert "timeline_fixture_unavailable" in html
    assert not any(tag == "div" and "data-report-timeline" in attrs for tag, attrs in parser.tags)
    assert not any(tag == "form" and attrs.get("aria-label") == "Timeline controls" for tag, attrs in parser.tags)


def test_claim_evidence_is_a_native_report_scoped_link_with_optional_drawer_enhancement() -> None:
    """Catch evidence drawers becoming inaccessible or losing immutable report scope without HTMX."""
    html = _render_report(timeline_available=False)
    parser = _SemanticParser()
    parser.feed(html)
    expected = f"/evidence/observed/{EVIDENCE_ID}?report_id={REPORT_ID}"

    links = [attrs for tag, attrs in parser.tags if tag == "a" and attrs.get("href") == expected]
    assert links
    assert all(attrs.get("hx-get") == expected for attrs in links)
    assert all(attrs.get("hx-target") == "closest details" for attrs in links)
    assert any(tag == "summary" for tag, _attrs in parser.tags)


def test_report_claim_values_wrap_inside_the_fixed_report_viewport() -> None:
    """Catch bounded canonical claim text widening the immutable report beyond the viewport."""
    html = _render_report(timeline_available=False)
    css = (Path(__file__).parents[2] / "src/generals_replay_analyzer/web/static/css/app.css").read_text(
        encoding="utf-8"
    )

    assert '<strong class="report-claim-value">10</strong>' in html
    assert ".report-claim-value { overflow-wrap: anywhere; word-break: break-word; }" in css


def test_report_timeline_table_has_a_keyboard_scroll_region() -> None:
    """Catch a wide frame table forcing the whole fixed report beyond a mobile viewport."""
    html = _render_report(timeline_available=True)
    css = (Path(__file__).parents[2] / "src/generals_replay_analyzer/web/static/css/app.css").read_text(
        encoding="utf-8"
    )

    assert '<div class="wide-table-scroll" role="region" aria-label="Replay timeline table" tabindex="0">' in html
    assert ".wide-table-scroll { max-width: 100%; overflow-x: auto;" in css


def test_evidence_deep_link_has_one_heading_typed_source_and_native_back_link() -> None:
    """Catch the no-JavaScript evidence route flattening source authority or trapping navigation."""
    variant = VARIANTS[0]
    query = EvidenceQueryDTO(
        report_public_id=REPORT_ID,
        evidence_public_id=EVIDENCE_ID,
        expected_tier="observed",
    )
    port = _EvidencePort(
        EvidenceDetailDTO(
            schema_version="web-evidence-inspector-v1",
            query=query,
            replay_public_id=REPLAY_ID,
            source_kind=variant.source_kind,
            source_schema_version=variant.schema_version,
            source=variant.source,
        )
    )
    with _client(port) as client:
        response = client.get(
            f"/evidence/observed/{EVIDENCE_ID}?report_id={REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )
    parser = _SemanticParser()
    parser.feed(response.text)

    assert response.status_code == 200
    assert sum(tag == "h1" for tag, _attrs in parser.tags) == 1
    assert any(tag == "dl" for tag, _attrs in parser.tags)
    assert any(tag == "pre" for tag, _attrs in parser.tags)
    assert any(
        tag == "a" and attrs.get("href") == f"/replays/{REPLAY_ID}/reports/{REPORT_ID}" for tag, attrs in parser.tags
    )
    assert variant.source_kind in response.text
