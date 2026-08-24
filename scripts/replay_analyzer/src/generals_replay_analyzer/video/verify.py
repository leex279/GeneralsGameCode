"""Closed ffprobe and source-audio verification for finished replay casts."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from generals_replay_analyzer.video.contracts import PublicId, Sha256, VideoContract, VideoSettingsV1

ProbeRunner = Callable[[tuple[str, ...]], str]


class MediaVerificationError(ValueError):
    """A rendered artifact cannot be published as a completed replay cast."""


class FfprobeFormatV1(VideoContract):
    duration: str


class FfprobeStreamV1(VideoContract):
    codec_type: Literal["video", "audio", "subtitle"]
    codec_name: str = Field(min_length=1)
    pix_fmt: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    avg_frame_rate: str | None = None
    r_frame_rate: str | None = None
    nb_frames: str | None = None
    sample_rate: str | None = None
    channels: int | None = Field(default=None, ge=1)
    duration: str | None = None


class FfprobeDocumentV1(VideoContract):
    format: FfprobeFormatV1
    streams: list[FfprobeStreamV1] = Field(min_length=1)


class ObservedVideoV1(VideoContract):
    codec_name: str
    pixel_format: str
    width: int
    height: int
    fps_numerator: int = Field(ge=1)
    fps_denominator: int = Field(ge=1)
    frame_count: int = Field(ge=1)
    duration_seconds: float = Field(gt=0.0)
    audio_codec_name: str
    audio_sample_rate: int = Field(ge=1)
    audio_channels: int = Field(ge=1)
    subtitle_codec_name: str | None = None


class VerificationLandmarkV1(VideoContract):
    frame: int = Field(ge=0)
    evidence_public_ids: tuple[PublicId, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _deduplicate_evidence(self) -> Self:
        object.__setattr__(self, "evidence_public_ids", tuple(sorted(set(self.evidence_public_ids))))
        return self


class VerifiedMediaV1(VideoContract):
    schema_version: Literal[1] = 1
    passed: Literal[True] = True
    final_video_sha256: Sha256
    narration_sha256: Sha256
    subtitle_sha256: Sha256 | None = None
    expected_duration_seconds: float = Field(gt=0.0)
    observed: ObservedVideoV1
    landmarks: tuple[VerificationLandmarkV1, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: str, label: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise MediaVerificationError(f"ffprobe {label} is not numeric") from error
    if not math.isfinite(result) or result <= 0.0:
        raise MediaVerificationError(f"ffprobe {label} must be finite and positive")
    return result


def _ratio(value: str | None, label: str) -> tuple[int, int]:
    if value is None or value.count("/") != 1:
        raise MediaVerificationError(f"ffprobe {label} is missing")
    numerator, denominator = value.split("/", 1)
    try:
        parsed = int(numerator), int(denominator)
    except ValueError as error:
        raise MediaVerificationError(f"ffprobe {label} is not a rational rate") from error
    if parsed[0] <= 0 or parsed[1] <= 0:
        raise MediaVerificationError(f"ffprobe {label} must be positive")
    return parsed


def _default_probe(argv: tuple[str, ...]) -> str:
    completed = subprocess.run(argv, shell=False, check=False, capture_output=True, text=True, timeout=120)
    if completed.returncode != 0:
        raise MediaVerificationError(f"ffprobe failed with exit code {completed.returncode}")
    return completed.stdout


def _require_pcm_signal(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as source:
            if source.getcomptype() != "NONE" or source.getnchannels() != 1 or source.getnframes() <= 0:
                raise MediaVerificationError("narration must be a non-empty mono PCM WAV")
            frames = source.readframes(source.getnframes())
    except (OSError, EOFError, wave.Error) as error:
        raise MediaVerificationError(f"narration WAV cannot be read: {error}") from error
    if not any(value != 0 for value in frames):
        raise MediaVerificationError("narration source is silent")


# TheSuperHackers @feature Leex 24/08/2026 Verify final replay media against closed ffprobe facts before immutable publication. (#TBD)
class MediaVerifier:
    def __init__(self, ffprobe_executable: Path, *, probe_runner: ProbeRunner = _default_probe) -> None:
        executable = ffprobe_executable.resolve()
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("ffprobe executable must be an existing absolute configured path")
        self._ffprobe_executable = executable
        self._probe_runner = probe_runner

    def verify(
        self,
        final_video: Path,
        narration_wav: Path,
        subtitles: Path | None,
        *,
        settings: VideoSettingsV1,
        final_frame: int,
        landmarks: tuple[VerificationLandmarkV1, ...],
    ) -> VerifiedMediaV1:
        if type(settings) is not VideoSettingsV1 or type(final_frame) is not int or final_frame < 0:
            raise TypeError("media verification requires fixed settings and a non-negative final frame")
        final = final_video.resolve()
        narration = narration_wav.resolve()
        if not final.is_file() or not narration.is_file():
            raise MediaVerificationError("final video and narration source must exist")
        subtitle = subtitles.resolve() if subtitles is not None else None
        if settings.subtitle_mode == "track" and (subtitle is None or not subtitle.is_file()):
            raise MediaVerificationError("subtitle track source must exist")
        if any(item.frame > final_frame for item in landmarks):
            raise MediaVerificationError("verification landmark exceeds authoritative evidence horizon")
        _require_pcm_signal(narration)
        document = self._probe(final)
        observed = self._observed(document)
        expected_duration = (final_frame + 1) / 30.0
        self._validate(observed, settings, expected_duration, final_frame)
        return VerifiedMediaV1(
            final_video_sha256=_sha256(final),
            narration_sha256=_sha256(narration),
            subtitle_sha256=_sha256(subtitle) if subtitle is not None else None,
            expected_duration_seconds=expected_duration,
            observed=observed,
            landmarks=landmarks,
        )

    def _probe(self, final: Path) -> FfprobeDocumentV1:
        argv = (str(self._ffprobe_executable), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(final))
        try:
            raw = self._probe_runner(argv)
            payload = json.loads(raw)
            return FfprobeDocumentV1.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            if isinstance(error, MediaVerificationError):
                raise
            raise MediaVerificationError(f"ffprobe returned invalid closed JSON: {error}") from error

    @staticmethod
    def _observed(document: FfprobeDocumentV1) -> ObservedVideoV1:
        video = next((item for item in document.streams if item.codec_type == "video"), None)
        audio = next((item for item in document.streams if item.codec_type == "audio"), None)
        subtitle = next((item for item in document.streams if item.codec_type == "subtitle"), None)
        if video is None or audio is None:
            raise MediaVerificationError("ffprobe must report one video and one audio stream")
        numerator, denominator = _ratio(video.avg_frame_rate or video.r_frame_rate, "video frame rate")
        if video.width is None or video.height is None or video.pix_fmt is None or video.nb_frames is None:
            raise MediaVerificationError("ffprobe video stream is incomplete")
        try:
            frames = int(video.nb_frames)
            sample_rate = int(audio.sample_rate or "")
        except ValueError as error:
            raise MediaVerificationError("ffprobe frame or audio sample count is invalid") from error
        if frames <= 0:
            raise MediaVerificationError("ffprobe video frame count must be positive")
        return ObservedVideoV1(
            codec_name=video.codec_name,
            pixel_format=video.pix_fmt,
            width=video.width,
            height=video.height,
            fps_numerator=numerator,
            fps_denominator=denominator,
            frame_count=frames,
            duration_seconds=_number(document.format.duration, "format duration"),
            audio_codec_name=audio.codec_name,
            audio_sample_rate=sample_rate,
            audio_channels=audio.channels or 0,
            subtitle_codec_name=subtitle.codec_name if subtitle is not None else None,
        )

    @staticmethod
    def _validate(observed: ObservedVideoV1, settings: VideoSettingsV1, expected_duration: float, final_frame: int) -> None:
        if observed.codec_name != "h264":
            raise MediaVerificationError("final video must use H.264")
        if observed.pixel_format != "yuv420p":
            raise MediaVerificationError("final video must use yuv420p")
        if (observed.width, observed.height) != (settings.width, settings.height):
            raise MediaVerificationError("final video dimensions differ from fixed settings")
        if observed.fps_numerator != settings.fps * observed.fps_denominator:
            raise MediaVerificationError("final video FPS differs from fixed settings")
        expected_frames = (final_frame + 1) * settings.fps // 30
        if observed.frame_count != expected_frames:
            raise MediaVerificationError("final video frame count differs from authoritative duration")
        tolerance = 1.0 / settings.fps
        if abs(observed.duration_seconds - expected_duration) > tolerance:
            raise MediaVerificationError("final video duration differs from authoritative duration")
        if observed.audio_codec_name != "aac" or observed.audio_channels != 1:
            raise MediaVerificationError("final video must contain mono AAC narration")
        if settings.subtitle_mode == "track" and observed.subtitle_codec_name is None:
            raise MediaVerificationError("final video is missing its subtitle track")
