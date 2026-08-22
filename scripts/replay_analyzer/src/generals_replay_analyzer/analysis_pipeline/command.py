"""Bounded replay-scoped orchestration for the public analyze command."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import (
    AnalysisRun,
    Job,
    JobDependency,
    JobStageResult,
    ManagedAsset,
    Replay,
    ReplayPlayer,
    Report,
)
from ..importing.job_contracts import JobClaimSelectorDTO
from ..importing.stages import (
    ANALYZE_LLM,
    ASSESS_STRATEGIES,
    DERIVE_FEATURES,
    IMPORT_OBSERVATIONS,
    RENDER_REPORT,
)
from .planner import AnalysisPlanDTO

SAFE_ANALYZE_STAGES = tuple(
    sorted((IMPORT_OBSERVATIONS, DERIVE_FEATURES, ASSESS_STRATEGIES, ANALYZE_LLM, RENDER_REPORT))
)
DETERMINISTIC_ANALYZE_STAGES = tuple(stage for stage in SAFE_ANALYZE_STAGES if stage != ANALYZE_LLM)
MAX_ANALYZE_CLAIMS = 32


class AnalysisPlannerPort(Protocol):
    def ensure_analysis_plan(self, replay_public_id: str, allow_ollama: bool) -> AnalysisPlanDTO: ...


class ScopedWorkerPort(Protocol):
    def run_once(self, selector: JobClaimSelectorDTO) -> bool: ...


@dataclass(frozen=True, slots=True)
class AnalysisJobStatusDTO:
    public_id: str
    stage: str
    status: str
    job_identity_digest: str
    result_public_id: str | None


@dataclass(frozen=True, slots=True)
class AnalysisReportStatusDTO:
    public_id: str
    input_digest: str
    cache_key: str
    structured_asset_public_id: str
    structured_asset_sha256: str
    presentation_asset_public_id: str
    presentation_asset_sha256: str


@dataclass(frozen=True, slots=True)
class AnalysisCommandResult:
    status: Literal[
        "awaiting_observations",
        "planned",
        "incomplete",
        "succeeded",
        "failed",
        "claim_limit_reached",
    ]
    replay_public_id: str
    allow_ollama: bool
    claims_executed: int
    jobs: tuple[AnalysisJobStatusDTO, ...]
    reports: tuple[AnalysisReportStatusDTO, ...]


# TheSuperHackers @feature Leex 22/08/2026 Bound foreground analysis to one replay and the safe analytics stages. (#TBD)
class AnalysisCommandService:
    """Plan by default and optionally drain at most 32 exact replay-scoped jobs."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        planner: AnalysisPlannerPort,
        runtime: ScopedWorkerPort,
    ) -> None:
        self._session_factory = session_factory
        self._planner = planner
        self._runtime = runtime

    def run(self, replay_public_id: str, *, execute: bool, allow_ollama: bool) -> AnalysisCommandResult:
        if type(execute) is not bool or type(allow_ollama) is not bool:
            raise TypeError("analysis command flags must be exact booleans")
        plan = self._planner.ensure_analysis_plan(replay_public_id, allow_ollama)
        claims = 0
        if execute:
            stages = SAFE_ANALYZE_STAGES if allow_ollama else DETERMINISTIC_ANALYZE_STAGES
            while claims < MAX_ANALYZE_CLAIMS:
                selected_ids = self._planned_job_public_ids(plan)
                if not selected_ids or not self._runtime.run_once(
                    JobClaimSelectorDTO(replay_public_id, stages, selected_ids)
                ):
                    break
                claims += 1
                plan = self._planner.ensure_analysis_plan(replay_public_id, allow_ollama)
        jobs = self._jobs(plan)
        reports = self._reports(plan)
        status = self._status(plan, jobs, reports, execute=execute, claims=claims)
        return AnalysisCommandResult(status, replay_public_id, allow_ollama, claims, jobs, reports)

    def _planned_job_public_ids(self, plan: AnalysisPlanDTO) -> tuple[str, ...]:
        planned_ids = {
            public_id
            for public_id in (
                plan.derive_job_public_id,
                plan.assess_job_public_id,
                plan.llm_job_public_id,
                plan.report_job_public_id,
            )
            if public_id is not None
        }
        if plan.derive_job_public_id is None:
            return tuple(sorted(planned_ids))
        with self._session_factory() as session:
            planned_ids.update(
                session.scalars(
                    select(Job.public_id)
                    .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                    .join(Replay, Replay.id == Job.replay_id)
                    .where(
                        JobDependency.job_id
                        == select(Job.id)
                        .join(Replay, Replay.id == Job.replay_id)
                        .where(
                            Job.public_id == plan.derive_job_public_id,
                            Replay.public_id == plan.replay_public_id,
                        )
                        .scalar_subquery(),
                        Job.stage == IMPORT_OBSERVATIONS,
                        Replay.public_id == plan.replay_public_id,
                    )
                )
            )
        return tuple(sorted(planned_ids))

    def _jobs(self, plan: AnalysisPlanDTO) -> tuple[AnalysisJobStatusDTO, ...]:
        planned_ids = {
            value
            for value in (
                plan.derive_job_public_id,
                plan.assess_job_public_id,
                plan.llm_job_public_id,
                plan.report_job_public_id,
            )
            if value is not None
        }
        with self._session_factory() as session:
            replay_id = session.scalar(select(Replay.id).where(Replay.public_id == plan.replay_public_id))
            if replay_id is None:
                return ()
            if plan.derive_job_public_id is not None:
                planned_ids.update(
                    session.scalars(
                        select(Job.public_id)
                        .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                        .where(
                            JobDependency.job_id
                            == select(Job.id)
                            .where(Job.public_id == plan.derive_job_public_id, Job.replay_id == replay_id)
                            .scalar_subquery(),
                            Job.stage == IMPORT_OBSERVATIONS,
                            Job.replay_id == replay_id,
                        )
                    )
                )
            rows = tuple(
                session.execute(
                    select(Job)
                    .add_columns(JobStageResult.public_id)
                    .outerjoin(JobStageResult, JobStageResult.job_id == Job.id)
                    .where(
                        Job.replay_id == replay_id,
                        Job.stage.in_(SAFE_ANALYZE_STAGES),
                    )
                    .order_by(Job.stage, Job.public_id)
                )
            )
        return tuple(
            AnalysisJobStatusDTO(
                row.public_id,
                row.stage,
                row.status,
                hashlib.sha256(row.idempotency_key.encode("utf-8")).hexdigest(),
                result_public_id,
            )
            for row, result_public_id in rows
            if row.public_id in planned_ids
            or (plan.status == "awaiting_observations" and row.stage == IMPORT_OBSERVATIONS)
        )

    def _reports(self, plan: AnalysisPlanDTO) -> tuple[AnalysisReportStatusDTO, ...]:
        if plan.report_job_public_id is None:
            return ()
        with self._session_factory() as session:
            replay_id = session.scalar(select(Replay.id).where(Replay.public_id == plan.replay_public_id))
            result = session.scalar(
                select(JobStageResult)
                .join(Job, JobStageResult.job_id == Job.id)
                .where(
                    Job.public_id == plan.report_job_public_id,
                    Job.replay_id == replay_id,
                    Job.stage == RENDER_REPORT,
                    Job.status == "succeeded",
                )
            )
            if result is None or not isinstance(result.output_json, Mapping):
                return ()
            report_entries = result.output_json.get("reports")
            if result.output_json.get("schema_version") != "report-output-v1" or not isinstance(report_entries, list):
                return ()
            if any(
                not isinstance(entry, Mapping)
                or not (
                    entry.get("replay_player_public_id") is None
                    or type(entry.get("replay_player_public_id")) is str
                )
                or not (entry.get("analysis_run_id") is None or type(entry.get("analysis_run_id")) is str)
                or type(entry.get("structured_asset_public_id")) is not str
                or type(entry.get("presentation_asset_public_id")) is not str
                for entry in report_entries
            ):
                return ()
            ordered_publications = tuple(
                (
                    entry.get("replay_player_public_id"),
                    entry.get("analysis_run_id"),
                    entry["structured_asset_public_id"],
                    entry["presentation_asset_public_id"],
                )
                for entry in report_entries
            )
            published_publications = set(ordered_publications)
            if not published_publications or len(published_publications) != len(ordered_publications):
                return ()
            rows: list[AnalysisReportStatusDTO] = []
            found_publications: set[tuple[str | None, str | None, str, str]] = set()
            for report in session.scalars(
                select(Report).where(Report.replay_id == replay_id).order_by(Report.public_id)
            ):
                if report.structured_asset_id is None or report.rendered_asset_id is None:
                    continue
                structured = session.get(ManagedAsset, report.structured_asset_id)
                presentation = session.get(ManagedAsset, report.rendered_asset_id)
                if structured is None or presentation is None:
                    continue
                replay_player_public_id = None
                if report.replay_player_id is not None:
                    replay_player = session.get(ReplayPlayer, report.replay_player_id)
                    if replay_player is None or replay_player.replay_id != replay_id:
                        continue
                    replay_player_public_id = replay_player.public_id
                analysis_run_id = None
                if report.analysis_run_id is not None:
                    analysis_run = session.get(AnalysisRun, report.analysis_run_id)
                    if analysis_run is None or analysis_run.replay_id != replay_id:
                        continue
                    analysis_run_id = analysis_run.run_id
                publication = (
                    replay_player_public_id,
                    analysis_run_id,
                    structured.public_id,
                    presentation.public_id,
                )
                if (
                    publication not in published_publications
                    or structured.kind != "report_structured_json"
                    or presentation.kind != "report_presentation_bundle"
                ):
                    continue
                found_publications.add(publication)
                rows.append(
                    AnalysisReportStatusDTO(
                        report.public_id,
                        report.input_digest,
                        report.cache_key,
                        structured.public_id,
                        structured.sha256,
                        presentation.public_id,
                        presentation.sha256,
                    )
                )
            if found_publications != published_publications or len(rows) != len(published_publications):
                return ()
            return tuple(rows)

    @staticmethod
    def _status(
        plan: AnalysisPlanDTO,
        jobs: tuple[AnalysisJobStatusDTO, ...],
        reports: tuple[AnalysisReportStatusDTO, ...],
        *,
        execute: bool,
        claims: int,
    ) -> Literal[
        "awaiting_observations",
        "planned",
        "incomplete",
        "succeeded",
        "failed",
        "claim_limit_reached",
    ]:
        if not execute:
            return plan.status
        if plan.status == "awaiting_observations":
            observations = tuple(job for job in jobs if job.stage == IMPORT_OBSERVATIONS)
            if observations and all(job.status in {"failed", "cancelled"} for job in observations):
                return "failed"
            if claims == MAX_ANALYZE_CLAIMS:
                return "claim_limit_reached"
            return "awaiting_observations"
        if any(job.status in {"failed", "cancelled"} for job in jobs):
            return "failed"
        if plan.report_job_public_id is not None and any(
            job.public_id == plan.report_job_public_id and job.status == "succeeded" for job in jobs
        ):
            return "succeeded" if reports else "failed"
        if claims == MAX_ANALYZE_CLAIMS:
            return "claim_limit_reached"
        return "incomplete"
