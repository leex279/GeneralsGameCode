"""Verified-byte intake tests for path-free watched replay submissions."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer import importing
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import Job, ManagedAsset, Replay, Source
from generals_replay_analyzer.importing import ImportRequest, ImportService, VerifiedReplaySubmission
from generals_replay_analyzer.storage import ContentAddressedStore, HashMismatchError

from .conftest import MutableClock

ROOT_ONE = "123e4567-e89b-42d3-a456-426614174000"
ROOT_TWO = "123e4567-e89b-42d3-a456-426614174001"
UUID_ONE = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
UUID_NIL = "00000000-0000-0000-0000-000000000000"


def _successful_parser(path: Path) -> SimpleNamespace:
    assert path.is_file()
    return SimpleNamespace(
        commands=(object(),),
        warnings=(),
        command_stream_offset=16,
        end_offset=32,
        completion_status="complete",
    )


def _service(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
    *,
    parser: Any = _successful_parser,
    telemetry_acquirer: Any = None,
) -> ImportService:
    return ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parser,
        telemetry_acquirer=telemetry_acquirer,
        clock=clock,
        parser_version="verified-test-parser-1",
        telemetry_acquirer_version="verified-test-acquirer-1",
    )


def _submission(
    content: bytes,
    *,
    root_public_id: str = ROOT_ONE,
    relative_name: str = "league/round.rep",
    request_telemetry: bool = False,
) -> VerifiedReplaySubmission:
    return VerifiedReplaySubmission(
        content=content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        root_public_id=root_public_id,
        relative_name=relative_name,
        import_mode="copy",
        request_telemetry=request_telemetry,
    )


def _drain(service: ImportService, *, maximum: int = 20) -> None:
    for _ in range(maximum):
        if not service.run_available("verified-worker"):
            return
    raise AssertionError("verified import did not drain")


def test_verified_replay_submission_contract_is_public() -> None:
    assert getattr(importing, "VerifiedReplaySubmission", None) is not None


def test_verified_submission_is_frozen_and_rejects_unsafe_boundary_values() -> None:
    valid = _submission(b"verified replay")
    assert "verified replay" not in repr(valid)
    with pytest.raises(FrozenInstanceError):
        valid.relative_name = "changed.rep"  # type: ignore[misc]

    invalid_values = (
        {"content": b""},
        {"content": bytearray(b"mutable")},
        {"expected_sha256": valid.expected_sha256.upper()},
        {"expected_sha256": "0" * 63},
        {"root_public_id": ROOT_ONE.upper()},
        {"root_public_id": "not-a-uuid"},
        {"relative_name": "../escape.rep"},
        {"relative_name": "/absolute.rep"},
        {"relative_name": "nested\\escape.rep"},
        {"relative_name": "C:/escape.rep"},
        {"relative_name": "not-a-replay.txt"},
        {"relative_name": "nested//match.rep"},
        {"relative_name": "control\x00.rep"},
        {"import_mode": "reference"},
        {"request_telemetry": 1},
    )
    baseline: dict[str, object] = {
        "content": valid.content,
        "expected_sha256": valid.expected_sha256,
        "root_public_id": valid.root_public_id,
        "relative_name": valid.relative_name,
        "import_mode": valid.import_mode,
        "request_telemetry": valid.request_telemetry,
    }
    for replacement in invalid_values:
        values = baseline | replacement
        with pytest.raises((TypeError, ValueError)):
            VerifiedReplaySubmission(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("root_public_id", "relative_name"),
    [
        (UUID_ONE, "match.rep"),
        (UUID_NIL, "match.rep"),
        (ROOT_ONE, ".rep"),
        (ROOT_ONE, "CON.rep"),
        (ROOT_ONE, "nested/aux.txt/match.rep"),
        (ROOT_ONE, "match.rep.rep"),
        (ROOT_ONE, "match.REP"),
        (ROOT_ONE, "folder./match.rep"),
        (ROOT_ONE, "folder /match.rep"),
        (ROOT_ONE, "what?/match.rep"),
        (ROOT_ONE, "per%cent/match.rep"),
        (ROOT_ONE, "star*/match.rep"),
        (ROOT_ONE, "pipe|/match.rep"),
        (ROOT_ONE, "less</match.rep"),
        (ROOT_ONE, "format\u200b/match.rep"),
        (ROOT_ONE, unicodedata.normalize("NFD", "é") + "/match.rep"),
        (ROOT_ONE, "a" * 1021 + ".rep"),
    ],
    ids=[
        "uuid1",
        "nil-uuid",
        "empty-stem",
        "reserved-final-basename",
        "reserved-directory-basename",
        "double-replay-suffix",
        "uppercase-suffix",
        "trailing-dot",
        "trailing-space",
        "question-mark",
        "percent",
        "wildcard",
        "pipe",
        "angle-bracket",
        "unicode-format",
        "non-nfc",
        "over-limit",
    ],
)
def test_verified_submission_rejects_cross_platform_ambiguous_ingress_identifiers(
    root_public_id: str,
    relative_name: str,
) -> None:
    content = b"verified replay"
    with pytest.raises(ValueError):
        VerifiedReplaySubmission(
            content=content,
            expected_sha256=hashlib.sha256(content).hexdigest(),
            root_public_id=root_public_id,
            relative_name=relative_name,
        )


def test_verified_submission_accepts_canonical_uuid4_and_nfc_posix_name() -> None:
    request = _submission(b"verified replay", relative_name="équipe/round.2.rep")

    assert request.root_public_id == ROOT_ONE
    assert request.relative_name == "équipe/round.2.rep"


def test_verified_submit_adopts_bytes_before_return_and_queues_only_safe_identity(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"immutable watched replay"
    request = _submission(content, request_telemetry=True)
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    submit_verified = getattr(service, "submit_verified", None)

    assert submit_verified is not None
    result = submit_verified(request)

    stored = replay_store.verify(request.expected_sha256)
    assert stored.path.read_bytes() == content
    with session_factory() as session:
        discovery = session.scalar(select(Job).where(Job.public_id == result.discovery_job.public_id))
        assert discovery is not None
        assert discovery.input_json == {
            "import_mode": "copy",
            "invocation_id": cast(dict[str, object], discovery.input_json)["invocation_id"],
            "relative_name": "league/round.rep",
            "replay_sha256": request.expected_sha256,
            "request_telemetry": True,
            "root_public_id": ROOT_ONE,
            "source_kind": "verified_watched_replay",
        }
        assert str(settings.data_root) not in json.dumps(discovery.input_json)
        assert content.decode("ascii") not in json.dumps(discovery.input_json)
        assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_verified_submit_rejects_untyped_or_mismatched_content_before_queueing(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)

    with pytest.raises(TypeError, match="typed application contract"):
        service.submit_verified(cast(VerifiedReplaySubmission, object()))
    with pytest.raises(HashMismatchError):
        service.submit_verified(
            VerifiedReplaySubmission(
                content=b"different bytes",
                expected_sha256="0" * 64,
                root_public_id=ROOT_ONE,
                relative_name="match.rep",
            )
        )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert tuple(path for path in settings.managed_replay_directory.rglob("*") if path.is_file()) == ()


def test_verified_import_survives_source_replacement_and_service_restart(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    original = b"original watched replay"
    watched_source = tmp_path / "private" / "match.rep"
    watched_source.parent.mkdir()
    watched_source.write_bytes(original)
    seen: list[bytes] = []

    def parser(path: Path) -> SimpleNamespace:
        seen.append(path.read_bytes())
        return _successful_parser(path)

    first = _service(session_factory, settings, replay_store, artifact_store, clock, parser=parser)
    submit_verified = getattr(first, "submit_verified", None)
    assert submit_verified is not None
    submission = submit_verified(
        _submission(watched_source.read_bytes(), relative_name="rounds/match.rep")
    )
    watched_source.write_bytes(b"replacement bytes")
    restarted = _service(session_factory, settings, replay_store, artifact_store, clock, parser=parser)

    _drain(restarted)

    assert seen == [original]
    with session_factory() as session:
        replay = session.scalar(select(Replay))
        source = session.scalar(select(Source))
        assert replay is not None and source is not None
        assert source.replay_id == replay.id
        assert source.source_kind == "watched_root"
        assert source.original_filename == "match.rep"
        assert not Path(source.original_locator).is_absolute()
        assert str(tmp_path) not in source.original_locator
        assert source.provenance_json == {
            "content_sha256": hashlib.sha256(original).hexdigest(),
            "discovery_job_public_id": submission.discovery_job.public_id,
            "import_mode": "copy",
            "relative_name": "rounds/match.rep",
            "request_telemetry": False,
            "root_public_id": ROOT_ONE,
        }


def test_verified_discovery_fails_closed_when_managed_bytes_are_corrupt(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"verified then corrupted"
    request = _submission(content)
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    submit_verified = getattr(service, "submit_verified", None)
    assert submit_verified is not None
    submit_verified(request)
    replay_store.verify(request.expected_sha256).path.write_bytes(b"corrupt")

    completed = service.run_available("verified-worker")

    assert len(completed) == 1
    assert completed[0].stage == "discover"
    assert completed[0].status == "failed"
    assert completed[0].error_code == "managed_asset_invalid"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 0
        assert session.scalar(select(func.count()).select_from(Replay)) == 0
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 0


def test_verified_hash_revalidates_managed_bytes_after_discovery_and_restart(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"corrupt after discovery"
    request = _submission(content)
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    submit_verified = getattr(service, "submit_verified", None)
    assert submit_verified is not None
    submit_verified(request)
    discovered = service.run_available("verified-worker")
    assert len(discovered) == 1 and discovered[0].status == "succeeded"
    replay_store.verify(request.expected_sha256).path.write_bytes(b"changed")
    restarted = _service(session_factory, settings, replay_store, artifact_store, clock)

    completed = restarted.run_available("verified-worker")

    assert len(completed) == 1
    assert completed[0].stage == "hash"
    assert completed[0].status == "failed"
    assert completed[0].error_code == "managed_asset_invalid"


def test_verified_hash_fails_closed_on_managed_metadata_mismatch(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"metadata mismatch"
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit_verified(_submission(content))
    discovered = service.run_available("verified-worker")
    assert len(discovered) == 1 and discovered[0].status == "succeeded"
    with session_factory.begin() as session:
        asset = session.scalar(select(ManagedAsset))
        assert asset is not None
        asset.size_bytes += 1

    completed = service.run_available("verified-worker")

    assert len(completed) == 1
    assert completed[0].stage == "hash"
    assert completed[0].status == "failed"
    assert completed[0].error_code == "managed_asset_invalid"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("relative_path", "managed/replays/ff/" + "f" * 64),
        ("kind", "artifact"),
        ("sha256", "f" * 64),
        ("public_id", UUID_ONE),
        ("media_type", "application/octet-stream"),
    ],
)
def test_verified_hash_requires_exact_managed_asset_registration(
    field_name: str,
    invalid_value: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"exact managed registration"
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit_verified(_submission(content))
    discovered = service.run_available("verified-worker")
    assert len(discovered) == 1 and discovered[0].status == "succeeded"
    with session_factory.begin() as session:
        asset = session.scalar(select(ManagedAsset))
        assert asset is not None
        setattr(asset, field_name, invalid_value)

    completed = service.run_available("verified-worker")

    assert len(completed) == 1
    assert completed[0].stage == "hash"
    assert (completed[0].status, completed[0].error_code) == ("failed", "managed_asset_invalid")


def test_verified_discovery_rejects_invalid_exact_asset_reuse_without_new_source(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"invalid exact reuse"
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit_verified(_submission(content, relative_name="first.rep"))
    first = service.run_available("verified-worker")
    assert len(first) == 1 and first[0].status == "succeeded"
    _drain(service)
    with session_factory.begin() as session:
        asset = session.scalar(select(ManagedAsset))
        assert asset is not None
        asset.relative_path = "replays/ff/" + "f" * 64
    service.submit_verified(
        _submission(content, root_public_id=ROOT_TWO, relative_name="second.rep")
    )

    rejected = service.run_available("verified-worker")

    assert len(rejected) == 1
    assert (rejected[0].status, rejected[0].error_code) == ("failed", "managed_asset_invalid")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 1


def test_verified_discovery_rolls_back_partial_identity_and_retries_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit_verified(_submission(b"transaction rollback replay"))
    original = service._register_asset
    crash_once = True

    def crash_after_registration(session: Session, stored: object, kind: str) -> ManagedAsset:
        nonlocal crash_once
        asset = original(session, cast(Any, stored), kind)
        if crash_once:
            crash_once = False
            raise RuntimeError("simulated process crash after asset registration")
        return asset

    monkeypatch.setattr(service, "_register_asset", crash_after_registration)

    failed = service.run_available("crashing-worker")

    assert len(failed) == 1
    assert (failed[0].status, failed[0].error_code, failed[0].retryable) == (
        "pending",
        "stage_failed",
        True,
    )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 0
        assert session.scalar(select(func.count()).select_from(Replay)) == 0
        assert session.scalar(select(func.count()).select_from(Source)) == 0

    clock.advance(seconds=5)
    retried = service.run_available("recovery-worker")

    assert len(retried) == 1 and retried[0].status == "succeeded"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 1
        assert session.scalar(select(func.count()).select_from(Replay)) == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 1


def test_verified_discovery_rolls_back_source_and_graph_on_late_crash(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit_verified(_submission(b"late transaction rollback replay"))
    original = service._ensure_content_graph
    crash_once = True

    def crash_after_source(session: Session, replay: Replay, mode: str, request_telemetry: bool) -> None:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise RuntimeError("simulated crash after source registration")
        original(session, replay, mode, request_telemetry)

    monkeypatch.setattr(service, "_ensure_content_graph", crash_after_source)

    failed = service.run_available("late-crashing-worker")

    assert len(failed) == 1 and (failed[0].status, failed[0].error_code) == ("pending", "stage_failed")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 0
        assert session.scalar(select(func.count()).select_from(Replay)) == 0
        assert session.scalar(select(func.count()).select_from(Source)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 1

    clock.advance(seconds=5)
    recovered = service.run_available("late-recovery-worker")

    assert len(recovered) == 1 and recovered[0].status == "succeeded"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 1
        assert session.scalar(select(func.count()).select_from(Replay)) == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 1


def test_concurrent_verified_discoveries_preserve_one_asset_and_replay_identity(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    first = _service(session_factory, settings, replay_store, artifact_store, clock)
    second = _service(session_factory, settings, replay_store, artifact_store, clock)
    content = b"concurrent verified identity"
    first.submit_verified(_submission(content, relative_name="first.rep"))
    second.submit_verified(_submission(content, root_public_id=ROOT_TWO, relative_name="second.rep"))
    barrier = Barrier(2)
    original = ImportService._discover_verified

    def aligned_discovery(service: ImportService, claimed: object) -> dict[str, Any]:
        barrier.wait(timeout=10)
        return dict(original(service, cast(Any, claimed)))

    monkeypatch.setattr(ImportService, "_discover_verified", aligned_discovery)

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(lambda item: item.run_available("concurrent-worker"), (first, second)))

    for service, result in zip((first, second), attempts, strict=True):
        if result and result[0].retryable:
            service.retry(result[0].public_id)
            recovered = service.run_available("concurrent-recovery")
            assert recovered and recovered[0].status == "succeeded"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 1
        assert session.scalar(select(func.count()).select_from(Replay)) == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 2


def test_second_verified_discovery_rejects_conflicting_managed_asset_link(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"conflicting replay link"
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    first = service.submit_verified(_submission(content, relative_name="first.rep"))
    second = service.submit_verified(
        _submission(content, root_public_id=ROOT_TWO, relative_name="second.rep")
    )
    discovered = service.run_available("verified-worker")
    assert [job.public_id for job in discovered] == [first.discovery_job.public_id]
    other = replay_store.store_bytes(b"other managed bytes")
    with session_factory.begin() as session:
        conflicting_asset = ManagedAsset(
            public_id=str(uuid4()),
            sha256=other.sha256,
            kind="replay",
            relative_path=other.path.relative_to(settings.data_root).as_posix(),
            size_bytes=other.size,
            media_type=None,
        )
        session.add(conflicting_asset)
        session.flush()
        replay = session.scalar(select(Replay))
        assert replay is not None
        replay.managed_asset_id = conflicting_asset.id

    rejected = service.run_available("verified-worker")

    assert [job.public_id for job in rejected] == [second.discovery_job.public_id]
    assert (rejected[0].status, rejected[0].error_code) == ("failed", "managed_asset_conflict")


def test_duplicate_verified_bytes_share_content_jobs_but_retain_distinct_opaque_sources(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    content = b"same replay from two watched roots"
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    submit_verified = getattr(service, "submit_verified", None)
    assert submit_verified is not None
    first = submit_verified(_submission(content, relative_name="one.rep"))
    second = submit_verified(
        _submission(content, root_public_id=ROOT_TWO, relative_name="nested/two.rep")
    )

    _drain(service)

    with session_factory() as session:
        sources = list(session.scalars(select(Source).order_by(Source.id)))
        jobs = list(session.scalars(select(Job).order_by(Job.id)))
        replay = session.scalar(select(Replay))
        assets = list(session.scalars(select(ManagedAsset)))
        assert replay is not None
        assert len(sources) == 2
        assert len({source.public_id for source in sources}) == 2
        assert [cast(dict[str, object], source.provenance_json)["root_public_id"] for source in sources] == [
            ROOT_ONE,
            ROOT_TWO,
        ]
        assert [source.original_filename for source in sources] == ["one.rep", "two.rep"]
        assert len(assets) == 1 and assets[0].sha256 == hashlib.sha256(content).hexdigest()
        assert [job.stage for job in jobs].count("discover") == 2
        assert [job.stage for job in jobs].count("hash") == 1
        assert [job.stage for job in jobs].count("manage_copy") == 1
        assert [job.stage for job in jobs].count("parse") == 1
    assert first.discovery_job.public_id != second.discovery_job.public_id
    assert tuple(path for path in settings.managed_replay_directory.rglob("*") if path.is_file()) == (
        replay_store.verify(hashlib.sha256(content).hexdigest()).path,
    )


def test_same_content_retains_copy_reference_and_telemetry_intent_variants(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
) -> None:
    class UnusedAcquirer:
        def acquire(self, _replay: Path, _replay_sha256: str) -> object:
            raise AssertionError("telemetry execution is outside this graph-construction test")

    content = b"mixed source intent replay"
    source = tmp_path / "reference.rep"
    source.write_bytes(content)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        telemetry_acquirer=UnusedAcquirer(),
    )
    service.submit_verified(_submission(content, relative_name="copy-no-telemetry.rep"))
    service.submit_verified(
        _submission(
            content,
            root_public_id=ROOT_TWO,
            relative_name="copy-with-telemetry.rep",
            request_telemetry=True,
        )
    )
    service.submit(ImportRequest(source, reference_only=True, request_telemetry=False))

    discovered = service.run_available("intent-worker", limit=3)

    assert len(discovered) == 3 and all(job.stage == "discover" for job in discovered)
    with session_factory() as session:
        sources = list(session.scalars(select(Source).order_by(Source.id)))
        jobs = list(session.scalars(select(Job)))
        variants = [
            (
                cast(dict[str, object], source_row.provenance_json)["import_mode"],
                cast(dict[str, object], source_row.provenance_json)["request_telemetry"],
            )
            for source_row in sources
        ]
        assert variants == [("copy", False), ("copy", True), ("reference", False)]
        assert [job.input_json["import_mode"] for job in jobs if job.stage == "manage_copy"] == [
            "copy",
            "reference",
        ]
        assert [job.input_json["import_mode"] for job in jobs if job.stage == "parse"] == [
            "copy",
            "reference",
        ]
        assert [job.input_json["import_mode"] for job in jobs if job.stage == "telemetry"] == ["copy"]
