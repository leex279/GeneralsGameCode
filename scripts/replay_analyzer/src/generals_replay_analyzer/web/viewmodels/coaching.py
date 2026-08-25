"""Deterministic player coaching projected only from immutable report evidence."""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.presentation import feature_label, format_frame, game_label, phase_label, strategy_label
from generals_replay_analyzer.report.model import CanonicalValue, thaw_report_value
from generals_replay_analyzer.web.ports import (
    ReplayPlayerDisplayDTO,
    ReplayReportDTO,
    ReportClaimDTO,
    ReportEvidenceReferenceDTO,
    TimelineChartDTO,
)

# TheSuperHackers @feature Leex 24/08/2026 Project observed power timings into player evidence highlights with raw-name fallbacks. (#TBD)
_PHASE_RANK = MappingProxyType({"opening": 0, "early": 1, "mid": 2, "late": 3, "cross_phase": 4})
# TheSuperHackers @feature Leex 24/08/2026 Present engagement swing candidates beside observed kills while preserving the no-strategic-causality boundary. (#TBD)
_HIGHLIGHT_ORDER = (
    "economy.supply_collection_rate",
    "economy.supply_collected_total",
    "production.completed_composition",
    "production.science_purchase_timing",
    "production.special_power_timing",
    "combat.turning_point_timing",
    "combat.observed_kill_timing",
    "scouting.first_observed_clear_timing",
    "combat.observed_damage_trade_ratio",
    "activity.effective_actions_per_minute",
)
_HIGHLIGHT_EXPLANATIONS = MappingProxyType(
    {
        "economy.supply_collection_rate": "Measured from observed supply collection events in this report.",
        "economy.supply_collected_total": "Total supplies recorded inside the available evidence horizon.",
        "production.completed_composition": "Completed units and upgrades observed in the available trace.",
        "production.special_power_timing": "Observed special-power uses with engine-provided names and frame timings; names remain raw when unrecognized.",
        "production.science_purchase_timing": "Observed science purchases with engine-provided names and frame timings; names remain raw when unrecognized.",
        "combat.observed_kill_timing": "Observed killing blows with engine-provided frame and template facts; these do not establish strategic causality.",
        "combat.turning_point_timing": "Reciprocal engagement swing candidates require the versioned combat and spatial criterion; they are not proof of strategic causality.",
        "scouting.first_observed_clear_timing": "Engine visibility transitions show when an object was first observed clear by this player.",
        "combat.observed_damage_trade_ratio": "Observed applied damage dealt divided by observed damage taken.",
        "activity.effective_actions_per_minute": "Supported replay orders per observed minute, not raw click APM.",
    }
)
# TheSuperHackers @bugfix Leex 25/08/2026 Separate narrative evidence lists from compact numeric report metrics. (#TBD)
_NARRATIVE_HIGHLIGHTS = frozenset(
    {
        "production.completed_composition",
        "production.science_purchase_timing",
        "production.special_power_timing",
        "combat.turning_point_timing",
        "combat.observed_kill_timing",
        "scouting.first_observed_clear_timing",
    }
)
_ADVICE = MappingProxyType(
    {
        "all_in_aggression": "Review whether the larger Humvee count was supported by steady supply income and unit preservation.",
        "china_dual_war_factory_pressure": "Review whether both War Factories stayed active without delaying supply collection.",
        "china_fast_propaganda_center": "Review whether the Propaganda Center timing converted into useful upgrades or advanced units.",
        "china_helix_pressure": "Review the Helix timing, cargo choice, and whether its first exposure created value.",
        "china_infantry_pressure": "Review whether the Red Guard force arrived together and avoided inefficient pathing or trades.",
        "defensive_opening": "Review whether both Firebases protected an important route without overcommitting resources.",
        "economic_expansion": "Review whether the second Supply Center became productive quickly enough to repay its cost.",
        "gla_dual_arms_dealer_pressure": "Review whether both Arms Dealers stayed productive and supported one coordinated attack plan.",
        "gla_fast_palace": "Review whether the Palace timing unlocked an immediate, useful transition.",
        "gla_forward_tunnel_pressure": "Review whether the Tunnel created safe reinforcement access before Workers were exposed.",
        "gla_technical_aggression": "Review the first Technical's timing, passenger choice, and exit route.",
        "gla_terror_tech": "Review whether the Terrorists and Technical were synchronized before entering defended space.",
        "oil_capture": "Review whether the Oil Derrick investment was protected long enough to create value.",
        "usa_combat_chinook_pressure": "Review the Combat Chinook timing, infantry loadout, and exposure to anti-air fire.",
        "usa_defensive_firebase_expansion": "Review whether the Firebase protected economy or map access without delaying production.",
        "usa_dual_airfield": "Review whether both Airfields stayed productive without starving the ground economy.",
        "usa_fast_strategy_center": "Review whether the Strategy Center timing led to a timely battle plan or tech transition.",
        "usa_humvee_pressure": "Review whether Humvee production had steady supply income and timely healing or evacuation.",
    }
)


