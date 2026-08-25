"""Behavior tests for transactional replay intake and public DTO boundaries."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from generals_replay_analyzer.cli import main
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    Job,
    JobDependency,
    JobStageResult,
    ManagedAsset,
    ParserRun,
    Player,
    PlayerAlias,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    ReplayQualityIssue,
    Source,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.importing import (
    AcquisitionDiagnostic,
    ImportRequest,
    ImportService,
    StageExecutionContext,
    StageFailure,
    StageHandlerRegistration,
    TelemetryArtifact,
    TerminalDependencyPolicy,
)
from generals_replay_analyzer.importing import service as importing_service
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import PINNED_REPLAY, MutableClock


def _drain(service: ImportService, *, worker: str = "worker-a", maximum: int = 20) -> None:
    for _ in range(maximum):
        if not service.run_available(worker):
            return
    raise AssertionError("registered import stages did not drain")


def _service(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
    *,
    parser: object | None = None,
    acquirer: object | None = None,
    stage_handlers: Iterable[StageHandlerRegistration] = (),
    acquirer_version: str = "test-acquirer-1",
) -> ImportService:
    if parser is None:
        parser = _successful_parser
    return ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parser,
        telemetry_acquirer=acquirer,
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version=acquirer_version,
        stage_handlers=stage_handlers,
    )


def _successful_parser(path: Path) -> SimpleNamespace:
    assert path.is_file()
    return SimpleNamespace(
        commands=(object(), object()),
        warnings=(SimpleNamespace(code="fixture_warning"),),
        command_stream_offset=32,
        end_offset=96,
        completion_status="complete",
    )


def _rows(factory: sessionmaker[Session], model: type[object]) -> list[object]:
    with factory() as session:
        return list(session.scalars(select(model).order_by(model.id)))  # type: ignore[attr-defined]


def test_external_executor_resolves_private_input_and_persists_result_before_settlement(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    """Catch an executor that returns raw job payloads or reports success before durable result publication."""
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    submission = service.submit(ImportRequest(replay_file))
    worker = "00000000-0000-4000-8000-000000000951"
    control = service.worker_control_port()
    executor = service.stage_executor_port()
    claim = control.claim_next(worker, 30)
    assert claim is not None and claim.job_public_id == submission.discovery_job.public_id

    outcome = executor.execute(claim.job_public_id, claim.execution_public_id)
    assert outcome.status == "succeeded" and outcome.result_public_id is not None
    assert not hasattr(outcome, "input_json")
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.public_id == claim.job_public_id))
        result = session.scalar(select(JobStageResult).where(JobStageResult.public_id == outcome.result_public_id))
        assert job is not None and job.status == "running" and job.attempt_count == 1
        assert result is not None and result.job_id == job.id
    control.settle_success(worker, claim, outcome.result_public_id)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.public_id == claim.job_public_id))
        assert job is not None and job.status == "succeeded" and job.attempt_count == 1


def test_single_file_creates_provenance_lowercase_replay_and_expected_dag(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)

    submission = service.submit(ImportRequest(replay_file))
    assert submission.discovery_job.stage == "discover"
    assert submission.discovery_job.status == "pending"
    _drain(service)

    expected_sha = hashlib.sha256(replay_file.read_bytes()).hexdigest()
    with session_factory() as session:
        source = session.scalar(select(Source))
        replay = session.scalar(select(Replay))
        assert source is not None and replay is not None
        assert source.replay_id == replay.id
        assert source.strata_match_id == "3133811"
        assert source.strata_source_user_token == "ABCDEF"
        assert replay.sha256 == expected_sha == expected_sha.lower()
        assert replay.lifecycle_state == "discovered"
        stages = set(session.scalars(select(Job.stage)))
        assert stages == {
            "discover",
            "hash",
            "manage_copy",
            "parse",
            "import_observations",
            "derive_features",
            "assess_strategies",
            "analyze_llm",
            "render_report",
        }
        dependent_job = aliased(Job)
        dependency_job = aliased(Job)
        edge_stages = {
            (job.stage, dependency.stage)
            for job, dependency in session.execute(
                select(dependent_job, dependency_job)
                .join(JobDependency, JobDependency.job_id == dependent_job.id)
                .join(dependency_job, JobDependency.depends_on_job_id == dependency_job.id)
            )
        }
        assert ("parse", "manage_copy") in edge_stages
        assert ("import_observations", "parse") in edge_stages
        assert ("render_report", "assess_strategies") in edge_stages
        assert ("render_report", "analyze_llm") not in edge_stages

    result = service.result_for_replay(replay.public_id)
    assert result.sha256 == expected_sha
    assert result.source_public_ids == (source.public_id,)
    assert all("\\" not in job.public_id and "/" not in job.public_id for job in result.jobs)


def test_telemetry_request_fails_retryably_before_a_parser_only_graph_is_created(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)

    service.submit(ImportRequest(replay_file, request_telemetry=True))
    outcomes = service.run_available("telemetry-unavailable", limit=2)

    assert tuple((outcome.stage, outcome.status) for outcome in outcomes) == (("discover", "pending"),)
    assert outcomes[-1].error_code == "telemetry_acquirer_unavailable"
    assert outcomes[-1].retryable is True
    with session_factory() as session:
        stages = tuple(session.scalars(select(Job.stage).order_by(Job.id)))
        assert stages == ("discover",)
        assert session.scalar(select(func.count()).select_from(Replay)) == 0


def test_registered_import_observations_runs_after_frozen_dependency_context(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    contexts: list[StageExecutionContext] = []
    returned_output: dict[str, Any] = {
        "summary": {"families": ["parser", "telemetry"]},
        "status": "imported",
    }

    def import_observations(context: StageExecutionContext) -> dict[str, Any]:
        contexts.append(context)
        assert tuple(dependency.stage for dependency in context.dependencies) == ("parse", "telemetry")
        assert context.input["replay_public_id"] == context.replay_public_id
        assert context.input["replay_sha256"] == context.replay_sha256
        assert context.dependencies[0].output["content_sha256"] == context.replay_sha256
        artifacts = context.dependencies[1].output["artifacts"]
        assert isinstance(artifacts, tuple) and artifacts
        with pytest.raises(FrozenInstanceError):
            context.stage = "mutated"  # type: ignore[misc]
        with pytest.raises(TypeError):
            context.input["replay_sha256"] = "f" * 64  # type: ignore[index]
        with pytest.raises(TypeError):
            context.dependencies[0].output["content_sha256"] = "f" * 64  # type: ignore[index]
        with pytest.raises(AttributeError):
            cast(Any, artifacts).append("mutated")
        with pytest.raises(AttributeError):
            cast(Any, context.dependencies).append("mutated")
        return returned_output

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(_artifact(tmp_path)),
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                import_observations,
            ),
        ),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    completed = service.run_available("task-4-worker", limit=10)
    assert tuple(job.stage for job in completed) == (
        "discover",
        "hash",
        "manage_copy",
        "parse",
        "telemetry",
        "import_observations",
    )
    assert len(contexts) == 1
    context = contexts[0]
    assert context.job_public_id == completed[-1].public_id
    assert context.stage == "import_observations"
    assert context.component_version == importing_service.IMPORT_OBSERVATIONS_VERSION
    assert context.replay_public_id == completed[-1].replay_public_id
    assert context.replay_sha256 == hashlib.sha256(replay_file.read_bytes()).hexdigest()

    cast(dict[str, Any], returned_output["summary"])["families"].append("caller-mutation")
    with session_factory() as session:
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert imported is not None and imported.status == "succeeded"
        assert context.idempotency_key == imported.idempotency_key
        assert context.input["selected_dependency_digest"] in imported.idempotency_key
        assert imported.output_json == {
            "status": "imported",
            "summary": {"families": ["parser", "telemetry"]},
        }


def test_registered_stage_never_receives_failed_dependency_output(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    received: list[StageExecutionContext] = []

    def import_observations(context: StageExecutionContext) -> dict[str, str]:
        received.append(context)
        return {"status": "unexpected"}

    def failing_parser(_path: Path) -> SimpleNamespace:
        raise ValueError("fixture parser failure")

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=failing_parser,
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                import_observations,
            ),
        ),
    )
    service.submit(ImportRequest(replay_file))
    completed = service.run_available("task-4-worker", limit=10)
    assert tuple(job.stage for job in completed) == ("discover", "hash", "manage_copy", "parse")
    assert received == []
    with session_factory() as session:
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert imported is not None and imported.status == "failed"
        assert imported.error_code == "dependency_failed"
        assert imported.output_json is None


def test_opted_in_terminal_parse_dependency_is_frozen_redacted_evidence(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch terminal parser evidence being projected away or leaking private mutable state."""
    received: list[StageExecutionContext] = []
    private_path = tmp_path / "private-run" / "source.rep"

    def failing_parser(_path: Path) -> SimpleNamespace:
        raise StageFailure(
            "parser_fixture_failed",
            f"parser failed while reading {private_path}",
            retryable=False,
            details={
                "exception_type": "FixtureParserError",
                "run_path": str(private_path.parent),
                "attempted_at": "2026-08-22T12:00:00Z",
                "nested": {"values": ["retained", str(private_path)]},
            },
        )

    def import_observations(context: StageExecutionContext) -> dict[str, str]:
        received.append(context)
        assert tuple(dependency.stage for dependency in context.dependencies) == ("parse",)
        dependency = context.dependencies[0]
        assert dependency.status == "failed"
        assert dependency.output is None
        assert dependency.error_code == "parser_fixture_failed"
        assert dependency.error_message is not None
        assert str(private_path) not in dependency.error_message
        assert dependency.error_details is not None
        assert set(dependency.error_details) == {"exception_type", "nested"}
        nested = dependency.error_details["nested"]
        assert isinstance(nested, Mapping)
        assert str(private_path) not in repr(dependency)
        with pytest.raises(TypeError):
            dependency.error_details["exception_type"] = "mutated"  # type: ignore[index]
        with pytest.raises(AttributeError):
            cast(Any, cast(Any, nested)["values"]).append("mutated")
        return {"status": "failure-evidence-imported"}

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=failing_parser,
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                import_observations,
                terminal_dependency_policy=TerminalDependencyPolicy(failed_stages=frozenset({"parse"})),
            ),
        ),
    )
    service.submit(ImportRequest(replay_file))
    completed = service.run_available("task-4-worker", limit=10)
    assert tuple(job.stage for job in completed) == (
        "discover",
        "hash",
        "manage_copy",
        "parse",
        "import_observations",
    )
    assert completed[-1].status == "succeeded"
    assert len(received) == 1
    with session_factory() as session:
        parse_job = session.scalar(select(Job).where(Job.stage == "parse"))
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert parse_job is not None and parse_job.status == "failed"
        assert imported is not None and imported.status == "succeeded"
        assert imported.input_json["selected_dependency_digest"] in imported.idempotency_key


