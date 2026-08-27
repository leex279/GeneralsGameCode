"""Validated standalone configuration for the Strata resolver."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

_DAY = 24 * 60 * 60
_MAX_TTL = 90 * _DAY


class ConfigurationError(ValueError):
    """Raised before adapters are created when resolver settings are unsafe."""


@dataclass(frozen=True, slots=True)
class AcquisitionCaps:
    """Hard safety ceilings for source discovery and replay downloads."""

    player_search_pages: int = 100
    candidate_ids: int = 10_000
    player_match_pages: int = 200
    replay_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        _bounded("max_player_search_pages", self.player_search_pages, 1, 100)
        _bounded("max_candidate_ids", self.candidate_ids, 1, 10_000)
        _bounded("max_player_match_pages", self.player_match_pages, 1, 200)
        _bounded("max_replay_bytes", self.replay_bytes, 1, 20 * 1024 * 1024)


def _bounded(name: str, value: float, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")


def _integer(name: str, value: object) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be an integer") from None
    return parsed


def _number(name: str, value: object) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be numeric") from None
    return parsed


def _source_value(
    values: Mapping[str, object], environment: Mapping[str, str], name: str, environment_name: str, default: object
) -> object:
    explicit = values.get(name)
    if explicit is not None:
        return explicit
    return environment.get(environment_name, default)


@dataclass(frozen=True, slots=True)
class ResolverSettings:
    """All validated local and network boundaries for one resolver run."""

    cache_path: Path
    browser: str = "chromium"
    timeout_seconds: float = 20
    search_ttl_seconds: int = _DAY
    profile_ttl_seconds: int = 7 * _DAY
    match_list_ttl_seconds: int = _DAY
    match_ttl_seconds: int = 30 * _DAY
    negative_ttl_seconds: int = 60 * 60
    http_concurrency: int = 2
    host_interval_seconds: float = 0.5
    max_attempts: int = 3
    max_retry_after_seconds: int = 120
    circuit_breaker_failures: int = 5
    html_bytes: int = 2 * 1024 * 1024
    caps: AcquisitionCaps = AcquisitionCaps()

    def __post_init__(self) -> None:
        if not self.cache_path.is_absolute():
            raise ConfigurationError("cache_path must be absolute")
        if self.browser not in {"chromium", "chrome"}:
            raise ConfigurationError("browser must be chromium or chrome")
        _bounded("timeout_seconds", self.timeout_seconds, 1, 120)
        for name in (
            "search_ttl_seconds",
            "profile_ttl_seconds",
            "match_list_ttl_seconds",
            "match_ttl_seconds",
            "negative_ttl_seconds",
        ):
            _bounded(name, getattr(self, name), 60, _MAX_TTL)
        _bounded("http_concurrency", self.http_concurrency, 1, 2)
        _bounded("host_interval_seconds", self.host_interval_seconds, 0.5, 60)
        _bounded("max_attempts", self.max_attempts, 1, 3)
        _bounded("max_retry_after_seconds", self.max_retry_after_seconds, 1, 120)
        _bounded("circuit_breaker_failures", self.circuit_breaker_failures, 1, 20)
        _bounded("html_bytes", self.html_bytes, 1024, 2 * 1024 * 1024)

    @property
    def user_agent(self) -> str:
        return "generals-strata-resolver/0.1.0 (+https://github.com/TheSuperHackers/GeneralsGameCode)"

    @classmethod
    def from_sources(cls, values: Mapping[str, object], environment: Mapping[str, str]) -> ResolverSettings:
        """Apply command/composition values, then supplied environment, then defaults."""
        default_cache = PlatformDirs("generals-strata-resolver", "TheSuperHackers").user_data_path / "resolver.sqlite3"
        cache_value = _source_value(values, environment, "cache_path", "STRATA_RESOLVER_CACHE", default_cache)
        cache_path = cache_value if isinstance(cache_value, Path) else Path(str(cache_value))
        caps = AcquisitionCaps(
            player_search_pages=_integer(
                "max_player_search_pages",
                _source_value(
                    values,
                    environment,
                    "max_player_search_pages",
                    "STRATA_RESOLVER_MAX_PLAYER_SEARCH_PAGES",
                    100,
                ),
            ),
            candidate_ids=_integer(
                "max_candidate_ids",
                _source_value(values, environment, "max_candidate_ids", "STRATA_RESOLVER_MAX_CANDIDATE_IDS", 10_000),
            ),
            player_match_pages=_integer(
                "max_player_match_pages",
                _source_value(
                    values,
                    environment,
                    "max_player_match_pages",
                    "STRATA_RESOLVER_MAX_PLAYER_MATCH_PAGES",
                    200,
                ),
            ),
            replay_bytes=_integer(
                "max_replay_bytes",
                _source_value(
                    values,
                    environment,
                    "max_replay_bytes",
                    "STRATA_RESOLVER_MAX_REPLAY_BYTES",
                    20 * 1024 * 1024,
                ),
            ),
        )
        # TheSuperHackers @feature Leex 27/08/2026 Validate crawl limits before opening a browser, network client, or database.
        return cls(
            cache_path=cache_path,
            browser=str(_source_value(values, environment, "browser", "STRATA_RESOLVER_BROWSER", "chromium")),
            timeout_seconds=_number(
                "timeout_seconds",
                _source_value(values, environment, "timeout_seconds", "STRATA_RESOLVER_TIMEOUT_SECONDS", 20),
            ),
            search_ttl_seconds=_integer(
                "search_ttl_seconds",
                _source_value(values, environment, "search_ttl_seconds", "STRATA_RESOLVER_SEARCH_TTL_SECONDS", _DAY),
            ),
            profile_ttl_seconds=_integer(
                "profile_ttl_seconds",
                _source_value(values, environment, "profile_ttl_seconds", "STRATA_RESOLVER_PROFILE_TTL_SECONDS", 7 * _DAY),
            ),
            match_list_ttl_seconds=_integer(
                "match_list_ttl_seconds",
                _source_value(values, environment, "match_list_ttl_seconds", "STRATA_RESOLVER_MATCH_LIST_TTL_SECONDS", _DAY),
            ),
            match_ttl_seconds=_integer(
                "match_ttl_seconds",
                _source_value(values, environment, "match_ttl_seconds", "STRATA_RESOLVER_MATCH_TTL_SECONDS", 30 * _DAY),
            ),
            negative_ttl_seconds=_integer(
                "negative_ttl_seconds",
                _source_value(values, environment, "negative_ttl_seconds", "STRATA_RESOLVER_NEGATIVE_TTL_SECONDS", 3600),
            ),
            http_concurrency=_integer(
                "http_concurrency",
                _source_value(values, environment, "http_concurrency", "STRATA_RESOLVER_HTTP_CONCURRENCY", 2),
            ),
            host_interval_seconds=_number(
                "host_interval_seconds",
                _source_value(values, environment, "host_interval_seconds", "STRATA_RESOLVER_HOST_INTERVAL_SECONDS", 0.5),
            ),
            max_attempts=_integer(
                "max_attempts", _source_value(values, environment, "max_attempts", "STRATA_RESOLVER_MAX_ATTEMPTS", 3)
            ),
            max_retry_after_seconds=_integer(
                "max_retry_after_seconds",
                _source_value(
                    values,
                    environment,
                    "max_retry_after_seconds",
                    "STRATA_RESOLVER_MAX_RETRY_AFTER_SECONDS",
                    120,
                ),
            ),
            circuit_breaker_failures=_integer(
                "circuit_breaker_failures",
                _source_value(
                    values,
                    environment,
                    "circuit_breaker_failures",
                    "STRATA_RESOLVER_CIRCUIT_BREAKER_FAILURES",
                    5,
                ),
            ),
            caps=caps,
        )
