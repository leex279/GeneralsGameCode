"""Candidate fan-out, fallback, and semantic cache identity tests."""

import copy
import itertools
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest

from generals_replay_analyzer.features.base import FeatureScope, FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef, thaw_canonical
from generals_replay_analyzer.features.registry import FeatureRegistry
from generals_replay_analyzer.strategy.candidates import (
    assess_candidates,
    strategy_cache_identity,
    strategy_definition_digests,
)
from generals_replay_analyzer.strategy.rules import StrategyContext, StrategyFeature
from generals_replay_analyzer.strategy.taxonomy import StrategyTaxonomy, default_taxonomy, load_taxonomy

from .conftest import MemoryResource
from .test_rules import _context


def _named_context(
    evidence_ref: Callable[..., EvidenceRef], strategy_feature: Callable[..., object]
) -> StrategyContext:
    return _context(
        evidence_ref=evidence_ref,
        features=cast(
            tuple[StrategyFeature, ...],
            (
                strategy_feature(sequence=1),
                strategy_feature(name="production.completed_count", raw_value=3, unit="count", sequence=2),
                strategy_feature(name="combat.applied_damage_taken", raw_value=75.0, unit="damage", sequence=3),
            ),
        ),
    )


def test_named_candidates_are_stable_sorted_and_fallback_is_not_forced(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)

    assessments = assess_candidates(_named_context(evidence_ref, strategy_feature), taxonomy, registry)

    assert tuple(item.strategy_id for item in assessments) == ("catalog_proven_pressure",)
    assert assessments[0].rule_score == pytest.approx(5.0 / 6.0)
    assert thaw_canonical(assessments[0].details)["score_kind"] == "transparent_rule_score_not_probability"  # type: ignore[index]


@pytest.mark.parametrize(
    "scenario_name",
    ["fast_production_pressure", "economic_expansion", "defensive_posture", "tech_transition"],
)
def test_generic_feature_scenarios_never_invent_a_gameplay_label(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    scenario_name: str,
) -> None:
    generic = cast(
        tuple[StrategyFeature, ...],
        (
            strategy_feature(name="production.completed_count", raw_value=20, unit="count", sequence=11),
            strategy_feature(name="economy.supply_collected_total", raw_value=50_000.0, unit="credits", sequence=12),
            strategy_feature(name="build.completed_count", raw_value=30, unit="count", sequence=13),
            strategy_feature(name="state.entity_transition_count", raw_value=100, unit="count", sequence=14),
        ),
    )
    context = replace(_context(evidence_ref=evidence_ref, features=generic), settings={"scenario": scenario_name})

    assessments = assess_candidates(context, default_taxonomy(registry), registry)

    assert tuple(item.strategy_id for item in assessments) == ("unknown_or_mixed",)
    assert assessments[0].quality == "unavailable"
    assert assessments[0].rule_score is None
    assert thaw_canonical(assessments[0].details)["reason"] == "no_named_rule_established"  # type: ignore[index]


