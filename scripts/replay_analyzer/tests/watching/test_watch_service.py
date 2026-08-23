from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, cast

import pytest

import generals_replay_analyzer.watching.status as status_module
from generals_replay_analyzer.importing import ImportService as AnalyticsImportService
from generals_replay_analyzer.importing import VerifiedReplaySubmission
from generals_replay_analyzer.ingress_contract import validate_root_public_id
from generals_replay_analyzer.watching import (
    RootRegistryError,
    SnapshotIngressError,
    WatchedRoot,
    WatchedRootRegistry,
)
from generals_replay_analyzer.watching.adapters import (
    RegistryWatchedRootAdapter,
    VerifiedIngressImportAdapter,
    create_analytics_watched_import_adapter,
)
from generals_replay_analyzer.watching.service import (
    WatchDiscoveryError,
    WatchedEntryDTO,
    WatchedRootSnapshotDTO,
    WatchScheduler,
)
from generals_replay_analyzer.watching.status import FileWatchStatusStore, WatchFolderStatusRecord

ROOT = "123e4567-e89b-42d3-a456-426614174000"


@dataclass
class FakeRoots:
    batches: list[tuple[WatchedRootSnapshotDTO, ...]]

    def snapshots(self) -> tuple[WatchedRootSnapshotDTO, ...]:
        return self.batches.pop(0)


@dataclass
class FakeImports:
    submissions: list[tuple[str, str]] = field(default_factory=list)
    problems: list[tuple[str, str]] = field(default_factory=list)

    def submit_stable(self, root_public_id: str, relative_name: str) -> str:
        self.submissions.append((root_public_id, relative_name))
        return "123e4567-e89b-42d3-a456-426614174001"

    def record_discovery_problem(self, root_public_id: str, problem_code: str) -> None:
        self.problems.append((root_public_id, problem_code))


def entry(name: str = "match.rep", *, size: int = 12, modified: int = 20, identity: str | None = "file-1") -> WatchedEntryDTO:
    return WatchedEntryDTO(ROOT, name, size, modified, identity)


def scan(
    *entries: WatchedEntryDTO,
    problems: tuple[str, ...] = (),
    root_public_id: str = ROOT,
    label: str = "Replay folder 1",
) -> WatchedRootSnapshotDTO:
    return WatchedRootSnapshotDTO(
        root_public_id,
        datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        entries,
        problems,
        label=label,
    )


def test_two_identical_scans_submit_once_and_repeated_scans_are_suppressed() -> None:
    roots = FakeRoots([(scan(entry()),), (scan(entry()),), (scan(entry()),)])
    imports = FakeImports()
    scheduler = WatchScheduler(roots, imports)

    assert scheduler.scan_once() == ()
    assert scheduler.scan_once() == ("123e4567-e89b-42d3-a456-426614174001",)
    assert scheduler.scan_once() == ()
    assert imports.submissions == [(ROOT, "match.rep")]


def test_changed_or_missing_entries_must_stabilize_again() -> None:
    roots = FakeRoots(
        [
            (scan(entry()),),
            (scan(entry(size=13)),),
            (scan(),),
            (scan(entry(size=13)),),
            (scan(entry(size=13)),),
        ]
    )
    imports = FakeImports()
    scheduler = WatchScheduler(roots, imports)

    for _ in range(4):
        assert scheduler.scan_once() == ()
    assert scheduler.scan_once()
    assert imports.submissions == [(ROOT, "match.rep")]


def test_rename_and_duplicate_content_are_distinct_stable_sources() -> None:
    first = entry("one.rep")
    second = entry("two.rep")
    roots = FakeRoots([(scan(first, second),), (scan(first, second),)])
    imports = FakeImports()

    scheduler = WatchScheduler(roots, imports)
    scheduler.scan_once()
    scheduler.scan_once()

    assert imports.submissions == [(ROOT, "one.rep"), (ROOT, "two.rep")]


def test_new_scheduler_conservatively_requires_two_scans() -> None:
    imports = FakeImports()
    first = WatchScheduler(FakeRoots([(scan(entry()),)]), imports)
    second = WatchScheduler(FakeRoots([(scan(entry()),), (scan(entry()),)]), imports)

    assert first.scan_once() == ()
    assert second.scan_once() == ()
    assert second.scan_once()