class _FrozenView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceHorizonView(_FrozenView):
    status: Literal["complete", "partial", "unavailable"]
    title: str
    frame_end: int | None
    description: str


class PlayerStrategyView(_FrozenView):
    strategy_id: str
    title: str
    player_label: str
    faction_label: str | None
    phase: str
    phase_label: str
    quality: Literal["available", "partial"]
    evidence_score: float | None
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class BuildOrderStepView(_FrozenView):
    frame: int
    time_label: str
    player_label: str
    structure_label: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class CoachingHighlightView(_FrozenView):
    signal_id: str
    layout: Literal["metric", "narrative"]
    title: str
    value: str
    explanation: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class ReviewPromptView(_FrozenView):
    strategy_id: str
    text: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class SignalReadView(_FrozenView):
    """A bounded interpretation of one measured signal, never a causal claim."""

    title: str
    statement: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class KeyMomentView(_FrozenView):
    frame: int
    map_frame_start: int
    map_frame_end: int
    time_label: str
    category_label: str
    title: str
    review_prompt: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


# TheSuperHackers @bugfix Leex 25/08/2026 Keep one stable claim reference per opening-lane event so dense report evidence remains usable. (#TBD)
class OpeningLaneEventView(_FrozenView):
    frame: int
    time_label: str
    title: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class OpeningLaneView(_FrozenView):
    lane_id: Literal["build", "scouting", "combat", "powers"]
    title: str
    description: str
    empty_message: str
    events: tuple[OpeningLaneEventView, ...]


class CoachingViewModel(_FrozenView):
    schema_version: Literal["coaching-view-v1"] = "coaching-view-v1"
    horizon: EvidenceHorizonView
    summary: str
    strategies: tuple[PlayerStrategyView, ...]
    build_order: tuple[BuildOrderStepView, ...]
    highlights: tuple[CoachingHighlightView, ...]
    signal_reads: tuple[SignalReadView, ...]
    prompts: tuple[ReviewPromptView, ...]
    key_moments: tuple[KeyMomentView, ...]
    opening_lanes: tuple[OpeningLaneView, ...]
    limitations: tuple[str, ...]
    local_model_summary: str | None


def _thaw(value: object) -> object:
    return thaw_report_value(cast(CanonicalValue, value))


def _selected_player(report: ReplayReportDTO) -> ReplayPlayerDisplayDTO | None:
    player_id = report.version.replay_player_public_id
    return next((player for player in report.players if player.replay_player_public_id == player_id), None)


def _claim_evidence(claim: ReportClaimDTO) -> tuple[ReportEvidenceReferenceDTO, ...]:
    return tuple(sorted(claim.evidence, key=lambda item: (item.tier, item.public_id)))


