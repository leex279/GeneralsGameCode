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
DERIVE_FEATURES = "derive_features"
ASSESS_STRATEGIES = "assess_strategies"
ANALYZE_LLM = "analyze_llm"
RENDER_REPORT = "render_report"

STAGES = frozenset(
    {
        DISCOVER,
        HASH,
        MANAGE_COPY,
        PARSE,
        TELEMETRY,
        IMPORT_OBSERVATIONS,
        DERIVE_FEATURES,
        ASSESS_STRATEGIES,
        ANALYZE_LLM,
        RENDER_REPORT,
    }
)

DISCOVER_VERSION = "1"
HASH_VERSION = "1"
MANAGE_COPY_VERSION = "1"
PARSE_VERSION = "1"
TELEMETRY_VERSION = "1"
IMPORT_OBSERVATIONS_VERSION = "1"
DERIVE_FEATURES_VERSION = "1"
ASSESS_STRATEGIES_VERSION = "1"
ANALYZE_LLM_VERSION = "1"
# TheSuperHackers @performance Leex 23/08/2026 Rebuild reports with bounded full-match observation timelines. (#TBD)
RENDER_REPORT_VERSION = "2"


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