def test_root_problems_are_recorded_without_source_locators() -> None:
    imports = FakeImports()
    scheduler = WatchScheduler(FakeRoots([(scan(problems=("watched_root_inaccessible", "entry_extension_rejected")),)]), imports)

    assert scheduler.scan_once() == ()
    assert imports.problems == [
        (ROOT, "watched_root_inaccessible"),
        (ROOT, "entry_extension_rejected"),
    ]


def test_valid_dotted_replay_basename_is_accepted() -> None:
    dotted = WatchedEntryDTO(ROOT, "match.v2.rep", 1, 1, None)

    assert dotted.relative_name == "match.v2.rep"


def test_one_open_time_race_degrades_that_root_and_other_roots_keep_running(tmp_path: Path) -> None:
    other_root = "123e4567-e89b-42d3-a456-426614174010"

    @dataclass
    class RacingImports(FakeImports):
        def submit_stable(self, root_public_id: str, relative_name: str) -> str:
            if root_public_id == ROOT:
                raise WatchDiscoveryError("replay_source_changed")
            return super().submit_stable(root_public_id, relative_name)

    first = entry("first.rep")
    second = WatchedEntryDTO(other_root, "second.rep", 3, 4, "file-2")
    roots = FakeRoots(
        [
            (scan(first), scan(second, root_public_id=other_root, label="Replay folder 2")),
            (scan(first), scan(second, root_public_id=other_root, label="Replay folder 2")),
        ]
    )
    imports = RacingImports()
    statuses = FileWatchStatusStore(tmp_path / "product")
    scheduler = WatchScheduler(roots, imports, status_store=statuses)

    assert scheduler.scan_once() == ()
    assert scheduler.scan_once() == ("123e4567-e89b-42d3-a456-426614174001",)
    assert imports.problems == [(ROOT, "replay_source_changed")]
    records = {record.root_public_id: record for record in statuses.read()}
    assert records[ROOT].state == "degraded"
    assert records[ROOT].reason_code == "replay_source_changed"
    assert records[other_root].state == "idle"


def test_root_collection_race_is_path_free_and_does_not_escape_after_a_known_scan(tmp_path: Path) -> None:
    class RacingRoots:
        def __init__(self) -> None:
            self.calls = 0

        def snapshots(self) -> tuple[WatchedRootSnapshotDTO, ...]:
            self.calls += 1
            if self.calls == 1:
                return (scan(entry()),)
            raise WatchDiscoveryError("watched_root_registry_changed")

    imports = FakeImports()
    statuses = FileWatchStatusStore(tmp_path / "product")
    scheduler = WatchScheduler(RacingRoots(), imports, status_store=statuses)

    assert scheduler.scan_once() == ()
    assert scheduler.scan_once() == ()
    assert imports.problems == [(ROOT, "watched_root_registry_changed")]
    assert statuses.read()[0].state == "degraded"
    assert statuses.read()[0].reason_code == "watched_root_registry_changed"


def test_restart_uses_persisted_safe_status_when_the_first_root_collection_fails(tmp_path: Path) -> None:
    class FailingRoots:
        def snapshots(self) -> tuple[WatchedRootSnapshotDTO, ...]:
            raise WatchDiscoveryError("watched_root_registry_changed")

    statuses = FileWatchStatusStore(tmp_path / "product")
    statuses.publish(
        (
            WatchFolderStatusRecord(
                ROOT,
                "Replay folder 1",
                "idle",
                datetime(2026, 8, 22, 11, 0, tzinfo=UTC),
                None,
            ),
        )
    )
    imports = FakeImports()

    scheduler = WatchScheduler(FailingRoots(), imports, status_store=statuses)

    assert scheduler.scan_once() == ()
    assert imports.problems == [(ROOT, "watched_root_registry_changed")]
    assert statuses.read() == (
        WatchFolderStatusRecord(
            ROOT,
            "Replay folder 1",
            "degraded",
            statuses.read()[0].last_scan_at_utc,
            "watched_root_registry_changed",
        ),
    )


def test_successful_root_snapshot_prunes_removed_status_records(tmp_path: Path) -> None:
    removed_root = "123e4567-e89b-42d3-a456-426614174010"
    statuses = FileWatchStatusStore(tmp_path / "product")
    statuses.publish(
        (
            WatchFolderStatusRecord(ROOT, "Replay folder 1", "idle", None, None),
            WatchFolderStatusRecord(removed_root, "Replay folder 2", "idle", None, None),
        )
    )

    scheduler = WatchScheduler(FakeRoots([(scan(),)]), FakeImports(), status_store=statuses)

    assert scheduler.scan_once() == ()
    assert tuple(record.root_public_id for record in statuses.read()) == (ROOT,)


