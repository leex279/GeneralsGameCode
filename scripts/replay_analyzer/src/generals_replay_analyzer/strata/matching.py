"""Pure deterministic alias partitioning and replay-match evidence policy."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from .contracts import (
    AliasRecord,
    Confidence,
    DownloadedReplayEvidence,
    JsonValue,
    MatchDocument,
    MatchEvidence,
    MatchFact,
    MatchKind,
    MatchResolution,
    NameResolution,
    PlayerCandidate,
    QueryName,
    ReplayContext,
    ResolutionStatus,
)

_MATCH_TYPE = re.compile(r"^(\d+)v(\d+)$", re.IGNORECASE)
_FACTIONS: dict[int, frozenset[str]] = {
    4: frozenset({"gla"}),
    7: frozenset({"usa airforce", "airforce general", "usa air force"}),
}


@dataclass(frozen=True, slots=True)
class AliasPartitions:
    exact: tuple[AliasRecord, ...]
    case_insensitive: tuple[AliasRecord, ...]
    fuzzy: tuple[AliasRecord, ...]


def _deduplicate(aliases: Sequence[AliasRecord]) -> tuple[AliasRecord, ...]:
    result: list[AliasRecord] = []
    seen: set[tuple[str, int, str]] = set()
    for alias in aliases:
        key = (alias.source, alias.player_id, alias.alias_raw)
        if key not in seen:
            seen.add(key)
            result.append(alias)
    return tuple(result)


def partition_aliases(query: QueryName, aliases: Sequence[AliasRecord], include_fuzzy: bool) -> AliasPartitions:
    """Partition without silently promoting case-folded or fuzzy aliases."""
    unique = _deduplicate(aliases)
    exact = tuple(alias for alias in unique if alias.alias_raw == query.raw)
    case_insensitive = tuple(
        alias for alias in unique if alias.alias_raw != query.raw and alias.alias_casefold == query.casefold
    )
    excluded = {id(alias) for alias in exact + case_insensitive}
    fuzzy: tuple[AliasRecord, ...] = ()
    if include_fuzzy:
        fuzzy = tuple(
            alias
            for alias in unique
            if id(alias) not in excluded
            and SequenceMatcher(None, query.casefold, alias.alias_casefold).ratio() >= 0.75
        )
    return AliasPartitions(exact, case_insensitive, fuzzy)


def _candidate(alias: AliasRecord, kind: MatchKind) -> PlayerCandidate:
    return PlayerCandidate(
        source=alias.source,
        player_id=alias.player_id,
        profile_url=alias.profile_url,
        most_known_name=alias.most_known_name,
        matched_alias=alias.alias_raw,
        match_kind=kind,
        alias_occurrence_count=alias.occurrence_count,
        source_rank=alias.source_rank,
        replay_context_score=alias.replay_context_score,
    )


def _rank(candidate: PlayerCandidate) -> tuple[int, int, int, int]:
    return (
        -candidate.replay_context_score,
        -candidate.alias_occurrence_count,
        candidate.source_rank,
        candidate.player_id,
    )


# TheSuperHackers @feature Leex 27/08/2026 Keep multiple exact owners ambiguous while exposing a deterministic best candidate.
def resolve_name(
    query: QueryName,
    aliases: Sequence[AliasRecord],
    search_complete: bool,
    checked_at: datetime,
    include_fuzzy: bool = False,
) -> NameResolution:
    partitions = partition_aliases(query, aliases, include_fuzzy)
    exact = sorted((_candidate(alias, MatchKind.EXACT) for alias in partitions.exact), key=_rank)
    casefold = sorted(
        (_candidate(alias, MatchKind.CASE_INSENSITIVE) for alias in partitions.case_insensitive), key=_rank
    )
    fuzzy = sorted((_candidate(alias, MatchKind.FUZZY) for alias in partitions.fuzzy), key=_rank)
    selected: PlayerCandidate | None = None
    alternatives: tuple[PlayerCandidate, ...] = ()
    if exact:
        reason = "highest occurrence count among exact case-sensitive alias matches"
        selected = replace(exact[0], selection_reason=reason)
        alternatives = tuple(exact[1:])
    if not search_complete:
        status = ResolutionStatus.INCOMPLETE
        confidence = Confidence.LOW if exact or casefold or fuzzy else Confidence.NONE
        reason_codes: tuple[str, ...] = ("search_incomplete",)
    elif len(exact) == 1:
        status = ResolutionStatus.RESOLVED
        confidence = Confidence.MEDIUM
        reason_codes = ()
    elif len(exact) > 1:
        status = ResolutionStatus.AMBIGUOUS
        confidence = Confidence.MEDIUM
        reason_codes = ()
    elif casefold or fuzzy:
        status = ResolutionStatus.AMBIGUOUS
        confidence = Confidence.LOW
        reason_codes = ("no_exact_case_sensitive_alias",)
    else:
        status = ResolutionStatus.NOT_FOUND
        confidence = Confidence.NONE
        reason_codes = ()
    return NameResolution(
        query=query,
        status=status,
        selected=selected,
        alternatives=alternatives,
        case_insensitive_suggestions=tuple(casefold),
        fuzzy_suggestions=tuple(fuzzy),
        confidence=confidence,
        needs_replay_context=len(exact) > 1 or bool(casefold) or bool(fuzzy),
        search_complete=search_complete,
        checked_at=checked_at,
        reason_codes=reason_codes,
    )


def _fact(
    label: str,
    observed: JsonValue,
    expected: JsonValue,
    agreement: bool | None,
    weight: str,
) -> MatchFact:
    return MatchFact(label, observed, expected, agreement, weight)


def _json_strings(values: Sequence[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(values)
    return result


def _map_key(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].strip().casefold()


def _match_type_count(value: str) -> int | None:
    matched = _MATCH_TYPE.fullmatch(value.strip())
    if matched is not None:
        return int(matched.group(1)) + int(matched.group(2))
    numbers = re.findall(r"\d+", value)
    if "ffa" in value.casefold() and numbers:
        return int(numbers[0])
    return None


def _assignments(
    context: ReplayContext,
    match: MatchDocument,
    slot_aliases: Mapping[int, Sequence[AliasRecord]],
) -> list[tuple[tuple[int, int], ...]]:
    players = context.human_players
    options: list[tuple[int, tuple[int, ...]]] = []
    for slot in players:
        allowed = {
            participant.player_id
            for participant in match.participants
            if participant.displayed_name == slot.name_raw
        }
        for alias in slot_aliases.get(slot.slot_index, ()):
            if alias.alias_raw == slot.name_raw:
                allowed.update(
                    participant.player_id
                    for participant in match.participants
                    if participant.player_id == alias.player_id
                )
        options.append((slot.slot_index, tuple(sorted(allowed))))
    complete: list[tuple[tuple[int, int], ...]] = []

    def visit(index: int, used: set[int], current: list[tuple[int, int]]) -> None:
        if len(complete) > 1:
            return
        if index == len(options):
            complete.append(tuple(current))
            return
        slot_index, candidates = options[index]
        for player_id in candidates:
            if player_id in used:
                continue
            used.add(player_id)
            current.append((slot_index, player_id))
            visit(index + 1, used, current)
            current.pop()
            used.remove(player_id)

    visit(0, set(), [])
    return complete


def _faction_agreement(context: ReplayContext, match: MatchDocument, assignment: tuple[tuple[int, int], ...]) -> bool | None:
    by_slot = {slot.slot_index: slot for slot in context.human_players}
    by_player = {participant.player_id: participant for participant in match.participants}
    compared = 0
    for slot_index, player_id in assignment:
        template = by_slot[slot_index].player_template
        expected = None if template is None else _FACTIONS.get(template)
        if expected is None:
            continue
        compared += 1
        if by_player[player_id].faction.strip().casefold() not in expected:
            return False
    return True if compared else None


# TheSuperHackers @feature Leex 27/08/2026 Resolve external IDs only from one shared match with inspectable independent facts.
def evaluate_match(
    context: ReplayContext,
    match: MatchDocument,
    downloaded: Sequence[DownloadedReplayEvidence],
    *,
    slot_aliases: Mapping[int, Sequence[AliasRecord]] | None = None,
) -> MatchEvidence:
    aliases = {} if slot_aliases is None else slot_aliases
    facts: list[MatchFact] = []
    raw_hashes = [item.replay_sha256 for item in downloaded]
    command_hashes = [item.command_stream_sha256 for item in downloaded if item.command_stream_sha256 is not None]
    signature_hashes = [item.match_signature_sha256 for item in downloaded if item.match_signature_sha256 is not None]
    raw_agreement = None if not raw_hashes else context.replay_sha256 in raw_hashes
    command_agreement = (
        None
        if context.command_stream_sha256 is None or not command_hashes
        else context.command_stream_sha256 in command_hashes
    )
    signature_agreement = (
        None if not signature_hashes else context.match_signature_sha256 in signature_hashes
    )
    facts.append(
        _fact("raw_replay_sha256", _json_strings(raw_hashes), context.replay_sha256, raw_agreement, "high")
    )
    facts.append(
        _fact(
            "command_stream_sha256",
            _json_strings(command_hashes),
            context.command_stream_sha256,
            command_agreement,
            "high",
        )
    )
    facts.append(
        _fact(
            "match_signature_sha256",
            _json_strings(signature_hashes),
            context.match_signature_sha256,
            signature_agreement,
            "medium",
        )
    )

    replay_start = datetime.fromtimestamp(context.start_time, tz=match.played_start_utc.tzinfo)
    timestamp_agreement = (
        match.played_start_utc - timedelta(seconds=60)
        <= replay_start
        <= match.played_end_utc + timedelta(seconds=60)
    )
    participant_count = len(context.human_players) == len(match.participants)
    replay_names = sorted(slot.name_raw or "" for slot in context.human_players)
    match_names = sorted(participant.displayed_name for participant in match.participants)
    exact_names = replay_names == match_names
    map_agreement = _map_key(context.map_path) == _map_key(match.map_name)
    match_type_count = _match_type_count(match.match_type)
    match_type_agreement = match_type_count == len(context.human_players)
    duration_agreement = abs(context.header_duration_seconds - match.duration_seconds) <= 5
    assignment_options = _assignments(context, match, aliases) if participant_count else []
    assignment = assignment_options[0] if len(assignment_options) == 1 else ()
    assignment_agreement = len(assignment_options) == 1
    factions = _faction_agreement(context, match, assignment) if assignment else None

    facts.extend(
        (
            _fact(
                "timestamp",
                int(replay_start.timestamp()),
                int(match.played_start_utc.timestamp()),
                timestamp_agreement,
                "medium",
            ),
            _fact("participant_count", len(context.human_players), len(match.participants), participant_count, "high"),
            _fact(
                "participant_names",
                _json_strings(replay_names),
                _json_strings(match_names),
                exact_names,
                "medium",
            ),
            _fact("participant_assignment", len(assignment_options), 1, assignment_agreement, "high"),
            _fact("map", _map_key(context.map_path), _map_key(match.map_name), map_agreement, "high"),
            _fact("match_type", match_type_count, len(context.human_players), match_type_agreement, "high"),
            _fact("duration", context.header_duration_seconds, match.duration_seconds, duration_agreement, "medium"),
            _fact("factions", factions, True, factions, "medium"),
        )
    )
    contradictions = {
        "command_stream_sha256": command_agreement is False,
        "participant_count": not participant_count,
        "participant_assignment": not assignment_options,
        "map": not map_agreement,
        "match_type": not match_type_agreement,
        "duration": not duration_agreement,
        "timestamp": not timestamp_agreement,
        "factions": factions is False,
    }
    reasons = tuple(label + "_mismatch" for label, contradicted in contradictions.items() if contradicted)
    viable = not reasons
    high = viable and assignment_agreement and (raw_agreement is True or command_agreement is True)
    medium = viable and assignment_agreement and all(
        value is not False
        for value in (timestamp_agreement, participant_count, map_agreement, match_type_agreement, duration_agreement, factions)
    )
    confidence = Confidence.HIGH if high else Confidence.MEDIUM if medium else Confidence.LOW if viable else Confidence.NONE
    return MatchEvidence(match.match_id, match.match_url, viable, confidence, tuple(facts), assignment, reasons)


def rank_match_evidence(evidence: Sequence[MatchEvidence]) -> MatchResolution:
    ordered = tuple(sorted(evidence, key=lambda item: item.match_id))
    resolvable = tuple(
        item
        for item in ordered
        if item.viable and item.assignments and item.confidence in {Confidence.HIGH, Confidence.MEDIUM}
    )
    if len(resolvable) == 1:
        selected = resolvable[0]
        return MatchResolution(
            ResolutionStatus.RESOLVED,
            selected.confidence,
            selected.match_id,
            selected.match_url,
            tuple(item.match_id for item in ordered if item.match_id != selected.match_id and item.viable),
            ordered,
            selected.assignments,
        )
    if resolvable:
        confidence = Confidence.HIGH if any(item.confidence is Confidence.HIGH for item in resolvable) else Confidence.MEDIUM
        return MatchResolution(
            ResolutionStatus.AMBIGUOUS,
            confidence,
            None,
            None,
            tuple(item.match_id for item in resolvable),
            ordered,
            (),
        )
    viable = tuple(item for item in ordered if item.viable)
    if viable:
        return MatchResolution(
            ResolutionStatus.AMBIGUOUS,
            Confidence.LOW,
            None,
            None,
            tuple(item.match_id for item in viable),
            ordered,
            (),
        )
    return MatchResolution(ResolutionStatus.NOT_FOUND, Confidence.NONE, None, None, (), ordered, ())
