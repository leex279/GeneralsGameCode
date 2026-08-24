"""Immutable manifest publication tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from generals_replay_analyzer.video.contracts import VideoSettingsV1
from generals_replay_analyzer.video.manifest import ArtifactHashV1, VideoManifestPublisher, VideoManifestV1
from generals_replay_analyzer.video.verify import ObservedVideoV1, VerifiedMediaV1


def _artifact(path: Path, name: str) -> ArtifactHashV1:
    return ArtifactHashV1(name=name, path=path.resolve(), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _verification(final: Path, narration: Path, subtitles: Path | None = None) -> VerifiedMediaV1:
    return VerifiedMediaV1(
        final_video_sha256=hashlib.sha256(final.read_bytes()).hexdigest(),
        narration_sha256=hashlib.sha256(narration.read_bytes()).hexdigest(),
        subtitle_sha256=None if subtitles is None else hashlib.sha256(subtitles.read_bytes()).hexdigest(),
        expected_duration_seconds=2.0,
        observed=ObservedVideoV1(
            codec_name="h264",
            pixel_format="yuv420p",
            width=1280,
            height=720,
            fps_numerator=30,
            fps_denominator=1,
            frame_count=60,
            duration_seconds=2.0,
            audio_codec_name="aac",
            audio_sample_rate=48_000,
            audio_channels=1,
            subtitle_codec_name="mov_text" if subtitles is not None else None,
        ),
        landmarks=(),
    )


def test_manifest_publishes_requested_and_observed_verified_media_bound_to_artifacts(tmp_path: Path) -> None:
    final = tmp_path / "final.mp4"
    narration = tmp_path / "narration.wav"
    subtitles = tmp_path / "subtitles.vtt"
    final.write_bytes(b"final")
    narration.write_bytes(b"narration")
    subtitles.write_bytes(b"WEBVTT")
    manifest = VideoManifestV1(
        render_public_id="10000000-0000-4000-8000-000000000001",
        verification_passed=True,
        requested_settings=VideoSettingsV1(width=1280, height=720, fps=30, subtitle_mode="track"),
        verified_media=_verification(final, narration, subtitles),
        artifacts=(_artifact(final, "final_video"), _artifact(narration, "narration"), _artifact(subtitles, "subtitles")),
    )
    destination = tmp_path / "video-manifest-v1.json"

    published = VideoManifestPublisher().publish(manifest, destination)

    document = json.loads(published.read_text(encoding="utf-8"))
    assert document["requested_settings"] == {"schema_version": 1, "width": 1280, "height": 720, "fps": 30, "subtitle_mode": "track"}
    assert document["verified_media"]["observed"]["frame_count"] == 60
    assert document["verified_media"]["final_video_sha256"] == hashlib.sha256(b"final").hexdigest()


def test_manifest_rejects_verified_media_that_does_not_match_final_artifact(tmp_path: Path) -> None:
    final = tmp_path / "final.mp4"
    narration = tmp_path / "narration.wav"
    subtitles = tmp_path / "subtitles.vtt"
    final.write_bytes(b"final")
    narration.write_bytes(b"narration")
    subtitles.write_bytes(b"WEBVTT")

    with pytest.raises(ValueError, match="verified media final hash"):
        VideoManifestV1(
            render_public_id="10000000-0000-4000-8000-000000000001",
            verification_passed=True,
            requested_settings=VideoSettingsV1(width=1280, height=720, fps=30, subtitle_mode="track"),
            verified_media=_verification(final, narration, subtitles).model_copy(update={"final_video_sha256": "a" * 64}),
            artifacts=(_artifact(final, "final_video"), _artifact(narration, "narration"), _artifact(subtitles, "subtitles")),
        )


def test_manifest_burned_subtitles_publish_no_subtitle_artifact(tmp_path: Path) -> None:
    final = tmp_path / "final.mp4"
    narration = tmp_path / "narration.wav"
    final.write_bytes(b"final")
    narration.write_bytes(b"narration")
    manifest = VideoManifestV1(
        render_public_id="10000000-0000-4000-8000-000000000001",
        verification_passed=True,
        requested_settings=VideoSettingsV1(width=1280, height=720, fps=30, subtitle_mode="burned"),
        verified_media=_verification(final, narration),
        artifacts=(_artifact(final, "final_video"), _artifact(narration, "narration")),
    )

    destination = VideoManifestPublisher().publish(manifest, tmp_path / "video-manifest-v1.json")

    document = json.loads(destination.read_text(encoding="utf-8"))
    assert all(artifact["name"] != "subtitles" for artifact in document["artifacts"])


def test_manifest_rejects_burned_subtitle_artifact(tmp_path: Path) -> None:
    final = tmp_path / "final.mp4"
    narration = tmp_path / "narration.wav"
    subtitles = tmp_path / "subtitles.vtt"
    final.write_bytes(b"final")
    narration.write_bytes(b"narration")
    subtitles.write_bytes(b"WEBVTT")

    with pytest.raises(ValueError, match="burned subtitle media"):
        VideoManifestV1(
            render_public_id="10000000-0000-4000-8000-000000000001",
            verification_passed=True,
            requested_settings=VideoSettingsV1(width=1280, height=720, fps=30, subtitle_mode="burned"),
            verified_media=_verification(final, narration),
            artifacts=(_artifact(final, "final_video"), _artifact(narration, "narration"), _artifact(subtitles, "subtitles")),
        )


def test_publisher_atomically_publishes_only_verified_complete_hash_graph(tmp_path: Path) -> None:
    replay = tmp_path / "replay.rep"
    final = tmp_path / "final.mp4"
    narration = tmp_path / "narration.wav"
    subtitles = tmp_path / "subtitles.vtt"
    replay.write_bytes(b"replay")
    final.write_bytes(b"final")
    narration.write_bytes(b"narration")
    subtitles.write_bytes(b"WEBVTT")
    manifest = VideoManifestV1(
        render_public_id="10000000-0000-4000-8000-000000000001",
        verification_passed=True,
        requested_settings=VideoSettingsV1(width=1280, height=720, fps=30, subtitle_mode="track"),
        verified_media=_verification(final, narration, subtitles),
        artifacts=(
            _artifact(final, "final_video"), _artifact(narration, "narration"), _artifact(subtitles, "subtitles"),
            _artifact(replay, "replay"),
        ),
    )
    destination = tmp_path / "published" / "video-manifest-v1.json"

    published = VideoManifestPublisher().publish(manifest, destination)

    assert published == destination.resolve()
    document = json.loads(published.read_text(encoding="utf-8"))
    assert all("path" not in artifact for artifact in document["artifacts"])
    assert [(artifact["name"], artifact["sha256"]) for artifact in document["artifacts"]] == [
        (artifact.name, artifact.sha256) for artifact in manifest.artifacts
    ]


def test_publisher_rejects_unverified_tampered_or_existing_publication(tmp_path: Path) -> None:
    final = tmp_path / "final.mp4"
    narration = tmp_path / "narration.wav"
    subtitles = tmp_path / "subtitles.vtt"
    final.write_bytes(b"final")
    narration.write_bytes(b"narration")
    subtitles.write_bytes(b"WEBVTT")
    artifact = _artifact(final, "final_video")
    narration_artifact = _artifact(narration, "narration")
    subtitle_artifact = _artifact(subtitles, "subtitles")
    requested_settings = VideoSettingsV1(width=1280, height=720, fps=30, subtitle_mode="track")
    verification = _verification(final, narration, subtitles)
    destination = tmp_path / "video-manifest-v1.json"
    with pytest.raises(ValueError, match="verified"):
        VideoManifestPublisher().publish(
            VideoManifestV1(
                    render_public_id="10000000-0000-4000-8000-000000000001",
                    verification_passed=False,
                    requested_settings=requested_settings,
                    verified_media=verification,
                    artifacts=(artifact, narration_artifact, subtitle_artifact),
            ),
            destination,
        )

    final.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        VideoManifestPublisher().publish(
            VideoManifestV1(
                render_public_id="10000000-0000-4000-8000-000000000001",
                verification_passed=True,
                requested_settings=requested_settings,
                verified_media=verification,
                artifacts=(artifact, narration_artifact, subtitle_artifact),
            ),
            destination,
        )