def test_file_status_store_is_atomic_and_contains_only_public_safe_status(tmp_path: Path) -> None:
    store = FileWatchStatusStore(tmp_path / "product")
    scanned_at = datetime(2026, 8, 22, 13, 45, tzinfo=UTC)
    record = WatchFolderStatusRecord(
        root_public_id=ROOT,
        label="Replay folder 1",
        state="degraded",
        last_scan_at_utc=scanned_at,
        reason_code="watched_root_scan_failed",
    )

    store.publish((record,))

    assert store.read() == (record,)
    serialized = (tmp_path / "product" / "watch-status-v1.json").read_text(encoding="utf-8")
    assert "watched_root_scan_failed" in serialized
    assert "source.rep" not in serialized
    assert str(tmp_path) not in serialized


@pytest.mark.parametrize(
    "make_invalid",
    [
        lambda: WatchFolderStatusRecord("bad", "Replay folder 1", "idle", None, None),
        lambda: WatchFolderStatusRecord(ROOT, "private folder", "idle", None, None),
        lambda: WatchFolderStatusRecord(ROOT, "Replay folder 1", "unknown", None, None),  # type: ignore[arg-type]
        lambda: WatchFolderStatusRecord(
            ROOT,
            "Replay folder 1",
            "idle",
            datetime(2026, 8, 22, 12, 0),  # noqa: DTZ001
            None,
        ),
        lambda: WatchFolderStatusRecord(ROOT, "Replay folder 1", "degraded", None, None),
        lambda: WatchFolderStatusRecord(ROOT, "Replay folder 1", "idle", None, "watched_root_failed"),
        lambda: WatchFolderStatusRecord(ROOT, "Replay folder 1", "degraded", None, "bad-code"),
    ],
)
def test_watch_status_record_rejects_nonpublic_or_inconsistent_values(make_invalid: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        make_invalid()


def test_file_status_store_rejects_mutable_or_duplicate_public_records(tmp_path: Path) -> None:
    store = FileWatchStatusStore(tmp_path / "product")
    record = WatchFolderStatusRecord(ROOT, "Replay folder 1", "idle", None, None)

    with pytest.raises(TypeError):
        store.publish([record])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate roots"):
        store.publish((record, record))


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"version":1,"version":1,"roots":[]}',
        b"{}",
        b'{"version":2,"roots":[]}',
        b'{"version":1,"roots":{}}',
        b'{"version":1,"roots":[null]}',
        (
            b'{"version":1,"roots":[{"root_public_id":"123e4567-e89b-42d3-a456-426614174000",'
            b'"label":"Replay folder 1","state":"idle","last_scan_at_utc":5,"reason_code":null}]}'
        ),
        b"x" * (256 * 1024 + 1),
    ],
    ids=[
        "invalid-json",
        "duplicate-key",
        "wrong-shape",
        "wrong-version",
        "wrong-roots-type",
        "invalid-record",
        "invalid-timestamp",
        "oversize",
    ],
)
def test_file_status_store_fails_closed_on_malformed_or_oversize_documents(tmp_path: Path, payload: bytes) -> None:
    root = tmp_path / "product"
    root.mkdir()
    (root / "watch-status-v1.json").write_bytes(payload)

    assert FileWatchStatusStore(root).read() == ()


def test_file_status_store_reads_only_a_bounded_open_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    store = FileWatchStatusStore(root)
    store.publish((WatchFolderStatusRecord(ROOT, "Replay folder 1", "idle", None, None),))
    real_fdopen = os.fdopen
    read_sizes: list[int] = []

    class BoundedReader:
        def __init__(self, descriptor: int, mode: str) -> None:
            self._handle = real_fdopen(descriptor, mode)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_ignored: object) -> None:
            self._handle.close()

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return self._handle.read(size)

    monkeypatch.setattr(status_module.os, "fdopen", BoundedReader)

    assert store.read()[0].root_public_id == ROOT
    assert read_sizes == [status_module._MAX_STATUS_BYTES + 1]


def test_file_status_store_rejects_identity_replacement_between_validation_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    store = FileWatchStatusStore(root)
    store.publish((WatchFolderStatusRecord(ROOT, "Replay folder 1", "idle", None, None),))
    status_path = root / "watch-status-v1.json"
    replacement = root / "replacement.json"
    replacement.write_text('{"version":1,"roots":[]}', encoding="utf-8")
    real_open = os.open

    def replace_then_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int) -> int:
        os.replace(replacement, status_path)
        return real_open(path, flags)

    monkeypatch.setattr(status_module.os, "open", replace_then_open)

    assert store.read() == ()


