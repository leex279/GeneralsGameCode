"""Standalone Strata replay-player identity resolution API."""

from .contracts import (
    AliasRecord,
    Confidence,
    EvidenceRecord,
    MatchKind,
    MatchDocument,
    MatchParticipantDocument,
    NameResolution,
    PlayerCandidate,
    QueryName,
    ProfileAliasDocument,
    ProfileDocument,
    ReplayContext,
    ReplayFingerprints,
    ReplayParticipant,
    ResolutionStatus,
)
from .extract import SourceContractError, extract_match, extract_profile
from .normalization import InvalidQueryNameError, normalize_query_name
from .replay_context import build_replay_context

__all__ = [
    "AliasRecord",
    "Confidence",
    "EvidenceRecord",
    "InvalidQueryNameError",
    "MatchKind",
    "MatchDocument",
    "MatchParticipantDocument",
    "NameResolution",
    "PlayerCandidate",
    "QueryName",
    "ProfileAliasDocument",
    "ProfileDocument",
    "ReplayContext",
    "ReplayFingerprints",
    "ReplayParticipant",
    "ResolutionStatus",
    "SourceContractError",
    "build_replay_context",
    "extract_match",
    "extract_profile",
    "normalize_query_name",
]
