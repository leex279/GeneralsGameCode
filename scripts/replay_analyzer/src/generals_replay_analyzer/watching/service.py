"""Pure two-scan watched-folder stabilization scheduler."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..ingress_contract import validate_replay_relative_name, validate_root_public_id
from .status import WatchFolderStatusRecord

_ROOT_LABEL = re.compile(r"^Replay folder [1-9][0-9]{0,8}$")


def _problem_code(value: str) -> None:
    if not value or len(value) > 128 or not value.replace("_", "").isalnum():
        raise ValueError("watch problem code is invalid")


class WatchDiscoveryError(RuntimeError):
    """Path-free expected watcher failure with one stable problem code."""

    def __init__(self, code: str) -> None:
        _problem_code(code)
        self.code = code
        super().__init__("Watched replay discovery could not be completed.")


def _public_id(value: str) -> None:
    validate_root_public_id(value)


def _relative_replay_name(value: str) -> None:
    validate_replay_relative_name(value)


@dataclass(frozen=True, slots=True)
class WatchedEntryDTO:
    root_public_id: str
    relative_name: str
    size_bytes: int
    modified_time_ns: int
    file_identity: str | None

    def __post_init__(self) -> None:
        _public_id(self.root_public_id)
        _relative_replay_name(self.relative_name)
        if type(self.size_bytes) is not int or self.size_bytes < 1:
            raise ValueError("size_bytes must be positive")
        if type(self.modified_time_ns) is not int or self.modified_time_ns < 0:
            raise ValueError("modified_time_ns must be nonnegative")
        if self.file_identity is not None and (not self.file_identity or len(self.file_identity) > 256):
            raise ValueError("file_identity is invalid")


@dataclass(frozen=True, slots=True)
class WatchedRootSnapshotDTO:
    root_public_id: str
    scanned_at_utc: datetime
    entries: tuple[WatchedEntryDTO, ...]
    problem_codes: tuple[str, ...]
    label: str = "Replay folder 1"

    def __post_init__(self) -> None:
        _public_id(self.root_public_id)
        if self.scanned_at_utc.tzinfo is None or self.scanned_at_utc.utcoffset() != datetime.now(UTC).utcoffset():
            raise ValueError("scanned_at_utc must use UTC")
        if not isinstance(self.entries, tuple) or not isinstance(self.problem_codes, tuple):
            raise TypeError("watch snapshots require immutable tuples")
        if any(entry.root_public_id != self.root_public_id for entry in self.entries):
            raise ValueError("entry root does not match its snapshot")
        if _ROOT_LABEL.fullmatch(self.label) is None:
            raise ValueError("watch root label must be an application-owned generic label")
        if len({entry.relative_name for entry in self.entries}) != len(self.entries):
            raise ValueError("watch snapshot contains duplicate relative names")
        for code in self.problem_codes:
            _problem_code(code)


class WatchedRootPort(Protocol):
    def snapshots(self) -> tuple[WatchedRootSnapshotDTO, ...]: ...


class WatchedImportPort(Protocol):
    def submit_stable(self, root_public_id: str, relative_name: str) -> str: ...

    def record_discovery_problem(self, root_public_id: str, problem_code: str) -> None: ...


class WatchStatusStorePort(Protocol):
    def read(self) -> tuple[WatchFolderStatusRecord, ...]: ...

    def publish(self, records: tuple[WatchFolderStatusRecord, ...]) -> None: ...


_Fingerprint = tuple[int, int, str | None]


# TheSuperHackers @feature Leex 22/08/2026 Stabilize watched replay discoveries without threads, paths, or source mutation. (#TBD)
class WatchScheduler:
    """Submit an opaque replay selection only after two identical scans."""

    def __init__(
        self,
        roots: WatchedRootPort,
        imports: WatchedImportPort,
        *,
        status_store: WatchStatusStorePort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._roots = roots
        self._imports = imports
        self._status_store = status_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._previous: dict[tuple[str, str], _Fingerprint] = {}
        self._submitted: dict[tuple[str, str], _Fingerprint] = {}
        persisted_statuses: tuple[WatchFolderStatusRecord, ...] = ()
        if status_store is not None:
            try:
                persisted_statuses = status_store.read()
            except (OSError, ValueError):
                pass
        self._known_statuses: dict[str, WatchFolderStatusRecord] = {
            record.root_public_id: record for record in persisted_statuses
        }

    def _record_problem(self, root_public_id: str, problem_code: str) -> None:
        try:
            self._imports.record_discovery_problem(root_public_id, problem_code)
        except (WatchDiscoveryError, OSError, ValueError):
            pass

    def _publish_statuses(self) -> None:
        if self._status_store is None:
            return
        try:
            self._status_store.publish(tuple(self._known_statuses.values()))
        except (OSError, ValueError):
            pass

    def scan_once(self) -> tuple[str, ...]:
        current: dict[tuple[str, str], _Fingerprint] = {}
        current_statuses: dict[str, WatchFolderStatusRecord] = {}
        submitted: list[str] = []
        try:
            snapshots = self._roots.snapshots()
        except WatchDiscoveryError as error:
            attempted_at = self._clock()
            for root_public_id, previous in tuple(self._known_statuses.items()):
                self._record_problem(root_public_id, error.code)
                self._known_statuses[root_public_id] = WatchFolderStatusRecord(
                    root_public_id,
                    previous.label,
                    "degraded",
                    attempted_at,
                    error.code,
                )
            self._previous = {}
            self._publish_statuses()
            return ()
        for snapshot in snapshots:
            reason_code = snapshot.problem_codes[0] if snapshot.problem_codes else None
            for problem_code in snapshot.problem_codes:
                self._record_problem(snapshot.root_public_id, problem_code)
            for entry in snapshot.entries:
                key = (entry.root_public_id, entry.relative_name)
                fingerprint = (entry.size_bytes, entry.modified_time_ns, entry.file_identity)
                current[key] = fingerprint
                if self._previous.get(key) != fingerprint or self._submitted.get(key) == fingerprint:
                    continue
                try:
                    submitted.append(self._imports.submit_stable(*key))
                    self._submitted[key] = fingerprint
                except WatchDiscoveryError as error:
                    reason_code = reason_code or error.code
                    self._record_problem(snapshot.root_public_id, error.code)
            current_statuses[snapshot.root_public_id] = WatchFolderStatusRecord(
                snapshot.root_public_id,
                snapshot.label,
                "degraded" if reason_code is not None else "idle",
                snapshot.scanned_at_utc,
                reason_code,
            )
        self._previous = current
        self._known_statuses = current_statuses
        self._publish_statuses()
        return tuple(submitted)
