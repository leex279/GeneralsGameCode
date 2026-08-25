"""Rendered-browser contracts for readable report highlight layouts."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser

from generals_replay_analyzer.web.resources import package_resource


@pytest.mark.browser
def test_long_form_highlight_uses_a_readable_full_width_layout(browser: Browser) -> None:
    """Catch a long army composition inflating a five-column report grid by thousands of pixels."""
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")
    composition = ", ".join(
        f"{count} {name}"
        for count, name in (
            (2, "Jarmen Kell"),
            (3, "Rebels"),
            (7, "Terrorists"),
            (14, "Tunnel Defenders"),
            (20, "Workers"),
            (11, "Scorpion Tanks"),
            (13, "Quad Cannons"),
            (5, "Technical transports"),
        )
    ) * 4
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    page.set_content(
        f"""
        <style>{css}</style>
        <main>
          <div class="insight-grid">
            <article class="insight-card insight-card-metric"><span>Supply income</span><strong>2,821 supplies/min</strong></article>
            <article class="insight-card insight-card-metric"><span>Supply collected</span><strong>43,804</strong></article>
            <article class="insight-card insight-card-narrative"><span>Army composition</span><strong>{composition}</strong></article>
            <article class="insight-card insight-card-narrative"><span>Observed kills</span><strong>Quad Cannon over Scout Drone at 8:13</strong></article>
          </div>
        </main>
        """,
        wait_until="domcontentloaded",
    )

    grid = page.locator(".insight-grid").bounding_box()
    narrative = page.locator(".insight-card-narrative").first.bounding_box()
    assert grid is not None and narrative is not None
    assert narrative["width"] >= grid["width"] * 0.9
    assert narrative["height"] < 900
    assert page.evaluate("document.documentElement.scrollHeight") < 2_000
    context.close()
