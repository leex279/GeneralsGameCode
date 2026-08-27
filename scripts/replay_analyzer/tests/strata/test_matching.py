from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from generals_replay_analyzer.strata.contracts import (
    AliasRecord,
    Confidence,
    DownloadedReplayEvidence,
    MatchDocument,
    ResolutionStatus,
)
from generals_replay_analyzer.strata.extract import extract_match, extract_profile
from generals_replay_analyzer.strata.matching import (
    evaluate_match,
    partition_aliases,
    rank_match_evidence,
    resolve_name,
)
from generals_replay_analyzer.strata.normalization import normalize_query_name
from generals_replay_analyzer.strata.replay_context import build_replay_context

FIXTURES = Path("tests/fixtures/strata")
REPLAY = Path("tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep")
NOW = datetime(2026, 8, 27, 20, 32, 43, tzinfo=UTC)


def _profile_aliases(player_id: int) -> tuple[AliasRecord, ...]:
    profile = extract_profile((FIXTURES / f"player-{player_id}.html").read_text(encoding="utf-8"), player_id)
    return tuple(
        AliasRecord(
            source="strata.gamereplays.org",
            player_id=profile.player_id,
            profile_url=profile.profile_url,
            most_known_name=profile.most_known_name,
            alias_raw=alias.name_raw,
            alias_nfc=alias.name_nfc,
            alias_casefold=alias.name_casefold,
            occurrence_count=alias.occurrence_count,
            source_rank=alias.source_rank,
        )
        for alias in profile.aliases
    )


FISH_ALIASES = tuple(
    alias
    for player_id in (17945, 6522, 30124, 30427, 52866)
    for alias in _profile_aliases(player_id)
)


def _match() -> MatchDocument:
    return extract_match((FIXTURES / "match-3133811.html").read_text(encoding="utf-8"), 3133811)


def test_multiple_exact_fish_owners_select_by_count_but_remain_ambiguous() -> None:
    result = resolve_name(normalize_query_name("fish"), FISH_ALIASES, search_complete=True, checked_at=NOW)

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.selected is not None and result.selected.player_id == 17945
    assert [item.player_id for item in result.alternatives] == [6522, 30124]
    assert [item.player_id for item in result.case_insensitive_suggestions] == [30427, 52866]
    assert result.confidence is Confidence.MEDIUM
    assert result.needs_replay_context is True


def test_case_insensitive_only_candidate_never_resolves_from_name_alone() -> None:
    result = resolve_name(
        normalize_query_name("FISH"),
        tuple(alias for alias in _profile_aliases(17945) if alias.alias_raw == "fish"),
        search_complete=True,
        checked_at=NOW,
    )

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.selected is None
    assert result.case_insensitive_suggestions[0].player_id == 17945


def test_one_exhausted_exact_candidate_resolves_medium() -> None:
    result = resolve_name(
        normalize_query_name("fish"),
        tuple(alias for alias in _profile_aliases(30124) if alias.alias_raw == "fish"),
        search_complete=True,
        checked_at=NOW,
    )

    assert result.status is ResolutionStatus.RESOLVED
    assert result.confidence is Confidence.MEDIUM
    assert result.selected is not None and result.selected.player_id == 30124


def test_incomplete_search_overrides_a_unique_name_selection() -> None:
    result = resolve_name(
        normalize_query_name("fish"),
        tuple(alias for alias in _profile_aliases(30124) if alias.alias_raw == "fish"),
        search_complete=False,
        checked_at=NOW,
    )

    assert result.status is ResolutionStatus.INCOMPLETE
    assert result.confidence is Confidence.LOW
    assert result.selected is not None


def test_partition_deduplicates_only_identical_source_player_raw_alias_and_fuzzy_never_selects() -> None:
    fish = next(alias for alias in _profile_aliases(30124) if alias.alias_raw == "fish")
    fuzzy = replace(
        fish,
        player_id=900,
        profile_url="https://strata.gamereplays.org/zh/player/900",
        alias_raw="fosh",
        alias_nfc="fosh",
        alias_casefold="fosh",
    )
    partitions = partition_aliases(normalize_query_name("fish"), (fish, fish, fuzzy), include_fuzzy=True)

    assert partitions.exact == (fish,)
    assert partitions.fuzzy == (fuzzy,)
    result = resolve_name(normalize_query_name("fesh"), (fuzzy,), True, NOW, include_fuzzy=True)
    assert result.selected is None


