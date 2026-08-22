"""Pure extractor and typed feature-value contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Protocol, TypeAlias, cast

from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    EvidenceRef,
    evidence_sort_key,
    freeze_canonical,
)
from generals_replay_analyzer.features.registry import FeatureRegistry, ScopeType, ValueType

FeatureQuality: TypeAlias = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class FeatureWindow:
    frame_start: int
    frame_end: int


@dataclass(frozen=True)
class FeatureScope:
    scope_type: ScopeType
    scope_key: str
    replay_player_public_id: str | None = None
    team_id: int | None = None
    entity_public_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.scope_key) is not str or not self.scope_key.strip():
            raise ValueError("scope_key must be nonempty")
        if self.scope_type == "player" and self.replay_player_public_id != self.scope_key:
            raise ValueError("player scope must identify its replay player")
        if self.scope_type == "team" and self.team_id is None:
            raise ValueError("team scope requires team_id")
        if self.scope_type == "entity" and self.entity_public_id != self.scope_key:
            raise ValueError("entity scope must identify its entity")


@dataclass(frozen=True)
class FeatureValue:
    name: str
    value_type: ValueType
    raw_value: int | float | str | bool | CanonicalValue | None
    unit: str | None
    scope: FeatureScope
    window: FeatureWindow
    quality: FeatureQuality
    quality_reason: str | None
    input_evidence: tuple[EvidenceRef, ...]
    supporting_evidence: tuple[EvidenceRef, ...] = ()
    contradicting_evidence: tuple[EvidenceRef, ...] = ()
    details: CanonicalValue = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_value", freeze_canonical(self.raw_value))
        object.__setattr__(self, "details", freeze_canonical(self.details))
        object.__setattr__(self, "input_evidence", tuple(self.input_evidence))
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "contradicting_evidence", tuple(self.contradicting_evidence))


@dataclass(frozen=True)
class FeatureBundle:
    extractor_name: str
    extractor_version: str
    values: tuple[FeatureValue, ...]


class FeatureExtractor(Protocol):
    name: str
    version: str
    feature_names: tuple[str, ...]

    def extract(self, context: object) -> FeatureBundle: ...


def _raw_type_matches(value_type: ValueType, raw_value: CanonicalValue) -> bool:
    if value_type == "integer":
        return type(raw_value) is int
    if value_type == "real":
        return type(raw_value) in (int, float)
    if value_type == "text":
        return type(raw_value) is str
    if value_type == "boolean":
        return type(raw_value) is bool
    return raw_value is not None and type(raw_value) not in (bool, int, float, str)


# TheSuperHackers @feature Leex 22/08/2026 Validate raw feature values against one immutable registry contract. (#TBD)
def validate_feature_value(value: FeatureValue, registry: FeatureRegistry) -> FeatureValue:
    definition = registry.definition(value.name)
    if value.value_type != definition.value_type:
        raise ValueError("feature value_type does not match registry definition")
    if value.unit != definition.unit:
        raise ValueError("feature unit does not match registry definition")
    if value.scope.scope_type not in definition.scope_types:
        raise ValueError("feature scope is not allowed by registry definition")
    if (
        type(value.window.frame_start) is not int
        or type(value.window.frame_end) is not int
        or value.window.frame_start < 0
        or value.window.frame_end < value.window.frame_start
    ):
        raise ValueError("feature window must be inclusive and nonnegative")
    reason = value.quality_reason
    if value.quality == "unavailable":
        if value.raw_value is not None or type(reason) is not str or not reason.strip():
            raise ValueError("unavailable feature must be null with a stable reason")
    else:
        if value.raw_value is None or not _raw_type_matches(value.value_type, cast(CanonicalValue, value.raw_value)):
            raise ValueError("available feature raw value does not match value_type")
        if value.quality == "complete" and reason is not None:
            raise ValueError("complete feature may not have a quality reason")
        if value.quality == "partial" and (type(reason) is not str or not reason.strip()):
            raise ValueError("partial feature requires a stable omission reason")
        if not value.input_evidence:
            raise ValueError("available feature requires direct observed input evidence")
    public_ids = tuple(ref.public_id for ref in value.input_evidence)
    if len(public_ids) != len(set(public_ids)):
        raise ValueError("duplicate input evidence")
    if any(ref.tier != "observed" for ref in value.input_evidence):
        raise ValueError("input evidence must be observed")
    return replace(
        value,
        input_evidence=tuple(sorted(value.input_evidence, key=evidence_sort_key)),
        supporting_evidence=tuple(sorted(value.supporting_evidence, key=evidence_sort_key)),
        contradicting_evidence=tuple(sorted(value.contradicting_evidence, key=evidence_sort_key)),
    )


def unavailable_value(
    name: str,
    scope: FeatureScope,
    window: FeatureWindow,
    reason: str,
    registry: FeatureRegistry,
    *,
    input_evidence: tuple[EvidenceRef, ...] = (),
    details: object = (),
) -> FeatureValue:
    definition = registry.definition(name)
    return validate_feature_value(
        FeatureValue(
            name=name,
            value_type=definition.value_type,
            raw_value=None,
            unit=definition.unit,
            scope=scope,
            window=window,
            quality="unavailable",
            quality_reason=reason,
            input_evidence=input_evidence,
            details=cast(CanonicalValue, details),
        ),
        registry,
    )


def complete_value(
    name: str,
    raw_value: object,
    scope: FeatureScope,
    window: FeatureWindow,
    input_evidence: tuple[EvidenceRef, ...],
    registry: FeatureRegistry,
    *,
    details: object = (),
) -> FeatureValue:
    definition = registry.definition(name)
    return validate_feature_value(
        FeatureValue(
            name=name,
            value_type=definition.value_type,
            raw_value=cast(CanonicalValue, raw_value),
            unit=definition.unit,
            scope=scope,
            window=window,
            quality="complete",
            quality_reason=None,
            input_evidence=input_evidence,
            details=cast(CanonicalValue, details),
        ),
        registry,
    )
