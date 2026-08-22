"""Stable strategy candidate fan-out and canonical cache identities."""

from __future__ import annotations

import hashlib
import re
from typing import cast

from generals_replay_analyzer.features.base import FeatureWindow
from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.evidence import CanonicalValue, EvidenceRef, evidence_sort_key, thaw_canonical
from generals_replay_analyzer.features.registry import FeatureRegistry
from generals_replay_analyzer.strategy.rules import RuleAssessment, StrategyContext, evaluate_rule
from generals_replay_analyzer.strategy.taxonomy import StrategyDefinition, StrategyTaxonomy

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FALLBACK_REASON_ORDER = (
    "missing_successful_features",
    "missing_complete_terminal_record",
    "missing_catalog_semantics",
    "catalog_identity_mismatch",
    "missing_observed_map_identity",
    "map_identity_mismatch",
    "insufficient_feature_quality",
    "ambiguous_feature_value",
    "no_named_rule_established",
)


def _details(value: RuleAssessment) -> dict[str, object]:
    thawed = thaw_canonical(value.details)
    if not isinstance(thawed, dict):
        raise TypeError("rule details must be a canonical object")
    return cast(dict[str, object], thawed)


def _eligible(value: RuleAssessment) -> bool:
    return _details(value).get("eligible") is True


def _fallback_definition(taxonomy: StrategyTaxonomy) -> StrategyDefinition:
    fallback = tuple(item for item in taxonomy.strategies if item.fallback)
    if len(fallback) != 1 or fallback[0].strategy_id != "unknown_or_mixed":
        raise ValueError("taxonomy requires exactly one unknown_or_mixed fallback")
    return fallback[0]


def _context_refs(context: StrategyContext) -> dict[str, EvidenceRef]:
    refs = [
        ref for feature in context.features for ref in (feature.derived_evidence.ref,) + feature.value.input_evidence
    ]
    refs.extend(
        ref
        for ref in (
            context.player_faction_evidence,
            context.opponent_faction_evidence,
            context.map_identity_evidence,
            None if context.catalog is None else context.catalog.evidence.ref,
        )
        if ref is not None
    )
    result: dict[str, EvidenceRef] = {}
    for ref in refs:
        if ref.public_id in result and result[ref.public_id] != ref:
            raise ValueError("conflicting strategy context evidence identity")
        result[ref.public_id] = ref
    return result


def _decisive_evidence(named: tuple[RuleAssessment, ...], context: StrategyContext) -> tuple[EvidenceRef, ...]:
    refs = _context_refs(context)
    public_ids: set[str] = set()
    for assessment in named:
        details = _details(assessment)
        if details.get("reason") != "required_predicate_not_matched":
            continue
        required_predicate_ids = details.get("required_predicate_ids")
        if not isinstance(required_predicate_ids, list) or not all(
            type(item) is str for item in required_predicate_ids
        ):
            continue
        required_ids = set(cast(list[str], required_predicate_ids))
        predicates = details.get("predicates")
        if not isinstance(predicates, list):
            continue
        for predicate in predicates:
            if (
                not isinstance(predicate, dict)
                or predicate.get("predicate_id") not in required_ids
                or predicate.get("state") != "not_matched"
            ):
                continue
            for key in ("feature_evidence_ids", "observed_evidence_ids"):
                values = predicate.get(key)
                if isinstance(values, list) and all(type(item) is str for item in values):
                    public_ids.update(cast(list[str], values))
    return tuple(sorted((refs[public_id] for public_id in public_ids), key=evidence_sort_key))


def _fallback_result(
    definition: StrategyDefinition,
    context: StrategyContext,
    named: tuple[RuleAssessment, ...],
) -> RuleAssessment:
    if context.final_frame is None:
        reason = "missing_complete_terminal_record"
        window = FeatureWindow(0, 0)
    else:
        window = FeatureWindow(0, context.final_frame)
        if not context.features:
            reason = "missing_successful_features"
        elif named and all(_details(item).get("reason") == "required_predicate_not_matched" for item in named):
            decisive = _decisive_evidence(named, context)
            return RuleAssessment(
                definition.strategy_id,
                definition.phase,
                window,
                "available",
                None,
                decisive,
                (),
                cast(
                    CanonicalValue,
                    {
                        "denominator": None,
                        "eligible": True,
                        "formula_version": "strategy-rule-score-v1",
                        "numerator": None,
                        "reason": "no_named_rule_established",
                        "rule_score": None,
                        "score_kind": "transparent_rule_score_not_probability",
                    },
                ),
            )
        elif not named:
            reason = "no_named_rule_established"
        else:
            reasons = {cast(str, _details(item).get("reason")) for item in named}
            if "missing_feature_value" in reasons or "missing_direct_observed_evidence" in reasons:
                reasons.add("insufficient_feature_quality")
            reason = next((item for item in _FALLBACK_REASON_ORDER if item in reasons), "no_named_rule_established")
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
                "denominator": None,
                "eligible": False,
                "formula_version": "strategy-rule-score-v1",
                "numerator": None,
                "reason": reason,
                "rule_score": None,
                "score_kind": "transparent_rule_score_not_probability",
            },
        ),
    )


