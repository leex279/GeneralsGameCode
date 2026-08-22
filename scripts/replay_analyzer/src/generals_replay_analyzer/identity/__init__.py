"""Safe player identity normalization and service interfaces."""

from generals_replay_analyzer.identity.dto import IdentityDecision, IdentityOperationReceipt, IdentityResolutionBatch
from generals_replay_analyzer.identity.normalize import (
    EMBEDDED_REPLAY_NAME_NAMESPACE,
    EXTERNAL_NAMESPACE_PREFIX,
    STRATA_FILENAME_NAMESPACE,
    InvalidPlayerNameError,
    normalize_embedded_name,
)
from generals_replay_analyzer.identity.service import (
    IdentityBusyError,
    IdentityConflictError,
    IdentityError,
    IdentityInvariantError,
    IdentityNotFoundError,
    PlayerIdentityService,
)

__all__ = [
    "EMBEDDED_REPLAY_NAME_NAMESPACE",
    "EXTERNAL_NAMESPACE_PREFIX",
    "STRATA_FILENAME_NAMESPACE",
    "IdentityBusyError",
    "IdentityConflictError",
    "IdentityDecision",
    "IdentityError",
    "IdentityInvariantError",
    "IdentityNotFoundError",
    "IdentityOperationReceipt",
    "IdentityResolutionBatch",
    "InvalidPlayerNameError",
    "PlayerIdentityService",
    "normalize_embedded_name",
]
