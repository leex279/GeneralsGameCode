"""Deterministic, evidence-backed coaching projection tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    FixedReportQueryDTO,
    OllamaReportStatusDTO,
    QualityIssueDTO,
    ReplayPlayerDisplayDTO,
    ReplayReportDTO,
    ReportClaimDTO,
    ReportEvidenceReferenceDTO,
    ReportLifecycleDTO,
    ReportSectionDTO,
    ReportVersionDTO,
    TerminalQualityDTO,
    TimelineChartDTO,
    TimelineChartQueryDTO,
    TimelineFamilyOptionDTO,
    TimelinePointDTO,
    TimelineSeriesDTO,
)
from generals_replay_analyzer.web.viewmodels.coaching import coaching_view

REPLAY = "123e4567-e89b-42d3-a456-426614170001"
REPORT = "123e4567-e89b-42d3-a456-426614170002"
PLAYER = "123e4567-e89b-42d3-a456-426614170003"
OPPONENT = "123e4567-e89b-42d3-a456-426614170004"
OPPONENT_REPORT = "123e4567-e89b-42d3-a456-426614170005"
STRATEGY_EVIDENCE = "123e4567-e89b-42d3-a456-426614170006"
BUILD_EVIDENCE = "123e4567-e89b-42d3-a456-426614170007"
METRIC_EVIDENCE = "123e4567-e89b-42d3-a456-426614170008"
POWER_EVIDENCE = "123e4567-e89b-42d3-a456-426614170009"
TURNING_EVIDENCE = "123e4567-e89b-42d3-a456-426614170010"
SCOUTING_EVIDENCE = "123e4567-e89b-42d3-a456-426614170011"
SECTION_KEYS = (
    "overview",
    "players_results",
    "opening_build_order",
    "economy",
    "production_composition",
    "combat_engagements",
    "activity",
    "strategy_phases",
    "spatial_analysis",
    "longitudinal_context",
    "llm_interpretation",
)


def _claim(
    *,
    claim_id: str,
    section: str,
    label: str,
    raw_value: object,
    display_value: str,
    evidence_id: str,
    frame_end: int,
    availability: str = "available",
) -> ReportClaimDTO:
    return ReportClaimDTO(
        claim_id=claim_id,
        section=section,  # type: ignore[arg-type]
        label=label,
        raw_value=raw_value,
        display_value=display_value,
        unit="json" if isinstance(raw_value, (dict, list)) else "credits_per_minute",
        availability=availability,  # type: ignore[arg-type]
        unavailable_reason=None if availability == "available" else "trace_incomplete",
        scope={"scope_type": "player", "public_id": PLAYER},
        frame_window=(0, frame_end),
        confidence=None,
        evidence=(ReportEvidenceReferenceDTO(public_id=evidence_id, tier="derived"),),
        details={"definition_version": "fixture-v1"},
    )


def _report(*, partial: bool = False) -> ReplayReportDTO:
    end = 105 if partial else 3_600
    build = _claim(
        claim_id="feature:build.completed_sequence:fixture",
        section="opening_build_order",
        label="build.completed_sequence",
        raw_value=[
            {"frame": 45, "template_name": "AmericaPowerPlant"},
            {"frame": 90, "template_name": "AmericaBarracks"},
            *([{"frame": 120, "template_name": "AmericaWarFactory"}] if partial else []),
        ],
        display_value="2 completed structures",
        evidence_id=BUILD_EVIDENCE,
        frame_end=end,
        availability="partial" if partial else "available",
    )
    strategy = _claim(
        claim_id="strategy:usa_humvee_pressure:fixture",
        section="strategy_phases",
        label="usa_humvee_pressure",
        raw_value={"confidence": 1.0, "phase": "early", "strategy_label": "usa_humvee_pressure"},
        display_value="Humvee pressure",
        evidence_id=STRATEGY_EVIDENCE,
        frame_end=end,
    )
    supply = _claim(
        claim_id="feature:economy.supply_collection_rate:fixture",
        section="economy",
        label="economy.supply_collection_rate",
        raw_value=1_350.0,
        display_value="1350",
        evidence_id=METRIC_EVIDENCE,
        frame_end=end,
        availability="partial" if partial else "available",
    )
    power = _claim(
        claim_id="feature:production.special_power_timing:fixture",
        section="production_composition",
        label="production.special_power_timing",
        raw_value=[{"frame": 240, "item_name": "SuperweaponSpySatellite"}],
        display_value='[{"frame":240,"item_name":"SuperweaponSpySatellite"}]',
        evidence_id=POWER_EVIDENCE,
        frame_end=end,
    )
    turning = _claim(
        claim_id="feature:combat.turning_point_timing:fixture",
        section="combat_engagements",
        label="combat.turning_point_timing",
        raw_value=[{"frame": 300, "attacker_template_name": "AmericaVehicleHumvee", "victim_template_name": "ChinaWarFactory", "criterion": "engagement-swing-v1"}],
        display_value='[{"frame":300,"victim_template_name":"ChinaWarFactory"}]',
        evidence_id=TURNING_EVIDENCE,
        frame_end=end,
    )
    observed_kills = _claim(
        claim_id="feature:combat.observed_kill_timing:fixture",
        section="combat_engagements",
        label="combat.observed_kill_timing",
        raw_value=[{"frame": 300, "victim_template_name": "ChinaWarFactory"}],
        display_value='[{"frame":300,"victim_template_name":"ChinaWarFactory"}]',
        evidence_id=TURNING_EVIDENCE,
        frame_end=end,
    )
    scouting = _claim(
        claim_id="feature:scouting.first_observed_clear_timing:fixture",
        section="activity",
        label="scouting.first_observed_clear_timing",
        raw_value=[{"frame": 150, "object_id": 42, "template_name": "ChinaWarFactory"}],
        display_value='[{"frame":150,"object_id":42,"template_name":"ChinaWarFactory"}]',
        evidence_id=SCOUTING_EVIDENCE,
        frame_end=end,
    )
    claims_by_section = {
        "opening_build_order": (build,),
        "economy": (supply,),
        "production_composition": (power,),
        "combat_engagements": (turning, observed_kills),
        "activity": (scouting,),
        "strategy_phases": () if partial else (strategy,),
    }
    sections = tuple(
        ReportSectionDTO(
            key=key,  # type: ignore[arg-type]
            title=key.replace("_", " ").title(),
            availability=AvailabilityDTO(
                state="partial" if partial and key in claims_by_section else "available"
            )
            if claims_by_section.get(key)
            else AvailabilityDTO(state="unavailable", reason_codes=("section_not_available",)),
            claims=claims_by_section.get(key, ()),
        )
        for key in SECTION_KEYS
    )
    return ReplayReportDTO(
        schema_version="web-replay-report-v1",
        generated_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        fixed_report=FixedReportQueryDTO(replay_public_id=REPLAY, report_public_id=REPORT),
        version=ReportVersionDTO(
            report_public_id=REPORT,
            report_version="replay-report-v1",
            replay_player_public_id=PLAYER,
        ),
        availability=AvailabilityDTO(state="partial" if partial else "available"),
        replay_label="Tournament Desert replay",
        replay_sha256="a" * 64,
        players=(
            ReplayPlayerDisplayDTO(
                replay_player_public_id=PLAYER,
                report_public_id=REPORT,
                display_name="Leex279",
                slot=1,
                faction="FactionAmerica",
                result=None if partial else "won",
            ),
            ReplayPlayerDisplayDTO(
                replay_player_public_id=OPPONENT,
                report_public_id=OPPONENT_REPORT,
                display_name="Opponent",
                slot=2,
                faction="FactionChina",
                result=None if partial else "lost",
            ),
        ),
        result=None if partial else "won",
        map_name="Tournament Desert",
        patch="1.04",
        duration_frames=108 if partial else 3_600,
        source_mode="deterministic_only",
        lifecycle=ReportLifecycleDTO(
            lifecycle_state="desynced" if partial else "engine_verified",
            parser_completion_status="complete",
            telemetry_status="failed" if partial else "succeeded",
            telemetry_runner_status="desynced" if partial else "success",
        ),
        terminal_quality=TerminalQualityDTO(
            lifecycle="desynced" if partial else "engine_verified",
            issues=(
                QualityIssueDTO(code="crc_mismatch", message="CRC mismatch at frame 105", frame_end=105),
            )
            if partial
            else (),
            engine_run_status="desynced" if partial else "succeeded",
            strategy_analysis_scope="player",
        ),
        sections=sections,
        ollama=OllamaReportStatusDTO(requested=False, status="not_requested"),
    )


def _timeline(*, late_parser_frame: int | None = None) -> TimelineChartDTO:
    series = (
        TimelineSeriesDTO(
            series_id="parser-commands",
            label="Parser commands",
            kind="marker",
            player_public_id=None,
            event_family="activity",
            unit=None,
            availability=AvailabilityDTO(state="available", evidence_references=(METRIC_EVIDENCE,)),
            points=(
                TimelinePointDTO(
                    frame=late_parser_frame,
                    value=None,
                    label="Parser command",
                    evidence=(ReportEvidenceReferenceDTO(public_id=METRIC_EVIDENCE, tier="observed"),),
                ),
            ),
            intervals=(),
        ),
    ) if late_parser_frame is not None else ()
    return TimelineChartDTO(
        schema_version="web-report-timeline-v1",
        query=TimelineChartQueryDTO(replay_public_id=REPLAY, report_public_id=REPORT),
        availability=AvailabilityDTO(state="available") if series else AvailabilityDTO(
            state="unavailable", reason_codes=("fixture",)
        ),
        timebase_fps=30,
        available_players=(),
        available_families=(TimelineFamilyOptionDTO(value="activity", label="Activity"),) if series else (),
        series=series,
    )


def test_complete_report_projects_strategy_build_order_metrics_and_review_prompt() -> None:
    coaching = coaching_view(_report(), _timeline())

    assert coaching.horizon.status == "complete"
    assert coaching.horizon.title == "Complete match evidence"
    assert "Leex279 showed Humvee pressure during the early game" in coaching.summary
    assert [(item.strategy_id, item.title, item.player_label) for item in coaching.strategies] == [
        ("usa_humvee_pressure", "Humvee pressure", "Leex279")
    ]
    assert [(item.time_label, item.structure_label) for item in coaching.build_order] == [
        ("0:01.5 (frame 45)", "Power Plant"),
        ("0:03.0 (frame 90)", "Barracks"),
    ]
    assert coaching.highlights[0].title == "Supply income"
    assert coaching.highlights[0].evidence[0].public_id == METRIC_EVIDENCE
    assert any(
        item.title == "Special power timing" and item.evidence[0].public_id == POWER_EVIDENCE
        for item in coaching.highlights
    )
    assert any(item.title == "Observed kills" for item in coaching.highlights)
    swing = next(item for item in coaching.highlights if item.title == "Evidence-backed engagement swing candidates")
    assert swing.value == "Humvee over War Factory at 0:10.0 (frame 300)"
    assert any(item.title == "First scouting clears" for item in coaching.highlights)
    assert 1 <= len(coaching.prompts) <= 5
    assert coaching.prompts[0].strategy_id == "usa_humvee_pressure"
    assert coaching.prompts[0].evidence[0].public_id == STRATEGY_EVIDENCE
    assert coaching.highlights[0].signal_id == "economy.supply_collection_rate"
    assert coaching.signal_reads[0].title == "Economy signal"
    assert "does not establish spend" in coaching.signal_reads[0].statement
    assert coaching.signal_reads[0].evidence[0].public_id == METRIC_EVIDENCE


def test_complete_engine_verified_report_uses_authoritative_timebase_despite_projection_warning() -> None:
    report = _report().model_copy(
        update={
            "availability": AvailabilityDTO(state="partial", reason_codes=("projection_unavailable",)),
            "terminal_quality": TerminalQualityDTO(
                lifecycle="engine_verified",
                issues=(
                    QualityIssueDTO(
                        code="projection_unavailable",
                        message="Some optional projections are unavailable",
                    ),
                ),
                engine_run_status="success",
                strategy_analysis_scope="player",
            ),
        }
    )
    timeline = _timeline().model_copy(update={"timebase_fps": 60})

    coaching = coaching_view(report, timeline)

    assert coaching.horizon.status == "complete"
    assert coaching.horizon.description.endswith("through 1:00.0 (frame 3600).")
    assert [item.time_label for item in coaching.build_order] == [
        "0:00.7 (frame 45)",
        "0:01.5 (frame 90)",
    ]
    assert next(item for item in coaching.highlights if item.title == "Observed kills").value.endswith(
        "at 0:05.0 (frame 300)"
    )
    assert coaching.limitations == ("Some optional projections are unavailable",)


def test_key_moments_turn_event_evidence_into_a_chronological_review_queue() -> None:
    """Catch tactical events being buried as unordered metric text or duplicate kill records."""
    coaching = coaching_view(_report(), _timeline())

    assert [
        (item.frame, item.category_label, item.title, item.time_label)
        for item in coaching.key_moments
    ] == [
        (150, "Scouting", "War Factory first observed", "Frame 150"),
        (
            240,
            "Power use",
            "Superweapon Spy Satellite (unrecognized) used",
            "Frame 240",
        ),
        (300, "Engagement", "Humvee over War Factory", "Frame 300"),
    ]
    assert coaching.key_moments[0].review_prompt == (
        "Review what changed after this information became visible."
    )
    assert coaching.key_moments[0].evidence[0].public_id == SCOUTING_EVIDENCE
    assert coaching.key_moments[-1].review_prompt == (
        "Review the positioning, trade, and follow-up around this evidence-backed swing candidate."
    )
    assert coaching.key_moments[-1].evidence[0].public_id == TURNING_EVIDENCE


def test_opening_lanes_align_build_scouting_combat_and_power_evidence() -> None:
    """Catch useful opening intelligence collapsing back into an unordered metric list."""
    coaching = coaching_view(_report(), _timeline())

    assert [lane.lane_id for lane in coaching.opening_lanes] == [
        "build",
        "scouting",
        "combat",
        "powers",
    ]
    assert [event.title for event in coaching.opening_lanes[0].events] == [
        "Power Plant completed",
        "Barracks completed",
    ]
    assert coaching.opening_lanes[1].events[0].title == "War Factory first observed"
    assert coaching.opening_lanes[2].events[0].title == "Humvee over War Factory"
    assert coaching.opening_lanes[3].events[0].title == (
        "Superweapon Spy Satellite (unrecognized) used"
    )
    assert coaching.opening_lanes[1].events[0].time_label == "0:05.0 (frame 150)"
    assert coaching.opening_lanes[1].events[0].evidence[0].public_id == SCOUTING_EVIDENCE


def test_opening_lanes_preserve_empty_categories_and_respect_partial_horizon() -> None:
    """Catch absent telemetry being hidden or events leaking past a desync boundary."""
    coaching = coaching_view(_report(partial=True), _timeline(late_parser_frame=56_003))

    assert [event.frame for event in coaching.opening_lanes[0].events] == [45, 90]
    assert all(event.frame <= 105 for lane in coaching.opening_lanes for event in lane.events)
    assert all(not lane.events for lane in coaching.opening_lanes[1:])
    assert all(lane.empty_message.endswith("inside the verified horizon.") for lane in coaching.opening_lanes[1:])


def test_partial_desync_report_never_invents_result_or_future_phases() -> None:
    coaching = coaching_view(_report(partial=True), _timeline(late_parser_frame=56_003))

    assert coaching.horizon.status == "partial"
    assert coaching.horizon.title == "Observed opening through 0:03.5"
    assert coaching.horizon.frame_end == 105
    combined = " ".join(
        (
            coaching.summary,
            *(item.title for item in coaching.strategies),
            *(item.text for item in coaching.prompts),
        )
    ).casefold()
    assert all(token not in combined for token in ("winner", "won", "lost", "victory", "mid game", "late game"))
    assert coaching.limitations == ("CRC mismatch at frame 105",)
    assert coaching.local_model_summary is None
    assert coaching.key_moments == ()


def test_partial_report_suppresses_metrics_extending_past_the_terminal_horizon() -> None:
    report = _report(partial=True)
    sections = tuple(
        section.model_copy(
            update={
                "claims": tuple(
                    claim.model_copy(update={"frame_window": (0, 120)})
                    if claim.label == "economy.supply_collection_rate"
                    else claim
                    for claim in section.claims
                )
            }
        )
        for section in report.sections
    )

    coaching = coaching_view(report.model_copy(update={"sections": sections}), _timeline(late_parser_frame=56_003))

    assert all(item.signal_id != "economy.supply_collection_rate" for item in coaching.highlights)
    assert coaching.signal_reads == ()


def test_ollama_state_cannot_change_deterministic_coaching() -> None:
    report = _report()
    offline = report.model_copy(
        update={
            "source_mode": "deterministic_with_ollama",
            "ollama": OllamaReportStatusDTO(
                requested=True,
                status="unavailable",
                diagnostic_codes=("ollama_offline",),
            ),
        }
    )
    succeeded = report.model_copy(
        update={
            "source_mode": "deterministic_with_ollama",
            "ollama": OllamaReportStatusDTO(
                requested=True,
                status="succeeded",
                analysis_run_id="123e4567-e89b-42d3-a456-426614170009",
                provider="ollama",
                model_name="fixture-model",
                model_digest="b" * 64,
                prompt_version="strategy-report-v1",
                response_schema_version="strategy-report-response-v1",
                validated_prose={"summary": "Validated local interpretation."},
            ),
        }
    )

    baseline = coaching_view(report, _timeline())
    offline_view = coaching_view(offline, _timeline())
    succeeded_view = coaching_view(succeeded, _timeline())

    assert offline_view == baseline
    assert succeeded_view.model_dump(exclude={"local_model_summary"}) == baseline.model_dump(
        exclude={"local_model_summary"}
    )
    assert succeeded_view.local_model_summary == "Validated local interpretation."


def test_coaching_rejects_cross_report_timeline_data() -> None:
    timeline = _timeline().model_copy(
        update={"query": TimelineChartQueryDTO(replay_public_id=REPLAY, report_public_id=OPPONENT_REPORT)}
    )

    with pytest.raises(ValueError, match="timeline identity"):
        coaching_view(_report(), timeline)
