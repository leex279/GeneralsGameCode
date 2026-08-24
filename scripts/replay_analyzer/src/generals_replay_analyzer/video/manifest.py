"""Content-addressed immutable publication of verified replay-cast manifests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from generals_replay_analyzer.video.contracts import PublicId, Sha256, VideoContract


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactHashV1(VideoContract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    path: Path
    sha256: Sha256


class VideoManifestV1(VideoContract):
    schema_version: Literal["video-manifest-v1"] = "video-manifest-v1"
    render_public_id: PublicId
    verification_passed: bool
    artifacts: tuple[ArtifactHashV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_unique_hash_graph(self) -> VideoManifestV1:
        if len({item.name for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("manifest artifact names must be unique")
        return self


# TheSuperHackers @feature Leex 24/08/2026 Publish render provenance only after every immutable artifact hash has been rechecked. (#TBD)
class VideoManifestPublisher:
    def publish(self, manifest: VideoManifestV1, destination: Path) -> Path:
        if type(manifest) is not VideoManifestV1:
            raise TypeError("manifest publication requires a fixed video-manifest-v1")
        if not manifest.verification_passed:
            raise ValueError("only verified media manifests may be published")
        for artifact in manifest.artifacts:
            if not artifact.path.is_file() or _hash(artifact.path) != artifact.sha256:
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