def test_file_status_store_rejects_duplicate_root_records_on_read(tmp_path: Path) -> None:
    root = tmp_path / "product"
    root.mkdir()
    record = WatchFolderStatusRecord(ROOT, "Replay folder 1", "idle", None, None).document()
    (root / "watch-status-v1.json").write_text(
        json.dumps({"version": 1, "roots": [record, record]}),
        encoding="utf-8",
    )

    assert FileWatchStatusStore(root).read() == ()
    assert not tuple((tmp_path / "product").glob("*.tmp"))


def test_production_import_adapter_reads_verified_bytes_and_never_uses_snapshot_path() -> None:
    class Snapshot:
        sha256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

        @property
        def snapshot_path(self) -> Path:
            raise AssertionError("direct snapshot_path use is forbidden")

        def read_verified_bytes(self) -> bytes:
            return b"hello"

    class Registry:
        def snapshot_replay(self, root_public_id: str, relative_name: str) -> Snapshot:
            assert (root_public_id, relative_name) == (ROOT, "match.rep")
            return Snapshot()

    submitted: list[tuple[bytes, str, str, str]] = []

    def submit(content: bytes, expected_sha256: str, root_public_id: str, relative_name: str) -> str:
        submitted.append((content, expected_sha256, root_public_id, relative_name))
        return "123e4567-e89b-42d3-a456-426614174099"

    adapter = VerifiedIngressImportAdapter(Registry(), submit)

    assert adapter.submit_stable(ROOT, "match.rep") == "123e4567-e89b-42d3-a456-426614174099"
    assert submitted == [(b"hello", Snapshot.sha256, ROOT, "match.rep")]


def test_production_import_adapter_rejects_a_changed_verified_digest() -> None:
    class Snapshot:
        sha256 = "0" * 64

        def read_verified_bytes(self) -> bytes:
            return b"changed after verification"

    class Registry:
        def snapshot_replay(self, _root_public_id: str, _relative_name: str) -> Snapshot:
            return Snapshot()

    adapter = VerifiedIngressImportAdapter(Registry(), lambda *_args: "unused")

    with pytest.raises(WatchDiscoveryError) as failure:
        adapter.submit_stable(ROOT, "match.rep")
    assert failure.value.code == "ingress_snapshot_changed"


def test_import_service_failure_becomes_a_path_free_discovery_problem(tmp_path: Path) -> None:
    content = b"verified"

    class Snapshot:
        sha256 = hashlib.sha256(content).hexdigest()

        def read_verified_bytes(self) -> bytes:
            return content

    class Registry:
        def snapshot_replay(self, _root_public_id: str, _relative_name: str) -> Snapshot:
            return Snapshot()

    def fail_import(*_args: object) -> str:
        raise RuntimeError(f"database failed beside {tmp_path}")

    adapter = VerifiedIngressImportAdapter(Registry(), fail_import)

    with pytest.raises(WatchDiscoveryError) as failure:
        adapter.submit_stable(ROOT, "match.rep")
    assert failure.value.code == "watched_import_unavailable"
    assert str(tmp_path) not in str(failure.value)


def test_existing_stable_import_failure_is_preserved() -> None:
    content = b"verified"

    class Snapshot:
        sha256 = hashlib.sha256(content).hexdigest()

        def read_verified_bytes(self) -> bytes:
            return content

    class Registry:
        def snapshot_replay(self, _root_public_id: str, _relative_name: str) -> Snapshot:
            return Snapshot()

    def fail_import(*_args: object) -> str:
        raise WatchDiscoveryError("watched_import_unavailable")

    with pytest.raises(WatchDiscoveryError) as failure:
        VerifiedIngressImportAdapter(Registry(), fail_import).submit_stable(ROOT, "match.rep")
    assert failure.value.code == "watched_import_unavailable"


def test_open_time_ingress_race_is_translated_to_a_scheduler_problem() -> None:
    class Snapshot:
        sha256 = "0" * 64

        def read_verified_bytes(self) -> bytes:
            return b"unused"

    class Registry:
        def snapshot_replay(self, _root_public_id: str, _relative_name: str) -> Snapshot:
            raise SnapshotIngressError("replay_source_changed")

    adapter = VerifiedIngressImportAdapter(Registry(), lambda *_args: "unused")

    with pytest.raises(WatchDiscoveryError) as failure:
        adapter.submit_stable(ROOT, "match.rep")
    assert failure.value.code == "replay_source_changed"


