"""Exact replay and stage filtering for durable worker claims."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import Replay
from generals_replay_analyzer.importing.job_contracts import JobClaimSelectorDTO
from generals_replay_analyzer.importing.job_lifecycle import JobLifecycleService
from generals_replay_analyzer.importing.jobs import JobCoordinator, JobSpec
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import MutableClock


def _service(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> JobLifecycleService:
    return JobLifecycleService(
        session_factory,
        registered_stages=("derive_features", "parse", "telemetry"),
        clock=clock,
        retry_base_delay=timedelta(0),
        retry_max_delay=timedelta(0),
        log_store=ContentAddressedStore(tmp_path / "selector-logs"),
        log_data_root=tmp_path,
    )


def _replay(
    session_factory: sessionmaker[Session], clock: MutableClock, public_id: str, digest_digit: str
) -> int:
    with session_factory.begin() as session:
        row = Replay(
            public_id=public_id,
            sha256=digest_digit * 64,
            replay_name=f"replay-{digest_digit}",
            version_string="1.04",
            version_number=104,
            frame_count=0,
            start_time=0,
            end_time=0,
            exe_crc=0,
            ini_crc=0,
            map_crc=0,
            map_name="maps/test.map",
            seed=0,
            starting_cash=None,
            header_json={},
            lifecycle_state="discovered",
            created_at=clock(),
            updated_at=clock(),
        )
        session.add(row)
        session.flush()
        return row.id


def _job(
    session_factory: sessionmaker[Session],
    clock: MutableClock,
    *,
    replay_id: int,
    stage: str,
    identity: str,
    priority: int,
) -> str:
    coordinator = JobCoordinator(session_factory, clock=clock)
    with session_factory.begin() as session:
        row = coordinator.ensure_job(
            session,
            JobSpec(
                stage,
                "1",
                identity,
                {"replay_sha256": identity},
                replay_id,
                priority=priority,
            ),
        )
        return row.public_id


def test_selector_is_an_immutable_locator_free_public_contract() -> None:
    selector = JobClaimSelectorDTO(
        replay_public_id="00000000-0000-4000-8000-000000000101",
        stages=("derive_features", "render_report"),
    )

    assert selector.replay_public_id == "00000000-0000-4000-8000-000000000101"
    assert selector.stages == ("derive_features", "render_report")
    with pytest.raises(FrozenInstanceError):
        selector.replay_public_id = None  # type: ignore[misc]


@pytest.mark.parametrize(
    ("values", "error_type"),
    (
        ({"replay_public_id": "00000000-0000-4000-8000-00000000010A"}, ValueError),
        ({"replay_public_id": "not-a-uuid"}, ValueError),
        ({"stages": ["parse"]}, TypeError),
        ({"stages": ("render_report", "derive_features")}, ValueError),
        ({"stages": ("parse", "parse")}, ValueError),
        ({"stages": ("private_stage",)}, ValueError),
    ),
)
def test_selector_rejects_noncanonical_or_open_claim_filters(
    values: dict[str, object], error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        JobClaimSelectorDTO(**values)  # type: ignore[arg-type]


def test_selector_rejects_more_than_sixteen_stage_entries_before_normalization() -> None:
    with pytest.raises(ValueError, match="at most 16"):
        JobClaimSelectorDTO(stages=("parse",) * 17)


def test_selector_claims_only_the_exact_replay_and_registered_stage_intersection(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    target_public_id = "00000000-0000-4000-8000-000000000111"
    other_public_id = "00000000-0000-4000-8000-000000000112"
    target_id = _replay(session_factory, clock, target_public_id, "1")
    other_id = _replay(session_factory, clock, other_public_id, "2")
    _job(
        session_factory,
        clock,
        replay_id=other_id,
        stage="parse",
        identity="other-high-priority",
        priority=900,
    )
    _job(
        session_factory,
        clock,
        replay_id=target_id,
        stage="telemetry",
        identity="target-disallowed-stage",
        priority=800,
    )
    expected = _job(
        session_factory,
        clock,
        replay_id=target_id,
        stage="derive_features",
        identity="target-allowed-stage",
        priority=100,
    )

    claim = _service(session_factory, clock, tmp_path).claim_next(
        "00000000-0000-4000-8000-000000000901",
        30,
        JobClaimSelectorDTO(target_public_id, ("derive_features", "render_report")),
    )

    assert claim is not None
    assert claim.job_public_id == expected
    assert claim.stage == "derive_features"


def test_unknown_replay_selector_never_broadens_to_a_global_claim(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    replay_id = _replay(
        session_factory,
        clock,
        "00000000-0000-4000-8000-000000000121",
        "3",
    )
    job_public_id = _job(
        session_factory,
        clock,
        replay_id=replay_id,
        stage="parse",
        identity="only-real-replay-job",
        priority=100,
    )
    service = _service(session_factory, clock, tmp_path)

    assert (
        service.claim_next(
            "00000000-0000-4000-8000-000000000902",
            30,
            JobClaimSelectorDTO("00000000-0000-4000-8000-000000000122"),
        )
        is None
    )
    claim = service.claim_next("00000000-0000-4000-8000-000000000902", 30)
    assert claim is not None and claim.job_public_id == job_public_id


def test_lifecycle_rejects_a_forged_selector_contract(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    with pytest.raises(TypeError, match="exact JobClaimSelectorDTO"):
        _service(session_factory, clock, tmp_path).claim_next(
            "00000000-0000-4000-8000-000000000903",
            30,
            cast(JobClaimSelectorDTO, object()),
        )


def test_default_selector_preserves_global_priority_ordering(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    low_replay = _replay(
        session_factory,
        clock,
        "00000000-0000-4000-8000-000000000131",
        "4",
    )
    high_replay = _replay(
        session_factory,
        clock,
        "00000000-0000-4000-8000-000000000132",
        "5",
    )
    _job(
        session_factory,
        clock,
        replay_id=low_replay,
        stage="derive_features",
        identity="low-global-priority",
        priority=10,
    )
    expected = _job(
        session_factory,
        clock,
        replay_id=high_replay,
        stage="parse",
        identity="high-global-priority",
        priority=900,
    )

    claim = _service(session_factory, clock, tmp_path).claim_next(
        "00000000-0000-4000-8000-000000000904",
        30,
    )

    assert claim is not None and claim.job_public_id == expected


def test_empty_registered_stage_intersection_returns_none_without_consuming_work(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    replay_public_id = "00000000-0000-4000-8000-000000000141"
    replay_id = _replay(session_factory, clock, replay_public_id, "6")
    job_public_id = _job(
        session_factory,
        clock,
        replay_id=replay_id,
        stage="parse",
        identity="registered-parse-only",
        priority=100,
    )
    service = _service(session_factory, clock, tmp_path)

    assert (
        service.claim_next(
            "00000000-0000-4000-8000-000000000905",
            30,
            JobClaimSelectorDTO(replay_public_id, ("render_report",)),
        )
        is None
    )
    claim = service.claim_next("00000000-0000-4000-8000-000000000905", 30)
    assert claim is not None and claim.job_public_id == job_public_id


def test_two_workers_cannot_double_claim_one_selected_replay_job(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    replay_public_id = "00000000-0000-4000-8000-000000000151"
    replay_id = _replay(session_factory, clock, replay_public_id, "7")
    expected = _job(
        session_factory,
        clock,
        replay_id=replay_id,
        stage="derive_features",
        identity="single-selected-job",
        priority=100,
    )
    selector = JobClaimSelectorDTO(replay_public_id, ("derive_features",))
    services = (
        _service(session_factory, clock, tmp_path),
        _service(session_factory, clock, tmp_path),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            future.result()
            for future in (
                executor.submit(
                    services[0].claim_next,
                    "00000000-0000-4000-8000-000000000906",
                    30,
                    selector,
                ),
                executor.submit(
                    services[1].claim_next,
                    "00000000-0000-4000-8000-000000000907",
                    30,
                    selector,
                ),
            )
        )

    assert sum(claim is not None for claim in claims) == 1
    assert next(claim for claim in claims if claim is not None).job_public_id == expected


def test_atomic_claim_rechecks_selector_after_candidate_selection(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    replay_public_id = "00000000-0000-4000-8000-000000000161"
    replay_id = _replay(session_factory, clock, replay_public_id, "8")
    job_public_id = _job(
        session_factory,
        clock,
        replay_id=replay_id,
        stage="derive_features",
        identity="selector-cas-candidate",
        priority=100,
    )
    bind = session_factory.kw["bind"]
    assert isinstance(bind, Engine)
    mutated = False

    def mutate_after_selection(
        connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal mutated
        if mutated or "FROM jobs" not in statement or "ORDER BY jobs.priority DESC" not in statement:
            return
        mutated = True
        connection.exec_driver_sql(  # type: ignore[attr-defined]
            "UPDATE jobs SET stage = 'telemetry' WHERE public_id = ?",
            (job_public_id,),
        )

    event.listen(bind, "after_cursor_execute", mutate_after_selection)
    try:
        claim = _service(session_factory, clock, tmp_path).claim_next(
            "00000000-0000-4000-8000-000000000908",
            30,
            JobClaimSelectorDTO(replay_public_id, ("derive_features",)),
        )
    finally:
        event.remove(bind, "after_cursor_execute", mutate_after_selection)

    assert mutated is True
    assert claim is None


def test_replay_only_claim_rechecks_registered_stage_after_candidate_selection(
    session_factory: sessionmaker[Session], clock: MutableClock, tmp_path: Path
) -> None:
    replay_public_id = "00000000-0000-4000-8000-000000000162"
    replay_id = _replay(session_factory, clock, replay_public_id, "9")
    job_public_id = _job(
        session_factory,
        clock,
        replay_id=replay_id,
        stage="derive_features",
        identity="replay-only-selector-cas-candidate",
        priority=100,
    )
    bind = session_factory.kw["bind"]
    assert isinstance(bind, Engine)
    mutated = False

    def mutate_after_selection(
        connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal mutated
        if mutated or "FROM jobs" not in statement or "ORDER BY jobs.priority DESC" not in statement:
            return
        mutated = True
        connection.exec_driver_sql(  # type: ignore[attr-defined]
            "UPDATE jobs SET stage = 'render_report' WHERE public_id = ?",
            (job_public_id,),
        )

    event.listen(bind, "after_cursor_execute", mutate_after_selection)
    try:
        claim = _service(session_factory, clock, tmp_path).claim_next(
            "00000000-0000-4000-8000-000000000909",
            30,
            JobClaimSelectorDTO(replay_public_id),
        )
    finally:
        event.remove(bind, "after_cursor_execute", mutate_after_selection)

    assert mutated is True
    assert claim is None
