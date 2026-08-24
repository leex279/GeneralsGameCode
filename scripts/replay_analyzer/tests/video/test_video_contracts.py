"""Closed camera and video contract coverage."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from generals_replay_analyzer.video.contracts import (
    CameraPlanAuthorityV1,
    CameraPlanV1,
    CameraSegmentV1,
    EvidenceCitationV1,
    EvidenceHorizonV1,
    VideoSettingsV1,
)

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
REPORT_ID = "20000000-0000-4000-8000-000000000001"
RUN_ID = "30000000-0000-4000-8000-000000000001"
MAP_ID = "40000000-0000-4000-8000-000000000001"
EVIDENCE_ID = "50000000-0000-4000-8000-000000000001"
SEGMENT_A = "60000000-0000-4000-8000-000000000001"
SEGMENT_B = "60000000-0000-4000-8000-000000000002"


def _authority() -> CameraPlanAuthorityV1:
    return CameraPlanAuthorityV1(
        replay_public_id=REPLAY_ID,
        replay_sha256="a" * 64,
        report_public_id=REPORT_ID,
        telemetry_run_public_id=RUN_ID,
        telemetry_trace_sha256="b" * 64,
        map_public_id=MAP_ID,
        map_content_sha256="c" * 64,
        evidence_horizon=EvidenceHorizonV1(frame_start=0, frame_end=300),
        logic_frames_per_second=30,
    )


def _citation(frame_start: int, frame_end: int) -> EvidenceCitationV1:
    return EvidenceCitationV1(
        evidence_public_id=EVIDENCE_ID,
        tier="observed",
        frame_start=frame_start,
        frame_end=frame_end,
    )


def _segment(segment_id: str, start: int, end: int, transition: str = "cut") -> CameraSegmentV1:
    return CameraSegmentV1(
        segment_id=segment_id,
        start_frame=start,
        end_frame=end,
        target_x=100.0,
        target_y=200.0,
        target_z=0.0,
        zoom=1.0,
        pitch=-45.0,
        yaw=0.0,
        transition=transition,
        transition_frames=0 if transition == "cut" else 15,
        focus_kind="base_context",
        label="Base context",
        evidence=(_citation(start, min(end, start + 1)),),
    )


def test_camera_plan_is_frozen_gapless_and_canonical() -> None:
    plan = CameraPlanV1(
        authority=_authority(),
        segments=(
            _segment(SEGMENT_A, 0, 149),
            _segment(SEGMENT_B, 150, 300, "ease"),
        ),
    )

    assert plan.schema_version == 1
    assert plan.logic_hz == 30
    assert '"schema_version":1' in plan.canonical_json()
    with pytest.raises(ValidationError):
        plan.logic_hz = 60  # type: ignore[misc]


@pytest.mark.parametrize(
    "segments",
    [
        (_segment(SEGMENT_A, 1, 300),),
        (_segment(SEGMENT_A, 0, 100), _segment(SEGMENT_B, 102, 300)),
        (_segment(SEGMENT_A, 0, 299),),
    ],
)
def test_camera_plan_rejects_non_gapless_or_wrong_horizon_segments(
    segments: tuple[CameraSegmentV1, ...],
) -> None:
    with pytest.raises(ValidationError):
        CameraPlanV1(authority=_authority(), segments=segments)


def test_camera_segment_rejects_invalid_transitions_and_values() -> None:
    payload = _segment(SEGMENT_A, 0, 20).model_dump(mode="python")
    payload.update({"transition": "ease", "transition_frames": 0})
    with pytest.raises(ValidationError):
        CameraSegmentV1.model_validate(payload)
    payload.update({"transition_frames": 22})
    with pytest.raises(ValidationError):
        CameraSegmentV1.model_validate(payload)
    payload = _segment(SEGMENT_A, 0, 20).model_dump(mode="python")
    payload["target_x"] = math.inf
    with pytest.raises(ValidationError):
        CameraSegmentV1.model_validate(payload)


def test_plan_rejects_citations_outside_authoritative_horizon() -> None:
    segment = _segment(SEGMENT_A, 0, 300).model_copy(
        update={"evidence": (_citation(0, 301),)}
    )
    with pytest.raises(ValidationError):
        CameraPlanV1(authority=_authority(), segments=(segment,))


def test_video_settings_accept_only_bounded_product_modes() -> None:
    assert VideoSettingsV1(width=1920, height=1080, fps=60, subtitle_mode="track").fps == 60
    with pytest.raises(ValidationError):
        VideoSettingsV1(width=1920, height=1080, fps=24, subtitle_mode="track")
    with pytest.raises(ValidationError):
        VideoSettingsV1(width=40, height=40, fps=30, subtitle_mode="burned")


def test_authority_propagates_sixty_logic_frames_per_second() -> None:
    authority = _authority().model_copy(update={"logic_frames_per_second": 60})
    plan = CameraPlanV1(authority=authority, segments=(_segment(SEGMENT_A, 0, 300),))

    assert authority.logic_frames_per_second == 60
    assert plan.logic_hz == 60


def test_authority_requires_an_explicit_logic_timebase() -> None:
    payload = _authority().model_dump(mode="python")
    payload.pop("logic_frames_per_second")

    with pytest.raises(ValidationError, match="logic_frames_per_second"):
        CameraPlanAuthorityV1.model_validate(payload)
