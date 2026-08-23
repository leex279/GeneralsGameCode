"""Keyboard, focus, responsive, and external screenshot acceptance."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from .populated_fixture import PopulatedFixtureResult
from .support import (
    ExternalWorkerController,
    assert_browser_clean,
    deterministic_context_options,
    install_browser_error_guard,
    install_same_origin_guard,
)
from .test_offline import VIEWPORTS, _goto_populated_page

SCREENSHOTS = (
    ("dashboard--desktop.png", "/", "Replay dashboard", "desktop"),
    ("library--desktop.png", "/replays", "Replay library", "desktop"),
    ("jobs--desktop.png", "/jobs", "Analysis jobs", "desktop"),
    ("maps-index--desktop.png", "/maps", "Authoritative map scenes", "desktop"),
    ("players--desktop.png", "/players", "Player Evidence", "desktop"),
    ("compare--desktop.png", "/compare", "Pattern Comparison", "desktop"),
    ("settings--desktop.png", "/settings", "Analyzer settings", "desktop"),
    ("library--tablet.png", "/replays", "Replay library", "tablet"),
    ("library--mobile.png", "/replays", "Replay library", "mobile"),
    ("compare--mobile.png", "/compare", "Pattern Comparison", "mobile"),
)
POPULATED_SCREENSHOTS = (
    ("report--desktop.png", "report", "Timeline data in authoritative replay frames", "desktop"),
    ("map-detail--desktop.png", "map", "Observed sample reduction", "desktop"),
    ("player-profile--desktop.png", "profile", "Names & Provenance", "desktop"),
    ("report--tablet.png", "report", "Timeline data in authoritative replay frames", "tablet"),
    ("map-detail--tablet.png", "map", "Observed sample reduction", "tablet"),
    ("report--mobile.png", "report", "Timeline data in authoritative replay frames", "mobile"),
    ("map-detail--mobile.png", "map", "Observed sample reduction", "mobile"),
)


def _tab_to(page: Page, locator: Locator, *, limit: int = 400) -> None:
    for _ in range(limit):
        page.keyboard.press("Tab")
        if locator.evaluate("element => element === document.activeElement"):
            return
    expect(locator).to_be_focused()


def _type_by_keyboard(page: Page, locator: Locator, value: str) -> None:
    _tab_to(page, locator)
    page.keyboard.type(value)
    expect(locator).to_have_value(value)


@pytest.mark.browser
def test_keyboard_skip_navigation_and_focus_indicator(installed_server: object, browser: Browser) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    install_same_origin_guard(page, origin)
    page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to main content")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("main")).to_be_focused()
    style = page.locator("main").evaluate(
        """element => { const value = getComputedStyle(element); return {
          outlineWidth: value.outlineWidth, outlineColor: value.outlineColor,
          boxShadow: value.boxShadow, borderWidth: value.borderWidth
        }; }"""
    )
    assert style["outlineWidth"] != "0px" or style["boxShadow"] != "none" or style["borderWidth"] != "0px"
    page.reload(wait_until="domcontentloaded")
    dashboard = page.get_by_role("link", name="Dashboard", exact=True)
    for _ in range(12):
        page.keyboard.press("Tab")
        if dashboard.evaluate("element => element === document.activeElement"):
            break
    expect(dashboard).to_be_focused()
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_keyboard_import_dialog_escape_returns_to_library_invoker(
    installed_server: object,
    browser: Browser,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    failures = install_same_origin_guard(page, origin)
    page.goto(f"{origin}/replays", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function(
        "() => document.documentElement.classList.contains('js-enhanced') && typeof window.htmx === 'object'",
        timeout=30_000,
    )
    invoker = page.get_by_role("link", name="Import replay", exact=True)
    for _ in range(40):
        page.keyboard.press("Tab")
        if invoker.evaluate("element => element === document.activeElement"):
            break
    expect(invoker).to_be_focused()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Import replay")
    expect(dialog).to_be_visible()
    close_button = page.get_by_role("button", name="Close import dialog")
    expect(close_button).to_be_focused()
    page.keyboard.press("Shift+Tab")
    reverse_tab_target = page.evaluate(
        """() => ({
            tag: document.activeElement?.tagName,
            id: document.activeElement?.id,
            role: document.activeElement?.getAttribute?.("role"),
            href: document.activeElement?.getAttribute?.("href"),
            text: document.activeElement?.textContent?.trim().slice(0, 80),
        })"""
    )
    assert dialog.evaluate(
        "element => element === document.activeElement || element.contains(document.activeElement)"
    ), reverse_tab_target
    page.keyboard.press("Tab")
    forward_tab_target = page.evaluate(
        """() => ({
            tag: document.activeElement?.tagName,
            id: document.activeElement?.id,
            role: document.activeElement?.getAttribute?.("role"),
            href: document.activeElement?.getAttribute?.("href"),
            text: document.activeElement?.textContent?.trim().slice(0, 80),
        })"""
    )
    assert dialog.evaluate(
        "element => element === document.activeElement || element.contains(document.activeElement)"
    ), forward_tab_target
    page.keyboard.press("Escape")
    expect(page).to_have_url(f"{origin}/replays")
    expect(page.get_by_role("link", name="Import replay", exact=True)).to_be_focused()
    assert failures == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_configured_root_duplicate_import_is_keyboard_submitted_truthfully(
    mutable_populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
) -> None:
    origin = mutable_populated_server.origin
    manifest = populated_fixture_template.manifest
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    page.goto(f"{origin}/replays", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function(
        "() => document.documentElement.classList.contains('js-enhanced') && typeof window.htmx === 'object'",
        timeout=30_000,
    )
    invoker = page.get_by_role("link", name="Import replay", exact=True)
    _tab_to(page, invoker)
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Import replay", exact=True)
    expect(dialog).to_be_visible()
    root = page.get_by_label("Configured replay root", exact=True)
    _tab_to(page, root)
    expect(root).to_have_value(manifest.import_root_public_id)
    relative_name = page.get_by_label("Relative replay name", exact=True)
    _type_by_keyboard(page, relative_name, manifest.import_relative_path)
    submit = page.get_by_role("button", name="Import configured replay", exact=True)
    _tab_to(page, submit)
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(rf"^{re.escape(origin)}/imports/root-selections$"), timeout=30_000)
    payload = json.loads(page.locator("body").inner_text())
    assert payload["duplicate_of_replay_public_id"] == manifest.replay_public_id
    assert payload["problem_code"] is None
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_keyboard_command_palette_traps_and_returns_focus(
    installed_server: object,
    browser: Browser,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    failures = install_same_origin_guard(page, origin)
    page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
    invoker = page.get_by_role("button", name="Navigate", exact=True)
    for _ in range(30):
        page.keyboard.press("Tab")
        if invoker.evaluate("element => element === document.activeElement"):
            break
    expect(invoker).to_be_focused()
    page.keyboard.press("Enter")
    palette = page.get_by_role("dialog", name="Navigate")
    expect(palette).to_be_visible()
    expect(palette.get_by_role("button", name="Close", exact=True)).to_be_focused()
    page.keyboard.press("Shift+Tab")
    reverse_tab_target = page.evaluate(
        """() => ({
            tag: document.activeElement?.tagName,
            id: document.activeElement?.id,
            role: document.activeElement?.getAttribute?.("role"),
            href: document.activeElement?.getAttribute?.("href"),
            text: document.activeElement?.textContent?.trim().slice(0, 80),
        })"""
    )
    assert palette.evaluate(
        "dialog => dialog === document.activeElement || dialog.contains(document.activeElement)"
    ), reverse_tab_target
    page.keyboard.press("Tab")
    assert palette.evaluate(
        "dialog => dialog === document.activeElement || dialog.contains(document.activeElement)"
    )
    page.keyboard.press("Escape")
    expect(palette).not_to_be_visible()
    expect(invoker).to_be_focused()
    expect(page.locator("#app-feedback")).to_contain_text("Navigation dialog closed")
    assert failures == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_pending_job_is_settled_only_by_the_external_installed_worker(
    mutable_populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    populated_worker_controller: ExternalWorkerController,
    browser: Browser,
) -> None:
    origin = mutable_populated_server.origin
    pending = populated_fixture_template.manifest.pending_job
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    failures = install_same_origin_guard(page, origin)
    page.goto(f"{origin}/jobs", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function(
        "() => document.documentElement.classList.contains('js-enhanced') && typeof window.htmx === 'object'",
        timeout=30_000,
    )
    job_row = page.get_by_role("row").filter(has_text=pending.job_public_id)
    expect(job_row).to_contain_text("pending")

    stage_filter = page.get_by_label("Stage", exact=True)
    for _ in range(40):
        page.keyboard.press("Tab")
        if stage_filter.evaluate("element => element === document.activeElement"):
            break
    expect(stage_filter).to_be_focused()
    populated_worker_controller.start()
    expect(job_row).to_contain_text("succeeded", timeout=60_000)
    expect(stage_filter).to_be_focused()

    job_link = page.get_by_role("link", name=pending.job_public_id, exact=True)
    for _ in range(300):
        page.keyboard.press("Tab")
        if job_link.evaluate("element => element === document.activeElement"):
            break
    expect(job_link).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Analysis job", exact=True)).to_be_visible()
    expect(page.get_by_text("succeeded", exact=True).first).to_be_visible()
    assert failures == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_report_timeline_and_evidence_disclosure_are_keyboard_operable(
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
    _goto_populated_page(page, origin, manifest.player_reports[0].fixed_url, populated_fixture_template)
    expect(page.get_by_role("heading", name="Quality and availability", exact=True)).to_be_visible()

    player_toggles = page.locator("[data-timeline-player]")
    assert player_toggles.count() > 0, "populated fixed report must expose a player timeline control"
    player_toggle = player_toggles.first
    _tab_to(page, player_toggle)
    expect(player_toggle).to_be_checked()
    page.keyboard.press("Space")
    expect(player_toggle).not_to_be_checked()
    page.keyboard.press("Space")
    expect(player_toggle).to_be_checked()

    family_toggle = page.locator("[data-timeline-family]").first
    _tab_to(page, family_toggle)
    expect(family_toggle).to_be_checked()
    page.keyboard.press("Space")
    expect(family_toggle).not_to_be_checked()

    drawer = page.locator("details.evidence-drawer").first
    summary = drawer.locator("summary")
    _tab_to(page, summary)
    page.keyboard.press("Enter")
    expect(drawer).to_have_attribute("open", "")
    evidence_link = drawer.get_by_role("link", name="Open evidence detail", exact=True)
    page.keyboard.press("Tab")
    expect(evidence_link).to_be_focused()
    href = evidence_link.get_attribute("href")
    assert href and href.startswith("/evidence/")
    page.keyboard.press("Shift+Tab")
    expect(summary).to_be_focused()
    page.keyboard.press("Enter")
    expect(drawer).not_to_have_attribute("open", "")
    expect(summary).to_be_focused()
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_map_filters_and_semantic_evidence_are_keyboard_operable(
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
    _goto_populated_page(page, origin, manifest.map.fixed_url, populated_fixture_template)

    frame_start = page.get_by_label("Frame start", exact=True)
    start_value = int(frame_start.input_value())
    _tab_to(page, frame_start)
    page.keyboard.press("ArrowUp")
    expect(frame_start).to_have_value(str(start_value + 1))

    start_slider = page.get_by_label("Start frame slider", exact=True)
    _tab_to(page, start_slider)
    page.keyboard.press("ArrowRight")
    expect(frame_start).to_have_value(str(start_value + 2))
    expect(start_slider).to_have_value(str(start_value + 2))

    coordinate = page.get_by_label("Coordinate display", exact=True)
    _tab_to(page, coordinate)
    page.keyboard.press("ArrowDown")
    assert coordinate.input_value() in {"map_normalized", "player_centric"}

    player_toggle = page.get_by_role("group", name="Players").locator('input[type="checkbox"]').first
    _tab_to(page, player_toggle)
    previous_player_state = player_toggle.is_checked()
    page.keyboard.press("Space")
    assert player_toggle.is_checked() is not previous_player_state

    family_toggle = page.get_by_role("group", name="Event overlays").locator('input[type="checkbox"]').first
    _tab_to(page, family_toggle)
    previous_family_state = family_toggle.is_checked()
    page.keyboard.press("Space")
    assert family_toggle.is_checked() is not previous_family_state

    tables = page.get_by_role("region", name="Map evidence tables", exact=True)
    _tab_to(page, tables)
    expect(tables).to_be_focused()
    expect(page.get_by_role("table", name="Observed samples", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Canonical scene JSON", exact=True)).to_be_visible()
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_identity_split_preview_cancel_and_execute_are_keyboard_operable(
    mutable_populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    browser_artifact_root: Path,
) -> None:
    origin = mutable_populated_server.origin
    player = populated_fixture_template.manifest.players[0]
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)

    def preview_split() -> None:
        page.goto(f"{origin}{player.identity_url}", wait_until="domcontentloaded", timeout=30_000)
        expect(page.get_by_role("heading", name="Preview Explicit Alias Split", exact=True)).to_be_visible()
        _type_by_keyboard(page, page.get_by_label("Embedded Alias Public ID", exact=True), player.alias_public_ids[0])
        _type_by_keyboard(
            page,
            page.get_by_label("Replay-Player Public IDs", exact=True),
            player.replay_player_public_id,
        )
        _type_by_keyboard(page, page.get_by_label("New Display Name", exact=True), "Task 9 Split Fixture")
        preview = page.get_by_role("button", name="Preview Explicit Split", exact=True)
        _tab_to(page, preview)
        page.keyboard.press("Enter")
        expect(page.get_by_role("heading", name="Confirm Identity Change", exact=True)).to_be_visible()
        expect(page.get_by_text("split_alias", exact=True)).to_be_visible()

    preview_split()
    page.screenshot(path=browser_artifact_root / "identity-confirmation--desktop.png", full_page=True)
    cancel = page.get_by_role("link", name="Cancel Identity Change", exact=True)
    _tab_to(page, cancel)
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Identity Guardrails", exact=True)).to_be_visible()

    preview_split()
    _type_by_keyboard(page, page.get_by_label("Operator Label", exact=True), "Task 9 browser gate")
    _type_by_keyboard(
        page,
        page.get_by_label("Reason", exact=True),
        "Exercise the accepted deterministic identity mutation.",
    )
    execute = page.get_by_role("button", name="Execute Confirmed split_alias", exact=True)
    _tab_to(page, execute)
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Identity Guardrails", exact=True)).to_be_visible()
    expect(page.locator('p[role="status"]')).to_contain_text("Invalidation")
    expect(page.get_by_role("table", name="Identity operations ordered by immutable audit time")).to_contain_text(
        "split_alias"
    )
    assert (browser_artifact_root / "identity-confirmation--desktop.png").stat().st_size > 1_000
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_settings_preview_cancel_apply_and_diagnostic_are_keyboard_operable(
    mutable_populated_server: object,
    browser: Browser,
) -> None:
    origin = mutable_populated_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)

    def preview_change() -> None:
        page.goto(f"{origin}/settings", wait_until="domcontentloaded", timeout=30_000)
        field = page.get_by_label("New Minimum longitudinal sample size", exact=True)
        _tab_to(page, field)
        page.keyboard.press("Control+A")
        page.keyboard.type("2")
        expect(field).to_have_value("2")
        preview = page.get_by_role(
            "button",
            name="Preview Minimum longitudinal sample size",
            exact=True,
        )
        _tab_to(page, preview)
        page.keyboard.press("Enter")
        expect(page.get_by_role("heading", name="Settings impact preview", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Affected analysis stages", exact=True)).to_be_visible()

    preview_change()
    cancel = page.get_by_role("link", name="Cancel settings change", exact=True)
    expect(cancel).to_be_visible()
    _tab_to(page, cancel)
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Analyzer settings", exact=True)).to_be_visible()

    preview_change()
    confirmation = page.get_by_label(
        "I confirm this versioned settings impact. Existing outputs remain unchanged until explicit re-analysis.",
        exact=True,
    )
    _tab_to(page, confirmation)
    page.keyboard.press("Space")
    expect(confirmation).to_be_checked()
    apply = page.get_by_role("button", name="Apply confirmed change", exact=True)
    _tab_to(page, apply)
    page.keyboard.press("Enter")
    mutation_status = page.locator('section.notice[role="status"]')
    expect(mutation_status).to_contain_text("Settings applied")
    expect(mutation_status).to_contain_text("No analysis jobs were queued")

    diagnostic = page.get_by_role("button", name=re.compile(r"^Run ")).first
    _tab_to(page, diagnostic)
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Diagnostic result", exact=True)).to_be_visible()
    expect(page.get_by_text("Settings revision", exact=True)).to_be_visible()
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize(("name", "path", "heading", "viewport_name"), SCREENSHOTS)
def test_named_release_screenshot_has_stable_reflow(
    installed_server: object,
    browser: Browser,
    browser_artifact_root: Path,
    name: str,
    path: str,
    heading: str,
    viewport_name: str,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS[viewport_name]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    dimensions = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert dimensions[0] <= dimensions[1]
    page.screenshot(path=browser_artifact_root / name, full_page=True)
    assert (browser_artifact_root / name).stat().st_size > 1_000
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize(
    ("name", "route_kind", "semantic_text", "viewport_name"),
    POPULATED_SCREENSHOTS,
)
def test_populated_named_release_screenshot_has_stable_reflow(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    browser_artifact_root: Path,
    name: str,
    route_kind: str,
    semantic_text: str,
    viewport_name: str,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    path = {
        "report": manifest.replay_report.fixed_url,
        "map": manifest.map.fixed_url,
        "profile": manifest.players[0].profile_url,
    }[route_kind]
    context = browser.new_context(**deterministic_context_options(VIEWPORTS[viewport_name]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    _goto_populated_page(page, origin, path, populated_fixture_template)
    expect(page.get_by_text(semantic_text, exact=False).first).to_be_visible()
    dimensions = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert dimensions[0] <= dimensions[1]
    page.screenshot(path=browser_artifact_root / name, full_page=True)
    assert (browser_artifact_root / name).stat().st_size > 1_000
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_populated_dialog_and_evidence_release_screenshots(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    browser_artifact_root: Path,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)

    page.goto(f"{origin}/replays", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function(
        "() => document.documentElement.classList.contains('js-enhanced') && typeof window.htmx === 'object'",
        timeout=30_000,
    )
    invoker = page.get_by_role("link", name="Import replay", exact=True)
    _tab_to(page, invoker)
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Import replay", exact=True)
    expect(dialog).to_be_visible()
    page.screenshot(path=browser_artifact_root / "import-dialog--desktop.png", full_page=True)

    page.keyboard.press("Escape")
    _goto_populated_page(page, origin, manifest.replay_report.fixed_url, populated_fixture_template)
    drawer = page.locator("details.evidence-drawer").first
    summary = drawer.locator("summary")
    _tab_to(page, summary)
    page.keyboard.press("Enter")
    expect(drawer).to_have_attribute("open", "")
    page.screenshot(path=browser_artifact_root / "evidence-drawer--desktop.png", full_page=True)

    for name in ("import-dialog--desktop.png", "evidence-drawer--desktop.png"):
        assert (browser_artifact_root / name).stat().st_size > 1_000
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()
