"""Installed-wheel proof for replay-wide terminal-quality timeline markers."""

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
def test_replay_wide_crc_mismatch_remains_a_visible_quality_timeline_marker(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    browser_artifact_root: Path,
) -> None:
    """Catch terminal integrity evidence being discarded when gameplay claims are unavailable."""

    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)

    response = page.goto(
        f"{origin}{manifest.replay_report.fixed_url}",
        wait_until="networkidle",
        timeout=30_000,
    )

    assert response is not None and response.status == 200
    expect(page.get_by_role("heading", name="Match timeline", exact=True)).to_be_visible()
    expect(page.get_by_text("Timeline unavailable", exact=False)).not_to_be_visible()
    page.locator("details.timeline-event-log > summary").click()
    expect(page.get_by_role("region", name="Replay timeline table", exact=True)).to_contain_text(
        "CRC mismatch at frame 100"
    )
    expect(page.get_by_role("region", name="Replay timeline table", exact=True)).to_contain_text("100")

    artifact = browser_artifact_root / "replay-wide-crc-quality-timeline--desktop.png"
    page.screenshot(path=artifact, full_page=True)
    assert artifact.stat().st_size > 1_000
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()
