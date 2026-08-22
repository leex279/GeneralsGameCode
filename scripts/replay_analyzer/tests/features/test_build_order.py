"""Source-grounded completed-build sequence tests."""

from collections.abc import Callable

from generals_replay_analyzer.features.build_order import BuildOrderExtractor
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import ObservedEvidence, thaw_canonical


def _by_name(bundle: object, name: str) -> object:
    return next(value for value in bundle.values if value.name == name)  # type: ignore[attr-defined]


def test_build_order_uses_only_observed_construction_completions_in_source_key_order(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    context = player_context(
        observed(
            public_id="00000000-0000-4000-8000-000000000251",
            source_key="telemetry:z",
            frame=60,
            event_type="construction_completed",
            facts={"template_name": "ChinaBarracks", "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000252",
            source_key="telemetry:a",
            frame=60,
            event_type="construction_completed",
            facts={"template_name": "ChinaPowerPlant", "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000253",
            source_key="telemetry:creation",
            frame=10,
            event_type="object_created",
            facts={"template_name": "FakeEarlyBuilding", "replay_player_public_id": player},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000254",
            source_key="parser:command",
            frame=5,
            event_type="replay_command",
            facts={"message_name": "MSG_DOZER_CONSTRUCT", "catalog_build_time": 1},
        ),
    )
    bundle = BuildOrderExtractor().extract(context)
    sequence = _by_name(bundle, "build.completed_sequence")
    assert thaw_canonical(sequence.raw_value) == [  # type: ignore[attr-defined]
        {"frame": 60, "template_name": "ChinaPowerPlant"},
        {"frame": 60, "template_name": "ChinaBarracks"},
    ]
    assert _by_name(bundle, "build.first_completed_frame").raw_value == 60  # type: ignore[attr-defined]
    assert _by_name(bundle, "build.completed_count").raw_value == 2  # type: ignore[attr-defined]
    assert tuple(ref.public_id for ref in sequence.input_evidence) == (  # type: ignore[attr-defined]
        "00000000-0000-4000-8000-000000000252",
        "00000000-0000-4000-8000-000000000251",
    )


def test_build_order_emits_null_reasons_when_telemetry_or_direct_scope_is_missing(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    unavailable = BuildOrderExtractor().extract(player_context(telemetry_status=None))
    assert all(value.raw_value is None and value.quality_reason == "missing_successful_telemetry" for value in unavailable.values)

    wrong_player = observed(
        event_type="construction_completed",
        facts={
            "template_name": "ChinaPowerPlant",
            "replay_player_public_id": "00000000-0000-4000-8000-000000000299",
        },
    )
    malformed = BuildOrderExtractor().extract(player_context(wrong_player))
    assert all(value.raw_value is None and value.quality_reason == "ambiguous_player_attribution" for value in malformed.values)