# TheSuperHackers @bugfix Leex 25/08/2026 Merge evidence only when coaching facts are semantically identical. (#TBD)
def _merge_evidence(
    first: tuple[ReportEvidenceReferenceDTO, ...],
    second: tuple[ReportEvidenceReferenceDTO, ...],
) -> tuple[ReportEvidenceReferenceDTO, ...]:
    """Union evidence references in stable order for semantically merged views."""

    merged = {(item.tier, item.public_id): item for item in (*first, *second)}
    return tuple(sorted(merged.values(), key=lambda item: (item.tier, item.public_id)))


def _available_claims(report: ReplayReportDTO, section_key: str) -> tuple[ReportClaimDTO, ...]:
    section = next(section for section in report.sections if section.key == section_key)
    return tuple(claim for claim in section.claims if claim.availability in ("available", "partial"))


def _evidence_end(report: ReplayReportDTO, timeline: TimelineChartDTO) -> int | None:
    frames = [
        claim.frame_window[1]
        for section in report.sections
        for claim in section.claims
        if claim.availability in ("available", "partial") and claim.frame_window is not None and claim.evidence
    ]
    for series in timeline.series:
        if series.availability.state == "unavailable":
            continue
        frames.extend(point.frame for point in series.points)
        frames.extend(interval.frame_end for interval in series.intervals)
    if not frames:
        return None
    observed_end = max(frames)
    # TheSuperHackers @fix Leex 23/08/2026 Cap coaching at the engine's terminal evidence boundary. (#TBD)
    terminal_boundaries = tuple(
        issue.frame_end
        for issue in report.terminal_quality.issues
        if issue.code in {"crc_mismatch", "telemetry_truncated"} and issue.frame_end is not None
    )
    return min(observed_end, *terminal_boundaries) if terminal_boundaries else observed_end


def _horizon(report: ReplayReportDTO, timeline: TimelineChartDTO) -> EvidenceHorizonView:
    duration = report.duration_frames
    # TheSuperHackers @bugfix Leex 25/08/2026 Keep non-terminal projection warnings from truncating engine-verified reports and use the authoritative replay clock. (#TBD)
    complete = (
        report.availability.state in ("available", "partial")
        and report.lifecycle.lifecycle_state == "engine_verified"
        and report.lifecycle.parser_completion_status == "complete"
        and report.lifecycle.telemetry_status in ("complete", "succeeded")
        and report.lifecycle.telemetry_runner_status in ("success", "succeeded")
        and duration is not None
    )
    frames_per_second = timeline.timebase_fps or 30
    if complete:
        assert duration is not None
        return EvidenceHorizonView(
            status="complete",
            title="Complete match evidence",
            frame_end=duration,
            description=(
                "The report covers the complete recorded match through "
                f"{format_frame(duration, frames_per_second=frames_per_second)}."
            ),
        )
    frame_end = _evidence_end(report, timeline)
    if frame_end is None:
        return EvidenceHorizonView(
            status="unavailable",
            title="Evidence horizon unavailable",
            frame_end=None,
            description="The report does not contain a verified frame horizon for player conclusions.",
        )
    clock = format_frame(frame_end, frames_per_second=frames_per_second).split(" (", 1)[0]
    return EvidenceHorizonView(
        status="partial",
        title=f"Observed opening through {clock}",
        frame_end=frame_end,
        description="Conclusions are limited to this observed opening and are not full-match claims.",
    )


def _claim_inside_horizon(claim: ReportClaimDTO, horizon: EvidenceHorizonView) -> bool:
    """Reject aggregate or event claims that can include facts beyond the accepted boundary."""

    if horizon.frame_end is None:
        return False
    # TheSuperHackers @bugfix Leex 25/08/2026 Keep complete engine-verified aggregates whose terminal settlement callback follows the last presentable replay frame. (#TBD)
    if horizon.status == "complete":
        return True
    if claim.frame_window is None or claim.frame_window[1] > horizon.frame_end:
        return False
    raw = _thaw(claim.raw_value)
    return not (
        isinstance(raw, list)
        and any(
            isinstance(item, dict) and type(item.get("frame")) is int and cast(int, item["frame"]) > horizon.frame_end
            for item in raw
        )
    )


