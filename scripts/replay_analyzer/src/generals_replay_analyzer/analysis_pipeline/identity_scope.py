"""Closed canonical-player identity bindings for durable analysis jobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from ..identity.audit import identity_cache_digest

IdentityScopeKind = Literal["full_replay", "identity_invalidation"]


class IdentityScopeError(ValueError):
    """An analysis identity scope is malformed or internally inconsistent."""


def _uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise IdentityScopeError(f"{label} must be a canonical lowercase UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise IdentityScopeError(f"{label} must be a canonical lowercase UUID") from error
    if str(parsed) != value:
        raise IdentityScopeError(f"{label} must be a canonical lowercase UUID")
    return value


# TheSuperHackers @feature Leex 23/08/2026 Bind player-scoped analysis to canonical identity revisions. (#TBD)
@dataclass(frozen=True, slots=True)
class CanonicalPlayerIdentityBinding:
    """One replay observation bound to the current canonical-player revision."""

    replay_player_public_id: str
    player_public_id: str
    identity_revision: int
    identity_cache_token: str

    def __post_init__(self) -> None:
        _uuid(self.replay_player_public_id, "replay_player_public_id")
        _uuid(self.player_public_id, "player_public_id")
        if type(self.identity_revision) is not int or self.identity_revision < 0:
            raise IdentityScopeError("identity revision must be a nonnegative integer")
        expected = identity_cache_digest(self.player_public_id, self.identity_revision)
        if self.identity_cache_token != expected:
            raise IdentityScopeError("identity cache token does not match the canonical player revision")

    def to_json(self) -> dict[str, object]:
        return {
            "replay_player_public_id": self.replay_player_public_id,
            "player_public_id": self.player_public_id,
            "identity_revision": self.identity_revision,
            "identity_cache_token": self.identity_cache_token,
        }

    @classmethod
    def from_json(cls, value: object) -> CanonicalPlayerIdentityBinding:
        if not isinstance(value, Mapping) or set(value) != {
            "replay_player_public_id",
            "player_public_id",
            "identity_revision",
            "identity_cache_token",
        }:
            raise IdentityScopeError("identity binding uses an unknown or missing field")
        return cls(
            _uuid(value["replay_player_public_id"], "replay_player_public_id"),
            _uuid(value["player_public_id"], "player_public_id"),
            cast(int, value["identity_revision"]),
            cast(str, value["identity_cache_token"]),
        )


@dataclass(frozen=True, slots=True)
class IdentityAnalysisScope:
    """Exact canonical-player scope captured in a durable job identity."""

    kind: IdentityScopeKind
    bindings: tuple[CanonicalPlayerIdentityBinding, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"full_replay", "identity_invalidation"}:
            raise IdentityScopeError("identity scope kind is invalid")
        if type(self.bindings) is not tuple:
            raise IdentityScopeError("identity bindings must be an immutable tuple")
        public_ids = tuple(binding.replay_player_public_id for binding in self.bindings)
        if public_ids != tuple(sorted(set(public_ids))):
            raise IdentityScopeError("identity bindings must be sorted and unique")
        if self.kind == "identity_invalidation" and not self.bindings:
            raise IdentityScopeError("identity invalidation scope must be nonempty")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": "analysis-identity-scope-v1",
            "kind": self.kind,
            "bindings": [binding.to_json() for binding in self.bindings],
        }

    @classmethod
    def from_json(cls, value: object) -> IdentityAnalysisScope:
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "kind", "bindings"}:
            raise IdentityScopeError("identity scope uses an unknown or missing field")
        if value["schema_version"] != "analysis-identity-scope-v1":
            raise IdentityScopeError("identity scope schema version is invalid")
        kind = value["kind"]
        if kind not in {"full_replay", "identity_invalidation"}:
            raise IdentityScopeError("identity scope kind is invalid")
        raw_bindings = value["bindings"]
        if type(raw_bindings) not in (list, tuple):
            raise IdentityScopeError("identity bindings must be an array")
        return cls(
            cast(IdentityScopeKind, kind),
            tuple(CanonicalPlayerIdentityBinding.from_json(item) for item in cast(list[object], raw_bindings)),
        )
