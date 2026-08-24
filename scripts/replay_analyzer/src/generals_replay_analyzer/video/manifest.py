"""Content-addressed immutable publication of verified replay-cast manifests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from generals_replay_analyzer.video.contracts import PublicId, Sha256, VideoContract, VideoSettingsV1
from generals_replay_analyzer.video.verify import VerifiedMediaV1


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactHashV1(VideoContract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    # TheSuperHackers @fix Leex 24/08/2026 Keep private render paths available for rehashing but out of public manifests. (#TBD)
    path: Path | None = Field(default=None, exclude=True)
    sha256: Sha256


class VideoManifestV1(VideoContract):
    schema_version: Literal["video-manifest-v1"] = "video-manifest-v1"
    render_public_id: PublicId
    verification_passed: bool
    requested_settings: VideoSettingsV1
    verified_media: VerifiedMediaV1
    artifacts: tuple[ArtifactHashV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_unique_hash_graph(self) -> VideoManifestV1:
        if len({item.name for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("manifest artifact names must be unique")
        hashes = {item.name: item.sha256 for item in self.artifacts}
        # TheSuperHackers @feature Leex 24/08/2026 Bind published ffprobe facts to the immutable final media hash graph. (#TBD)
        if hashes.get("final_video") != self.verified_media.final_video_sha256:
            raise ValueError("verified media final hash must match the final video artifact")
        if hashes.get("narration") != self.verified_media.narration_sha256:
            raise ValueError("verified media narration hash must match the narration artifact")
        subtitle_hash = self.verified_media.subtitle_sha256
        if self.requested_settings.subtitle_mode == "track":
            if subtitle_hash is None or hashes.get("subtitles") != subtitle_hash:
                raise ValueError("verified media subtitle hash must match the subtitle artifact")
        elif subtitle_hash is not None or "subtitles" in hashes:
            # TheSuperHackers @bugfix Leex 24/08/2026 Prevent burned subtitles from publishing a standalone subtitle artifact. (#TBD)
            raise ValueError("burned subtitle media must not publish a subtitle artifact")
        return self


# TheSuperHackers @feature Leex 24/08/2026 Publish render provenance only after every immutable artifact hash has been rechecked. (#TBD)
class VideoManifestPublisher:
    def publish(self, manifest: VideoManifestV1, destination: Path) -> Path:
        if type(manifest) is not VideoManifestV1:
            raise TypeError("manifest publication requires a fixed video-manifest-v1")
        if not manifest.verification_passed:
            raise ValueError("only verified media manifests may be published")
        for artifact in manifest.artifacts:
            if artifact.path is None or not artifact.path.is_file() or _hash(artifact.path) != artifact.sha256:
                raise ValueError(f"artifact hash no longer matches immutable manifest: {artifact.name}")
        target = destination.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError("manifest destination already exists and cannot be overwritten")
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                output.write(manifest.model_dump_json(indent=None, by_alias=True, exclude_none=True))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target