def test_exhausted_retryable_parse_materializes_stable_terminal_context_and_identity(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    """Catch exhausted parser failures retaining retry eligibility while downstream evidence executes."""
    received: list[StageExecutionContext] = []

    def unavailable_parser(_path: Path) -> SimpleNamespace:
        raise OSError("parser dependency is temporarily unavailable")

    def import_observations(context: StageExecutionContext) -> dict[str, str]:
        received.append(context)
        dependency = context.dependencies[0]
        assert (dependency.stage, dependency.status, dependency.error_code) == (
            "parse",
            "failed",
            "parser_failed",
        )
        return {"status": "terminal-evidence-imported"}

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=unavailable_parser,
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                import_observations,
                terminal_dependency_policy=TerminalDependencyPolicy(failed_stages=frozenset({"parse"})),
            ),
        ),
    )
    service.submit(ImportRequest(replay_file))
    first = service.run_available("parser-worker", limit=10)
    assert first[-1].stage == "parse"
    assert (first[-1].status, first[-1].retryable, first[-1].attempt_count) == ("pending", True, 1)
    assert received == []

    clock.advance(seconds=5)
    second = service.run_available("parser-worker", limit=10)
    assert len(second) == 1 and second[0].stage == "parse"
    assert (second[0].status, second[0].retryable, second[0].attempt_count) == ("pending", True, 2)
    assert received == []

    clock.advance(seconds=10)
    exhausted = service.run_available("parser-worker", limit=10)
    assert tuple(job.stage for job in exhausted) == ("parse", "import_observations")
    assert (exhausted[0].status, exhausted[0].retryable, exhausted[0].attempt_count) == (
        "failed",
        False,
        3,
    )
    assert len(received) == 1
    with session_factory() as session:
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert imported is not None and imported.status == "succeeded"
        selected_digest = imported.input_json["selected_dependency_digest"]
        assert isinstance(selected_digest, str) and imported.idempotency_key.endswith(selected_digest)
        assert received[0].idempotency_key == imported.idempotency_key


def test_retryable_failed_dependency_cannot_materialize_or_enter_direct_context(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    """Catch materialization and context checks accepting a failed row that remains retryable."""

    def terminal_parser(_path: Path) -> SimpleNamespace:
        raise StageFailure("parser_terminal", "terminal fixture", retryable=False)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=terminal_parser,
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                lambda _context: {"status": "imported"},
                terminal_dependency_policy=TerminalDependencyPolicy(failed_stages=frozenset({"parse"})),
            ),
        ),
    )
    service.submit(ImportRequest(replay_file))
    for _ in range(4):
        completed = service.run_available("parser-worker", limit=1)
        assert len(completed) == 1
    with session_factory.begin() as session:
        dependency = session.scalar(select(Job).where(Job.stage == "parse"))
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert dependency is not None and imported is not None
        assert dependency.status == "failed"
        dependency.retryable = True
        provisional_key = imported.idempotency_key

    service._materialize_ready_observation_jobs()
    with session_factory() as session:
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert imported is not None
        assert imported.idempotency_key == provisional_key
        assert imported.input_json["dependency_identity_bound"] is False

    with session_factory.begin() as session:
        dependency = session.scalar(select(Job).where(Job.stage == "parse"))
        assert dependency is not None
        dependency.retryable = False
    service._materialize_ready_observation_jobs()
    claimed = service._jobs.claim(
        "observation-worker",
        service._handlers,
        terminal_failure_stages=service._terminal_failure_stages,
    )
    assert claimed is not None and claimed.stage == "import_observations"
    with session_factory.begin() as session:
        dependency = session.scalar(select(Job).where(Job.stage == "parse"))
        assert dependency is not None
        dependency.retryable = True
    with pytest.raises(StageFailure) as failure:
        service._stage_execution_context(claimed)
    assert failure.value.code == "dependency_unavailable"


