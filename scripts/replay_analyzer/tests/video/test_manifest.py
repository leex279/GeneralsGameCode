"""Immutable manifest publication tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from generals_replay_analyzer.video.manifest import ArtifactHashV1, VideoManifestPublisher, VideoManifestV1


def _artifact(path: Path, name: str) -> ArtifactHashV1:
    return ArtifactHashV1(name=name, path=path.resolve(), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_publisher_atomically_publishes_only_verified_complete_hash_graph(tmp_path: Path) -> None:
    replay = tmp_path / "replay.rep"
    final = tmp_path / "final.mp4"
    replay.write_bytes(b"replay")
    final.write_bytes(b"final")
    manifest = VideoManifestV1(
        render_public_id="10000000-0000-4000-8000-000000000001",
        verification_passed=True,
        artifacts=(_artifact(final, "final_video"), _artifact(replay, "replay")),
    )
    destination = tmp_path / "published" / "video-manifest-v1.json"

    published = VideoManifestPublisher().publish(manifest, destination)

    assert published == destination.resolve()
    assert VideoManifestV1.model_validate_json(published.read_text(encoding="utf-8")) == manifest


def test_publisher_rejects_unverified_tampered_or_existing_publication(tmp_path: Path) -> None:
    final = tmp_path / "final.mp4"
    final.write_bytes(b"final")
    artifact = _artifact(final, "final_video")
    destination = tmp_path / "video-manifest-v1.json"
    with pytest.raises(ValueError, match="verified"):
        VideoManifestPublisher().publish(
            VideoManifestV1(
                render_public_id="10000000-0000-4000-8000-000000000001",
                verification_passed=False,
                artifacts=(artifact,),
            ),
            destination,
        )

    final.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        VideoManifestPublisher().publish(
            VideoManifestV1(
                render_public_id="10000000-0000-4000-8000-000000000001",
                verification_passed=True,
                artifacts=(artifact,),
            ),
            destination,
        )
