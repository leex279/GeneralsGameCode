"""Immutable ordered extractor context and semantic cache identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast

from generals_replay_analyzer.features.base import FeatureScope, FeatureValue
from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    EvidenceRef,
    FrozenMapping,
    ObservedEvidence,
    freeze_canonical,
)
from generals_replay_analyzer.features.registry import REGISTRY_SCHEMA

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = {"absolute_path", "created_at", "database_id", "managed_path", "row_id", "source_path", "updated_at"}


def observation_sort_key(item: ObservedEvidence) -> tuple[int, str, str, str]:
    return (item.frame if item.frame is not None else -1, item.ref.source_kind, item.ref.source_key, item.ref.public_id)


@dataclass(frozen=True)
class FeatureContext:
    cache_schema: Literal["feature-context-v1"]
    replay_public_id: str
    replay_sha256: str
    replay_player_public_id: str | None
    scope: FeatureScope
    observation_schema_versions: tuple[tuple[str, str], ...]
    parser_completion_status: str | None
    telemetry_status: str | None
    final_frame: int | None
    logic_frames_per_second: Literal[30, 60] | None
    catalog_identity: str | None
    observed: tuple[ObservedEvidence, ...]
    settings: CanonicalValue
    parser_run_public_id: str | None = None
    telemetry_run_public_id: str | None = None

    def __post_init__(self) -> None:
        if self.cache_schema != "feature-context-v1":
            raise ValueError("unsupported feature context schema")
        if not _SHA256.fullmatch(self.replay_sha256):
            raise ValueError("replay sha256 must be lower-case hexadecimal")
        if self.final_frame is not None and (type(self.final_frame) is not int or self.final_frame < 0):
            raise ValueError("final_frame must be nonnegative")
        if self.logic_frames_per_second not in (None, 30, 60):
            raise ValueError("logic_frames_per_second must be 30, 60, or unavailable")
        if self.scope.scope_type == "player" and self.replay_player_public_id != self.scope.scope_key:
            raise ValueError("player context scope mismatch")
        versions = tuple(sorted(self.observation_schema_versions))
        if len(set(versions)) != len(versions):
            raise ValueError("duplicate observation schema family")
        object.__setattr__(self, "observation_schema_versions", versions)
        public_ids = tuple(item.ref.public_id for item in self.observed)
        source_keys = tuple(item.ref.source_key for item in self.observed)
        if len(public_ids) != len(set(public_ids)):
            raise ValueError("duplicate public evidence identity")
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("duplicate source evidence identity")
        if any(item.ref.tier != "observed" for item in self.observed):
            raise ValueError("feature context accepts observed evidence only")
        if any(item.frame is not None and item.frame < 0 for item in self.observed):
            raise ValueError("observed frame must be nonnegative")
        object.__setattr__(self, "observed", tuple(sorted(self.observed, key=observation_sort_key)))
        object.__setattr__(self, "settings", freeze_canonical(self.settings))


def _semantic_sort(
    values: list[object], original: tuple[object, ...] | list[object] | AbstractSet[object]
) -> list[object]:
    if isinstance(original, AbstractSet):
        return sorted(values, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    if original and all(isinstance(item, ObservedEvidence) for item in original):
        paired = zip(cast(tuple[ObservedEvidence, ...], original), values, strict=True)
        return [value for _, value in sorted(paired, key=lambda pair: observation_sort_key(pair[0]))]
    if original and all(isinstance(item, EvidenceRef) for item in original):
        paired_refs = zip(cast(tuple[EvidenceRef, ...], original), values, strict=True)
        return [value for _, value in sorted(paired_refs, key=lambda pair: (pair[0].source_kind, pair[0].source_key, pair[0].public_id))]
    if original and all(isinstance(item, FeatureValue) for item in original):
        paired_features = zip(cast(tuple[FeatureValue, ...], original), values, strict=True)
        return [
            value
            for _, value in sorted(
                paired_features,
                key=lambda pair: (
                    pair[0].name,
                    pair[0].scope.scope_type,
                    pair[0].scope.scope_key,
                    pair[0].window.frame_start,
                    pair[0].window.frame_end,
                ),
            )
        ]
    if values and all(isinstance(item, dict) and type(item.get("message_type")) is int for item in values):
        return sorted(values, key=lambda item: cast(int, cast(dict[str, object], item)["message_type"]))
    if values and all(isinstance(item, dict) and type(item.get("public_id")) is str for item in values):
        return sorted(values, key=lambda item: cast(str, cast(dict[str, object], item)["public_id"]))
    return values


def _semantic(value: object) -> object:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        number = value
        if not math.isfinite(number) or (number == 0.0 and math.copysign(1.0, number) < 0):
            raise ValueError("canonical floats must be finite and may not be negative zero")
        return number
    if isinstance(value, (bytes, bytearray, memoryview, Path, date, datetime)):
        raise TypeError("paths, bytes, and timestamps are not canonical cache input")
    if hasattr(value, "_sa_instance_state"):
        raise TypeError("ORM objects are not canonical cache input")
    if isinstance(value, FrozenMapping):
        return {key: _semantic(item) for key, item in value}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _semantic(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("canonical mapping keys must be built-in strings")
            if key in _FORBIDDEN_KEYS:
                raise ValueError(f"nonsemantic persistence key is forbidden: {key}")
            result[key] = _semantic(item)
        return result
    if isinstance(value, (tuple, list, AbstractSet)):
        original = cast(tuple[object, ...] | list[object] | AbstractSet[object], value)
        return _semantic_sort([_semantic(item) for item in value], original)
    raise TypeError(f"unsupported canonical cache input: {type(value).__name__}")


# TheSuperHackers @feature Leex 22/08/2026 Hash only ordered semantic replay facts and explicit extractor versions. (#TBD)
def canonical_json(value: object) -> str:
    return json.dumps(_semantic(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def input_digest(context: FeatureContext) -> str:
    return hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()


def cache_key_from_digest(
    context_digest: str,
    extractor_name: str,
    extractor_version: str,
    *,
    registry_schema: str = REGISTRY_SCHEMA,
) -> str:
    # TheSuperHackers @performance Leex 25/08/2026 Reuse the canonical context digest instead of serializing full telemetry per extractor. (#TBD)
    identity = {
        "cache_schema": "feature-cache-v2",
        "context_digest": context_digest,
        "extractor": {"name": extractor_name, "version": extractor_version},
        "registry_schema": registry_schema,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def cache_key(
    context: FeatureContext,
    extractor_name: str,
    extractor_version: str,
    *,
    registry_schema: str = REGISTRY_SCHEMA,
) -> str:
    return cache_key_from_digest(
        input_digest(context), extractor_name, extractor_version, registry_schema=registry_schema
    )
