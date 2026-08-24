"""Content-addressed immutable publication of verified replay-cast manifests."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
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
    # TheSuperHackers @feature Leex 24/08/2026 Re-open durable worker output as a validated manifest for resume-safe orchestration. (#TBD)
    def load(self, source: Path, artifact_paths: Mapping[str, Path]) -> VideoManifestV1:
        """Load a public manifest only after rebinding and rehashing every private artifact."""
        if source.is_symlink():
            raise ValueError("published video manifest must be an ordinary file")
        target = source.resolve()
        if not target.is_file():
            raise ValueError("published video manifest must be an ordinary file")
        try:
            manifest = VideoManifestV1.model_validate_json(target.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise ValueError("published video manifest is invalid") from error
        if not manifest.verification_passed:
            raise ValueError("published video manifest is not verified")
        expected_names = {artifact.name for artifact in manifest.artifacts}
        if set(artifact_paths) != expected_names:
            raise ValueError("artifact bindings must exactly match the published manifest")
        rebound: list[ArtifactHashV1] = []
        for artifact in manifest.artifacts:
            candidate = artifact_paths[artifact.name]
            if candidate.is_symlink():
                raise ValueError(f"artifact must be an ordinary file: {artifact.name}")
            resolved = candidate.resolve()
            if not resolved.is_file() or _hash(resolved) != artifact.sha256:
                raise ValueError(f"artifact hash no longer matches published manifest: {artifact.name}")
            rebound.append(artifact.model_copy(update={"path": resolved}))
        return manifest.model_copy(update={"artifacts": tuple(rebound)})

    def publish(self, manifest: VideoManifestV1, destination: Path) -> Path:
        if type(manifest) is not VideoManifestV1:
            raise TypeError("manifest publication requires a fixed video-manifest-v1")
        if not manifest.verification_passed:
            raise ValueError("only verified media manifests may be published")
        for artifact in manifest.artifacts:
            # TheSuperHackers @fix Leex 24/08/2026 Keep publication and resume on the same ordinary-file trust boundary. (#TBD)
            if artifact.path is None or artifact.path.is_symlink() or not artifact.path.is_file():
                raise ValueError(f"artifact must be an ordinary file: {artifact.name}")
            if _hash(artifact.path) != artifact.sha256:
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