def test_opted_in_terminal_telemetry_dependency_keeps_mixed_success_failure_context(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch mixed parser success and terminal telemetry evidence being flattened to one status."""
    received: list[StageExecutionContext] = []

    def import_observations(context: StageExecutionContext) -> dict[str, str]:
        received.append(context)
        assert tuple((item.stage, item.status) for item in context.dependencies) == (
            ("parse", "succeeded"),
            ("telemetry", "failed"),
        )
        parsed, telemetry = context.dependencies
        assert parsed.output is not None and parsed.error_code is None
        assert telemetry.output is None and telemetry.error_code == "exporter_failure"
        assert telemetry.error_details is not None
        artifacts = telemetry.error_details["artifacts"]
        assert isinstance(artifacts, tuple) and artifacts
        assert cast(Any, artifacts[0])["logical_path"] == "stdout.log"
        with pytest.raises(TypeError):
            cast(Any, artifacts[0])["logical_path"] = "changed.log"
        return {"status": "mixed-evidence-imported"}

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(_artifact(tmp_path, status="nonzero_engine_failure")),
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                import_observations,
                terminal_dependency_policy=TerminalDependencyPolicy(
                    failed_stages=frozenset({"parse", "telemetry"})
                ),
            ),
        ),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    completed = service.run_available("task-4-worker", limit=10)
    assert tuple(job.stage for job in completed)[-2:] == ("telemetry", "import_observations")
    assert completed[-2].status == "failed"
    assert completed[-1].status == "succeeded"
    assert len(received) == 1


def test_terminal_failure_evidence_changes_materialized_identity_deterministically(
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch failed dependency status/details being omitted from the durable observation identity."""
    keys: list[str] = []
    variants = (
        ("first", "parser_fixture_failed", "fixture failure", {"reason": "alpha"}),
        ("changed", "parser_fixture_failed", "fixture failure", {"reason": "beta"}),
        ("repeat", "parser_fixture_failed", "fixture failure", {"reason": "alpha"}),
    )

    def failing_parser_for(
        failure_code: str,
        failure_message: str,
        failure_details: dict[str, str],
    ) -> Callable[[Path], SimpleNamespace]:
        def fail(_path: Path) -> SimpleNamespace:
            raise StageFailure(
                failure_code,
                failure_message,
                retryable=False,
                details=failure_details,
            )

        return fail

    for label, code, message, details in variants:
        root = tmp_path / label
        configured = AnalyzerSettings(data_root=root / "product-data")
        configured.ensure_directories()
        upgrade_database(configured.database_path)
        engine = create_database_engine(configured.database_path)
        factory = create_session_factory(engine)
        replay = root / "same.rep"
        replay.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PINNED_REPLAY, replay)

        try:
            service = _service(
                factory,
                configured,
                ContentAddressedStore(configured.managed_replay_directory),
                ContentAddressedStore(configured.cache_directory / "artifacts"),
                clock,
                parser=failing_parser_for(code, message, details),
                stage_handlers=(
                    StageHandlerRegistration(
                        "import_observations",
                        importing_service.IMPORT_OBSERVATIONS_VERSION,
                        lambda _context: {"status": "imported"},
                        terminal_dependency_policy=TerminalDependencyPolicy(
                            failed_stages=frozenset({"parse"})
                        ),
                    ),
                ),
            )
            service.submit(ImportRequest(replay))
            completed = service.run_available(f"{label}-worker", limit=10)
            assert completed[-1].stage == "import_observations" and completed[-1].status == "succeeded"
            with factory() as session:
                imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
                assert imported is not None
                keys.append(imported.idempotency_key)
        finally:
            engine.dispose()

    assert keys[0] != keys[1]
    assert keys[0] == keys[2]


def test_registered_stage_uses_existing_typed_failure_and_retry_semantics(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    def import_observations(context: StageExecutionContext) -> dict[str, str]:
        raise StageFailure(
            "observation_validation_failed",
            "fixture observation validation failed",
            retryable=False,
            details={"dependency_count": len(context.dependencies)},
        )

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                import_observations,
            ),
        ),
    )
    service.submit(ImportRequest(replay_file))
    completed = service.run_available("task-4-worker", limit=10)
    assert completed[-1].stage == "import_observations"
    assert completed[-1].status == "failed"
    assert completed[-1].error_code == "observation_validation_failed"
    assert completed[-1].retryable is False
    with session_factory() as session:
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert imported is not None
        assert imported.error_details_json == {"dependency_count": 1}


def test_registered_stage_omits_absent_optional_dependency_and_copies_configuration(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    received: list[StageExecutionContext] = []

    def import_observations(context: StageExecutionContext) -> dict[str, str]:
        received.append(context)
        return {"status": "imported"}

    registrations = [
        StageHandlerRegistration(
            "import_observations",
            importing_service.IMPORT_OBSERVATIONS_VERSION,
            import_observations,
        )
    ]
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        stage_handlers=registrations,
    )
    registrations.clear()
    service.submit(ImportRequest(replay_file))
    completed = service.run_available("task-4-worker", limit=10)
    assert completed[-1].stage == "import_observations"
    assert len(received) == 1
    assert tuple(dependency.stage for dependency in received[0].dependencies) == ("parse",)


