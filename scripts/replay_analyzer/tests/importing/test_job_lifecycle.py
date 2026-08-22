"""Secure durable lifecycle behavior for external workers and the Jobs UI."""

from __future__ import annotations

import hashlib
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import Job, JobEvent, JobLogSnapshot, JobStageResult, ManagedAsset
from generals_replay_analyzer.importing.job_contracts import (
    CancelJobCommandDTO,
    JobLogQueryDTO,
    JobProgressDTO,
    JobQueryDTO,
    JobState,
    OwnedExecutionSettlementDTO,
    RetryJobCommandDTO,
    StageExecutionOutcomeDTO,
    WorkerLeaseDTO,
)
from generals_replay_analyzer.importing.job_lifecycle import JobLifecycleService
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec, JobStateError
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import MutableClock


def _job(session_factory: sessionmaker[Session], clock: MutableClock, stage: str = "parse", **values: object) -> str:
    coordinator = JobCoordinator(session_factory, clock=clock)
    spec = JobSpec(
        stage=stage,
        component_version="component-v1",
        idempotency_key=str(values.pop("idempotency_key", f"{stage}-identity")),
        input_json={"private_input": "must-never-cross-contract"},
        max_attempts=int(values.pop("max_attempts", 3)),
        retryable=bool(values.pop("retryable", True)),
    )
    assert not values
    return coordinator.create_job(spec).public_id


def _service(
    session_factory: sessionmaker[Session],
    clock: MutableClock,
    tmp_path: Path,
    stages: tuple[str, ...] = ("parse",),
) -> JobLifecycleService:
    return JobLifecycleService(
        session_factory,
        registered_stages=stages,
        clock=clock,
        retry_base_delay=timedelta(0),
        retry_max_delay=timedelta(0),
        log_store=ContentAddressedStore(tmp_path / "managed-logs"),
        log_relative_root="job-logs",
        redaction_values=(str(tmp_path), "TOP_SECRET"),
    )


def _claim(service: JobLifecycleService, worker: str = "00000000-0000-4000-8000-000000000901") -> WorkerLeaseDTO:
    claim = service.claim_next(worker, 30)
    assert claim is not None
    return claim


def test_contract_module_is_orm_free_immutable_and_contains_no_privileged_job_views() -> None:
    """Catch a public DTO importing persistence/runtime layers or leaking raw job implementation fields."""
    import generals_replay_analyzer.importing.job_contracts as contracts

    imported_modules = {
        value.__name__
        for value in vars(contracts).values()
        if isinstance(value, ModuleType)
    }
    assert not any(
        name.startswith(("sqlalchemy", "generals_replay_analyzer.db", "generals_replay_analyzer.web", "pathlib"))
        for name in imported_modules
    )
    assert tuple(JobState) == (
        JobState.PENDING,
        JobState.RUNNING,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    )
    forbidden = {"input_json", "output_json", "path", "pid", "process", "owner", "lease_token_sha256"}
    for value in vars(contracts).values():
        if inspect.isclass(value) and hasattr(value, "__dataclass_fields__"):
            assert value.__dataclass_params__.frozen is True
            assert forbidden.isdisjoint(field.name for field in fields(value))


