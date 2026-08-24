"""Directly observed and attributed combat features."""

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


# TheSuperHackers @feature Leex 22/08/2026 Exclude unknown damage sources instead of guessing combat attribution. (#TBD)
class CombatExtractor:
    name = "combat"
    version = "combat-v2"
    feature_names = (
        "combat.applied_damage_dealt",
        "combat.applied_damage_taken",
        "combat.killing_blow_count",
        "combat.observed_damage_trade_ratio",
        "combat.observed_kill_timing",
        "combat.turning_point_timing",
    )
    _ENGAGEMENT_WINDOW_FRAMES = 150

    def extract(self, context: FeatureContext) -> FeatureBundle:
        window = FeatureWindow(0, context.final_frame or 0)
        if context.telemetry_status != "succeeded":
            return self._all_unavailable(context, window, "missing_successful_telemetry")
        events = tuple(item for item in context.observed if item.event_type == "damage_applied")
        if not events or any(type(fact(item, "applied_amount")) not in (int, float) for item in events):
            return self._all_unavailable(context, window, "no_observed_events")
        player = context.replay_player_public_id
        refs = tuple(item.ref for item in events)
        dealt_events = tuple(item for item in events if fact(item, "source_replay_player_public_ids") == (player,))
        taken_events = tuple(item for item in events if fact(item, "victim_replay_player_public_id") == player)
        dealt = sum(cast(int | float, fact(item, "applied_amount")) for item in dealt_events)
        taken = sum(cast(int | float, fact(item, "applied_amount")) for item in taken_events)
        killing_blows = sum(fact(item, "killing_blow") is True for item in dealt_events)
        details = {
            "ambiguous_source_event_count": len(events) - len(dealt_events),
            "attribution_policy": "unique_direct_source_only",
        }
        values: list[FeatureValue] = [
            complete_value("combat.applied_damage_dealt", dealt, context.scope, window, refs, BASE_REGISTRY, details=details),
            complete_value("combat.applied_damage_taken", taken, context.scope, window, refs, BASE_REGISTRY, details=details),
            complete_value("combat.killing_blow_count", killing_blows, context.scope, window, refs, BASE_REGISTRY),
        ]
        # TheSuperHackers @feature Leex 24/08/2026 Require a reversed object-pair exchange and nearby player-owned position before labeling a kill an engagement-swing candidate; this is not strategic causality. (#TBD)
        observed_kills = tuple(
            {
                "frame": item.frame,
                "attacker_template_name": fact(item, "attacker_template_name"),
                "victim_template_name": fact(item, "victim_template_name"),
            }
            for item in dealt_events
            if fact(item, "killing_blow") is True
            and item.frame is not None
            and type(fact(item, "attacker_template_name")) is str
            and type(fact(item, "victim_template_name")) is str
        )
        values.append(
            complete_value(
                "combat.observed_kill_timing", observed_kills, context.scope, window,
                tuple(item.ref for item in dealt_events if fact(item, "killing_blow") is True), BASE_REGISTRY,
            )
            if observed_kills
            else unavailable_value("combat.observed_kill_timing", context.scope, window, "no_observed_killing_blows", BASE_REGISTRY)
        )
        turning_point_value = self._turning_point_value(
            context,
            window,
            dealt_events,
            taken_events,
        )
        values.append(turning_point_value)
        if taken == 0:
            values.append(
                unavailable_value(
                    "combat.observed_damage_trade_ratio",
                    context.scope,
                    window,
                    "zero_trade_denominator",
                    BASE_REGISTRY,
                    input_evidence=refs,
                    details=details,
                )
            )
        else:
            values.append(
                complete_value(
                    "combat.observed_damage_trade_ratio",
                    dealt / taken,
                    context.scope,
                    window,
                    refs,
                    BASE_REGISTRY,
                    details=details,
                )
            )
        return FeatureBundle(self.name, self.version, tuple(sorted(values, key=lambda value: value.name)))

    def _turning_point_value(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        dealt_events: tuple[ObservedEvidence, ...],
        taken_events: tuple[ObservedEvidence, ...],
    ) -> FeatureValue:
        candidates = tuple(
            item
            for item in dealt_events
            if fact(item, "killing_blow") is True
            and item.frame is not None
            and type(fact(item, "attacker_template_name")) is str
            and type(fact(item, "victim_template_name")) is str
        )
        if not candidates:
            return unavailable_value("combat.turning_point_timing", context.scope, window, "no_candidate_killing_blows", BASE_REGISTRY)
        selected: list[tuple[ObservedEvidence, tuple[ObservedEvidence, ...]]] = []
        failure_reasons: list[str] = []
        for candidate in candidates:
            assert candidate.frame is not None
            attacker_object_id = fact(candidate, "attacker_object_id")
            victim_object_id = fact(candidate, "victim_object_id")
            opponent = fact(candidate, "victim_replay_player_public_id")
            if type(attacker_object_id) is not int or type(victim_object_id) is not int or type(opponent) is not str or opponent == context.replay_player_public_id:
                failure_reasons.append("insufficient_engagement_evidence")
                continue
            nearby_taken = tuple(
                item
                for item in taken_events
                if item.frame is not None
                and abs(item.frame - candidate.frame) <= self._ENGAGEMENT_WINDOW_FRAMES
                and fact(item, "source_replay_player_public_ids") == (opponent,)
                and fact(item, "victim_replay_player_public_id") == context.replay_player_public_id
                and fact(item, "attacker_object_id") == victim_object_id
                and fact(item, "victim_object_id") == attacker_object_id
            )
            if not nearby_taken:
                failure_reasons.append("insufficient_engagement_evidence")
                continue
            nearby_spatial = tuple(
                item
                for item in context.observed
                if item.event_type == "entity_sample"
                and item.frame is not None
                and abs(item.frame - candidate.frame) <= self._ENGAGEMENT_WINDOW_FRAMES
                and fact(item, "object_id") == attacker_object_id
                and fact(item, "owner_scope_key") == context.replay_player_public_id
                and self._has_position(item)
            )
            if not nearby_spatial:
                failure_reasons.append("insufficient_spatial_context")
                continue
            if nearby_taken and nearby_spatial:
                selected.append((candidate, (*nearby_taken, *nearby_spatial)))
        if not selected:
            reason = failure_reasons[0] if failure_reasons else "insufficient_engagement_evidence"
            return unavailable_value("combat.turning_point_timing", context.scope, window, reason, BASE_REGISTRY)
        rows = tuple(
            {
                "frame": candidate.frame,
                "attacker_template_name": fact(candidate, "attacker_template_name"),
                "victim_template_name": fact(candidate, "victim_template_name"),
                "criterion": "engagement-swing-v1",
            }
            for candidate, _support in selected
        )
        refs = tuple(ref for candidate, support in selected for ref in (candidate.ref, *(item.ref for item in support)))
        return complete_value(
            "combat.turning_point_timing",
            rows,
            context.scope,
            window,
            tuple(dict.fromkeys(refs)),
            BASE_REGISTRY,
            details={
                "criterion_version": "engagement-swing-v1",
                "interpretation": "reciprocal engagement swing candidate; not proof of strategic causality",
                "window_frames": self._ENGAGEMENT_WINDOW_FRAMES,
            },
        )

    @staticmethod
    def _has_position(item: ObservedEvidence) -> bool:
        position = thaw_canonical(fact(item, "position"))
        return (
            isinstance(position, dict)
            and type(position.get("x")) in (int, float)
            and type(position.get("y")) in (int, float)
        )

    def _all_unavailable(self, context: FeatureContext, window: FeatureWindow, reason: str) -> FeatureBundle:
        return FeatureBundle(
            self.name,
            self.version,
            tuple(unavailable_value(name, context.scope, window, reason, BASE_REGISTRY) for name in self.feature_names),
        )
