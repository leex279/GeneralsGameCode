from __future__ import annotations

from pathlib import Path

from generals_replay_analyzer.strata.browser import PlaywrightListingBrowser
from generals_replay_analyzer.strata.config import AcquisitionCaps, ResolverSettings
from generals_replay_analyzer.strata.normalization import normalize_query_name

from .fakes import FakeBrowserPage

FIXTURES = Path("tests/fixtures/strata")


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _browser(tmp_path: Path, page: FakeBrowserPage) -> PlaywrightListingBrowser:
    settings = ResolverSettings.from_sources({"cache_path": tmp_path / "resolver.sqlite3"}, {})
    return PlaywrightListingBrowser(settings, page_factory=lambda: page)


def test_player_search_deduplicates_numeric_profile_ids_and_exhausts_next(tmp_path: Path) -> None:
    page = FakeBrowserPage((_html("player-search-fish-page-1.html"), _html("player-search-fish-page-2.html")))
    browser = _browser(tmp_path, page)

    result = browser.search_players(normalize_query_name("fish"), AcquisitionCaps())

    assert result.player_ids == (17945, 6522, 30124, 30427, 52866)
    assert result.complete is True
    assert result.pages_visited == 2
    assert page.navigated_urls == ["https://strata.gamereplays.org/zh/players?search=fish&amount=100"]


def test_search_url_encodes_raw_name_without_stripping_punctuation(tmp_path: Path) -> None:
    page = FakeBrowserPage((_html("player-search-empty.html"),))

    _browser(tmp_path, page).search_players(normalize_query_name(" [TAG]Fish! "), AcquisitionCaps())

    assert page.navigated_urls == [
        "https://strata.gamereplays.org/zh/players?search=+%5BTAG%5DFish%21+&amount=100"
    ]


def test_skeleton_without_rows_or_verified_empty_state_is_incomplete(tmp_path: Path) -> None:
    page = FakeBrowserPage((_html("player-search-skeleton.html"),), ready=(False,))

    result = _browser(tmp_path, page).search_players(normalize_query_name("fish"), AcquisitionCaps())

    assert result.complete is False
    assert result.reason_codes == ("listing_not_ready",)


def test_verified_empty_state_is_a_complete_empty_result(tmp_path: Path) -> None:
    page = FakeBrowserPage((_html("player-search-empty.html"),))

    result = _browser(tmp_path, page).search_players(normalize_query_name("nobody"), AcquisitionCaps())

    assert result.complete is True
    assert result.player_ids == ()


def test_page_cap_and_pagination_cycle_fail_closed(tmp_path: Path) -> None:
    first = _html("player-search-fish-page-1.html")
    cap_page = FakeBrowserPage((first, _html("player-search-fish-page-2.html")))
    capped = _browser(tmp_path, cap_page).search_players(
        normalize_query_name("fish"), AcquisitionCaps(player_search_pages=1)
    )
    assert capped.complete is False and capped.reason_codes == ("player_search_page_cap",)

    cycle_page = FakeBrowserPage((first, first))
    cycled = _browser(tmp_path, cycle_page).search_players(normalize_query_name("fish"), AcquisitionCaps())
    assert cycled.complete is False and cycled.reason_codes == ("pagination_cycle",)


def test_candidate_id_cap_fails_closed_without_exceeding_limit(tmp_path: Path) -> None:
    page = FakeBrowserPage((_html("player-search-fish-page-1.html"),))

    result = _browser(tmp_path, page).search_players(normalize_query_name("fish"), AcquisitionCaps(candidate_ids=1))

    assert result.player_ids == (17945,)
    assert result.complete is False
    assert result.reason_codes == ("candidate_id_cap",)


def test_player_match_listing_collects_numeric_match_ids(tmp_path: Path) -> None:
    page = FakeBrowserPage((_html("player-17945-matches-page-1.html"),))

    result = _browser(tmp_path, page).player_matches(17945, AcquisitionCaps())

    assert result.match_ids == (3376163, 3133811)
    assert result.complete is True
    assert page.navigated_urls == ["https://strata.gamereplays.org/zh/player/17945?tab=matches"]