def test_logical_topology_materializes_distinct_observation_branch_identity(
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    def recording_handler(
        received: list[StageExecutionContext],
    ) -> Callable[[StageExecutionContext], dict[str, str]]:
        def handle(context: StageExecutionContext) -> dict[str, str]:
            received.append(context)
            return {"status": "imported"}

        return handle

    keys: list[str] = []
    manifests: list[list[tuple[str, str, int]]] = []
    logical_paths: list[list[str]] = []
    for name, trace_name in (
        ("first", "trace-a.ndjson"),
        ("second", "trace-b.ndjson"),
        ("repeat", "trace-a.ndjson"),
    ):
        side_root = tmp_path / name
        configured = AnalyzerSettings(data_root=side_root / "product-data")
        configured.ensure_directories()
        upgrade_database(configured.database_path)
        engine = create_database_engine(configured.database_path)
        factory = create_session_factory(engine)
        replay = side_root / "same.rep"
        replay.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PINNED_REPLAY, replay)
        artifact = _bundle_artifact(side_root / "bundle", trace_name=trace_name)
        contexts: list[StageExecutionContext] = []

        try:
            service = _service(
                factory,
                configured,
                ContentAddressedStore(configured.managed_replay_directory),
                ContentAddressedStore(configured.cache_directory / "artifacts"),
                clock,
                acquirer=_FakeAcquirer(artifact),
                    stage_handlers=(
                        StageHandlerRegistration(
                            "import_observations",
                            importing_service.IMPORT_OBSERVATIONS_VERSION,
                            recording_handler(contexts),
                        ),
                    ),
            )
            service.submit(ImportRequest(replay, request_telemetry=True))
            completed = service.run_available(f"{name}-worker", limit=10)
            assert completed[-1].stage == "import_observations" and completed[-1].status == "succeeded"
            repeated_source = side_root / "same-second-source.rep"
            shutil.copyfile(PINNED_REPLAY, repeated_source)
            service.submit(ImportRequest(repeated_source, request_telemetry=True))
            assert tuple(job.stage for job in service.run_available(f"{name}-repeat", limit=10)) == ("discover",)
            assert len(contexts) == 1
            with factory() as session:
                imports = list(session.scalars(select(Job).where(Job.stage == "import_observations")))
                assert len(imports) == 1
                imported = imports[0]
                telemetry = session.scalar(select(Job).where(Job.stage == "telemetry"))
                assert imported is not None and telemetry is not None
                keys.append(imported.idempotency_key)
                assert contexts[0].idempotency_key == imported.idempotency_key
                artifact_manifest = cast(dict[str, Any], telemetry.output_json)["artifacts"]
                manifests.append(
                    sorted((entry["kind"], entry["sha256"], entry["size_bytes"]) for entry in artifact_manifest)
                )
                logical_paths.append([entry["logical_path"] for entry in artifact_manifest])
        finally:
            engine.dispose()

    assert manifests[0] == manifests[1] == manifests[2]
    assert logical_paths[0] != logical_paths[1]
    assert logical_paths[0] == logical_paths[2]
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]


def test_new_observation_version_requeues_without_mutating_exhausted_history(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch an observation importer fix that cannot create a fresh durable job after v1 exhausted."""
    current_version = importing_service.IMPORT_OBSERVATIONS_VERSION
    monkeypatch.setattr(importing_service, "IMPORT_OBSERVATIONS_VERSION", "1")
    original_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    original_service.submit(ImportRequest(replay_file))
    _drain(original_service, worker="observation-v1")

    with session_factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert original is not None
        original.status = "failed"
        original.attempt_count = original.max_attempts
        original.completed_at = clock()
        original.error_code = "import_failed"
        original.error_message = "legacy observation importer exhausted"
        original.error_details_json = {}
        original.retryable = False
        original_identity = (original.public_id, original.idempotency_key)

    monkeypatch.setattr(importing_service, "IMPORT_OBSERVATIONS_VERSION", current_version)
    repeated_source = tmp_path / "same-replay-new-observation-version.rep"
    shutil.copyfile(replay_file, repeated_source)
    recovery_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    recovery_service.submit(ImportRequest(repeated_source))
    _drain(recovery_service, worker="observation-current")

    with session_factory() as session:
        imports = list(
            session.scalars(select(Job).where(Job.stage == "import_observations").order_by(Job.id))
        )
        assert [(job.component_version, job.status) for job in imports] == [
            ("1", "failed"),
            ("2", "pending"),
        ]
        assert (imports[0].public_id, imports[0].idempotency_key) == original_identity


def test_new_report_version_requeues_without_mutating_exhausted_history(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a report-authority fix that cannot create a fresh durable job after v4 exhausted."""
    current_version = importing_service.RENDER_REPORT_VERSION
    monkeypatch.setattr(importing_service, "RENDER_REPORT_VERSION", "4")
    original_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    original_service.submit(ImportRequest(replay_file))
    _drain(original_service, worker="report-v4")

    with session_factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == "render_report"))
        assert original is not None
        original.status = "failed"
        original.attempt_count = original.max_attempts
        original.completed_at = clock()
        original.error_code = "render_report_failed"
        original.error_message = "legacy report authority selection exhausted"
        original.error_details_json = {}
        original.retryable = False
        original_identity = (original.public_id, original.idempotency_key)

    monkeypatch.setattr(importing_service, "RENDER_REPORT_VERSION", current_version)
    repeated_source = tmp_path / "same-replay-new-report-version.rep"
    shutil.copyfile(replay_file, repeated_source)
    recovery_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    recovery_service.submit(ImportRequest(repeated_source))
    _drain(recovery_service, worker="report-current")

    with session_factory() as session:
        reports = list(session.scalars(select(Job).where(Job.stage == "render_report").order_by(Job.id)))
        assert [(job.component_version, job.status) for job in reports] == [
            ("4", "failed"),
            ("5", "pending"),
        ]
        assert (reports[0].public_id, reports[0].idempotency_key) == original_identity


def test_converged_materialized_jobs_coalesce_without_losing_downstream_edges(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    bootstrap = _service(session_factory, settings, replay_store, artifact_store, clock)
    bootstrap.submit(ImportRequest(replay_file))
    _drain(bootstrap, worker="bootstrap")
    with session_factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == "import_observations"))
        derive = session.scalar(select(Job).where(Job.stage == "derive_features"))
        assert original is not None and derive is not None and original.status == "pending"
        duplicate = Job(
            public_id=str(uuid4()),
            replay_id=original.replay_id,
            stage=original.stage,
            component_version=original.component_version,
            idempotency_key=f"import_observations:{original.component_version}:{cast(Replay, session.get(Replay, original.replay_id)).sha256}:{'f' * 64}",
            status="pending",
            priority=original.priority,
            attempt_count=0,
            max_attempts=original.max_attempts,
            available_at=clock(),
            lease_owner=None,
            lease_expires_at=None,
            started_at=None,
            completed_at=None,
            input_json=dict(cast(dict[str, Any], original.input_json)),
            output_json=None,
            error_code=None,
            error_message=None,
            error_details_json=None,
            retryable=True,
        )
        session.add(duplicate)
        session.flush()
        dependency_ids = list(
            session.scalars(
                select(JobDependency.depends_on_job_id).where(JobDependency.job_id == original.id)
            )
        )
        for dependency_id in dependency_ids:
            session.add(
                JobDependency(job_id=duplicate.id, depends_on_job_id=dependency_id, created_at=clock())
            )
        session.add(JobDependency(job_id=derive.id, depends_on_job_id=duplicate.id, created_at=clock()))

    contexts: list[StageExecutionContext] = []

    def import_observations(context: StageExecutionContext) -> dict[str, str]:
        contexts.append(context)
        return {"status": "imported"}

    resumed = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                importing_service.IMPORT_OBSERVATIONS_VERSION,
                import_observations,
            ),
        ),
    )
    completed = resumed.run_available("materializer", limit=2)
    assert tuple(job.stage for job in completed) == ("import_observations",)
    assert len(contexts) == 1
    with session_factory() as session:
        imports = list(session.scalars(select(Job).where(Job.stage == "import_observations")))
        assert len(imports) == 1 and imports[0].status == "succeeded"
        derive = session.scalar(select(Job).where(Job.stage == "derive_features"))
        assert derive is not None
        derive_dependencies = set(
            session.scalars(
                select(JobDependency.depends_on_job_id).where(JobDependency.job_id == derive.id)
            )
        )
        assert imports[0].id in derive_dependencies
        assert all(job.error_code is None for job in imports)


