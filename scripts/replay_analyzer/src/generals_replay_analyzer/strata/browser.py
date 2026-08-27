"""Playwright-backed discovery for lazy Strata player and match listings."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from .config import AcquisitionCaps, ResolverSettings
from .contracts import QueryName

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Locator, Page, Playwright

_PLAYER_PATH = re.compile(r"^/zh/player/([1-9]\d*)$")
_MATCH_PATH = re.compile(r"^/zh/match/([1-9]\d*)$")
_PLAYER_EMPTY = frozenset({"No players found", "No player found", "There are no players to display"})
_MATCH_EMPTY = frozenset({"No matches found", "No match found", "There are no matches to display"})


class BrowserPagePort(Protocol):
    def navigate(self, url: str, timeout_ms: int) -> None: ...

    def wait_ready(self, kind: str, timeout_ms: int) -> bool: ...

    def content(self) -> str: ...

    def next_enabled(self) -> bool: ...

    def click_next(self, timeout_ms: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class PlayerSearchDiscovery:
    player_ids: tuple[int, ...]
    complete: bool
    pages_visited: int
    reason_codes: tuple[str, ...]
    from_cache: bool = False
    stale: bool = False


@dataclass(frozen=True, slots=True)
class MatchListDiscovery:
    match_ids: tuple[int, ...]
    complete: bool
    pages_visited: int
    reason_codes: tuple[str, ...]
    from_cache: bool = False
    stale: bool = False


class ListingBrowserPort(Protocol):
    def search_players(self, query: QueryName, caps: AcquisitionCaps) -> PlayerSearchDiscovery: ...

    def player_matches(self, player_id: int, caps: AcquisitionCaps) -> MatchListDiscovery: ...


class _PlaywrightPage:
    def __init__(self, page: Page) -> None:
        self._page = page

    def navigate(self, url: str, timeout_ms: int) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def wait_ready(self, kind: str, timeout_ms: int) -> bool:
        path = r"/zh/player/[1-9]\d*$" if kind == "player" else r"/zh/match/[1-9]\d*$"
        empty = list(_PLAYER_EMPTY if kind == "player" else _MATCH_EMPTY)
        expression = """
            ({path, empty}) => {
                const linkPattern = new RegExp(path);
                const hasRow = [...document.querySelectorAll('a[href]')].some((link) => {
                    try { return linkPattern.test(new URL(link.href, document.baseURI).pathname); }
                    catch (_) { return false; }
                });
                const texts = [...document.querySelectorAll('p,div')].map((node) => node.textContent.trim());
                return hasRow || texts.some((text) => empty.includes(text));
            }
        """
        try:
            self._page.wait_for_function(expression, arg={"path": path, "empty": empty}, timeout=timeout_ms)
        except Exception as error:
            if error.__class__.__name__ != "TimeoutError":
                raise
            return False
        return True

    def content(self) -> str:
        return self._page.content()

    def _next(self) -> Locator | None:
        controls = self._page.locator("button, a")
        for index in range(controls.count()):
            candidate = controls.nth(index)
            if candidate.is_visible() and candidate.inner_text().strip() == "Next":
                return candidate
        return None

    def next_enabled(self) -> bool:
        control = self._next()
        if control is None:
            return False
        classes = control.get_attribute("class") or ""
        return (
            control.get_attribute("disabled") is None
            and control.get_attribute("aria-disabled") != "true"
            and "disabled" not in classes.split()
        )

    def click_next(self, timeout_ms: int) -> bool:
        control = self._next()
        if control is None or not self.next_enabled():
            return False
        previous = self._page.locator("body").inner_text()
        control.click()
        try:
            self._page.wait_for_function(
                "previous => document.body.innerText !== previous", arg=previous, timeout=timeout_ms
            )
        except Exception as error:
            if error.__class__.__name__ != "TimeoutError":
                raise
            return False
        return True


class PlaywrightListingBrowser:
    """Discover complete numeric candidate sets from Livewire-rendered listings."""

    def __init__(
        self,
        settings: ResolverSettings,
        page_factory: Callable[[], BrowserPagePort] | None = None,
    ) -> None:
        self.settings = settings
        self._page_factory = page_factory
        self._page: BrowserPagePort | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def __enter__(self) -> Self:
        if self._page_factory is not None:
            return self
        # TheSuperHackers @feature Leex 27/08/2026 Use a real browser only for lazy public listings, never private Livewire payloads.
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        browser_type = getattr(self._playwright, self.settings.browser)
        if self.settings.browser == "chrome":
            self._browser = browser_type.launch(headless=True, channel="chrome")
        else:
            self._browser = browser_type.launch(headless=True)
        self._page = _PlaywrightPage(self._browser.new_page())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None

    def _get_page(self) -> BrowserPagePort:
        if self._page_factory is not None:
            return self._page_factory()
        if self._page is None:
            raise RuntimeError("Playwright listing browser is not open")
        return self._page

    def search_players(self, query: QueryName, caps: AcquisitionCaps) -> PlayerSearchDiscovery:
        page = self._get_page()
        page.navigate(
            "https://strata.gamereplays.org/zh/players?" + urlencode({"search": query.raw, "amount": "100"}),
            int(self.settings.timeout_seconds * 1000),
        )
        ids, complete, pages, reasons = self._collect(
            page,
            kind="player",
            page_cap=caps.player_search_pages,
            id_cap=caps.candidate_ids,
        )
        return PlayerSearchDiscovery(ids, complete, pages, reasons)

    def player_matches(self, player_id: int, caps: AcquisitionCaps) -> MatchListDiscovery:
        if player_id <= 0:
            return MatchListDiscovery((), False, 0, ("invalid_player_id",))
        page = self._get_page()
        page.navigate(
            f"https://strata.gamereplays.org/zh/player/{player_id}?tab=matches",
            int(self.settings.timeout_seconds * 1000),
        )
        ids, complete, pages, reasons = self._collect(
            page,
            kind="match",
            page_cap=caps.player_match_pages,
            id_cap=10_000,
        )
        return MatchListDiscovery(ids, complete, pages, reasons)

    def _collect(
        self,
        page: BrowserPagePort,
        *,
        kind: str,
        page_cap: int,
        id_cap: int,
    ) -> tuple[tuple[int, ...], bool, int, tuple[str, ...]]:
        collected: list[int] = []
        seen_ids: set[int] = set()
        seen_pages: set[bytes] = set()
        pages = 0
        timeout_ms = int(self.settings.timeout_seconds * 1000)
        while True:
            if not page.wait_ready(kind, timeout_ms):
                return tuple(collected), False, pages, ("listing_not_ready",)
            html = page.content()
            signature = hashlib.sha256(html.encode("utf-8")).digest()
            if signature in seen_pages:
                return tuple(collected), False, pages, ("pagination_cycle",)
            seen_pages.add(signature)
            pages += 1
            page_ids = _numeric_links(html, kind)
            empty = _verified_empty(html, kind)
            if not page_ids and not empty:
                return tuple(collected), False, pages, ("listing_contract_changed",)
            for value in page_ids:
                if value in seen_ids:
                    continue
                if len(collected) >= id_cap:
                    return tuple(collected), False, pages, ("candidate_id_cap",)
                seen_ids.add(value)
                collected.append(value)
            if not page.next_enabled():
                return tuple(collected), True, pages, ()
            if pages >= page_cap:
                reason = "player_search_page_cap" if kind == "player" else "player_match_page_cap"
                return tuple(collected), False, pages, (reason,)
            if not page.click_next(timeout_ms):
                return tuple(collected), False, pages, ("pagination_transition_failed",)


def _numeric_links(html: str, kind: str) -> tuple[int, ...]:
    soup = BeautifulSoup(html, "html.parser")
    pattern = _PLAYER_PATH if kind == "player" else _MATCH_PATH
    result: list[int] = []
    seen: set[int] = set()
    for link in soup.find_all("a", href=True):
        parsed = urlparse(str(link["href"]))
        if parsed.hostname not in {None, "strata.gamereplays.org"} or parsed.query or parsed.fragment:
            continue
        matched = pattern.fullmatch(parsed.path)
        if matched is None:
            continue
        value = int(matched.group(1))
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _verified_empty(html: str, kind: str) -> bool:
    expected = _PLAYER_EMPTY if kind == "player" else _MATCH_EMPTY
    soup = BeautifulSoup(html, "html.parser")
    return any(tag.get_text(" ", strip=True) in expected for tag in soup.find_all(("p", "div")))
