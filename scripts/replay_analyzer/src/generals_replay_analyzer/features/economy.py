"""Observed cash, supply, and terminal-balance features."""

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
from generals_replay_analyzer.features.evidence import ObservedEvidence, fact
from generals_replay_analyzer.features.registry import BASE_REGISTRY


# TheSuperHackers @feature Leex 22/08/2026 Measure economy only from signed cash and observed supply evidence. (#TBD)
class EconomyExtractor:
    name = "economy"
    version = "economy-v1"
    feature_names = (
        "economy.cash_balance_final",
        "economy.cash_change_total",
        "economy.supply_collected_total",
        "economy.supply_collection_rate",
        "economy.supply_source_resolved_share",
        "economy.tracked_income_total",
    )

    def extract(self, context: FeatureContext) -> FeatureBundle:
        window = FeatureWindow(0, context.final_frame or 0)
        if context.telemetry_status != "succeeded":
            return FeatureBundle(
                self.name,
                self.version,
                tuple(
                    unavailable_value(name, context.scope, window, "missing_successful_telemetry", BASE_REGISTRY)
                    for name in self.feature_names
                ),
            )
        relevant = tuple(
            item
            for item in context.observed
            if item.event_type in ("cash_changed", "supply_collected", "complete")
        )
        if any(fact(item, "replay_player_public_id") != context.replay_player_public_id for item in relevant):
            return FeatureBundle(
                self.name,
                self.version,
                tuple(
                    unavailable_value(name, context.scope, window, "ambiguous_player_attribution", BASE_REGISTRY)
                    for name in self.feature_names
                ),
            )
        cash = tuple(item for item in relevant if item.event_type == "cash_changed")
        supply = tuple(item for item in relevant if item.event_type == "supply_collected")
        terminal = tuple(item for item in relevant if item.event_type == "complete")
        values: list[FeatureValue] = []
        values.extend(self._cash_values(context, window, cash))
        values.extend(self._supply_values(context, window, supply))
        if len(terminal) == 1 and type(fact(terminal[0], "final_cash_balance")) in (int, float):
            values.append(
                complete_value(
                    "economy.cash_balance_final",
                    fact(terminal[0], "final_cash_balance"),
                    context.scope,
                    window,
                    (terminal[0].ref,),
                    BASE_REGISTRY,
                )
            )
        else:
            values.append(
                unavailable_value(
                    "economy.cash_balance_final",
                    context.scope,
                    window,
                    "missing_complete_terminal_record",
                    BASE_REGISTRY,
                )
            )
        return FeatureBundle(self.name, self.version, tuple(sorted(values, key=lambda value: value.name)))

    def _cash_values(
        self, context: FeatureContext, window: FeatureWindow, events: tuple[ObservedEvidence, ...]
    ) -> tuple[FeatureValue, ...]:
        if not events or any(type(fact(item, "delta")) not in (int, float) for item in events):
            return tuple(
                unavailable_value(name, context.scope, window, "no_observed_events", BASE_REGISTRY)
                for name in ("economy.cash_change_total", "economy.tracked_income_total")
            )
        refs = tuple(item.ref for item in events)
        total = sum(cast(int | float, fact(item, "delta")) for item in events)
        tracked = sum(
            cast(int | float, fact(item, "delta")) for item in events if fact(item, "track_income") is True
        )
        return (
            complete_value("economy.cash_change_total", total, context.scope, window, refs, BASE_REGISTRY),
            complete_value("economy.tracked_income_total", tracked, context.scope, window, refs, BASE_REGISTRY),
        )

    def _supply_values(
        self, context: FeatureContext, window: FeatureWindow, events: tuple[ObservedEvidence, ...]
    ) -> tuple[FeatureValue, ...]:
        names = (
            "economy.supply_collected_total",
            "economy.supply_collection_rate",
            "economy.supply_source_resolved_share",
        )
        if not events or any(type(fact(item, "amount")) not in (int, float) or item.frame is None for item in events):
            return tuple(unavailable_value(name, context.scope, window, "no_observed_events", BASE_REGISTRY) for name in names)
        refs = tuple(item.ref for item in events)
        total = sum(cast(int | float, fact(item, "amount")) for item in events)
        values = [complete_value(names[0], total, context.scope, window, refs, BASE_REGISTRY)]
        duration = cast(int, events[-1].frame) - cast(int, events[0].frame)
        if duration <= 0:
            values.append(
                unavailable_value(
                    names[1],
                    context.scope,
                    window,
                    "insufficient_observed_duration",
                    BASE_REGISTRY,
                    input_evidence=refs,
                    details={"first_frame": events[0].frame, "last_frame": events[-1].frame, "duration_frames": duration},
                )
            )
        else:
            rate = total * 1800.0 / duration
            values.append(
                complete_value(
                    names[1],
                    rate,
                    context.scope,
                    window,
                    refs,
                    BASE_REGISTRY,
                    details={"first_frame": events[0].frame, "last_frame": events[-1].frame, "duration_frames": duration},
                )
            )
        statuses = tuple(fact(item, "source_status") for item in events)
        if any(status not in ("resolved", "unknown", "mixed") for status in statuses):
            values.append(
                unavailable_value(
                    names[2], context.scope, window, "missing_source_attribution", BASE_REGISTRY, input_evidence=refs
                )
            )
        else:
            resolved = sum(status == "resolved" for status in statuses)
            values.append(complete_value(names[2], resolved / len(events), context.scope, window, refs, BASE_REGISTRY))
        return tuple(values)
