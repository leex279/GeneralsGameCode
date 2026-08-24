"""Subtitle timing must be the exact scheduled narration timing."""

from __future__ import annotations

import wave
from pathlib import Path

from generals_replay_analyzer.video.contracts import (
    CommentaryEventV1,
    CommentaryPlanV1,
    EvidenceCitationV1,
    EvidenceHorizonV1,
)
from generals_replay_analyzer.video.subtitles import render_webvtt
from generals_replay_analyzer.video.voice import NarrationScheduler, VoiceClipV1


def test_webvtt_uses_scheduled_frame_windows_and_subtitle_copy(tmp_path: Path) -> None:
    event = CommentaryEventV1(
        event_id="50000000-0000-4000-8000-000000000001",
        start_frame=30,
        latest_end_frame=90,
        text="Spoken version.",
        subtitle_text="Readable subtitle.",
        role="analysis",
        evidence=(EvidenceCitationV1(
            evidence_public_id="30000000-0000-4000-8000-000000000001",
            tier="derived",
            frame_start=0,
            frame_end=90,
        ),),
        confidence_tier="derived",
        camera_segment_id="40000000-0000-4000-8000-000000000001",
    )
    plan = CommentaryPlanV1(
        logic_hz=30,
        replay_public_id="10000000-0000-4000-8000-000000000001",
        report_public_id="20000000-0000-4000-8000-000000000001",
        evidence_horizon=EvidenceHorizonV1(frame_end=120),
        events=(event,),
    )
    clip_path = tmp_path / "clip.wav"
    with wave.open(str(clip_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(bytes(48_000 * 2))
    clip = VoiceClipV1.from_wav(event, clip_path, provider_name="fixture", voice_name="Fixture")
    schedule = NarrationScheduler().schedule(plan, (clip,), final_frame=120)

    destination = render_webvtt(schedule, tmp_path / "render" / "commentary.vtt")

    assert destination.read_text(encoding="utf-8") == (
        "WEBVTT\n\n"
        "50000000-0000-4000-8000-000000000001\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "Readable subtitle.\n"
    )

