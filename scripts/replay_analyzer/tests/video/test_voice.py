"""Measured, deterministic narration scheduling and WAV assembly tests."""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from generals_replay_analyzer.video.contracts import (
    CommentaryEventV1,
    CommentaryPlanV1,
    EvidenceCitationV1,
    EvidenceHorizonV1,
)
from generals_replay_analyzer.video.voice import (
    NarrationScheduleError,
    NarrationScheduler,
    VoiceClipV1,
    render_narration_wav,
    require_terminal_narration_coverage,
)

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
REPORT_ID = "20000000-0000-4000-8000-000000000001"
EVIDENCE_ID = "30000000-0000-4000-8000-000000000001"
CAMERA_ID = "40000000-0000-4000-8000-000000000001"


def _event(event_id: str, start: int, end: int, text: str) -> CommentaryEventV1:
    return CommentaryEventV1(
        event_id=event_id,
        start_frame=start,
        latest_end_frame=end,
        text=text,
        subtitle_text=text,
        role="play_by_play",
        evidence=(EvidenceCitationV1(
            evidence_public_id=EVIDENCE_ID,
            tier="observed",
            frame_start=start,
            frame_end=end,
        ),),
        confidence_tier="observed",
        camera_segment_id=CAMERA_ID,
    )


def _plan(*events: CommentaryEventV1, final_frame: int = 299) -> CommentaryPlanV1:
    return CommentaryPlanV1(
        logic_hz=30,
        replay_public_id=REPLAY_ID,
        report_public_id=REPORT_ID,
        evidence_horizon=EvidenceHorizonV1(frame_end=final_frame),
        events=events,
    )


def _wav(path: Path, sample_count: int, value: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframesraw(value.to_bytes(2, "little", signed=True) * sample_count)


def _clip(event: CommentaryEventV1, path: Path, samples: int, value: int = 1) -> VoiceClipV1:
    _wav(path, samples, value)
    return VoiceClipV1.from_wav(event, path, provider_name="fixture", voice_name="Fixture Voice")


def test_scheduler_uses_measured_samples_and_exact_logic_frame_conversion(tmp_path: Path) -> None:
    first = _event("50000000-0000-4000-8000-000000000001", 30, 89, "First call")
    second = _event("50000000-0000-4000-8000-000000000002", 120, 179, "Second call")
    first_clip = _clip(first, tmp_path / "first.wav", 48_001)
    second_clip = _clip(second, tmp_path / "second.wav", 24_000, 2)

    schedule = NarrationScheduler().schedule(_plan(first, second), (second_clip, first_clip), final_frame=299)

    assert [(item.start_frame, item.end_frame) for item in schedule.events] == [(30, 60), (120, 134)]
    assert schedule.events[0].clip.sample_count == 48_001
    assert schedule.events[0].clip.text_sha256 == hashlib.sha256(first.text.encode()).hexdigest()
    assert schedule.events[0].clip.provider_sha256 == hashlib.sha256(b"fixture").hexdigest()
    assert schedule.events[0].clip.voice_sha256 == hashlib.sha256(b"Fixture Voice").hexdigest()
    assert schedule.events[0].clip.clip_sha256 == hashlib.sha256((tmp_path / "first.wav").read_bytes()).hexdigest()


def test_scheduler_uses_sixty_hz_logic_timebase(tmp_path: Path) -> None:
    event = _event("50000000-0000-4000-8000-000000000003", 0, 60, "Half second")
    clip = _clip(event, tmp_path / "sixty.wav", 24_000)
    plan = _plan(event, final_frame=119).model_copy(update={"logic_hz": 60})

    schedule = NarrationScheduler().schedule(plan, (clip,), final_frame=119)

    assert schedule.logic_hz == 60
    assert (schedule.events[0].start_frame, schedule.events[0].end_frame) == (0, 29)


def test_scheduler_rejects_clips_that_cannot_fit_without_crossing_latest_end(tmp_path: Path) -> None:
    event = _event("50000000-0000-4000-8000-000000000001", 30, 59, "Too long")
    clip = _clip(event, tmp_path / "long.wav", 48_001)

    with pytest.raises(NarrationScheduleError, match="latest_end_frame"):
        NarrationScheduler().schedule(_plan(event), (clip,), final_frame=299)


def test_scheduler_rejects_tampered_clip_bytes_and_mismatched_event_sets(tmp_path: Path) -> None:
    event = _event("50000000-0000-4000-8000-000000000001", 0, 60, "Grounded")
    clip = _clip(event, tmp_path / "clip.wav", 16_000)
    (tmp_path / "clip.wav").write_bytes(b"tampered")
    with pytest.raises(NarrationScheduleError, match="hash"):
        NarrationScheduler().schedule(_plan(event), (clip,), final_frame=299)

    other = _event("50000000-0000-4000-8000-000000000002", 90, 120, "Other")
    other_clip = _clip(other, tmp_path / "other.wav", 8_000)
    with pytest.raises(NarrationScheduleError, match="exactly one clip"):
        NarrationScheduler().schedule(_plan(event), (other_clip,), final_frame=299)


def test_production_narration_rejects_more_than_thirty_seconds_of_terminal_silence(tmp_path: Path) -> None:
    event = _event("50000000-0000-4000-8000-000000000004", 0, 60, "Opening only")
    clip = _clip(event, tmp_path / "opening-only.wav", 48_000)
    schedule = NarrationScheduler().schedule(_plan(event, final_frame=930), (clip,), final_frame=930)

    with pytest.raises(NarrationScheduleError, match="more than 30 seconds of terminal silence"):
        require_terminal_narration_coverage(schedule, maximum_silent_frames=900)


def test_production_narration_accepts_exactly_thirty_seconds_of_terminal_silence(tmp_path: Path) -> None:
    event = _event("50000000-0000-4000-8000-000000000005", 0, 60, "Opening only")
    clip = _clip(event, tmp_path / "bounded-tail.wav", 48_000)
    schedule = NarrationScheduler().schedule(_plan(event, final_frame=929), (clip,), final_frame=929)

    assert schedule.events[-1].end_frame == 29
    assert schedule.final_frame - schedule.events[-1].end_frame == 900
    require_terminal_narration_coverage(schedule, maximum_silent_frames=900)


def test_terminal_narration_coverage_rejects_invalid_frame_limits(tmp_path: Path) -> None:
    event = _event("50000000-0000-4000-8000-000000000006", 0, 60, "Opening only")
    clip = _clip(event, tmp_path / "limit-validation.wav", 48_000)
    schedule = NarrationScheduler().schedule(_plan(event, final_frame=929), (clip,), final_frame=929)

    with pytest.raises(TypeError, match="integer frame count"):
        require_terminal_narration_coverage(schedule, maximum_silent_frames=True)
    with pytest.raises(ValueError, match="non-negative"):
        require_terminal_narration_coverage(schedule, maximum_silent_frames=-1)


def test_render_narration_wav_has_exact_authoritative_duration_and_silence_gaps(tmp_path: Path) -> None:
    event = _event("50000000-0000-4000-8000-000000000001", 30, 60, "One second")
    clip = _clip(event, tmp_path / "clip.wav", 48_000, 7)
    schedule = NarrationScheduler().schedule(_plan(event, final_frame=89), (clip,), final_frame=89)

    destination = render_narration_wav(schedule, tmp_path / "render" / "narration.wav")

    with wave.open(str(destination), "rb") as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 48_000)
        assert source.getnframes() == 144_000
        assert source.readframes(48_000) == bytes(96_000)
        assert source.readframes(1) == (7).to_bytes(2, "little", signed=True)
