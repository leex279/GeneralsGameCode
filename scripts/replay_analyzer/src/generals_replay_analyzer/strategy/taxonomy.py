"""Versioned canonical JSON strategy taxonomy loading and validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, Literal, TypeAlias, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.registry import FeatureDefinition, FeatureRegistry

StrategyPhase: TypeAlias = Literal["opening", "early", "mid", "late", "cross_phase"]
AssessmentQuality: TypeAlias = Literal["available", "partial", "unavailable"]
PredicateOperator: TypeAlias = Literal["eq", "ne", "lt", "lte", "gt", "gte", "contains", "not_contains"]

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*(?:[a-z0-9_-]*[a-z0-9])?$")
_VERSION = re.compile(r"^[a-z][a-z0-9_]*(?:[a-z0-9_.-]*[a-z0-9])?$")
_QUALITY_RANK: dict[AssessmentQuality, int] = {"unavailable": 0, "partial": 1, "available": 2}


@dataclass(frozen=True)
class FeaturePredicate:
    predicate_id: str
    feature_name: str
    operator: PredicateOperator
    expected_value: int | float | str | bool
    unit: str
    allowed_scope_types: tuple[str, ...]
    weight: int


@dataclass(frozen=True)
class Applicability:
    faction_template_names: tuple[str, ...]
    opponent_faction_template_names: tuple[str, ...]
    map_identities: tuple[str, ...]


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    display_name: str
    synonyms: tuple[str, ...]
    phase: StrategyPhase
    applicability: Applicability
    required: tuple[FeaturePredicate, ...]
    supporting: tuple[FeaturePredicate, ...]
    contradicting: tuple[FeaturePredicate, ...]
    minimum_quality: AssessmentQuality
    rule_version: str
    fallback: bool


@dataclass(frozen=True)
class StrategyTaxonomy:
    schema_version: Literal["strategy-taxonomy-v1"]
    taxonomy_version: str
    content_sha256: str
    strategies: tuple[StrategyDefinition, ...]


class TaxonomyValidationError(ValueError):
    """A taxonomy resource is absent, malformed, or semantically incompatible."""


def quality_at_least(actual: AssessmentQuality, minimum: AssessmentQuality) -> bool:
    return _QUALITY_RANK[actual] >= _QUALITY_RANK[minimum]


def _resource(name: str) -> Traversable:
    return resources.files("generals_replay_analyzer").joinpath("data", name)


def _decoded_resource(resource: Traversable, *, label: str) -> object:
    try:
        raw = resource.read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise TaxonomyValidationError(f"{label} resource is unavailable") from error
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TaxonomyValidationError(f"{label} is not valid UTF-8 JSON") from error
    _reject_nonfinite(value)
    return value


def _reject_nonfinite(value: object) -> None:
    if type(value) is float:
        if not math.isfinite(value):
            raise TaxonomyValidationError("taxonomy numeric thresholds must be finite")
        if value == 0.0 and math.copysign(1.0, value) < 0:
            raise TaxonomyValidationError("taxonomy numeric thresholds may not be negative zero")
        return
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)


def _validate_schema(document: object) -> dict[str, Any]:
    schema = _decoded_resource(_resource("strategy-taxonomy-v1.schema.json"), label="taxonomy schema")
    if not isinstance(schema, dict):
        raise TaxonomyValidationError("taxonomy schema root must be an object")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
    except SchemaError as error:
        raise TaxonomyValidationError("packaged taxonomy schema is invalid") from error
    except ValidationError as error:
        path = ".".join(str(item) for item in error.absolute_path)
        identity = " identifier" if path.endswith(("predicate_id", "strategy_id", "rule_version")) else ""
        location = f" at {path}" if path else ""
        raise TaxonomyValidationError(
            f"taxonomy schema validation failed{location}{identity}: {error.message}"
        ) from error
    if not isinstance(document, dict):
        raise TaxonomyValidationError("taxonomy schema requires an object")
    return cast(dict[str, Any], document)


def _strict_sorted_unique(values: list[str], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise TaxonomyValidationError(f"{label} must be unique")
    if values != sorted(values):
        raise TaxonomyValidationError(f"{label} must be sorted")
    return tuple(values)


def _validate_expected(predicate: FeaturePredicate, definition: FeatureDefinition) -> None:
    expected = predicate.expected_value
    if type(expected) is float:
        _reject_nonfinite(expected)
    if definition.value_type == "json":
        raise TaxonomyValidationError("JSON feature predicates are prohibited in taxonomy v1")
    value_matches = {
        "integer": type(expected) is int,
        "real": type(expected) in (int, float),
        "text": type(expected) is str,
        "boolean": type(expected) is bool,
    }[definition.value_type]
    if not value_matches:
        raise TaxonomyValidationError("predicate expected value type does not match registry feature value type")
    if predicate.operator in ("lt", "lte", "gt", "gte") and definition.value_type not in ("integer", "real"):
        raise TaxonomyValidationError("numeric predicate operator requires a numeric registry feature")
    if predicate.operator in ("contains", "not_contains") and definition.value_type != "text":
        raise TaxonomyValidationError("contains predicate operator requires a text registry feature")


def _predicate(value: dict[str, Any], registry: FeatureRegistry) -> FeaturePredicate:
    predicate = FeaturePredicate(
        predicate_id=cast(str, value["predicate_id"]),
        feature_name=cast(str, value["feature_name"]),
        operator=cast(PredicateOperator, value["operator"]),
        expected_value=cast(int | float | str | bool, value["expected_value"]),
        unit=cast(str, value["unit"]),
        allowed_scope_types=_strict_sorted_unique(cast(list[str], value["allowed_scope_types"]), "predicate scopes"),
        weight=cast(int, value["weight"]),
    )
    if not _IDENTIFIER.fullmatch(predicate.predicate_id):
        raise TaxonomyValidationError("predicate identifier is invalid")
    if type(predicate.weight) is not int or predicate.weight <= 0:
        raise TaxonomyValidationError("predicate weight must be a positive integer")
    try:
        definition = registry.definition(predicate.feature_name)
    except KeyError as error:
        raise TaxonomyValidationError("predicate names an unknown registry feature") from error
    if predicate.unit != definition.unit:
        raise TaxonomyValidationError("predicate unit does not match registry feature")
    if not set(predicate.allowed_scope_types) <= set(definition.scope_types):
        raise TaxonomyValidationError("predicate scope is incompatible with registry feature")
    _validate_expected(predicate, definition)
    return predicate


def _predicate_group(
    value: list[dict[str, Any]], label: str, registry: FeatureRegistry
) -> tuple[FeaturePredicate, ...]:
    predicates = tuple(_predicate(item, registry) for item in value)
    ids = tuple(item.predicate_id for item in predicates)
    if len(ids) != len(set(ids)):
        raise TaxonomyValidationError(f"{label} predicate IDs must be unique")
    if ids != tuple(sorted(ids)):
        raise TaxonomyValidationError(f"{label} predicate IDs must be sorted")
    return predicates


def _strategy(value: dict[str, Any], registry: FeatureRegistry) -> StrategyDefinition:
    applicability_raw = cast(dict[str, list[str]], value["applicability"])
    applicability = Applicability(
        _strict_sorted_unique(applicability_raw["faction_template_names"], "faction applicability"),
        _strict_sorted_unique(applicability_raw["opponent_faction_template_names"], "opponent applicability"),
        _strict_sorted_unique(applicability_raw["map_identities"], "map applicability"),
    )
    definition = StrategyDefinition(
        strategy_id=cast(str, value["strategy_id"]),
        display_name=cast(str, value["display_name"]),
        synonyms=_strict_sorted_unique(cast(list[str], value["synonyms"]), "strategy synonyms"),
        phase=cast(StrategyPhase, value["phase"]),
        applicability=applicability,
        required=_predicate_group(cast(list[dict[str, Any]], value["required"]), "required", registry),
        supporting=_predicate_group(cast(list[dict[str, Any]], value["supporting"]), "supporting", registry),
        contradicting=_predicate_group(cast(list[dict[str, Any]], value["contradicting"]), "contradicting", registry),
        minimum_quality=cast(AssessmentQuality, value["minimum_quality"]),
        rule_version=cast(str, value["rule_version"]),
        fallback=cast(bool, value["fallback"]),
    )
    if not _IDENTIFIER.fullmatch(definition.strategy_id) or not _VERSION.fullmatch(definition.rule_version):
        raise TaxonomyValidationError("strategy identifier or version is invalid")
    all_predicates = definition.required + definition.supporting + definition.contradicting
    predicate_ids = tuple(item.predicate_id for item in all_predicates)
    if len(predicate_ids) != len(set(predicate_ids)):
        raise TaxonomyValidationError("predicate ID may occur in exactly one predicate group")
    selectors = tuple(item.feature_name for item in all_predicates)
    if len(selectors) != len(set(selectors)):
        raise TaxonomyValidationError("overlapping feature selector is prohibited")
    if definition.fallback:
        if definition.strategy_id != "unknown_or_mixed" or definition.phase != "cross_phase" or all_predicates:
            raise TaxonomyValidationError("fallback must be the empty cross-phase unknown_or_mixed definition")
        if applicability != Applicability(("*",), ("*",), ("*",)):
            raise TaxonomyValidationError("fallback must use explicit wildcard applicability")
    else:
        if not definition.required:
            raise TaxonomyValidationError("regular strategy requires at least one required predicate")
        if "*" in (
            applicability.faction_template_names
            + applicability.opponent_faction_template_names
            + applicability.map_identities
        ):
            raise TaxonomyValidationError("regular strategy may not use wildcard applicability")
    return definition


# TheSuperHackers @feature Leex 22/08/2026 Load strategy rules only from canonical package resources and the explicit frozen registry. (#TBD)
def load_taxonomy(resource: Traversable, registry: FeatureRegistry) -> StrategyTaxonomy:
    document = _validate_schema(_decoded_resource(resource, label="taxonomy"))
    taxonomy_version = cast(str, document["taxonomy_version"])
    if not taxonomy_version.startswith("strategy-taxonomy-v1."):
        raise TaxonomyValidationError("taxonomy version identity is incompatible with schema v1")
    strategy_values = cast(list[dict[str, Any]], document["strategies"])
    raw_ids = [cast(str, item["strategy_id"]) for item in strategy_values]
    if len(raw_ids) != len(set(raw_ids)):
        raise TaxonomyValidationError("strategy IDs must be unique")
    if raw_ids != sorted(raw_ids):
        raise TaxonomyValidationError("strategy IDs must be sorted")
    if sum(cast(bool, item["fallback"]) for item in strategy_values) != 1:
        raise TaxonomyValidationError("taxonomy must define exactly one fallback")
    strategies = tuple(_strategy(item, registry) for item in strategy_values)
    fallbacks = tuple(item for item in strategies if item.fallback)
    if len(fallbacks) != 1:
        raise TaxonomyValidationError("taxonomy must define exactly one fallback")
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return StrategyTaxonomy("strategy-taxonomy-v1", taxonomy_version, digest, strategies)


def default_taxonomy(registry: FeatureRegistry) -> StrategyTaxonomy:
    return load_taxonomy(_resource("strategy-taxonomy-v1.json"), registry)
