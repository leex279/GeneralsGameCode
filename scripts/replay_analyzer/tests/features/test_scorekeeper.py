"""Terminal engine ScoreKeeper feature tests."""

from collections.abc import Callable

from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import ObservedEvidence, thaw_canonical
from generals_replay_analyzer.features.scorekeeper import ScoreKeeperExtractor

_METRICS = {
    "money_earned": 1000,
    "money_spent": 700,
    "units_built": 8,
    "units_lost": 3,
    "units_destroyed": 5,
    "buildings_built": 4,
    "buildings_lost": 1,
    "buildings_destroyed": 2,
    "tech_buildings_captured": 1,
    "faction_buildings_captured": 0,
}


def _values(context: FeatureContext) -> dict[str, object]:
    return {value.name: value for value in ScoreKeeperExtractor().extract(context).values}


def test_scorekeeper_exposes_all_terminal_metrics_and_caveated_event_deltas(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    context = player_context(
        observed(
            public_id="00000000-0000-4000-8000-000000000281",
            source_key="telemetry:score:terminal",
            frame=300,
            event_type="scorekeeper_snapshot",
            facts={**_METRICS, "scoring_enabled": True, "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000282",
            source_key="telemetry:cash:income",
            frame=20,
            event_type="cash_changed",
            facts={"delta": 900, "track_income": True, "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000283",
            source_key="telemetry:production",
            frame=30,
            event_type="production_completed",
            facts={"replay_player_public_id": player},
        ),
    )

    values = _values(context)
    assert thaw_canonical(values["scorekeeper.terminal_snapshot"].raw_value) == {  # type: ignore[attr-defined]
        "scoring_enabled": True,
        **_METRICS,
    }
    reconciliation = thaw_canonical(values["scorekeeper.event_reconciliation"].raw_value)  # type: ignore[attr-defined]
    assert set(reconciliation) == set(_METRICS)
    assert reconciliation["money_earned"] == {
        "delta": 100,
        "engine_total": 1000,
        "event_total": 900,
        "reason": "event_income_is_not_scorekeeper_money_earned",
        "status": "partial",
    }
    assert reconciliation["units_built"] == {
        "delta": 7,
        "engine_total": 8,
        "event_total": 1,
        "reason": "event_completion_is_not_scorekeeper_unit_building_classification",
        "status": "partial",
    }
    assert reconciliation["buildings_lost"] == {
        "delta": None,
        "engine_total": 1,
        "event_total": None,
        "reason": "no_semantically_comparable_event_total",
        "status": "not_comparable",
    }
    assert values["scorekeeper.event_reconciliation"].quality == "complete"  # type: ignore[attr-defined]
    assert values["scorekeeper.event_reconciliation"].input_evidence  # type: ignore[attr-defined]


def test_scorekeeper_handles_missing_optional_snapshot_without_validation_failure(
    player_context: Callable[..., FeatureContext]
) -> None:
    values = _values(player_context())
    for value in values.values():
        assert value.raw_value is None  # type: ignore[attr-defined]
        assert value.quality == "unavailable"  # type: ignore[attr-defined]
        assert value.quality_reason == "missing_scorekeeper_terminal_snapshot"  # type: ignore[attr-defined]
