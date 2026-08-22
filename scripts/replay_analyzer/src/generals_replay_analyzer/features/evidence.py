"""Immutable ORM-free evidence values used by pure extractors."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, TypeAlias, cast

EvidenceTier: TypeAlias = Literal["observed", "derived", "inferred"]
EvidenceRole: TypeAlias = Literal["input", "supporting", "contradicting"]


class FrozenMapping(tuple[tuple[str, object], ...]):
    """Tuple-backed mapping marker that remains distinct from an empty JSON array."""

    def __new__(cls, items: tuple[tuple[str, object], ...]) -> Self:
        return super().__new__(cls, items)


CanonicalValue: TypeAlias = None | bool | int | float | str | tuple["CanonicalValue", ...] | FrozenMapping

_FORBIDDEN_IDENTITY_KEYS = {
    "absolute_path",
    "created_at",
    "database_id",
    "managed_path",
    "row_id",
    "source_path",
    "updated_at",
}


def thaw_canonical(value: CanonicalValue) -> object:
    """Return a plain JSON-compatible copy of an immutable canonical value."""
    if isinstance(value, FrozenMapping):
        return {key: thaw_canonical(cast(CanonicalValue, item)) for key, item in value}
    if isinstance(value, tuple):
        return [thaw_canonical(item) for item in cast(tuple[CanonicalValue, ...], value)]
    return value


def freeze_canonical(value: object) -> CanonicalValue:
    """Freeze JSON-like semantic input while rejecting process and persistence identities."""
    if value is None or type(value) in (bool, int, str):
        return cast(None | bool | int | str, value)
    if type(value) is float:
        number = value
        if not math.isfinite(number) or (number == 0.0 and math.copysign(1.0, number) < 0):
            raise ValueError("canonical floats must be finite and may not be negative zero")
        return number
    if isinstance(value, (bytes, bytearray, memoryview, Path)):
        raise TypeError("paths and byte sequences are not canonical feature values")
    if hasattr(value, "_sa_instance_state"):
        raise TypeError("ORM objects are not canonical feature values")
    if isinstance(value, FrozenMapping):
        return value
    if isinstance(value, Mapping):
        items: list[tuple[str, object]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("canonical mapping keys must be built-in strings")
            if key in _FORBIDDEN_IDENTITY_KEYS:
                raise ValueError(f"nonsemantic persistence key is forbidden: {key}")
            items.append((key, freeze_canonical(item)))
        return FrozenMapping(tuple(sorted(items, key=lambda item: item[0])))
    if isinstance(value, (list, tuple)):
        return tuple(freeze_canonical(item) for item in value)
    if isinstance(value, AbstractSet):
        frozen = [freeze_canonical(item) for item in value]
        return tuple(
            sorted(
                frozen,
                key=lambda item: json.dumps(
                    thaw_canonical(item), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                ),
            )
        )
    raise TypeError(f"unsupported canonical feature value: {type(value).__name__}")


def _stable_nonempty(value: str, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a nonempty built-in string")


# TheSuperHackers @feature Leex 22/08/2026 Keep extractor evidence immutable and independent of database row identity. (#TBD)
@dataclass(frozen=True)
class EvidenceRef:
    public_id: str
    tier: EvidenceTier
    source_kind: str
    source_key: str
    schema_version: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.public_id, "public_id"),
            (self.source_kind, "source_kind"),
            (self.source_key, "source_key"),
            (self.schema_version, "schema_version"),
        ):
            _stable_nonempty(value, label)
        if self.tier not in ("observed", "derived", "inferred"):
            raise ValueError("invalid evidence tier")


@dataclass(frozen=True)
class ObservedEvidence:
    ref: EvidenceRef
    frame: int | None
    event_type: str
    facts: CanonicalValue

    def __post_init__(self) -> None:
        if self.ref.tier != "observed":
            raise ValueError("ObservedEvidence requires an observed reference")
        if self.frame is not None and (type(self.frame) is not int or self.frame < 0):
            raise ValueError("observed evidence frame must be nonnegative")
        _stable_nonempty(self.event_type, "event_type")
        object.__setattr__(self, "facts", freeze_canonical(self.facts))


@dataclass(frozen=True)
class DerivedEvidence:
    ref: EvidenceRef
    extractor_name: str
    extractor_version: str
    input_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.ref.tier != "derived":
            raise ValueError("DerivedEvidence requires a derived reference")
        _stable_nonempty(self.extractor_name, "extractor_name")
        _stable_nonempty(self.extractor_version, "extractor_version")
        object.__setattr__(self, "input_evidence_ids", tuple(sorted(self.input_evidence_ids)))


@dataclass(frozen=True)
class InferredEvidence:
    ref: EvidenceRef
    provider: str
    model_identity: str
    cited_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.ref.tier != "inferred":
            raise ValueError("InferredEvidence requires an inferred reference")
        _stable_nonempty(self.provider, "provider")
        _stable_nonempty(self.model_identity, "model_identity")
        object.__setattr__(self, "cited_evidence_ids", tuple(sorted(self.cited_evidence_ids)))


def evidence_sort_key(ref: EvidenceRef) -> tuple[str, str, str]:
    return (ref.source_kind, ref.source_key, ref.public_id)


def fact(evidence: ObservedEvidence, key: str, default: CanonicalValue = None) -> CanonicalValue:
    """Read one immutable observed fact without exposing mutable mapping state."""
    if not isinstance(evidence.facts, FrozenMapping):
        return default
    for item_key, value in evidence.facts:
        if item_key == key:
            return cast(CanonicalValue, value)
    return default