def test_open_time_read_oserror_is_translated_without_a_private_locator(tmp_path: Path) -> None:
    class Snapshot:
        sha256 = "0" * 64

        def read_verified_bytes(self) -> bytes:
            raise OSError(f"private source beside {tmp_path}")

    class Registry:
        def snapshot_replay(self, _root_public_id: str, _relative_name: str) -> Snapshot:
            return Snapshot()

    with pytest.raises(WatchDiscoveryError) as failure:
        VerifiedIngressImportAdapter(Registry(), lambda *_args: "unused").submit_stable(ROOT, "match.rep")
    assert failure.value.code == "replay_source_changed"
    assert str(tmp_path) not in str(failure.value)


def test_analytics_import_factory_submits_exact_verified_content_without_a_path() -> None:
    content = b"verified"

    class Snapshot:
        sha256 = hashlib.sha256(content).hexdigest()

        def read_verified_bytes(self) -> bytes:
            return content

    class Registry:
        def snapshot_replay(self, _root_public_id: str, _relative_name: str) -> Snapshot:
            return Snapshot()

    @dataclass(frozen=True)
    class DiscoveryJob:
        public_id: str

    @dataclass(frozen=True)
    class Submission:
        discovery_job: DiscoveryJob

    class ImportService:
        def __init__(self) -> None:
            self.received: list[object] = []

        def submit_verified(self, request: object) -> Submission:
            self.received.append(request)
            return Submission(DiscoveryJob("123e4567-e89b-42d3-a456-426614174099"))

    service = ImportService()
    adapter = create_analytics_watched_import_adapter(
        Registry(),
        cast(AnalyticsImportService, service),
        request_telemetry=True,
    )

    assert adapter.submit_stable(ROOT, "match.rep") == "123e4567-e89b-42d3-a456-426614174099"
    assert len(service.received) == 1
    request = cast(VerifiedReplaySubmission, service.received[0])
    assert request.content == content
    assert request.expected_sha256 == hashlib.sha256(content).hexdigest()
    assert request.root_public_id == ROOT
    assert request.relative_name == "match.rep"
    assert request.request_telemetry is True


def test_production_import_adapter_records_only_path_free_discovery_codes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = VerifiedIngressImportAdapter(object(), lambda *_args: "unused")  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        adapter.record_discovery_problem(ROOT, "watched_root_scan_failed")

    assert caplog.messages == [
        f"watched replay discovery problem root_public_id={ROOT} code=watched_root_scan_failed"
    ]
    assert str(tmp_path) not in caplog.text


def test_file_backed_watcher_stabilizes_and_preserves_the_source(tmp_path: Path) -> None:
    watched = tmp_path / "watched"
    watched.mkdir()
    source = watched / "league.rep"
    source.write_bytes(b"retail replay bytes")
    registry = WatchedRootRegistry(tmp_path / "product")
    roots = RegistryWatchedRootAdapter(registry, (watched,))
    submitted: list[tuple[bytes, str, str, str]] = []

    def submit(content: bytes, digest: str, root_public_id: str, relative_name: str) -> str:
        submitted.append((content, digest, root_public_id, relative_name))
        return "123e4567-e89b-42d3-a456-426614174088"

    imports = VerifiedIngressImportAdapter(registry, submit)
    scheduler = WatchScheduler(roots, imports)

    assert scheduler.scan_once() == ()
    assert scheduler.scan_once() == ("123e4567-e89b-42d3-a456-426614174088",)
    content, digest, root_public_id, relative_name = submitted[0]
    assert content == b"retail replay bytes"
    assert digest == hashlib.sha256(content).hexdigest()
    assert validate_root_public_id(root_public_id) == root_public_id
    assert relative_name == "league.rep"
    assert source.read_bytes() == b"retail replay bytes"
    assert not (tmp_path / "product" / "watched-handoff").exists()


def test_root_adapter_allows_dots_before_the_final_replay_suffix(tmp_path: Path) -> None:
    watched = tmp_path / "watched"
    nested = watched / "league.2026"
    nested.mkdir(parents=True)
    (nested / "round.v2.rep").write_bytes(b"replay")
    registry = WatchedRootRegistry(tmp_path / "product")

    snapshots = RegistryWatchedRootAdapter(registry, (watched,)).snapshots()

    assert tuple(item.relative_name for item in snapshots[0].entries) == ("league.2026/round.v2.rep",)
    assert snapshots[0].problem_codes == ()


