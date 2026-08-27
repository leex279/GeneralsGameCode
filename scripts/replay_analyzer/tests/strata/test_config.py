from __future__ import annotations

from pathlib import Path

import pytest

from generals_replay_analyzer.strata.config import ConfigurationError, ResolverSettings


def test_cli_values_override_environment_then_defaults(tmp_path: Path) -> None:
    settings = ResolverSettings.from_sources(
        {"cache_path": tmp_path / "cli.sqlite3", "timeout_seconds": 30, "browser": "chrome"},
        {
            "STRATA_RESOLVER_CACHE": str(tmp_path / "env.sqlite3"),
            "STRATA_RESOLVER_TIMEOUT_SECONDS": "25",
            "STRATA_RESOLVER_BROWSER": "chromium",
        },
    )

    assert settings.cache_path == tmp_path / "cli.sqlite3"
    assert settings.timeout_seconds == 30
    assert settings.browser == "chrome"
    assert settings.search_ttl_seconds == 24 * 60 * 60
    assert settings.profile_ttl_seconds == 7 * 24 * 60 * 60
    assert settings.match_ttl_seconds == 30 * 24 * 60 * 60
    assert settings.negative_ttl_seconds == 60 * 60
    assert settings.http_concurrency == 2
    assert settings.host_interval_seconds == 0.5
    assert settings.max_attempts == 3
    assert settings.caps.player_search_pages == 100
    assert settings.caps.candidate_ids == 10_000
    assert settings.caps.player_match_pages == 200
    assert settings.caps.replay_bytes == 20 * 1024 * 1024


def test_environment_values_apply_without_reading_global_environment(tmp_path: Path) -> None:
    settings = ResolverSettings.from_sources(
        {"cache_path": tmp_path / "resolver.sqlite3"},
        {"STRATA_RESOLVER_TIMEOUT_SECONDS": "12", "STRATA_RESOLVER_MAX_PLAYER_SEARCH_PAGES": "4"},
    )

    assert settings.timeout_seconds == 12
    assert settings.caps.player_search_pages == 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cache_path", Path("relative.sqlite3")),
        ("timeout_seconds", 0),
        ("timeout_seconds", 121),
        ("browser", "firefox"),
        ("search_ttl_seconds", 59),
        ("profile_ttl_seconds", 91 * 24 * 60 * 60),
        ("max_player_search_pages", 0),
        ("max_player_search_pages", 101),
        ("max_candidate_ids", 10_001),
        ("max_player_match_pages", 201),
        ("max_replay_bytes", 20 * 1024 * 1024 + 1),
    ],
)
def test_invalid_settings_fail_before_adapters_are_constructed(tmp_path: Path, field: str, value: object) -> None:
    values: dict[str, object] = {"cache_path": tmp_path / "resolver.sqlite3", field: value}

    with pytest.raises(ConfigurationError, match=field):
        ResolverSettings.from_sources(values, {})
