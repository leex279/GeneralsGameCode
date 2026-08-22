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
from generals_replay_analyzer.features.evidence import fact
from generals_replay_analyzer.features.registry import BASE_REGISTRY


# TheSuperHackers @feature Leex 22/08/2026 Exclude unknown damage sources instead of guessing combat attribution. (#TBD)
class CombatExtractor:
    name = "combat"
    version = "combat-v1"
    feature_names = (
        "combat.applied_damage_dealt",
        "combat.applied_damage_taken",
        "combat.killing_blow_count",
        "combat.observed_damage_trade_ratio",
    )

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

    def _all_unavailable(self, context: FeatureContext, window: FeatureWindow, reason: str) -> FeatureBundle:
        return FeatureBundle(
            self.name,
            self.version,
            tuple(unavailable_value(name, context.scope, window, reason, BASE_REGISTRY) for name in self.feature_names),
        )
