from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from generals_replay_analyzer.report.model import (
    OllamaReportStatus,
    ReportDocument,
    ReportEvidenceRef,
    ReportLifecycle,
    ReportValue,
)
from generals_replay_analyzer.report.read_model import (
    PublishedReportAssetDTO,
    PublishedReportDTO,
    PublishedReportGraphDTO,
    ReportPlayerIdentityDTO,
    ReportReplayIdentityDTO,
)
from generals_replay_analyzer.video.commentary import CommentaryPlanContractError, CommentaryPlanService
from generals_replay_analyzer.video.contracts import (
    CameraPlanAuthorityV1,
    CameraPlanV1,
    CameraSegmentV1,
    CommentaryEventV1,
    EvidenceCitationV1,
    EvidenceHorizonV1,
    ValidatedCommentaryEnrichmentV1,
)

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
REPORT_ID = "20000000-0000-4000-8000-000000000001"
PLAYER_REPORT_ID = "20000000-0000-4000-8000-000000000002"
RUN_ID = "30000000-0000-4000-8000-000000000001"
MAP_ID = "40000000-0000-4000-8000-000000000001"
PLAYER_ONE_ID = "70000000-0000-4000-8000-000000000001"
PLAYER_TWO_ID = "70000000-0000-4000-8000-000000000002"
MAP_EVIDENCE = "50000000-0000-4000-8000-000000000001"
STRATEGY_EVIDENCE = "50000000-0000-4000-8000-000000000002"
MILESTONE_EVIDENCE = "50000000-0000-4000-8000-000000000003"
ENGAGEMENT_EVIDENCE = "50000000-0000-4000-8000-000000000004"
OUTCOME_EVIDENCE = "50000000-0000-4000-8000-000000000005"
BOUNDARY_EVIDENCE = "50000000-0000-4000-8000-000000000006"


def _authority(
    frame_end: int, *, replay_public_id: str = REPLAY_ID, report_public_id: str = REPORT_ID
) -> CameraPlanAuthorityV1:
    return CameraPlanAuthorityV1(
        replay_public_id=replay_public_id,
        replay_sha256="a" * 64,
        report_public_id=report_public_id,
        telemetry_run_public_id=RUN_ID,
        telemetry_trace_sha256="b" * 64,
        map_public_id=MAP_ID,
        map_content_sha256="c" * 64,
        evidence_horizon=EvidenceHorizonV1(frame_end=frame_end),
        logic_frames_per_second=30,
    )


def _value(
    claim_id: str, section: str, label: str, frame_window: tuple[int, int], evidence_id: str, raw_value: object
) -> ReportValue:
    return ReportValue(
        claim_id=claim_id,
        section=section,
        label=label,
        raw_value=raw_value,
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=frame_window,
        evidence=(ReportEvidenceRef(evidence_id, "observed"),),
        details={},
    )


