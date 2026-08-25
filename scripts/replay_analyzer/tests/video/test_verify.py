"""Media verification must reject unproven replay cast output."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from generals_replay_analyzer.video.contracts import VideoSettingsV1
from generals_replay_analyzer.video.verify import (
    MediaVerificationError,
    MediaVerifier,
    VerificationLandmarkV1,
)


def _wav(path: Path, *, silent: bool = False, frames: int = 2_000) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(30_000)
        output.writeframes((b"\0\0" if silent else b"\x10\0") * frames)
    return path


def _probe(
    *,
    codec: str = "h264",
    pix_fmt: str = "yuv420p",
    fps: str = "30/1",
    duration: str = "2.0",
    sample_rate: str = "30000",
    color_range: str | None = "tv",
    color_space: str | None = "bt709",
    color_transfer: str | None = "bt709",
    color_primaries: str | None = "bt709",
) -> str:
    return json.dumps(
        {
            "format": {"duration": duration},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": codec,
                    "pix_fmt": pix_fmt,
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": fps,
                    "r_frame_rate": fps,
                    "nb_frames": "60",
                    "duration": duration,
                    "color_range": color_range,
                    "color_space": color_space,
                    "color_transfer": color_transfer,
                    "color_primaries": color_primaries,
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": sample_rate,
                    "channels": 1,
                    "duration": duration,
                },
                {"codec_type": "subtitle", "codec_name": "webvtt", "duration": duration},
            ],
        }
    )


def _verify(tmp_path: Path, payload: str, *, silent: bool = False, final_silent: bool = False) -> object:
    ffprobe = (tmp_path / "tools" / "ffprobe.exe").resolve()
    ffmpeg = (tmp_path / "tools" / "ffmpeg.exe").resolve()
    ffprobe.parent.mkdir(exist_ok=True)
    ffprobe.write_bytes(b"tool")
    ffmpeg.write_bytes(b"tool")
    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    subtitles = tmp_path / "captions.vtt"
    subtitles.write_text("WEBVTT\n", encoding="utf-8")
    captured: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        captured.append(argv)
        return payload

    def decode(argv: tuple[str, ...]) -> None:
        _wav(Path(argv[-1]), silent=final_silent, frames=60_000)

    result = MediaVerifier(ffprobe, ffmpeg, probe_runner=runner, audio_decode_runner=decode).verify(
        final,
        _wav(tmp_path / "narration.wav", silent=silent),
        subtitles,
        settings=VideoSettingsV1(width=640, height=360, fps=30, subtitle_mode="track"),
        final_frame=59,
        logic_frames_per_second=30,
        landmarks=(VerificationLandmarkV1(frame=30, evidence_public_ids=("10000000-0000-4000-8000-000000000001",)),),
    )
    assert captured[0][0] == str(ffprobe)
    assert captured[0][-1] == str(final.resolve())
    return result


def test_verifier_accepts_probed_authoritative_media_and_records_landmarks(tmp_path: Path) -> None:
    result = _verify(tmp_path, _probe())

    assert result.passed is True
    assert result.observed.codec_name == "h264"
    assert result.landmarks[0].frame == 30


def test_verifier_ignores_documented_ffprobe_metadata_extras_but_requires_authoritative_fields(tmp_path: Path) -> None:
    payload = json.loads(_probe())
    payload["format"].update({"filename": "C:\\private\\final.mp4", "format_name": "mov,mp4", "tags": {"encoder": "Lavf60.3"}})
    payload["streams"][0].update({"index": 0, "codec_long_name": "H.264", "profile": "High", "time_base": "1/15360", "disposition": {"default": 1}, "tags": {"language": "und"}})
    payload["streams"][1].update({"index": 1, "codec_long_name": "AAC", "sample_fmt": "fltp", "channel_layout": "mono", "time_base": "1/48000"})
    payload["streams"][2].update({"index": 2, "codec_long_name": "MOV text", "codec_tag_string": "tx3g", "time_base": "1/1000000"})
    result = _verify(tmp_path, json.dumps(payload))
    assert result.observed.frame_count == 60

    missing = json.loads(_probe())
    del missing["streams"][0]["nb_frames"]
    with pytest.raises(MediaVerificationError, match="incomplete"):
        _verify(tmp_path, json.dumps(missing))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"codec": "mpeg4"}, "H.264"),
        ({"pix_fmt": "yuv444p"}, "yuv420p"),
        ({"fps": "60/1"}, "FPS"),
        ({"duration": "1.0"}, "duration"),
        ({"sample_rate": "48000"}, "sample rate"),
    ],
)
def test_verifier_rejects_wrong_media_properties(tmp_path: Path, kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(MediaVerificationError, match=message):
        _verify(tmp_path, _probe(**kwargs))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"color_range": None}, "color_range"),
        ({"color_range": "pc"}, "color_range"),
        ({"color_space": None}, "color_space"),
        ({"color_space": "smpte170m"}, "color_space"),
        ({"color_transfer": None}, "color_transfer"),
        ({"color_transfer": "smpte170m"}, "color_transfer"),
        ({"color_primaries": None}, "color_primaries"),
        ({"color_primaries": "smpte170m"}, "color_primaries"),
    ],
)
def test_verifier_rejects_missing_or_non_bt709_color_metadata(
    tmp_path: Path, kwargs: dict[str, str | None], message: str
) -> None:
    with pytest.raises(MediaVerificationError, match=message):
        _verify(tmp_path, _probe(**kwargs))


def test_verifier_rejects_silent_narration_and_unknown_ffprobe_fields(tmp_path: Path) -> None:
    with pytest.raises(MediaVerificationError, match="silent"):
        _verify(tmp_path, _probe(), silent=True)

    with pytest.raises(MediaVerificationError, match="final audio source is silent"):
        _verify(tmp_path, _probe(), final_silent=True)

    payload = json.loads(_probe())
    payload["unexpected"] = True
    with pytest.raises(MediaVerificationError, match="ffprobe"):
        _verify(tmp_path, json.dumps(payload))


def test_verifier_rejects_out_of_horizon_landmark(tmp_path: Path) -> None:
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe.write_bytes(b"tool")
    ffmpeg.write_bytes(b"tool")
    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    with pytest.raises(MediaVerificationError, match="landmark"):
        MediaVerifier(ffprobe, ffmpeg, probe_runner=lambda _: _probe(), audio_decode_runner=lambda argv: _wav(Path(argv[-1]), frames=60_000)).verify(
            final,
            _wav(tmp_path / "narration.wav"),
            None,
            settings=VideoSettingsV1(width=640, height=360, fps=30, subtitle_mode="burned"),
            final_frame=59,
            logic_frames_per_second=30,
            landmarks=(VerificationLandmarkV1(frame=60, evidence_public_ids=("10000000-0000-4000-8000-000000000001",)),),
        )