def test_stage_handler_registration_rejects_invalid_or_ambiguous_contracts(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    def handler(_context: StageExecutionContext) -> dict[str, str]:
        return {"status": "unused"}

    cases = (
        ((StageHandlerRegistration("parse", "1", handler),), "built_in_stage"),
        ((StageHandlerRegistration("private_task_4_stage", "1", handler),), "unknown_stage"),
        ((StageHandlerRegistration("import_observations", "999", handler),), "version_mismatch"),
        (
            (
                StageHandlerRegistration(
                    "import_observations",
                    importing_service.IMPORT_OBSERVATIONS_VERSION,
                    handler,
                    terminal_dependency_policy=TerminalDependencyPolicy(
                        failed_stages=frozenset({"*"})
                    ),
                ),
            ),
            "unknown_terminal_dependency_stage",
        ),
        (
            (
                StageHandlerRegistration(
                    "import_observations",
                    importing_service.IMPORT_OBSERVATIONS_VERSION,
                    handler,
                    terminal_dependency_policy=TerminalDependencyPolicy(
                        failed_stages=frozenset({"manage_copy"})
                    ),
                ),
            ),
            "non_direct_terminal_dependency_stage",
        ),
        (
            (
                StageHandlerRegistration(
                    "import_observations",
                    importing_service.IMPORT_OBSERVATIONS_VERSION,
                    handler,
                ),
                StageHandlerRegistration(
                    "import_observations",
                    importing_service.IMPORT_OBSERVATIONS_VERSION,
                    handler,
                ),
            ),
            "duplicate_stage",
        ),
    )
    for registrations, error_code in cases:
        with pytest.raises(ValueError, match=error_code):
            _service(
                session_factory,
                settings,
                replay_store,
                artifact_store,
                clock,
                stage_handlers=registrations,
            )
    with pytest.raises(TypeError, match="invalid_terminal_dependency_policy"):
        _service(
            session_factory,
            settings,
            replay_store,
            artifact_store,
            clock,
            stage_handlers=(
                StageHandlerRegistration(
                    "import_observations",
                    importing_service.IMPORT_OBSERVATIONS_VERSION,
                    handler,
                    terminal_dependency_policy=cast(Any, frozenset({"parse"})),
                ),
            ),
        )


def test_default_service_keeps_future_observation_stage_pending(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit(ImportRequest(replay_file))
    completed = service.run_available("task-3-worker", limit=10)
    assert tuple(job.stage for job in completed) == ("discover", "hash", "manage_copy", "parse")
    with session_factory() as session:
        imported = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert imported is not None and imported.status == "pending"


def test_folder_snapshot_filters_sorts_and_respects_recursion(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    folder = tmp_path / "snapshot"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    shutil.copyfile(PINNED_REPLAY, folder / "b.REP")
    shutil.copyfile(PINNED_REPLAY, folder / "A.rep")
    shutil.copyfile(PINNED_REPLAY, nested / "c.rep")
    (folder / "notes.txt").write_text("not a replay", encoding="utf-8")
    symlink = folder / "linked.rep"
    symlink_created = True
    try:
        symlink.symlink_to(folder / "A.rep")
    except OSError:
        symlink_created = False

    first = _service(session_factory, settings, replay_store, artifact_store, clock)
    first.submit(ImportRequest(folder))
    first.run_available("discovery-only")
    with session_factory() as session:
        names = list(session.scalars(select(Source.original_filename).order_by(Source.id)))
        discovery = session.scalar(select(Job).where(Job.stage == "discover"))
        assert discovery is not None
        assert names == ["A.rep", "b.REP"]
        assert discovery.output_json["accepted_path_count"] == 2
        assert discovery.output_json["rejected_path_count"] >= 1 + int(symlink_created)

    second = _service(session_factory, settings, replay_store, artifact_store, clock)
    second.submit(ImportRequest(folder, recursive=True))
    _drain(second, worker="recursive-discovery")
    with session_factory() as session:
        names = list(session.scalars(select(Source.original_filename).order_by(Source.id)))
        assert names[-3:] == ["A.rep", "b.REP", "c.rep"]


def test_duplicate_bytes_keep_distinct_sources_and_coalesce_content_jobs(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    first_path = tmp_path / "first.rep"
    second_path = tmp_path / "second.rep"
    shutil.copyfile(PINNED_REPLAY, first_path)
    shutil.copyfile(PINNED_REPLAY, second_path)
    service = _service(session_factory, settings, replay_store, artifact_store, clock)

    service.submit(ImportRequest(first_path))
    _drain(service)
    service.submit(ImportRequest(second_path))
    _drain(service)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 2
        assert session.scalar(select(func.count()).select_from(Replay)) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.stage == "hash")) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.stage == "parse")) == 1
        assert len(set(session.scalars(select(Source.replay_id)))) == 1