def _report(*, partial: bool = False) -> PublishedReportGraphDTO:
    observed = [
        _value("map.start", "map", "Map start", (0, 0), MAP_EVIDENCE, {"map": "Tournament Desert"}),
        _value(
            "strategy:usa_humvee_pressure:one",
            "strategy",
            "Humvee pressure",
            (15, 30),
            STRATEGY_EVIDENCE,
            {"player_public_id": PLAYER_ONE_ID, "strategy_label": "Humvee pressure"},
        ),
        _value("build.supply", "build_order", "Supply Center", (45, 55), MILESTONE_EVIDENCE, {"count": 1}),
        _value("combat.engagement", "combat", "Main engagement", (70, 90), ENGAGEMENT_EVIDENCE, {"damage": 500}),
    ]
    if partial:
        observed.append(
            _value(
                "telemetry.boundary", "quality", "Observed boundary", (100, 105), BOUNDARY_EVIDENCE, {"complete": False}
            )
        )
    else:
        observed.append(
            _value("outcome.match", "outcome", "Match result", (290, 300), OUTCOME_EVIDENCE, {"winner": PLAYER_ONE_ID})
        )
    document = ReportDocument(
        schema_version="replay-report-v1",
        report_public_id=REPORT_ID,
        report_version="replay-report-v1",
        input_digest="d" * 64,
        cache_key="e" * 64,
        replay_public_id=REPLAY_ID,
        replay_sha256="a" * 64,
        replay_player_public_id=None,
        lifecycle=ReportLifecycle("complete", "complete", "complete", "complete"),
        evidence_availability=(),
        quality_issues=(),
        observed=tuple(observed),
        derived=(),
        inferred=(),
        ollama=OllamaReportStatus.not_requested(),
        warnings=(),
    )
    asset = PublishedReportAssetDTO(
        "80000000-0000-4000-8000-000000000001", "f" * 64, "report_structured_json", "application/json", 2
    )
    presentation = PublishedReportAssetDTO(
        "80000000-0000-4000-8000-000000000002", "1" * 64, "report_presentation_bundle", "application/json", 2
    )
    published = PublishedReportDTO(document, asset, presentation, "<p>report</p>", "report", datetime.now(UTC))
    return PublishedReportGraphDTO(
        "replay-report-read-model-v1",
        "report-output-v1",
        REPLAY_ID,
        REPORT_ID,
        ReportReplayIdentityDTO(
            "Replay",
            "Tournament Desert",
            "1.04",
            300,
            (
                ReportPlayerIdentityDTO(PLAYER_ONE_ID, "Alice", 1, "USA", "won"),
                ReportPlayerIdentityDTO(PLAYER_TWO_ID, "Bob", 2, "China", "lost"),
            ),
        ),
        published,
        (),
    )


def _player_selected_report() -> PublishedReportGraphDTO:
    graph = _report()
    player_document = replace(
        graph.replay_wide.document,
        report_public_id=PLAYER_REPORT_ID,
        replay_player_public_id=PLAYER_ONE_ID,
        observed=tuple(value for value in graph.replay_wide.document.observed if value.claim_id != "map.start"),
    )
    player_report = PublishedReportDTO(
        player_document,
        graph.replay_wide.structured_asset,
        graph.replay_wide.presentation_asset,
        graph.replay_wide.html,
        graph.replay_wide.text,
        graph.replay_wide.created_at_utc,
    )
    return PublishedReportGraphDTO(
        graph.schema_version,
        graph.output_schema_version,
        graph.replay_public_id,
        PLAYER_REPORT_ID,
        graph.identity,
        graph.replay_wide,
        (player_report,),
    )


def _camera(frame_end: int, *, replay_public_id: str = REPLAY_ID) -> CameraPlanV1:
    return CameraPlanV1(
        authority=_authority(frame_end, replay_public_id=replay_public_id),
        segments=(
            CameraSegmentV1(
                segment_id="60000000-0000-4000-8000-000000000001",
                start_frame=0,
                end_frame=frame_end,
                target_x=0.0,
                target_y=0.0,
                target_z=0.0,
                zoom=1.0,
                pitch=-45.0,
                yaw=0.0,
                transition="cut",
                transition_frames=0,
                focus_kind="base_context",
                label="Base context",
                evidence=(
                    EvidenceCitationV1(
                        evidence_public_id=MAP_EVIDENCE,
                        tier="observed",
                        frame_start=0,
                        frame_end=0,
                    ),
                ),
            ),
        ),
    )


def test_complete_plan_introduces_match_and_covers_strategy_milestone_engagement_and_outcome() -> None:
    plan = CommentaryPlanService().create(_report(), _camera(300))

    assert plan.schema_version == 1
    assert plan.logic_hz == 30
    assert plan.events[0].role == "intro"
    assert "Tournament Desert" in plan.events[0].text
    assert "Alice" in plan.events[0].text and "Bob" in plan.events[0].text
    assert any("Humvee pressure" in event.text for event in plan.events)
    assert any("Supply Center" in event.text for event in plan.events)
    assert any("Main engagement" in event.text for event in plan.events)
    assert plan.events[-1].role == "outro"
    assert "Alice wins" in plan.events[-1].text
    assert all(event.evidence for event in plan.events)
    assert all(event.latest_end_frame <= 300 for event in plan.events)
    assert all(later.start_frame > earlier.latest_end_frame for earlier, later in zip(plan.events, plan.events[1:]))


