from __future__ import annotations

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
    EvidenceCitationV1,
    EvidenceHorizonV1,
    ValidatedCommentaryEnrichmentV1,
)

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
REPORT_ID = "20000000-0000-4000-8000-000000000001"
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


def _authority(frame_end: int, *, replay_public_id: str = REPLAY_ID) -> CameraPlanAuthorityV1:
    return CameraPlanAuthorityV1(
        replay_public_id=replay_public_id,
        replay_sha256="a" * 64,
        report_public_id=REPORT_ID,
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
            _value("telemetry.boundary", "quality", "Observed boundary", (100, 105), BOUNDARY_EVIDENCE, {"complete": False})
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
    asset = PublishedReportAssetDTO("80000000-0000-4000-8000-000000000001", "f" * 64, "report_structured_json", "application/json", 2)
    presentation = PublishedReportAssetDTO("80000000-0000-4000-8000-000000000002", "1" * 64, "report_presentation_bundle", "application/json", 2)
    published = PublishedReportDTO(document, asset, presentation, "<p>report</p>", "report", datetime.now(UTC))
    return PublishedReportGraphDTO(
        "replay-report-read-model-v1", "report-output-v1", REPLAY_ID, REPORT_ID,
        ReportReplayIdentityDTO("Replay", "Tournament Desert", "1.04", 300, (
            ReportPlayerIdentityDTO(PLAYER_ONE_ID, "Alice", 1, "USA", "won"),
            ReportPlayerIdentityDTO(PLAYER_TWO_ID, "Bob", 2, "China", "lost"),
        )),
        published, (),
    )


def _camera(frame_end: int, *, replay_public_id: str = REPLAY_ID) -> CameraPlanV1:
    return CameraPlanV1(
        authority=_authority(frame_end, replay_public_id=replay_public_id),
        segments=(CameraSegmentV1(
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
            evidence=(EvidenceCitationV1(
                evidence_public_id=MAP_EVIDENCE,
                tier="observed",
                frame_start=0,
                frame_end=frame_end,
            ),),
        ),),
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


def test_partial_plan_announces_boundary_without_later_phases_or_winner_and_is_offline_deterministic() -> None:
    service = CommentaryPlanService()
    first = service.create(_report(partial=True), _camera(105))
    second = service.create(_report(partial=True), _camera(105))

    assert first.canonical_json() == second.canonical_json()
    assert any(event.role == "transition" and "Evidence ends at frame 105" in event.text for event in first.events)
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
