from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from generals_replay_analyzer.errors import ReplayParseError
from generals_replay_analyzer.strata.acquisition import StrataAcquirer
from generals_replay_analyzer.strata.browser import MatchListDiscovery, PlayerSearchDiscovery
from generals_replay_analyzer.strata.cache import ResolverCache
from generals_replay_analyzer.strata.config import ResolverSettings
from generals_replay_analyzer.strata.contracts import Confidence, ResolutionStatus
from generals_replay_analyzer.strata.service import StrataResolver

FIXTURES = Path("tests/fixtures/strata")
PINNED_REPLAY = Path("tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep")
NOW = datetime(2026, 8, 27, 20, 32, 43, tzinfo=UTC)


@dataclass
class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _profile_html(name: str) -> str:
    return (
        f"<!doctype html><html><head><title>{name} | Strata</title></head><body>"
        f"<section><p>Known Names</p><div><span>{name}</span><span>10</span></div></section>"
        "</body></html>"
    )


class FixtureBrowser:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.match_calls: list[int] = []

    def search_players(self, query, caps):  # type: ignore[no-untyped-def]
        self.search_calls.append(query.raw)
        ids = {
            "fish": (17945, 6522, 30124, 30427, 52866),
            "leex279": (27965,),
            "FOX27": (102894,),
        }.get(query.raw, ())
        return PlayerSearchDiscovery(ids, True, 1, ())

    def player_matches(self, player_id, caps):  # type: ignore[no-untyped-def]
        self.match_calls.append(player_id)
        return MatchListDiscovery((3133811,), True, 1, ())


class FixtureHttp:
    def __init__(self, *, fail_profile: int | None = None) -> None:
        self.fail_profile = fail_profile
        self.profile_calls: list[int] = []
        self.match_calls: list[int] = []
        self.download_calls: list[str] = []

    def get_profile(self, player_id: int) -> str:
        self.profile_calls.append(player_id)
        if player_id == self.fail_profile:
            return "<html><title>changed</title></html>"
        fixture = FIXTURES / f"player-{player_id}.html"
        if fixture.exists():
            return fixture.read_text(encoding="utf-8")
        names = {27965: "leex279", 102894: "FOX27"}
        return _profile_html(names[player_id])

    def get_match(self, match_id: int) -> str:
        self.match_calls.append(match_id)
        return (FIXTURES / f"match-{match_id}.html").read_text(encoding="utf-8")

    def download_replay(self, url: str) -> bytes:
        self.download_calls.append(url)
        return PINNED_REPLAY.read_bytes()


def _resolver(tmp_path: Path, *, fail_profile: int | None = None):  # type: ignore[no-untyped-def]
    settings = ResolverSettings.from_sources({"cache_path": tmp_path / "resolver.sqlite3"}, {})
    cache = ResolverCache(settings.cache_path, clock=FrozenClock())
    cache.__enter__()
    browser = FixtureBrowser()
    http = FixtureHttp(fail_profile=fail_profile)
    acquirer = StrataAcquirer(cache, browser, http, settings, FrozenClock())
    return StrataResolver(acquirer, cache, http, FrozenClock()), cache, browser, http


def test_name_service_fetches_all_profiles_preserves_ambiguity_and_audits(tmp_path: Path) -> None:
    resolver, cache, browser, http = _resolver(tmp_path)
    try:
        first = resolver.resolve_name("fish", refresh=False, offline=False, include_fuzzy=False)
        second = resolver.resolve_name("fish", refresh=False, offline=False, include_fuzzy=False)

        assert first.status is ResolutionStatus.AMBIGUOUS
        assert first.selected is not None and first.selected.player_id == 17945
        assert [item.player_id for item in first.alternatives] == [6522, 30124]
        assert [item.player_id for item in first.case_insensitive_suggestions] == [30427, 52866]
        assert sorted(http.profile_calls) == [6522, 17945, 30124, 30427, 52866]
        assert browser.search_calls == ["fish"]
        assert cache.status().audit_rows == 2
        assert second.to_dict()["query_name"] == "fish"
    finally:
        cache.__exit__(None, None, None)


def test_profile_failure_marks_name_result_incomplete_without_leaking_html(tmp_path: Path) -> None:
    resolver, cache, _, _ = _resolver(tmp_path, fail_profile=6522)
    try:
        result = resolver.resolve_name("fish", refresh=False, offline=False, include_fuzzy=False)
        assert result.status is ResolutionStatus.INCOMPLETE
        assert "profile_contract_changed" in result.reason_codes
        assert "<html>" not in str(result.to_dict())
    finally:
        cache.__exit__(None, None, None)


def test_pinned_replay_resolves_shared_match_and_both_player_ids(tmp_path: Path) -> None:
    resolver, cache, browser, http = _resolver(tmp_path)
    try:
        result = resolver.resolve_replay(PINNED_REPLAY, refresh=False, offline=False, include_fuzzy=False)

        assert result.match_resolution.status is ResolutionStatus.RESOLVED
        assert result.match_resolution.selected_match_id == 3133811
        assert [(item.slot_index, item.query.raw, item.selected.player_id) for item in result.players if item.selected] == [
            (0, "leex279", 27965),
            (1, "FOX27", 102894),
        ]
        assert all(item.confidence in {Confidence.HIGH, Confidence.MEDIUM} for item in result.players)
        assert browser.match_calls == [27965, 102894]
        assert http.match_calls == [3133811]
        assert len(http.download_calls) == 2
        assert cache.status().audit_rows == 2
    finally:
        cache.__exit__(None, None, None)


def test_invalid_replay_fails_before_network(tmp_path: Path) -> None:
    resolver, cache, browser, http = _resolver(tmp_path)
    invalid = tmp_path / "invalid.rep"
    invalid.write_bytes(b"not a replay")
    try:
        with pytest.raises(ReplayParseError):
            resolver.resolve_replay(invalid, refresh=False, offline=False, include_fuzzy=False)
        assert browser.search_calls == []
        assert http.profile_calls == []
        assert http.match_calls == []
        assert http.download_calls == []
    finally:
        cache.__exit__(None, None, None)