def test_commentary_speech_window_never_crosses_its_camera_segment_cut() -> None:
    graph = _report()
    document = replace(
        graph.replay_wide.document,
        observed=tuple(
            value
            for value in graph.replay_wide.document.observed
            if value.claim_id in ("map.start", "strategy:usa_humvee_pressure:one")
        ),
    )
    report = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    citation = EvidenceCitationV1(
        evidence_public_id=MAP_EVIDENCE,
        tier="observed",
        frame_start=0,
        frame_end=0,
    )
    camera = CameraPlanV1(
        authority=_authority(300),
        segments=(
            CameraSegmentV1(
                segment_id="60000000-0000-4000-8000-000000000011",
                start_frame=0,
                end_frame=44,
                target_x=0.0,
                target_y=0.0,
                target_z=0.0,
                zoom=1.0,
                pitch=-45.0,
                yaw=0.0,
                transition="cut",
                transition_frames=0,
                focus_kind="base_context",
                label="Opening base",
                evidence=(citation,),
            ),
            CameraSegmentV1(
                segment_id="60000000-0000-4000-8000-000000000012",
                start_frame=45,
                end_frame=300,
                target_x=100.0,
                target_y=100.0,
                target_z=0.0,
                zoom=1.0,
                pitch=-45.0,
                yaw=0.0,
                transition="cut",
                transition_frames=0,
                focus_kind="milestone",
                label="Next camera shot",
                evidence=(citation,),
            ),
        ),
    )

    plan = CommentaryPlanService().create(report, camera)

    strategy = next(event for event in plan.events if event.role == "analysis")
    assert strategy.camera_segment_id == "60000000-0000-4000-8000-000000000011"
    assert strategy.latest_end_frame == 44


def test_long_cast_adds_cited_late_exchange_inside_terminal_silence_budget() -> None:
    graph = _report()
    document = replace(
        graph.replay_wide.document,
        observed=tuple(
            value
            for value in graph.replay_wide.document.observed
            if value.claim_id in ("map.start", "combat.engagement")
        ),
    )
    report = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    base = _camera(3_600).segments[0].model_copy(update={"end_frame": 2_499})
    late = CameraSegmentV1(
        segment_id="60000000-0000-4000-8000-000000000013",
        start_frame=2_500,
        end_frame=3_600,
        target_x=100.0,
        target_y=100.0,
        target_z=0.0,
        zoom=1.05,
        pitch=-45.0,
        yaw=0.0,
        transition="ease",
        transition_frames=30,
        focus_kind="damage",
        label="Damage location",
        evidence=(
            EvidenceCitationV1(
                evidence_public_id=ENGAGEMENT_EVIDENCE,
                tier="observed",
                frame_start=2_500,
                frame_end=2_500,
                support_role="event_timing",
                observed_frame=2_500,
            ),
        ),
    )
    camera = CameraPlanV1(authority=_authority(3_600), segments=(base, late))

    plan = CommentaryPlanService().create(report, camera)

    terminal = plan.events[-1]
    assert terminal.role == "outro"
    assert terminal.start_frame == 2_850
    assert terminal.latest_end_frame == 3_600
    assert terminal.camera_segment_id == late.segment_id
    assert terminal.evidence == late.evidence


def test_partial_plan_announces_boundary_without_later_phases_or_winner_and_is_offline_deterministic() -> None:
    service = CommentaryPlanService()
    first = service.create(_report(partial=True), _camera(105))
    second = service.create(_report(partial=True), _camera(105))

    assert first.canonical_json() == second.canonical_json()
    assert first.events[0].role == "intro"
    assert first.events[0].text == "Tournament Desert. Preview."
    assert all(event.latest_end_frame <= 105 for event in first.events)
    assert all("wins" not in event.text for event in first.events)
    assert all(event.evidence[0].tier == "observed" for event in first.events)


