"""Path-free Web command adapter for durable commented replay casts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import Job, JobStageResult, Replay, Report, TelemetryRun
from generals_replay_analyzer.importing.jobs import JobCoordinator
from generals_replay_analyzer.importing.stages import RENDER_REPORT
from generals_replay_analyzer.video.jobs import (
    VideoJobPlanner,
    VideoJobRequestError,
    _is_canonical_public_id,
    _is_sha256,
)
from generals_replay_analyzer.video.resolver import VideoResolutionError, _resolve_authoritative_logic_fps
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    VerifiedVideoMediaDTO,
    VideoCastRequestDTO,
    VideoCastSubmissionDTO,
)


# TheSuperHackers @fix Leex 24/08/2026 Derive cast authority from persisted parser and telemetry lifecycle, not issue wording. (#TBD)
def _evidence_horizon(report_json: dict[str, object]) -> Literal["complete", "partial"]:
    lifecycle = report_json.get("lifecycle")
    if isinstance(lifecycle, dict):
        if lifecycle.get("parser_completion_status") == "complete" and lifecycle.get("telemetry_status") == "complete":
            return "complete"
        return "partial"
    issues = report_json.get("quality_issues", [])
    return "partial" if isinstance(issues, list) and any(
        isinstance(item, dict) and item.get("issue_code") == "crc_mismatch" for item in issues
    ) else "complete"


class AnalyticsVideoAdapter:
    """Enqueue a cast through public identities; it never starts a renderer."""

    def __init__(self, session_factory: sessionmaker[Session], settings: AnalyzerSettings, *, clock: Callable[[], datetime]) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._clock = clock

    # TheSuperHackers @bugfix Leex 24/08/2026 Enqueue evidence-scoped casts with their immutable replay content identity. (#TBD)
    def submit_video_cast(self, command: VideoCastRequestDTO) -> VideoCastSubmissionDTO:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == command.replay_public_id))
            report = session.scalar(
                select(Report).where(Report.public_id == command.report_public_id, Report.replay_id == (None if replay is None else replay.id))
            )
            report_job = session.scalar(
                select(Job)
                .where(Job.replay_id == (None if replay is None else replay.id), Job.stage == RENDER_REPORT, Job.status == "succeeded")
                .order_by(Job.completed_at.desc(), Job.id.desc())
            )
            if replay is None or report is None or report_job is None:
                raise PublicProblem(status=409, code="video_report_unavailable", detail="A completed replay report is required")
            report_json = report.report_json if isinstance(report.report_json, dict) else {}
            horizon = _evidence_horizon(report_json)
            # TheSuperHackers @bugfix Leex 24/08/2026 Freeze the engine manifest clock instead of guessing from replay wall time. (#TBD)
            telemetry_timebases = tuple(
                (
                    run.schema_version,
                    run.settings_json if isinstance(run.settings_json, dict) else {},
                )
                for run in session.scalars(
                    select(TelemetryRun)
                    .where(TelemetryRun.replay_id == replay.id, TelemetryRun.status == "succeeded")
                    .order_by(TelemetryRun.run_id)
                )
            )
            planner = VideoJobPlanner(clock=self._clock)
            try:
                logic_frames_per_second = _resolve_authoritative_logic_fps(
                    replay.header_json,
                    telemetry_timebases,
                )
                spec = planner.plan(
                    replay_public_id=replay.public_id,
                    replay_sha256=replay.sha256,
                    logic_frames_per_second=logic_frames_per_second,
                    report_public_id=report.public_id,
                    report_job_public_id=report_job.public_id,
                    evidence_horizon=horizon,
                    diagnostic_preview=command.diagnostic_preview,
                )
            except (VideoJobRequestError, VideoResolutionError) as error:
                raise PublicProblem(status=409, code="video_evidence_incomplete", detail=str(error)) from error
            coordinator = JobCoordinator(self._session_factory, clock=self._clock)
            snapshot = coordinator.create_job(replace(spec, replay_id=replay.id))
            coordinator.add_dependency(snapshot.public_id, report_job.public_id)
        return VideoCastSubmissionDTO(
            job_public_id=snapshot.public_id,
            evidence_horizon=horizon,
            diagnostic_preview=command.diagnostic_preview,
            availability=AvailabilityDTO(state="available"),
        )

    def read_verified_video_media(self, job_public_id: str, manifest: bool) -> VerifiedVideoMediaDTO:
        with self._session_factory() as session:
            row = session.scalar(select(Job).where(Job.public_id == job_public_id, Job.stage == "render_video", Job.status == "succeeded"))
            result = None if row is None else session.scalar(select(JobStageResult).where(JobStageResult.job_id == row.id))
            output = None if result is None else result.output_json
        if not isinstance(output, dict):
            raise PublicProblem(status=404, code="verified_video_unavailable", detail="Verified replay cast is unavailable")
        run_id = output.get("run_public_id")
        final_hash = output.get("final_video_sha256")
        manifest_id = output.get("manifest_public_id")
        manifest_hash = output.get("manifest_sha256")
        # TheSuperHackers @bugfix Leex 24/08/2026 Revalidate persisted media identities and contain every download below its render run. (#TBD)
        if not (
            output.get("schema_version") == "video-stage-output-v1"
            and _is_canonical_public_id(run_id)
            and _is_canonical_public_id(manifest_id)
            and _is_sha256(final_hash)
            and _is_sha256(manifest_hash)
        ):
            raise PublicProblem(status=404, code="verified_video_unavailable", detail="Verified replay cast is unavailable")
        assert isinstance(run_id, str) and isinstance(final_hash, str) and isinstance(manifest_hash, str)
        root = self._settings.video_run_directory.resolve()
        directory = (root / run_id).resolve()
        if directory.parent != root:
            raise PublicProblem(status=404, code="verified_video_unavailable", detail="Verified replay cast is unavailable")
        path = (directory / ("video-manifest-v1.json" if manifest else f"final-{final_hash}.mp4")).resolve()
        if path.parent != directory:
            raise PublicProblem(status=404, code="verified_video_unavailable", detail="Verified replay cast is unavailable")
        try:
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise PublicProblem(status=404, code="verified_video_unavailable", detail="Verified replay cast is unavailable") from error
        expected = manifest_hash if manifest else final_hash
        if digest.hexdigest() != expected:
            raise PublicProblem(status=409, code="verified_video_identity_mismatch", detail="Verified replay cast identity changed")
        if manifest:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as error:
                raise PublicProblem(status=409, code="verified_video_identity_mismatch", detail="Verified manifest is invalid") from error
            if document.get("render_public_id") != run_id or document.get("verification_passed") is not True:
                raise PublicProblem(status=409, code="verified_video_identity_mismatch", detail="Verified manifest is invalid")
        def chunks() -> Iterator[bytes]:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    yield chunk
        if manifest:
            return VerifiedVideoMediaDTO(media_type="application/json", filename="video-manifest-v1.json", chunks=chunks)
        return VerifiedVideoMediaDTO(media_type="video/mp4", filename=f"replay-cast-{run_id}.mp4", chunks=chunks)
