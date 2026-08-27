"""Cache-aware orchestration for Strata browser and HTTP evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .browser import ListingBrowserPort, MatchListDiscovery, PlayerSearchDiscovery
from .cache import GAME, SOURCE, ResolverCache
from .config import ResolverSettings
from .contracts import MatchDocument, ProfileDocument, QueryName
from .extract import SourceContractError, extract_match, extract_profile
from .http import SourceRequestError
from .ports import Clock, StrataHttpPort


class AcquisitionIncompleteError(RuntimeError):
    """Stable incomplete-evidence result for one failed source acquisition."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AcquisitionDecision:
    kind: str
    key: str
    source: str
    stale: bool
    reason_code: str | None


class StrataAcquirer:
    """Apply resolver TTL and offline semantics around injected source ports."""

    def __init__(
        self,
        cache: ResolverCache,
        browser: ListingBrowserPort,
        http: StrataHttpPort,
        settings: ResolverSettings,
        clock: Clock,
    ) -> None:
        self.cache = cache
        self.browser = browser
        self.http = http
        self.settings = settings
        self.clock = clock
        self.decisions: list[AcquisitionDecision] = []

    def search(self, query: QueryName, *, refresh: bool, offline: bool) -> PlayerSearchDiscovery:
        entry = self.cache.get_search(SOURCE, GAME, query.raw)
        now = self.clock.now()
        if entry is not None and not refresh and (entry.expires_at > now or offline):
            stale = entry.expires_at <= now
            reason = "offline_stale_cache" if stale else None
            self.decisions.append(AcquisitionDecision("search", query.raw, "cache", stale, reason))
            return PlayerSearchDiscovery(entry.candidate_ids, entry.complete, 0, () if reason is None else (reason,), True, stale)
        if offline:
            self.decisions.append(AcquisitionDecision("search", query.raw, "cache", False, "offline_cache_miss"))
            return PlayerSearchDiscovery((), False, 0, ("offline_cache_miss",), True, False)
        discovery = self.browser.search_players(query, self.settings.caps)
        ttl = self.settings.negative_ttl_seconds if discovery.complete and not discovery.player_ids else self.settings.search_ttl_seconds
        self.cache.put_search(
            query.raw,
            discovery.player_ids,
            complete=discovery.complete,
            fetched_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        self.decisions.append(AcquisitionDecision("search", query.raw, "source", False, None))
        return discovery

    def profile(self, player_id: int, *, refresh: bool, offline: bool) -> ProfileDocument:
        entry = self.cache.get_profile(SOURCE, GAME, player_id)
        now = self.clock.now()
        if entry is not None and not refresh and (entry.expires_at > now or offline):
            stale = entry.expires_at <= now
            reason = "offline_stale_cache" if stale else None
            self.decisions.append(AcquisitionDecision("profile", str(player_id), "cache", stale, reason))
            return entry.value
        if offline:
            raise AcquisitionIncompleteError("offline_cache_miss")
        try:
            profile = extract_profile(self.http.get_profile(player_id), player_id)
        except SourceContractError as error:
            raise AcquisitionIncompleteError("profile_contract_changed") from error
        except SourceRequestError as error:
            raise AcquisitionIncompleteError("profile_request_failed") from error
        self.cache.put_profile(
            profile,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.settings.profile_ttl_seconds),
        )
        self.decisions.append(AcquisitionDecision("profile", str(player_id), "source", False, None))
        return profile

    def match(self, match_id: int, *, refresh: bool, offline: bool) -> MatchDocument:
        entry = self.cache.get_match(SOURCE, GAME, match_id)
        now = self.clock.now()
        if entry is not None and not refresh and (entry.expires_at > now or offline):
            stale = entry.expires_at <= now
            reason = "offline_stale_cache" if stale else None
            self.decisions.append(AcquisitionDecision("match", str(match_id), "cache", stale, reason))
            return entry.value
        if offline:
            raise AcquisitionIncompleteError("offline_cache_miss")
        try:
            match = extract_match(self.http.get_match(match_id), match_id)
        except SourceContractError as error:
            raise AcquisitionIncompleteError("match_contract_changed") from error
        except SourceRequestError as error:
            raise AcquisitionIncompleteError("match_request_failed") from error
        self.cache.put_match(
            match,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.settings.match_ttl_seconds),
        )
        self.decisions.append(AcquisitionDecision("match", str(match_id), "source", False, None))
        return match

    def candidate_matches(self, player_id: int, *, refresh: bool, offline: bool) -> MatchListDiscovery:
        entry = self.cache.get_match_page(SOURCE, GAME, player_id, 1)
        now = self.clock.now()
        if entry is not None and not refresh and (entry.expires_at > now or offline):
            stale = entry.expires_at <= now
            reason = "offline_stale_cache" if stale else None
            self.decisions.append(AcquisitionDecision("match_list", str(player_id), "cache", stale, reason))
            return MatchListDiscovery(entry.match_ids, entry.complete, 0, () if reason is None else (reason,), True, stale)
        if offline:
            return MatchListDiscovery((), False, 0, ("offline_cache_miss",), True, False)
        discovery = self.browser.player_matches(player_id, self.settings.caps)
        self.cache.put_match_page(
            player_id,
            1,
            discovery.match_ids,
            complete=discovery.complete,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.settings.match_list_ttl_seconds),
        )
        self.decisions.append(AcquisitionDecision("match_list", str(player_id), "source", False, None))
        return discovery
