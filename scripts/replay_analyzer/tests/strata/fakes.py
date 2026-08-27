from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FakeBrowserPage:
    snapshots: tuple[str, ...]
    ready: tuple[bool, ...] | None = None

    def __post_init__(self) -> None:
        self.index = 0
        self.navigated_urls: list[str] = []

    def navigate(self, url: str, timeout_ms: int) -> None:
        self.navigated_urls.append(url)

    def wait_ready(self, kind: str, timeout_ms: int) -> bool:
        return True if self.ready is None else self.ready[self.index]

    def content(self) -> str:
        return self.snapshots[self.index]

    def next_enabled(self) -> bool:
        return self.index < len(self.snapshots) - 1

    def click_next(self, timeout_ms: int) -> bool:
        if not self.next_enabled():
            return False
        self.index += 1
        return True


class FailIfCalledBrowser:
    def search_players(self, query, caps):  # type: ignore[no-untyped-def]
        raise AssertionError("browser must not be called")

    def player_matches(self, player_id, caps):  # type: ignore[no-untyped-def]
        raise AssertionError("browser must not be called")


class FailIfCalledHttp:
    def get_profile(self, player_id: int) -> str:
        raise AssertionError("HTTP must not be called")

    def get_match(self, match_id: int) -> str:
        raise AssertionError("HTTP must not be called")

    def download_replay(self, url: str) -> bytes:
        raise AssertionError("HTTP must not be called")
