"""Deterministic strategy-first projections for opponent scouting."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.web.ports import (
    PlayerInsightDTO,
    PlayerProfileDTO,
    PlayerSummaryDTO,
)

_GAME_FPS = 30.0


class ScoutingEvidenceLinkViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    href: str


class ScoutingSignalViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    value: str
    meaning: str
    sample_label: str


class ScoutingOpeningViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    sequence: tuple[str, ...]
    frequency_label: str
    frequency_percent: int | None
    sample_label: str
    confidence_label: str
    confidence_range: str | None
    timing_label: str
    threat_level: Literal["HIGH", "MEDIUM", "EMERGING", "UNESTABLISHED"]
    threat_copy: str
    counter_plan: str
    evidence: tuple[ScoutingEvidenceLinkViewModel, ...]


class ScoutingPlanStepViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    number: str
    title: str
    instruction: str
    evidence_label: str


class ScoutingWorkspaceViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    players: tuple[PlayerSummaryDTO, ...]
    selected_player: PlayerSummaryDTO | None
    selected_player_public_id: str | None
    history_count: int
    engine_verified_history_count: int
    faction_context: tuple[str, ...]
    map_context: tuple[str, ...]
    primary_opening: ScoutingOpeningViewModel | None
    alternative_openings: tuple[ScoutingOpeningViewModel, ...]
    signals: tuple[ScoutingSignalViewModel, ...]
    plan: tuple[ScoutingPlanStepViewModel, ...]
    unavailable_reason: str | None = None


def _clock(frame: float) -> str:
    seconds = max(0, round(frame / _GAME_FPS))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _humanize_template(value: str) -> str:
    name = re.sub(r"^(?:America|China|GLA)", "", value)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return name.strip() or value


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _opening_payload(
    insight: PlayerInsightDTO,
    statistics: Mapping[str, object] | None,
) -> tuple[float | None, tuple[str, ...]]:
    raw: object = insight.raw_value if statistics is None else statistics
    raw_share = _numeric(raw)
    if raw_share is not None:
        return (raw_share if 0.0 <= raw_share <= 1.0 else None), ()
    if isinstance(raw, dict):
        candidate_share = _numeric(raw.get("share"))
        share = candidate_share if candidate_share is not None and 0.0 <= candidate_share <= 1.0 else None
        prefix = raw.get("recurring_prefix")
        if isinstance(prefix, (list, tuple)):
            sequence = tuple(_humanize_template(item) for item in prefix if isinstance(item, str) and item)
        else:
            sequence = ()
        return share, sequence
    return None, ()


def _timing(insights: tuple[PlayerInsightDTO, ...]) -> tuple[str, str]:
    for insight in insights:
        raw_timing = _numeric(insight.raw_value)
        if (
            insight.insight_kind == "timing_distribution"
            and insight.availability.state in {"available", "partial"}
            and raw_timing is not None
            and insight.unit == "frames"
        ):
            center = _clock(raw_timing)
            if insight.interval is not None:
                window = f"{_clock(insight.interval.lower)}\u2013{_clock(insight.interval.upper)} evidence range"
            else:
                window = f"{center} observed median"
            return f"Primary timing {center}", window
    return "Timing not established", "No repeated pressure timing is established yet"


def _threat(share: float | None) -> tuple[Literal["HIGH", "MEDIUM", "EMERGING", "UNESTABLISHED"], str]:
    if share is None:
        return "UNESTABLISHED", "The opening exists in the evidence, but its repeat rate is not established."
    if share >= 0.65:
        return "HIGH", "This is the opponent's dominant opening and should shape your first scouting decision."
    if share >= 0.40:
        return "MEDIUM", "This opening is common enough to prepare for without assuming it every match."
    return "EMERGING", "Treat this as a credible branch, then confirm it before committing your counter."


def _counter_plan(sequence: tuple[str, ...]) -> str:
    combined = " ".join(sequence).casefold()
    if any(token in combined for token in ("barracks", "war factory", "arms dealer", "airfield")):
        return "Confirm the production building early, hold the first unit wave, then punish the economy window behind it."
    if any(token in combined for token in ("supply", "stash", "worker", "derrick")):
        return "Pressure the exposed economy before it pays back, while keeping enough defense for the first counter-wave."
    return "Scout before committing, keep production flexible, and punish the first confirmed tech or economy trade-off."


def _evidence_links(insight: PlayerInsightDTO, report_public_id: str | None) -> tuple[ScoutingEvidenceLinkViewModel, ...]:
    if report_public_id is None:
        return ()
    return tuple(
        ScoutingEvidenceLinkViewModel(
            label=f"Review {item.tier} evidence",
            href=f"/evidence/{item.tier}/{item.evidence_public_id}?report_id={report_public_id}",
        )
        for item in insight.evidence
    )


def _opening(
    insight: PlayerInsightDTO,
    *,
    timing_label: str,
    report_public_id: str | None,
    statistics: Mapping[str, object] | None,
) -> ScoutingOpeningViewModel:
    share, sequence = _opening_payload(insight, statistics)
    threat_level, threat_copy = _threat(share)
    interval = insight.interval
    confidence_range = None
    confidence_label = "Confidence unavailable" if interval is None else f"{round(interval.confidence_level * 100)}% confidence"
    if statistics is not None:
        interval_value = statistics.get("wilson_interval")
        confidence_value = _numeric(statistics.get("confidence_level"))
        if (
            isinstance(interval_value, (list, tuple))
            and len(interval_value) == 2
            and (lower := _numeric(interval_value[0])) is not None
            and (upper := _numeric(interval_value[1])) is not None
            and 0.0 <= lower <= upper <= 1.0
        ):
            confidence_range = f"Likely range {round(lower * 100)}\u2013{round(upper * 100)}%"
        if confidence_value is not None and 0.0 < confidence_value < 1.0:
            confidence_label = f"{round(confidence_value * 100)}% confidence"
    elif interval is not None and 0.0 <= interval.lower <= interval.upper <= 1.0:
        confidence_range = f"Likely range {round(interval.lower * 100)}\u2013{round(interval.upper * 100)}%"
    return ScoutingOpeningViewModel(
        title=insight.label,
        sequence=sequence,
        frequency_label="Frequency not established" if share is None else f"{round(share * 100)}%",
        frequency_percent=None if share is None else round(share * 100),
        sample_label=f"{insight.sample_count} accepted match{'es' if insight.sample_count != 1 else ''}",
        confidence_label=confidence_label,
        confidence_range=confidence_range,
        timing_label=timing_label,
        threat_level=threat_level,
        threat_copy=threat_copy,
        counter_plan=_counter_plan(sequence),
        evidence=_evidence_links(insight, report_public_id),
    )


_SIGNAL_MEANINGS = {
    "timing_distribution": "Use this timing to decide when your defense and first scout must already be in position.",
    "transition_preference": "This is the most repeated follow-up; verify it before locking into counter-tech.",
    "spatial_habit": "Protect the repeated route or map area, but keep vision on the alternative lane.",
    "personal_baseline_deviation": "Expect this behavior to differ from the player's normal pace in this cohort.",
    "opponent_associated_difference": "This tendency changes in the selected matchup; avoid applying it to every opponent.",
    "trend": "Recent matches are moving in this direction, so older replays should carry less weight.",
    "change_point_candidate": "A possible style change is present; treat it as provisional until more matches confirm it.",
    "consistency": "This indicates how reliably the player repeats the measured behavior.",
}


def _signal_value(insight: PlayerInsightDTO) -> str:
    value = _numeric(insight.raw_value)
    if value is not None:
        if insight.unit == "frames":
            return _clock(value)
        return f"{value:g}{' ' + insight.unit if insight.unit else ''}"
    return "Observed pattern"


# TheSuperHackers @feature Leex 24/08/2026 Convert immutable longitudinal evidence into an understandable opponent response plan. (#TBD)
def scouting_workspace_view(
    players: tuple[PlayerSummaryDTO, ...],
    profile: PlayerProfileDTO | None,
    *,
    selected_player_public_id: str | None,
    unavailable_reason: str | None = None,
    opening_statistics: Mapping[str, Mapping[str, object]] | None = None,
    opening_evidence_report_ids: Mapping[str, str] | None = None,
) -> ScoutingWorkspaceViewModel:
    if profile is None:
        selected = next((item for item in players if item.player_public_id == selected_player_public_id), None)
        return ScoutingWorkspaceViewModel(
            players=players,
            selected_player=selected,
            selected_player_public_id=selected_player_public_id,
            history_count=0,
            engine_verified_history_count=0,
            faction_context=(),
            map_context=(),
            primary_opening=None,
            alternative_openings=(),
            signals=(),
            plan=(),
            unavailable_reason=unavailable_reason,
        )

    supported = tuple(
        item
        for item in profile.insights
        if item.availability.state in {"available", "partial"}
        and item.raw_value is not None
        and item.sample_count > 0
    )
    timing_label, timing_window = _timing(supported)
    report_public_id = (
        profile.version.fixed_reports[0].report_public_id
        if len(profile.version.fixed_reports) == 1
        else None
    )
    statistics_by_result = opening_statistics or {}
    evidence_reports_by_result = opening_evidence_report_ids or {}
    openings = tuple(
        _opening(
            item,
            timing_label=timing_label,
            report_public_id=evidence_reports_by_result.get(item.result_public_id, report_public_id),
            statistics=statistics_by_result.get(item.result_public_id),
        )
        for item in supported
        if item.insight_kind == "recurring_opening"
    )
    openings = tuple(
        sorted(
            openings,
            key=lambda item: (-(item.frequency_percent if item.frequency_percent is not None else -1), item.title),
        )
    )
    signals = tuple(
        ScoutingSignalViewModel(
            label=item.label,
            value=_signal_value(item),
            meaning=_SIGNAL_MEANINGS.get(item.insight_kind, "Use this as supporting context, not a standalone prediction."),
            sample_label=f"{item.sample_count} samples",
        )
        for item in supported
        if item.insight_kind != "recurring_opening"
    )
    primary = openings[0] if openings else None
    plan: tuple[ScoutingPlanStepViewModel, ...] = ()
    if primary is not None:
        sequence = " \u2192 ".join(primary.sequence) if primary.sequence else primary.title
        plan = (
            ScoutingPlanStepViewModel(
                number="01",
                title="Confirm the opening",
                instruction=f"Scout for {sequence}. Do not commit the full counter until the sequence is visible.",
                evidence_label=f"{primary.frequency_label} across {primary.sample_label}",
            ),
            ScoutingPlanStepViewModel(
                number="02",
                title="Prepare before the timing",
                instruction=f"{primary.timing_label}. Have the first safe response ready before that contact window.",
                evidence_label=timing_window,
            ),
            ScoutingPlanStepViewModel(
                number="03",
                title="Punish the trade-off",
                instruction=primary.counter_plan,
                evidence_label=f"{primary.threat_level} preparation priority",
            ),
        )

    factions = tuple(sorted({item.faction for item in profile.replay_history if item.faction and not item.faction.isdigit()}))
    maps = tuple(sorted({item.map_display_name for item in profile.replay_history if item.map_display_name}))
    return ScoutingWorkspaceViewModel(
        players=players,
        selected_player=profile.player,
        selected_player_public_id=profile.player.player_public_id,
        history_count=profile.history_total_items,
        engine_verified_history_count=profile.engine_verified_history_count,
        faction_context=factions,
        map_context=maps,
        primary_opening=primary,
        alternative_openings=openings[1:],
        signals=signals,
        plan=plan,
        unavailable_reason=unavailable_reason,
    )
