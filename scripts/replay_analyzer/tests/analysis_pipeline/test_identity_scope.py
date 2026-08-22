"""Canonical identity bindings carried by player-scoped analysis jobs."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from generals_replay_analyzer.analysis_pipeline.identity_scope import (
    CanonicalPlayerIdentityBinding,
    IdentityAnalysisScope,
    IdentityScopeError,
)
from generals_replay_analyzer.identity.audit import identity_cache_digest

REPLAY_PLAYER_ID = "00000000-0000-4000-8000-00000000b101"
PLAYER_ID = "00000000-0000-4000-8000-00000000b102"


def _binding(revision: int = 3) -> CanonicalPlayerIdentityBinding:
    return CanonicalPlayerIdentityBinding(
        replay_player_public_id=REPLAY_PLAYER_ID,
        player_public_id=PLAYER_ID,
        identity_revision=revision,
        identity_cache_token=identity_cache_digest(PLAYER_ID, revision),
    )


def test_scope_round_trip_is_frozen_canonical_and_path_free() -> None:
    scope = IdentityAnalysisScope("identity_invalidation", (_binding(),))

    assert IdentityAnalysisScope.from_json(scope.to_json()) == scope
    assert scope.to_json() == {
        "schema_version": "analysis-identity-scope-v1",
        "kind": "identity_invalidation",
        "bindings": [
            {
                "replay_player_public_id": REPLAY_PLAYER_ID,
                "player_public_id": PLAYER_ID,
                "identity_revision": 3,
                "identity_cache_token": identity_cache_digest(PLAYER_ID, 3),
            }
        ],
    }
    with pytest.raises(FrozenInstanceError):
        scope.kind = "full_replay"  # type: ignore[misc]


def test_scope_rejects_empty_invalidation_and_duplicate_replay_player_bindings() -> None:
    with pytest.raises(IdentityScopeError, match="nonempty"):
        IdentityAnalysisScope("identity_invalidation", ())
    with pytest.raises(IdentityScopeError, match="sorted and unique"):
        IdentityAnalysisScope("full_replay", (_binding(4), _binding(3)))


def test_binding_rejects_forged_cache_token() -> None:
    with pytest.raises(IdentityScopeError, match="cache token"):
        CanonicalPlayerIdentityBinding(
            replay_player_public_id=REPLAY_PLAYER_ID,
            player_public_id=PLAYER_ID,
            identity_revision=3,
            identity_cache_token="f" * 64,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("replay_player_public_id", 7, "canonical lowercase UUID"),
        ("replay_player_public_id", "not-a-uuid", "canonical lowercase UUID"),
        ("replay_player_public_id", REPLAY_PLAYER_ID.upper(), "canonical lowercase UUID"),
        ("identity_revision", True, "nonnegative integer"),
        ("identity_revision", -1, "nonnegative integer"),
    ],
)
def test_binding_rejects_noncanonical_public_values(field: str, value: object, message: str) -> None:
    values: dict[str, object] = {
        "replay_player_public_id": REPLAY_PLAYER_ID,
        "player_public_id": PLAYER_ID,
        "identity_revision": 3,
        "identity_cache_token": identity_cache_digest(PLAYER_ID, 3),
    }
    values[field] = value
    with pytest.raises(IdentityScopeError, match=message):
        CanonicalPlayerIdentityBinding(**values)  # type: ignore[arg-type]


def test_closed_json_decoders_reject_unknown_shapes_and_mutable_scope_contracts() -> None:
    binding_json = _binding().to_json()
    with pytest.raises(IdentityScopeError, match="unknown or missing"):
        CanonicalPlayerIdentityBinding.from_json({**binding_json, "extra": True})
    with pytest.raises(IdentityScopeError, match="kind"):
        IdentityAnalysisScope("unknown", ())  # type: ignore[arg-type]
    with pytest.raises(IdentityScopeError, match="immutable tuple"):
        IdentityAnalysisScope("full_replay", [])  # type: ignore[arg-type]

    valid = IdentityAnalysisScope("full_replay", (_binding(),)).to_json()
    with pytest.raises(IdentityScopeError, match="unknown or missing"):
        IdentityAnalysisScope.from_json({**valid, "extra": True})
    with pytest.raises(IdentityScopeError, match="schema version"):
        IdentityAnalysisScope.from_json({**valid, "schema_version": "v2"})
    with pytest.raises(IdentityScopeError, match="kind"):
        IdentityAnalysisScope.from_json({**valid, "kind": "unknown"})
    with pytest.raises(IdentityScopeError, match="array"):
        IdentityAnalysisScope.from_json({**valid, "bindings": {}})
