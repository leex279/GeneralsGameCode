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


def _wav(path: Path, *, silent: bool = False) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(30_000)
        output.writeframes((b"\0\0" if silent else b"\x10\0") * 2_000)
    return path


def _probe(*, codec: str = "h264", pix_fmt: str = "yuv420p", fps: str = "30/1", duration: str = "2.0") -> str:
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
                },
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "30000", "channels": 1, "duration": duration},
                {"codec_type": "subtitle", "codec_name": "webvtt", "duration": duration},
            ],
        }
    )


def _verify(tmp_path: Path, payload: str, *, silent: bool = False) -> object:
    ffprobe = (tmp_path / "tools" / "ffprobe.exe").resolve()
    ffprobe.parent.mkdir(exist_ok=True)
    ffprobe.write_bytes(b"tool")
    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    subtitles = tmp_path / "captions.vtt"
    subtitles.write_text("WEBVTT\n", encoding="utf-8")
    captured: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        captured.append(argv)
        return payload

    result = MediaVerifier(ffprobe, probe_runner=runner).verify(
        final,
        _wav(tmp_path / "narration.wav", silent=silent),
        subtitles,
        settings=VideoSettingsV1(width=640, height=360, fps=30, subtitle_mode="track"),
        final_frame=59,
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


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"codec": "mpeg4"}, "H.264"),
        ({"pix_fmt": "yuv444p"}, "yuv420p"),
        ({"fps": "60/1"}, "FPS"),
        ({"duration": "1.0"}, "duration"),
    ],
)
def test_verifier_rejects_wrong_media_properties(tmp_path: Path, kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(MediaVerificationError, match=message):
        _verify(tmp_path, _probe(**kwargs))


def test_verifier_rejects_silent_narration_and_unknown_ffprobe_fields(tmp_path: Path) -> None:
    with pytest.raises(MediaVerificationError, match="silent"):
        _verify(tmp_path, _probe(), silent=True)

    payload = json.loads(_probe())
    payload["unexpected"] = True
    with pytest.raises(MediaVerificationError, match="ffprobe"):
        _verify(tmp_path, json.dumps(payload))


def test_verifier_rejects_out_of_horizon_landmark(tmp_path: Path) -> None:
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffprobe.write_bytes(b"tool")
    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    with pytest.raises(MediaVerificationError, match="landmark"):
        MediaVerifier(ffprobe, probe_runner=lambda _: _probe()).verify(
            final,
            _wav(tmp_path / "narration.wav"),
            None,
            settings=VideoSettingsV1(width=640, height=360, fps=30, subtitle_mode="burned"),
            final_frame=59,
            landmarks=(VerificationLandmarkV1(frame=60, evidence_public_ids=("10000000-0000-4000-8000-000000000001",)),),
        )
