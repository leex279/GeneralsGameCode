"""Observed construction-completion build-order features."""

from __future__ import annotations

from typing import cast

from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureWindow,
    complete_value,
    unavailable_value,
)
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import fact
from generals_replay_analyzer.features.registry import BASE_REGISTRY


# TheSuperHackers @feature Leex 22/08/2026 Derive build milestones only from observed construction completion. (#TBD)
class BuildOrderExtractor:
    name = "build"
    version = "build-v1"
    feature_names = (
        "build.completed_count",
        "build.completed_sequence",
        "build.first_completed_frame",
    )

    def extract(self, context: FeatureContext) -> FeatureBundle:
        window = FeatureWindow(0, context.final_frame or 0)
        if context.telemetry_status != "succeeded":
            return self._unavailable(context, window, "missing_successful_telemetry")
        completions = tuple(item for item in context.observed if item.event_type == "construction_completed")
        player_id = context.replay_player_public_id
        if any(fact(item, "replay_player_public_id") != player_id for item in completions):
            return self._unavailable(context, window, "ambiguous_player_attribution")
        if not completions:
            return self._unavailable(context, window, "no_observed_events")
        if any(item.frame is None or type(fact(item, "template_name")) is not str for item in completions):
            return self._unavailable(context, window, "no_observed_events")
        refs = tuple(item.ref for item in completions)
        sequence = tuple(
            {"frame": cast(int, item.frame), "template_name": cast(str, fact(item, "template_name"))}
            for item in completions
        )
        values = (
            complete_value("build.completed_count", len(completions), context.scope, window, refs, BASE_REGISTRY),
            complete_value("build.completed_sequence", sequence, context.scope, window, refs, BASE_REGISTRY),
            complete_value(
                "build.first_completed_frame", cast(int, completions[0].frame), context.scope, window, refs, BASE_REGISTRY
            ),
        )
        return FeatureBundle(self.name, self.version, values)

    def _unavailable(self, context: FeatureContext, window: FeatureWindow, reason: str) -> FeatureBundle:
        values = tuple(unavailable_value(name, context.scope, window, reason, BASE_REGISTRY) for name in self.feature_names)
        return FeatureBundle(self.name, self.version, values)
