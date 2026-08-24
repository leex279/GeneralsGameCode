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


def test_economy_folds_projected_cash_per_minute_snapshots_and_income_buckets(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    context = player_context(
        observed(
            public_id="00000000-0000-4000-8000-000000000271",
            source_key="telemetry:cash:bucket-0",
            frame=29,
            event_type="cash_changed",
            facts={
                "track_income": True,
                "tracked_income_amount": 100,
                "income_bucket_index": 0,
                "replay_player_public_id": player,
            },
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000272",
            source_key="telemetry:cpm:30",
            frame=30,
            event_type="cash_per_minute_snapshot",
            facts={"cash_per_minute": 100, "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000273",
            source_key="telemetry:cash:bucket-1",
            frame=59,
            event_type="cash_changed",
            facts={
                "track_income": True,
                "tracked_income_amount": 50,
                "income_bucket_index": 1,
                "replay_player_public_id": player,
            },
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000274",
            source_key="telemetry:cpm:60",
            frame=60,
            event_type="cash_per_minute_snapshot",
            facts={"cash_per_minute": 150, "replay_player_public_id": player},
        ),
    )

    values = _values(context)
    assert values["economy.cash_per_minute_latest"].raw_value == 150  # type: ignore[attr-defined]
    assert values["economy.cash_per_minute_peak"].raw_value == 150  # type: ignore[attr-defined]
    assert thaw_canonical(values["economy.cash_per_minute_series"].raw_value) == [  # type: ignore[attr-defined]
        {"cash_per_minute": 100, "frame": 30},
        {"cash_per_minute": 150, "frame": 60},
    ]
    assert values["economy.cash_per_minute_reconciled_share"].raw_value == 1.0  # type: ignore[attr-defined]
    assert all(
        values[name].input_evidence
        for name in (
            "economy.cash_per_minute_latest",
            "economy.cash_per_minute_peak",
            "economy.cash_per_minute_series",
            "economy.cash_per_minute_reconciled_share",
        )
    )


def test_economy_caves_partial_and_missing_optional_cash_per_minute_families(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    partial = _values(
        player_context(
            observed(
                public_id="00000000-0000-4000-8000-000000000275",
                source_key="telemetry:cash:legacy",
                frame=29,
                event_type="cash_changed",
                facts={"track_income": True, "replay_player_public_id": player},
            ),
            observed(
                public_id="00000000-0000-4000-8000-000000000276",
                source_key="telemetry:cpm:30",
                frame=30,
                event_type="cash_per_minute_snapshot",
                facts={"cash_per_minute": 100, "replay_player_public_id": player},
            ),
        )
    )
    assert partial["economy.cash_per_minute_reconciled_share"].raw_value == 0.0  # type: ignore[attr-defined]
    assert partial["economy.cash_per_minute_reconciled_share"].quality == "partial"  # type: ignore[attr-defined]
    assert partial["economy.cash_per_minute_reconciled_share"].quality_reason == "missing_income_bucket_provenance"  # type: ignore[attr-defined]

    missing = _values(player_context())
    for name in (
        "economy.cash_per_minute_latest",
        "economy.cash_per_minute_peak",
        "economy.cash_per_minute_series",
        "economy.cash_per_minute_reconciled_share",
    ):
        assert missing[name].raw_value is None  # type: ignore[attr-defined]
        assert missing[name].quality_reason == "missing_cash_per_minute_snapshots"  # type: ignore[attr-defined]
