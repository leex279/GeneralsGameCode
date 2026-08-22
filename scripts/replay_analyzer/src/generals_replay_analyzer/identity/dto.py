"""Frozen public values returned by the player identity service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class IdentityDecision:
    replay_player_public_id: str
    normalized_name: str | None
    outcome: Literal["linked", "created", "already_linked", "manual_review", "ineligible"]
    player_public_id: str | None
    alias_public_id: str | None
    operation_public_id: str | None
    reason_code: str


@dataclass(frozen=True)
class IdentityResolutionBatch:
    replay_public_id: str
    parser_run_id: str
    decisions: tuple[IdentityDecision, ...]
    affected_player_revisions: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class IdentityOperationReceipt:
    operation_public_id: str
    operation_kind: str
    inverse_of_operation_public_id: str | None
    affected_player_revisions: tuple[tuple[str, int], ...]
    cache_tokens: tuple[tuple[str, str], ...]
