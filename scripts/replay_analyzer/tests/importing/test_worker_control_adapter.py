"""External worker claims materialize observation identities before selection."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.importing.job_contracts import (
    DEFAULT_JOB_CLAIM_SELECTOR,
    JobClaimSelectorDTO,
)
from generals_replay_analyzer.importing.job_lifecycle import JobLifecycleService
from generals_replay_analyzer.importing.service import ImportService
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import MutableClock


def _service(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> ImportService:
    return ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=lambda _path: (_ for _ in ()).throw(AssertionError("parser is not used")),
        clock=clock,
        parser_version="parser-1",
        telemetry_acquirer_version="telemetry-1",
    )


def test_external_claim_materializes_before_delegating_default_and_selected_claims(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
    monkeypatch: object,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    events: list[tuple[str, JobClaimSelectorDTO | None]] = []

    def materialize() -> None:
        events.append(("materialize", None))

    def claim(
        _lifecycle: JobLifecycleService,
        _worker_public_id: str,
        _lease_seconds: int,
        selector: JobClaimSelectorDTO = DEFAULT_JOB_CLAIM_SELECTOR,
    ) -> None:
        events.append(("claim", selector))

    monkeypatch.setattr(service, "_materialize_ready_jobs", materialize)  # type: ignore[attr-defined]
    monkeypatch.setattr(JobLifecycleService, "claim_next", claim)  # type: ignore[attr-defined]
    worker = service.worker_control_port()
    selected = JobClaimSelectorDTO(
        "00000000-0000-4000-8000-000000000171",
        ("derive_features",),
    )

    assert worker.claim_next("00000000-0000-4000-8000-000000000172", 30) is None
    assert worker.claim_next("00000000-0000-4000-8000-000000000172", 30, selected) is None
    assert events == [
        ("materialize", None),
        ("claim", DEFAULT_JOB_CLAIM_SELECTOR),
        ("materialize", None),
        ("claim", selected),
    ]
