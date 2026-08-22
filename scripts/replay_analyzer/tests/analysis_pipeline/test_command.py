"""Replay-scoped analysis command orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.command import (
    DETERMINISTIC_ANALYZE_STAGES,
    SAFE_ANALYZE_STAGES,
    AnalysisCommandService,
)
from generals_replay_analyzer.analysis_pipeline.planner import AnalysisPlanDTO
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    Job,
    JobStageResult,
    ManagedAsset,
    ParserRun,
    Replay,
    ReplayPlayer,
    Report,
)
from generals_replay_analyzer.importing import JobClaimSelectorDTO, StageExecutionOutcomeDTO
from generals_replay_analyzer.importing.job_lifecycle import JobLifecycleService
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec

REPLAY_ID = "123e4567-e89b-42d3-a456-426614174100"
DERIVE_ID = "123e4567-e89b-42d3-a456-426614174101"
ASSESS_ID = "123e4567-e89b-42d3-a456-426614174102"
REPORT_ID = "123e4567-e89b-42d3-a456-426614174103"


def _replay(session_factory: sessionmaker[Session], clock: datetime) -> Replay:
    with session_factory.begin() as session:
        row = Replay(
            public_id=REPLAY_ID,
            sha256="a" * 64,
            replay_name="fixture.rep",
            version_string="1.04",
            version_number=1,
            frame_count=1,
            start_time=0,
            end_time=1,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="fixture.map",
            seed=4,
            header_json={},
            lifecycle_state="parsed",
            created_at=clock,
            updated_at=clock,
        )
        session.add(row)
        session.flush()
        session.expunge(row)
        return row


@dataclass
class _Planner:
    plans: list[AnalysisPlanDTO]
    calls: list[tuple[str, bool]] = field(default_factory=list)

    def ensure_analysis_plan(self, replay_public_id: str, allow_ollama: bool) -> AnalysisPlanDTO:
        self.calls.append((replay_public_id, allow_ollama))
        if len(self.plans) > 1:
            return self.plans.pop(0)
        return self.plans[0]


@dataclass
class _Runtime:
    outcomes: list[bool]
    selectors: list[JobClaimSelectorDTO] = field(default_factory=list)

    def run_once(self, selector: JobClaimSelectorDTO) -> bool:
        self.selectors.append(selector)
        return self.outcomes.pop(0) if self.outcomes else False


def _job(
    session_factory: sessionmaker[Session],
    clock: datetime,
    replay: Replay,
    stage: str,
    public_id: str,
    *,
    status: str = "pending",
) -> Job:
    coordinator = JobCoordinator(session_factory, clock=lambda: clock)
    with session_factory.begin() as session:
        row = coordinator.ensure_job(
            session,
            JobSpec(stage, "1", f"identity-{stage}-{public_id}", {"replay_public_id": replay.public_id}, replay.id),
        )
        row.public_id = public_id
        if status != "pending":
            row.status = status
            row.completed_at = clock
            row.retryable = False
        session.flush()
        session.expunge(row)
        return row


def test_default_analysis_only_plans_and_never_executes(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch the default command starting a child process or local model while evidence is still pending."""
    replay = _replay(session_factory, clock)
    planner = _Planner([AnalysisPlanDTO("awaiting_observations", replay.public_id, False)])
    runtime = _Runtime([True])

    result = AnalysisCommandService(session_factory, planner, runtime).run(
        replay.public_id,
        execute=False,
        allow_ollama=False,
    )

    assert result.status == "awaiting_observations"
    assert result.claims_executed == 0
    assert result.allow_ollama is False
    assert runtime.selectors == []
    assert planner.calls == [(replay.public_id, False)]