def test_copy_survives_source_deletion_but_reference_mode_reports_source_missing(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    copied = tmp_path / "copied.rep"
    shutil.copyfile(PINNED_REPLAY, copied)
    copy_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    copy_service.submit(ImportRequest(copied))
    copy_service.run_available("copy")  # discover
    copy_service.run_available("copy")  # hash
    copy_service.run_available("copy")  # manage_copy
    copied.unlink()
    parsed = copy_service.run_available("copy")
    assert parsed[0].stage == "parse" and parsed[0].status == "succeeded"
    with session_factory() as session:
        replay = session.scalar(select(Replay))
        asset = session.scalar(select(ManagedAsset).where(ManagedAsset.kind == "replay"))
        assert replay is not None and asset is not None
        assert replay.managed_asset_id == asset.id
        assert not Path(asset.relative_path).is_absolute()
        assert "\\" not in asset.relative_path

    referenced = tmp_path / "referenced.rep"
    referenced.write_bytes(b"distinct-reference-replay")
    reference_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    reference_service.submit(ImportRequest(referenced, reference_only=True))
    reference_service.run_available("reference")
    reference_service.run_available("reference")
    reference_service.run_available("reference")
    referenced.unlink()
    failed = reference_service.run_available("reference")
    assert failed[0].stage == "parse"
    assert failed[0].status == "failed"
    assert failed[0].error_code == "source_missing"
    with session_factory() as session:
        reference_replay = session.scalar(select(Replay).where(Replay.sha256 == hashlib.sha256(b"distinct-reference-replay").hexdigest()))
        assert reference_replay is not None and reference_replay.managed_asset_id is None


def test_parser_failure_retains_job_diagnostics_and_zero_observations(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    def broken_parser(path: Path) -> object:
        raise OSError(f"fixture parser cannot read {path.name}")

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=broken_parser,
    )
    service.submit(ImportRequest(replay_file))
    for _ in range(4):
        jobs = service.run_available("parser-worker")
    assert jobs[0].stage == "parse"
    assert jobs[0].status == "pending"
    assert jobs[0].retryable is True
    assert jobs[0].error_code == "parser_failed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ParserRun)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayCommand)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayPlayer)) == 0


class _FakeAcquirer:
    def __init__(self, artifact: TelemetryArtifact) -> None:
        self.artifact = artifact

    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
        assert replay.is_file()
        assert replay_sha256 == replay_sha256.lower()
        return self.artifact


class _MutatingAcquirer:
    def __init__(self, artifact: TelemetryArtifact) -> None:
        self.artifact = artifact

    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
        replay.write_bytes(replay.read_bytes() + b"telemetry-mutation")
        return self.artifact


def _artifact(tmp_path: Path, *, status: str = "success") -> TelemetryArtifact:
    trace = tmp_path / "trace.ndjson"
    stdout = tmp_path / "stdout.log"
    trace.write_text('{"type":"fixture"}\n', encoding="utf-8")
    stdout.write_text("fixture output", encoding="utf-8")
    return TelemetryArtifact(
        run_id=str(uuid4()),
        runner_status=status,
        replay_quality="complete" if status == "success" else "failed",
        strategy_analysis_scope="full" if status == "success" else "none",
        trace_path=trace if status == "success" else None,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=None,
        stdout_path=stdout,
        stderr_path=None,
        exit_code=0 if status == "success" else 7,
        engine_build="fixture-build",
        engine_executable_sha256="a" * 64,
        diagnostics=(AcquisitionDiagnostic("fixture", "diagnostic retained"),),
    )


def _bundle_artifact(
    root: Path,
    *,
    trace_name: str = "trace.ndjson",
    run_id: str = "123e4567-e89b-12d3-a456-426614174000",
) -> TelemetryArtifact:
    root.mkdir(parents=True, exist_ok=True)
    trace = root / trace_name
    trace.write_bytes(b'{"schema_version":2,"type":"fixture"}\n')
    catalog_bytes = b'{"schema_version":1,"type":"game_data_catalog"}\n'
    catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    catalog = root / f"game-data-catalog-v1-{catalog_sha256}.json"
    catalog.write_bytes(catalog_bytes)
    map_content_sha256 = "b" * 64
    map_root = root / "map-assets-v2" / map_content_sha256
    map_root.mkdir(parents=True)
    map_members = {
        "manifest.json": b'{"schema_version":2,"type":"map_asset"}\n',
        "height.f32.zlib": b"height-map-fixture",
        "pathing-amphibious.u8.zlib": b"amphibious-pathing-fixture",
        "pathing-ground.u8.zlib": b"ground-pathing-fixture",
        "terrain.u8.zlib": b"terrain-fixture",
        "zones.i32.zlib": b"zones-fixture",
    }
    map_paths = tuple(map_root / name for name in reversed(tuple(map_members)))
    for path in map_paths:
        path.write_bytes(map_members[path.name])
    outcome = root / "replay-outcome.json"
    stdout = root / "stdout.log"
    stderr = root / "stderr.log"
    outcome.write_bytes(b'{"terminal_reason":"complete"}\n')
    stdout.write_bytes(b"fixture stdout")
    stderr.write_bytes(b"fixture stderr")
    return TelemetryArtifact(
        run_id=run_id,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        trace_path=trace,
        catalog_path=catalog,
        map_asset_paths=map_paths,
        outcome_path=outcome,
        stdout_path=stdout,
        stderr_path=stderr,
        exit_code=0,
        engine_build="fixture-build",
        engine_executable_sha256="a" * 64,
        diagnostics=(),
    )


def test_v2_bundle_manifest_retains_safe_loader_relative_topology(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    bundle_root = tmp_path / "private-run-root"
    artifact = replace(
        _bundle_artifact(bundle_root),
        diagnostics=(AcquisitionDiagnostic("fixture", f"retained from {bundle_root / 'trace.ndjson'}"),),
    )
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(artifact),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)

    with session_factory() as session:
        telemetry = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert telemetry is not None and telemetry.status == "succeeded"
        manifest = cast(dict[str, Any], telemetry.output_json)["artifacts"]
        logical_paths = [entry["logical_path"] for entry in manifest]
        assert logical_paths == [
            artifact.catalog_path.name,
            f"map-assets-v2/{'b' * 64}/height.f32.zlib",
            f"map-assets-v2/{'b' * 64}/manifest.json",
            f"map-assets-v2/{'b' * 64}/pathing-amphibious.u8.zlib",
            f"map-assets-v2/{'b' * 64}/pathing-ground.u8.zlib",
            f"map-assets-v2/{'b' * 64}/terrain.u8.zlib",
            f"map-assets-v2/{'b' * 64}/zones.i32.zlib",
            "replay-outcome.json",
            "stderr.log",
            "stdout.log",
            "trace.ndjson",
        ]
        assert all({"asset_public_id", "kind", "logical_path", "sha256", "size_bytes"} == set(entry) for entry in manifest)
        catalog_asset = session.scalar(
            select(ManagedAsset).where(ManagedAsset.kind == "telemetry_catalog")
        )
        assert catalog_asset is not None and catalog_asset.media_type == "application/json"
        persisted = json.dumps(telemetry.output_json, sort_keys=True)
        assert str(bundle_root) not in persisted
        assert "private-run-root" not in persisted


@pytest.mark.parametrize(
    "logical_path",
    ["", "/absolute", "C:/absolute", "dir\\member", "bad:name", "\x00", ".", "..", "a/../b", "a//b", "a/"],
)
def test_logical_artifact_descriptor_rejects_unsafe_text(logical_path: str) -> None:
    with pytest.raises(ValueError):
        importing_service._validated_logical_path(logical_path, set())


def test_logical_artifact_descriptor_rejects_windows_case_collision() -> None:
    seen: set[str] = set()
    assert importing_service._validated_logical_path("Logs/STDOUT.log", seen) == "Logs/STDOUT.log"
    with pytest.raises(ValueError):
        importing_service._validated_logical_path("logs/stdout.LOG", seen)


@pytest.mark.parametrize("malformation", ["traversal", "outside_root", "case_collision"])
def test_usable_bundle_rejects_unsafe_topology_before_asset_persistence(
    malformation: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    bundle_root = tmp_path / "private-bundle-root"
    artifact = _bundle_artifact(bundle_root)
    if malformation == "traversal":
        (bundle_root / "nested").mkdir()
        artifact = replace(artifact, stdout_path=bundle_root / "nested" / ".." / "stdout.log")
    elif malformation == "outside_root":
        outside = tmp_path / "outside-stdout.log"
        outside.write_bytes(b"fixture stdout")
        artifact = replace(artifact, stdout_path=outside)
    else:
        upper = bundle_root / "Case.log"
        lower = bundle_root / "case.log"
        upper.write_bytes(b"case collision")
        if not lower.exists():
            lower.write_bytes(b"case collision")
        artifact = replace(artifact, stdout_path=upper, stderr_path=lower)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(artifact),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)

    with session_factory() as session:
        telemetry = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert telemetry is not None and telemetry.status == "failed"
        assert telemetry.error_code == "invalid_telemetry_artifact"
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedAsset).where(ManagedAsset.kind.like("telemetry_%"))
            )
            == 0
        )
        assert str(bundle_root) not in json.dumps(telemetry.error_details_json, sort_keys=True)


