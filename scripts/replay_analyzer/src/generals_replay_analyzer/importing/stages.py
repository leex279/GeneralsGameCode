"""Closed replay-analysis stage names, component versions, and identities."""

from __future__ import annotations

import hashlib
import json
from typing import Any

# TheSuperHackers @feature Leex 21/08/2026 Freeze replay-analysis stage and version identities for durable retries. (#TBD)
DISCOVER = "discover"
HASH = "hash"
MANAGE_COPY = "manage_copy"
PARSE = "parse"
TELEMETRY = "telemetry"
IMPORT_OBSERVATIONS = "import_observations"
RECONCILE_IDENTITIES = "reconcile_identities"
DERIVE_FEATURES = "derive_features"
ASSESS_STRATEGIES = "assess_strategies"
ANALYZE_LLM = "analyze_llm"
RENDER_REPORT = "render_report"
RENDER_VIDEO = "render_video"

STAGES = frozenset(
    {
        DISCOVER,
        HASH,
        MANAGE_COPY,
        PARSE,
        TELEMETRY,
        IMPORT_OBSERVATIONS,
        RECONCILE_IDENTITIES,
        DERIVE_FEATURES,
        ASSESS_STRATEGIES,
        ANALYZE_LLM,
        RENDER_REPORT,
        RENDER_VIDEO,
    }
)

DISCOVER_VERSION = "1"
HASH_VERSION = "1"
MANAGE_COPY_VERSION = "1"
PARSE_VERSION = "1"
TELEMETRY_VERSION = "1"
IMPORT_OBSERVATIONS_VERSION = "1"
RECONCILE_IDENTITIES_VERSION = "1"
# TheSuperHackers @bugfix Leex 25/08/2026 Regenerate durable feature graphs with projected legacy combat template identities. (#TBD)
DERIVE_FEATURES_VERSION = "2"
# TheSuperHackers @fix Leex 25/08/2026 Requeue assessment after preserving feature cache identities across optimized extraction. (#TBD)
ASSESS_STRATEGIES_VERSION = "2"
ANALYZE_LLM_VERSION = "1"
# TheSuperHackers @performance Leex 23/08/2026 Rebuild reports with bounded full-match observation timelines. (#TBD)
RENDER_REPORT_VERSION = "2"
# TheSuperHackers @bugfix Leex 25/08/2026 Invalidate failed casts after production-scale scene authority resolution fixes. (#TBD)
# TheSuperHackers @bugfix Leex 25/08/2026 Keep camera render jobs valid for engine-validated airspace targets above terrain bounds. (#TBD)
# TheSuperHackers @feature Leex 25/08/2026 Render sparse strategy, build, production, and battle commentary from full-match evidence. (#TBD)
# TheSuperHackers @bugfix Leex 25/08/2026 Requeue casts with the rendered absolute replay launch contract. (#TBD)
# TheSuperHackers @bugfix Leex 25/08/2026 Requeue casts with logic-timebase video sampling and absolute capture horizons. (#TBD)
# TheSuperHackers @bugfix Leex 25/08/2026 Requeue casts with bounded terminal settlement-frame padding. (#TBD)
# TheSuperHackers @feature Leex 25/08/2026 Requeue casts with calmer camera pacing and sparse identity-specific combat calls. (#TBD)
RENDER_VIDEO_VERSION = "23"


def canonical_json(value: Any) -> str:
    """Apply Task 2's byte-stable canonical JSON policy."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def input_digest(identity: Any) -> str:
    """Hash semantic stage identity without path, row, time, or presentation inputs."""
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def content_key(stage: str, version: str, replay_sha256: str, identity: Any) -> str:
    """Return the frozen content-stage idempotency-key shape."""
    normalized = replay_sha256.lower()
    if stage not in STAGES or len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("content jobs require a closed stage and lowercase SHA-256")
    return f"{stage}:{version}:{normalized}:{input_digest(identity)}"
