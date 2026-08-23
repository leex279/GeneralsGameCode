"""Local Axe and explicit semantic accessibility release checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, expect

from .populated_fixture import PopulatedFixtureResult
from .support import (
    assert_browser_clean,
    deterministic_context_options,
    install_browser_error_guard,
    install_same_origin_guard,
    local_axe_script,
)
from .test_offline import VIEWPORTS, _goto_populated_page

PRIMARY_PAGES = (
    ("dashboard", "/", "Replay dashboard"),
    ("library", "/replays", "Replay library"),
    ("jobs", "/jobs", "Analysis jobs"),
    ("maps", "/maps", "Authoritative map scenes"),
    ("players", "/players", "Player Evidence"),
    ("compare", "/compare", "Pattern Comparison"),
    ("settings", "/settings", "Analyzer settings"),
)
WCAG_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa")


@pytest.mark.browser
@pytest.mark.parametrize(("page_name", "path", "heading"), PRIMARY_PAGES)
def test_primary_pages_pass_local_axe_and_landmark_contract(
    installed_server: object,
    browser: Browser,
    page_name: str,
    path: str,
    heading: str,
    browser_artifact_root: Path,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options({"width": 1440, "height": 900}))
    page = context.new_page()
    rejected = install_same_origin_guard(page, origin)
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    assert page.locator("h1").count() == 1
    assert page.get_by_role("banner").count() == 1
    assert page.get_by_role("navigation", name="Primary navigation").count() == 1
    assert page.locator("main").count() == 1
    assert page.locator("footer").count() == 1

    axe = Axe(axe_script=local_axe_script().decode("utf-8"))
    results = axe.run(page, options={"runOnly": {"type": "tag", "values": list(WCAG_TAGS)}})
    assert results.response["testEngine"]["version"]
    (browser_artifact_root / "axe-engine-version.txt").write_text(
        results.response["testEngine"]["version"], encoding="utf-8"
    )
    assert results.violations_count == 0, results.generate_report()
    all_results = axe.run(page, options={"resultTypes": ["violations"]})
    best_practice = tuple(
        violation for violation in all_results.response["violations"] if "best-practice" in violation["tags"]
    )
    assert not tuple(
        violation for violation in best_practice if violation["impact"] in {"serious", "critical"}
    )
    (browser_artifact_root / f"axe--{page_name}.json").write_text(
        json.dumps(
            {
                "axe_engine_version": results.response["testEngine"]["version"],
                "best_practice_findings": [
                    {"id": finding["id"], "impact": finding["impact"], "nodes": len(finding["nodes"])}
                    for finding in best_practice
                ],
                "page": page_name,
                "wcag_violations": results.violations_count,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert rejected == []
    assert console_errors == []
    assert page_errors == []
    context.close()


@pytest.mark.browser
def test_skip_link_focus_and_reduced_motion_are_explicit(installed_server: object, browser: Browser) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options({"width": 1440, "height": 900}))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Skip to main content")
    expect(skip).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("main")).to_be_focused()
    maximum_duration_ms = page.locator("*").evaluate_all(
        """elements => elements.flatMap(element => {
          const style = getComputedStyle(element);
          return [style.animationDuration, style.transitionDuration];
        }).flatMap(value => value.split(',')).reduce((maximum, value) => {
          const trimmed = value.trim();
          const milliseconds = trimmed.endsWith('ms')
            ? Number.parseFloat(trimmed)
            : Number.parseFloat(trimmed) * 1000;
          return Math.max(maximum, milliseconds || 0);
        }, 0)"""
    )
    assert maximum_duration_ms <= 0.1
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_populated_evidence_pages_pass_axe_and_semantic_alternatives(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    browser_artifact_root: Path,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    pages = (
        ("report", manifest.replay_report.fixed_url),
        ("evidence", manifest.evidence.fixed_url),
        ("map-detail", manifest.map.fixed_url),
        ("player-leex279", manifest.players[0].profile_url),
        ("player-fox27", manifest.players[1].profile_url),
        ("comparison", manifest.comparison.fixed_url),
        ("job-detail", manifest.pending_job.fixed_url),
        ("identity", manifest.players[0].identity_url),
    )
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    axe = Axe(axe_script=local_axe_script().decode("utf-8"))
    axe_version: str | None = None
    for page_name, path in pages:
        _goto_populated_page(page, origin, path, populated_fixture_template)
        assert page.locator("h1").count() == 1
        assert page.get_by_role("banner").count() == 1
        assert page.get_by_role("navigation", name="Primary navigation").count() == 1
        assert page.locator("main").count() == 1
        assert page.locator("footer").count() == 1
        results = axe.run(page, options={"runOnly": {"type": "tag", "values": list(WCAG_TAGS)}})
        axe_version = results.response["testEngine"]["version"]
        assert results.violations_count == 0, results.generate_report()
        all_results = axe.run(page, options={"resultTypes": ["violations"]})
        best_practice = tuple(
            violation
            for violation in all_results.response["violations"]
            if "best-practice" in violation["tags"]
        )
        assert not tuple(
            violation
            for violation in best_practice
            if violation["impact"] in {"serious", "critical"}
        )
        (browser_artifact_root / f"axe--{page_name}.json").write_text(
            json.dumps(
                {
                    "axe_engine_version": axe_version,
                    "best_practice_findings": [
                        {"id": item["id"], "impact": item["impact"], "nodes": len(item["nodes"])}
                        for item in best_practice
                    ],
                    "page": page_name,
                    "wcag_violations": results.violations_count,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    assert axe_version
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_populated_charts_and_map_have_complete_server_rendered_alternatives(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)

    _goto_populated_page(page, origin, manifest.replay_report.fixed_url, populated_fixture_template)
    expect(page.get_by_role("img", name="Replay event timeline")).to_be_visible()
    page.locator("details.timeline-event-log > summary").click()
    expect(page.get_by_role("table", name="Timeline data in authoritative replay frames")).to_be_visible()
    assert page.locator("details.evidence-drawer").count() > 0

    _goto_populated_page(page, origin, manifest.map.fixed_url, populated_fixture_template)
    expect(page.get_by_role("img", name="Authoritative replay map scene")).to_be_visible()
    for caption in (
        "Observed samples",
        "Validated route segments and gaps",
        "Engagements",
        "Observed sample presence share",
    ):
        expect(page.get_by_role("table", name=caption)).to_be_visible()
    expect(page.get_by_text("Original samples", exact=True)).to_be_visible()
    expect(page.get_by_text("Returned samples", exact=True)).to_be_visible()

    _goto_populated_page(page, origin, manifest.players[0].profile_url, populated_fixture_template)
    expect(page.get_by_text("More analyzed matches are needed", exact=True)).to_be_visible()
    assert page.get_by_role("img", name="Player distribution chart").count() == 0
    assert page.get_by_role(
        "table", name="Evidence-backed longitudinal insights with sample and missing counts"
    ).count() == 0

    _goto_populated_page(page, origin, manifest.comparison.fixed_url, populated_fixture_template)
    assert manifest.comparison.state == "not_comparable"
    expect(page.get_by_text("More comparable matches are needed", exact=True)).to_be_visible()
    expect(page.locator(".reason-line")).to_contain_text("Why:")
    assert page.locator("[data-comparison-chart] canvas").count() == 0
    expect(
        page.get_by_role("table", name="Comparison values, samples, exclusions, intervals, and evidence")
    ).to_be_visible()
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()
