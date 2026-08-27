"""Stable public contracts for Strata player identity resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class MatchKind(StrEnum):
    """The comparison tier that connected a replay name to an alias."""

    EXACT = "exact"
    CASE_INSENSITIVE = "case_insensitive"
    FUZZY = "fuzzy"


class ResolutionStatus(StrEnum):
    """Stable resolver outcomes used by the CLI and library API."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    INCOMPLETE = "incomplete"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


class Confidence(StrEnum):
    """Strength of the evidence supporting a selected identity."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class QueryName:
    """A replay name preserved in raw, NFC, and case-folded forms."""

    raw: str
    nfc: str
    casefold: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"raw": self.raw, "nfc": self.nfc, "casefold": self.casefold}


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One independently inspectable fact used during resolution."""

    label: str
    observed: JsonValue
    expected: JsonValue
    agreement: bool | None
    weight_class: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "label": self.label,
            "observed": self.observed,
            "expected": self.expected,
            "agreement": self.agreement,
            "weight_class": self.weight_class,
        }


@dataclass(frozen=True, slots=True)
class AliasRecord:
    """One exact Known Names entry retained with its Strata source order."""

    source: str
    player_id: int
    profile_url: str
    most_known_name: str
    alias_raw: str
    alias_nfc: str
    alias_casefold: str
    occurrence_count: int
    source_rank: int
    replay_context_score: int = 0

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source": self.source,
            "player_id": self.player_id,
            "profile_url": self.profile_url,
            "most_known_name": self.most_known_name,
            "alias_raw": self.alias_raw,
            "alias_nfc": self.alias_nfc,
            "alias_casefold": self.alias_casefold,
            "occurrence_count": self.occurrence_count,
            "source_rank": self.source_rank,
            "replay_context_score": self.replay_context_score,
        }


@dataclass(frozen=True, slots=True)
class PlayerCandidate:
    """A Strata player candidate and the alias evidence that produced it."""

    source: str
    player_id: int
    profile_url: str
    most_known_name: str
    matched_alias: str
    match_kind: MatchKind
    alias_occurrence_count: int
    source_rank: int
    replay_context_score: int = 0
    selection_reason: str | None = None
    evidence: tuple[EvidenceRecord, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        document: dict[str, JsonValue] = {
            "source": self.source,
            "player_id": self.player_id,
            "profile_url": self.profile_url,
            "most_known_name": self.most_known_name,
            "matched_alias": self.matched_alias,
            "match_kind": self.match_kind.value,
            "alias_occurrence_count": self.alias_occurrence_count,
        }
        if self.selection_reason is not None:
            document["selection_reason"] = self.selection_reason
        if self.evidence:
            document["evidence"] = [item.to_dict() for item in self.evidence]
        return document


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class NameResolution:
    """The complete name-only result, including all non-selected candidates."""

    query: QueryName
    status: ResolutionStatus
    selected: PlayerCandidate | None
    alternatives: tuple[PlayerCandidate, ...]
    case_insensitive_suggestions: tuple[PlayerCandidate, ...]
    fuzzy_suggestions: tuple[PlayerCandidate, ...]
    confidence: Confidence
    needs_replay_context: bool
    search_complete: bool
    checked_at: datetime
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _utc_timestamp(self.checked_at)

    def to_dict(self) -> dict[str, JsonValue]:
        # TheSuperHackers @feature Leex 27/08/2026 Preserve ambiguity and comparison tiers in the public identity result.
        return {
            "schema_version": "strata-name-resolution-v1",
            "query_name": self.query.raw,
            "query_name_nfc": self.query.nfc,
            "status": self.status.value,
            "selected": None if self.selected is None else self.selected.to_dict(),
            "alternatives": [item.to_dict() for item in self.alternatives],
            "case_insensitive_suggestions": [item.to_dict() for item in self.case_insensitive_suggestions],
            "fuzzy_suggestions": [item.to_dict() for item in self.fuzzy_suggestions],
            "confidence": self.confidence.value,
            "needs_replay_context": self.needs_replay_context,
            "search_complete": self.search_complete,
            "checked_at": _utc_timestamp(self.checked_at),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class ReplayParticipant:
    """One replay slot with local identifiers and exact embedded name values."""

    slot_index: int
    player_index: int | None
    kind: str
    name_raw: str | None
    name_nfc: str | None
    name_casefold: str | None
    player_template: int | None
    team: int | None
    color: int | None
    start_position: int | None
    ai_difficulty: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "slot_index": self.slot_index,
            "player_index": self.player_index,
            "kind": self.kind,
            "name_raw": self.name_raw,
            "name_nfc": self.name_nfc,
            "name_casefold": self.name_casefold,
            "player_template": self.player_template,
            "team": self.team,
            "color": self.color,
            "start_position": self.start_position,
            "ai_difficulty": self.ai_difficulty,
        }


@dataclass(frozen=True, slots=True)
class ReplayFingerprints:
    """Exact and semantic replay fingerprints kept as separate evidence."""

    raw_replay_sha256: str
    command_stream_sha256: str | None
    match_signature_sha256: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "raw_replay_sha256": self.raw_replay_sha256,
            "command_stream_sha256": self.command_stream_sha256,
            "match_signature_sha256": self.match_signature_sha256,
        }


