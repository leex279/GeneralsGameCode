"""Standalone Strata replay-player identity resolution API."""

from .contracts import (
    AliasRecord,
    Confidence,
    EvidenceRecord,
    MatchDocument,
    MatchKind,
    MatchParticipantDocument,
    NameResolution,
    PlayerCandidate,
    ProfileAliasDocument,
    ProfileDocument,
    QueryName,
    ReplayContext,
    ReplayFingerprints,
    ReplayParticipant,
    ReplayPlayerResolution,
    ReplayResolution,
    ResolutionStatus,
)
from .extract import SourceContractError, extract_match, extract_profile
from .normalization import InvalidQueryNameError, normalize_query_name
from .replay_context import build_replay_context
from .service import StrataResolver

__all__ = [
    "AliasRecord",
    "Confidence",
    "EvidenceRecord",
    "InvalidQueryNameError",
    "MatchDocument",
    "MatchKind",
    "MatchParticipantDocument",
    "NameResolution",
    "PlayerCandidate",
    "ProfileAliasDocument",
    "ProfileDocument",
    "QueryName",
    "ReplayContext",
    "ReplayFingerprints",
    "ReplayParticipant",
    "ReplayPlayerResolution",
    "ReplayResolution",
    "ResolutionStatus",
    "SourceContractError",
    "StrataResolver",
    "build_replay_context",
    "extract_match",
    "extract_profile",
    "normalize_query_name",
]
