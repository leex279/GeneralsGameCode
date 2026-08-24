"""Durable, worker-owned commented replay-video job planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from generals_replay_analyzer.importing.jobs import JobSpec
from generals_replay_analyzer.importing.stages import (
    RENDER_VIDEO,
    RENDER_VIDEO_VERSION,
    content_key,
)

EvidenceHorizon = Literal["complete", "partial"]


class VideoJobRequestError(ValueError):
    """A cast request exceeds the evidence authority available to it."""


class VideoJobCoordinator(Protocol):
    """The closed durable operations the Web-facing planner is allowed to request."""

    def create_job(self, spec: JobSpec) -> object: ...

    def add_dependency(self, job_public_id: str, depends_on_public_id: str) -> None: ...


class VideoRenderer(Protocol):
    def render(self, request: object) -> object: ...


class VideoRenderStageHandler:
    """Bridge a frozen durable job to the private renderer only inside a worker."""

    def __init__(self, *, request_factory: Callable[[Mapping[str, object]], object], renderer: VideoRenderer) -> None:
        self._request_factory = request_factory
        self._renderer = renderer

    # TheSuperHackers @feature Leex 24/08/2026 Publish only verified content identities from the worker renderer. (#TBD)
    def __call__(self, context: object) -> Mapping[str, object]:
        raw_input = getattr(context, "input_json", None)
        if not isinstance(raw_input, Mapping):
            raise VideoJobRequestError("video job input is invalid")
        request = self._request_factory(raw_input)
        result = self._renderer.render(request)
        fields = ("run_public_id", "final_video_sha256", "manifest_public_id", "manifest_sha256")
        values = {field: getattr(result, field, None) for field in fields}
        if (
            not all(type(value) is str and value for value in values.values())
            or len(str(values["final_video_sha256"])) != 64
            or len(str(values["manifest_sha256"])) != 64
        ):
            raise RuntimeError("renderer did not return verified public media identities")
        return {"schema_version": "video-stage-output-v1", **values}


@dataclass(frozen=True, slots=True)
class VideoJobPlanner:
    """Freeze only public report identities into a replay-video worker job."""

    clock: Callable[[], datetime]

    # TheSuperHackers @feature Leex 24/08/2026 Keep commented cast requests durable and evidence-bound. (#TBD)
    def plan(
        self,
        *,
        replay_public_id: str,
        report_public_id: str,
        report_job_public_id: str,
        evidence_horizon: EvidenceHorizon,
        diagnostic_preview: bool,
    ) -> JobSpec:
        if any(str(UUID(value)) != value for value in (replay_public_id, report_public_id, report_job_public_id)):
            raise VideoJobRequestError("replay, report, and report job identities must be canonical public IDs")
        if evidence_horizon not in {"complete", "partial"}:
            raise VideoJobRequestError("evidence horizon is invalid")
        if type(diagnostic_preview) is not bool:
            raise VideoJobRequestError("diagnostic preview must be a boolean")
        if evidence_horizon == "partial" and not diagnostic_preview:
            raise VideoJobRequestError("partial evidence requires an explicit diagnostic preview")
        identity = {
            "replay_public_id": replay_public_id,
            "report_public_id": report_public_id,
            "report_job_public_id": report_job_public_id,
            "evidence_horizon": evidence_horizon,
            "diagnostic_preview": diagnostic_preview,
        }
        # The public identifiers prevent a Web request from selecting executable paths or process options.
        return JobSpec(
            stage=RENDER_VIDEO,
            component_version=RENDER_VIDEO_VERSION,
            idempotency_key=content_key(RENDER_VIDEO, RENDER_VIDEO_VERSION, "0" * 64, identity),
            input_json=identity,
            priority=100,
            max_attempts=3,
            retryable=True,
        )

    def enqueue(
        self,
        coordinator: VideoJobCoordinator,
        *,
        replay_public_id: str,
        report_public_id: str,
        report_job_public_id: str,
        evidence_horizon: EvidenceHorizon,
        diagnostic_preview: bool,
    ) -> str:
        """Persist a render job and its report prerequisite; rendering remains worker-owned."""
        spec = self.plan(
            replay_public_id=replay_public_id,
            report_public_id=report_public_id,
            report_job_public_id=report_job_public_id,
            evidence_horizon=evidence_horizon,
            diagnostic_preview=diagnostic_preview,
        )
        snapshot = coordinator.create_job(spec)
        public_id = getattr(snapshot, "public_id", None)
        report_job_public_id = spec.input_json["report_job_public_id"]
        if type(public_id) is not str or type(report_job_public_id) is not str:
            raise RuntimeError("video job coordinator returned an invalid durable identity")
        coordinator.add_dependency(public_id, report_job_public_id)
        return public_id
