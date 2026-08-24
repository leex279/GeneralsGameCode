"""Directly attributed observed combat feature tests."""

from collections.abc import Callable

import pytest

from generals_replay_analyzer.features.combat import CombatExtractor
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import ObservedEvidence, thaw_canonical


def _values(context: FeatureContext) -> dict[str, object]:
    return {value.name: value for value in CombatExtractor().extract(context).values}


def test_combat_sums_direct_dealt_taken_and_explicit_killing_blows_without_cost_valuation(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    other = "00000000-0000-4000-8000-000000000280"

    def damage(public_id: str, amount: float, sources: object, victim: str, killing: bool) -> ObservedEvidence:
        return observed(
            public_id=public_id,
            source_key=f"telemetry:{public_id}",
            event_type="damage_applied",
            facts={
                "applied_amount": amount,
                "source_replay_player_public_ids": sources,
                "victim_replay_player_public_id": victim,
                "killing_blow": killing,
                "unit_cost": 999999,
            },
        )

    values = _values(
        player_context(
            damage("00000000-0000-4000-8000-000000000281", 30.5, [player], other, True),
            damage("00000000-0000-4000-8000-000000000282", 20.25, [other], player, False),
            damage("00000000-0000-4000-8000-000000000283", 50.0, [player, other], other, True),
            damage("00000000-0000-4000-8000-000000000284", 10.25, [], player, False),
        )
    )
    assert values["combat.applied_damage_dealt"].raw_value == 30.5  # type: ignore[attr-defined]
    assert values["combat.applied_damage_taken"].raw_value == 30.5  # type: ignore[attr-defined]
    assert values["combat.killing_blow_count"].raw_value == 1  # type: ignore[attr-defined]
    assert values["combat.observed_damage_trade_ratio"].raw_value == 1.0  # type: ignore[attr-defined]
    assert "cost" not in str(thaw_canonical(values["combat.observed_damage_trade_ratio"].details)).lower()  # type: ignore[attr-defined]


def test_combat_excludes_ambiguous_sources_and_reports_zero_denominator_without_fabricating_loss(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    ambiguous = observed(
        event_type="damage_applied",
        facts={
            "applied_amount": 5,
            "source_replay_player_public_ids": [player, "00000000-0000-4000-8000-000000000280"],
            "victim_replay_player_public_id": "00000000-0000-4000-8000-000000000281",
            "killing_blow": True,
        },
    )
    values = _values(player_context(ambiguous))
    assert values["combat.applied_damage_dealt"].raw_value == 0  # type: ignore[attr-defined]
    assert values["combat.applied_damage_taken"].raw_value == 0  # type: ignore[attr-defined]
    assert values["combat.killing_blow_count"].raw_value == 0  # type: ignore[attr-defined]
    assert values["combat.observed_damage_trade_ratio"].raw_value is None  # type: ignore[attr-defined]
    assert values["combat.observed_damage_trade_ratio"].quality_reason == "zero_trade_denominator"  # type: ignore[attr-defined]

    missing = _values(player_context(telemetry_status=None))
    assert all(value.quality_reason == "missing_successful_telemetry" for value in missing.values())  # type: ignore[attr-defined]


def test_combat_keeps_unqualified_killing_blows_out_of_strategic_turning_points(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    item = observed(
        event_type="damage_applied",
        frame=210,
        facts={
            "source_replay_player_public_ids": [player],
            "victim_replay_player_public_id": "00000000-0000-4000-8000-000000000251",
            "applied_amount": 100.0,
            "killing_blow": True,
            "victim_template_name": "ChinaWarFactory",
            "attacker_template_name": "AmericaVehicleHumvee",
        },
    )
    values = _values(player_context(item))
    assert thaw_canonical(values["combat.observed_kill_timing"].raw_value) == [  # type: ignore[attr-defined]
        {
            "frame": 210,
            "attacker_template_name": "AmericaVehicleHumvee",
            "victim_template_name": "ChinaWarFactory",
        }
    ]
    assert values["combat.turning_point_timing"].raw_value is None  # type: ignore[attr-defined]
    assert values["combat.turning_point_timing"].quality_reason == "insufficient_engagement_evidence"  # type: ignore[attr-defined]


def test_combat_marks_a_kill_as_engagement_swing_only_with_versioned_supporting_evidence(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    enemy = "00000000-0000-4000-8000-000000000251"
    kill = observed(
        public_id="00000000-0000-0000-0000-000000000291", source_key="telemetry:turning:291", frame=210, event_type="damage_applied",
        facts={
            "source_replay_player_public_ids": [player], "victim_replay_player_public_id": enemy,
            "attacker_object_id": 10, "victim_object_id": 20,
            "applied_amount": 100.0, "killing_blow": True,
            "victim_template_name": "ChinaWarFactory", "attacker_template_name": "AmericaVehicleHumvee",
            "location": {"x": 10.0, "y": 20.0, "z": 0.0},
        },
    )
    reciprocal = observed(
        public_id="00000000-0000-0000-0000-000000000292", source_key="telemetry:turning:292", frame=180, event_type="damage_applied",
        facts={
            "source_replay_player_public_ids": [enemy], "victim_replay_player_public_id": player,
            "attacker_object_id": 20, "victim_object_id": 10,
            "applied_amount": 25.0, "killing_blow": False,
        },
    )
    spatial = observed(
        public_id="00000000-0000-0000-0000-000000000293", source_key="telemetry:turning:293", frame=200, event_type="entity_sample",
        facts={"position": {"x": 10.0, "y": 20.0}, "object_id": 10, "owner_scope_key": player},
    )
    value = _values(player_context(kill, reciprocal, spatial))["combat.turning_point_timing"]
    assert thaw_canonical(value.raw_value) == [  # type: ignore[attr-defined]
        {
            "frame": 210,
            "attacker_template_name": "AmericaVehicleHumvee",
            "victim_template_name": "ChinaWarFactory",
            "criterion": "engagement-swing-v1",
        }
    ]
    assert thaw_canonical(value.details)["criterion_version"] == "engagement-swing-v1"  # type: ignore[attr-defined]


@pytest.mark.parametrize("source_ids", ([], ["00000000-0000-4000-8000-000000000250"]))
def test_combat_rejects_unknown_or_self_attributed_reciprocal_damage(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext], source_ids: list[str]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    enemy = "00000000-0000-4000-8000-000000000251"
    candidate = observed(
        public_id="turning-source-candidate", source_key="telemetry:turning:source:candidate", frame=200,
        event_type="damage_applied", facts={
            "source_replay_player_public_ids": [player], "victim_replay_player_public_id": enemy,
            "attacker_object_id": 10, "victim_object_id": 20, "applied_amount": 100.0, "killing_blow": True,
            "victim_template_name": "ChinaWarFactory", "attacker_template_name": "AmericaVehicleHumvee",
        },
    )
    reciprocal = observed(
        public_id="turning-source-reciprocal", source_key="telemetry:turning:source:reciprocal", frame=200,
        event_type="damage_applied", facts={
            "source_replay_player_public_ids": source_ids, "victim_replay_player_public_id": player,
            "attacker_object_id": 20, "victim_object_id": 10, "applied_amount": 25.0, "killing_blow": False,
        },
    )
    value = _values(player_context(candidate, reciprocal))["combat.turning_point_timing"]
    assert value.quality_reason == "insufficient_engagement_evidence"  # type: ignore[attr-defined]


def test_combat_engagement_swing_window_is_inclusive_and_outside_events_are_rejected(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    enemy = "00000000-0000-4000-8000-000000000251"

    def event(public_id: str, frame: int, event_type: str, facts: dict[str, object]) -> ObservedEvidence:
        return observed(public_id=public_id, source_key=f"telemetry:turning:window:{public_id}", frame=frame, event_type=event_type, facts=facts)

    candidate = event("candidate", 200, "damage_applied", {
        "source_replay_player_public_ids": [player], "victim_replay_player_public_id": enemy,
        "attacker_object_id": 10, "victim_object_id": 20, "applied_amount": 100.0, "killing_blow": True,
        "victim_template_name": "ChinaWarFactory", "attacker_template_name": "AmericaVehicleHumvee",
        "location": {"x": 1.0, "y": 2.0, "z": 0.0},
    })
    reciprocal = event("reciprocal", 350, "damage_applied", {
        "source_replay_player_public_ids": [enemy], "victim_replay_player_public_id": player,
        "attacker_object_id": 20, "victim_object_id": 10, "applied_amount": 25.0, "killing_blow": False,
    })
    spatial = event("spatial", 350, "entity_sample", {"object_id": 10, "owner_scope_key": player, "position": {"x": 1.0, "y": 2.0}})
    complete = _values(player_context(candidate, reciprocal, spatial))["combat.turning_point_timing"]
    assert complete.quality_reason is None  # type: ignore[attr-defined]

    outside_engagement = event("reciprocal-outside", 351, "damage_applied", {
        "source_replay_player_public_ids": [enemy], "victim_replay_player_public_id": player,
        "attacker_object_id": 20, "victim_object_id": 10, "applied_amount": 25.0, "killing_blow": False,
    })
    rejected_engagement = _values(player_context(candidate, outside_engagement, spatial))["combat.turning_point_timing"]
    assert rejected_engagement.quality_reason == "insufficient_engagement_evidence"  # type: ignore[attr-defined]

def test_combat_engagement_swing_requires_linked_spatial_objects_and_supports_multiple_candidates(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    enemy = "00000000-0000-4000-8000-000000000251"

    def damage(public_id: str, frame: int, source: str, victim: str, attacker: int, target: int, killing: bool) -> ObservedEvidence:
        return observed(
            public_id=public_id, source_key=f"telemetry:turning:multi:{public_id}", frame=frame, event_type="damage_applied",
            facts={
                "source_replay_player_public_ids": [source], "victim_replay_player_public_id": victim,
                "attacker_object_id": attacker, "victim_object_id": target, "applied_amount": 25.0,
                "killing_blow": killing, "victim_template_name": "ChinaWarFactory", "attacker_template_name": "AmericaVehicleHumvee",
            },
        )

    candidate_one = damage("candidate-one", 100, player, enemy, 10, 20, True)
    reciprocal_one = damage("reciprocal-one", 100, enemy, player, 20, 10, False)
    candidate_two = damage("candidate-two", 400, player, enemy, 11, 21, True)
    reciprocal_two = damage("reciprocal-two", 400, enemy, player, 21, 11, False)
    spatial_one = observed(public_id="spatial-one", source_key="telemetry:turning:multi:spatial-one", frame=100, event_type="entity_sample", facts={"object_id": 10, "owner_scope_key": player, "position": {"x": 1.0, "y": 2.0}})
    spatial_two = observed(public_id="spatial-two", source_key="telemetry:turning:multi:spatial-two", frame=400, event_type="entity_sample", facts={"object_id": 11, "owner_scope_key": player, "position": {"x": 3.0, "y": 4.0}})
    value = _values(player_context(candidate_one, reciprocal_one, candidate_two, reciprocal_two, spatial_one, spatial_two))["combat.turning_point_timing"]
    assert len(thaw_canonical(value.raw_value)) == 2  # type: ignore[arg-type, attr-defined]

    unrelated_spatial = observed(public_id="unrelated-spatial", source_key="telemetry:turning:unrelated:spatial", frame=100, event_type="entity_sample", facts={"object_id": 99, "owner_scope_key": player, "position": {"x": 1.0, "y": 2.0}})
    rejected = _values(player_context(candidate_one, reciprocal_one, unrelated_spatial))["combat.turning_point_timing"]
    assert rejected.quality_reason == "insufficient_spatial_context"  # type: ignore[attr-defined]