def test_exact_ties_use_source_rank_then_numeric_player_id() -> None:
    base = next(alias for alias in _profile_aliases(30124) if alias.alias_raw == "fish")
    tied = (
        replace(base, player_id=20, profile_url="https://strata.gamereplays.org/zh/player/20", source_rank=3),
        replace(base, player_id=10, profile_url="https://strata.gamereplays.org/zh/player/10", source_rank=3),
    )

    result = resolve_name(normalize_query_name("fish"), tied, True, NOW)

    assert result.selected is not None and result.selected.player_id == 10


def test_raw_or_command_fingerprint_confirms_high_confidence_match() -> None:
    context = build_replay_context(REPLAY)
    match = _match()
    downloaded = (
        DownloadedReplayEvidence(
            replay_url=match.participants[0].replay_url or "",
            replay_sha256="different",
            command_stream_sha256=context.command_stream_sha256,
            match_signature_sha256="different",
        ),
    )

    evidence = evaluate_match(context, match, downloaded)

    assert evidence.viable is True
    assert evidence.confidence is Confidence.HIGH
    assert evidence.assignments == ((0, 27965), (1, 102894))


def test_complete_metadata_without_download_resolves_medium() -> None:
    evidence = evaluate_match(build_replay_context(REPLAY), _match(), ())

    assert evidence.viable is True
    assert evidence.confidence is Confidence.MEDIUM
    assert {fact.label for fact in evidence.facts if fact.agreement is True} >= {
        "timestamp",
        "participant_count",
        "participant_assignment",
        "map",
        "match_type",
        "duration",
        "factions",
    }


def test_high_value_contradictions_eliminate_match() -> None:
    context = build_replay_context(REPLAY)
    match = _match()
    mismatch_download = (
        DownloadedReplayEvidence("https://example.invalid/replay.rep", "different", "different", "different"),
    )

    assert evaluate_match(context, replace(match, map_name="Tournament Desert"), ()).viable is False
    assert evaluate_match(context, replace(match, participants=match.participants[:1]), ()).viable is False
    assert evaluate_match(context, match, mismatch_download).viable is False


def test_duration_tolerance_accepts_five_seconds_and_rejects_six() -> None:
    context = build_replay_context(REPLAY)
    match = _match()

    assert evaluate_match(context, replace(match, duration_seconds=context.header_duration_seconds + 5), ()).viable
    assert not evaluate_match(context, replace(match, duration_seconds=context.header_duration_seconds + 6), ()).viable


def test_alias_candidates_can_form_unique_bipartite_assignment() -> None:
    context = build_replay_context(REPLAY)
    match = _match()
    renamed = replace(
        match,
        participants=(replace(match.participants[0], displayed_name="current-name"), match.participants[1]),
    )
    alias = AliasRecord(
        source="strata.gamereplays.org",
        player_id=27965,
        profile_url="https://strata.gamereplays.org/zh/player/27965",
        most_known_name="current-name",
        alias_raw="leex279",
        alias_nfc="leex279",
        alias_casefold="leex279",
        occurrence_count=3,
        source_rank=1,
    )

    evidence = evaluate_match(context, renamed, (), slot_aliases={0: (alias,)})

    assert evidence.assignments == ((0, 27965), (1, 102894))


def test_unique_shared_match_resolves_and_two_viable_matches_remain_ambiguous() -> None:
    evidence = evaluate_match(build_replay_context(REPLAY), _match(), ())
    unique = rank_match_evidence((evidence,))
    second = replace(evidence, match_id=3133812, match_url="https://strata.gamereplays.org/zh/match/3133812")
    ambiguous = rank_match_evidence((evidence, second))

    assert unique.status is ResolutionStatus.RESOLVED
    assert unique.selected_match_id == 3133811
    assert unique.assignments == ((0, 27965), (1, 102894))
    assert ambiguous.status is ResolutionStatus.AMBIGUOUS
    assert ambiguous.selected_match_id is None