def test_missing_catalog_semantics_or_required_evidence_returns_only_an_unavailable_fallback(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    no_catalog = replace(_named_context(evidence_ref, strategy_feature), catalog=None)
    no_features = replace(_context(evidence_ref=evidence_ref), features=())

    missing_catalog = assess_candidates(no_catalog, taxonomy, registry)
    missing_features = assess_candidates(no_features, taxonomy, registry)

    assert tuple(item.strategy_id for item in missing_catalog) == ("unknown_or_mixed",)
    assert thaw_canonical(missing_catalog[0].details)["reason"] == "missing_catalog_semantics"  # type: ignore[index]
    assert thaw_canonical(missing_features[0].details)["reason"] == "missing_successful_features"  # type: ignore[index]
    assert missing_catalog[0].supporting_evidence == missing_catalog[0].contradicting_evidence == ()


def test_candidate_and_cache_outputs_are_identical_under_one_hundred_feature_permutations(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    original_features = cast(
        tuple[StrategyFeature, ...],
        (
            strategy_feature(sequence=1),
            strategy_feature(name="production.completed_count", raw_value=3, unit="count", sequence=2),
            strategy_feature(name="combat.applied_damage_taken", raw_value=75.0, unit="damage", sequence=3),
            strategy_feature(name="activity.supported_order_action_count", raw_value=9, unit="count", sequence=4),
            strategy_feature(name="economy.cash_balance_final", raw_value=500.0, unit="credits", sequence=5),
        ),
    )
    features = tuple(
        replace(
            feature,
            value=replace(
                feature.value,
                input_evidence=feature.value.input_evidence + (evidence_ref(700 + index),),
            ),
            derived_evidence=replace(
                feature.derived_evidence,
                input_evidence_ids=tuple(
                    ref.public_id for ref in feature.value.input_evidence + (evidence_ref(700 + index),)
                ),
            ),
        )
        for index, feature in enumerate(original_features)
    )
    baseline = _context(evidence_ref=evidence_ref, features=features)
    expected_assessments = assess_candidates(baseline, taxonomy, registry)
    expected_identity = strategy_cache_identity(baseline, taxonomy, registry)

    for case_index, order in enumerate(itertools.islice(itertools.permutations(features), 100)):
        evidence_permuted = tuple(
            replace(
                feature,
                value=replace(
                    feature.value,
                    input_evidence=(
                        tuple(reversed(feature.value.input_evidence))
                        if (case_index + feature_index) % 2 == 0
                        else feature.value.input_evidence
                    ),
                ),
            )
            for feature_index, feature in enumerate(order)
        )
        context = replace(baseline, features=evidence_permuted)
        assert assess_candidates(context, taxonomy, registry) == expected_assessments
        assert strategy_cache_identity(context, taxonomy, registry) == expected_identity


def test_cache_identity_changes_for_every_semantic_context_or_version_change(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    context = _named_context(evidence_ref, strategy_feature)
    baseline = strategy_cache_identity(context, taxonomy, registry)
    original = context.features[1]
    changed_ref = EvidenceRef(
        "00000000-0000-4000-8000-000000000777",
        "observed",
        "observed",
        "observed:fixture:777",
        "observed-v1",
    )
    changed_value = replace(original.value, raw_value=151.0)
    changed_evidence_value = replace(original.value, input_evidence=(changed_ref,))
    changed_derived = replace(original.derived_evidence, input_evidence_ids=(changed_ref.public_id,))
    semantic_changes = (
        replace(context, features=(replace(original, value=changed_value),) + context.features[1:]),
        replace(
            context,
            features=(replace(original, value=changed_evidence_value, derived_evidence=changed_derived),)
            + context.features[1:],
        ),
        replace(context, window=FeatureWindow(0, 299)),
        replace(context, settings={"feature_set_public_ids": ["00000000-0000-4000-8000-000000000802"]}),
        replace(context, map_identity="maps/test/other.ini"),
        replace(context, player_faction_template_name="FactionChina"),
    )

    for changed in semantic_changes:
        assert strategy_cache_identity(changed, taxonomy, registry) != baseline
    assert (
        strategy_cache_identity(context, replace(taxonomy, taxonomy_version="strategy-taxonomy-v1.0.1"), registry)
        != baseline
    )
    assert strategy_cache_identity(context, replace(taxonomy, content_sha256="b" * 64), registry) != baseline


def test_cache_identity_rejects_invalid_taxonomy_digest(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
) -> None:
    invalid = replace(default_taxonomy(registry), content_sha256="A" * 64)

    with pytest.raises(ValueError, match="taxonomy"):
        strategy_cache_identity(_named_context(evidence_ref, strategy_feature), invalid, registry)


def test_context_scope_changes_input_digest_and_complete_cache_key(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    context = _named_context(evidence_ref, strategy_feature)

    changed = strategy_cache_identity(
        replace(context, scope=FeatureScope("replay", context.replay_public_id)),
        taxonomy,
        registry,
    )

    assert changed[0] != strategy_cache_identity(context, taxonomy, registry)[0]
    assert changed[1] != strategy_cache_identity(context, taxonomy, registry)[1]


def test_registry_schema_and_definition_fingerprint_each_change_the_complete_cache_key(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    context = _named_context(evidence_ref, strategy_feature)
    baseline = strategy_cache_identity(context, taxonomy, registry)
    fewer_definitions = FeatureRegistry(registry.schema_version, registry.definitions[:-1])
    future_schema = object.__new__(FeatureRegistry)
    object.__setattr__(future_schema, "schema_version", "feature-registry-v2")
    object.__setattr__(future_schema, "definitions", registry.definitions)

    definition_changed = strategy_cache_identity(context, taxonomy, fewer_definitions)
    schema_changed = strategy_cache_identity(context, taxonomy, future_schema)

    assert definition_changed[0] == schema_changed[0] == baseline[0]
    assert len({baseline[1], definition_changed[1], schema_changed[1]}) == 3


def test_taxonomy_semantic_content_changes_cache_even_under_a_declared_digest_collision(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    context = _named_context(evidence_ref, strategy_feature)
    first = taxonomy.strategies[0]
    colliding = replace(
        taxonomy,
        strategies=(replace(first, display_name="Changed canonical taxonomy content"),) + taxonomy.strategies[1:],
    )

    baseline = strategy_cache_identity(context, taxonomy, registry)
    changed = strategy_cache_identity(context, colliding, registry)
    _, baseline_rules_sha256 = strategy_definition_digests(taxonomy, registry)
    _, changed_rules_sha256 = strategy_definition_digests(colliding, registry)

    assert colliding.content_sha256 == taxonomy.content_sha256
    assert baseline_rules_sha256 == taxonomy.content_sha256
    assert changed_rules_sha256 != baseline_rules_sha256
    assert changed[0] == baseline[0]
    assert changed[1] != baseline[1]


def test_required_predicate_not_matched_suppresses_the_named_candidate(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    low_supply = strategy_feature(raw_value=99.0, sequence=1)
    context = _context(evidence_ref=evidence_ref, features=(low_supply,))  # type: ignore[arg-type]

    assessments = assess_candidates(context, taxonomy, registry)

    assert tuple(item.strategy_id for item in assessments) == ("unknown_or_mixed",)
    assert assessments[0].quality == "available"
    assert assessments[0].rule_score is None


def test_partial_required_failure_cannot_upgrade_the_fallback_to_available(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    low_supply = strategy_feature(raw_value=99.0, quality="partial", sequence=1)
    context = _context(evidence_ref=evidence_ref, features=(low_supply,))  # type: ignore[arg-type]

    fallback = assess_candidates(context, taxonomy, registry)[0]

    assert fallback.strategy_id == "unknown_or_mixed"
    assert fallback.quality == "unavailable"
    assert fallback.supporting_evidence == fallback.contradicting_evidence == ()
    assert thaw_canonical(fallback.details)["reason"] == "insufficient_feature_quality"  # type: ignore[index]


def test_available_fallback_cites_only_complete_decisive_required_failures(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)
    features = cast(
        tuple[StrategyFeature, ...],
        (
            strategy_feature(raw_value=99.0, sequence=1),
            strategy_feature(name="production.completed_count", raw_value=1, unit="count", sequence=2),
            strategy_feature(name="combat.applied_damage_taken", raw_value=10.0, unit="damage", sequence=3),
        ),
    )

    fallback = assess_candidates(_context(evidence_ref=evidence_ref, features=features), taxonomy, registry)[0]

    assert fallback.quality == "available"
    assert {ref.public_id for ref in fallback.supporting_evidence} == {
        "00000000-0000-4000-8000-000000000101",
        "00000000-0000-4000-8000-000000000001",
    }
    assert fallback.contradicting_evidence == ()


def test_missing_terminal_record_takes_precedence_and_uses_the_empty_window_sentinel(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
) -> None:
    context = replace(_named_context(evidence_ref, strategy_feature), final_frame=None)

    result = assess_candidates(context, default_taxonomy(registry), registry)[0]

    assert result.window == FeatureWindow(0, 0)
    assert thaw_canonical(result.details)["reason"] == "missing_complete_terminal_record"  # type: ignore[index]


def test_two_independent_named_candidates_are_sorted_by_phase_and_strategy_id(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_document: dict[str, Any],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    document = copy.deepcopy(taxonomy_document)
    second = copy.deepcopy(document["strategies"][0])
    second.update(
        strategy_id="catalog_proven_activity",
        display_name="Catalog-proven activity",
        phase="opening",
        rule_version="catalog-proven-activity-v1",
        required=[
            {
                "allowed_scope_types": ["player"],
                "expected_value": 5,
                "feature_name": "activity.supported_order_action_count",
                "operator": "gte",
                "predicate_id": "activity_floor",
                "unit": "count",
                "weight": 1,
            }
        ],
        supporting=[],
        contradicting=[],
        synonyms=[],
    )
    document["strategies"] = sorted(
        [document["strategies"][0], second, document["strategies"][1]], key=lambda item: item["strategy_id"]
    )
    taxonomy: StrategyTaxonomy = load_taxonomy(taxonomy_resource(document), registry)
    context = _named_context(evidence_ref, strategy_feature)
    context = replace(
        context,
        features=context.features
        + cast(
            tuple[StrategyFeature, ...],
            (strategy_feature(name="activity.supported_order_action_count", raw_value=9, unit="count", sequence=4),),
        ),
    )

    assessments = assess_candidates(context, taxonomy, registry)

    assert tuple((item.phase, item.strategy_id) for item in assessments) == (
        ("early", "catalog_proven_pressure"),
        ("opening", "catalog_proven_activity"),
    )


def test_conflicting_supported_candidates_also_emit_a_cited_partial_mixed_fallback(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_document: dict[str, Any],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    document = copy.deepcopy(taxonomy_document)
    second = copy.deepcopy(document["strategies"][0])
    second.update(
        strategy_id="catalog_proven_activity",
        display_name="Catalog-proven activity",
        phase="opening",
        rule_version="catalog-proven-activity-v1",
        required=[
            {
                "allowed_scope_types": ["player"],
                "expected_value": 5,
                "feature_name": "activity.supported_order_action_count",
                "operator": "gte",
                "predicate_id": "activity_floor",
                "unit": "count",
                "weight": 1,
            }
        ],
        supporting=[],
        contradicting=[
            {
                "allowed_scope_types": ["player"],
                "expected_value": 2,
                "feature_name": "production.completed_count",
                "operator": "gte",
                "predicate_id": "production_conflict",
                "unit": "count",
                "weight": 1,
            }
        ],
        synonyms=[],
    )
    document["strategies"] = sorted(
        [document["strategies"][0], second, document["strategies"][1]], key=lambda item: item["strategy_id"]
    )
    taxonomy = load_taxonomy(taxonomy_resource(document), registry)
    context = _named_context(evidence_ref, strategy_feature)
    context = replace(
        context,
        window=FeatureWindow(100, 200),
        features=context.features
        + cast(
            tuple[StrategyFeature, ...],
            (strategy_feature(name="activity.supported_order_action_count", raw_value=9, unit="count", sequence=4),),
        ),
    )

    assessments = assess_candidates(context, taxonomy, registry)

    assert tuple((item.phase, item.strategy_id) for item in assessments) == (
        ("early", "catalog_proven_pressure"),
        ("opening", "catalog_proven_activity"),
        ("cross_phase", "unknown_or_mixed"),
    )
    mixed = assessments[-1]
    assert mixed.quality == "partial"
    assert mixed.rule_score is None
    assert mixed.window == FeatureWindow(0, 300)
    assert mixed.supporting_evidence
    assert mixed.contradicting_evidence == ()
    assert thaw_canonical(mixed.details)["reason"] == "contradictory_supported_candidates"  # type: ignore[index]

    without_terminal = assess_candidates(replace(context, final_frame=None), taxonomy, registry)[-1]
    assert without_terminal.strategy_id == "unknown_or_mixed"
    assert without_terminal.window == FeatureWindow(0, 0)
    assert without_terminal.quality == "unavailable"
    assert without_terminal.supporting_evidence == without_terminal.contradicting_evidence == ()
    assert thaw_canonical(without_terminal.details)["reason"] == "missing_complete_terminal_record"  # type: ignore[index]
