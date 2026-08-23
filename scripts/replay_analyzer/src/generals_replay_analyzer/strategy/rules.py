"""Pure deterministic strategy predicate evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from generals_replay_analyzer.features.base import FeatureScope, FeatureValue, FeatureWindow, validate_feature_value
from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    DerivedEvidence,
    EvidenceRef,
    ObservedEvidence,
    evidence_sort_key,
    fact,
    freeze_canonical,
)
from generals_replay_analyzer.features.registry import FeatureRegistry
from generals_replay_analyzer.strategy.taxonomy import (
    AssessmentQuality,
    FeaturePredicate,
    StrategyDefinition,
    StrategyPhase,
    quality_at_least,
)

PredicateState: TypeAlias = Literal["matched", "not_matched", "unavailable"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class StrategyFeature:
    value: FeatureValue
    derived_evidence: DerivedEvidence


@dataclass(frozen=True)
class CatalogProof:
    catalog_identity: str
    evidence: ObservedEvidence
    factions_by_template: tuple[tuple[str, str | None], ...]
    category_tags_by_template: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        if type(self.catalog_identity) is not str or not self.catalog_identity.strip():
            raise ValueError("catalog identity must be nonempty")
        factions = tuple(sorted(self.factions_by_template))
        categories = tuple(sorted((name, tuple(sorted(tags))) for name, tags in self.category_tags_by_template))
        if len({name for name, _ in factions}) != len(factions):
            raise ValueError("duplicate catalog faction template")
        if len({name for name, _ in categories}) != len(categories):
            raise ValueError("duplicate catalog category template")
        if any(len(tags) != len(set(tags)) for _, tags in categories):
            raise ValueError("duplicate catalog category tag")
        object.__setattr__(self, "factions_by_template", factions)
        object.__setattr__(self, "category_tags_by_template", categories)


def strategy_feature_sort_key(item: StrategyFeature) -> tuple[str, str, str, int, int, str, str]:
    value = item.value
    return (
        value.name,
        value.scope.scope_type,
        value.scope.scope_key,
        value.window.frame_start,
        value.window.frame_end,
        item.derived_evidence.ref.source_key,
        item.derived_evidence.ref.public_id,
    )


@dataclass(frozen=True)
class StrategyContext:
    replay_public_id: str
    replay_sha256: str
    replay_player_public_id: str | None
    scope: FeatureScope
    window: FeatureWindow
    final_frame: int | None
    player_faction_template_name: str | None
    player_faction_evidence: EvidenceRef | None
    opponent_faction_template_name: str | None
    opponent_faction_evidence: EvidenceRef | None
    map_identity: str | None
    map_identity_evidence: EvidenceRef | None
    catalog: CatalogProof | None
    features: tuple[StrategyFeature, ...]
    settings: CanonicalValue

    def __post_init__(self) -> None:
        if type(self.replay_public_id) is not str or not self.replay_public_id.strip():
            raise ValueError("replay public ID must be nonempty")
        if not _SHA256.fullmatch(self.replay_sha256):
            raise ValueError("replay sha256 must be lower-case hexadecimal")
        if self.window.frame_start < 0 or self.window.frame_end < self.window.frame_start:
            raise ValueError("strategy context window must be inclusive and nonnegative")
        if self.final_frame is not None and (type(self.final_frame) is not int or self.final_frame < 0):
            raise ValueError("final frame must be nonnegative")
        if self.scope.scope_type == "player" and self.replay_player_public_id != self.scope.scope_key:
            raise ValueError("player strategy context scope mismatch")
        for ref in (
            self.player_faction_evidence,
            self.opponent_faction_evidence,
            self.map_identity_evidence,
        ):
            if ref is not None and ref.tier != "observed":
                raise ValueError("strategy context facts require observed evidence")
        object.__setattr__(self, "features", tuple(sorted(self.features, key=strategy_feature_sort_key)))
        object.__setattr__(self, "settings", freeze_canonical(self.settings))


@dataclass(frozen=True)
class PredicateResult:
    predicate: FeaturePredicate
    state: PredicateState
    feature_evidence: tuple[EvidenceRef, ...]
    observed_evidence: tuple[EvidenceRef, ...]
    reason: str | None


@dataclass(frozen=True)
class RuleAssessment:
    strategy_id: str
    phase: StrategyPhase
    window: FeatureWindow
    quality: AssessmentQuality
    rule_score: float | None
    supporting_evidence: tuple[EvidenceRef, ...]
    contradicting_evidence: tuple[EvidenceRef, ...]
    details: CanonicalValue

    def __post_init__(self) -> None:
        if self.rule_score is not None and not 0.0 <= self.rule_score <= 1.0:
            raise ValueError("rule score must be bounded")
        object.__setattr__(self, "supporting_evidence", _deduplicate_refs(self.supporting_evidence))
        object.__setattr__(self, "contradicting_evidence", _deduplicate_refs(self.contradicting_evidence))
        if {ref.public_id for ref in self.supporting_evidence} & {ref.public_id for ref in self.contradicting_evidence}:
            raise ValueError("assessment evidence cannot be both supporting and contradicting")
        object.__setattr__(self, "details", freeze_canonical(self.details))


def _deduplicate_refs(refs: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    by_public_id: dict[str, EvidenceRef] = {}
    by_source: dict[tuple[str, str], EvidenceRef] = {}
    for ref in refs:
        existing = by_public_id.get(ref.public_id)
        source_existing = by_source.get((ref.source_kind, ref.source_key))
        if (existing is not None and existing != ref) or (source_existing is not None and source_existing != ref):
            raise ValueError("conflicting evidence identity")
        by_public_id[ref.public_id] = ref
        by_source[(ref.source_kind, ref.source_key)] = ref
    return tuple(sorted(by_public_id.values(), key=evidence_sort_key))


def _overlaps(left: FeatureWindow, right: FeatureWindow) -> bool:
    return left.frame_start <= right.frame_end and right.frame_start <= left.frame_end


# TheSuperHackers @feature Leex 23/08/2026 Match exact templates nested in immutable feature values. (#TBD)
def _canonical_contains(raw: object, expected: str) -> bool:
    if type(raw) is str:
        return raw == expected
    if isinstance(raw, tuple):
        return any(_canonical_contains(item, expected) for item in raw)
    return False


def _predicate_value(predicate: FeaturePredicate, raw: object) -> bool:
    expected = predicate.expected_value
    if predicate.operator == "eq":
        return (
            raw == expected
            and type(raw) is type(expected)
            or (
                type(raw) in (int, float)
                and type(expected) in (int, float)
                and float(cast(int | float, raw)) == float(expected)
            )
        )
    if predicate.operator == "ne":
        return not _predicate_value(
            FeaturePredicate(
                predicate.predicate_id,
                predicate.feature_name,
                "eq",
                predicate.expected_value,
                predicate.unit,
                predicate.allowed_scope_types,
                predicate.weight,
            ),
            raw,
        )
    if predicate.operator == "lt":
        return cast(int | float, raw) < cast(int | float, expected)
    if predicate.operator == "lte":
        return cast(int | float, raw) <= cast(int | float, expected)
    if predicate.operator == "gt":
        return cast(int | float, raw) > cast(int | float, expected)
    if predicate.operator == "gte":
        return cast(int | float, raw) >= cast(int | float, expected)
    if predicate.operator == "contains":
        return _canonical_contains(raw, cast(str, expected))
    return not _canonical_contains(raw, cast(str, expected))


def _evaluate_predicate(
    predicate: FeaturePredicate, context: StrategyContext, registry: FeatureRegistry
) -> tuple[PredicateResult, AssessmentQuality]:
    named = tuple(item for item in context.features if item.value.name == predicate.feature_name)
    compatible_window = tuple(
        item for item in named if item.value.scope == context.scope and _overlaps(item.value.window, context.window)
    )
    if not compatible_window:
        reason = (
            "incompatible_feature_value"
            if named and any(_overlaps(item.value.window, context.window) for item in named)
            else "missing_feature_value"
        )
        return PredicateResult(predicate, "unavailable", (), (), reason), "unavailable"
    if len(compatible_window) != 1:
        return PredicateResult(predicate, "unavailable", (), (), "ambiguous_feature_value"), "unavailable"
    strategy_feature = compatible_window[0]
    value = strategy_feature.value
    if not value.input_evidence:
        return PredicateResult(predicate, "unavailable", (), (), "missing_direct_observed_evidence"), "unavailable"
    try:
        validated = validate_feature_value(value, registry)
    except (KeyError, TypeError, ValueError):
        return PredicateResult(predicate, "unavailable", (), (), "incompatible_feature_value"), "unavailable"
    if validated.unit != predicate.unit or validated.scope.scope_type not in predicate.allowed_scope_types:
        return PredicateResult(predicate, "unavailable", (), (), "incompatible_feature_value"), "unavailable"
    if validated.quality == "unavailable" or validated.raw_value is None:
        return PredicateResult(predicate, "unavailable", (), (), "insufficient_feature_quality"), "unavailable"
    input_ids = tuple(sorted(ref.public_id for ref in validated.input_evidence))
    if (
        any(ref.tier != "observed" for ref in validated.input_evidence)
        or strategy_feature.derived_evidence.input_evidence_ids != input_ids
    ):
        return PredicateResult(predicate, "unavailable", (), (), "missing_direct_observed_evidence"), "unavailable"
    state: PredicateState = "matched" if _predicate_value(predicate, validated.raw_value) else "not_matched"
    return (
        PredicateResult(
            predicate,
            state,
            (strategy_feature.derived_evidence.ref,),
            _deduplicate_refs(validated.input_evidence),
            None,
        ),
        "partial" if validated.quality == "partial" else "available",
    )


def _applicability(
    definition: StrategyDefinition, context: StrategyContext
) -> tuple[bool, str | None, dict[str, object]]:
    if definition.fallback:
        return True, None, {"status": "fallback"}
    catalog = context.catalog
    if (
        catalog is None
        or context.player_faction_template_name is None
        or context.opponent_faction_template_name is None
    ):
        return False, "missing_catalog_semantics", {"status": "unavailable"}
    if fact(catalog.evidence, "catalog_identity") != catalog.catalog_identity:
        return False, "catalog_identity_mismatch", {"status": "unavailable"}
    factions = {faction for _, faction in catalog.factions_by_template if faction is not None}
    if (
        context.player_faction_evidence is None
        or context.opponent_faction_evidence is None
        or context.player_faction_template_name not in factions
        or context.opponent_faction_template_name not in factions
    ):
        return (
            False,
            "missing_catalog_semantics",
            {"status": "unavailable", "catalog_identity": catalog.catalog_identity},
        )
    if (
        context.player_faction_template_name not in definition.applicability.faction_template_names
        or context.opponent_faction_template_name not in definition.applicability.opponent_faction_template_names
    ):
        return (
            False,
            "missing_catalog_semantics",
            {"status": "not_applicable", "catalog_identity": catalog.catalog_identity},
        )
    if context.map_identity is None or context.map_identity_evidence is None:
        return (
            False,
            "missing_observed_map_identity",
            {"status": "unavailable", "catalog_identity": catalog.catalog_identity},
        )
    if context.map_identity not in definition.applicability.map_identities:
        return (
            False,
            "map_identity_mismatch",
            {
                "status": "unavailable",
                "catalog_identity": catalog.catalog_identity,
                "map_identity": context.map_identity,
            },
        )
    return (
        True,
        None,
        {
            "status": "matched",
            "catalog_identity": catalog.catalog_identity,
            "player_faction_template_name": context.player_faction_template_name,
            "opponent_faction_template_name": context.opponent_faction_template_name,
            "map_identity": context.map_identity,
            "evidence_ids": sorted(
                (
                    catalog.evidence.ref.public_id,
                    context.player_faction_evidence.public_id,
                    context.opponent_faction_evidence.public_id,
                    context.map_identity_evidence.public_id,
                )
            ),
        },
    )


def _predicate_details(result: PredicateResult) -> dict[str, object]:
    return {
        "predicate_id": result.predicate.predicate_id,
        "feature_name": result.predicate.feature_name,
        "operator": result.predicate.operator,
        "expected_value": result.predicate.expected_value,
        "unit": result.predicate.unit,
        "weight": result.predicate.weight,
        "state": result.state,
        "feature_evidence_ids": [ref.public_id for ref in result.feature_evidence],
        "observed_evidence_ids": [ref.public_id for ref in result.observed_evidence],
        "reason": result.reason,
    }


def _unavailable(
    definition: StrategyDefinition,
    window: FeatureWindow,
    reason: str,
    applicability: object,
    predicate_results: tuple[PredicateResult, ...] = (),
) -> RuleAssessment:
    return RuleAssessment(
        definition.strategy_id,
        definition.phase,
        window,
        "unavailable",
        None,
        (),
        (),
        cast(
            CanonicalValue,
            {
                "applicability": applicability,
                "denominator": None,
                "eligible": False,
                "formula_version": "strategy-rule-score-v1",
                "numerator": None,
                "predicates": [_predicate_details(result) for result in predicate_results],
                "reason": reason,
                "required_predicate_ids": [item.predicate_id for item in definition.required],
                "rule_score": None,
                "score_kind": "transparent_rule_score_not_probability",
            },
        ),
    )


# TheSuperHackers @feature Leex 22/08/2026 Evaluate strategy labels only from explicit compatible feature and observed evidence. (#TBD)
def evaluate_rule(
    definition: StrategyDefinition, context: StrategyContext, registry: FeatureRegistry
) -> RuleAssessment:
    if definition.fallback:
        if context.final_frame is None:
            return _unavailable(
                definition, FeatureWindow(0, 0), "missing_complete_terminal_record", {"status": "fallback"}
            )
        window = FeatureWindow(0, context.final_frame)
        reason = "missing_successful_features" if not context.features else "no_named_rule_established"
        return _unavailable(definition, window, reason, {"status": "fallback"})

    applicable, applicability_reason, applicability_details = _applicability(definition, context)
    predicates = definition.required + definition.supporting + definition.contradicting
    evaluated = tuple(_evaluate_predicate(predicate, context, registry) for predicate in predicates)
    predicate_results = tuple(item[0] for item in evaluated)
    qualities = tuple(item[1] for item in evaluated)
    if not applicable:
        return _unavailable(
            definition,
            context.window,
            applicability_reason or "missing_catalog_semantics",
            applicability_details,
            predicate_results,
        )
    required_evaluated = evaluated[: len(definition.required)]
    failed_required = tuple(item for item in required_evaluated if item[0].state == "not_matched")
    if failed_required:
        reason = (
            "insufficient_feature_quality"
            if any(quality != "available" for _, quality in failed_required)
            else "required_predicate_not_matched"
        )
        return _unavailable(
            definition,
            context.window,
            reason,
            applicability_details,
            predicate_results,
        )
    unavailable = next((result.reason for result in predicate_results if result.state == "unavailable"), None)
    if unavailable is not None:
        return _unavailable(definition, context.window, unavailable, applicability_details, predicate_results)
    quality: AssessmentQuality = "partial" if "partial" in qualities else "available"
    if not quality_at_least(quality, definition.minimum_quality):
        return _unavailable(
            definition, context.window, "below_minimum_quality", applicability_details, predicate_results
        )

    required_end = len(definition.required)
    supporting_end = required_end + len(definition.supporting)
    numerator = sum(
        result.predicate.weight for result in predicate_results[:supporting_end] if result.state == "matched"
    )
    denominator = sum(result.predicate.weight for result in predicate_results)
    score = numerator / denominator
    applicability_evidence = tuple(
        ref
        for ref in (
            None if context.catalog is None else context.catalog.evidence.ref,
            context.player_faction_evidence,
            context.opponent_faction_evidence,
            context.map_identity_evidence,
        )
        if ref is not None
    )
    supporting = applicability_evidence + tuple(
        ref
        for result in predicate_results[:supporting_end]
        if result.state == "matched"
        for ref in result.feature_evidence + result.observed_evidence
    )
    contradicting = tuple(
        ref
        for result in predicate_results[supporting_end:]
        if result.state == "matched"
        for ref in result.feature_evidence + result.observed_evidence
    )
    return RuleAssessment(
        definition.strategy_id,
        definition.phase,
        context.window,
        quality,
        score,
        supporting,
        contradicting,
        cast(
            CanonicalValue,
            {
                "applicability": applicability_details,
                "denominator": denominator,
                "eligible": True,
                "formula_version": "strategy-rule-score-v1",
                "numerator": numerator,
                "predicates": [_predicate_details(result) for result in predicate_results],
                "reason": None,
                "required_predicate_ids": [item.predicate_id for item in definition.required],
                "rule_score": score,
                "score_kind": "transparent_rule_score_not_probability",
            },
        ),
    )