def test_failed_no_trace_artifacts_use_closed_role_descriptors_without_source_topology(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    private_root = tmp_path / "customer-private-run-name"
    private_root.mkdir()
    catalog = private_root / "secret-catalog-name.json"
    outcome = private_root / "secret-outcome-name.json"
    stdout = private_root / "secret-stdout-name.log"
    stderr = private_root / "secret-stderr-name.log"
    first_map = private_root / "secret-map-one.bin"
    second_map = private_root / "secret-map-two.bin"
    for path, content in (
        (catalog, b"catalog"),
        (outcome, b"outcome"),
        (stdout, b"stdout"),
        (stderr, b"stderr"),
        (first_map, b"map-one"),
        (second_map, b"map-two"),
    ):
        path.write_bytes(content)
    artifact = TelemetryArtifact(
        run_id="123e4567-e89b-12d3-a456-426614174000",
        runner_status="nonzero_engine_failure",
        replay_quality="failed",
        strategy_analysis_scope="none",
        trace_path=None,
        catalog_path=catalog,
        map_asset_paths=(second_map, first_map),
        outcome_path=outcome,
        stdout_path=stdout,
        stderr_path=stderr,
        exit_code=7,
        engine_build="fixture-build",
        engine_executable_sha256="a" * 64,
        diagnostics=(AcquisitionDiagnostic("fixture", "failure retained"),),
    )
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(artifact),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)

    with session_factory() as session:
        telemetry = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert telemetry is not None and telemetry.status == "failed"
        payload = cast(dict[str, Any], telemetry.error_details_json)
        descriptors = [entry["logical_path"] for entry in payload["artifacts"]]
        map_descriptors = sorted(
            f"map-assets/{hashlib.sha256(content).hexdigest()}.asset" for content in (b"map-one", b"map-two")
        )
        assert descriptors == ["catalog.json", *map_descriptors, "outcome.json", "stderr.log", "stdout.log"]
        persisted = json.dumps(payload, sort_keys=True)
        assert str(private_root) not in persisted
        assert "customer-private-run-name" not in persisted


@pytest.mark.parametrize("runner_status", ["success", "nonzero_engine_failure"])
def test_telemetry_artifacts_and_failure_diagnostics_are_retained_without_observations(
    runner_status: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path, status=runner_status)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(artifact),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)

    with session_factory() as session:
        telemetry_job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert telemetry_job is not None
        payload = telemetry_job.output_json if runner_status == "success" else telemetry_job.error_details_json
        assert payload["runner_status"] == runner_status
        assert payload["diagnostics"] == [{"code": "fixture", "message": "diagnostic retained"}]
        assert any(asset["kind"] == "telemetry_stdout" for asset in payload["artifacts"])
        assert "run_path" not in json.dumps(payload)
        assert session.scalar(select(func.count()).select_from(TelemetryRun)) == 0
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_source_provenance_never_creates_identity_or_missing_telemetry_issue(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    parser_only = _service(session_factory, settings, replay_store, artifact_store, clock)
    parser_only.submit(ImportRequest(replay_file))
    _drain(parser_only)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Player)) == 0
        assert session.scalar(select(func.count()).select_from(PlayerAlias)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayPlayer)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayQualityIssue)) == 0
        assert session.scalar(select(func.count()).select_from(Job).where(Job.stage == "telemetry")) == 0
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        parse_job = session.scalar(select(Job).where(Job.stage == "parse"))
        assert import_job is not None and parse_job is not None
        dependencies = set(
            session.scalars(
                select(JobDependency.depends_on_job_id).where(JobDependency.job_id == import_job.id)
            )
        )
        assert dependencies == {parse_job.id}


def test_reference_parser_revalidates_bytes_after_consumption(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    def mutating_parser(path: Path) -> SimpleNamespace:
        path.write_bytes(path.read_bytes() + b"parser-mutation")
        return _successful_parser(path)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=mutating_parser,
    )
    service.submit(ImportRequest(replay_file, reference_only=True))
    for _ in range(4):
        result = service.run_available("reference-parser")
    assert result[0].stage == "parse"
    assert result[0].status == "failed"
    assert result[0].error_code == "source_changed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ParserRun)) == 0


def test_reference_telemetry_revalidates_bytes_before_copying_artifacts(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_MutatingAcquirer(_artifact(tmp_path)),
    )
    service.submit(ImportRequest(replay_file, reference_only=True, request_telemetry=True))
    for _ in range(5):
        result = service.run_available("reference-telemetry")
    assert result[0].stage == "telemetry"
    assert result[0].status == "failed"
    assert result[0].error_code == "source_changed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 0
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_failed_reference_branch_cannot_block_later_successful_copy_branch(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    original_bytes = b"same-replay-content"
    reference = tmp_path / "stale-reference.rep"
    copied = tmp_path / "fresh-copy.rep"
    reference.write_bytes(original_bytes)
    copied.write_bytes(original_bytes)
    parser_calls = 0

    def first_parser_fails_after_mutation(path: Path) -> SimpleNamespace:
        nonlocal parser_calls
        parser_calls += 1
        if parser_calls == 1:
            path.write_bytes(path.read_bytes() + b"stale")
            raise ValueError("reference parser observed unstable bytes")
        return _successful_parser(path)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=first_parser_fails_after_mutation,
    )
    service.submit(ImportRequest(reference, reference_only=True))
    for _ in range(4):
        reference_result = service.run_available("reference")
    assert reference_result[0].error_code == "source_changed"
    assert service.run_available("dependency-projector") == ()

    service.submit(ImportRequest(copied))
    _drain(service, worker="copy")
    with session_factory() as session:
        parse_jobs = list(session.scalars(select(Job).where(Job.stage == "parse").order_by(Job.id)))
        assert [(job.input_json["import_mode"], job.status) for job in parse_jobs] == [
            ("reference", "failed"),
            ("copy", "succeeded"),
        ]
        import_jobs = list(
            session.scalars(select(Job).where(Job.stage == "import_observations").order_by(Job.id))
        )
        assert len(import_jobs) == 2
        assert [job.status for job in import_jobs] == ["failed", "pending"]
        copy_dependencies = set(
            session.scalars(
                select(JobDependency.depends_on_job_id).where(JobDependency.job_id == import_jobs[1].id)
            )
        )
        assert copy_dependencies == {parse_jobs[1].id}


