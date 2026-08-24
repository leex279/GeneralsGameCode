"""Observed terminal ScoreKeeper snapshots with deliberately caveated event folds."""

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

_METRICS = (
    "money_earned",
    "money_spent",
    "units_built",
    "units_lost",
    "units_destroyed",
    "buildings_built",
    "buildings_lost",
    "buildings_destroyed",
    "tech_buildings_captured",
    "faction_buildings_captured",
)
_NO_COMPARISON_REASON = "no_semantically_comparable_event_total"
_EVENT_RULES = {
    "money_earned": ("cash_changed", "event_income_is_not_scorekeeper_money_earned"),
    "money_spent": ("cash_changed", "event_cash_debits_are_not_scorekeeper_money_spent"),
    "units_built": ("production_completed", "event_completion_is_not_scorekeeper_unit_building_classification"),
    "buildings_built": ("construction_completed", "event_completion_is_not_scorekeeper_unit_building_classification"),
}


# TheSuperHackers @feature Leex 23/08/2026 Preserve raw terminal score totals and never elevate semantic deltas to trace failure. (#0)
class ScoreKeeperExtractor:
    name = "scorekeeper"
    version = "scorekeeper-v1"
    feature_names = ("scorekeeper.event_reconciliation", "scorekeeper.terminal_snapshot")

    def extract(self, context: FeatureContext) -> FeatureBundle:
        window = FeatureWindow(0, context.final_frame or 0)
        if context.telemetry_status != "succeeded":
            return self._unavailable(context, window, "missing_successful_telemetry")
        snapshots = tuple(item for item in context.observed if item.event_type == "scorekeeper_snapshot")
        if len(snapshots) != 1:
            return self._unavailable(context, window, "missing_scorekeeper_terminal_snapshot")
        snapshot = snapshots[0]
        totals = {metric: fact(snapshot, metric) for metric in _METRICS}
        scoring_enabled = fact(snapshot, "scoring_enabled")
        if any(type(value) is not int for value in totals.values()) or type(scoring_enabled) is not bool:
            return self._unavailable(context, window, "invalid_scorekeeper_terminal_snapshot")
        score_totals = {metric: cast(int, totals[metric]) for metric in _METRICS}
        terminal_snapshot = {"scoring_enabled": scoring_enabled, **score_totals}
        terminal = complete_value(
            "scorekeeper.terminal_snapshot", terminal_snapshot, context.scope, window, (snapshot.ref,), BASE_REGISTRY
        )
        reconciliation: dict[str, object] = {}
        evidence = [snapshot.ref]
        for metric in _METRICS:
            if not scoring_enabled:
                reconciliation[metric] = {
                    "engine_total": score_totals[metric], "event_total": None, "delta": None,
                    "status": "not_comparable", "reason": "scorekeeper_scoring_disabled",
                }
                continue
            rule = _EVENT_RULES.get(metric)
            if rule is None:
                reconciliation[metric] = {
                    "engine_total": score_totals[metric], "event_total": None, "delta": None,
                    "status": "not_comparable", "reason": _NO_COMPARISON_REASON,
                }
                continue
            event_type, reason = rule
            events = tuple(item for item in context.observed if item.event_type == event_type)
            if metric == "money_earned":
                events = tuple(item for item in events if fact(item, "track_income") is True and type(fact(item, "delta")) in (int, float) and cast(int | float, fact(item, "delta")) > 0)
                event_total = sum(cast(int | float, fact(item, "delta")) for item in events)
            elif metric == "money_spent":
                events = tuple(item for item in events if type(fact(item, "delta")) in (int, float) and cast(int | float, fact(item, "delta")) < 0)
                event_total = -sum(cast(int | float, fact(item, "delta")) for item in events)
            else:
                event_total = len(events)
            if not events:
                reconciliation[metric] = {
                    "engine_total": score_totals[metric], "event_total": None, "delta": None,
                    "status": "not_comparable", "reason": _NO_COMPARISON_REASON,
                }
                continue
            evidence.extend(item.ref for item in events)
            reconciliation[metric] = {
                "engine_total": score_totals[metric], "event_total": event_total,
                "delta": score_totals[metric] - event_total,
                "status": "partial", "reason": reason,
            }
        compared = complete_value(
            "scorekeeper.event_reconciliation", reconciliation, context.scope, window,
            tuple(sorted(set(evidence), key=lambda ref: (ref.source_key, ref.public_id))), BASE_REGISTRY,
        )
        return FeatureBundle(self.name, self.version, (compared, terminal))

    def _unavailable(self, context: FeatureContext, window: FeatureWindow, reason: str) -> FeatureBundle:
        return FeatureBundle(
            self.name,
            self.version,
            tuple(unavailable_value(name, context.scope, window, reason, BASE_REGISTRY) for name in self.feature_names),
        )
