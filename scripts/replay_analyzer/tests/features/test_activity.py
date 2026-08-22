"""Closed effective-APM and observed state feature tests."""

from collections.abc import Callable

from generals_replay_analyzer.features.activity import ActivityExtractor
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import ObservedEvidence, thaw_canonical
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage


def _values(context: FeatureContext) -> dict[str, object]:
    return {value.name: value for value in ActivityExtractor().extract(context).values}


def test_activity_uses_exact_closed_manifest_dedup_policy_and_source_grounded_state(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    other = "00000000-0000-4000-8000-000000000290"

    def order(public_id: str, key: str, frame: int, selected: list[int], source_player: str = player) -> ObservedEvidence:
        return observed(
            public_id=public_id,
            source_key=key,
            frame=frame,
            event_type="order_issued",
            facts={
                "source_replay_player_public_id": source_player,
                "message_type": 1068,
                "message_name": "MSG_DO_MOVETO",
                "selected_object_ids": selected,
                "target_kind": "location",
                "target_object_id": None,
                "target_location": {"x": 1.125, "y": 2.25, "z": 0.0},
                "command_source": "player",
            },
        )

    context = player_context(
        order("00000000-0000-4000-8000-000000000291", "telemetry:a13", 13, [1]),
        order("00000000-0000-4000-8000-000000000292", "telemetry:b11", 11, [2]),
        order("00000000-0000-4000-8000-000000000293", "telemetry:a10", 10, [1]),
        order("00000000-0000-4000-8000-000000000294", "telemetry:other", 12, [9], other),
        observed(
            public_id="00000000-0000-4000-8000-000000000295",
            source_key="telemetry:not-covered",
            frame=14,
            event_type="order_issued",
            facts={
                "source_replay_player_public_id": player,
                "message_type": 9999,
                "message_name": "MSG_NOT_COVERED",
                "selected_object_ids": [1],
                "target_kind": "none",
                "target_object_id": None,
                "target_location": None,
                "command_source": "player",
            },
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000296",
            source_key="telemetry:manifest",
            frame=0,
            event_type="manifest",
            facts={"order_coverage": canonical_order_coverage()},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000297",
            source_key="telemetry:state",
            frame=20,
            event_type="entity_state_changed",
            facts={"replay_player_public_id": player, "previous_state": "idle", "current_state": "moving"},
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000298",
            source_key="telemetry:outcome",
            frame=300,
            event_type="match_outcome",
            facts={
                "replay_player_public_id": player,
                "result_payload": {"result": "won", "source": "victory_conditions", "status": "decided"},
            },
        ),
        observed(
            public_id="00000000-0000-4000-8000-000000000299",
            source_key="telemetry:complete",
            frame=300,
            event_type="complete",
            facts={"terminal_reason": "clean_completion", "final_frame": 300},
        ),
    )
    values = _values(context)
    assert values["activity.supported_order_action_count"].raw_value == 3  # type: ignore[attr-defined]
    assert values["activity.effective_actions_per_minute"].raw_value == 18.0  # type: ignore[attr-defined]
    assert thaw_canonical(values["activity.supported_order_coverage"].raw_value) == canonical_order_coverage()  # type: ignore[attr-defined]
    details = thaw_canonical(values["activity.effective_actions_per_minute"].details)  # type: ignore[attr-defined]
    assert details["policy_version"] == "effective-apm-policy-v1"
    assert details["deduplication_window_frames"] == 3
    assert details["accepted_count"] == 3
    assert details["suppressed_count"] == 0
    assert details["denominator_frames"] == 300
    assert "skill" not in str(details).lower()
    assert thaw_canonical(values["state.final_result"].raw_value) == {  # type: ignore[attr-defined]
        "result": "won",
        "source": "victory_conditions",
        "status": "decided",
    }
    assert values["state.entity_transition_count"].raw_value == 1  # type: ignore[attr-defined]


def test_activity_count_and_coverage_survive_missing_terminal_but_rate_does_not(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    manifest = observed(
        public_id="00000000-0000-4000-8000-0000000002b1",
        source_key="telemetry:manifest:missing-terminal",
        event_type="manifest",
        facts={"order_coverage": canonical_order_coverage()},
        frame=0,
    )
    order = observed(
        public_id="00000000-0000-4000-8000-0000000002b2",
        source_key="telemetry:order:missing-terminal",
        event_type="order_issued",
        facts={
            "source_replay_player_public_id": player,
            "message_type": 1074,
            "message_name": "MSG_DO_STOP",
            "selected_object_ids": [1],
            "target_kind": "none",
            "target_object_id": None,
            "target_location": None,
            "command_source": "player",
        },
    )
    values = _values(player_context(manifest, order, final_frame=None))
    assert values["activity.supported_order_action_count"].raw_value == 1  # type: ignore[attr-defined]
    assert values["activity.effective_actions_per_minute"].quality_reason == "missing_complete_terminal_record"  # type: ignore[attr-defined]

    zero = _values(player_context(manifest, order, final_frame=0))
    assert zero["activity.effective_actions_per_minute"].quality_reason == "insufficient_observed_duration"  # type: ignore[attr-defined]


def test_activity_rejects_changed_manifest_and_suppresses_consecutive_duplicate_fingerprints(
    observed: Callable[..., ObservedEvidence], player_context: Callable[..., FeatureContext]
) -> None:
    player = "00000000-0000-4000-8000-000000000250"
    changed = canonical_order_coverage()
    changed["coverage"] = "full"
    bad = _values(player_context(observed(event_type="manifest", facts={"order_coverage": changed}, frame=0)))
    assert bad["activity.supported_order_coverage"].quality_reason == "changed_order_coverage_manifest"  # type: ignore[attr-defined]

    manifest = observed(
        public_id="00000000-0000-4000-8000-0000000002b3",
        source_key="telemetry:manifest:dedup",
        event_type="manifest",
        facts={"order_coverage": canonical_order_coverage()},
        frame=0,
    )

    def duplicate(public_id: str, frame: int) -> ObservedEvidence:
        return observed(
            public_id=public_id,
            source_key=f"telemetry:{frame}:{public_id}",
            frame=frame,
            event_type="order_issued",
            facts={
                "source_replay_player_public_id": player,
                "message_type": 1074,
                "message_name": "MSG_DO_STOP",
                "selected_object_ids": [1],
                "target_kind": "none",
                "target_object_id": None,
                "target_location": None,
                "command_source": "player",
            },
        )

    values = _values(
        player_context(
            manifest,
            duplicate("00000000-0000-4000-8000-0000000002a1", 10),
            duplicate("00000000-0000-4000-8000-0000000002a2", 13),
            observed(
                public_id="00000000-0000-4000-8000-0000000002b4",
                source_key="telemetry:complete:dedup",
                event_type="complete",
                frame=300,
                facts={"terminal_reason": "clean_completion", "final_frame": 300},
            ),
        )
    )
    assert values["activity.supported_order_action_count"].raw_value == 1  # type: ignore[attr-defined]
    assert thaw_canonical(values["activity.effective_actions_per_minute"].details)["suppressed_count"] == 1  # type: ignore[attr-defined]
