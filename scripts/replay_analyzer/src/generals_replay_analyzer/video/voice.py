"""Measured voice artifacts and deterministic frame-based narration scheduling."""

from __future__ import annotations

import hashlib
import math
import os
import wave
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import uuid4

from pydantic import Field, model_validator

from generals_replay_analyzer.video.contracts import (
    CommentaryEventV1,
    CommentaryPlanV1,
    PublicId,
    Sha256,
    VideoContract,
)


class NarrationScheduleError(ValueError):
    """Voice clips cannot form the requested authoritative narration timeline."""


class VoiceProvider(Protocol):
    def render(self, event: CommentaryEventV1, destination: Path) -> VoiceClipV1: ...


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _measure_pcm(path: Path) -> tuple[int, int, int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            if source.getcomptype() != "NONE":
                raise NarrationScheduleError("voice clip must be uncompressed PCM WAV")
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            samples = source.getnframes()
    except (OSError, EOFError, wave.Error) as error:
        raise NarrationScheduleError(f"voice clip is not a readable PCM WAV: {error}") from error
    if channels != 1 or width not in {1, 2, 3, 4} or rate <= 0 or samples <= 0:
        raise NarrationScheduleError("voice clip must be non-empty mono PCM with a supported sample width")
    return channels, width, rate, samples


# TheSuperHackers @feature Leex 24/08/2026 Bind every measured PCM clip to its event, provider, voice, text, and exact bytes. (#TBD)
class VoiceClipV1(VideoContract):
    schema_version: Literal[1] = 1
    event_id: PublicId
    source_path: Path
    provider_name: str = Field(min_length=1, max_length=160)
    voice_name: str = Field(min_length=1, max_length=160)
    channels: int = Field(ge=1, le=1)
    sample_width_bytes: int = Field(ge=1, le=4)
    sample_rate: int = Field(ge=8_000, le=192_000)
    sample_count: int = Field(ge=1)
    provider_sha256: Sha256
    voice_sha256: Sha256
    text_sha256: Sha256
    clip_sha256: Sha256

    @model_validator(mode="after")
    def _require_logic_compatible_pcm(self) -> Self:
        return self

    @classmethod
    def from_wav(
        cls,
        event: CommentaryEventV1,
        path: Path,
        *,
        provider_name: str,
        voice_name: str,
    ) -> VoiceClipV1:
        channels, width, rate, samples = _measure_pcm(path)
        return cls(
            event_id=event.event_id,
            source_path=path.resolve(),
            provider_name=provider_name,
            voice_name=voice_name,
            channels=channels,
            sample_width_bytes=width,
            sample_rate=rate,
            sample_count=samples,
            provider_sha256=_sha256_bytes(provider_name.encode("utf-8")),
            voice_sha256=_sha256_bytes(voice_name.encode("utf-8")),
            text_sha256=_sha256_bytes(event.text.encode("utf-8")),
            clip_sha256=_sha256_file(path),
        )


class ScheduledNarrationEventV1(VideoContract):
    event: CommentaryEventV1
    clip: VoiceClipV1
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_bound_ordered_event(self) -> Self:
        if self.clip.event_id != self.event.event_id:
            raise ValueError("scheduled voice clip must match its commentary event")
        if self.end_frame < self.start_frame:
            raise ValueError("scheduled narration frame window must be ordered")
        return self


class NarrationScheduleV1(VideoContract):
    schema_version: Literal[1] = 1
    logic_hz: Literal[30, 60]
    final_frame: int = Field(ge=0)
    channels: int = Field(ge=1, le=1)
    sample_width_bytes: int = Field(ge=1, le=4)
    sample_rate: int = Field(ge=8_000, le=192_000)
    commentary_plan_sha256: Sha256
    events: tuple[ScheduledNarrationEventV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_gapless_authority(self) -> Self:
        prior_end = -1
        for item in self.events:
            if item.start_frame <= prior_end or item.end_frame > self.final_frame:
                raise ValueError("scheduled narration must be ordered, non-overlapping, and inside the final frame")
            if (
                item.clip.channels != self.channels
                or item.clip.sample_width_bytes != self.sample_width_bytes
                or item.clip.sample_rate != self.sample_rate
            ):
                raise ValueError("scheduled voice clips must share one PCM format")
            prior_end = item.end_frame
        return self


def _verify_clip(event: CommentaryEventV1, clip: VoiceClipV1) -> None:
    if clip.event_id != event.event_id:
        raise NarrationScheduleError("commentary requires exactly one clip for every event")
    path = clip.source_path
    if not path.is_file() or _sha256_file(path) != clip.clip_sha256:
        raise NarrationScheduleError("voice clip hash does not match its immutable artifact")
    channels, width, rate, samples = _measure_pcm(path)
    if (channels, width, rate, samples) != (
        clip.channels,
        clip.sample_width_bytes,
        clip.sample_rate,
        clip.sample_count,
    ):
        raise NarrationScheduleError("voice clip PCM metadata does not match its immutable artifact")
    if clip.text_sha256 != _sha256_bytes(event.text.encode("utf-8")):
        raise NarrationScheduleError("voice clip text hash does not match its commentary event")


# TheSuperHackers @feature Leex 24/08/2026 Schedule measured narration only inside each accepted evidence window. (#TBD)
class NarrationScheduler:
    def schedule(
        self,
        plan: CommentaryPlanV1,
        clips: tuple[VoiceClipV1, ...],
        final_frame: int,
    ) -> NarrationScheduleV1:
        if type(plan) is not CommentaryPlanV1 or type(final_frame) is not int or final_frame < 0:
            raise TypeError("narration scheduling requires a fixed commentary plan and non-negative final frame")
        if final_frame != plan.evidence_horizon.frame_end:
            raise NarrationScheduleError("final frame must equal the accepted commentary evidence horizon")
        clip_by_event = {clip.event_id: clip for clip in clips}
        if len(clip_by_event) != len(clips) or set(clip_by_event) != {event.event_id for event in plan.events}:
            raise NarrationScheduleError("commentary requires exactly one clip for every event")

        scheduled: list[ScheduledNarrationEventV1] = []
        prior_end = -1
        pcm_format: tuple[int, int, int] | None = None
        for event in plan.events:
            clip = clip_by_event[event.event_id]
            _verify_clip(event, clip)
            current_format = (clip.channels, clip.sample_width_bytes, clip.sample_rate)
            if pcm_format is None:
                pcm_format = current_format
            elif current_format != pcm_format:
                raise NarrationScheduleError("all voice clips must share one PCM format")
            duration_frames = math.ceil(clip.sample_count * plan.logic_hz / clip.sample_rate)
            start_frame = max(event.start_frame, prior_end + 1)
            end_frame = start_frame + duration_frames - 1
            if end_frame > event.latest_end_frame:
                raise NarrationScheduleError(
                    f"event {event.event_id} cannot fit before latest_end_frame {event.latest_end_frame}"
                )
            scheduled.append(
                ScheduledNarrationEventV1(event=event, clip=clip, start_frame=start_frame, end_frame=end_frame)
            )
            prior_end = end_frame
        assert pcm_format is not None
        if pcm_format[2] % plan.logic_hz:
            raise NarrationScheduleError("voice clip sample rate must divide exactly into the logic timeline")
        return NarrationScheduleV1(
            logic_hz=plan.logic_hz,
            final_frame=final_frame,
            channels=pcm_format[0],
            sample_width_bytes=pcm_format[1],
            sample_rate=pcm_format[2],
            commentary_plan_sha256=_sha256_bytes(plan.canonical_json().encode("utf-8")),
            events=tuple(scheduled),
        )


def _temporary_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")


# TheSuperHackers @feature Leex 24/08/2026 Assemble one exact-duration mono PCM narration artifact without truncating clips. (#TBD)
def render_narration_wav(schedule: NarrationScheduleV1, destination: Path) -> Path:
    if type(schedule) is not NarrationScheduleV1:
        raise TypeError("WAV rendering requires a validated narration schedule")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)
    if schedule.sample_rate % schedule.logic_hz:
        raise NarrationScheduleError("voice sample rate must divide exactly into the logic timeline")
    samples_per_logic_frame = schedule.sample_rate // schedule.logic_hz
    total_samples = (schedule.final_frame + 1) * samples_per_logic_frame
    cursor = 0
    try:
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(schedule.channels)
            output.setsampwidth(schedule.sample_width_bytes)
            output.setframerate(schedule.sample_rate)
            for item in schedule.events:
                _verify_clip(item.event, item.clip)
                start_sample = item.start_frame * samples_per_logic_frame
                if start_sample < cursor:
                    raise NarrationScheduleError("scheduled clips overlap at PCM sample precision")
                output.writeframesraw(bytes((start_sample - cursor) * schedule.sample_width_bytes))
                with wave.open(str(item.clip.source_path), "rb") as source:
                    frames = source.readframes(item.clip.sample_count)
                output.writeframesraw(frames)
                cursor = start_sample + item.clip.sample_count
            if cursor > total_samples:
                raise NarrationScheduleError("narration exceeds the authoritative final frame")
            output.writeframesraw(bytes((total_samples - cursor) * schedule.sample_width_bytes))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
