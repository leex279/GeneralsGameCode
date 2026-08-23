"""Pure deterministic strategy rule tests."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from generals_replay_analyzer.features.base import FeatureScope, FeatureValue, FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence, evidence_sort_key, thaw_canonical
from generals_replay_analyzer.features.registry import FeatureRegistry
from generals_replay_analyzer.strategy.rules import (
    CatalogProof,
    RuleAssessment,
    StrategyContext,
    StrategyFeature,
    evaluate_rule,
)
from generals_replay_analyzer.strategy.taxonomy import (
    Applicability,
    FeaturePredicate,
    StrategyDefinition,
    default_taxonomy,
    load_taxonomy,
)

from .conftest import MemoryResource

PLAYER = "00000000-0000-4000-8000-000000000250"


def _context(
    *,
    evidence_ref: Callable[..., EvidenceRef],
    features: tuple[StrategyFeature, ...] = (),
    final_frame: int | None = 300,
    window: FeatureWindow | None = None,
    player_faction: str | None = "FactionAmerica",
    opponent_faction: str | None = "FactionChina",
    map_identity: str | None = "maps/test/map.ini",
    catalog_identity: str = "catalog-v1:fixture",
    catalog_evidence_identity: str | None = None,
) -> StrategyContext:
    catalog_ref = evidence_ref(901, source_kind="catalog", source_key="catalog:fixture")
    catalog_observed = ObservedEvidence(
        ref=catalog_ref,
        frame=None,
        event_type="catalog_manifest",
        facts={
            "catalog_identity": catalog_identity if catalog_evidence_identity is None else catalog_evidence_identity
        },
    )
    catalog = CatalogProof(
        catalog_identity=catalog_identity,
        evidence=catalog_observed,
        factions_by_template=(
            ("AmericaTankDozer", "FactionAmerica"),
            ("ChinaDozer", "FactionChina"),
        ),
        category_tags_by_template=(
            ("AmericaTankDozer", ("builder",)),
            ("ChinaDozer", ("builder",)),
        ),
    )
    return StrategyContext(
        replay_public_id="00000000-0000-4000-8000-000000000249",
        replay_sha256="a" * 64,
        replay_player_public_id=PLAYER,
        scope=FeatureScope("player", PLAYER, PLAYER),
        window=FeatureWindow(0, 300) if window is None else window,
        final_frame=final_frame,
        player_faction_template_name=player_faction,
        player_faction_evidence=None if player_faction is None else evidence_ref(902),
        opponent_faction_template_name=opponent_faction,
        opponent_faction_evidence=None if opponent_faction is None else evidence_ref(903),
        map_identity=map_identity,
        map_identity_evidence=None if map_identity is None else evidence_ref(904, source_kind="map_manifest"),
        catalog=catalog,
        features=features,
        settings={"feature_set_public_ids": ["00000000-0000-4000-8000-000000000801"]},
    )


def _named_definition(
    registry: FeatureRegistry,
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
) -> StrategyDefinition:
    return load_taxonomy(taxonomy_resource(), registry).strategies[0]


def test_fallback_without_a_complete_terminal_record_is_unavailable(registry: FeatureRegistry) -> None:
    context = StrategyContext(
        replay_public_id="00000000-0000-4000-8000-000000000249",
        replay_sha256="a" * 64,
        replay_player_public_id=PLAYER,
        scope=FeatureScope("player", PLAYER, PLAYER),
        window=FeatureWindow(0, 0),
        final_frame=None,
        player_faction_template_name=None,
        player_faction_evidence=None,
        opponent_faction_template_name=None,
        opponent_faction_evidence=None,
        map_identity=None,
        map_identity_evidence=None,
        catalog=None,
        features=(),
        settings=(),
    )

    fallback = next(item for item in default_taxonomy(registry).strategies if item.fallback)
    result = evaluate_rule(fallback, context, registry)

    assert result.strategy_id == "unknown_or_mixed"
    assert result.quality == "unavailable"
    assert result.rule_score is None
    assert result.window == FeatureWindow(0, 0)
    assert thaw_canonical(result.details)["reason"] == "missing_complete_terminal_record"  # type: ignore[index]


def test_named_rule_links_derived_and_direct_evidence_and_uses_the_fixed_score_formula(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
) -> None:
    supply = strategy_feature(sequence=1)
    production = strategy_feature(name="production.completed_count", raw_value=3, unit="count", sequence=2)
    damage = strategy_feature(name="combat.applied_damage_taken", raw_value=75.0, unit="damage", sequence=3)
    definition = _named_definition(registry, taxonomy_resource)

    result = evaluate_rule(
        definition,
        _context(evidence_ref=evidence_ref, features=(damage, supply, production)),  # type: ignore[arg-type]
        registry,
    )

    assert result.strategy_id == "catalog_proven_pressure"
    assert result.quality == "available"
    assert result.rule_score == pytest.approx(5.0 / 6.0)
    assert {ref.public_id for ref in result.supporting_evidence} == {
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000101",
        "00000000-0000-4000-8000-000000000102",
        "00000000-0000-4000-8000-000000000901",
        "00000000-0000-4000-8000-000000000902",
        "00000000-0000-4000-8000-000000000903",
        "00000000-0000-4000-8000-000000000904",
    }
    assert result.supporting_evidence == tuple(sorted(result.supporting_evidence, key=evidence_sort_key))
    assert tuple(ref.public_id for ref in result.contradicting_evidence) == (
        "00000000-0000-4000-8000-000000000103",
        "00000000-0000-4000-8000-000000000003",
    )
    details = thaw_canonical(result.details)
    assert details["numerator"] == 5  # type: ignore[index]
    assert details["denominator"] == 6  # type: ignore[index]
    assert details["formula_version"] == "strategy-rule-score-v1"  # type: ignore[index]
    assert details["score_kind"] == "transparent_rule_score_not_probability"  # type: ignore[index]
    assert "probability" not in str(details).replace("transparent_rule_score_not_probability", "")


def test_contains_predicate_finds_an_exact_template_inside_a_canonical_build_sequence(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
) -> None:
    """Catch strategy membership falling back to shallow container comparison."""
    definition = replace(
        _named_definition(registry, taxonomy_resource),
        required=(
            FeaturePredicate(
                "strategy_center_completed",
                "build.completed_sequence",
                "contains",
                "AmericaStrategyCenter",
                "json",
                ("player",),
                3,
            ),
        ),
        supporting=(),
        contradicting=(),
    )
    sequence = strategy_feature(
        name="build.completed_sequence",
        raw_value=(
            {"frame": 180, "template_name": "AmericaSupplyCenter"},
            {"frame": 900, "template_name": "AmericaStrategyCenter"},
        ),
        unit="json",
        sequence=11,
    )

    result = evaluate_rule(
        definition,
        _context(evidence_ref=evidence_ref, features=(sequence,)),  # type: ignore[arg-type]
        registry,
    )

    assert result.quality == "available"
    assert result.rule_score == 1.0
    assert thaw_canonical(result.details)["predicates"][0]["state"] == "matched"  # type: ignore[index]


def test_json_membership_threshold_and_wildcard_applicability_require_two_exact_templates(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
) -> None:
    """Catch dual-production labels accepting one template or one fixed map only."""
    definition = replace(
        _named_definition(registry, taxonomy_resource),
        applicability=Applicability(("FactionAmerica",), ("*",), ("*",)),
        required=(
            FeaturePredicate(
                "two_airfields_completed",
                "build.completed_sequence",
                "contains",
                "AmericaAirfield",
                "json",
                ("player",),
                3,
                2,
            ),
        ),
        supporting=(),
        contradicting=(),
    )
    one = strategy_feature(
        name="build.completed_sequence",
        raw_value=({"frame": 300, "template_name": "AmericaAirfield"},),
        unit="json",
        sequence=21,
    )
    two = strategy_feature(
        name="build.completed_sequence",
        raw_value=(
            {"frame": 300, "template_name": "AmericaAirfield"},
            {"frame": 600, "template_name": "AmericaAirfield"},
        ),
        unit="json",
        sequence=22,
    )

    one_result = evaluate_rule(
        definition,
        _context(evidence_ref=evidence_ref, features=(one,), map_identity="maps/other/map.ini"),  # type: ignore[arg-type]
        registry,
    )
    two_result = evaluate_rule(
        definition,
        _context(evidence_ref=evidence_ref, features=(two,), map_identity="maps/other/map.ini"),  # type: ignore[arg-type]
        registry,
    )

    assert one_result.quality == "unavailable"
    assert two_result.quality == "available"
    assert thaw_canonical(two_result.details)["predicates"][0]["minimum_occurrences"] == 2  # type: ignore[index]


def test_partial_feature_produces_an_exact_partial_rule_score(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
) -> None:
    supply = strategy_feature(sequence=1, quality="partial")
    production = strategy_feature(name="production.completed_count", raw_value=3, unit="count", sequence=2)
    damage = strategy_feature(name="combat.applied_damage_taken", raw_value=10.0, unit="damage", sequence=3)

    result = evaluate_rule(
        _named_definition(registry, taxonomy_resource),
        _context(evidence_ref=evidence_ref, features=(supply, production, damage)),  # type: ignore[arg-type]
        registry,
    )

    assert result.quality == "partial"
    assert result.rule_score == pytest.approx(5.0 / 6.0)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda value: replace(value, unit="count"), "incompatible_feature_value"),
        (lambda value: replace(value, window=FeatureWindow(301, 400)), "missing_feature_value"),
        (lambda value: replace(value, input_evidence=()), "missing_direct_observed_evidence"),
        (
            lambda value: replace(value, raw_value=None, quality="unavailable", quality_reason="fixture_unavailable"),
            "insufficient_feature_quality",
        ),
    ],
)
def test_incompatible_or_insufficient_feature_values_make_a_required_predicate_unavailable(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
    change: Callable[[FeatureValue], FeatureValue],
    reason: str,
) -> None:
    original = strategy_feature(sequence=1)
    assert isinstance(original, StrategyFeature)
    changed = replace(original, value=change(original.value))

    result = evaluate_rule(
        _named_definition(registry, taxonomy_resource),
        _context(evidence_ref=evidence_ref, features=(changed,)),
        registry,
    )

    assert result.quality == "unavailable"
    assert result.rule_score is None
    assert reason in str(thaw_canonical(result.details))


def test_inclusive_window_boundary_is_eligible_but_duplicate_exact_values_are_ambiguous(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
) -> None:
    at_boundary = strategy_feature(sequence=1, frame_start=300, frame_end=400)
    definition = _named_definition(registry, taxonomy_resource)

    boundary_result = evaluate_rule(
        definition,
        _context(evidence_ref=evidence_ref, features=(at_boundary,)),  # type: ignore[arg-type]
        registry,
    )
    ambiguous_result = evaluate_rule(
        definition,
        _context(evidence_ref=evidence_ref, features=(at_boundary, strategy_feature(sequence=2))),  # type: ignore[arg-type]
        registry,
    )

    boundary_details = thaw_canonical(boundary_result.details)
    assert boundary_details["predicates"][0]["state"] == "matched"  # type: ignore[index]
    assert ambiguous_result.quality == "unavailable"
    assert "ambiguous_feature_value" in str(thaw_canonical(ambiguous_result.details))


@pytest.mark.parametrize(
    ("context_change", "reason"),
    [
        ({"catalog_evidence_identity": "catalog-v1:other"}, "catalog_identity_mismatch"),
        ({"player_faction": "factionamerica"}, "missing_catalog_semantics"),
        ({"map_identity": "Maps/Test/Map.ini"}, "map_identity_mismatch"),
        ({"map_identity": None}, "missing_observed_map_identity"),
    ],
)
def test_applicability_requires_exact_catalog_faction_and_map_evidence_without_normalization(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
    context_change: dict[str, object],
    reason: str,
) -> None:
    result = evaluate_rule(
        _named_definition(registry, taxonomy_resource),
        _context(evidence_ref=evidence_ref, features=(strategy_feature(sequence=1),), **context_change),  # type: ignore[arg-type]
        registry,
    )

    assert result.quality == "unavailable"
    assert result.rule_score is None
    assert reason in str(thaw_canonical(result.details))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"catalog_identity": ""}, "identity"),
        ({"factions_by_template": (("Template", "FactionAmerica"), ("Template", "FactionChina"))}, "faction"),
        ({"category_tags_by_template": (("Template", ("a",)), ("Template", ("b",)))}, "category template"),
        ({"category_tags_by_template": (("Template", ("same", "same")),)}, "category tag"),
    ],
)
def test_catalog_proof_rejects_ambiguous_or_unstable_semantics(
    evidence_ref: Callable[..., EvidenceRef], change: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "catalog_identity": "catalog-v1:fixture",
        "evidence": ObservedEvidence(
            evidence_ref(1), None, "catalog_manifest", {"catalog_identity": "catalog-v1:fixture"}
        ),
        "factions_by_template": (("Template", "FactionAmerica"),),
        "category_tags_by_template": (("Template", ("builder",)),),
    }
    values.update(change)

    with pytest.raises(ValueError, match=message):
        CatalogProof(**values)  # type: ignore[arg-type]


def test_strategy_context_rejects_nonsemantic_identity_window_and_evidence(
    evidence_ref: Callable[..., EvidenceRef],
) -> None:
    valid = _context(evidence_ref=evidence_ref)
    bad_fact = evidence_ref(50, tier="derived")

    for change, message in (
        ({"replay_public_id": ""}, "public ID"),
        ({"replay_sha256": "A" * 64}, "sha256"),
        ({"window": FeatureWindow(-1, 0)}, "window"),
        ({"final_frame": -1}, "final frame"),
        ({"replay_player_public_id": "other"}, "scope"),
        ({"map_identity_evidence": bad_fact}, "observed"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(valid, **change)


def test_rule_assessment_rejects_unbounded_and_self_contradictory_scores(
    evidence_ref: Callable[..., EvidenceRef],
) -> None:
    observed = evidence_ref(1)
    with pytest.raises(ValueError, match="bounded"):
        RuleAssessment("fixture", "early", FeatureWindow(0, 1), "available", 1.1, (), (), {})
    with pytest.raises(ValueError, match="both supporting and contradicting"):
        RuleAssessment("fixture", "early", FeatureWindow(0, 1), "available", 0.5, (observed,), (observed,), {})
    conflicting = replace(observed, source_key="observed:other")
    with pytest.raises(ValueError, match="conflicting"):
        RuleAssessment("fixture", "early", FeatureWindow(0, 1), "available", 0.5, (observed, conflicting), (), {})


def test_derived_feature_must_name_the_exact_direct_observed_inputs(
    registry: FeatureRegistry,
    evidence_ref: Callable[..., EvidenceRef],
    strategy_feature: Callable[..., object],
    taxonomy_resource: Callable[[dict[str, object] | bytes | None], MemoryResource],
) -> None:
    original = strategy_feature(sequence=1)
    assert isinstance(original, StrategyFeature)
    invalid = replace(original, derived_evidence=replace(original.derived_evidence, input_evidence_ids=("other",)))

    result = evaluate_rule(
        _named_definition(registry, taxonomy_resource),
        _context(evidence_ref=evidence_ref, features=(invalid,)),
        registry,
    )

    assert "missing_direct_observed_evidence" in str(thaw_canonical(result.details))
