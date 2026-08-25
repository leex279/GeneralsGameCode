"""Observed production and upgrade terminal-record features."""

from __future__ import annotations

from collections import Counter
from typing import cast

from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureValue,
    FeatureWindow,
    complete_value,
    unavailable_value,
)
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence, fact
from generals_replay_analyzer.features.registry import BASE_REGISTRY

_EVENTS = {
    "production_queued",
    "production_cancelled",
    "production_completed",
    "upgrade_queued",
    "upgrade_cancelled",
    "upgrade_completed",
    "science_purchased",
    "special_power_used",
}


# TheSuperHackers @feature Leex 22/08/2026 Pair production durations only across observed queue and terminal identities. (#TBD)
class ProductionExtractor:
    name = "production"
    version = "production-v2"
    feature_names = (
        "production.cancelled_count",
        "production.completed_composition",
        "production.completed_count",
        "production.observed_duration_frames",
        "production.queued_count",
        "production.science_purchase_timing",
        "production.special_power_timing",
    )

    def extract(self, context: FeatureContext) -> FeatureBundle:
        window = FeatureWindow(0, context.final_frame or 0)
        if context.telemetry_status != "succeeded":
            return self._all_unavailable(context, window, "missing_successful_telemetry")
        if not context.catalog_identity:
            return self._all_unavailable(context, window, "missing_catalog_identity")
        events = tuple(item for item in context.observed if item.event_type in _EVENTS)
        if any(fact(item, "replay_player_public_id") != context.replay_player_public_id for item in events):
            return self._all_unavailable(context, window, "ambiguous_player_attribution")
        if not events:
            return self._all_unavailable(context, window, "no_observed_events")
        refs = tuple(item.ref for item in events)
        queued = tuple(item for item in events if item.event_type.endswith("_queued"))
        completed = tuple(item for item in events if item.event_type.endswith("_completed"))
        cancelled = tuple(item for item in events if item.event_type.endswith("_cancelled"))
        # TheSuperHackers @fix Leex 25/08/2026 Bound completion aggregates to their exact observed frames so pre-desync facts remain reviewable. (#TBD)
        completed_window = self._event_window(completed, window)
        completed_refs = tuple(item.ref for item in completed) or refs
        composition = Counter(
            cast(str, fact(item, "item_name"))
            for item in completed
            if type(fact(item, "item_name")) is str
        )
        values: list[FeatureValue] = [
            complete_value("production.cancelled_count", len(cancelled), context.scope, window, refs, BASE_REGISTRY),
            complete_value(
                "production.completed_composition",
                dict(sorted(composition.items())),
                context.scope,
                completed_window,
                completed_refs,
                BASE_REGISTRY,
            ),
            complete_value(
                "production.completed_count",
                len(completed),
                context.scope,
                completed_window,
                completed_refs,
                BASE_REGISTRY,
            ),
            complete_value("production.queued_count", len(queued), context.scope, window, refs, BASE_REGISTRY),
        ]
        # TheSuperHackers @feature Leex 24/08/2026 Surface observed science and special-power timing without inventing unresolved game labels. (#TBD)
        science = tuple(item for item in context.observed if item.event_type == "science_purchased")
        powers = tuple(item for item in context.observed if item.event_type == "special_power_used")
        for name, timing_events in (
            ("production.science_purchase_timing", science),
            ("production.special_power_timing", powers),
        ):
            timing_rows = tuple(
                {"frame": item.frame, "item_name": fact(item, "item_name")}
                for item in timing_events
                if item.frame is not None and type(fact(item, "item_name")) is str
            )
            if timing_rows:
                values.append(
                    complete_value(
                        name,
                        timing_rows,
                        context.scope,
                        window,
                        tuple(item.ref for item in timing_events),
                        BASE_REGISTRY,
                    )
                )
            else:
                values.append(
                    unavailable_value(name, context.scope, window, "no_observed_events", BASE_REGISTRY)
                )
        durations, duration_inputs = self._durations(queued, completed + cancelled)
        if durations:
            values.append(
                complete_value(
                    "production.observed_duration_frames",
                    durations,
                    context.scope,
                    window,
                    duration_inputs,
                    BASE_REGISTRY,
                    details={"catalog_identity": context.catalog_identity},
                )
            )
        else:
            values.append(
                unavailable_value(
                    "production.observed_duration_frames",
                    context.scope,
                    window,
                    "missing_complete_terminal_record",
                    BASE_REGISTRY,
                    input_evidence=refs,
                    details={"catalog_identity": context.catalog_identity},
                )
            )
        return FeatureBundle(self.name, self.version, tuple(sorted(values, key=lambda value: value.name)))

    @staticmethod
    def _event_window(events: tuple[ObservedEvidence, ...], fallback: FeatureWindow) -> FeatureWindow:
        frames = tuple(item.frame for item in events if item.frame is not None)
        return fallback if not frames else FeatureWindow(min(frames), max(frames))

    def _durations(
        self,
        queued: tuple[ObservedEvidence, ...],
        terminal: tuple[ObservedEvidence, ...],
    ) -> tuple[tuple[dict[str, object], ...], tuple[EvidenceRef, ...]]:
        queued_by_identity = {
            cast(str, fact(item, "production_identity")): item
            for item in queued
            if type(fact(item, "production_identity")) is str and item.frame is not None
        }
        durations: list[dict[str, object]] = []
        evidence: list[EvidenceRef] = []
        for item in terminal:
            identity = fact(item, "production_identity")
            if type(identity) is not str or item.frame is None or identity not in queued_by_identity:
                continue
            start = queued_by_identity[identity]
            duration = item.frame - cast(int, start.frame)
            if duration < 0:
                continue
            durations.append(
                {"duration_frames": duration, "identity": identity, "item_name": fact(item, "item_name")}
            )
            evidence.extend((start.ref, item.ref))
        durations.sort(key=lambda item: cast(str, item["identity"]))
        unique_evidence = {item.public_id: item for item in evidence}
        return tuple(durations), tuple(unique_evidence.values())

    def _all_unavailable(self, context: FeatureContext, window: FeatureWindow, reason: str) -> FeatureBundle:
        return FeatureBundle(
            self.name,
            self.version,
            tuple(unavailable_value(name, context.scope, window, reason, BASE_REGISTRY) for name in self.feature_names),
        )