def _strategies(report: ReplayReportDTO, horizon: EvidenceHorizonView) -> tuple[PlayerStrategyView, ...]:
    player = _selected_player(report)
    player_label = "Replay" if player is None else player.display_name
    faction = None if player is None or player.faction is None else game_label(player.faction)
    output: dict[tuple[str, str, str, str | None], PlayerStrategyView] = {}
    for claim in _available_claims(report, "strategy_phases"):
        if not _claim_inside_horizon(claim, horizon):
            continue
        raw = _thaw(claim.raw_value)
        if (
            not claim.claim_id.startswith(f"strategy:{claim.label}:")
            or not isinstance(raw, dict)
            or raw.get("strategy_label") != claim.label
            or raw.get("phase") not in _PHASE_RANK
        ):
            continue
        score = raw.get("confidence")
        evidence_score = float(cast(int | float, score)) if type(score) in (int, float) else None
        phase = cast(str, raw["phase"])
        view = PlayerStrategyView(
                strategy_id=claim.label,
                title=strategy_label(claim.label),
                player_label=player_label,
                faction_label=faction,
                phase=phase,
                phase_label=phase_label(phase),
                quality=cast(Literal["available", "partial"], claim.availability),
                evidence_score=evidence_score,
                evidence=_claim_evidence(claim),
            )
        key = (view.strategy_id, view.phase, view.player_label, view.faction_label)
        prior = output.get(key)
        if prior is None:
            output[key] = view
        else:
            output[key] = prior.model_copy(
                update={
                    "quality": "available" if "available" in (prior.quality, view.quality) else "partial",
                    "evidence_score": max(
                        (score for score in (prior.evidence_score, view.evidence_score) if score is not None),
                        default=None,
                    ),
                    "evidence": _merge_evidence(prior.evidence, view.evidence),
                }
            )
    slot = 0 if player is None else player.slot
    return tuple(
        sorted(
            output.values(),
            key=lambda item: (slot, _PHASE_RANK[item.phase], -(item.evidence_score or 0.0), item.strategy_id),
        )
    )


def _build_order(
    report: ReplayReportDTO,
    horizon: EvidenceHorizonView,
    frames_per_second: int,
) -> tuple[BuildOrderStepView, ...]:
    if horizon.frame_end is None:
        return ()
    player = _selected_player(report)
    player_label = "Replay" if player is None else player.display_name
    output: dict[tuple[int, str, str], BuildOrderStepView] = {}
    for claim in _available_claims(report, "opening_build_order"):
        if claim.label != "build.completed_sequence" or not claim.claim_id.startswith(
            "feature:build.completed_sequence:"
        ):
            continue
        raw = _thaw(claim.raw_value)
        if not isinstance(raw, list):
            continue
        for step in raw:
            if (
                not isinstance(step, dict)
                or type(step.get("frame")) is not int
                or type(step.get("template_name")) is not str
            ):
                continue
            frame = cast(int, step["frame"])
            if frame < 0 or frame > horizon.frame_end:
                continue
            view = BuildOrderStepView(
                    frame=frame,
                    time_label=format_frame(frame, frames_per_second=frames_per_second),
                    player_label=player_label,
                    structure_label=game_label(cast(str, step["template_name"])),
                    evidence=_claim_evidence(claim),
                )
            key = (view.frame, view.player_label, view.structure_label)
            prior = output.get(key)
            output[key] = view if prior is None else prior.model_copy(
                update={"evidence": _merge_evidence(prior.evidence, view.evidence)}
            )
    return tuple(sorted(output.values(), key=lambda item: (item.frame, item.player_label, item.structure_label))[:24])


# TheSuperHackers @bugfix Leex 25/08/2026 Keep dense event summaries useful without rendering the full match log. (#TBD)
def _event_summary(events: list[str], empty_label: str) -> str:
    # TheSuperHackers @bugfix Leex 25/08/2026 Keep repeated feature rows from duplicating one player-facing event summary. (#TBD)
    events = list(dict.fromkeys(events))
    if not events:
        return empty_label
    if len(events) <= 5:
        return ", ".join(events)
    hidden_count = len(events) - 5
    return f"{', '.join(events[:3])}; {hidden_count} additional observed events; latest: {', '.join(events[-2:])}"