def test_invalid_enrichment_is_rejected_as_a_whole_and_keeps_deterministic_text() -> None:
    deterministic = CommentaryPlanService().create(_report(), _camera(300))
    bad = ValidatedCommentaryEnrichmentV1(
        ollama_run_public_id="90000000-0000-4000-8000-000000000001",
        sentences=(
            {
                "event_id": deterministic.events[1].event_id,
                "text": "Unsupported claim.",
                "evidence_public_ids": ("50000000-0000-4000-8000-000000000099",),
                "frame_start": deterministic.events[1].start_frame,
                "frame_end": deterministic.events[1].latest_end_frame,
            },
        ),
    )

    enriched = CommentaryPlanService().create(_report(), _camera(300), enrichment=bad)

    assert enriched.canonical_json() == deterministic.canonical_json()


def test_enrichment_requires_exact_event_evidence_and_frame_window() -> None:
    deterministic = CommentaryPlanService().create(_report(), _camera(300))
    target = deterministic.events[1]
    valid = ValidatedCommentaryEnrichmentV1(
        ollama_run_public_id="90000000-0000-4000-8000-000000000001",
        sentences=(
            {
                "event_id": target.event_id,
                "text": "Alice commits to Humvee pressure.",
                "evidence_public_ids": tuple(item.evidence_public_id for item in target.evidence),
                "frame_start": target.start_frame,
                "frame_end": target.latest_end_frame,
            },
        ),
    )

    enriched = CommentaryPlanService().create(_report(), _camera(300), enrichment=valid)

    assert enriched.events[1].text == "Alice commits to Humvee pressure."
    assert enriched.events[1].ollama_run_public_id == valid.ollama_run_public_id


def test_rejects_report_and_camera_from_different_replays() -> None:
    with pytest.raises(CommentaryPlanContractError, match="replay"):
        CommentaryPlanService().create(_report(), _camera(300, replay_public_id="10000000-0000-4000-8000-000000000099"))


def test_player_selected_plan_preserves_the_camera_report_authority() -> None:
    camera = _camera(300).model_copy(update={"authority": _authority(300, report_public_id=PLAYER_REPORT_ID)})

    plan = CommentaryPlanService().create(_player_selected_report(), camera)

    assert plan.report_public_id == PLAYER_REPORT_ID


def test_commentary_may_speak_after_point_evidence_becomes_available() -> None:
    event = CommentaryEventV1(
        event_id="60000000-0000-4000-8000-000000000099",
        start_frame=15,
        latest_end_frame=60,
        text="The opening order is now confirmed.",
        subtitle_text="The opening order is now confirmed.",
        role="analysis",
        evidence=(
            EvidenceCitationV1(
                evidence_public_id=STRATEGY_EVIDENCE,
                tier="observed",
                frame_start=15,
                frame_end=15,
            ),
        ),
        confidence_tier="observed",
        camera_segment_id="60000000-0000-4000-8000-000000000001",
    )

    assert event.latest_end_frame == 60


