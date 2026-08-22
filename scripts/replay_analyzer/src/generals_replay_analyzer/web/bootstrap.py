"""Single-owner database bootstrap and exact-schema worker guard."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4


class BootstrapError(RuntimeError):
    """Base class for controlled startup failures."""


class BootstrapLockError(BootstrapError):
    """Another bootstrap coordinator owns the product root."""


class BootstrapIntegrityError(BootstrapError):
    """The migrated database failed its required SQLite checks."""


class IncompatibleSchemaError(BootstrapError):
    """A worker observed a schema other than the coordinator-recorded identity."""


@dataclass(frozen=True)
class IntegrityResult:
    foreign_keys_enabled: bool
    integrity_check: str


@dataclass(frozen=True)
class BootstrapResult:
    schema_revision: str
    backup_path: Path | None


class BootstrapSettings(Protocol):
    data_root: Path
    database_path: Path

    def ensure_directories(self) -> None: ...


class BootstrapLock(Protocol):
    def exclusive(self, data_root: Path) -> AbstractContextManager[None]: ...


class DatabaseBackup(Protocol):
    def create(self, database_path: Path, backup_root: Path) -> Path: ...


class MigrationAdapter(Protocol):
    def head_revision(self, database_path: Path) -> str: ...

    def current_revision(self, database_path: Path) -> str | None: ...

    def upgrade(self, database_path: Path, revision: str) -> None: ...


class IntegrityAdapter(Protocol):
    def verify(self, database_path: Path) -> IntegrityResult: ...


class SchemaIdentities(Protocol):
    def record(self, data_root: Path, revision: str) -> None: ...


class BootstrapReadiness(Protocol):
    def mark_usable(self, revision: str) -> None: ...


class ProductRootLock:
    """Hold a nonblocking operating-system lock for one product root."""

    @contextmanager
    def exclusive(self, data_root: Path) -> Iterator[None]:
        lock_path = data_root / ".web-bootstrap.lock"
        handle = lock_path.open("a+b")
        acquired = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl: Any = __import__("fcntl")
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise BootstrapLockError("web bootstrap is already in progress") from error
            acquired = True
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = __import__("fcntl")
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class SQLiteBackupAdapter:
    """Create a consistent recoverable SQLite backup before migration."""

    def create(self, database_path: Path, backup_root: Path) -> Path:
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = backup_root / f"{database_path.stem}-{stamp}-{uuid4().hex}.sqlite3"
        with sqlite3.connect(database_path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        return destination


class PackageMigrationAdapter:
    """Use only the accepted package-owned Alembic graph."""

    def head_revision(self, database_path: Path) -> str:
        from alembic.script import ScriptDirectory

        from generals_replay_analyzer.db.migration import make_alembic_config

        heads = ScriptDirectory.from_config(make_alembic_config(database_path)).get_heads()
        if len(heads) != 1:
            raise BootstrapError("packaged migration graph must have exactly one head")
        return heads[0]

    def current_revision(self, database_path: Path) -> str | None:
        if not database_path.is_file():
            return None
        try:
            with sqlite3.connect(database_path) as connection:
                row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else str(row[0])

    def upgrade(self, database_path: Path, revision: str) -> None:
        from generals_replay_analyzer.db.migration import upgrade_database

        upgrade_database(database_path, revision)


class SQLiteIntegrityAdapter:
    """Verify the same foreign-key policy and database integrity used at runtime."""

    def verify(self, database_path: Path) -> IntegrityResult:
        from sqlalchemy import text

        from generals_replay_analyzer.db.session import create_database_engine

        engine = create_database_engine(database_path)
        try:
            with engine.connect() as connection:
                foreign_keys = int(connection.execute(text("PRAGMA foreign_keys")).scalar_one())
                integrity = str(connection.execute(text("PRAGMA integrity_check")).scalar_one())
        finally:
            engine.dispose()
        return IntegrityResult(foreign_keys_enabled=foreign_keys == 1, integrity_check=integrity)


class SchemaIdentityStore:
    """Persist and verify the exact schema identity workers must consume."""

    _FILENAME = "schema-identity.json"

    def record(self, data_root: Path, revision: str) -> None:
        destination = data_root / self._FILENAME
        temporary = data_root / f".{self._FILENAME}.{uuid4().hex}.tmp"
        payload = {"schema_revision": revision}
        temporary.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(destination)

    def verify_worker_schema(self, data_root: Path, actual_revision: str) -> None:
        try:
            payload = json.loads((data_root / self._FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise IncompatibleSchemaError("worker schema identity is incompatible") from error
        if payload != {"schema_revision": actual_revision}:
            raise IncompatibleSchemaError("worker schema identity is incompatible")


class BootstrapReadinessState:
    """Publish only the accepted schema revision after successful bootstrap."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._schema_revision: str | None = None

    def mark_usable(self, revision: str) -> None:
        with self._lock:
            self._schema_revision = revision

    def schema_revision(self) -> str | None:
        with self._lock:
            return self._schema_revision


# TheSuperHackers @feature Leex 22/08/2026 Give web bootstrap sole migration ownership before readiness. (#TBD)
class BootstrapCoordinator:
    def __init__(
        self,
        *,
        lock: BootstrapLock,
        backup: DatabaseBackup,
        migrations: MigrationAdapter,
        integrity: IntegrityAdapter,
        identities: SchemaIdentities,
        readiness: BootstrapReadiness,
    ) -> None:
        self._lock = lock
        self._backup = backup
        self._migrations = migrations
        self._integrity = integrity
        self._identities = identities
        self._readiness = readiness

    def prepare(self, settings: BootstrapSettings) -> BootstrapResult:
        settings.ensure_directories()
        with self._lock.exclusive(settings.data_root):
            database_existed = settings.database_path.is_file()
            head = self._migrations.head_revision(settings.database_path)
            current = self._migrations.current_revision(settings.database_path)
            backup_path = None
            if database_existed and current != head:
                backup_path = self._backup.create(settings.database_path, settings.data_root / "backups")
            self._migrations.upgrade(settings.database_path, head)
            if self._migrations.current_revision(settings.database_path) != head:
                raise BootstrapIntegrityError("database schema did not reach the accepted head")
            integrity = self._integrity.verify(settings.database_path)
            if not integrity.foreign_keys_enabled or integrity.integrity_check != "ok":
                raise BootstrapIntegrityError("database integrity policy failed")
            self._identities.record(settings.data_root, head)
            self._readiness.mark_usable(head)
            return BootstrapResult(schema_revision=head, backup_path=backup_path)


def create_production_bootstrapper(readiness: BootstrapReadinessState) -> BootstrapCoordinator:
    """Compose bootstrap infrastructure without importing Analytics application adapters."""
    return BootstrapCoordinator(
        lock=ProductRootLock(),
        backup=SQLiteBackupAdapter(),
        migrations=PackageMigrationAdapter(),
        integrity=SQLiteIntegrityAdapter(),
        identities=SchemaIdentityStore(),
        readiness=readiness,
    )
