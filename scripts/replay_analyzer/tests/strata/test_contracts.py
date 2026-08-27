from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from generals_replay_analyzer.strata.contracts import (
    AliasRecord,
    Confidence,
    EvidenceRecord,
    MatchKind,
    NameResolution,
    PlayerCandidate,
    QueryName,
    ResolutionStatus,
)

NOW = datetime(2026, 8, 27, 20, 32, 43, tzinfo=UTC)


def _candidate(player_id: int = 17945) -> PlayerCandidate:
    return PlayerCandidate(
        source="strata.gamereplays.org",
        player_id=player_id,
        profile_url=f"https://strata.gamereplays.org/zh/player/{player_id}",
        most_known_name="-DoMiNaToR-",
        matched_alias="fish",
        match_kind=MatchKind.EXACT,
        alias_occurrence_count=8,
        source_rank=15,
        replay_context_score=0,
        selection_reason="highest occurrence count among exact case-sensitive alias matches",
        evidence=(EvidenceRecord("alias_raw", "fish", "fish", True, "name"),),
    )


def test_public_enums_have_stable_wire_values() -> None:
    assert MatchKind.EXACT.value == "exact"
    assert ResolutionStatus.INCOMPLETE.value == "incomplete"
    assert Confidence.MEDIUM.value == "medium"


def test_alias_record_serializes_comparison_values_without_losing_raw_alias() -> None:
    alias = AliasRecord(
        source="strata.gamereplays.org",
        player_id=17945,
        profile_url="https://strata.gamereplays.org/zh/player/17945",
        most_known_name="-DoMiNaToR-",
        alias_raw="Fi\u0301sh",
        alias_nfc="F\u00edsh",
        alias_casefold="f\u00edsh",
        occurrence_count=8,
        source_rank=15,
    )

    assert alias.to_dict()["alias_raw"] == "Fi\u0301sh"
    assert alias.to_dict()["alias_nfc"] == "F\u00edsh"


def test_name_resolution_matches_the_stable_json_contract() -> None:
    selected = _candidate()
    resolution = NameResolution(
        query=QueryName(raw="fish", nfc="fish", casefold="fish"),
        status=ResolutionStatus.AMBIGUOUS,
        selected=selected,
        alternatives=(_candidate(6522),),
        case_insensitive_suggestions=(),
        fuzzy_suggestions=(),
        confidence=Confidence.MEDIUM,
        needs_replay_context=True,
        search_complete=True,
        checked_at=NOW,
        reason_codes=(),
    )

    document = resolution.to_dict()
    assert document["schema_version"] == "strata-name-resolution-v1"
    assert document["query_name"] == "fish"
    assert document["query_name_nfc"] == "fish"
    assert document["status"] == "ambiguous"
    assert document["selected"]["player_id"] == 17945  # type: ignore[index]
    assert document["alternatives"][0]["player_id"] == 6522  # type: ignore[index]
    assert document["checked_at"] == "2026-08-27T20:32:43Z"


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(hours=2)])
def test_contracts_serialize_aware_times_as_utc(offset: timedelta) -> None:
    checked_at = datetime(2026, 8, 27, 22, 32, 43, tzinfo=UTC) - offset
    if offset:
        checked_at = checked_at.replace(tzinfo=UTC) + offset
    resolution = NameResolution(
        query=QueryName("fish", "fish", "fish"),
        status=ResolutionStatus.NOT_FOUND,
        selected=None,
        alternatives=(),
        case_insensitive_suggestions=(),
        fuzzy_suggestions=(),
        confidence=Confidence.NONE,
        needs_replay_context=False,
        search_complete=True,
        checked_at=checked_at,
        reason_codes=(),
    )

    assert str(resolution.to_dict()["checked_at"]).endswith("Z")


def test_contracts_reject_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        NameResolution(
            query=QueryName("fish", "fish", "fish"),
            status=ResolutionStatus.NOT_FOUND,
            selected=None,
            alternatives=(),
            case_insensitive_suggestions=(),
            fuzzy_suggestions=(),
            confidence=Confidence.NONE,
            needs_replay_context=False,
            search_complete=True,
            checked_at=NOW.replace(tzinfo=None),
            reason_codes=(),
        )