def test_full_match_player_features_create_timed_strategy_build_and_production_commentary() -> None:
    graph = _player_selected_report()
    supply = _value(
        "observed:supply",
        "timeline",
        "object_created",
        (60, 60),
        MILESTONE_EVIDENCE,
        {"owner_player_index": 1, "template_name": "GLASupplyStash"},
    )
    palace = _value(
        "observed:palace",
        "timeline",
        "object_created",
        (180, 180),
        BOUNDARY_EVIDENCE,
        {"owner_player_index": 1, "template_name": "GLAPalace"},
    )
    quad = _value(
        "observed:quad",
        "timeline",
        "production_completed",
        (120, 120),
        ENGAGEMENT_EVIDENCE,
        {"player_index": 1, "state": "completed", "template_name": "GLAVehicleQuadCannon"},
    )
    build_sequence = ReportValue(
        claim_id="feature:build.completed_sequence:test",
        section="features",
        label="build.completed_sequence",
        raw_value=(
            {"frame": 60, "template_name": "GLASupplyStash"},
            {"frame": 180, "template_name": "GLAPalace"},
        ),
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={"scope_key": PLAYER_ONE_ID, "scope_type": "player"},
        frame_window=(0, 302),
        evidence=(
            ReportEvidenceRef(MILESTONE_EVIDENCE, "observed"),
            ReportEvidenceRef(BOUNDARY_EVIDENCE, "observed"),
            ReportEvidenceRef(STRATEGY_EVIDENCE, "derived"),
        ),
        details={},
    )
    strategy = ReportValue(
        claim_id="strategy:gla_fast_palace:test",
        section="strategy",
        label="gla_fast_palace",
        raw_value={"confidence": 1.0, "phase": "mid", "strategy_label": "gla_fast_palace"},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "player"},
        frame_window=(0, 301),
        evidence=(
            ReportEvidenceRef(BOUNDARY_EVIDENCE, "observed"),
            ReportEvidenceRef(STRATEGY_EVIDENCE, "derived"),
        ),
        details={},
    )
    document = replace(
        graph.selected.document,
        observed=(supply, quad, palace),
        derived=(build_sequence, strategy),
    )
    report = replace(graph, player_reports=(replace(graph.selected, document=document),))
    camera = _camera(300).model_copy(update={"authority": _authority(300, report_public_id=PLAYER_REPORT_ID)})

    plan = CommentaryPlanService().create(report, camera)

    texts = " ".join(event.text for event in plan.events)
    assert "Alice" in texts
    assert "Supply Stash" in texts
    assert "Quad Cannon" in texts
    assert "fast Palace" in texts
    assert len(plan.events) >= 4


def test_strategy_anchor_order_is_deterministic_for_duplicate_frame_and_template_claims() -> None:
    graph = _player_selected_report()
    build_sequence = ReportValue(
        claim_id="feature:build.completed_sequence:a",
        section="features",
        label="build.completed_sequence",
        raw_value=({"frame": 180, "template_name": "GLAPalace"},),
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={"scope_key": PLAYER_ONE_ID, "scope_type": "player"},
        frame_window=(0, 300),
        evidence=(
            ReportEvidenceRef(BOUNDARY_EVIDENCE, "observed"),
            ReportEvidenceRef(STRATEGY_EVIDENCE, "derived"),
        ),
        details={},
    )
    strategy = ReportValue(
        claim_id="strategy:gla_fast_palace:duplicate-anchor",
        section="strategy",
        label="gla_fast_palace",
        raw_value={"confidence": 1.0, "phase": "mid", "strategy_label": "gla_fast_palace"},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "player"},
        frame_window=(0, 300),
        evidence=(ReportEvidenceRef(STRATEGY_EVIDENCE, "derived"),),
        details={},
    )
    document = replace(
        graph.selected.document,
        observed=(),
        derived=(build_sequence, replace(build_sequence, claim_id="feature:build.completed_sequence:b"), strategy),
    )
    report = replace(graph, player_reports=(replace(graph.selected, document=document),))
    camera = _camera(300).model_copy(update={"authority": _authority(300, report_public_id=PLAYER_REPORT_ID)})

    plan = CommentaryPlanService().create(report, camera)

    assert any("fast Palace" in event.text for event in plan.events)


