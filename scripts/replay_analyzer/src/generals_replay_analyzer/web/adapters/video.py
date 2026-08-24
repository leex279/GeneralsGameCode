"""Path-free Web command adapter for durable commented replay casts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import Job, Replay, Report
from generals_replay_analyzer.importing.jobs import JobCoordinator
from generals_replay_analyzer.importing.stages import RENDER_REPORT
from generals_replay_analyzer.video.jobs import VideoJobPlanner, VideoJobRequestError
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import AvailabilityDTO, VideoCastRequestDTO, VideoCastSubmissionDTO


class AnalyticsVideoAdapter:
    """Enqueue a cast through public identities; it never starts a renderer."""

    def __init__(self, session_factory: sessionmaker[Session], *, clock: Callable[[], datetime]) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # TheSuperHackers @feature Leex 24/08/2026 Enqueue evidence-scoped replay casts without exposing execution capabilities. (#TBD)
    def submit_video_cast(self, command: VideoCastRequestDTO) -> VideoCastSubmissionDTO:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == command.replay_public_id))
            report = session.scalar(
                select(Report).where(Report.public_id == command.report_public_id, Report.replay_id == Replay.id)
            )
            report_job = session.scalar(
                select(Job)
                .where(Job.replay_id == (None if replay is None else replay.id), Job.stage == RENDER_REPORT, Job.status == "succeeded")
                .order_by(Job.completed_at.desc(), Job.id.desc())
            )
            if replay is None or report is None or report_job is None:
                raise PublicProblem(status=409, code="video_report_unavailable", detail="A completed replay report is required")
            report_json = report.report_json if isinstance(report.report_json, dict) else {}
            issues = report_json.get("quality_issues", [])
            horizon: Literal["complete", "partial"] = "partial" if isinstance(issues, list) and any(
                isinstance(item, dict) and item.get("issue_code") == "crc_mismatch" for item in issues
            ) else "complete"
            planner = VideoJobPlanner(clock=self._clock)
            try:
                spec = planner.plan(
                    replay_public_id=replay.public_id,
                    report_public_id=report.public_id,
                    report_job_public_id=report_job.public_id,
                    evidence_horizon=horizon,
                    diagnostic_preview=command.diagnostic_preview,
                )
            except VideoJobRequestError as error:
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
