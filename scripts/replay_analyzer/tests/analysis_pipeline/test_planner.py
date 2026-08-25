"""Durable, observation-bound replay-analysis planning."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.planner import (
    AnalysisPlanDTO,
    AnalysisPlanner,
    AnalysisPlanningError,
    IdentityInvalidationPlanDTO,
    IdentityInvalidationReplayPlanDTO,
)
from generals_replay_analyzer.db.models import (
    Job,
    JobDependency,
    ParserRun,
    Player,
    PlayerAlias,
    Replay,
    ReplayPlayer,
)
from generals_replay_analyzer.identity.normalize import EMBEDDED_REPLAY_NAME_NAMESPACE
from generals_replay_analyzer.identity.service import PlayerIdentityService
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec
from generals_replay_analyzer.importing.stages import (
    ANALYZE_LLM,
    ASSESS_STRATEGIES,
    DERIVE_FEATURES,
    DERIVE_FEATURES_VERSION,
    IMPORT_OBSERVATIONS,
    IMPORT_OBSERVATIONS_VERSION,
    PARSE,
    RENDER_REPORT,
    TELEMETRY,
    content_key,
    input_digest,
)

REPLAY_PUBLIC_ID = "00000000-0000-4000-8000-00000000a181"
REPLAY_SHA256 = "a" * 64
DOWNSTREAM_STAGES = (DERIVE_FEATURES, ASSESS_STRATEGIES, ANALYZE_LLM, RENDER_REPORT)


def _planner(session_factory: sessionmaker[Session], clock: datetime) -> AnalysisPlanner:
    return AnalysisPlanner(session_factory, clock=lambda: clock)


def _replay(session_factory: sessionmaker[Session], clock: datetime) -> Replay:
    with session_factory.begin() as session:
        replay = Replay(
            public_id=REPLAY_PUBLIC_ID,
            sha256=REPLAY_SHA256,
            replay_name="planner.rep",
            version_string="1.04",
            version_number=104,
            frame_count=10,
            start_time=0,
            end_time=10,
            exe_crc=0,
            ini_crc=0,
            map_crc=0,
            map_name="maps/test.map",
            seed=1,
            starting_cash=10000,
            header_json={},
            lifecycle_state="engine_verified",
            created_at=clock,
            updated_at=clock,
        )
        session.add(replay)
        session.flush()
        session.expunge(replay)
        return replay


def _identity_subjects(
    session_factory: sessionmaker[Session],
    clock: datetime,
    replay: Replay,
    *,
    count: int = 3,
) -> tuple[tuple[str, str], ...]:
    with session_factory.begin() as session:
        parser = ParserRun(
            run_id="00000000-0000-4000-8000-00000000b100",
            replay_id=replay.id,
            parser_version="parser-primary",
            schema_version=1,
            input_sha256=replay.sha256,
            result_sha256="b" * 64,
            status="running",
            completion_status="complete",
            command_stream_offset=0,
            end_offset=1,
            warnings_json=[],
            error_json=None,
            started_at=clock,
            completed_at=clock,
        )
        session.add(parser)
        session.flush()
        subjects: list[tuple[str, str]] = []
        for index in range(count):
            player_public_id = f"00000000-0000-4000-8000-{0xB110 + index:012x}"
            replay_player_public_id = f"00000000-0000-4000-8000-{0xB120 + index:012x}"
            player = Player(
                public_id=player_public_id,
                display_name=f"Identity Player {index}",
                identity_revision=0,
                created_at=clock,
                updated_at=clock,
            )
            session.add(player)
            session.flush()
            session.add(
                ReplayPlayer(
                    public_id=replay_player_public_id,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    player_id=player.id,
                    slot_index=index,
                    slot_kind="human",
                    original_name=f"Identity Player {index}",
                    normalized_name=f"identity player {index}",
                    player_index=index,
                    team_id=None,
                    faction="China",
                    color=None,
                    start_position=index,
                    result=None,
                    observed_json={},
                )
            )
            subjects.append((replay_player_public_id, player_public_id))
        session.flush()
        parser.status = "succeeded"
        return tuple(subjects)


def _observation_authority(
    session_factory: sessionmaker[Session],
    clock: datetime,
    replay: Replay,
    *,
    branch: str = "primary",
    legacy_recipe: bool = False,
) -> Job:
    coordinator = JobCoordinator(session_factory, clock=lambda: clock)
    with session_factory.begin() as session:
        parse_identity = {
            "import_mode": "copy",
            "parse_version": "1",
            "parser_version": f"parser-{branch}",
        }
        parser_input = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "import_mode": "copy",
        }
        parser_output = {"parser_run_id": f"parser-{branch}"}
        parser = coordinator.ensure_job(
            session,
            JobSpec(
                PARSE,
                "1",
                content_key(PARSE, "1", replay.sha256, parse_identity),
                parser_input,
                replay.id,
            ),
        )
        parser.status = "succeeded"
        parser.output_json = parser_output
        parser.retryable = False
        parser.completed_at = clock
        branch_recipe = (
            {
                "branch": branch,
                "import_observations_version": IMPORT_OBSERVATIONS_VERSION,
            }
            if legacy_recipe
            else {
                "import_observations_version": IMPORT_OBSERVATIONS_VERSION,
                "import_mode": "copy",
                "parse": parse_identity,
                "telemetry": None,
            }
        )
        dependency_identity = {
            "stage": PARSE,
            "component_version": "1",
            "status": "succeeded",
            "input": {
                "import_mode": "copy",
                "replay_sha256": replay.sha256,
            },
            "output": parser_output,
        }
        selected_identity = {
            "replay_sha256": replay.sha256,
            "branch_recipe": branch_recipe,
            "dependencies": [dependency_identity],
        }
        selected_digest = input_digest(selected_identity)
        provisional_key = content_key(
            IMPORT_OBSERVATIONS,
            IMPORT_OBSERVATIONS_VERSION,
            replay.sha256,
            branch_recipe,
        )
        observation = coordinator.ensure_job(
            session,
            JobSpec(
                IMPORT_OBSERVATIONS,
                IMPORT_OBSERVATIONS_VERSION,
                content_key(
                    IMPORT_OBSERVATIONS,
                    IMPORT_OBSERVATIONS_VERSION,
                    replay.sha256,
                    selected_identity,
                ),
                {
                    "replay_public_id": replay.public_id,
                    "replay_sha256": replay.sha256,
                    "branch_recipe": branch_recipe,
                    "dependency_identity_bound": True,
                    "provisional_idempotency_key": provisional_key,
                    "selected_dependency_digest": selected_digest,
                },
                replay.id,
            ),
        )
        materialized_parser = session.scalar(
            select(ParserRun).where(
                ParserRun.replay_id == replay.id,
                ParserRun.parser_version == f"parser-{branch}",
            )
        )
        if materialized_parser is None:
            materialized_parser = ParserRun(
                run_id=f"00000000-0000-4000-8000-{0xB900 + len(branch):012x}",
                replay_id=replay.id,
                parser_version=f"parser-{branch}",
                schema_version=1,
                input_sha256=replay.sha256,
                result_sha256="d" * 64,
                status="succeeded",
                completion_status="complete",
                command_stream_offset=0,
                end_offset=1,
                warnings_json=[],
                error_json=None,
                started_at=clock,
                completed_at=clock,
            )
            session.add(materialized_parser)
            session.flush()
        observation.status = "succeeded"
        observation.output_json = {
            "idempotency_key": "planner-observations",
            "parser_run_id": materialized_parser.run_id,
            "parser_command_count": 0,
            "telemetry_run_id": None,
            "telemetry_event_count": 0,
        }
        observation.retryable = False
        observation.completed_at = clock
        coordinator.ensure_dependency(session, observation.id, parser.id)
        session.flush()
        session.expunge(observation)
        return observation


def _telemetry_observation_authority(
    session_factory: sessionmaker[Session],
    clock: datetime,
    replay: Replay,
    *,
    cross_branch: bool,
) -> Job:
    coordinator = JobCoordinator(session_factory, clock=lambda: clock)
    with session_factory.begin() as session:
        parser_input = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "import_mode": "copy",
        }

        def parser(branch: str) -> Job:
            identity = {
                "import_mode": "copy",
                "parse_version": "1",
                "parser_version": f"parser-{branch}",
            }
            row = coordinator.ensure_job(
                session,
                JobSpec(
                    PARSE,
                    "1",
                    content_key(PARSE, "1", replay.sha256, identity),
                    parser_input,
                    replay.id,
                ),
            )
            row.status = "succeeded"
            row.output_json = {"parser_run_id": f"parser-{branch}"}
            row.retryable = False
            row.completed_at = clock
            return row

        parser_a = parser("a")
        telemetry_parent = parser("b") if cross_branch else parser_a
        telemetry_identity = {
            "acquirer_version": "telemetry-b",
            "import_mode": "copy",
            "telemetry_version": "1",
        }
        telemetry_input = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "import_mode": "copy",
        }
        telemetry = coordinator.ensure_job(
            session,
            JobSpec(
                TELEMETRY,
                "1",
                content_key(TELEMETRY, "1", replay.sha256, telemetry_identity),
                telemetry_input,
                replay.id,
            ),
        )
        telemetry.status = "succeeded"
        telemetry.output_json = {"telemetry_run_id": "telemetry-b"}
        telemetry.retryable = False
        telemetry.completed_at = clock
        coordinator.ensure_dependency(session, telemetry.id, telemetry_parent.id)
        branch_recipe = {
            "import_observations_version": IMPORT_OBSERVATIONS_VERSION,
            "import_mode": "copy",
            "parse": {
                "import_mode": "copy",
                "parse_version": "1",
                "parser_version": "parser-a",
            },
            "telemetry": telemetry_identity,
        }
        dependencies = [parser_a, telemetry]
        dependency_identities = []
        for dependency in dependencies:
            semantic_input = {
                key: value
                for key, value in cast(dict[str, Any], dependency.input_json).items()
                if key != "replay_public_id"
            }
            dependency_identities.append(
                {
                    "stage": dependency.stage,
                    "component_version": dependency.component_version,
                    "status": "succeeded",
                    "input": semantic_input,
                    "output": cast(dict[str, Any], dependency.output_json),
                }
            )
        selected_identity = {
            "replay_sha256": replay.sha256,
            "branch_recipe": branch_recipe,
            "dependencies": dependency_identities,
        }
        observation = coordinator.ensure_job(
            session,
            JobSpec(
                IMPORT_OBSERVATIONS,
                IMPORT_OBSERVATIONS_VERSION,
                content_key(
                    IMPORT_OBSERVATIONS,
                    IMPORT_OBSERVATIONS_VERSION,
                    replay.sha256,
                    selected_identity,
                ),
                {
                    "replay_public_id": replay.public_id,
                    "replay_sha256": replay.sha256,
                    "branch_recipe": branch_recipe,
                    "dependency_identity_bound": True,
                    "provisional_idempotency_key": content_key(
                        IMPORT_OBSERVATIONS,
                        IMPORT_OBSERVATIONS_VERSION,
                        replay.sha256,
                        branch_recipe,
                    ),
                    "selected_dependency_digest": input_digest(selected_identity),
                },
                replay.id,
            ),
        )
        materialized_parser = ParserRun(
            run_id="00000000-0000-4000-8000-00000000b9aa",
            replay_id=replay.id,
            parser_version="parser-a",
            schema_version=1,
            input_sha256=replay.sha256,
            result_sha256="d" * 64,
            status="succeeded",
            completion_status="complete",
            command_stream_offset=0,
            end_offset=1,
            warnings_json=[],
            error_json=None,
            started_at=clock,
            completed_at=clock,
        )
        session.add(materialized_parser)
        session.flush()
        observation.status = "succeeded"
        observation.output_json = {
            "idempotency_key": "telemetry-planner-observations",
            "parser_run_id": materialized_parser.run_id,
            "parser_command_count": 0,
            "telemetry_run_id": None,
            "telemetry_event_count": 0,
        }
        observation.retryable = False
        observation.completed_at = clock
        coordinator.ensure_dependency(session, observation.id, parser_a.id)
        coordinator.ensure_dependency(session, observation.id, telemetry.id)
        session.flush()
        session.expunge(observation)
        return observation


def _planned_jobs(session_factory: sessionmaker[Session], replay_id: int) -> list[Job]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Job)
                .where(Job.replay_id == replay_id, Job.stage.in_(DOWNSTREAM_STAGES))
                .order_by(Job.id)
            )
        )


def _dependencies(session_factory: sessionmaker[Session], job_public_id: str) -> set[str]:
    with session_factory() as session:
        job_id = session.scalar(select(Job.id).where(Job.public_id == job_public_id))
        assert job_id is not None
        return set(
            session.scalars(
                select(Job.stage)
                .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                .where(JobDependency.job_id == job_id)
            )
        )


def test_plan_contract_is_frozen_and_rejects_noncanonical_arguments(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    planner = _planner(session_factory, clock)
    waiting = planner.ensure_analysis_plan(replay.public_id, False)

    assert waiting == AnalysisPlanDTO("awaiting_observations", replay.public_id, False)
    with pytest.raises(FrozenInstanceError):
        waiting.status = "planned"  # type: ignore[misc]
    with pytest.raises(ValueError, match="canonical lowercase UUID"):
        planner.ensure_analysis_plan(replay.public_id.upper(), False)
    with pytest.raises(TypeError, match="exact boolean"):
        planner.ensure_analysis_plan(replay.public_id, 1)  # type: ignore[arg-type]


def test_plan_dtos_fail_closed_on_inconsistent_identity_and_job_shapes() -> None:
    job_id = "00000000-0000-4000-8000-00000000b201"
    replay_player_id = "00000000-0000-4000-8000-00000000b202"
    with pytest.raises(TypeError, match="exact boolean"):
        AnalysisPlanDTO("awaiting_observations", REPLAY_PUBLIC_ID, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="awaiting plans"):
        AnalysisPlanDTO("awaiting_observations", REPLAY_PUBLIC_ID, False, job_id)
    with pytest.raises(ValueError, match="status"):
        AnalysisPlanDTO("unknown", REPLAY_PUBLIC_ID, False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires deterministic"):
        AnalysisPlanDTO("planned", REPLAY_PUBLIC_ID, False)
    with pytest.raises(ValueError, match="LLM job identity"):
        AnalysisPlanDTO("planned", REPLAY_PUBLIC_ID, False, job_id, job_id, job_id, job_id)

    with pytest.raises(ValueError, match="sorted, unique, and nonempty"):
        IdentityInvalidationReplayPlanDTO("awaiting_observations", REPLAY_PUBLIC_ID, ())
    with pytest.raises(ValueError, match="awaiting invalidation"):
        IdentityInvalidationReplayPlanDTO(
            "awaiting_observations",
            REPLAY_PUBLIC_ID,
            (replay_player_id,),
            job_id,
        )
    with pytest.raises(ValueError, match="requires derive"):
        IdentityInvalidationReplayPlanDTO("planned", REPLAY_PUBLIC_ID, (replay_player_id,))
    waiting = IdentityInvalidationReplayPlanDTO(
        "awaiting_observations",
        REPLAY_PUBLIC_ID,
        (replay_player_id,),
    )
    with pytest.raises(ValueError, match="sorted and unique"):
        IdentityInvalidationPlanDTO(
            "00000000-0000-4000-8000-00000000b203",
            (waiting, waiting),
        )


def test_awaiting_observations_creates_no_downstream_jobs(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)

    result = _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert result.status == "awaiting_observations"
    assert _planned_jobs(session_factory, replay.id) == []


def test_opt_out_plan_is_exact_and_idempotent(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    observation = _observation_authority(session_factory, clock, replay)
    planner = _planner(session_factory, clock)

    first = planner.ensure_analysis_plan(replay.public_id, False)
    second = planner.ensure_analysis_plan(replay.public_id, False)
    jobs = _planned_jobs(session_factory, replay.id)

    assert first == second
    assert first.status == "planned"
    assert first.llm_job_public_id is None
    assert {job.stage for job in jobs} == {DERIVE_FEATURES, ASSESS_STRATEGIES, RENDER_REPORT}
    assert _dependencies(session_factory, cast(str, first.derive_job_public_id)) == {IMPORT_OBSERVATIONS}
    assert _dependencies(session_factory, cast(str, first.assess_job_public_id)) == {DERIVE_FEATURES}
    assert _dependencies(session_factory, cast(str, first.report_job_public_id)) == {ASSESS_STRATEGIES}
    assert observation.public_id not in {
        first.derive_job_public_id,
        first.assess_job_public_id,
        first.report_job_public_id,
    }


def test_plan_persists_the_exact_authoritative_parser_run_and_version(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(session_factory, clock, replay)

    plan = _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)
    planned_ids = {
        plan.derive_job_public_id,
        plan.assess_job_public_id,
        plan.report_job_public_id,
    }
    jobs = tuple(job for job in _planned_jobs(session_factory, replay.id) if job.public_id in planned_ids)

    assert len(jobs) == 3
    for job in jobs:
        planned_input = cast(dict[str, Any], job.input_json)
        assert planned_input["parser_run_id"] == "00000000-0000-4000-8000-00000000b907"
        assert planned_input["parser_version"] == "parser-primary"


def test_identity_revision_changes_player_scoped_job_identity_without_mutating_history(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    subjects = _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)
    planner = _planner(session_factory, clock)
    first = planner.ensure_analysis_plan(replay.public_id, False)
    original_jobs = {
        job.public_id: (job.status, dict(cast(dict[str, Any], job.input_json)))
        for job in _planned_jobs(session_factory, replay.id)
    }

    with session_factory.begin() as session:
        player = session.scalar(select(Player).where(Player.public_id == subjects[0][1]))
        assert player is not None
        player.identity_revision += 1
    second = planner.ensure_analysis_plan(replay.public_id, False)

    assert second.derive_job_public_id != first.derive_job_public_id
    assert second.assess_job_public_id != first.assess_job_public_id
    assert second.report_job_public_id != first.report_job_public_id
    jobs = _planned_jobs(session_factory, replay.id)
    assert len(jobs) == 6
    assert {
        job.public_id: (job.status, dict(cast(dict[str, Any], job.input_json)))
        for job in jobs
        if job.public_id in original_jobs
    } == original_jobs
    latest_derive = next(job for job in jobs if job.public_id == second.derive_job_public_id)
    scope = cast(dict[str, Any], cast(dict[str, Any], latest_derive.input_json)["identity_scope"])
    binding = next(item for item in scope["bindings"] if item["player_public_id"] == subjects[0][1])
    assert scope["kind"] == "full_replay"
    assert binding["identity_revision"] == 1
    assert binding["identity_cache_token"] != ""


def test_committed_merge_plans_only_affected_player_work_and_reuses_replay_wide_evidence(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    subjects = _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)
    identity = PlayerIdentityService(session_factory, now_factory=lambda: clock)
    receipt = identity.merge_players(
        target_player_public_id=subjects[0][1],
        source_player_public_ids=(subjects[1][1],),
        expected_revisions={subjects[0][1]: 0, subjects[1][1]: 0},
        actor="local operator",
        reason="same observed player",
    )

    plan = _planner(session_factory, clock).ensure_identity_invalidation_plan(receipt.operation_public_id)

    assert plan.operation_public_id == receipt.operation_public_id
    assert len(plan.replays) == 1
    replay_plan = plan.replays[0]
    assert replay_plan.status == "planned"
    assert replay_plan.replay_player_public_ids == tuple(sorted((subjects[0][0], subjects[1][0])))
    assert replay_plan.llm_job_public_id is None
    jobs = _planned_jobs(session_factory, replay.id)
    assert {job.stage for job in jobs} == {DERIVE_FEATURES, ASSESS_STRATEGIES, RENDER_REPORT}
    for job in jobs:
        scope = cast(dict[str, Any], cast(dict[str, Any], job.input_json)["identity_scope"])
        assert scope["kind"] == "identity_invalidation"
        assert tuple(item["replay_player_public_id"] for item in scope["bindings"]) == tuple(
            sorted((subjects[0][0], subjects[1][0]))
        )
        assert subjects[2][0] not in str(scope)


def test_identity_invalidation_requires_a_committed_audit_operation(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)

    with pytest.raises(AnalysisPlanningError, match="unknown_identity_operation"):
        _planner(session_factory, clock).ensure_identity_invalidation_plan(
            "00000000-0000-4000-8000-00000000b999"
        )

    assert _planned_jobs(session_factory, replay.id) == []


def test_identity_invalidation_planning_rolls_back_as_one_graph_and_is_retryable(
    session_factory: sessionmaker[Session], clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay = _replay(session_factory, clock)
    subjects = _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)
    receipt = PlayerIdentityService(session_factory, now_factory=lambda: clock).merge_players(
        target_player_public_id=subjects[0][1],
        source_player_public_ids=(subjects[1][1],),
        expected_revisions={subjects[0][1]: 0, subjects[1][1]: 0},
        actor="local operator",
        reason="same observed player",
    )
    original = JobCoordinator.ensure_dependency
    calls = 0

    def fail_second_edge(
        coordinator: JobCoordinator,
        session: Session,
        job_id: int,
        depends_on_job_id: int,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected invalidation planning failure")
        original(coordinator, session, job_id, depends_on_job_id)

    monkeypatch.setattr(JobCoordinator, "ensure_dependency", fail_second_edge)
    with pytest.raises(RuntimeError, match="injected invalidation planning failure"):
        _planner(session_factory, clock).ensure_identity_invalidation_plan(receipt.operation_public_id)
    assert _planned_jobs(session_factory, replay.id) == []

    monkeypatch.setattr(JobCoordinator, "ensure_dependency", original)
    retried = _planner(session_factory, clock).ensure_identity_invalidation_plan(receipt.operation_public_id)
    assert retried.replays[0].status == "planned"
    assert len(_planned_jobs(session_factory, replay.id)) == 3


def test_concurrent_identity_invalidation_reuses_one_exact_scoped_graph(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    subjects = _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)
    receipt = PlayerIdentityService(session_factory, now_factory=lambda: clock).merge_players(
        target_player_public_id=subjects[0][1],
        source_player_public_ids=(subjects[1][1],),
        expected_revisions={subjects[0][1]: 0, subjects[1][1]: 0},
        actor="local operator",
        reason="same observed player",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        plans = tuple(
            future.result()
            for future in (
                executor.submit(
                    _planner(session_factory, clock).ensure_identity_invalidation_plan,
                    receipt.operation_public_id,
                )
                for _ in range(2)
            )
        )

    assert plans[0] == plans[1]
    assert len(_planned_jobs(session_factory, replay.id)) == 3


def test_inverse_identity_operation_gets_new_revision_bound_jobs_without_rewriting_merge_history(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    subjects = _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)
    identity = PlayerIdentityService(session_factory, now_factory=lambda: clock)
    merged = identity.merge_players(
        target_player_public_id=subjects[0][1],
        source_player_public_ids=(subjects[1][1],),
        expected_revisions={subjects[0][1]: 0, subjects[1][1]: 0},
        actor="local operator",
        reason="same observed player",
    )
    merge_plan = _planner(session_factory, clock).ensure_identity_invalidation_plan(
        merged.operation_public_id
    )
    merge_jobs = {
        job.public_id: dict(cast(dict[str, Any], job.input_json))
        for job in _planned_jobs(session_factory, replay.id)
    }
    inverse = identity.inverse_operation(
        operation_public_id=merged.operation_public_id,
        expected_revisions=dict(merged.affected_player_revisions),
        actor="local operator",
        reason="undo mistaken merge",
    )

    inverse_plan = _planner(session_factory, clock).ensure_identity_invalidation_plan(
        inverse.operation_public_id
    )

    assert inverse_plan.replays[0].derive_job_public_id != merge_plan.replays[0].derive_job_public_id
    jobs = _planned_jobs(session_factory, replay.id)
    assert len(jobs) == 6
    assert {
        job.public_id: dict(cast(dict[str, Any], job.input_json))
        for job in jobs
        if job.public_id in merge_jobs
    } == merge_jobs


def test_split_identity_operation_plans_only_explicitly_changed_membership(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    subjects = _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)
    alias_public_id = "00000000-0000-4000-8000-00000000b190"
    with session_factory.begin() as session:
        player_id = session.scalar(select(Player.id).where(Player.public_id == subjects[0][1]))
        assert player_id is not None
        session.add(
            PlayerAlias(
                public_id=alias_public_id,
                player_id=player_id,
                namespace=EMBEDDED_REPLAY_NAME_NAMESPACE,
                normalized_name="identity player 0",
                original_name="Identity Player 0",
                external_subject=None,
                created_at=clock,
            )
        )
    split = PlayerIdentityService(session_factory, now_factory=lambda: clock).split_alias(
        alias_public_id=alias_public_id,
        replay_player_public_ids=(subjects[0][0],),
        new_display_name="Separated Identity",
        expected_revisions={subjects[0][1]: 0},
        actor="local operator",
        reason="explicit replay membership differs",
    )

    plan = _planner(session_factory, clock).ensure_identity_invalidation_plan(split.operation_public_id)

    assert plan.replays[0].replay_player_public_ids == (subjects[0][0],)
    assert len(_planned_jobs(session_factory, replay.id)) == 3


def test_identity_invalidation_scope_uses_only_authoritative_observation_membership(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    subjects = _identity_subjects(session_factory, clock, replay)
    _observation_authority(session_factory, clock, replay)
    historical_replay_player_id = "00000000-0000-4000-8000-00000000b199"
    with session_factory.begin() as session:
        player_id = session.scalar(select(Player.id).where(Player.public_id == subjects[0][1]))
        assert player_id is not None
        parser = ParserRun(
            run_id="00000000-0000-4000-8000-00000000b198",
            replay_id=replay.id,
            parser_version="historical-parser-v1",
            schema_version=1,
            input_sha256=replay.sha256,
            result_sha256="c" * 64,
            status="running",
            completion_status="complete",
            command_stream_offset=0,
            end_offset=1,
            warnings_json=[],
            error_json=None,
            started_at=clock,
            completed_at=clock,
        )
        session.add(parser)
        session.flush()
        session.add(
            ReplayPlayer(
                public_id=historical_replay_player_id,
                replay_id=replay.id,
                parser_run_id=parser.id,
                player_id=player_id,
                slot_index=0,
                slot_kind="human",
                original_name="Historical Player",
                normalized_name="historical player",
                player_index=0,
                team_id=None,
                faction="China",
                color=None,
                start_position=0,
                result=None,
                observed_json={},
            )
        )
        session.flush()
        parser.status = "succeeded"
    merged = PlayerIdentityService(session_factory, now_factory=lambda: clock).merge_players(
        target_player_public_id=subjects[0][1],
        source_player_public_ids=(subjects[1][1],),
        expected_revisions={subjects[0][1]: 0, subjects[1][1]: 0},
        actor="local operator",
        reason="same observed player",
    )

    plan = _planner(session_factory, clock).ensure_identity_invalidation_plan(merged.operation_public_id)

    assert plan.replays[0].replay_player_public_ids == tuple(sorted((subjects[0][0], subjects[1][0])))
    assert historical_replay_player_id not in plan.replays[0].replay_player_public_ids


def test_exact_production_telemetry_branch_is_authoritative(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _telemetry_observation_authority(
        session_factory,
        clock,
        replay,
        cross_branch=False,
    )

    plan = _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert plan.status == "planned"
    assert len(_planned_jobs(session_factory, replay.id)) == 3


def test_opt_in_reuses_deterministic_jobs_and_changes_report_identity_and_dependencies(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(session_factory, clock, replay)
    planner = _planner(session_factory, clock)
    without_llm = planner.ensure_analysis_plan(replay.public_id, False)

    with_llm = planner.ensure_analysis_plan(replay.public_id, True)
    jobs = _planned_jobs(session_factory, replay.id)

    assert with_llm.derive_job_public_id == without_llm.derive_job_public_id
    assert with_llm.assess_job_public_id == without_llm.assess_job_public_id
    assert with_llm.llm_job_public_id is not None
    assert with_llm.report_job_public_id != without_llm.report_job_public_id
    assert sum(job.stage == DERIVE_FEATURES for job in jobs) == 1
    assert sum(job.stage == ASSESS_STRATEGIES for job in jobs) == 1
    assert sum(job.stage == ANALYZE_LLM for job in jobs) == 1
    assert sum(job.stage == RENDER_REPORT for job in jobs) == 2
    assert _dependencies(session_factory, with_llm.llm_job_public_id) == {ASSESS_STRATEGIES}
    assert _dependencies(session_factory, cast(str, with_llm.report_job_public_id)) == {ANALYZE_LLM}


def test_plan_inputs_are_path_and_endpoint_free(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(session_factory, clock, replay)
    _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, True)

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = key.lower()
                assert "path" not in lowered
                assert "endpoint" not in lowered
                assert "url" not in lowered
                assert "host" not in lowered
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
        elif isinstance(value, str):
            assert "\\" not in value and "/" not in value and "://" not in value

    for job in _planned_jobs(session_factory, replay.id):
        inspect(job.input_json)


def test_multiple_succeeded_observation_authorities_fail_closed_before_writes(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(session_factory, clock, replay, branch="first")
    _observation_authority(session_factory, clock, replay, branch="second")

    with pytest.raises(AnalysisPlanningError, match="ambiguous_observation_graph"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert _planned_jobs(session_factory, replay.id) == []


def test_malformed_succeeded_observation_authority_fails_closed(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    observation = _observation_authority(session_factory, clock, replay)
    with session_factory.begin() as session:
        row = session.scalar(select(Job).where(Job.id == observation.id))
        assert row is not None
        damaged = dict(cast(dict[str, Any], row.input_json))
        damaged["selected_dependency_digest"] = "f" * 64
        row.input_json = damaged

    with pytest.raises(AnalysisPlanningError, match="invalid_observation_graph"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert _planned_jobs(session_factory, replay.id) == []


@pytest.mark.parametrize(
    "invalidity",
    (
        "missing_id",
        "missing_row",
        "failed",
        "incomplete",
        "unfinished",
        "cross_replay",
        "historical",
    ),
)
def test_parser_run_authority_must_be_selected_succeeded_complete_and_same_replay_before_writes(
    session_factory: sessionmaker[Session],
    clock: datetime,
    invalidity: str,
) -> None:
    replay = _replay(session_factory, clock)
    authority = _observation_authority(session_factory, clock, replay)
    with session_factory.begin() as session:
        observation = session.get(Job, authority.id)
        assert observation is not None
        output = dict(cast(dict[str, object], observation.output_json))
        if invalidity == "missing_id":
            output.pop("parser_run_id")
            observation.output_json = output
        elif invalidity == "missing_row":
            output["parser_run_id"] = "00000000-0000-4000-8000-00000000ffff"
            observation.output_json = output
        else:
            parser_replay_id = replay.id
            if invalidity == "cross_replay":
                other = Replay(
                    public_id="00000000-0000-4000-8000-00000000a199",
                    sha256="f" * 64,
                    replay_name="other.rep",
                    version_string="1.04",
                    version_number=104,
                    frame_count=10,
                    start_time=0,
                    end_time=10,
                    exe_crc=0,
                    ini_crc=0,
                    map_crc=0,
                    map_name="maps/other.map",
                    seed=2,
                    starting_cash=10000,
                    header_json={},
                    lifecycle_state="engine_verified",
                    created_at=clock,
                    updated_at=clock,
                )
                session.add(other)
                session.flush()
                parser_replay_id = other.id
            status = "failed" if invalidity == "failed" else (
                "running" if invalidity in {"incomplete", "unfinished"} else "succeeded"
            )
            completion_status = (
                "failed" if invalidity == "failed" else (
                    "truncated" if invalidity == "incomplete" else "complete"
                )
            )
            invalid_parser = ParserRun(
                run_id=f"00000000-0000-4000-8000-{0xBA00 + len(invalidity):012x}",
                replay_id=parser_replay_id,
                parser_version=(
                    "parser-historical" if invalidity == "historical" else "parser-primary"
                ),
                schema_version=1,
                input_sha256=replay.sha256,
                result_sha256="e" * 64,
                status=status,
                completion_status=completion_status,
                command_stream_offset=0,
                end_offset=1,
                warnings_json=[],
                error_json=None,
                started_at=clock,
                completed_at=None if invalidity == "unfinished" else clock,
            )
            session.add(invalid_parser)
            session.flush()
            output["parser_run_id"] = invalid_parser.run_id
            observation.output_json = output

    with pytest.raises(AnalysisPlanningError, match="invalid_observation_graph"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert _planned_jobs(session_factory, replay.id) == []


def test_legacy_permissive_branch_recipe_is_rejected_even_when_digest_matches(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(
        session_factory,
        clock,
        replay,
        legacy_recipe=True,
    )

    with pytest.raises(AnalysisPlanningError, match="invalid_observation_graph"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert _planned_jobs(session_factory, replay.id) == []


def test_cross_branch_telemetry_parent_is_rejected_even_when_final_identity_matches(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _telemetry_observation_authority(
        session_factory,
        clock,
        replay,
        cross_branch=True,
    )

    with pytest.raises(AnalysisPlanningError, match="invalid_observation_graph"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert _planned_jobs(session_factory, replay.id) == []


def test_invalid_terminal_dependency_is_translated_to_planning_error(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    observation = _observation_authority(session_factory, clock, replay)
    with session_factory.begin() as session:
        parser = session.scalar(
            select(Job)
            .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
            .where(JobDependency.job_id == observation.id)
        )
        assert parser is not None
        parser.status = "failed"
        parser.output_json = None
        parser.retryable = False
        parser.error_code = None

    with pytest.raises(AnalysisPlanningError, match="invalid_observation_graph"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    assert _planned_jobs(session_factory, replay.id) == []


def test_unknown_replay_fails_without_writes(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    with pytest.raises(AnalysisPlanningError, match="unknown_replay"):
        _planner(session_factory, clock).ensure_analysis_plan(
            "00000000-0000-4000-8000-00000000a999",
            False,
        )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_exact_key_collision_with_incompatible_job_fails_and_rolls_back(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    observation = _observation_authority(session_factory, clock, replay)
    observation_input = cast(dict[str, Any], observation.input_json)
    identity = {
        "analysis_plan_version": 1,
        "observation_job_key": observation.idempotency_key,
        "selected_dependency_digest": observation_input["selected_dependency_digest"],
        "identity_scope": {
            "schema_version": "analysis-identity-scope-v1",
            "kind": "full_replay",
            "bindings": [],
        },
    }
    poisoned_key = content_key(DERIVE_FEATURES, DERIVE_FEATURES_VERSION, replay.sha256, identity)
    coordinator = JobCoordinator(session_factory, clock=lambda: clock)
    with session_factory.begin() as session:
        poisoned = coordinator.ensure_job(
            session,
            JobSpec(
                RENDER_REPORT,
                "1",
                poisoned_key,
                {"replay_public_id": replay.public_id, "replay_sha256": replay.sha256},
                replay.id,
            ),
        )
        poisoned_public_id = poisoned.public_id

    with pytest.raises(AnalysisPlanningError, match="analysis_job_identity_conflict"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, False)

    jobs = _planned_jobs(session_factory, replay.id)
    assert [(job.public_id, job.stage) for job in jobs] == [(poisoned_public_id, RENDER_REPORT)]


def test_legacy_placeholder_jobs_are_never_reused(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(session_factory, clock, replay)
    coordinator = JobCoordinator(session_factory, clock=lambda: clock)
    placeholders: dict[str, str] = {}
    with session_factory.begin() as session:
        for stage in DOWNSTREAM_STAGES:
            placeholder = coordinator.ensure_job(
                session,
                JobSpec(
                    stage,
                    "1",
                    f"legacy-placeholder-{stage}",
                    {"replay_public_id": replay.public_id, "replay_sha256": replay.sha256},
                    replay.id,
                ),
            )
            placeholders[stage] = placeholder.public_id

    plan = _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, True)

    assert plan.derive_job_public_id != placeholders[DERIVE_FEATURES]
    assert plan.assess_job_public_id != placeholders[ASSESS_STRATEGIES]
    assert plan.llm_job_public_id != placeholders[ANALYZE_LLM]
    assert plan.report_job_public_id != placeholders[RENDER_REPORT]


def test_planning_failure_rolls_back_every_new_job(
    session_factory: sessionmaker[Session], clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(session_factory, clock, replay)
    original = JobCoordinator.ensure_dependency
    calls = 0

    def fail_second_edge(
        coordinator: JobCoordinator,
        session: Session,
        job_id: int,
        depends_on_job_id: int,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected planning failure")
        original(coordinator, session, job_id, depends_on_job_id)

    monkeypatch.setattr(JobCoordinator, "ensure_dependency", fail_second_edge)
    with pytest.raises(RuntimeError, match="injected planning failure"):
        _planner(session_factory, clock).ensure_analysis_plan(replay.public_id, True)

    assert _planned_jobs(session_factory, replay.id) == []


def test_concurrent_planning_reuses_one_exact_graph(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay = _replay(session_factory, clock)
    _observation_authority(session_factory, clock, replay)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(
                _planner(session_factory, clock).ensure_analysis_plan,
                replay.public_id,
                True,
            )
            for _ in range(2)
        )
        plans = tuple(future.result() for future in futures)

    assert plans[0] == plans[1]
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(Job).where(
                Job.replay_id == replay.id,
                Job.stage.in_(DOWNSTREAM_STAGES),
            )
        ) == 4
        assert session.scalar(select(func.count()).select_from(JobDependency)) == 5
