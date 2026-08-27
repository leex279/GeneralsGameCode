"""Standalone Strata replay-player identity resolution API."""

from .contracts import (
    AliasRecord,
    Confidence,
    EvidenceRecord,
    MatchKind,
    NameResolution,
    PlayerCandidate,
    QueryName,
    ResolutionStatus,
)
from .normalization import InvalidQueryNameError, normalize_query_name

__all__ = [
    "AliasRecord",
    "Confidence",
    "EvidenceRecord",
    "InvalidQueryNameError",
    "MatchKind",
    "NameResolution",
    "PlayerCandidate",
    "QueryName",
    "ResolutionStatus",
    "normalize_query_name",
]