def test_observed_strategy_anchor_uses_claim_identity_to_break_equal_frame_and_template_ties() -> None:
    graph = _player_selected_report()
    technical_a = _value(
        "observed:technical:a",
        "timeline",
        "production_completed",
        (120, 120),
        MILESTONE_EVIDENCE,
        {"player_index": 1, "template_name": "GLAVehicleTechnical"},
    )
    technical_b = _value(
        "observed:technical:b",
        "timeline",
        "production_completed",
        (120, 120),
        BOUNDARY_EVIDENCE,
        {"player_index": 1, "template_name": "GLAVehicleTechnical"},
    )
    strategy = ReportValue(
        claim_id="strategy:gla_technical_aggression:duplicate-anchor",
        section="strategy",
        label="gla_technical_aggression",
        raw_value={"confidence": 1.0, "phase": "opening", "strategy_label": "gla_technical_aggression"},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "player"},
        frame_window=(0, 300),
        evidence=(ReportEvidenceRef(STRATEGY_EVIDENCE, "derived"),),
        details={},
    )
    document = replace(graph.selected.document, observed=(technical_b, technical_a), derived=(strategy,))
    report = replace(graph, player_reports=(replace(graph.selected, document=document),))
    camera = _camera(300).model_copy(update={"authority": _authority(300, report_public_id=PLAYER_REPORT_ID)})

    plan = CommentaryPlanService().create(report, camera)

    event = next(item for item in plan.events if "Technical aggression" in item.text)
    assert MILESTONE_EVIDENCE in {citation.evidence_public_id for citation in event.evidence}
    assert BOUNDARY_EVIDENCE not in {citation.evidence_public_id for citation in event.evidence}


def test_observed_kill_timing_gets_specific_commentary_and_replaces_cluster() -> None:
    graph = _player_selected_report()
    observed_kills = ReportValue(
        claim_id="feature:combat.observed_kill_timing:test",
        section="features",
        label="combat.observed_kill_timing",
        raw_value=(
            {
                "frame": 180,
                "attacker_template_name": "GLAVehicleTechnical",
                "victim_template_name": "AmericaVehicleHumvee",
            },
        ),
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "player"},
        frame_window=(0, 300),
        evidence=(ReportEvidenceRef("50000000-0000-4000-8000-000000000007", "derived"),),
        details={},
    )
    player_document = replace(graph.selected.document, derived=(observed_kills,))
    report = replace(graph, player_reports=(replace(graph.selected, document=player_document),))

    camera = _camera(300).model_copy(update={"authority": _authority(300, report_public_id=PLAYER_REPORT_ID)})
    plan = CommentaryPlanService().create(report, camera)
    texts = " ".join(event.text for event in plan.events)

    assert "Technical scores a confirmed kill on the Humvee" in texts
    assert "confirmed destructions" not in texts


def test_observed_kill_commentary_reserves_one_of_five_windows_for_the_latest_supported_kill() -> None:
    graph = _player_selected_report()
    kills = (
        {
            "frame": 11_000,
            "attacker_template_name": "GLAVehicleTechnical",
            "victim_template_name": "AmericaVehicleHumvee",
        },
        *(
            {
                "frame": 120 + index * 1_800,
                "attacker_template_name": "GLAVehicleTechnical",
                "victim_template_name": "AmericaVehicleHumvee",
            }
            for index in range(7)
        ),
    )
    observed_kills = ReportValue(
        claim_id="feature:combat.observed_kill_timing:sparse",
        section="features",
        label="combat.observed_kill_timing",
        raw_value=kills,
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "player"},
        frame_window=(0, 11_700),
        evidence=(ReportEvidenceRef("50000000-0000-4000-8000-000000000007", "derived"),),
        details={},
    )
    player_document = replace(graph.selected.document, derived=(observed_kills,))
    report = replace(graph, player_reports=(replace(graph.selected, document=player_document),))
    camera = _camera(11_700).model_copy(
        update={"authority": _authority(11_700, report_public_id=PLAYER_REPORT_ID)}
    )

    plan = CommentaryPlanService().create(report, camera)
    kill_events = [event for event in plan.events if "scores a confirmed kill" in event.text]

    assert len(kill_events) <= 5
    assert kill_events[-1].start_frame == 11_000
    assert plan.evidence_horizon.frame_end - kill_events[-1].start_frame <= plan.logic_hz * 30