@dataclass(frozen=True, slots=True)
class ReplayContext:
    """Strict-parser replay metadata used for Strata match correlation."""

    path: Path
    replay_sha256: str
    command_stream_sha256: str | None
    match_signature_sha256: str
    hinted_strata_match_id: int | None
    version_string: str
    version_number: int
    start_time: int
    end_time: int
    frame_count: int
    header_duration_seconds: int
    logic_duration_seconds: float | None
    map_path: str
    map_crc: int
    map_size: int
    seed: int
    starting_cash: int | None
    local_player_index: int
    slots: tuple[ReplayParticipant, ...]
    completion_status: str

    @property
    def human_players(self) -> tuple[ReplayParticipant, ...]:
        return tuple(slot for slot in self.slots if slot.kind == "human")

    @property
    def fingerprints(self) -> ReplayFingerprints:
        return ReplayFingerprints(
            raw_replay_sha256=self.replay_sha256,
            command_stream_sha256=self.command_stream_sha256,
            match_signature_sha256=self.match_signature_sha256,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": "strata-replay-context-v1",
            "source_filename": self.path.name,
            "fingerprints": self.fingerprints.to_dict(),
            "hinted_strata_match_id": self.hinted_strata_match_id,
            "version_string": self.version_string,
            "version_number": self.version_number,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "frame_count": self.frame_count,
            "header_duration_seconds": self.header_duration_seconds,
            "logic_duration_seconds": self.logic_duration_seconds,
            "map_path": self.map_path,
            "map_crc": self.map_crc,
            "map_size": self.map_size,
            "seed": self.seed,
            "starting_cash": self.starting_cash,
            "local_player_index": self.local_player_index,
            "slots": [slot.to_dict() for slot in self.slots],
            "completion_status": self.completion_status,
        }


@dataclass(frozen=True, slots=True)
class ProfileAliasDocument:
    """One Known Names chip extracted from a server-rendered profile page."""

    name_raw: str
    name_nfc: str
    name_casefold: str
    occurrence_count: int
    source_rank: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name_raw": self.name_raw,
            "name_nfc": self.name_nfc,
            "name_casefold": self.name_casefold,
            "occurrence_count": self.occurrence_count,
            "source_rank": self.source_rank,
        }


@dataclass(frozen=True, slots=True)
class ProfileDocument:
    """Validated player profile facts extracted from Strata HTML."""

    player_id: int
    profile_url: str
    most_known_name: str
    aliases: tuple[ProfileAliasDocument, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "player_id": self.player_id,
            "profile_url": self.profile_url,
            "most_known_name": self.most_known_name,
            "aliases": [alias.to_dict() for alias in self.aliases],
        }


