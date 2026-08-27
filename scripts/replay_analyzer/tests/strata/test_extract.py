from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from generals_replay_analyzer.strata.extract import SourceContractError, extract_match, extract_profile

FIXTURES = Path("tests/fixtures/strata")


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_sanitized_fixture_manifest_pins_every_source_contract() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["captured_at"] == "2026-08-27"
    for entry in manifest["fixtures"]:
        data = (FIXTURES / entry["filename"]).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        assert hashlib.sha256(data).hexdigest().upper() == entry["sha256"]
        assert entry["terms"]


def test_profile_17945_uses_highest_count_and_preserves_known_name_order() -> None:
    profile = extract_profile(_html("player-17945.html"), 17945)

    assert profile.player_id == 17945
    assert profile.profile_url == "https://strata.gamereplays.org/zh/player/17945"
    assert profile.most_known_name == "-DoMiNaToR-"
    assert profile.aliases[0].name_raw == "-DoMiNaToR-"
    assert profile.aliases[0].occurrence_count == 252
    assert profile.aliases[15].name_raw == "fish"
    assert profile.aliases[15].occurrence_count == 8
    assert profile.aliases[15].source_rank == 15


def test_profile_accepts_grouped_counts_and_uses_highest_alias_instead_of_title() -> None:
    html = """<html><head><title>Current | Strata</title></head><body><section><p>Known Names</p>
    <div><span>Historical</span><span>1,646</span></div>
    <div><span>Current</span><span>169</span></div></section></body></html>"""

    profile = extract_profile(html, 10)

    assert profile.most_known_name == "Historical"
    assert profile.aliases[0].occurrence_count == 1646


def test_profile_aggregates_duplicate_raw_alias_chips_at_first_source_rank() -> None:
    html = """<html><head><title>DrGoldFish | Strata</title></head><body><section><p>Known Names</p>
    <div><span>DrGoldFish</span><span>453</span></div>
    <div><span>DrGoldFish</span><span>299</span></div>
    <div><span>Other</span><span>10</span></div></section></body></html>"""

    profile = extract_profile(html, 3116)

    assert [(alias.name_raw, alias.occurrence_count, alias.source_rank) for alias in profile.aliases] == [
        ("DrGoldFish", 752, 0),
        ("Other", 10, 1),
    ]


@pytest.mark.parametrize(
    ("player_id", "most_known_name", "matched_alias", "count"),
    [
        (6522, "StockFish'", "fish", 3),
        (30124, "fish", "fish", 2),
        (30427, "Fish", "Fish", 149),
        (52866, "Fish_3", "Fish", 8),
    ],
)
def test_fish_candidate_profiles_extract_relevant_aliases(
    player_id: int,
    most_known_name: str,
    matched_alias: str,
    count: int,
) -> None:
    profile = extract_profile(_html(f"player-{player_id}.html"), player_id)

    assert profile.most_known_name == most_known_name
    assert [(item.name_raw, item.occurrence_count) for item in profile.aliases if item.name_raw == matched_alias] == [
        (matched_alias, count)
    ]


def test_match_3133811_extracts_shared_context_and_participant_ids() -> None:
    match = extract_match(_html("match-3133811.html"), 3133811)

    assert match.match_id == 3133811
    assert match.match_url == "https://strata.gamereplays.org/zh/match/3133811"
    assert match.map_name == "[RANK] Sand Scorpion"
    assert match.match_type == "1v1"
    assert match.duration_seconds == 942
    assert match.played_start_utc == datetime(2026, 8, 1, 14, 21, tzinfo=UTC)
    assert match.played_end_utc == datetime(2026, 8, 1, 14, 36, tzinfo=UTC)
    assert match.starting_cash == 10_000
    assert [(item.player_id, item.displayed_name, item.faction, item.result) for item in match.participants] == [
        (27965, "leex279", "USA Airforce", "Won"),
        (102894, "FOX27", "GLA", "Lost"),
    ]
    assert all(item.replay_url and item.replay_url.endswith("_replay.rep") for item in match.participants)


def test_match_duration_accepts_site_total_minutes_above_fifty_nine() -> None:
    match = extract_match(_html("match-3133811.html").replace("15m 42s", "61m 42s"), 3133811)

    assert match.duration_seconds == 3702


@pytest.mark.parametrize(
    ("function", "html", "expected_id"),
    [
        (extract_profile, "<html><title>fish | Strata</title></html>", 17945),
        (extract_profile, "<html><title>-DoMiNaToR- | Strata</title><p>Known Names</p></html>", 17945),
        (extract_profile, "<html><title>-DoMiNaToR- | Strata</title><p>Known Names</p><div><span>fish</span><span>-1</span></div></html>", 17945),
        (extract_profile, "<html><title>other | Strata</title><p>Known Names</p><div><span>fish</span><span>1</span></div></html>", 17945),
        (extract_match, "<html><title>Match #99 | Strata</title></html>", 3133811),
        (extract_match, "<html><title>Match #3133811 | Strata</title></html>", 3133811),
    ],
)
def test_structurally_incomplete_pages_fail_closed(function: object, html: str, expected_id: int) -> None:
    with pytest.raises(SourceContractError) as raised:
        function(html, expected_id)  # type: ignore[operator]

    assert raised.value.code == "source_contract_changed"


def test_malformed_profile_fixture_fails_closed() -> None:
    with pytest.raises(SourceContractError, match="profile title"):
        extract_profile(_html("malformed-profile.html"), 17945)