def _metric_value(claim: ReportClaimDTO, frames_per_second: int) -> str:
    raw = _thaw(claim.raw_value)
    if claim.label == "economy.supply_collection_rate" and type(raw) in (int, float):
        return f"{float(cast(int | float, raw)):,.0f} supplies/min"
    if claim.label == "economy.supply_collected_total" and type(raw) in (int, float):
        return f"{float(cast(int | float, raw)):,.0f} supplies"
    if claim.label == "combat.observed_damage_trade_ratio" and type(raw) in (int, float):
        return f"{float(cast(int | float, raw)):.2f} : 1"
    if claim.label == "activity.effective_actions_per_minute" and type(raw) in (int, float):
        return f"{float(cast(int | float, raw)):.1f} observed actions/min"
    if claim.label == "production.completed_composition" and isinstance(raw, dict):
        composition = [
            f"{count} {game_label(identity)}"
            for identity, count in sorted(raw.items())
            if type(identity) is str and type(count) is int and count > 0
        ]
        if composition:
            return ", ".join(composition)
    if claim.label in ("production.science_purchase_timing", "production.special_power_timing") and isinstance(raw, list):
        return _event_summary(
            [
                f"{game_label(item['item_name'])} at "
                f"{format_frame(item['frame'], frames_per_second=frames_per_second)}"
                for item in raw
                if isinstance(item, dict)
                and type(item.get("frame")) is int
                and type(item.get("item_name")) is str
            ],
            "No observed timing events",
        )
    if claim.label == "combat.observed_kill_timing" and isinstance(raw, list):
        return _event_summary(
            [
                f"{game_label(item['victim_template_name'])} at "
                f"{format_frame(item['frame'], frames_per_second=frames_per_second)}"
                for item in raw
                if isinstance(item, dict)
                and type(item.get("frame")) is int
                and type(item.get("victim_template_name")) is str
            ],
            "No observed kills",
        )
    if claim.label == "combat.turning_point_timing" and isinstance(raw, list):
        return _event_summary(
            [
                f"{game_label(item['attacker_template_name'])} over {game_label(item['victim_template_name'])} at "
                f"{format_frame(item['frame'], frames_per_second=frames_per_second)}"
                for item in raw
                if (
                    isinstance(item, dict)
                    and type(item.get("frame")) is int
                    and type(item.get("attacker_template_name")) is str
                    and type(item.get("victim_template_name")) is str
                )
            ],
            "No evidence-backed engagement swing candidates",
        )
    if claim.label == "scouting.first_observed_clear_timing" and isinstance(raw, list):
        return _event_summary(
            [
                f"{game_label(item['template_name'])} at "
                f"{format_frame(item['frame'], frames_per_second=frames_per_second)}"
                for item in raw
                if isinstance(item, dict)
                and type(item.get("frame")) is int
                and type(item.get("template_name")) is str
            ],
            "No observed scouting clears",
        )
    return claim.display_value or "Unavailable"


def _highlights(
    report: ReplayReportDTO,
    horizon: EvidenceHorizonView,
    frames_per_second: int,
) -> tuple[CoachingHighlightView, ...]:
    claims = {
        claim.label: claim
        for section in report.sections
        for claim in section.claims
        if claim.availability in ("available", "partial")
        and claim.claim_id.startswith(f"feature:{claim.label}:")
        and claim.evidence
        and _claim_inside_horizon(claim, horizon)
    }
    return tuple(
        CoachingHighlightView(
            signal_id=name,
            layout="narrative" if name in _NARRATIVE_HIGHLIGHTS else "metric",
            title=feature_label(name),
            value=_metric_value(claims[name], frames_per_second),
            explanation=_HIGHLIGHT_EXPLANATIONS[name],
            evidence=_claim_evidence(claims[name]),
        )
        for name in _HIGHLIGHT_ORDER
        if name in claims
    )[:5]


