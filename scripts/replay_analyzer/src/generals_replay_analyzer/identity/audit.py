"""Canonical identity audit and cache-token helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Encode one value with the database's canonical JSON policy."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def identity_cache_digest(player_public_id: str, identity_revision: int) -> str:
    """Return the stable lower-case Task 9 identity cache component."""
    payload = {
        "identity_revision": identity_revision,
        "player_public_id": player_public_id,
        "schema": "player-identity-cache-v1",
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
