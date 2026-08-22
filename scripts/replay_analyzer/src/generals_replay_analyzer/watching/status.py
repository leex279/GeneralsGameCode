"""Atomic path-free watched-folder status shared by worker and Web readers."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

_STATUS_NAME = "watch-status-v1.json"
_STATUS_VERSION = 1
_MAX_STATUS_BYTES = 256 * 1024
_SAFE_LABEL = re.compile(r"^Replay folder [1-9][0-9]{0,8}$")
_SAFE_CODE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
WatchFolderState = Literal["idle", "scanning", "degraded", "disabled"]


def _canonical_public_id(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (AttributeError, ValueError):
        return False


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


@dataclass(frozen=True, slots=True)
class WatchFolderStatusRecord:
    """One public-safe durable watched-folder state."""

    root_public_id: str
    label: str
    state: WatchFolderState
    last_scan_at_utc: datetime | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if not _canonical_public_id(self.root_public_id):
            raise ValueError("root_public_id must be a lowercase hyphenated UUID")
        if _SAFE_LABEL.fullmatch(self.label) is None:
            raise ValueError("watch label must be an application-owned generic label")
        if self.state not in {"idle", "scanning", "degraded", "disabled"}:
            raise ValueError("watch state is invalid")
        if self.last_scan_at_utc is not None and (
            self.last_scan_at_utc.tzinfo is None
            or self.last_scan_at_utc.utcoffset() != datetime.now(UTC).utcoffset()
        ):
            raise ValueError("last_scan_at_utc must use UTC")
        if self.reason_code is not None and _SAFE_CODE.fullmatch(self.reason_code) is None:
            raise ValueError("watch reason code is invalid")
        if self.state == "degraded" and self.reason_code is None:
            raise ValueError("degraded watch status requires a reason code")
        if self.state != "degraded" and self.reason_code is not None:
            raise ValueError("only degraded watch status may carry a reason code")

    def document(self) -> dict[str, str | None]:
        return {
            "root_public_id": self.root_public_id,
            "label": self.label,
            "state": self.state,
            "last_scan_at_utc": (
                self.last_scan_at_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if self.last_scan_at_utc is not None
                else None
            ),
            "reason_code": self.reason_code,
        }


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateKey
        document[key] = value
    return document


def _parse_record(value: Any) -> WatchFolderStatusRecord:
    if not isinstance(value, dict) or set(value) != {
        "root_public_id",
        "label",
        "state",
        "last_scan_at_utc",
        "reason_code",
    }:
        raise ValueError("watch status record is invalid")
    timestamp = value["last_scan_at_utc"]
    if timestamp is not None:
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise ValueError("watch status timestamp is invalid")
        timestamp = datetime.fromisoformat(timestamp)
    return WatchFolderStatusRecord(
        root_public_id=value["root_public_id"],
        label=value["label"],
        state=value["state"],
        last_scan_at_utc=timestamp,
        reason_code=value["reason_code"],
    )


# TheSuperHackers @feature Leex 22/08/2026 Publish path-free watched-folder health through atomic product storage. (#TBD)
class FileWatchStatusStore:
    """Write complete safe snapshots atomically and let Web read the last complete snapshot."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = Path(data_root)
        self._status_path = self._data_root / _STATUS_NAME

    def publish(self, records: tuple[WatchFolderStatusRecord, ...]) -> None:
        if not isinstance(records, tuple):
            raise TypeError("watch status records must be an immutable tuple")
        if len({record.root_public_id for record in records}) != len(records):
            raise ValueError("watch status contains duplicate roots")
        self._data_root.mkdir(parents=True, exist_ok=True)
        root_info = self._data_root.lstat()
        if _is_reparse(root_info) or self._data_root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
            raise OSError("watch status root is unsafe")
        root_identity = (root_info.st_dev, root_info.st_ino)
        if self._status_path.exists() or self._status_path.is_symlink():
            status_info = self._status_path.lstat()
            if _is_reparse(status_info) or self._status_path.is_symlink() or not stat.S_ISREG(status_info.st_mode):
                raise OSError("watch status identity is unsafe")
        payload = json.dumps(
            {
                "version": _STATUS_VERSION,
                "roots": [record.document() for record in sorted(records, key=lambda item: item.root_public_id)],
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_STATUS_BYTES:
            raise ValueError("watch status is too large")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".watch-status-", suffix=".tmp", dir=self._data_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            fresh_root = self._data_root.lstat()
            if (
                _is_reparse(fresh_root)
                or self._data_root.is_symlink()
                or (fresh_root.st_dev, fresh_root.st_ino) != root_identity
            ):
                raise OSError("watch status root changed")
            os.replace(temporary, self._status_path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self) -> tuple[WatchFolderStatusRecord, ...]:
        # TheSuperHackers @fix Leex 22/08/2026 Bind bounded status bytes to one verified open file identity. (#TBD)
        descriptor = -1
        try:
            initial = self._status_path.lstat()
            if _is_reparse(initial) or self._status_path.is_symlink() or not stat.S_ISREG(initial.st_mode):
                return ()
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._status_path, flags)
            opened = os.fstat(descriptor)
            fresh = self._status_path.lstat()
            initial_identity = (initial.st_dev, initial.st_ino)
            if (
                _is_reparse(opened)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _MAX_STATUS_BYTES
                or (opened.st_dev, opened.st_ino) != initial_identity
                or _is_reparse(fresh)
                or not stat.S_ISREG(fresh.st_mode)
                or (fresh.st_dev, fresh.st_ino) != initial_identity
            ):
                return ()
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(_MAX_STATUS_BYTES + 1)
            final = self._status_path.lstat()
            if (
                _is_reparse(final)
                or not stat.S_ISREG(final.st_mode)
                or (final.st_dev, final.st_ino) != initial_identity
            ):
                return ()
        except OSError:
            return ()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > _MAX_STATUS_BYTES:
            return ()
        try:
            document = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(document, dict) or set(document) != {"version", "roots"}:
                return ()
            if document["version"] != _STATUS_VERSION or not isinstance(document["roots"], list):
                return ()
            records = tuple(_parse_record(item) for item in document["roots"])
            if len({record.root_public_id for record in records}) != len(records):
                return ()
            return records
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return ()
