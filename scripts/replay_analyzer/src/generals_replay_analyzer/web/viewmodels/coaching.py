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

_PHASE_RANK = MappingProxyType({"opening": 0, "early": 1, "mid": 2, "late": 3, "cross_phase": 4})
_HIGHLIGHT_ORDER = (
    "economy.supply_collection_rate",
    "economy.supply_collected_total",
    "production.completed_composition",
    "combat.observed_damage_trade_ratio",
    "activity.effective_actions_per_minute",
)
_HIGHLIGHT_EXPLANATIONS = MappingProxyType(
    {
        "economy.supply_collection_rate": "Measured from observed supply collection events in this report.",
        "economy.supply_collected_total": "Total supplies recorded inside the available evidence horizon.",
        "production.completed_composition": "Completed units and upgrades observed in the available trace.",
        "combat.observed_damage_trade_ratio": "Observed applied damage dealt divided by observed damage taken.",
        "activity.effective_actions_per_minute": "Supported replay orders per observed minute, not raw click APM.",
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
    title: str
    value: str
    explanation: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class ReviewPromptView(_FrozenView):
    strategy_id: str
    text: str
    evidence: tuple[ReportEvidenceReferenceDTO, ...]


class CoachingViewModel(_FrozenView):
    schema_version: Literal["coaching-view-v1"] = "coaching-view-v1"
    horizon: EvidenceHorizonView
    summary: str
    strategies: tuple[PlayerStrategyView, ...]
    build_order: tuple[BuildOrderStepView, ...]
    highlights: tuple[CoachingHighlightView, ...]
    prompts: tuple[ReviewPromptView, ...]
    limitations: tuple[str, ...]
    local_model_summary: str | None


def _thaw(value: object) -> object:
    return thaw_report_value(cast(CanonicalValue, value))


def _selected_player(report: ReplayReportDTO) -> ReplayPlayerDisplayDTO | None:
    player_id = report.version.replay_player_public_id
    return next((player for player in report.players if player.replay_player_public_id == player_id), None)


def _claim_evidence(claim: ReportClaimDTO) -> tuple[ReportEvidenceReferenceDTO, ...]:
    return tuple(sorted(claim.evidence, key=lambda item: (item.tier, item.public_id)))


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
    complete = (
        report.availability.state == "available"
        and report.lifecycle.lifecycle_state == "engine_verified"
        and report.lifecycle.parser_completion_status == "complete"
        and report.lifecycle.telemetry_status in ("complete", "succeeded")
        and report.lifecycle.telemetry_runner_status in ("success", "succeeded")
        and not report.terminal_quality.issues
        and duration is not None
    )
    if complete:
        assert duration is not None
        return EvidenceHorizonView(
            status="complete",
            title="Complete match evidence",
            frame_end=duration,
            description=f"The report covers the complete recorded match through {format_frame(duration)}.",
        )
    frame_end = _evidence_end(report, timeline)
    if frame_end is None:
        return EvidenceHorizonView(
            status="unavailable",
            title="Evidence horizon unavailable",
            frame_end=None,
            description="The report does not contain a verified frame horizon for player conclusions.",
        )
    clock = format_frame(frame_end).split(" (", 1)[0]
    return EvidenceHorizonView(
        status="partial",
        title=f"Observed opening through {clock}",
        frame_end=frame_end,
        description="Conclusions are limited to this observed opening and are not full-match claims.",
    )


def _strategies(report: ReplayReportDTO) -> tuple[PlayerStrategyView, ...]:
    player = _selected_player(report)
    player_label = "Replay" if player is None else player.display_name
    faction = None if player is None or player.faction is None else game_label(player.faction)
    output: list[PlayerStrategyView] = []
    for claim in _available_claims(report, "strategy_phases"):
        raw = _thaw(claim.raw_value)
        if (
            not claim.claim_id.startswith(f"strategy:{claim.label}:")
            or not isinstance(raw, dict)
            or raw.get("strategy_label") != claim.label
            or raw.get("phase") not in _PHASE_RANK
            or not any(item.tier == "derived" for item in claim.evidence)
        ):
            continue
        score = raw.get("confidence")
        evidence_score = float(cast(int | float, score)) if type(score) in (int, float) else None
        phase = cast(str, raw["phase"])
        output.append(
            PlayerStrategyView(
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
        )
    slot = 0 if player is None else player.slot
    return tuple(
        sorted(
            output,
            key=lambda item: (slot, _PHASE_RANK[item.phase], -(item.evidence_score or 0.0), item.strategy_id),
        )
    )


def _build_order(report: ReplayReportDTO) -> tuple[BuildOrderStepView, ...]:
    player = _selected_player(report)
    player_label = "Replay" if player is None else player.display_name
    output: list[BuildOrderStepView] = []
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
            if frame < 0:
                continue
            output.append(
                BuildOrderStepView(
                    frame=frame,
                    time_label=format_frame(frame),
                    player_label=player_label,
                    structure_label=game_label(cast(str, step["template_name"])),
                    evidence=_claim_evidence(claim),
                )
            )
    return tuple(sorted(output, key=lambda item: (item.frame, item.player_label, item.structure_label))[:24])


def _metric_value(claim: ReportClaimDTO) -> str:
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
    return claim.display_value or "Unavailable"


def _highlights(report: ReplayReportDTO) -> tuple[CoachingHighlightView, ...]:
    claims = {
        claim.label: claim
        for section in report.sections
        for claim in section.claims
        if claim.availability in ("available", "partial")
        and claim.claim_id.startswith(f"feature:{claim.label}:")
        and claim.evidence
    }
    return tuple(
        CoachingHighlightView(
            title=feature_label(name),
            value=_metric_value(claims[name]),
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


def _summary(
    report: ReplayReportDTO,
    horizon: EvidenceHorizonView,
    strategies: tuple[PlayerStrategyView, ...],
    build_order: tuple[BuildOrderStepView, ...],
    highlights: tuple[CoachingHighlightView, ...],
) -> str:
    player = _selected_player(report)
    player_label = "The selected player" if player is None else player.display_name
    if horizon.status == "partial":
        clock = "an unknown time" if horizon.frame_end is None else format_frame(horizon.frame_end).split(" (", 1)[0]
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
    horizon = _horizon(report, timeline)
    strategies = _strategies(report)
    build_order = _build_order(report)
    highlights = _highlights(report)
    return CoachingViewModel(
        horizon=horizon,
        summary=_summary(report, horizon, strategies, build_order, highlights),
        strategies=strategies,
        build_order=build_order,
        highlights=highlights,
        prompts=_prompts(strategies),
        limitations=_limitations(report, horizon),
        local_model_summary=_local_model_summary(report),
    )
