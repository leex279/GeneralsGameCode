"""Standalone Strata replay-player identity resolution API."""

from .contracts import (
    AliasRecord,
    Confidence,
    EvidenceRecord,
    MatchKind,
    NameResolution,
    PlayerCandidate,
    QueryName,
    ReplayContext,
    ReplayFingerprints,
    ReplayParticipant,
    ResolutionStatus,
)
from .normalization import InvalidQueryNameError, normalize_query_name
from .replay_context import build_replay_context

__all__ = [
    "AliasRecord",
    "Confidence",
    "EvidenceRecord",
    "InvalidQueryNameError",
    "MatchKind",
    "NameResolution",
    "PlayerCandidate",
    "QueryName",
    "ReplayContext",
    "ReplayFingerprints",
    "ReplayParticipant",
    "ResolutionStatus",
    "build_replay_context",
    "normalize_query_name",
]