def test_root_adapter_reports_unavailable_roots_and_rejected_extensions(tmp_path: Path) -> None:
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "notes.txt").write_text("not a replay", encoding="utf-8")

    unavailable = RegistryWatchedRootAdapter._snapshot(
        watched,
        WatchedRoot(ROOT, "Replay folder 1", False, "watched_root_unavailable"),
    )
    available = RegistryWatchedRootAdapter._snapshot(
        watched,
        WatchedRoot(ROOT, "Replay folder 1", True, None),
    )

    assert unavailable.problem_codes == ("watched_root_unavailable",)
    assert available.problem_codes == ("entry_extension_rejected",)


def test_root_adapter_delegates_unsafe_replay_names_to_the_neutral_ingress_contract(tmp_path: Path) -> None:
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "%ambiguous.rep").write_bytes(b"replay")
    registry = WatchedRootRegistry(tmp_path / "product")

    snapshot = RegistryWatchedRootAdapter(registry, (watched,)).snapshots()[0]

    assert snapshot.entries == ()
    assert snapshot.problem_codes == ("replay_relative_name_invalid",)


def test_root_registry_race_degrades_known_roots_without_escaping(tmp_path: Path) -> None:
    watched = tmp_path / "watched"
    watched.mkdir()

    class Registry:
        def __init__(self) -> None:
            self.calls = 0

        def reconcile(self, _folders: tuple[Path, ...]) -> tuple[WatchedRoot, ...]:
            self.calls += 1
            if self.calls == 1:
                return (WatchedRoot(ROOT, "Replay folder 1", True, None),)
            raise RootRegistryError("watched_root_registry_changed")

    adapter = RegistryWatchedRootAdapter(Registry(), (watched,))  # type: ignore[arg-type]

    assert adapter.snapshots()[0].problem_codes == ()
    degraded = adapter.snapshots()
    assert len(degraded) == 1
    assert degraded[0].root_public_id == ROOT
    assert degraded[0].problem_codes == ("watched_root_registry_changed",)


def test_initial_root_registry_failure_is_explicit_instead_of_an_empty_success(tmp_path: Path) -> None:
    class Registry:
        def reconcile(self, _folders: tuple[Path, ...]) -> tuple[WatchedRoot, ...]:
            raise RootRegistryError("watched_root_registry_unavailable")

    adapter = RegistryWatchedRootAdapter(Registry(), (tmp_path / "watched",))  # type: ignore[arg-type]

    with pytest.raises(WatchDiscoveryError) as raised:
        adapter.snapshots()

    assert raised.value.code == "watched_root_registry_unavailable"


@pytest.mark.parametrize(
    "make_invalid",
    [
        lambda: WatchedEntryDTO("UPPER", "match.rep", 1, 1, None),
        lambda: WatchedEntryDTO(ROOT, "../match.rep", 1, 1, None),
        lambda: WatchedEntryDTO(ROOT, "%ambiguous.rep", 1, 1, None),
        lambda: WatchedEntryDTO(ROOT, "match.rep.rep", 1, 1, None),
        lambda: WatchedEntryDTO(ROOT, "match.rep", 0, 1, None),
        lambda: WatchedEntryDTO(ROOT, "match.rep", 1, -1, None),
        lambda: WatchedEntryDTO(ROOT, "match.rep", 1, 1, ""),
        lambda: WatchedRootSnapshotDTO(ROOT, datetime(2026, 8, 22, 12, 0), (), ()),  # noqa: DTZ001
        lambda: WatchedRootSnapshotDTO(ROOT, datetime(2026, 8, 22, 12, 0, tzinfo=UTC), [], ()),  # type: ignore[arg-type]
        lambda: WatchedRootSnapshotDTO(
            ROOT,
            datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
            (replace(entry(), root_public_id="123e4567-e89b-42d3-a456-426614174099"),),
            (),
        ),
        lambda: WatchedRootSnapshotDTO(ROOT, datetime(2026, 8, 22, 12, 0, tzinfo=UTC), (entry(), entry()), ()),
        lambda: WatchedRootSnapshotDTO(ROOT, datetime(2026, 8, 22, 12, 0, tzinfo=UTC), (), ("bad-code!",)),
    ],
)
def test_watched_scheduler_contract_rejects_mutable_or_unsafe_snapshot_values(
    make_invalid: Callable[[], object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        make_invalid()