def _prompts(strategies: tuple[PlayerStrategyView, ...]) -> tuple[ReviewPromptView, ...]:
    return tuple(
        ReviewPromptView(strategy_id=item.strategy_id, text=_ADVICE[item.strategy_id], evidence=item.evidence)
        for item in strategies
        if item.strategy_id in _ADVICE and item.evidence
    )[:5]


# TheSuperHackers @feature Leex 24/08/2026 Explain measured economy signals without inferring spend, army value, or causality. (#TBD)
def _signal_reads(highlights: tuple[CoachingHighlightView, ...]) -> tuple[SignalReadView, ...]:
    supply = next((item for item in highlights if item.signal_id == "economy.supply_collection_rate"), None)
    if supply is None:
        return ()
    return (
        SignalReadView(
            title="Economy signal",
            statement=(
                f"Observed collection reached {supply.value} inside the verified horizon. "
                "This measures income recorded by the engine; it does not establish spend, army value, or strategic cause."
            ),
            evidence=supply.evidence,
        ),
    )


def _event_rows(report: ReplayReportDTO, label: str) -> tuple[tuple[dict[str, object], ReportClaimDTO], ...]:
    rows: list[tuple[dict[str, object], ReportClaimDTO]] = []
    for section in report.sections:
        for claim in section.claims:
            if (
                claim.label != label
                or claim.availability not in ("available", "partial")
                or not claim.claim_id.startswith(f"feature:{label}:")
                or not claim.evidence
            ):
                continue
            raw = _thaw(claim.raw_value)
            if isinstance(raw, list):
                rows.extend((cast(dict[str, object], row), claim) for row in raw if isinstance(row, dict))
    return tuple(rows)


# TheSuperHackers @feature Leex 25/08/2026 Bind each tactical checkpoint to a fifteen-second battlefield review window. (#TBD)
def _moment_map_window(
    frame: int,
    horizon: EvidenceHorizonView,
    frames_per_second: int,
) -> tuple[int, int]:
    assert horizon.frame_end is not None
    radius = 15 * frames_per_second
    return max(0, frame - radius), min(horizon.frame_end, frame + radius)


