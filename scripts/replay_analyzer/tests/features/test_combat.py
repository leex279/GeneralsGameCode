"""Directly attributed observed combat feature tests."""

from collections.abc import Callable

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
