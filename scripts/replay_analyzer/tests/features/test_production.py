"""Observed production and upgrade lifecycle feature tests."""

from collections.abc import Callable

from generals_replay_analyzer.features.base import FeatureWindow
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import ObservedEvidence, thaw_canonical
from generals_replay_analyzer.features.production import ProductionExtractor


def _values(context: FeatureContext) -> dict[str, object]:
    return {value.name: value for value in ProductionExtractor().extract(context).values}


def test_production_counts_observed_states_composition_and_matched_durations(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"

    def event(public_id: str, key: str, frame: int, event_type: str, identity: str, name: str) -> ObservedEvidence:
        return observed(
            public_id=public_id,
            source_key=key,
            frame=frame,
            event_type=event_type,
            facts={
                "item_kind": "unit" if event_type.startswith("production") else "upgrade",
                "item_name": name,
                "production_identity": identity,
                "replay_player_public_id": player,
            },
        )

    context = player_context(
        event("00000000-0000-4000-8000-000000000271", "telemetry:q1", 10, "production_queued", "unit:1", "Tank"),
        event("00000000-0000-4000-8000-000000000272", "telemetry:c1", 50, "production_completed", "unit:1", "Tank"),
        event("00000000-0000-4000-8000-000000000273", "telemetry:q2", 20, "production_queued", "unit:2", "Dozer"),
        event("00000000-0000-4000-8000-000000000274", "telemetry:x2", 30, "production_cancelled", "unit:2", "Dozer"),
        event("00000000-0000-4000-8000-000000000275", "telemetry:qu", 15, "upgrade_queued", "upgrade:1", "Armor"),
        event("00000000-0000-4000-8000-000000000276", "telemetry:cu", 75, "upgrade_completed", "upgrade:1", "Armor"),
    )
    values = _values(context)
    assert values["production.queued_count"].raw_value == 3  # type: ignore[attr-defined]
    assert values["production.completed_count"].raw_value == 2  # type: ignore[attr-defined]
    assert values["production.cancelled_count"].raw_value == 1  # type: ignore[attr-defined]
    assert thaw_canonical(values["production.completed_composition"].raw_value) == {"Armor": 1, "Tank": 1}  # type: ignore[attr-defined]
    assert values["production.completed_composition"].window == FeatureWindow(50, 75)  # type: ignore[attr-defined]
    assert values["production.completed_count"].window == FeatureWindow(50, 75)  # type: ignore[attr-defined]
    assert thaw_canonical(values["production.observed_duration_frames"].raw_value) == [  # type: ignore[attr-defined]
        {"duration_frames": 40, "identity": "unit:1", "item_name": "Tank"},
        {"duration_frames": 10, "identity": "unit:2", "item_name": "Dozer"},
        {"duration_frames": 60, "identity": "upgrade:1", "item_name": "Armor"},
    ]


def test_production_requires_catalog_and_never_fabricates_completion_or_duration_from_queue(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    queued = observed(
        event_type="production_queued",
        facts={
            "item_kind": "unit",
            "item_name": "Tank",
            "production_identity": "unit:missing-terminal",
            "catalog_build_time": 1,
            "replay_player_public_id": player,
        },
    )
    missing_catalog = _values(player_context(queued, catalog_identity=None))
    assert all(value.raw_value is None and value.quality_reason == "missing_catalog_identity" for value in missing_catalog.values())  # type: ignore[attr-defined]

    values = _values(player_context(queued))
    assert values["production.queued_count"].raw_value == 1  # type: ignore[attr-defined]
    assert values["production.completed_count"].raw_value == 0  # type: ignore[attr-defined]
    assert values["production.completed_composition"].raw_value == ()  # type: ignore[attr-defined]
    assert values["production.observed_duration_frames"].raw_value is None  # type: ignore[attr-defined]
    assert values["production.observed_duration_frames"].quality_reason == "missing_complete_terminal_record"  # type: ignore[attr-defined]


def test_production_exposes_observed_science_and_special_power_timing(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    science = observed(
        public_id="00000000-0000-4000-8000-000000000277",
        source_key="telemetry:science:1",
        frame=120,
        event_type="science_purchased",
        facts={"item_name": "SCIENCE_ArtilleryBarrage1", "replay_player_public_id": player},
    )
    power = observed(
        public_id="00000000-0000-4000-8000-000000000278",
        source_key="telemetry:power:1",
        frame=180,
        event_type="special_power_used",
        facts={"item_name": "SuperweaponSpySatellite", "replay_player_public_id": player},
    )

    values = _values(player_context(science, power))

    assert thaw_canonical(values["production.science_purchase_timing"].raw_value) == [  # type: ignore[attr-defined]
        {"frame": 120, "item_name": "SCIENCE_ArtilleryBarrage1"}
    ]
    assert thaw_canonical(values["production.special_power_timing"].raw_value) == [  # type: ignore[attr-defined]
        {"frame": 180, "item_name": "SuperweaponSpySatellite"}
    ]