def _conflict_evidence(named: tuple[RuleAssessment, ...]) -> tuple[EvidenceRef, ...]:
    supporting = {ref.public_id: ref for assessment in named for ref in assessment.supporting_evidence}
    contradicting = {ref.public_id: ref for assessment in named for ref in assessment.contradicting_evidence}
    return tuple(
        sorted(
            (supporting[public_id] for public_id in supporting.keys() & contradicting.keys()),
            key=evidence_sort_key,
        )
    )


def _mixed_fallback(
    definition: StrategyDefinition,
    context: StrategyContext,
    named: tuple[RuleAssessment, ...],
    evidence: tuple[EvidenceRef, ...],
) -> RuleAssessment:
    if context.final_frame is None:
        return _fallback_result(definition, context, named)
    return RuleAssessment(
        definition.strategy_id,
        definition.phase,
        FeatureWindow(0, context.final_frame),
        "partial",
        None,
        evidence,
        (),
        cast(
            CanonicalValue,
            {
                "candidate_ids": [item.strategy_id for item in named],
                "conflict_evidence_ids": [item.public_id for item in evidence],
                "denominator": None,
                "eligible": True,
                "formula_version": "strategy-rule-score-v1",
                "numerator": None,
                "reason": "contradictory_supported_candidates",
                "rule_score": None,
                "score_kind": "transparent_rule_score_not_probability",
            },
        ),
    )


def strategy_definition_digests(taxonomy: StrategyTaxonomy, registry: FeatureRegistry) -> tuple[str, str]:
    registry_content_sha256 = hashlib.sha256(canonical_json(registry.definitions).encode("utf-8")).hexdigest()
    taxonomy_rules_sha256 = hashlib.sha256(
        canonical_json(
            {
                "schema_version": taxonomy.schema_version,
                "strategies": taxonomy.strategies,
                "taxonomy_version": taxonomy.taxonomy_version,
            }
        ).encode("utf-8")
    ).hexdigest()
    return registry_content_sha256, taxonomy_rules_sha256


# TheSuperHackers @feature Leex 22/08/2026 Return every evidence-eligible rule in stable order without guessing a winner. (#TBD)
def assess_candidates(
    context: StrategyContext, taxonomy: StrategyTaxonomy, registry: FeatureRegistry
) -> tuple[RuleAssessment, ...]:
    fallback = _fallback_definition(taxonomy)
    named = tuple(evaluate_rule(item, context, registry) for item in taxonomy.strategies if not item.fallback)
    eligible = tuple(
        sorted((item for item in named if _eligible(item)), key=lambda item: (item.phase, item.strategy_id))
    )
    if eligible:
        conflict_evidence = _conflict_evidence(eligible)
        if conflict_evidence:
            return eligible + (_mixed_fallback(fallback, context, eligible, conflict_evidence),)
        return eligible
    return (_fallback_result(fallback, context, named),)


# TheSuperHackers @feature Leex 22/08/2026 Bind cache keys only to canonical immutable strategy inputs and versions. (#TBD)
def strategy_cache_identity(
    context: StrategyContext, taxonomy: StrategyTaxonomy, registry: FeatureRegistry
) -> tuple[str, str]:
    if taxonomy.schema_version != "strategy-taxonomy-v1" or not _SHA256.fullmatch(taxonomy.content_sha256):
        raise ValueError("invalid strategy taxonomy identity")
    digest = hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()
    registry_content_sha256, taxonomy_rules_sha256 = strategy_definition_digests(taxonomy, registry)
    identity = {
        "cache_schema": "strategy-rule-cache-v1",
        "context": context,
        "registry_content_sha256": registry_content_sha256,
        "registry_schema": registry.schema_version,
        "taxonomy": {
            "schema_version": taxonomy.schema_version,
            "version": taxonomy.taxonomy_version,
            "content_sha256": taxonomy.content_sha256,
            "rules_sha256": taxonomy_rules_sha256,
        },
        "evaluator": {"name": "strategy-rule", "version": "strategy-rule-v1"},
    }
    return digest, hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