def test_artifact_port_rejects_duplicate_or_unsafe_paths_without_observations(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path)
    malformed = replace(artifact, map_asset_paths=(artifact.stdout_path,))
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(malformed),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.status == "failed"
        assert job.error_code == "invalid_telemetry_artifact"
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedAsset).where(ManagedAsset.kind.like("telemetry_%"))
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_artifact_port_rejects_supplied_symlink_before_path_canonicalization(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path)
    assert artifact.stdout_path is not None
    linked_stdout = tmp_path / "linked-stdout.log"
    try:
        linked_stdout.symlink_to(artifact.stdout_path)
    except OSError as error:
        pytest.skip(f"symlink/reparse creation is unavailable: {error}")
    assert linked_stdout.is_symlink()
    malformed = replace(artifact, stdout_path=linked_stdout)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(malformed),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.status == "failed"
        assert job.error_code == "invalid_telemetry_artifact"
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedAsset).where(ManagedAsset.kind.like("telemetry_%"))
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_artifact_port_inspects_supplied_alias_before_resolving_target(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path)
    assert artifact.stdout_path is not None
    target = artifact.stdout_path
    supplied_alias = tmp_path / "synthetic-reparse.log"
    original_resolve = Path.resolve
    original_lstat = Path.lstat
    original_is_symlink = Path.is_symlink
    resolved_alias = False

    def resolve_alias(path: Path, *args: object, **kwargs: object) -> Path:
        nonlocal resolved_alias
        if path == supplied_alias:
            resolved_alias = True
            return target
        return original_resolve(path, *args, **kwargs)  # type: ignore[arg-type]

    def lstat_alias(path: Path) -> object:
        if path == supplied_alias:
            return SimpleNamespace(st_mode=target.lstat().st_mode, st_file_attributes=0x400)
        return original_lstat(path)

    def identify_alias(path: Path) -> bool:
        if path == supplied_alias:
            return False
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "resolve", resolve_alias)
    monkeypatch.setattr(Path, "lstat", lstat_alias)
    monkeypatch.setattr(Path, "is_symlink", identify_alias)
    malformed = replace(artifact, stdout_path=supplied_alias)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(malformed),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.status == "failed"
        assert job.error_code == "invalid_telemetry_artifact"
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedAsset).where(ManagedAsset.kind.like("telemetry_%"))
            )
            == 0
        )
    assert resolved_alias is False


@pytest.mark.parametrize(
    "malformation",
    [
        "invalid_uuid",
        "uppercase_uuid",
        "negative_exit",
        "uppercase_hash",
        "wrong_hash_type",
        "wrong_path_type",
        "mutable_map_paths",
        "missing_trace",
        "missing_file",
        "directory",
    ],
)
def test_artifact_port_rejects_each_malformed_public_field(
    malformation: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path)
    if malformation == "invalid_uuid":
        artifact = replace(artifact, run_id="not-a-uuid")
    elif malformation == "uppercase_uuid":
        artifact = replace(artifact, run_id=artifact.run_id.upper())
    elif malformation == "negative_exit":
        artifact = replace(artifact, exit_code=-1)
    elif malformation == "uppercase_hash":
        artifact = replace(artifact, engine_executable_sha256="A" * 64)
    elif malformation == "wrong_hash_type":
        artifact = replace(artifact, engine_executable_sha256=cast(Any, 7))
    elif malformation == "wrong_path_type":
        artifact = replace(artifact, stdout_path=cast(Any, str(tmp_path / "private.log")))
    elif malformation == "mutable_map_paths":
        artifact = replace(artifact, map_asset_paths=cast(Any, []))
    elif malformation == "missing_trace":
        artifact = replace(artifact, trace_path=None)
    elif malformation == "missing_file":
        assert artifact.stdout_path is not None
        artifact.stdout_path.unlink()
    else:
        artifact = replace(artifact, stdout_path=tmp_path)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(artifact),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.error_code == "invalid_telemetry_artifact"
        assert job.error_details_json["failure_envelope"]["type"] == "telemetry_artifact_failure"
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_import_boundary_rejects_missing_nonreplay_and_symlink_paths(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    with pytest.raises(ValueError, match="unavailable"):
        service.submit(ImportRequest(tmp_path / "missing.rep"))
    nonreplay = tmp_path / "notes.txt"
    nonreplay.write_text("not replay", encoding="utf-8")
    with pytest.raises(ValueError, match="extension"):
        service.submit(ImportRequest(nonreplay))
    symlink = tmp_path / "link.rep"
    try:
        symlink.symlink_to(nonreplay)
    except OSError:
        return
    with pytest.raises(ValueError, match="symlink"):
        service.submit(ImportRequest(symlink))


def test_changed_or_missing_copy_source_fails_before_managed_publication(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    changed = tmp_path / "changed.rep"
    changed.write_bytes(b"first")
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit(ImportRequest(changed))
    service.run_available("changed")
    changed.write_bytes(b"second")
    hash_failure = service.run_available("changed")
    assert hash_failure[0].status == "failed" and hash_failure[0].error_code == "source_changed"

    missing = tmp_path / "missing-copy.rep"
    missing.write_bytes(b"copy-source")
    service.submit(ImportRequest(missing))
    service.run_available("missing")
    service.run_available("missing")
    missing.unlink()
    copy_failure = service.run_available("missing")
    assert copy_failure[0].stage == "manage_copy"
    assert copy_failure[0].status == "failed" and copy_failure[0].error_code == "source_missing"


def test_non_io_parser_failure_is_terminal_and_limit_must_be_positive(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    def invalid_parser(path: Path) -> object:
        raise ValueError(path.name)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=invalid_parser,
    )
    with pytest.raises(ValueError, match="limit"):
        service.run_available("worker", limit=0)
    service.submit(ImportRequest(replay_file))
    for _ in range(4):
        result = service.run_available("worker")
    assert result[0].status == "failed"
    assert result[0].retryable is False and result[0].error_code == "parser_failed"


def test_cli_import_json_reference_only_and_jobs_retry_use_external_product_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = tmp_path / "cli-product-data"
    replay = tmp_path / "cli.rep"
    shutil.copyfile(PINNED_REPLAY, replay)
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))

    assert main(["import", str(replay), "--reference-only", "--json"]) == 0
    import_document = json.loads(capsys.readouterr().out)
    assert import_document["accepted_path_count"] == 0
    assert import_document["discovery_job"]["stage"] == "discover"
    job_id = import_document["discovery_job"]["public_id"]
    assert str(data_root) not in json.dumps(import_document)
    assert str(replay) not in json.dumps(import_document)

    assert main(["jobs", "retry", job_id]) == 0
    retry_document = json.loads(capsys.readouterr().out)
    assert retry_document["public_id"] == job_id
    assert retry_document["status"] == "pending"

    engine = create_database_engine(data_root / "replay-analyzer.sqlite3")
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            job = session.scalar(select(Job).where(Job.public_id == job_id))
            assert job is not None and job.input_json["import_mode"] == "reference"
    finally:
        engine.dispose()