@dataclass(frozen=True, slots=True)
class MatchParticipantDocument:
    """One player row extracted from a Strata match page."""

    source_rank: int
    player_id: int
    displayed_name: str
    faction: str
    result: str
    replay_url: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source_rank": self.source_rank,
            "player_id": self.player_id,
            "displayed_name": self.displayed_name,
            "faction": self.faction,
            "result": self.result,
            "replay_url": self.replay_url,
        }


@dataclass(frozen=True, slots=True)
class MatchDocument:
    """Validated shared match context extracted from a Strata match page."""

    match_id: int
    match_url: str
    played_start_utc: datetime
    played_end_utc: datetime
    map_id: int | None
    map_name: str
    match_type: str
    duration_seconds: int
    starting_cash: int | None
    game_version: str | None
    data_pack: str | None
    participants: tuple[MatchParticipantDocument, ...]

    def __post_init__(self) -> None:
        _utc_timestamp(self.played_start_utc)
        _utc_timestamp(self.played_end_utc)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "match_id": self.match_id,
            "match_url": self.match_url,
            "played_start_utc": _utc_timestamp(self.played_start_utc),
            "played_end_utc": _utc_timestamp(self.played_end_utc),
            "map_id": self.map_id,
            "map_name": self.map_name,
            "match_type": self.match_type,
            "duration_seconds": self.duration_seconds,
            "starting_cash": self.starting_cash,
            "game_version": self.game_version,
            "data_pack": self.data_pack,
            "participants": [participant.to_dict() for participant in self.participants],
        }


@dataclass(frozen=True, slots=True)
class DownloadedReplayEvidence:
    """Fingerprints derived by parsing one untrusted downloaded replay."""

    replay_url: str
    replay_sha256: str
    command_stream_sha256: str | None
    match_signature_sha256: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "replay_url": self.replay_url,
            "replay_sha256": self.replay_sha256,
            "command_stream_sha256": self.command_stream_sha256,
            "match_signature_sha256": self.match_signature_sha256,
        }


@dataclass(frozen=True, slots=True)
class MatchFact:
    """One labelled replay-versus-Strata comparison fact."""

    label: str
    observed: JsonValue
    expected: JsonValue
    agreement: bool | None
    weight_class: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "label": self.label,
            "observed": self.observed,
            "expected": self.expected,
            "agreement": self.agreement,
            "weight_class": self.weight_class,
        }


@dataclass(frozen=True, slots=True)
class MatchEvidence:
    """All facts and the unique slot assignment for one candidate match."""

    match_id: int
    match_url: str
    viable: bool
    confidence: Confidence
    facts: tuple[MatchFact, ...]
    assignments: tuple[tuple[int, int], ...]
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "match_id": self.match_id,
            "match_url": self.match_url,
            "viable": self.viable,
            "confidence": self.confidence.value,
            "facts": [fact.to_dict() for fact in self.facts],
            "assignments": [
                {"slot_index": slot_index, "player_id": player_id}
                for slot_index, player_id in self.assignments
            ],
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class MatchResolution:
    """Selection across every discovered shared Strata match candidate."""

    status: ResolutionStatus
    confidence: Confidence
    selected_match_id: int | None
    selected_match_url: str | None
    alternatives: tuple[int, ...]
    evidence: tuple[MatchEvidence, ...]
    assignments: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "status": self.status.value,
            "confidence": self.confidence.value,
            "selected_match_id": self.selected_match_id,
            "selected_match_url": self.selected_match_url,
            "alternatives": list(self.alternatives),
            "evidence": [item.to_dict() for item in self.evidence],
            "assignments": [
                {"slot_index": slot_index, "player_id": player_id}
                for slot_index, player_id in self.assignments
            ],
        }
