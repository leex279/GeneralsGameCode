"""Command-center visual-system contracts for the first meaningful preview."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.bootstrap import BootstrapSettings
from generals_replay_analyzer.web.dependencies import WebApplicationPortFactory
from generals_replay_analyzer.web.resources import package_resource

from .conftest import CountingPortFactory, FakeWebApplicationPort, RecordingBootstrapper


def _page(path: str) -> str:
    app = create_app(
        cast(BootstrapSettings, object()),
        port_factory=cast(WebApplicationPortFactory, CountingPortFactory()),
        bootstrapper=RecordingBootstrapper(),
    )
    with TestClient(app) as client:
        response = client.get(path, headers={"host": "localhost"})
    assert response.status_code == 200
    return response.text


def test_command_center_uses_the_approved_tactical_visual_system() -> None:
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")

    for token, value in (
        ("--cc-canvas", "#080f15"),
        ("--cc-surface-1", "#0d1a24"),
        ("--cc-surface-2", "#111f2b"),
        ("--cc-surface-3", "#1a2f41"),
        ("--cc-rule", "#31536a"),
        ("--cc-text", "#eaf6ff"),
        ("--cc-muted", "#9bb4c4"),
        ("--cc-cyan", "#7fc6f5"),
        ("--cc-blue", "#4a9fd8"),
        ("--cc-success", "#a9d05a"),
        ("--cc-warning", "#f0966e"),
        ("--cc-opponent", "#d9764f"),
    ):
        assert f"{token}: {value}" in css
    assert "max-width: 1600px" in css
    assert "prefers-color-scheme" not in css
    assert "linear-gradient" in css and "radial-gradient" in css


def test_shell_has_a_persistent_mode_strip_and_keyboard_navigation_contract() -> None:
    html = _page("/")
    script = package_resource("web/static/js/app.js").read_text(encoding="utf-8")

    assert 'class="mode-banner status-strip"' in html
    assert 'data-mode="unavailable"' in html
    assert "No analytics adapter" in html
    assert 'class="utility-row steel-masthead"' in html
    assert 'class="primary-row tab-strip"' in html
    assert ">Navigate<" in html
    assert "Ctrl+K" in script and "metaKey" in script


def test_shell_uses_the_reference_three_band_chrome_and_clipped_active_navigation() -> None:
    html = _page("/")
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")

    assert 'class="mode-banner status-strip"' in html
    assert 'class="utility-row steel-masthead"' in html
    assert 'class="primary-row tab-strip"' in html
    assert 'class="primary-navigation tactical-tabs"' in html
    assert 'aria-current="page"' in html
    assert '.mode-banner.status-strip' in css
    assert 'min-height: 32px' in css
    assert '.utility-row.steel-masthead' in css
    assert '.primary-row.tab-strip' in css
    assert '.tactical-tabs a[aria-current="page"]' in css
    assert 'clip-path: polygon' in css
    assert '"Bahnschrift"' in css


def test_dashboard_is_a_player_first_workspace_with_honest_unavailable_state() -> None:
    html = _page("/")

    assert "<h1>Replay dashboard</h1>" in html
    assert "Strategy / Match intelligence" in html
    assert 'class="command-strip"' in html
    assert 'class="dashboard-layout"' in html
    assert 'class="operations-rail"' not in html
    assert "Opponent scouting" in html
    assert "Compare matchups" in html
    assert "Operations / Overview" not in html
    assert "Inspect analysis jobs" not in html
    assert "Review player identity" not in html
    assert "No replay evidence in this library yet" in html
    assert "analytics_adapter_pending" in html
    assert "kpi-card" not in html


def test_library_has_dense_required_columns_and_responsive_row_labels() -> None:
    html = _page("/replays")

    assert 'class="library-toolbar"' in html
    assert 'class="filter-grid filter-grid-primary"' in html
    assert 'class="table-scroll"' in html
    for heading in ("Replay", "Players", "Result", "Faction", "Map", "Evidence", "Status", "Observed", "More"):
        assert f'>{heading}<' in html
    for label in ("Replay", "Players", "Result", "Faction", "Map", "Evidence", "Status", "Observed"):
        assert f'data-label="{label}"' in html


def test_command_center_keeps_mobile_targets_forced_colors_and_reduced_motion() -> None:
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")

    assert "@media (max-width: 767px)" in css
    assert "@media (max-width: 479px)" in css
    assert "min-height: 44px" in css
    assert "@media (forced-colors: active)" in css
    assert '.clipped-control:focus-visible' in css
    assert '.tactical-tabs a[aria-current="page"]:focus-visible' in css
    forced_colors = css.split("@media (forced-colors: active)", maxsplit=1)[1]
    assert ".clipped-control" in forced_colors
    assert '.tactical-tabs a[aria-current="page"]' in forced_colors
    assert "prefers-reduced-motion: reduce" in css


def test_command_center_declares_dark_browser_chrome_and_resilient_interactions() -> None:
    dashboard = _page("/")
    library = _page("/replays")
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")

    assert '<meta name="theme-color" content="#080f15">' in dashboard
    assert 'autocomplete="off"' in library
    assert 'placeholder="Filename or player…"' in library
    assert "touch-action: manipulation" in css
    assert "overscroll-behavior: contain" in css
    assert "text-wrap: balance" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "overflow-wrap: anywhere" in css
    assert "-webkit-tap-highlight-color" in css


def test_review_hardening_keeps_controls_readable_and_unavailable_non_failure() -> None:
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")

    assert "--cc-control-border: #4a7592" in css
    assert "border: 1px solid var(--cc-control-border)" in css
    assert ".availability-unavailable { border-left: 4px solid var(--cc-amber); }" in css
    assert ".filter-grid label" in css and "font-size: 0.8125rem" in css
    assert ".replay-table th" in css and ".replay-table td" in css
    assert "width: calc(100% - 48px)" in css
    assert "box-shadow" not in css


def test_player_pattern_evidence_has_a_wrapping_scroll_contract() -> None:
    css = (Path(__file__).parents[2] / "src/generals_replay_analyzer/web/static/css/app.css").read_text(encoding="utf-8")
    html = (Path(__file__).parents[2] / "src/generals_replay_analyzer/web/templates/players/detail.html").read_text(encoding="utf-8")

    assert 'wide-table-scroll player-pattern-table' in html
    assert ".player-pattern-table table { min-width: 960px; }" in css
    assert ".player-pattern-table th, .player-pattern-table td { overflow-wrap: anywhere; vertical-align: top; }" in css


def test_strategy_report_has_tactical_panels_and_matching_chart_palette() -> None:
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")
    scripts = tuple(
        package_resource(f"web/static/js/{name}.js").read_text(encoding="utf-8")
        for name in ("report", "compare", "map")
    )

    for selector in (
        ".report-hero",
        ".status-chip",
        ".versus-grid",
        ".player-match-card",
        ".strategy-grid",
        ".strategy-card",
        ".review-prompt-list",
        ".build-order-list",
        ".technical-evidence",
    ):
        assert selector in css
    assert "clip-path: polygon" in css
    for script in scripts:
        assert "#7fc6f5" in script
        assert "#a9d05a" in script
        assert "#f0966e" in script


def test_timeline_uses_its_filter_controls_instead_of_a_duplicate_series_legend() -> None:
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")
    script = package_resource("web/static/js/report.js").read_text(encoding="utf-8")

    assert "legend: { show: false" in script
    assert 'root.style.height = "12rem"' in script
    assert "[data-report-timeline] { min-height: 12rem" in css
    assert "axisLabel: { show: false }" in script
    assert '[data-timeline-controls] input[type="checkbox"]' in css
    assert ".timeline-family-filter" in css


def test_command_dialog_restores_the_actual_invoker_and_mobile_uses_the_menu() -> None:
    html = _page("/")
    script = package_resource("web/static/js/app.js").read_text(encoding="utf-8")
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")

    assert "lastInvoker" in script
    assert "document.activeElement" in script
    assert "lastInvoker.focus()" in script
    assert "<kbd>Ctrl K</kbd>" not in html
    assert 'class="global-pipeline"' in html
    assert "Pipeline idle" in html
    assert "Pipeline unavailable" not in html
    assert "@media (max-width: 767px)" in css
    assert ".primary-row.tab-strip { display: none; }" in css
    assert ".library-meta a" in css and "min-height: 44px" in css


def test_library_promotes_triage_filters_sort_chips_clear_and_scroll_instruction() -> None:
    html = _page("/replays?result=win&faction=USA&evidence_tier=observed&sort=replay_asc")

    primary = html.index('class="filter-grid filter-grid-primary"')
    advanced = html.index('class="filter-dialog"')
    for control in ('name="result"', 'name="faction"', 'name="map_public_id"', 'name="evidence_tier"'):
        assert primary < html.index(control) < advanced
    assert 'name="sort"' in html
    assert "Replay name A–Z" in html
    assert "3 active filters" in html
    assert "Result: win" in html
    assert "Faction: USA" in html
    assert "Evidence: observed" in html
    assert '>Clear filters<' in html
    assert 'class="scroll-instruction"' in html
    assert 'aria-describedby="replay-table-scroll-instruction"' in html
    assert 'aria-sort="none"' in html
    assert 'class="review-unavailable" aria-disabled="true">Review unavailable<' in html


def test_dashboard_can_render_adapter_supplied_operational_sections_without_inventing_rows() -> None:
    from generals_replay_analyzer.web.ports import (
        AvailabilityDTO,
        DashboardDTO,
        DashboardNoticeDTO,
        DashboardReplayDTO,
        DashboardTrendDTO,
    )

    class _AvailableDashboardPort(FakeWebApplicationPort):
        def dashboard(self) -> DashboardDTO:
            availability = AvailabilityDTO(state="available")
            return DashboardDTO(
                generated_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
                availability=availability,
                recent_replays=(
                    DashboardReplayDTO(
                        replay_public_id="123e4567-e89b-42d3-a456-426614174000",
                        report_public_id="123e4567-e89b-42d3-a456-426614174010",
                        label="command-center.rep",
                        players=("leex279", "FOX27"),
                        player_factions=("leex279 (USA)", "FOX27 (GLA)"),
                        result="win",
                        map_name="Tournament Desert",
                        observed_horizon="Observed evidence through 0:03.5 (frame 105)",
                        strategy_labels=("Humvee pressure",),
                        analysis_state="verified",
                        evidence_tier="observed",
                        observed_at=datetime(2026, 8, 22, 11, 0, tzinfo=UTC),
                    ),
                ),
                trends=(
                    DashboardTrendDTO(
                        label="Verification activity",
                        period_label="Last 30 days",
                        sample_count=12,
                        summary="10 verified, 2 partial",
                        availability=availability,
                        filter_analysis_status="partial",
                    ),
                ),
                notable_evidence=(
                    DashboardNoticeDTO(
                        code="partial_telemetry",
                        title="Partial telemetry",
                        summary="One replay needs evidence review",
                        replay_public_id="123e4567-e89b-42d3-a456-426614174000",
                    ),
                ),
            )

    @contextmanager
    def _factory() -> Iterator[_AvailableDashboardPort]:
        yield _AvailableDashboardPort()

    app = create_app(
        cast(BootstrapSettings, object()),
        port_factory=cast(WebApplicationPortFactory, _factory),
        bootstrapper=RecordingBootstrapper(),
    )
    with TestClient(app) as client:
        response = client.get("/", headers={"host": "localhost"})
        action_response = client.get(
            "/replays?search=123e4567-e89b-42d3-a456-426614174000",
            headers={"host": "localhost"},
        )

    assert response.status_code == 200
    assert action_response.status_code == 200
    assert "Recent matches" in response.text and "leex279 (USA)" in response.text
    assert "Tournament Desert" in response.text
    assert "Observed evidence through 0:03.5 (frame 105)" in response.text
    assert "Humvee pressure" in response.text
    assert "command-center.rep" not in response.text
    assert "Activity &amp; quality trends" in response.text and "10 verified, 2 partial" in response.text
    assert "Notable evidence" in response.text and "One replay needs evidence review" in response.text
    assert 'class="dashboard-table recent-match-table"' in response.text
    assert 'class="dashboard-kpi-grid"' in response.text
    assert '>Replays<' in response.text
    assert 'Maps seen' in response.text
    assert 'Match analysis' in response.text and '>Review<' in response.text
    assert (
        'href="/replays/123e4567-e89b-42d3-a456-426614174000/reports/'
        '123e4567-e89b-42d3-a456-426614174010">View analysis</a>'
    ) in response.text
    assert 'href="/replays?analysis_status=partial">Inspect matching replays</a>' in response.text
    assert 'href="/replays?search=123e4567-e89b-42d3-a456-426614174000">Inspect replay evidence</a>' in response.text


def test_dashboard_recent_match_card_leads_with_fixed_report_analysis_facts() -> None:
    template = (Path(__file__).parents[2] / "src/generals_replay_analyzer/web/templates/dashboard.html").read_text(encoding="utf-8")

    assert 'class="dashboard-table recent-match-table"' in template
    assert 'class="recent-match-grid"' not in template
    assert 'class="recent-match-card"' not in template
    assert "replay.player_factions" in template
    assert "replay.observed_horizon" in template
    assert "replay.strategy_labels" in template
    assert 'class="recent-match-operations"' not in template
    assert 'class="dashboard-system-link"' in template


def test_library_uses_a_desktop_filter_rail_and_preserves_mobile_labelled_rows() -> None:
    source_root = Path(__file__).parents[2] / "src/generals_replay_analyzer/web"
    template = (source_root / "templates/replays/index.html").read_text(encoding="utf-8")
    css = (source_root / "static/css/app.css").read_text(encoding="utf-8")

    assert 'class="library-filter-rail"' in template
    assert ".library-workspace, .jobs-workspace {" in css
    assert "grid-template-columns: 16rem minmax(0, 1fr)" in css
    assert "@media (max-width: 767px)" in css
    assert ".replay-table td::before" in css


def test_library_prefers_a_direct_analysis_journey_over_searching_the_same_row() -> None:
    template = package_resource("web/templates/replays/_table.html").read_text(encoding="utf-8")

    assert "replay.report_public_id" in template
    assert 'reports/{{ replay.report_public_id }}' in template
    assert ">View analysis<" in template
    assert 'href="/replays?search={{ replay.replay_public_id }}">Inspect</a>' not in template


def test_compact_filters_and_table_have_keyboard_truthful_interaction_contracts() -> None:
    default_html = _page("/replays")
    replay_sorted = _page("/replays?sort=replay_asc")
    status_sorted = _page("/replays?sort=status_asc")
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")
    script = package_resource("web/static/js/app.js").read_text(encoding="utf-8")

    assert 'type="button" data-all-filters-open' in default_html
    assert '<dialog id="all-filters"' in default_html
    assert 'data-all-filters-close' in default_html
    assert '<details class="advanced-filters-fallback">' in default_html
    assert 'name="player_public_id"' in default_html
    assert 'class="button button-primary" type="submit">Apply all filters</button>' in default_html
    assert 'document.documentElement.classList.add("js-enhanced")' in script
    assert "filterDialog.showModal()" in script
    assert "filterInvoker.focus()" in script
    assert 'class="table-scroll" tabindex="0"' in default_html
    assert '>Observed</th>' in default_html and 'aria-sort="descending">Observed' in default_html
    assert 'aria-sort="ascending">Replay' in replay_sorted
    assert 'aria-sort="ascending">Status' in status_sorted
    assert ".filter-chip, .clear-filters" in css and "min-height: 44px" in css
    assert ".command-list a, .command-list > li > span" in css


def test_unavailable_dashboard_suppresses_stale_adapter_rows() -> None:
    from generals_replay_analyzer.web.ports import AvailabilityDTO, DashboardDTO, DashboardReplayDTO

    class _UnavailableDashboardPort(FakeWebApplicationPort):
        def dashboard(self) -> DashboardDTO:
            return DashboardDTO(
                generated_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
                availability=AvailabilityDTO(state="unavailable", reason_codes=("adapter_unavailable",)),
                recent_replays=(
                    DashboardReplayDTO(
                        replay_public_id="123e4567-e89b-42d3-a456-426614174000",
                        label="stale-must-not-render.rep",
                        players=("player",),
                        analysis_state="verified",
                    ),
                ),
            )

    @contextmanager
    def _factory() -> Iterator[_UnavailableDashboardPort]:
        yield _UnavailableDashboardPort()

    app = create_app(
        cast(BootstrapSettings, object()),
        port_factory=cast(WebApplicationPortFactory, _factory),
        bootstrapper=RecordingBootstrapper(),
    )
    with TestClient(app) as client:
        response = client.get("/", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "stale-must-not-render.rep" not in response.text
    assert response.text.count("No replay evidence in this library yet") == 1