def test_analysis_command_rejects_non_boolean_execution_flags(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    command = AnalysisCommandService(
        session_factory,
        _Planner([AnalysisPlanDTO("awaiting_observations", replay.public_id, False)]),
        _Runtime([]),
    )

    with pytest.raises(TypeError, match="exact booleans"):
        command.run(replay.public_id, execute=1, allow_ollama=False)  # type: ignore[arg-type]


def test_execute_reports_awaiting_when_no_observation_job_is_claimable(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    waiting = AnalysisPlanDTO("awaiting_observations", replay.public_id, False)

    result = AnalysisCommandService(session_factory, _Planner([waiting]), _Runtime([False])).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    assert result.status == "awaiting_observations"


def test_pending_observation_attempt_keeps_awaiting_despite_historical_failure(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _job(
        session_factory,
        clock,
        replay,
        "import_observations",
        "123e4567-e89b-42d3-a456-426614174160",
        status="failed",
    )
    _job(
        session_factory,
        clock,
        replay,
        "import_observations",
        "123e4567-e89b-42d3-a456-426614174161",
    )
    waiting = AnalysisPlanDTO("awaiting_observations", replay.public_id, False)

    output = AnalysisCommandService(session_factory, _Planner([waiting]), _Runtime([False])).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    assert output.status == "awaiting_observations"


def test_execute_replans_after_every_claim_and_uses_only_the_exact_safe_scope(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch analysis claiming parse/telemetry or another replay and missing newly materialized jobs."""
    replay = _replay(session_factory, clock)
    observation_id = "123e4567-e89b-42d3-a456-426614174109"
    observation = _job(session_factory, clock, replay, "import_observations", observation_id)
    for stage, public_id in (
        ("derive_features", DERIVE_ID),
        ("assess_strategies", ASSESS_ID),
        ("render_report", REPORT_ID),
    ):
        _job(session_factory, clock, replay, stage, public_id)
    with session_factory.begin() as session:
        derive = session.scalar(select(Job).where(Job.public_id == DERIVE_ID))
        assert derive is not None
        JobCoordinator(session_factory, clock=lambda: clock).ensure_dependency(session, derive.id, observation.id)
    planned = AnalysisPlanDTO("planned", replay.public_id, False, DERIVE_ID, ASSESS_ID, None, REPORT_ID)
    planner = _Planner([planned])
    runtime = _Runtime([True, True, False])

    result = AnalysisCommandService(session_factory, planner, runtime).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    assert result.claims_executed == 2
    assert planner.calls == [(replay.public_id, False)] * 3
    selected_ids = tuple(sorted((observation_id, DERIVE_ID, ASSESS_ID, REPORT_ID)))
    assert runtime.selectors == [
        JobClaimSelectorDTO(replay.public_id, DETERMINISTIC_ANALYZE_STAGES, selected_ids),
        JobClaimSelectorDTO(replay.public_id, DETERMINISTIC_ANALYZE_STAGES, selected_ids),
        JobClaimSelectorDTO(replay.public_id, DETERMINISTIC_ANALYZE_STAGES, selected_ids),
    ]
    assert set(SAFE_ANALYZE_STAGES) == {
        "import_observations",
        "derive_features",
        "assess_strategies",
        "analyze_llm",
        "render_report",
    }
    assert {job.stage for job in result.jobs} == {
        "import_observations",
        "derive_features",
        "assess_strategies",
        "render_report",
    }


def test_historical_failed_observation_does_not_poison_a_new_planned_graph(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch an unrelated prior observation attempt forcing a healthy current graph to failed."""
    replay = _replay(session_factory, clock)
    _job(
        session_factory,
        clock,
        replay,
        "import_observations",
        "123e4567-e89b-42d3-a456-426614174150",
        status="failed",
    )
    for stage, public_id in (
        ("derive_features", DERIVE_ID),
        ("assess_strategies", ASSESS_ID),
        ("render_report", REPORT_ID),
    ):
        _job(session_factory, clock, replay, stage, public_id)
    plan = AnalysisPlanDTO("planned", replay.public_id, False, DERIVE_ID, ASSESS_ID, None, REPORT_ID)

    output = AnalysisCommandService(session_factory, _Planner([plan]), _Runtime([False])).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    assert output.status == "incomplete"
    assert all(job.stage != "import_observations" for job in output.jobs)


def test_execute_includes_the_network_stage_only_after_durable_ollama_opt_in(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch an old pending Ollama job running during an otherwise deterministic execution request."""
    replay = _replay(session_factory, clock)
    planned = AnalysisPlanDTO("planned", replay.public_id, True, DERIVE_ID, ASSESS_ID, REPORT_ID, REPORT_ID)
    runtime = _Runtime([False])

    AnalysisCommandService(session_factory, _Planner([planned]), runtime).run(
        replay.public_id,
        execute=True,
        allow_ollama=True,
    )

    assert runtime.selectors == [
        JobClaimSelectorDTO(
            replay.public_id,
            SAFE_ANALYZE_STAGES,
            tuple(sorted((DERIVE_ID, ASSESS_ID, REPORT_ID))),
        )
    ]
    assert "analyze_llm" not in DETERMINISTIC_ANALYZE_STAGES


def test_execute_stops_at_exactly_32_claims(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch a poisoned or cyclic queue keeping the foreground command alive without a bound."""
    replay = _replay(session_factory, clock)
    planned = AnalysisPlanDTO("planned", replay.public_id, False, DERIVE_ID, ASSESS_ID, None, REPORT_ID)
    planner = _Planner([planned])
    runtime = _Runtime([True] * 40)

    result = AnalysisCommandService(session_factory, planner, runtime).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    assert result.status == "claim_limit_reached"
    assert result.claims_executed == 32
    assert len(runtime.selectors) == 32
    assert len(planner.calls) == 33


def test_execute_leaves_a_higher_priority_unsafe_job_untouched(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch replay-scoped foreground execution stealing parser or engine-owned work."""
    replay = _replay(session_factory, clock)
    coordinator = JobCoordinator(session_factory, clock=lambda: clock)
    with session_factory.begin() as session:
        unsafe = coordinator.ensure_job(
            session,
            JobSpec("parse", "1", "unsafe-high-priority", {}, replay.id, priority=0, retryable=False),
        )
        target = coordinator.ensure_job(
            session,
            JobSpec("render_report", "1", "safe-lower-priority", {}, replay.id, priority=100, retryable=False),
        )
        unsafe_id = unsafe.public_id
        target_id = target.public_id

    class ClaimAndFailRuntime:
        def __init__(self) -> None:
            self.control = JobLifecycleService(
                session_factory,
                registered_stages=("parse", "render_report"),
                clock=lambda: clock,
            )

        def run_once(self, selector: JobClaimSelectorDTO) -> bool:
            claim = self.control.claim_next(
                "123e4567-e89b-42d3-a456-426614174130",
                30,
                selector,
            )
            if claim is None:
                return False
            self.control.settle_failure(
                "123e4567-e89b-42d3-a456-426614174130",
                claim,
                StageExecutionOutcomeDTO("failed", None, "stage_failed", "stage failed", False),
            )
            return True

    plan = AnalysisPlanDTO("planned", replay.public_id, False, DERIVE_ID, ASSESS_ID, None, target_id)
    result = AnalysisCommandService(session_factory, _Planner([plan]), ClaimAndFailRuntime()).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    with session_factory() as session:
        unsafe_row = session.scalar(select(Job).where(Job.public_id == unsafe_id))
        target_row = session.scalar(select(Job).where(Job.public_id == target_id))
        assert unsafe_row is not None and unsafe_row.status == "pending" and unsafe_row.attempt_count == 0
        assert target_row is not None and target_row.status == "failed" and target_row.attempt_count == 1
    assert result.status == "failed"
    assert result.claims_executed == 1


def test_execute_leaves_a_higher_priority_historical_safe_job_untouched(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch replay/stage scoping stealing a safe-stage job outside the selected plan."""
    replay = _replay(session_factory, clock)
    coordinator = JobCoordinator(session_factory, clock=lambda: clock)
    with session_factory.begin() as session:
        historical = coordinator.ensure_job(
            session,
            JobSpec("render_report", "1", "historical-safe-job", {}, replay.id, priority=0, retryable=False),
        )
        current = coordinator.ensure_job(
            session,
            JobSpec("render_report", "1", "current-plan-job", {}, replay.id, priority=100, retryable=False),
        )
        historical_id = historical.public_id
        current_id = current.public_id

    class ClaimAndFailRuntime:
        def __init__(self) -> None:
            self.control = JobLifecycleService(
                session_factory,
                registered_stages=("render_report",),
                clock=lambda: clock,
            )

        def run_once(self, selector: JobClaimSelectorDTO) -> bool:
            claim = self.control.claim_next(
                "123e4567-e89b-42d3-a456-426614174130",
                30,
                selector,
            )
            if claim is None:
                return False
            self.control.settle_failure(
                "123e4567-e89b-42d3-a456-426614174130",
                claim,
                StageExecutionOutcomeDTO("failed", None, "stage_failed", "stage failed", False),
            )
            return True

    plan = AnalysisPlanDTO("planned", replay.public_id, False, DERIVE_ID, ASSESS_ID, None, current_id)
    result = AnalysisCommandService(session_factory, _Planner([plan]), ClaimAndFailRuntime()).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    with session_factory() as session:
        historical_row = session.scalar(select(Job).where(Job.public_id == historical_id))
        current_row = session.scalar(select(Job).where(Job.public_id == current_id))
        assert historical_row is not None and historical_row.status == "pending"
        assert historical_row.attempt_count == 0
        assert current_row is not None and current_row.status == "failed"
        assert current_row.attempt_count == 1
    assert result.status == "failed"
    assert result.claims_executed == 1


def test_succeeded_report_snapshot_exposes_only_public_ids_and_digests(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch the CLI leaking managed paths or omitting the immutable report artifact identities."""
    replay = _replay(session_factory, clock)
    derive = _job(session_factory, clock, replay, "derive_features", DERIVE_ID, status="succeeded")
    assess = _job(session_factory, clock, replay, "assess_strategies", ASSESS_ID, status="succeeded")
    report_job = _job(session_factory, clock, replay, "render_report", REPORT_ID, status="succeeded")
    with session_factory.begin() as session:
        parser = ParserRun(
            run_id="123e4567-e89b-42d3-a456-426614174106",
            replay_id=replay.id,
            parser_version="1",
            schema_version=1,
            input_sha256=replay.sha256,
            status="running",
            warnings_json=[],
            started_at=clock,
        )
        session.add(parser)
        session.flush()
        replay_player = ReplayPlayer(
            public_id="123e4567-e89b-42d3-a456-426614174107",
            replay_id=replay.id,
            parser_run_id=parser.id,
            slot_index=0,
            slot_kind="human",
            player_index=0,
            observed_json={},
        )
        session.add(replay_player)
        session.flush()
        analysis = AnalysisRun(
            run_id="123e4567-e89b-42d3-a456-426614174108",
            replay_id=replay.id,
            replay_player_id=replay_player.id,
            provider="ollama",
            model_name="fixture",
            model_digest="1" * 64,
            prompt_version="1",
            prompt_digest="2" * 64,
            response_schema_version="1",
            response_schema_digest="3" * 64,
            settings_digest="4" * 64,
            input_digest="5" * 64,
            cache_key="6" * 64,
            status="running",
            diagnostics_json=[],
            created_at=clock,
        )
        session.add(analysis)
        session.flush()
        structured = ManagedAsset(
            public_id="123e4567-e89b-42d3-a456-426614174110",
            sha256="b" * 64,
            kind="report_structured_json",
            relative_path="reports/structured",
            size_bytes=10,
            media_type="application/json",
            created_at=clock,
        )
        presentation = ManagedAsset(
            public_id="123e4567-e89b-42d3-a456-426614174111",
            sha256="c" * 64,
            kind="report_presentation_bundle",
            relative_path="reports/presentation",
            size_bytes=20,
            media_type="application/json",
            created_at=clock,
        )
        session.add_all((structured, presentation))
        session.flush()
        report = Report(
            public_id="123e4567-e89b-42d3-a456-426614174112",
            replay_id=replay.id,
            replay_player_id=replay_player.id,
            analysis_run_id=analysis.id,
            report_version="1",
            input_digest="d" * 64,
            cache_key="e" * 64,
            report_json={},
            structured_asset_id=structured.id,
            rendered_asset_id=presentation.id,
            created_at=clock,
        )
        result = JobStageResult(
            public_id="123e4567-e89b-42d3-a456-426614174113",
            job_id=report_job.id,
            stage="render_report",
            component_version="1",
            idempotency_key=report_job.idempotency_key,
            output_json={
                "schema_version": "report-output-v1",
                "reports": [
                    {
                        "replay_player_public_id": replay_player.public_id,
                        "analysis_run_id": analysis.run_id,
                        "structured_asset_public_id": structured.public_id,
                        "presentation_asset_public_id": presentation.public_id,
                    }
                ],
            },
            created_at=clock,
        )
        session.add_all((report, result))
    planned = AnalysisPlanDTO("planned", replay.public_id, False, derive.public_id, assess.public_id, None, report_job.public_id)

    command = AnalysisCommandService(session_factory, _Planner([planned]), _Runtime([False]))
    output = command.run(replay.public_id, execute=True, allow_ollama=False)

    assert output.status == "succeeded"
    assert output.jobs[-1].result_public_id == "123e4567-e89b-42d3-a456-426614174113"
    assert [asdict(item) for item in output.reports] == [
        {
            "public_id": "123e4567-e89b-42d3-a456-426614174112",
            "input_digest": "d" * 64,
            "cache_key": "e" * 64,
            "structured_asset_public_id": "123e4567-e89b-42d3-a456-426614174110",
            "structured_asset_sha256": "b" * 64,
            "presentation_asset_public_id": "123e4567-e89b-42d3-a456-426614174111",
            "presentation_asset_sha256": "c" * 64,
        }
    ]
    serialized = repr(asdict(output))
    assert "reports/" not in serialized and "\\" not in serialized and "://" not in serialized


@pytest.mark.parametrize(
    ("replay_player_public_id", "analysis_run_id", "structured_kind", "presentation_kind"),
    (
        (None, None, "telemetry_trace", "telemetry_catalog"),
        (
            "123e4567-e89b-42d3-a456-426614174174",
            None,
            "report_structured_json",
            "report_presentation_bundle",
        ),
        (
            None,
            "123e4567-e89b-42d3-a456-426614174175",
            "report_structured_json",
            "report_presentation_bundle",
        ),
    ),
)
def test_report_publication_requires_exact_subject_run_and_asset_kinds(
    session_factory: sessionmaker[Session],
    clock: datetime,
    replay_player_public_id: str | None,
    analysis_run_id: str | None,
    structured_kind: str,
    presentation_kind: str,
) -> None:
    """Catch asset-pair matching authenticating the wrong publication graph."""
    replay = _replay(session_factory, clock)
    report_job = _job(session_factory, clock, replay, "render_report", REPORT_ID, status="succeeded")
    with session_factory.begin() as session:
        structured = ManagedAsset(
            public_id="123e4567-e89b-42d3-a456-426614174170",
            sha256="5" * 64,
            kind=structured_kind,
            relative_path="reports/wrong-structured-kind",
            size_bytes=1,
            media_type="application/json",
            created_at=clock,
        )
        presentation = ManagedAsset(
            public_id="123e4567-e89b-42d3-a456-426614174171",
            sha256="6" * 64,
            kind=presentation_kind,
            relative_path="reports/wrong-presentation-kind",
            size_bytes=1,
            media_type="application/json",
            created_at=clock,
        )
        session.add_all((structured, presentation))
        session.flush()
        session.add(
            Report(
                public_id="123e4567-e89b-42d3-a456-426614174172",
                replay_id=replay.id,
                report_version="1",
                input_digest="7" * 64,
                cache_key="8" * 64,
                report_json={},
                structured_asset_id=structured.id,
                rendered_asset_id=presentation.id,
                created_at=clock,
            )
        )
        result = JobStageResult(
            public_id="123e4567-e89b-42d3-a456-426614174173",
            job_id=report_job.id,
            stage="render_report",
            component_version="1",
            idempotency_key=report_job.idempotency_key,
            output_json={
                "schema_version": "report-output-v1",
                "reports": [
                    {
                        "replay_player_public_id": replay_player_public_id,
                        "analysis_run_id": analysis_run_id,
                        "structured_asset_public_id": structured.public_id,
                        "presentation_asset_public_id": presentation.public_id,
                    }
                ],
            },
            created_at=clock,
        )
        session.add(result)
    plan = AnalysisPlanDTO("planned", replay.public_id, False, DERIVE_ID, ASSESS_ID, None, report_job.public_id)
    command = AnalysisCommandService(session_factory, _Planner([plan]), _Runtime([False]))

    assert command.run(replay.public_id, execute=True, allow_ollama=False).status == "failed"

def test_terminal_report_job_without_authenticated_publication_fails_closed(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch a terminal job being reported as product success after its report graph disappeared."""
    replay = _replay(session_factory, clock)
    derive = _job(session_factory, clock, replay, "derive_features", DERIVE_ID, status="succeeded")
    assess = _job(session_factory, clock, replay, "assess_strategies", ASSESS_ID, status="succeeded")
    report = _job(session_factory, clock, replay, "render_report", REPORT_ID, status="succeeded")
    plan = AnalysisPlanDTO("planned", replay.public_id, False, derive.public_id, assess.public_id, None, report.public_id)

    output = AnalysisCommandService(session_factory, _Planner([plan]), _Runtime([False])).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    assert output.status == "failed"
    assert output.reports == ()


def test_partial_report_publication_graph_fails_closed(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    """Catch one valid asset pair masking another missing pair in the immutable stage result."""
    replay = _replay(session_factory, clock)
    report_job = _job(session_factory, clock, replay, "render_report", REPORT_ID, status="succeeded")
    with session_factory.begin() as session:
        structured = ManagedAsset(
            public_id="123e4567-e89b-42d3-a456-426614174180",
            sha256="1" * 64,
            kind="report_structured_json",
            relative_path="reports/one",
            size_bytes=1,
            media_type="application/json",
            created_at=clock,
        )
        presentation = ManagedAsset(
            public_id="123e4567-e89b-42d3-a456-426614174181",
            sha256="2" * 64,
            kind="report_presentation_bundle",
            relative_path="reports/two",
            size_bytes=1,
            media_type="application/json",
            created_at=clock,
        )
        session.add_all((structured, presentation))
        session.flush()
        session.add(
            Report(
                public_id="123e4567-e89b-42d3-a456-426614174182",
                replay_id=replay.id,
                report_version="1",
                input_digest="3" * 64,
                cache_key="4" * 64,
                report_json={},
                structured_asset_id=structured.id,
                rendered_asset_id=presentation.id,
                created_at=clock,
            )
        )
        session.add(
            JobStageResult(
                public_id="123e4567-e89b-42d3-a456-426614174183",
                job_id=report_job.id,
                stage="render_report",
                component_version="1",
                idempotency_key=report_job.idempotency_key,
                output_json={
                    "schema_version": "report-output-v1",
                    "reports": [
                        {
                            "structured_asset_public_id": structured.public_id,
                            "presentation_asset_public_id": presentation.public_id,
                        },
                        {
                            "structured_asset_public_id": "123e4567-e89b-42d3-a456-426614174190",
                            "presentation_asset_public_id": "123e4567-e89b-42d3-a456-426614174191",
                        },
                    ],
                },
                created_at=clock,
            )
        )
    plan = AnalysisPlanDTO("planned", replay.public_id, False, DERIVE_ID, ASSESS_ID, None, report_job.public_id)

    output = AnalysisCommandService(session_factory, _Planner([plan]), _Runtime([False])).run(
        replay.public_id,
        execute=True,
        allow_ollama=False,
    )

    assert output.status == "failed"
    assert output.reports == ()