def test_two_services_atomically_claim_once_and_persist_only_the_token_digest(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch a claim race, duplicate attempt increment, raw token persistence, or unregistered-stage admission."""
    job_public_id = _job(session_factory, clock)
    first = _service(session_factory, clock, tmp_path)
    second = _service(session_factory, clock, tmp_path)
    workers = (
        "00000000-0000-4000-8000-000000000911",
        "00000000-0000-4000-8000-000000000912",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(lambda worker: first.claim_next(worker, 30), workers))
    claim = next(value for value in claims if value is not None)
    assert sum(value is not None for value in claims) == 1
    assert claim.job_public_id == job_public_id
    assert claim.attempt_count == 1
    assert len(claim.lease_token) >= 43
    with session_factory() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_public_id))
        assert row is not None
        assert row.attempt_count == 1
        assert row.lease_token_sha256 == hashlib.sha256(claim.lease_token.encode("utf-8")).hexdigest()
        assert claim.lease_token not in "|".join(
            str(value)
            for value in (
                row.lease_owner,
                row.lease_token_sha256,
                row.lease_execution_public_id,
                row.input_json,
                row.error_details_json,
            )
        )
    assert second.claim_next(workers[1], 30) is None
    _job(session_factory, clock, "derive_features", idempotency_key="unregistered")
    assert first.claim_next(workers[0], 30) is None


def test_heartbeat_progress_and_capability_checks_preserve_attempt_and_heartbeat_revision(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch partial lease validation, heartbeat token churn, or nonmonotonic progress within one attempt."""
    _job(session_factory, clock)
    service = _service(session_factory, clock, tmp_path)
    worker = "00000000-0000-4000-8000-000000000921"
    claim = _claim(service, worker)
    detail = service.get_job(claim.job_public_id)
    initial_revision = detail.summary.revision
    clock.advance(seconds=10)
    extended = service.heartbeat(worker, claim, 60)
    assert extended.lease_expires_at == clock.current + timedelta(seconds=60)
    after_heartbeat = service.get_job(claim.job_public_id)
    assert after_heartbeat.summary.revision == initial_revision
    assert after_heartbeat.summary.attempt_count == 1

    service.report_progress(worker, extended, JobProgressDTO(2, 5, "items", clock.current))
    progressed = service.get_job(claim.job_public_id)
    assert progressed.summary.progress == JobProgressDTO(2, 5, "items", clock.current)
    assert progressed.summary.revision == initial_revision + 1
    service.report_progress(worker, extended, JobProgressDTO(2, 5, "items", clock.current))
    assert service.get_job(claim.job_public_id).summary.revision == progressed.summary.revision
    with pytest.raises(JobStateError, match="progress_regressed"):
        service.report_progress(worker, extended, JobProgressDTO(1, 5, "items", clock.current))

    wrong_claims = (
        replace(extended, lease_token="wrong"),
        replace(extended, attempt_count=2),
        replace(extended, execution_public_id="00000000-0000-4000-8000-000000000999"),
    )
    for wrong in wrong_claims:
        with pytest.raises(JobStateError, match="lease_mismatch"):
            service.heartbeat(worker, wrong, 30)
    with pytest.raises(JobStateError, match="lease_mismatch"):
        service.heartbeat("00000000-0000-4000-8000-000000000922", extended, 30)


def test_pending_and_running_cancellation_preserve_lease_until_exact_owned_tree_settlement(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch process-signalling cancellation, success-after-cancel, stale mutation, or uncertain cancellation success."""
    pending_id = _job(session_factory, clock, idempotency_key="pending-cancel")
    service = _service(session_factory, clock, tmp_path)
    pending = service.get_job(pending_id)
    mutation = service.cancel_job(
        CancelJobCommandDTO(pending_id, pending.summary.revision, "operator", "user_request")
    )
    assert mutation.state is JobState.CANCELLED
    assert mutation.attempt_count == 0
    with pytest.raises(JobStateError, match="revision_conflict"):
        service.cancel_job(CancelJobCommandDTO(pending_id, pending.summary.revision, "operator", "user_request"))

    running_id = _job(session_factory, clock, idempotency_key="running-cancel")
    claim = _claim(service)
    assert claim.job_public_id == running_id
    running = service.get_job(running_id)
    requested = service.cancel_job(
        CancelJobCommandDTO(running_id, running.summary.revision, "operator", "user_request")
    )
    assert requested.state is JobState.RUNNING
    assert service.cancellation("00000000-0000-4000-8000-000000000901", claim).requested is True
    with pytest.raises(JobStateError, match="cancellation_requested"):
        service.settle_success("00000000-0000-4000-8000-000000000901", claim, "opaque-result")
    with pytest.raises(JobStateError, match="owned_execution_mismatch"):
        service.settle_cancelled(
            "00000000-0000-4000-8000-000000000901",
            claim,
            OwnedExecutionSettlementDTO(
                "00000000-0000-4000-8000-000000000999", True
            ),
        )
    service.settle_cancelled(
        "00000000-0000-4000-8000-000000000901",
        claim,
        OwnedExecutionSettlementDTO(claim.execution_public_id, True),
    )
    assert service.get_job(running_id).summary.state is JobState.CANCELLED

    uncertain_id = _job(session_factory, clock, idempotency_key="uncertain-cancel")
    uncertain_claim = _claim(service)
    uncertain_detail = service.get_job(uncertain_id)
    service.cancel_job(
        CancelJobCommandDTO(uncertain_id, uncertain_detail.summary.revision, "operator", "user_request")
    )
    service.settle_cancelled(
        "00000000-0000-4000-8000-000000000901",
        uncertain_claim,
        OwnedExecutionSettlementDTO(uncertain_claim.execution_public_id, False),
    )
    failed = service.get_job(uncertain_id).summary
    assert failed.state is JobState.FAILED
    assert failed.error is not None and failed.error.code == "owned_child_settlement_failed"


def test_expiry_shutdown_retry_and_dependency_cancellation_are_distinct(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch stale-owner settlement, attempt reset, shutdown overclaim, or collapsed dependency terminal reasons."""
    parent_id = _job(session_factory, clock, idempotency_key="expiry-parent", max_attempts=2)
    child_id = _job(session_factory, clock, "report", idempotency_key="expiry-child")
    coordinator = JobCoordinator(session_factory, clock=clock)
    coordinator.add_dependency(child_id, parent_id)
    service = _service(session_factory, clock, tmp_path, ("parse", "report"))
    old_claim = _claim(service)
    clock.advance(seconds=31)
    new_claim = service.claim_next("00000000-0000-4000-8000-000000000932", 30)
    assert new_claim is not None and new_claim.job_public_id == parent_id and new_claim.attempt_count == 2
    with pytest.raises(JobStateError, match="lease_mismatch"):
        service.settle_failure(
            "00000000-0000-4000-8000-000000000901",
            old_claim,
            StageExecutionOutcomeDTO("failed", None, "stale", "stale", False),
        )
    service.release_after_shutdown(
        "00000000-0000-4000-8000-000000000932",
        new_claim,
        OwnedExecutionSettlementDTO(new_claim.execution_public_id, True),
    )
    exhausted = service.get_job(parent_id).summary
    assert exhausted.state is JobState.FAILED and exhausted.error is not None
    assert exhausted.error.code == "worker_shutdown"

    cancelled_parent = _job(session_factory, clock, idempotency_key="cancel-parent")
    cancelled_child = _job(session_factory, clock, "report", idempotency_key="cancel-child")
    coordinator.add_dependency(cancelled_child, cancelled_parent)
    detail = service.get_job(cancelled_parent)
    service.cancel_job(
        CancelJobCommandDTO(cancelled_parent, detail.summary.revision, "operator", "user_request")
    )
    descendant = service.get_job(cancelled_child).summary
    assert descendant.state is JobState.FAILED
    assert descendant.error is not None and descendant.error.code == "dependency_cancelled"


def test_durable_result_is_reused_after_settlement_loss_without_another_attempt(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch handler re-execution after an immutable result committed but job settlement was lost."""
    job_id = _job(session_factory, clock, idempotency_key="crash-safe-result")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    result_id = service.persist_stage_result(claim, {"stable": "output"})
    with session_factory() as session:
        result = session.scalar(select(JobStageResult).where(JobStageResult.public_id == result_id))
        assert result is not None and result.output_json == {"stable": "output"}
    clock.advance(seconds=31)
    assert service.claim_next("00000000-0000-4000-8000-000000000942", 30) is None
    settled = service.get_job(job_id).summary
    assert settled.state is JobState.SUCCEEDED
    assert settled.attempt_count == 1
    assert settled.result_public_id == result_id


def test_cancellation_wins_over_durable_result_recovery_after_lease_expiry(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch crash-result recovery settling success after a cancellation request has won."""
    job_id = _job(session_factory, clock, idempotency_key="cancelled-crash-safe-result")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    result_id = service.persist_stage_result(claim, {"stable": "output"})
    detail = service.get_job(job_id)
    service.cancel_job(CancelJobCommandDTO(job_id, detail.summary.revision, "operator", "user_request"))

    clock.advance(seconds=31)
    assert service.claim_next("00000000-0000-4000-8000-000000000943", 30) is None
    settled = service.get_job(job_id).summary
    assert settled.state is JobState.FAILED
    assert settled.error is not None and settled.error.code == "owned_child_settlement_failed"
    assert settled.result_public_id == result_id


def test_events_and_logs_are_path_free_immutable_redacted_bounded_and_ownership_checked(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch unsafe event fields, locator exposure, unbounded reads, or raw supervisor output publication."""
    first_id = _job(session_factory, clock, idempotency_key="logs-first")
    second_id = _job(session_factory, clock, idempotency_key="logs-second")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    payload = (
        f"\x1b[31mfailed at {tmp_path} https://user:pass@example.invalid --token TOP_SECRET\x1b[0m\n"
        "Traceback (most recent call last):\n  File secret.py, line 1\nValueError: TOP_SECRET\n"
        + ("x" * 70000)
    ).encode("utf-8")
    reference = service.publish_log(
        "00000000-0000-4000-8000-000000000901", claim, "stderr", 0, payload
    )
    assert reference.job_public_id == first_id
    assert not hasattr(reference, "locator")
    chunk = service.read_log(JobLogQueryDTO(first_id, reference.public_id, 0, 65536))
    assert chunk.state == "available"
    assert len(chunk.content.encode("utf-8")) <= 65536
    assert chunk.next_offset is not None
    assert "\x1b" not in chunk.content
    assert str(tmp_path) not in chunk.content
    assert "user:pass" not in chunk.content
    assert "TOP_SECRET" not in chunk.content
    assert "Traceback" not in chunk.content
    with pytest.raises(JobStateError, match="log_ownership_mismatch"):
        service.read_log(JobLogQueryDTO(second_id, reference.public_id, 0, 1024))
    with session_factory() as session:
        event = session.scalar(
            select(JobEvent).where(
                JobEvent.job_id == select(Job.id).where(Job.public_id == first_id).scalar_subquery()
            )
        )
        snapshot = session.scalar(select(JobLogSnapshot).where(JobLogSnapshot.public_id == reference.public_id))
        asset = session.get(ManagedAsset, snapshot.managed_asset_id if snapshot is not None else -1)
        assert event is not None
        assert event.reason_code is None
        assert snapshot is not None and asset is not None
        assert not Path(asset.relative_path).is_absolute()


def test_stale_log_publisher_is_rejected_before_any_managed_bytes_are_written(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch a rejected publisher leaving an orphaned blob in the Analytics-managed log store."""
    _job(session_factory, clock, idempotency_key="stale-log-publisher")
    service = _service(session_factory, clock, tmp_path)
    claim = _claim(service)
    log_root = tmp_path / "managed-logs"
    before = tuple(path.relative_to(log_root) for path in log_root.rglob("*") if path.is_file())

    with pytest.raises(JobStateError, match="lease_mismatch"):
        service.publish_log(
            "00000000-0000-4000-8000-000000000999",
            claim,
            "stderr",
            0,
            b"must-not-be-published",
        )

    after = tuple(path.relative_to(log_root) for path in log_root.rglob("*") if path.is_file())
    assert after == before


def test_revisioned_retry_and_listing_expose_only_safe_snapshots(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    """Catch nonoptimistic retry, retrying terminal success, or a public list surface with privileged payloads."""
    job_id = _job(session_factory, clock, idempotency_key="retryable-failure")
    service = _service(session_factory, clock, tmp_path)
    with session_factory.begin() as session:
        row = session.scalar(select(Job).where(Job.public_id == job_id))
        assert row is not None
        row.status = "failed"
        row.attempt_count = 1
        row.retryable = True
        row.completed_at = clock.current
        row.error_code = "temporary"
        row.error_message = "safe summary"
        row.error_details_json = {}
    detail = service.get_job(job_id)
    retried = service.retry_job(RetryJobCommandDTO(job_id, detail.summary.revision))
    assert retried.state is JobState.PENDING
    assert retried.attempt_count == 1
    with pytest.raises(JobStateError, match="revision_conflict"):
        service.retry_job(RetryJobCommandDTO(job_id, detail.summary.revision))
    page = service.list_jobs(JobQueryDTO(states=(JobState.PENDING,), limit=10))
    assert page.items[0].public_id == job_id
    assert not hasattr(page.items[0], "lease_token")
    assert not hasattr(page.items[0], "input_json")