# TheSuperHackers @feature Leex 24/08/2026 Turn observed tactical events into a bounded chronological review queue. (#TBD)
def _key_moments(
    report: ReplayReportDTO,
    horizon: EvidenceHorizonView,
    frames_per_second: int,
) -> tuple[KeyMomentView, ...]:
    if horizon.frame_end is None:
        return ()
    moments: list[KeyMomentView] = []
    swing_keys: set[tuple[int, str]] = set()

    for row, claim in _event_rows(report, "scouting.first_observed_clear_timing"):
        frame, template_name = row.get("frame"), row.get("template_name")
        if type(frame) is int and 0 <= frame <= horizon.frame_end and type(template_name) is str:
            map_frame_start, map_frame_end = _moment_map_window(frame, horizon, frames_per_second)
            moments.append(
                KeyMomentView(
                    frame=frame,
                    map_frame_start=map_frame_start,
                    map_frame_end=map_frame_end,
                    time_label=f"Frame {frame:,}",
                    category_label="Scouting",
                    title=f"{game_label(template_name)} first observed",
                    review_prompt="Review what changed after this information became visible.",
                    evidence=_claim_evidence(claim),
                )
            )

    for row, claim in _event_rows(report, "production.special_power_timing"):
        frame, item_name = row.get("frame"), row.get("item_name")
        if type(frame) is int and 0 <= frame <= horizon.frame_end and type(item_name) is str:
            map_frame_start, map_frame_end = _moment_map_window(frame, horizon, frames_per_second)
            moments.append(
                KeyMomentView(
                    frame=frame,
                    map_frame_start=map_frame_start,
                    map_frame_end=map_frame_end,
                    time_label=f"Frame {frame:,}",
                    category_label="Power use",
                    title=f"{game_label(item_name)} used",
                    review_prompt="Review whether this timing created useful information, pressure, or protection.",
                    evidence=_claim_evidence(claim),
                )
            )

    for row, claim in _event_rows(report, "combat.turning_point_timing"):
        frame = row.get("frame")
        attacker = row.get("attacker_template_name")
        victim = row.get("victim_template_name")
        if (
            type(frame) is int
            and 0 <= frame <= horizon.frame_end
            and type(attacker) is str
            and type(victim) is str
        ):
            swing_keys.add((frame, victim))
            map_frame_start, map_frame_end = _moment_map_window(frame, horizon, frames_per_second)
            moments.append(
                KeyMomentView(
                    frame=frame,
                    map_frame_start=map_frame_start,
                    map_frame_end=map_frame_end,
                    time_label=f"Frame {frame:,}",
                    category_label="Engagement",
                    title=f"{game_label(attacker)} over {game_label(victim)}",
                    review_prompt=(
                        "Review the positioning, trade, and follow-up around this evidence-backed swing candidate."
                    ),
                    evidence=_claim_evidence(claim),
                )
            )

    for row, claim in _event_rows(report, "combat.observed_kill_timing"):
        frame, victim = row.get("frame"), row.get("victim_template_name")
        if (
            type(frame) is int
            and 0 <= frame <= horizon.frame_end
            and type(victim) is str
            and (frame, victim) not in swing_keys
        ):
            map_frame_start, map_frame_end = _moment_map_window(frame, horizon, frames_per_second)
            moments.append(
                KeyMomentView(
                    frame=frame,
                    map_frame_start=map_frame_start,
                    map_frame_end=map_frame_end,
                    time_label=f"Frame {frame:,}",
                    category_label="Combat",
                    title=f"{game_label(victim)} destroyed",
                    review_prompt="Review the trade, positioning, and immediate follow-up around this observed kill.",
                    evidence=_claim_evidence(claim),
                )
            )

    # TheSuperHackers @bugfix Leex 25/08/2026 Merge repeated projections of the same tactical fact while retaining all citations. (#TBD)
    unique: dict[tuple[int, str, str], KeyMomentView] = {}
    for moment in moments:
        key = (moment.frame, moment.category_label, moment.title)
        prior = unique.get(key)
        unique[key] = moment if prior is None else prior.model_copy(
            update={"evidence": _merge_evidence(prior.evidence, moment.evidence)}
        )
    return tuple(
        sorted(unique.values(), key=lambda item: (item.frame, item.category_label, item.title))[:8]
    )


def _opening_lanes(
    build_order: tuple[BuildOrderStepView, ...],
    key_moments: tuple[KeyMomentView, ...],
    frames_per_second: int,
) -> tuple[OpeningLaneView, ...]:
    events: dict[str, list[OpeningLaneEventView]] = {
        "build": [],
        "scouting": [],
        "combat": [],
        "powers": [],
    }
    events["build"].extend(
        OpeningLaneEventView(
            frame=step.frame,
            time_label=format_frame(step.frame, frames_per_second=frames_per_second),
            title=f"{step.structure_label} completed",
            evidence=step.evidence[:1],
        )
        for step in build_order
    )
    lane_by_category = {
        "Scouting": "scouting",
        "Combat": "combat",
        "Engagement": "combat",
        "Power use": "powers",
    }
    for moment in key_moments:
        lane_id = lane_by_category.get(moment.category_label)
        if lane_id is None:
            continue
        events[lane_id].append(
            OpeningLaneEventView(
                frame=moment.frame,
                time_label=format_frame(moment.frame, frames_per_second=frames_per_second),
                title=moment.title,
                evidence=moment.evidence[:1],
            )
        )
    lane_config = (
        ("build", "Build order", "Verified structure completions", "No verified build completion inside the verified horizon."),
        ("scouting", "Scouting", "First clear sightings of opponent assets", "No verified scouting clear inside the verified horizon."),
        ("combat", "Army and attacks", "Observed kills and engagement swing candidates", "No verified combat event inside the verified horizon."),
        ("powers", "Powers", "Observed special-power activations", "No verified power use inside the verified horizon."),
    )
    return tuple(
        OpeningLaneView(
            lane_id=cast(Literal["build", "scouting", "combat", "powers"], lane_id),
            title=title,
            description=description,
            empty_message=empty_message,
            events=tuple(sorted(events[lane_id], key=lambda item: (item.frame, item.title))[:12]),
        )
        for lane_id, title, description, empty_message in lane_config
    )


