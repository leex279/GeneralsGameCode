"""Exclusive bootstrap ownership and exact-schema worker safety tests."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from generals_replay_analyzer.web.bootstrap import (
    BootstrapCoordinator,
    BootstrapIntegrityError,
    BootstrapLockError,
    IncompatibleSchemaError,
    IntegrityResult,
    ProductRootLock,
    SchemaIdentityStore,
    SQLiteBackupAdapter,
)


@dataclass
class FakeSettings:
    data_root: Path
    database_path: Path
    events: list[str]

    def ensure_directories(self) -> None:
        self.events.append("directories")
        self.data_root.mkdir(parents=True, exist_ok=True)


class RecordingLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @contextmanager
    def exclusive(self, data_root: Path) -> Iterator[None]:
        assert data_root.is_dir()
        self.events.append("lock.acquire")
        try:
            yield
        finally:
            self.events.append("lock.release")


class RecordingMigrations:
    def __init__(self, events: list[str], current: str | None = "old", head: str = "accepted-head") -> None:
        self.events = events
        self.current = current
        self.head = head

    def head_revision(self, database_path: Path) -> str:
        self.events.append("migration.head")
        return self.head

    def current_revision(self, database_path: Path) -> str | None:
        self.events.append("migration.current")
        return self.current

    def upgrade(self, database_path: Path, revision: str) -> None:
        assert revision == self.head
        self.events.append("migration.upgrade")
        self.current = revision


class RecordingBackup:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def create(self, database_path: Path, backup_root: Path) -> Path:
        assert database_path.exists()
        self.events.append("backup")
        return backup_root / "backup.sqlite3"


class RecordingIntegrity:
    def __init__(self, events: list[str], result: IntegrityResult | None = None) -> None:
        self.events = events
        self.result = result or IntegrityResult(foreign_keys_enabled=True, integrity_check="ok")

    def verify(self, database_path: Path) -> IntegrityResult:
        self.events.append("integrity")
        return self.result


class RecordingIdentities:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def record(self, data_root: Path, revision: str) -> None:
        self.events.append("identity.record")


class RecordingReadiness:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def mark_usable(self, revision: str) -> None:
        self.events.append("readiness.usable")


def _coordinator(events: list[str], *, integrity: RecordingIntegrity | None = None) -> BootstrapCoordinator:
    return BootstrapCoordinator(
        lock=RecordingLock(events),
        backup=RecordingBackup(events),
        migrations=RecordingMigrations(events),
        integrity=integrity or RecordingIntegrity(events),
        identities=RecordingIdentities(events),
        readiness=RecordingReadiness(events),
    )


def test_existing_database_is_backed_up_before_upgrade_and_lock_outlives_readiness(tmp_path: Path) -> None:
    events: list[str] = []
    database = tmp_path / "library.sqlite3"
    database.write_bytes(b"existing")
    settings = FakeSettings(tmp_path, database, events)

    result = _coordinator(events).prepare(settings)

    assert result.schema_revision == "accepted-head"
    assert events == [
        "directories",
        "lock.acquire",
        "migration.head",
        "migration.current",
        "backup",
        "migration.upgrade",
        "migration.current",
        "integrity",
        "identity.record",
        "readiness.usable",
        "lock.release",
    ]


def test_integrity_failure_releases_lock_without_recording_usable_schema(tmp_path: Path) -> None:
    events: list[str] = []
    database = tmp_path / "library.sqlite3"
    database.write_bytes(b"existing")
    integrity = RecordingIntegrity(events, IntegrityResult(foreign_keys_enabled=False, integrity_check="corrupt"))

    with pytest.raises(BootstrapIntegrityError, match="integrity policy failed"):
        _coordinator(events, integrity=integrity).prepare(FakeSettings(tmp_path, database, events))

    assert events[-1] == "lock.release"
    assert "identity.record" not in events
    assert "readiness.usable" not in events


def test_product_root_lock_rejects_contention_and_is_reusable_after_release(tmp_path: Path) -> None:
    lock = ProductRootLock()

    with (
        lock.exclusive(tmp_path),
        pytest.raises(BootstrapLockError, match="already in progress"),
        ProductRootLock().exclusive(tmp_path),
    ):
        pass

    with ProductRootLock().exclusive(tmp_path):
        assert (tmp_path / ".web-bootstrap.lock").exists()


def test_product_root_lock_keeps_one_stable_path_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / ".web-bootstrap.lock"

    with ProductRootLock().exclusive(tmp_path):
        assert lock_path.exists()

    assert lock_path.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX inode regression")
def test_posix_waiter_and_next_owner_use_the_same_lock_inode(tmp_path: Path) -> None:
    lock_path = tmp_path / ".web-bootstrap.lock"

    with ProductRootLock().exclusive(tmp_path), lock_path.open("rb") as waiter:
        waiter_inode = os.fstat(waiter.fileno()).st_ino

    assert lock_path.stat().st_ino == waiter_inode
    with ProductRootLock().exclusive(tmp_path):
        assert lock_path.stat().st_ino == waiter_inode


def test_sqlite_backup_is_recoverable_and_never_overwrites_the_database(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('observed')")

    backup = SQLiteBackupAdapter().create(database, tmp_path / "backups")

    assert backup != database
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == ("observed",)


def test_worker_schema_verification_requires_exact_recorded_identity_without_migrating(tmp_path: Path) -> None:
    identities = SchemaIdentityStore()
    identities.record(tmp_path, "accepted-head")

    identities.verify_worker_schema(tmp_path, "accepted-head")
    with pytest.raises(IncompatibleSchemaError, match="schema identity is incompatible"):
        identities.verify_worker_schema(tmp_path, "future-head")
