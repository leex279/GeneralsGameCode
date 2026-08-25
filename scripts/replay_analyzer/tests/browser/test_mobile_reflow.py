"""Mobile layout regression coverage for release routes."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page, expect

from .support import deterministic_context_options
from .test_offline import VIEWPORTS


def _overflowing_elements(page: Page) -> list[dict[str, str | float]]:
    """Return visible in-flow elements whose right edge exceeds the viewport."""
    return page.evaluate(
        """() => [...document.querySelectorAll('body *')]
            .map((element) => ({element, rect: element.getBoundingClientRect()}))
            .filter(({element, rect}) => getComputedStyle(element).position !== 'fixed'
                && rect.width > 0
                && rect.right > document.documentElement.clientWidth + 1)
            .map(({element, rect}) => ({
                tag: element.tagName,
                className: element.className,
                right: Math.round(rect.right),
                width: Math.round(rect.width),
            }))"""
    )


@pytest.mark.browser
@pytest.mark.parametrize("path", ("/replays", "/compare"))
def test_mobile_release_routes_do_not_extend_past_viewport(installed_server: object, browser: Browser, path: str) -> None:
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["mobile"]))
    page = context.new_page()
    page.goto(f"{installed_server.origin}{path}", wait_until="domcontentloaded", timeout=30_000)
    expect(page.locator("main")).to_be_visible()
    dimensions = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert dimensions[0] <= dimensions[1], _overflowing_elements(page)
    context.close()