def _summary(
    report: ReplayReportDTO,
    horizon: EvidenceHorizonView,
    strategies: tuple[PlayerStrategyView, ...],
    build_order: tuple[BuildOrderStepView, ...],
    highlights: tuple[CoachingHighlightView, ...],
    frames_per_second: int,
) -> str:
    player = _selected_player(report)
    player_label = "The selected player" if player is None else player.display_name
    if horizon.status == "partial":
        clock = (
            "an unknown time"
            if horizon.frame_end is None
            else format_frame(horizon.frame_end, frames_per_second=frames_per_second).split(" (", 1)[0]
        )
        return (
            f"Observed opening through {clock}: {player_label} completed {len(build_order)} verified structures. "
            "The trace ends before a full-match result can be established."
        )
    if strategies:
        primary = strategies[0]
        summary = (
            f"{player_label} showed {primary.title} during the {primary.phase_label.casefold()}. "
            f"The report records {len(build_order)} completed structures and {len(highlights)} reviewable metrics."
        )
    else:
        summary = (
            f"A named strategy was not established for {player_label}. "
            f"The report still records {len(build_order)} completed structures and {len(highlights)} reviewable metrics."
        )
    if player is not None and player.result is not None:
        summary = f"{summary} Recorded result: {player.result}."
    return summary


def _limitations(report: ReplayReportDTO, horizon: EvidenceHorizonView) -> tuple[str, ...]:
    values = [issue.message for issue in report.terminal_quality.issues]
    values.extend(report.warnings)
    if horizon.status == "partial" and not values:
        values.append("Only the observed opening is available; later match phases are unknown.")
    return tuple(dict.fromkeys(values))


def _local_model_summary(report: ReplayReportDTO) -> str | None:
    if report.ollama.status != "succeeded" or report.ollama.validated_prose is None:
        return None
    prose = _thaw(report.ollama.validated_prose)
    summary = prose.get("summary") if isinstance(prose, dict) else None
    return summary if type(summary) is str and summary else None


# TheSuperHackers @feature Leex 23/08/2026 Convert immutable evidence into bounded coaching without inventing skill judgments. (#TBD)
def coaching_view(report: ReplayReportDTO, timeline: TimelineChartDTO) -> CoachingViewModel:
    """Build the player-first layer after validating fixed report and timeline identity."""

    if (
        timeline.query.replay_public_id != report.fixed_report.replay_public_id
        or timeline.query.report_public_id != report.fixed_report.report_public_id
    ):
        raise ValueError("timeline identity does not match the fixed report")
    frames_per_second = timeline.timebase_fps or 30
    horizon = _horizon(report, timeline)
    strategies = _strategies(report, horizon)
    build_order = _build_order(report, horizon, frames_per_second)
    highlights = _highlights(report, horizon, frames_per_second)
    key_moments = _key_moments(report, horizon, frames_per_second)
    return CoachingViewModel(
        horizon=horizon,
        summary=_summary(report, horizon, strategies, build_order, highlights, frames_per_second),
        strategies=strategies,
        build_order=build_order,
        highlights=highlights,
        signal_reads=_signal_reads(highlights),
        prompts=_prompts(strategies),
        key_moments=key_moments,
        opening_lanes=_opening_lanes(build_order, key_moments, frames_per_second),
        limitations=_limitations(report, horizon),
        local_model_summary=_local_model_summary(report),
    )
