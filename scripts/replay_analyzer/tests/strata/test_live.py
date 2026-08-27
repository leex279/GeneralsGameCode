from __future__ import annotations

from dataclasses import replace

import pytest
from playwright.sync_api import Error as PlaywrightError

from generals_replay_analyzer.strata.acquisition import AcquisitionIncompleteError, StrataAcquirer
from generals_replay_analyzer.strata.browser import PlaywrightListingBrowser
from generals_replay_analyzer.strata.cache import ResolverCache
from generals_replay_analyzer.strata.config import AcquisitionCaps, ResolverSettings
from generals_replay_analyzer.strata.http import HttpxStrataClient
from generals_replay_analyzer.strata.normalization import normalize_query_name


@pytest.mark.strata_live
def test_bounded_public_strata_source_contract(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Probe one listing, one complete profile, and one shared match through production boundaries."""
    base = ResolverSettings.from_sources({"cache_path": tmp_path / "resolver.sqlite3"}, {})
    settings = replace(base, caps=AcquisitionCaps(player_search_pages=1, candidate_ids=1))
    try:
        with (
            ResolverCache(settings.cache_path) as cache,
            PlaywrightListingBrowser(settings) as browser,
            HttpxStrataClient(settings, random_seed=0) as http,
        ):
            acquirer = StrataAcquirer(cache, browser, http, settings, cache.clock)
            discovery = acquirer.search(normalize_query_name("fish"), refresh=True, offline=False)
            assert discovery.complete is False
            assert discovery.reason_codes == ("candidate_id_cap",)
            assert discovery.pages_visited == 1

            profile = acquirer.profile(17945, refresh=True, offline=False)
            fish = next(alias for alias in profile.aliases if alias.name_raw == "fish")
            assert profile.most_known_name == "-DoMiNaToR-"
            assert fish.occurrence_count == 8

            match = acquirer.match(3133811, refresh=True, offline=False)
            assert [participant.player_id for participant in match.participants] == [27965, 102894]
    except PlaywrightError as error:
        pytest.skip(f"Chromium is unavailable for the opt-in Strata check: {error.__class__.__name__}")
    except AcquisitionIncompleteError as error:
        if error.code.endswith("request_failed"):
            pytest.skip(f"Strata network is unavailable for the opt-in check: {error.code}")
        raise
