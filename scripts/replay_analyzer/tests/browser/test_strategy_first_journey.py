"""Installed-wheel proof for the strategy-first one-replay journey."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect

from .populated_fixture import PopulatedFixtureResult
from .support import (
    assert_browser_clean,
    deterministic_context_options,
    install_browser_error_guard,
    install_same_origin_guard,
)
from .test_offline import VIEWPORTS


@pytest.mark.browser
def test_pinned_replay_opens_an_honest_strategy_first_report_from_the_installed_wheel(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    browser_artifact_root: Path,
) -> None:
    """Prove the primary product journey without claiming evidence after the CRC boundary."""

    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)

    response = page.goto(f"{origin}/replays", wait_until="domcontentloaded", timeout=30_000)
    assert response is not None and response.status == 200
    replay_row = page.locator(f'tr[data-replay-public-id="{manifest.replay_public_id}"]')
    expect(replay_row).to_contain_text("leex279")
    expect(replay_row).to_contain_text("FOX27")
    expect(replay_row.get_by_text("unavailable", exact=True)).to_be_visible()
    replay_row.get_by_role("link", name="View analysis", exact=True).click()
    expect(page).to_have_url(f"{origin}{manifest.player_reports[0].fixed_url}")
    expect(page.locator("#timeline-axis-frame")).to_be_enabled()

    expect(page.get_by_role("heading", name="Observed opening through 0:03.5", exact=True)).to_be_visible()
    expect(page.get_by_text("Opening-only analysis:", exact=False)).to_be_visible()
    expect(page.get_by_text("result and later phases are not established", exact=False)).to_be_visible()
    expect(page.get_by_role("heading", name="What happened", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Strategy", exact=True)).to_be_visible()
    expect(page.get_by_text("Army composition", exact=True)).to_be_visible()
    expect(page.get_by_text("Crusader", exact=False).first).to_be_visible()
    expect(page.get_by_role("heading", name="Match timeline", exact=True)).to_be_visible()

    technical = page.locator("#technical-evidence")
    expect(technical).not_to_have_attribute("open", "")
    expect(technical.locator(":scope > summary")).to_contain_text("Technical evidence and provenance")
    assert page.locator(".versus-grid em").count() == 0
    visible_product_text = page.locator("main").inner_text().casefold()
    assert all(token not in visible_product_text for token in ("winner", "victory", "mid game", "late game"))

    artifact = browser_artifact_root / "strategy-first-one-replay--desktop.png"
    page.screenshot(path=artifact, full_page=True)
    assert artifact.stat().st_size > 1_000
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()
