"""Observed cash and supply feature tests."""

from collections.abc import Callable

from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.economy import EconomyExtractor
from generals_replay_analyzer.features.evidence import ObservedEvidence, thaw_canonical


def _values(context: FeatureContext) -> dict[str, object]:
    return {value.name: value for value in EconomyExtractor().extract(context).values}


def test_economy_preserves_exact_signed_sums_terminal_balance_rate_and_source_share(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    context = player_context(
        observed(
            public_id="00000000-0000-4000-8000-000000000261",
            source_key="telemetry:cash:1",
            frame=20,
            event_type="cash_changed",
            facts={"delta": 125.5, "track_income": True, "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000262",
            source_key="telemetry:cash:2",
            frame=25,
            event_type="cash_changed",
            facts={"delta": -20.25, "track_income": False, "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000263",
            source_key="telemetry:supply:1",
            frame=30,
            event_type="supply_collected",
            facts={"amount": 100, "source_status": "resolved", "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000264",
            source_key="telemetry:supply:2",
            frame=90,
            event_type="supply_collected",
            facts={"amount": 200.125, "source_status": "unknown", "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000265",
            source_key="telemetry:complete",
            frame=300,
            event_type="complete",
            facts={"final_cash_balance": 777, "replay_player_public_id": player, "terminal_reason": "clean_completion"},
        ),
    )
    values = _values(context)
    assert values["economy.cash_change_total"].raw_value == 105.25  # type: ignore[attr-defined]
    assert values["economy.tracked_income_total"].raw_value == 125.5  # type: ignore[attr-defined]
    assert values["economy.supply_collected_total"].raw_value == 300.125  # type: ignore[attr-defined]
    assert values["economy.supply_collection_rate"].raw_value == 300.125 * 1800.0 / 60.0  # type: ignore[attr-defined]
    assert values["economy.cash_balance_final"].raw_value == 777  # type: ignore[attr-defined]
    assert values["economy.supply_source_resolved_share"].raw_value == 0.5  # type: ignore[attr-defined]
    assert "ideal" not in str(thaw_canonical(values["economy.supply_collection_rate"].details)).lower()  # type: ignore[attr-defined]


def test_economy_keeps_observed_zero_distinct_from_unavailable_and_rejects_zero_duration(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    zero_cash = observed(
        event_type="cash_changed",
        facts={"delta": 0, "track_income": True, "replay_player_public_id": player},
    )
    supply = observed(
        public_id="00000000-0000-4000-8000-000000000266",
        source_key="telemetry:supply:one",
        frame=40,
        event_type="supply_collected",
        facts={"amount": 100, "source_status": "resolved", "replay_player_public_id": player},
    )
    values = _values(player_context(zero_cash, supply))
    assert values["economy.cash_change_total"].raw_value == 0  # type: ignore[attr-defined]
    assert values["economy.cash_change_total"].quality == "complete"  # type: ignore[attr-defined]
    assert values["economy.supply_collection_rate"].raw_value is None  # type: ignore[attr-defined]
    assert values["economy.supply_collection_rate"].quality_reason == "insufficient_observed_duration"  # type: ignore[attr-defined]
    assert values["economy.cash_balance_final"].quality_reason == "missing_complete_terminal_record"  # type: ignore[attr-defined]

    missing = _values(player_context(telemetry_status=None))
    assert all(value.raw_value is None and value.quality_reason == "missing_successful_telemetry" for value in missing.values())  # type: ignore[attr-defined]
