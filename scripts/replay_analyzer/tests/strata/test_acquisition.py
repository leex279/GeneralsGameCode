from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from generals_replay_analyzer.strata.acquisition import AcquisitionIncompleteError, StrataAcquirer
from generals_replay_analyzer.strata.browser import PlayerSearchDiscovery
from generals_replay_analyzer.strata.cache import ResolverCache
from generals_replay_analyzer.strata.config import ResolverSettings
from generals_replay_analyzer.strata.extract import extract_profile
from generals_replay_analyzer.strata.normalization import normalize_query_name

from .fakes import FailIfCalledBrowser, FailIfCalledHttp

NOW = datetime(2026, 8, 27, 20, 32, 43, tzinfo=UTC)
FIXTURES = Path("tests/fixtures/strata")


@dataclass
class MutableClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


class OneSearchBrowser:
    def __init__(self) -> None:
        self.calls = 0

    def search_players(self, query, caps):  # type: ignore[no-untyped-def]
        self.calls += 1
        return PlayerSearchDiscovery((17945,), True, 1, ())

    def player_matches(self, player_id, caps):  # type: ignore[no-untyped-def]
        raise AssertionError("not used")


def _settings(tmp_path: Path) -> ResolverSettings:
    return ResolverSettings.from_sources({"cache_path": tmp_path / "resolver.sqlite3"}, {})


def _profile(player_id: int = 17945):  # type: ignore[no-untyped-def]
    return extract_profile((FIXTURES / f"player-{player_id}.html").read_text(encoding="utf-8"), player_id)


def test_unexpired_profile_cache_avoids_http_and_preserves_alias_order(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    with ResolverCache(settings.cache_path, clock=clock) as cache:
        cache.put_profile(_profile(), fetched_at=NOW, expires_at=NOW + timedelta(days=7))
        acquirer = StrataAcquirer(cache, FailIfCalledBrowser(), FailIfCalledHttp(), settings, clock)

        profile = acquirer.profile(17945, refresh=False, offline=False)

        assert profile.most_known_name == "-DoMiNaToR-"
        assert profile.aliases[15].name_raw == "fish"


def test_offline_cache_miss_is_incomplete_not_not_found(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    with ResolverCache(settings.cache_path, clock=clock) as cache:
        acquirer = StrataAcquirer(cache, FailIfCalledBrowser(), FailIfCalledHttp(), settings, clock)

        result = acquirer.search(normalize_query_name("fish"), refresh=False, offline=True)

        assert result.complete is False
        assert result.reason_codes == ("offline_cache_miss",)


def test_fresh_search_is_cached_and_second_call_avoids_browser(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    browser = OneSearchBrowser()
    with ResolverCache(settings.cache_path, clock=clock) as cache:
        acquirer = StrataAcquirer(cache, browser, FailIfCalledHttp(), settings, clock)

        first = acquirer.search(normalize_query_name("fish"), refresh=False, offline=False)
        second = acquirer.search(normalize_query_name("fish"), refresh=False, offline=False)

        assert first.player_ids == second.player_ids == (17945,)
        assert browser.calls == 1
        assert second.from_cache is True


def test_offline_stale_profile_is_available_but_marked_by_acquirer(tmp_path: Path) -> None:
    clock = MutableClock(NOW + timedelta(days=8))
    settings = _settings(tmp_path)
    with ResolverCache(settings.cache_path, clock=clock) as cache:
        cache.put_profile(_profile(), fetched_at=NOW, expires_at=NOW + timedelta(days=7))
        acquirer = StrataAcquirer(cache, FailIfCalledBrowser(), FailIfCalledHttp(), settings, clock)

        assert acquirer.profile(17945, refresh=False, offline=True).player_id == 17945
        assert acquirer.decisions[-1].reason_code == "offline_stale_cache"


def test_profile_source_failure_has_stable_incomplete_error(tmp_path: Path) -> None:
    class BadHttp(FailIfCalledHttp):
        def get_profile(self, player_id: int) -> str:
            return "<html><title>changed</title></html>"

    clock = MutableClock()
    settings = _settings(tmp_path)
    with ResolverCache(settings.cache_path, clock=clock) as cache:
        acquirer = StrataAcquirer(cache, FailIfCalledBrowser(), BadHttp(), settings, clock)

        with pytest.raises(AcquisitionIncompleteError) as raised:
            acquirer.profile(17945, refresh=False, offline=False)

    assert raised.value.code == "profile_contract_changed"
