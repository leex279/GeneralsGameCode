"""Durability, dependency, lease, retry, and restart tests for import jobs."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import Job, JobDependency
from generals_replay_analyzer.importing.jobs import (
    DependencyCycleError,
    JobCoordinator,
    JobSpec,
    JobStateError,
    StageFailure,
)

from .conftest import MutableClock


def _coordinator(factory: sessionmaker[Session], clock: MutableClock) -> JobCoordinator:
    return JobCoordinator(
        factory,
        clock=clock,
        lease_duration=timedelta(seconds=30),
        retry_base_delay=timedelta(seconds=5),
        retry_max_delay=timedelta(seconds=20),
    )


def _spec(name: str, *, version: str = "1", max_attempts: int = 3, retryable: bool = True) -> JobSpec:
    return JobSpec(
        stage=name,
        component_version=version,
        idempotency_key=f"{name}:{version}:fixture",
        input_json={"name": name, "version": version},
        max_attempts=max_attempts,
        retryable=retryable,
    )


def test_idempotency_key_coalesces_but_versioned_input_creates_new_job(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    jobs = _coordinator(session_factory, clock)
    first = jobs.create_job(_spec("parse"))
    duplicate = jobs.create_job(_spec("parse"))
    changed = jobs.create_job(_spec("parse", version="2"))
    assert duplicate.public_id == first.public_id
    assert changed.public_id != first.public_id
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2


def test_dependencies_gate_claims_reject_cycles_and_project_terminal_failure(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    jobs = _coordinator(session_factory, clock)
    root = jobs.create_job(_spec("root"))
    middle = jobs.create_job(_spec("middle"))
    leaf = jobs.create_job(_spec("leaf"))
    jobs.add_dependency(middle.public_id, root.public_id)
    jobs.add_dependency(leaf.public_id, middle.public_id)

    assert jobs.claim("worker", frozenset({"middle", "leaf"})) is None
    with pytest.raises(DependencyCycleError):
        jobs.add_dependency(root.public_id, leaf.public_id)
    with pytest.raises(DependencyCycleError):
        jobs.add_dependency(root.public_id, root.public_id)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobDependency)) == 2

    claimed = jobs.claim("worker", frozenset({"root", "middle", "leaf"}))
    assert claimed is not None and claimed.public_id == root.public_id
    jobs.fail(claimed.public_id, "worker", StageFailure("broken", "terminal fixture", retryable=False))
    assert jobs.claim("worker", frozenset({"middle", "leaf"})) is None
    with session_factory() as session:
        projected = session.scalar(select(Job).where(Job.public_id == middle.public_id))
        assert projected is not None
        assert projected.status == "failed"
        assert projected.error_code == "dependency_failed"
        assert projected.retryable is False
        assert projected.lease_owner is None and projected.lease_expires_at is None


def test_terminal_failure_policy_never_treats_nonterminal_dependencies_as_evidence(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    """Catch pending, retryable-pending, or running dependencies making an opted-in stage runnable."""
    jobs = _coordinator(session_factory, clock)
    terminal = jobs.create_job(_spec("terminal-parse"))
    pending = jobs.create_job(_spec("pending-parse"))
    retryable = jobs.create_job(_spec("retryable-parse"))
    running = jobs.create_job(_spec("running-parse"))
    terminal_child = jobs.create_job(_spec("terminal-import"))
    pending_child = jobs.create_job(_spec("pending-import"))
    retryable_child = jobs.create_job(_spec("retryable-import"))
    running_child = jobs.create_job(_spec("running-import"))
    jobs.add_dependency(terminal_child.public_id, terminal.public_id)
    jobs.add_dependency(pending_child.public_id, pending.public_id)
    jobs.add_dependency(retryable_child.public_id, retryable.public_id)
    jobs.add_dependency(running_child.public_id, running.public_id)

    retryable_claim = jobs.claim("dependency-worker", frozenset({"retryable-parse"}))
    assert retryable_claim is not None
    jobs.fail(
        retryable_claim.public_id,
        "dependency-worker",
        StageFailure("retryable_fixture", "retryable fixture", retryable=True),
    )
    running_claim = jobs.claim("dependency-worker", frozenset({"running-parse"}))
    assert running_claim is not None
    terminal_claim = jobs.claim("dependency-worker", frozenset({"terminal-parse"}))
    assert terminal_claim is not None
    jobs.fail(
        terminal_claim.public_id,
        "dependency-worker",
        StageFailure("terminal_fixture", "terminal fixture", retryable=False),
    )

    policies = {
        "terminal-import": frozenset({"terminal-parse"}),
        "pending-import": frozenset({"pending-parse"}),
        "retryable-import": frozenset({"retryable-parse"}),
        "running-import": frozenset({"running-parse"}),
    }
    claimed = jobs.claim(
        "observation-worker",
        frozenset(policies),
        terminal_failure_stages=policies,
    )
    assert claimed is not None and claimed.public_id == terminal_child.public_id
    assert jobs.claim(
        "observation-worker",
        frozenset(policies),
        terminal_failure_stages=policies,
    ) is None
    with session_factory() as session:
        states = {
            row.stage: row.status
            for row in session.scalars(
                select(Job).where(
                    Job.public_id.in_(
                        {
                            pending_child.public_id,
                            retryable_child.public_id,
                            running_child.public_id,
                        }
                    )
                )
            )
        }
    assert states == {
        "pending-import": "pending",
        "retryable-import": "pending",
        "running-import": "pending",
    }


def test_claim_is_exclusive_and_increments_attempt_exactly_once(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    first_service = _coordinator(session_factory, clock)
    second_service = _coordinator(session_factory, clock)
    created = first_service.create_job(_spec("parse"))
    first_claim = first_service.claim("worker-a", frozenset({"parse"}))
    second_claim = second_service.claim("worker-b", frozenset({"parse"}))
    assert first_claim is not None and first_claim.public_id == created.public_id
    assert first_claim.attempt_count == 1
    assert second_claim is None
    with session_factory() as session:
        row = session.scalar(select(Job).where(Job.public_id == created.public_id))
        assert row is not None and row.attempt_count == 1 and row.lease_owner == "worker-a"


def test_retry_rejects_running_job_without_clearing_another_workers_lease(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    first_service = _coordinator(session_factory, clock)
    second_service = _coordinator(session_factory, clock)
    created = first_service.create_job(_spec("parse"))
    claimed = first_service.claim("worker-a", frozenset({"parse"}))
    assert claimed is not None and claimed.public_id == created.public_id

    with pytest.raises(JobStateError) as failure:
        second_service.retry(created.public_id)
    assert failure.value.code == "running_job"
    assert second_service.claim("worker-b", frozenset({"parse"})) is None
    with session_factory() as session:
        row = session.scalar(select(Job).where(Job.public_id == created.public_id))
        assert row is not None
        assert row.status == "running"
        assert row.lease_owner == "worker-a"
        assert row.lease_expires_at is not None
        assert row.attempt_count == 1


def test_expired_leases_retry_with_bounded_delay_or_fail_without_live_lease(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    jobs = _coordinator(session_factory, clock)
    retryable = jobs.create_job(_spec("retryable", max_attempts=2))
    nonretryable = jobs.create_job(_spec("nonretryable", retryable=False))
    exhausted = jobs.create_job(_spec("exhausted", max_attempts=1))

    assert jobs.claim("worker-a", frozenset({"retryable"})) is not None
    assert jobs.claim("worker-a", frozenset({"nonretryable"})) is not None
    assert jobs.claim("worker-a", frozenset({"exhausted"})) is not None
    clock.advance(seconds=31)
    assert jobs.claim("worker-b", frozenset({"retryable", "nonretryable", "exhausted"})) is None
    with session_factory() as session:
        by_id = {row.public_id: row for row in session.scalars(select(Job))}
        assert by_id[retryable.public_id].status == "pending"
        assert by_id[retryable.public_id].error_code == "lease_expired"
        assert by_id[nonretryable.public_id].status == "failed"
        assert by_id[exhausted.public_id].status == "failed"
        assert all(row.lease_owner is None and row.lease_expires_at is None for row in by_id.values())

    clock.advance(seconds=5)
    reclaimed = jobs.claim("worker-b", frozenset({"retryable"}))
    assert reclaimed is not None and reclaimed.attempt_count == 2


def test_retry_is_eligible_only_and_does_not_reset_independent_descendant_failure(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    jobs = _coordinator(session_factory, clock)
    pending = jobs.create_job(_spec("pending"))
    successful = jobs.create_job(_spec("successful"))
    terminal = jobs.create_job(_spec("terminal", retryable=False))

    claimed_success = jobs.claim("worker", frozenset({"successful"}))
    assert claimed_success is not None
    jobs.succeed(successful.public_id, "worker", {"ok": True})
    claimed_terminal = jobs.claim("worker", frozenset({"terminal"}))
    assert claimed_terminal is not None
    jobs.fail(terminal.public_id, "worker", StageFailure("own_failure", "do not reset", retryable=False))

    retried = jobs.retry(pending.public_id)
    assert retried.status == "pending" and retried.error_code is None
    with pytest.raises(JobStateError, match="successful"):
        jobs.retry(successful.public_id)
    with pytest.raises(JobStateError, match="nonretryable"):
        jobs.retry(terminal.public_id)
    with pytest.raises(JobStateError, match="unknown"):
        jobs.retry("00000000-0000-0000-0000-000000000000")
    with session_factory() as session:
        own_failure = session.scalar(select(Job).where(Job.public_id == terminal.public_id))
        assert own_failure is not None and own_failure.error_code == "own_failure"


def test_fresh_service_reclaims_durable_work_and_retains_output_and_error(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    first_process = _coordinator(session_factory, clock)
    job = first_process.create_job(_spec("parse", max_attempts=2))
    assert first_process.claim("crashed-worker", frozenset({"parse"})) is not None
    clock.advance(seconds=31)

    second_process = _coordinator(session_factory, clock)
    assert second_process.claim("restart-worker", frozenset({"parse"})) is None
    clock.advance(seconds=5)
    reclaimed = second_process.claim("restart-worker", frozenset({"parse"}))
    assert reclaimed is not None and reclaimed.public_id == job.public_id
    second_process.succeed(job.public_id, "restart-worker", {"resumed": True})

    third_process = _coordinator(session_factory, clock)
    snapshot = third_process.snapshot(job.public_id)
    assert snapshot.status == "succeeded"
    assert snapshot.output_json == {"resumed": True}
    assert snapshot.error_code is None
    assert snapshot.attempt_count == 2


def test_unregistered_future_stage_is_never_claimed(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    jobs = _coordinator(session_factory, clock)
    future = jobs.create_job(_spec("derive_features"))
    assert jobs.claim("task-3-worker", frozenset({"discover", "hash", "parse"})) is None
    assert jobs.snapshot(future.public_id).status == "pending"


def test_graph_local_idempotency_conflict_reloads_competing_job_and_keeps_transaction_usable(
    session_factory: sessionmaker[Session], database_engine: Engine, clock: MutableClock
) -> None:
    first = _coordinator(session_factory, clock)
    competitor = _coordinator(session_factory, clock)
    contested = _spec("contested")
    injected_public_id: str | None = None
    injected = False

    def inject_competing_commit(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal injected, injected_public_id
        if injected or not statement.lstrip().upper().startswith("INSERT INTO JOBS"):
            return
        injected = True
        injected_public_id = competitor.create_job(contested).public_id

    event.listen(database_engine, "before_cursor_execute", inject_competing_commit)
    try:
        with session_factory.begin() as session:
            coalesced = first.ensure_job(session, contested)
            followup = first.ensure_job(session, _spec("followup"))
            assert coalesced.public_id == injected_public_id
            assert followup.stage == "followup"
    finally:
        event.remove(database_engine, "before_cursor_execute", inject_competing_commit)

    assert injected is True
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2


def test_job_boundary_rejects_invalid_clock_specs_workers_and_transitions(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    with pytest.raises(ValueError, match="durations"):
        JobCoordinator(session_factory, clock=clock, lease_duration=timedelta(0))
    naive_time = datetime(2026, 8, 21, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    naive = JobCoordinator(session_factory, clock=lambda: naive_time)
    with pytest.raises(ValueError, match="aware UTC"):
        naive.create_job(_spec("naive"))

    jobs = _coordinator(session_factory, clock)
    with pytest.raises(ValueError, match="nonempty"):
        jobs.create_job(JobSpec("", "1", "", {}))
    with pytest.raises(ValueError, match="maximum attempts"):
        jobs.create_job(JobSpec("invalid", "1", "invalid", {}, max_attempts=0))
    assert jobs.claim("worker", frozenset()) is None
    with pytest.raises(ValueError, match="worker_id"):
        jobs.claim(" ", frozenset({"parse"}))
    created = jobs.create_job(_spec("owned"))
    with pytest.raises(JobStateError, match="lease_mismatch"):
        jobs.succeed(created.public_id, "wrong-worker", {})
    with pytest.raises(JobStateError, match="unknown"):
        jobs.snapshot("00000000-0000-0000-0000-000000000000")


def test_randomized_acyclic_edges_preserve_topological_claim_order_and_reject_cycle(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    seed = 0xC0FFEE
    generator = random.Random(seed)
    jobs = _coordinator(session_factory, clock)
    for permutation in range(100):
        prefix = f"p{permutation}"
        nodes = [jobs.create_job(_spec(f"{prefix}-{name}")) for name in ("a", "b", "c", "d")]
        edges = [(1, 0), (2, 0), (3, 1), (3, 2)]
        generator.shuffle(edges)
        for child, parent in edges:
            jobs.add_dependency(nodes[child].public_id, nodes[parent].public_id)

        completed: set[str] = set()
        for _ in nodes:
            claimed = jobs.claim(f"worker-{permutation}", frozenset(node.stage for node in nodes))
            assert claimed is not None, f"seed={seed} permutation={permutation}"
            dependencies = {
                nodes[parent].public_id for child, parent in edges if nodes[child].public_id == claimed.public_id
            }
            assert dependencies <= completed, f"seed={seed} permutation={permutation}"
            jobs.succeed(claimed.public_id, f"worker-{permutation}", {"done": True})
            completed.add(claimed.public_id)

        with session_factory() as session:
            before = session.scalar(select(func.count()).select_from(JobDependency))
        with pytest.raises(DependencyCycleError):
            jobs.add_dependency(nodes[0].public_id, nodes[3].public_id)
        with session_factory() as session:
            after = session.scalar(select(func.count()).select_from(JobDependency))
        assert after == before, f"seed={seed} permutation={permutation}"
