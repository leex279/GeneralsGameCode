"""Closed supported-order activity and observed terminal-state features."""

from __future__ import annotations

from typing import cast

from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureValue,
    FeatureWindow,
    complete_value,
    unavailable_value,
)
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import ObservedEvidence, fact, thaw_canonical
from generals_replay_analyzer.features.registry import BASE_REGISTRY
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage

POLICY_VERSION = "effective-apm-policy-v1"
DEDUPLICATION_WINDOW_FRAMES = 3


def _manifest_tuples(manifest: dict[str, object]) -> frozenset[tuple[int, str, str]]:
    commands = cast(list[dict[str, object]], manifest["supported_commands"])
    return frozenset(
        (cast(int, item["message_type"]), cast(str, item["message_name"]), cast(str, item["target_kind"]))
        for item in commands
    )


_CANONICAL_MANIFEST = canonical_order_coverage()
_SUPPORTED_TUPLES = _manifest_tuples(_CANONICAL_MANIFEST)


# TheSuperHackers @feature Leex 22/08/2026 Bound effective APM to the exact closed telemetry order manifest. (#TBD)
class ActivityExtractor:
    name = "activity"
    version = "activity-v1"
    feature_names = (
        "activity.effective_actions_per_minute",
        "activity.supported_order_action_count",
        "activity.supported_order_coverage",
        "state.entity_transition_count",
        "state.final_result",
        "scouting.first_observed_clear_timing",
        "scouting.visibility_transition_count",
    )

    def extract(self, context: FeatureContext) -> FeatureBundle:
        window = FeatureWindow(0, context.final_frame or 0)
        if context.telemetry_status != "succeeded":
            return self._all_unavailable(context, window, "missing_successful_telemetry")
        values = list(self._activity_values(context, window))
        values.extend(self._state_values(context, window))
        values.extend(self._scouting_values(context, window))
        return FeatureBundle(self.name, self.version, tuple(sorted(values, key=lambda value: value.name)))

    def _scouting_values(self, context: FeatureContext, window: FeatureWindow) -> tuple[FeatureValue, ...]:
        transitions = tuple(item for item in context.observed if item.event_type == "object_visibility_changed")
        # TheSuperHackers @fix Leex 25/08/2026 Report opponent scouting reveals instead of own units and ambient map objects. (#TBD)
        first_clear = tuple(
            item
            for item in transitions
            if fact(item, "first_observed_clear") is True
            and type(fact(item, "object_owner_replay_player_public_id")) is str
            and fact(item, "object_owner_replay_player_public_id") != context.replay_player_public_id
            and type(fact(item, "object_kind_of_flags")) is tuple
            and "SELECTABLE" in cast(tuple[object, ...], fact(item, "object_kind_of_flags"))
        )
        if not transitions:
            return (
                unavailable_value("scouting.first_observed_clear_timing", context.scope, window, "no_observed_events", BASE_REGISTRY),
                unavailable_value("scouting.visibility_transition_count", context.scope, window, "no_observed_events", BASE_REGISTRY),
            )
        timing = tuple(
            {
                "frame": item.frame,
                "object_id": fact(item, "object_id"),
                "template_name": fact(item, "template_name"),
            }
            for item in first_clear
            if item.frame is not None
            and type(fact(item, "object_id")) is int
            and type(fact(item, "template_name")) is str
        )
        timing_value = (
            complete_value(
                "scouting.first_observed_clear_timing", timing, context.scope, window,
                tuple(item.ref for item in first_clear), BASE_REGISTRY,
            )
            if timing
            else unavailable_value("scouting.first_observed_clear_timing", context.scope, window, "no_first_clear_events", BASE_REGISTRY)
        )
        count = complete_value(
            "scouting.visibility_transition_count", len(transitions), context.scope, window,
            tuple(item.ref for item in transitions), BASE_REGISTRY,
        )
        return timing_value, count

    def _activity_values(self, context: FeatureContext, window: FeatureWindow) -> tuple[FeatureValue, ...]:
        manifests = tuple(item for item in context.observed if item.event_type == "manifest")
        if len(manifests) != 1 or thaw_canonical(fact(manifests[0], "order_coverage")) != _CANONICAL_MANIFEST:
            return tuple(
                unavailable_value(name, context.scope, window, "changed_order_coverage_manifest", BASE_REGISTRY)
                for name in (
                    "activity.effective_actions_per_minute",
                    "activity.supported_order_action_count",
                    "activity.supported_order_coverage",
                )
            )
        manifest = manifests[0]
        orders = tuple(
            item
            for item in context.observed
            if item.event_type == "order_issued"
            and fact(item, "source_replay_player_public_id") == context.replay_player_public_id
            and (
                fact(item, "message_type"),
                fact(item, "message_name"),
                fact(item, "target_kind"),
            )
            in _SUPPORTED_TUPLES
        )
        accepted, suppressed = self._deduplicate(orders)
        accepted_refs = tuple(item.ref for item in accepted)
        evidence = (manifest.ref,) + accepted_refs
        details = {
            "accepted_count": len(accepted),
            "coverage_manifest": _CANONICAL_MANIFEST,
            "deduplication_window_frames": DEDUPLICATION_WINDOW_FRAMES,
            "denominator_frames": context.final_frame,
            "logic_frames_per_second": context.logic_frames_per_second,
            "policy_version": POLICY_VERSION,
            "suppressed_count": suppressed,
        }
        values: list[FeatureValue] = [
            complete_value(
                "activity.supported_order_action_count", len(accepted), context.scope, window, evidence, BASE_REGISTRY, details=details
            ),
            complete_value(
                "activity.supported_order_coverage",
                _CANONICAL_MANIFEST,
                context.scope,
                window,
                (manifest.ref,),
                BASE_REGISTRY,
                details=details,
            ),
        ]
        terminal = tuple(item for item in context.observed if item.event_type == "complete")
        if context.final_frame == 0:
            values.append(
                unavailable_value(
                    "activity.effective_actions_per_minute",
                    context.scope,
                    window,
                    "insufficient_observed_duration",
                    BASE_REGISTRY,
                    input_evidence=evidence,
                    details=details,
                )
            )
        elif context.logic_frames_per_second is None:
            values.append(
                unavailable_value(
                    "activity.effective_actions_per_minute",
                    context.scope,
                    window,
                    "missing_logic_timebase",
                    BASE_REGISTRY,
                    input_evidence=evidence,
                    details=details,
                )
            )
        elif (
            context.final_frame is None
            or len(terminal) != 1
            or fact(terminal[0], "terminal_reason") != "clean_completion"
            or fact(terminal[0], "final_frame") != context.final_frame
        ):
            values.append(
                unavailable_value(
                    "activity.effective_actions_per_minute",
                    context.scope,
                    window,
                    "missing_complete_terminal_record",
                    BASE_REGISTRY,
                    input_evidence=evidence,
                    details=details,
                )
            )
        else:
            # TheSuperHackers @fix Leex 24/08/2026 Derive effective APM from the replay's authoritative logic clock. (#TBD)
            rate = 60.0 * len(accepted) / (context.final_frame / context.logic_frames_per_second)
            values.append(
                complete_value(
                    "activity.effective_actions_per_minute",
                    rate,
                    context.scope,
                    window,
                    evidence + (terminal[0].ref,),
                    BASE_REGISTRY,
                    details=details,
                )
            )
        return tuple(values)

    def _deduplicate(self, orders: tuple[ObservedEvidence, ...]) -> tuple[tuple[ObservedEvidence, ...], int]:
        accepted: list[ObservedEvidence] = []
        previous_fingerprint: tuple[object, ...] | None = None
        previous_frame: int | None = None
        suppressed = 0
        for item in orders:
            fingerprint = (
                fact(item, "source_replay_player_public_id"),
                fact(item, "message_type"),
                fact(item, "selected_object_ids"),
                fact(item, "target_kind"),
                fact(item, "target_object_id"),
                fact(item, "target_location"),
                fact(item, "command_source"),
            )
            if (
                previous_fingerprint == fingerprint
                and previous_frame is not None
                and item.frame is not None
                and item.frame - previous_frame <= DEDUPLICATION_WINDOW_FRAMES
            ):
                suppressed += 1
                continue
            accepted.append(item)
            previous_fingerprint = fingerprint
            previous_frame = item.frame
        return tuple(accepted), suppressed

    def _state_values(self, context: FeatureContext, window: FeatureWindow) -> tuple[FeatureValue, ...]:
        transitions = tuple(
            item
            for item in context.observed
            if item.event_type == "entity_state_changed"
            and fact(item, "replay_player_public_id") == context.replay_player_public_id
        )
        outcomes = tuple(
            item
            for item in context.observed
            if item.event_type == "match_outcome"
            and fact(item, "replay_player_public_id") == context.replay_player_public_id
        )
        fallback = tuple(item.ref for item in context.observed if item.event_type in ("manifest", "complete"))[:1]
        transition_inputs = tuple(item.ref for item in transitions) or fallback
        if transition_inputs:
            transition = complete_value(
                "state.entity_transition_count",
                len(transitions),
                context.scope,
                window,
                transition_inputs,
                BASE_REGISTRY,
            )
        else:
            transition = unavailable_value(
                "state.entity_transition_count", context.scope, window, "no_observed_events", BASE_REGISTRY
            )
        if len(outcomes) == 1 and fact(outcomes[0], "result_payload") is not None:
            result = complete_value(
                "state.final_result",
                fact(outcomes[0], "result_payload"),
                context.scope,
                window,
                (outcomes[0].ref,),
                BASE_REGISTRY,
            )
        else:
            result = unavailable_value(
                "state.final_result", context.scope, window, "missing_complete_terminal_record", BASE_REGISTRY
            )
        return transition, result

    def _all_unavailable(self, context: FeatureContext, window: FeatureWindow, reason: str) -> FeatureBundle:
        return FeatureBundle(
            self.name,
            self.version,
            tuple(unavailable_value(name, context.scope, window, reason, BASE_REGISTRY) for name in self.feature_names),
        )
