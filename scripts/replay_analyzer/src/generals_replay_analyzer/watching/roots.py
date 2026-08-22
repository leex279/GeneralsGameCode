"""Persistent opaque watched-root identities and immutable replay ingress."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from threading import Lock, RLock
from typing import Any, cast
from uuid import UUID, uuid4

_REGISTRY_NAME = "watched-roots-v1.json"
_LOCK_NAME = ".watched-roots-v1.lock"
_REGISTRY_VERSION = 1
_MAX_REGISTRY_BYTES = 256 * 1024
_MAX_REGISTRY_ENTRIES = 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GENERIC_LABEL_PATTERN = re.compile(r"^Replay folder ([1-9][0-9]{0,8})$")
_WINDOWS_INVALID_COMPONENT_CHARACTERS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
    | {f"com{number}" for number in ("\u00b9", "\u00b2", "\u00b3")}
    | {f"lpt{number}" for number in ("\u00b9", "\u00b2", "\u00b3")}
)
_READ_CHUNK_SIZE = 1024 * 1024
_THREAD_LOCKS_GUARD = Lock()
_THREAD_LOCKS: dict[str, RLock] = {}


class RootRegistryError(RuntimeError):
    """A path-free watched-root registry failure with a stable reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        messages = {
            "watched_root_registry_invalid": "Watched-root registry validation failed.",
            "watched_root_registry_unavailable": "Watched-root registry is unavailable.",
            "watched_root_registry_changed": "Watched-root registry changed during reconciliation.",
            "watched_root_path_collision": "Configured watched roots are ambiguous.",
        }
        super().__init__(messages.get(code, "Watched-root operation failed."))


class SnapshotIngressError(RuntimeError):
    """A path-free immutable-ingress failure with a stable reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        messages = {
            "watched_root_unknown": "Watched root is not currently configured.",
            "replay_relative_name_invalid": "Replay selection name is invalid.",
            "replay_source_unavailable": "Replay source is unavailable.",
            "replay_source_unsafe": "Replay source path is unsafe.",
            "replay_source_not_regular": "Replay source is not a regular file.",
            "replay_source_hardlinked": "Replay source has an ambiguous file identity.",
            "replay_source_oversize": "Replay source exceeds the configured size limit.",
            "replay_source_changed": "Replay source changed during immutable ingress.",
            "ingress_snapshot_unavailable": "Immutable replay ingress is unavailable.",
            "ingress_snapshot_collision": "Immutable replay ingress identity is occupied by different bytes.",
            "ingress_snapshot_changed": "Immutable replay ingress changed during publication.",
        }
        super().__init__(messages.get(code, "Immutable replay ingress failed."))


@dataclass(frozen=True, slots=True)
class WatchedRoot:
    """Public-safe status for one currently configured opaque root."""

    root_public_id: str
    label: str
    available: bool
    reason_code: str | None


# TheSuperHackers @feature Leex 22/08/2026 Keep verified replay ingress descriptors immutable and locator-safe. (#TBD)
@dataclass(frozen=True, slots=True)
class IngressSnapshot:
    """Private Analytics descriptor for verified product-owned replay bytes."""

    snapshot_path: Path = dataclass_field(repr=False)
    sha256: str
    size_bytes: int
    root_public_id: str
    relative_name: str
    created: bool
    _data_root_path: Path | None = dataclass_field(default=None, repr=False, compare=False)
    _ingress_identity: _ObjectIdentity | None = dataclass_field(default=None, repr=False, compare=False)
    _shard_identity: _ObjectIdentity | None = dataclass_field(default=None, repr=False, compare=False)
    _target_identity: _ObjectIdentity | None = dataclass_field(default=None, repr=False, compare=False)

    def read_verified_bytes(self) -> bytes:
        """Reopen product-owned bytes through verified parents and recheck immutable identity."""
        if (
            self._data_root_path is None
            or self._ingress_identity is None
            or self._shard_identity is None
            or self._target_identity is None
        ):
            raise SnapshotIngressError("ingress_snapshot_collision")
        return _read_verified_ingress_target(
            self.snapshot_path,
            self.sha256,
            self.size_bytes,
            data_root=self._data_root_path,
            ingress_identity=self._ingress_identity,
            shard_identity=self._shard_identity,
            target_identity=self._target_identity,
        )


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    path_key_sha256: str
    root_public_id: str
    label: str

    def document(self) -> dict[str, str]:
        return {
            "path_key_sha256": self.path_key_sha256,
            "root_public_id": self.root_public_id,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class _SelectableRoot:
    path: Path
    directory_chain: tuple[tuple[Path, _ObjectIdentity], ...]


def _thread_lock(path: Path) -> RLock:
    key = os.path.normcase(os.path.abspath(os.fspath(path))).casefold()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, RLock())


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:  # pragma: no cover - exercised on POSIX hosts
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)  # type: ignore[attr-defined]


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - exercised on POSIX hosts
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined]


@contextmanager
def _registry_lock(data_root: Path) -> Iterator[_BoundDirectory]:
    bound_root: tuple[_BoundDirectory, ...] = ()
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        bound_root = _open_absolute_directory_chain(data_root)
        lock_path = data_root / _LOCK_NAME
        with _thread_lock(lock_path):
            if os.name == "nt":
                handle = _win_open_relative(
                    bound_root[-1],
                    _LOCK_NAME,
                    directory=False,
                    deny_mutation=False,
                    prevent_rename=True,
                    writable=True,
                    disposition=_FILE_OPEN_IF,
                )
                try:
                    descriptor = msvcrt.open_osfhandle(
                        handle,
                        os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
                    )
                except Exception:
                    _win_close(handle)
                    raise
            else:  # pragma: no cover - exercised on POSIX hosts
                descriptor = os.open(
                    _LOCK_NAME,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=bound_root[-1].handle,
                )
            try:
                opened = os.fstat(descriptor)
                if os.name == "nt":
                    lock_identity = _win_file_identity(msvcrt.get_osfhandle(descriptor))
                    lock_is_invalid = (
                        lock_identity.object_identity.reparse_attributes != 0
                        or lock_identity.object_identity.file_type != stat.S_IFREG
                        or lock_identity.link_count != 1
                    )
                else:  # pragma: no cover - exercised on POSIX hosts
                    named = os.stat(
                        _LOCK_NAME,
                        dir_fd=bound_root[-1].handle,
                        follow_symlinks=False,
                    )
                    lock_identity = _file_identity(named)
                    lock_is_invalid = (
                        _is_reparse(named)
                        or not stat.S_ISREG(named.st_mode)
                        or named.st_nlink != 1
                        or not _same_opened_identity(opened, named)
                    )
                if lock_is_invalid:
                    raise RootRegistryError("watched_root_registry_invalid")
                if opened.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                    if os.name == "nt":
                        lock_identity = _win_file_identity(msvcrt.get_osfhandle(descriptor))
                _lock_descriptor(descriptor)
                try:
                    yield bound_root[-1]
                finally:
                    if os.name == "nt":
                        lock_changed = _win_file_identity(msvcrt.get_osfhandle(descriptor)) != lock_identity
                    else:  # pragma: no cover - exercised on POSIX hosts
                        current = os.stat(
                            _LOCK_NAME,
                            dir_fd=bound_root[-1].handle,
                            follow_symlinks=False,
                        )
                        lock_changed = current.st_nlink != 1 or not _same_opened_identity(
                            os.fstat(descriptor), current
                        )
                    if lock_changed:
                        raise RootRegistryError("watched_root_registry_changed")
                    _revalidate_bound_directory_chain(data_root, bound_root)
                    _unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)
    except RootRegistryError:
        raise
    except OSError:
        raise RootRegistryError("watched_root_registry_unavailable") from None
    finally:
        for directory in reversed(bound_root):
            directory.close()


@contextmanager
def _posix_digest_coordination(  # pragma: no cover - exercised on Linux hosts
    lock_directory: Path, digest: str
) -> Iterator[None]:
    """Use a Linux abstract socket so directory replacement cannot split digest serialization."""
    material = f"{_normalized_path_key(lock_directory)}:{digest}".encode("ascii")
    address = b"\0gra-" + hashlib.sha256(material).hexdigest().encode("ascii")
    coordinator = socket.socket(cast(Any, socket).AF_UNIX, socket.SOCK_STREAM)
    try:
        while True:
            try:
                coordinator.bind(address)
                break
            except OSError as error:
                if error.errno not in {98, 10048}:
                    raise
                time.sleep(0.01)
        yield
    finally:
        coordinator.close()


@contextmanager
def _digest_lock(
    lock_directory: Path,
    digest: str,
    *,
    bound_ingress: _BoundDirectory | None = None,
) -> Iterator[None]:
    lock_path = lock_directory / f"{digest}.lock"
    bound_directory: tuple[_BoundDirectory, ...] = ()
    try:
        if bound_ingress is None:
            _ensure_owned_directory(lock_directory.parent)
            _ensure_owned_directory(lock_directory, enforcement_site="locks")
            bound_directory = _open_absolute_directory_chain(lock_directory)
        else:
            bound_directory = (
                _ensure_owned_child_directory(bound_ingress, lock_directory.name, enforcement_site="locks"),
            )
        _race_hook("digest_lock_directory_bound")
        coordination = (
            _posix_digest_coordination(lock_directory, digest)
            if os.name != "nt"
            else nullcontext()
        )
        with coordination, _thread_lock(lock_path):
            if os.name == "nt":
                handle = _win_open_relative(
                    bound_directory[-1],
                    lock_path.name,
                    directory=False,
                    deny_mutation=False,
                    prevent_rename=True,
                    writable=True,
                    disposition=_FILE_OPEN_IF,
                )
                try:
                    descriptor = msvcrt.open_osfhandle(
                        handle,
                        os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
                    )
                except Exception:
                    _win_close(handle)
                    raise
            else:  # pragma: no cover - exercised on POSIX hosts
                descriptor = os.open(
                    lock_path.name,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=bound_directory[-1].handle,
                )
            try:
                opened = os.fstat(descriptor)
                if os.name == "nt":
                    lock_identity = _win_file_identity(msvcrt.get_osfhandle(descriptor))
                    invalid = (
                        _enforce_reparse_identity(
                            "locks", lock_identity.object_identity.reparse_attributes != 0
                        )
                        or lock_identity.object_identity.file_type != stat.S_IFREG
                        or lock_identity.link_count != 1
                    )
                else:  # pragma: no cover - exercised on POSIX hosts
                    named = os.stat(
                        lock_path.name,
                        dir_fd=bound_directory[-1].handle,
                        follow_symlinks=False,
                    )
                    lock_identity = _file_identity(named)
                    invalid = (
                        _is_reparse(named)
                        or not stat.S_ISREG(named.st_mode)
                        or named.st_nlink != 1
                        or not _same_opened_identity(opened, named)
                    )
                if invalid:
                    raise SnapshotIngressError("ingress_snapshot_collision")
                if opened.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                    _fsync_bound_directory(bound_directory[-1])
                    if os.name == "nt":
                        lock_identity = _win_file_identity(msvcrt.get_osfhandle(descriptor))
                _lock_descriptor(descriptor)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        changed = _win_file_identity(msvcrt.get_osfhandle(descriptor)) != lock_identity
                    else:  # pragma: no cover - exercised on POSIX hosts
                        current = os.stat(
                            lock_path.name,
                            dir_fd=bound_directory[-1].handle,
                            follow_symlinks=False,
                        )
                        changed = current.st_nlink != 1 or not _same_opened_identity(
                            os.fstat(descriptor), current
                        )
                    if changed:
                        raise SnapshotIngressError("ingress_snapshot_changed")
                    if bound_ingress is None:
                        _revalidate_bound_directory_chain(lock_directory, bound_directory)
                    else:
                        _revalidate_bound_child(bound_ingress, lock_directory.name, bound_directory[-1])
                    _unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)
    except SnapshotIngressError:
        raise
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None
    finally:
        for directory in reversed(bound_directory):
            directory.close()


def _absolute_without_following(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _normalized_path_key(path: Path) -> str:
    normalized = unicodedata.normalize("NFC", os.path.normpath(os.fspath(_absolute_without_following(path))))
    portable = normalized.replace("\\", "/").casefold()
    return hashlib.sha256(portable.encode("utf-8")).hexdigest()


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _enforce_reparse_identity(_site: str, marked: bool) -> bool:
    """Native-identity seam for enforcement-site tests and platform adapters."""
    return marked


def _path_components(path: Path) -> tuple[Path, ...]:
    absolute = _absolute_without_following(path)
    anchor = Path(absolute.anchor)
    current = anchor
    components: list[Path] = []
    for part in absolute.parts:
        if part == absolute.anchor:
            continue
        current /= part
        components.append(current)
    return tuple(components)


def _root_availability(path: Path) -> tuple[bool, str | None]:
    opened: tuple[_BoundDirectory, ...] = ()
    try:
        components = _path_components(path)
        if not components:
            return False, "watched_root_unsafe"
        for component in components:
            info = component.lstat()
            if component.is_symlink() or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                return False, "watched_root_unsafe"
        opened = _open_absolute_directory_chain(path)
    except OSError:
        return False, "watched_root_unavailable"
    finally:
        for directory in reversed(opened):
            directory.close()
    return True, None


def _canonical_uuid4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


class _DuplicateJSONKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _parse_registry(raw: bytes) -> list[_RegistryEntry]:
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise RootRegistryError("watched_root_registry_invalid")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKey):
        raise RootRegistryError("watched_root_registry_invalid") from None
    if not isinstance(document, dict) or set(document) != {"version", "roots"}:
        raise RootRegistryError("watched_root_registry_invalid")
    if type(document["version"]) is not int or document["version"] != _REGISTRY_VERSION:
        raise RootRegistryError("watched_root_registry_invalid")
    if not isinstance(document["roots"], list) or len(document["roots"]) > _MAX_REGISTRY_ENTRIES:
        raise RootRegistryError("watched_root_registry_invalid")
    entries: list[_RegistryEntry] = []
    keys: set[str] = set()
    public_ids: set[str] = set()
    labels: set[str] = set()
    for raw_entry in document["roots"]:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"path_key_sha256", "root_public_id", "label"}:
            raise RootRegistryError("watched_root_registry_invalid")
        path_key = raw_entry["path_key_sha256"]
        public_id = raw_entry["root_public_id"]
        label = raw_entry["label"]
        if (
            not isinstance(path_key, str)
            or _SHA256_PATTERN.fullmatch(path_key) is None
            or not _canonical_uuid4(public_id)
            or not isinstance(label, str)
            or _GENERIC_LABEL_PATTERN.fullmatch(label) is None
            or path_key in keys
            or public_id in public_ids
            or label in labels
        ):
            raise RootRegistryError("watched_root_registry_invalid")
        keys.add(path_key)
        public_ids.add(public_id)
        labels.add(label)
        entries.append(_RegistryEntry(path_key, public_id, label))
    return entries


def _read_registry(path: Path) -> tuple[list[_RegistryEntry], bytes | None, _FileIdentity | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [], None, None
    except OSError:
        raise RootRegistryError("watched_root_registry_unavailable") from None
    if (
        path.is_symlink()
        or _enforce_reparse_identity("registry", _is_reparse(info))
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise RootRegistryError("watched_root_registry_invalid")
    descriptor = -1
    try:
        descriptor = os.open(path, _source_open_flags())
        opened = os.fstat(descriptor)
        if not _same_opened_identity(opened, info) or opened.st_nlink != 1:
            raise RootRegistryError("watched_root_registry_changed")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_REGISTRY_BYTES - size + 1))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_REGISTRY_BYTES:
                raise RootRegistryError("watched_root_registry_invalid")
        after = os.fstat(descriptor)
        named_after = path.lstat()
        if not _same_opened_identity(opened, after) or not _same_opened_identity(after, named_after):
            raise RootRegistryError("watched_root_registry_changed")
        raw = b"".join(chunks)
    except RootRegistryError:
        raise
    except OSError:
        raise RootRegistryError("watched_root_registry_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _parse_registry(raw), raw, _file_identity(after)


def _serialized_registry(entries: Sequence[_RegistryEntry]) -> bytes:
    document = {"version": _REGISTRY_VERSION, "roots": [entry.document() for entry in entries]}
    return (json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        handle = _KERNEL32.CreateFileW(
            os.fspath(path),
            0x80000000 | 0x40000000,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            3,
            0x02000000 | _FILE_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            _win_raise_last_error()
        try:
            _win_file_identity(int(handle))
            if not _KERNEL32.FlushFileBuffers(handle):
                _win_raise_last_error()
        finally:
            _win_close(int(handle))
        return
    if os.name != "nt":  # pragma: no cover - exercised on POSIX hosts
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _fsync_bound_directory(directory: _BoundDirectory) -> None:
    if directory._windows:
        path = _win_final_path(directory.handle)
        flush_handle = _KERNEL32.CreateFileW(
            path,
            0x80000000 | 0x40000000,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            3,
            0x02000000 | _FILE_OPEN_REPARSE_POINT,
            None,
        )
        if flush_handle == _INVALID_HANDLE_VALUE:
            _win_raise_last_error()
        try:
            if _win_file_identity(int(flush_handle)).object_identity != directory.identity.object_identity:
                raise OSError("bound directory changed before flush")
            if not _KERNEL32.FlushFileBuffers(flush_handle):
                _win_raise_last_error()
        finally:
            _win_close(int(flush_handle))
        return
    os.fsync(directory.handle)  # pragma: no cover - exercised on POSIX hosts


def _unlink_if_identity(path: Path, expected: _FileIdentity) -> None:
    try:
        current = path.lstat()
    except OSError:
        return
    if (
        _object_identity(current) == expected.object_identity
        and not path.is_symlink()
        and not _is_reparse(current)
        and stat.S_ISREG(current.st_mode)
    ):
        path.unlink(missing_ok=True)


def _replace_registry_windows(
    path: Path,
    temporary_path: Path,
    expected_bytes: bytes | None,
    expected_identity: _FileIdentity | None,
    temporary_descriptor: int,
    bound_data_root: _BoundDirectory,
) -> tuple[Path | None, _FileIdentity | None]:
    if expected_bytes is None:
        _race_hook("registry_after_final_compare")
        try:
            _win_link_handle(msvcrt.get_osfhandle(temporary_descriptor), bound_data_root, path.name)
        except FileExistsError:
            raise RootRegistryError("watched_root_registry_changed") from None
        _win_mark_delete(msvcrt.get_osfhandle(temporary_descriptor))
        return None, None

    backup_path = path.parent / f".{path.name}.{uuid4().hex}.backup"
    _race_hook("registry_after_final_compare")
    if not _KERNEL32.ReplaceFileW(os.fspath(path), os.fspath(temporary_path), os.fspath(backup_path), 0, None, None):
        _win_raise_last_error()
    backup_identity = _file_identity(backup_path.lstat())
    try:
        _entries, displaced_bytes, displaced_identity = _read_registry(backup_path)
    except RootRegistryError:
        if not _KERNEL32.ReplaceFileW(os.fspath(path), os.fspath(backup_path), None, 0, None, None):
            raise RootRegistryError("watched_root_registry_changed") from None
        raise RootRegistryError("watched_root_registry_changed") from None
    identity_matches = (
        expected_identity is not None
        and displaced_identity is not None
        and displaced_identity.object_identity == expected_identity.object_identity
        and displaced_identity.size == expected_identity.size
        and displaced_identity.modified_ns == expected_identity.modified_ns
        and displaced_identity.link_count == expected_identity.link_count
    )
    if displaced_bytes != expected_bytes or not identity_matches:
        if not _KERNEL32.ReplaceFileW(os.fspath(path), os.fspath(backup_path), None, 0, None, None):
            raise RootRegistryError("watched_root_registry_changed")
        raise RootRegistryError("watched_root_registry_changed")
    return backup_path, backup_identity


def _read_registry_at(  # pragma: no cover - exercised on POSIX hosts
    parent: _BoundDirectory, name: str
) -> tuple[list[_RegistryEntry], bytes, _FileIdentity]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent.handle,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _is_reparse(opened):
            raise RootRegistryError("watched_root_registry_invalid")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_REGISTRY_BYTES - size + 1))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_REGISTRY_BYTES:
                raise RootRegistryError("watched_root_registry_invalid")
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent.handle, follow_symlinks=False)
        if not _same_opened_identity(opened, after) or not _same_opened_identity(after, named):
            raise RootRegistryError("watched_root_registry_changed")
        raw = b"".join(chunks)
        return _parse_registry(raw), raw, _file_identity(after)
    finally:
        os.close(descriptor)


def _rename_exchange_at(  # pragma: no cover - exercised on Linux hosts
    parent: _BoundDirectory, left: str, right: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(parent.handle, os.fsencode(left), parent.handle, os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "secure POSIX registry exchange failed")


def _replace_registry_posix(  # pragma: no cover - exercised on POSIX hosts
    path: Path,
    temporary_path: Path,
    parent: _BoundDirectory,
    expected_bytes: bytes | None,
    expected_identity: _FileIdentity | None,
    payload: bytes,
) -> None:
    _race_hook("registry_after_final_compare")
    if expected_bytes is None:
        os.link(
            temporary_path.name,
            path.name,
            src_dir_fd=parent.handle,
            dst_dir_fd=parent.handle,
            follow_symlinks=False,
        )
        os.unlink(temporary_path.name, dir_fd=parent.handle)
        return
    _rename_exchange_at(parent, temporary_path.name, path.name)
    try:
        _entries, displaced_bytes, displaced_identity = _read_registry_at(parent, temporary_path.name)
        identity_matches = (
            expected_identity is not None
            and displaced_identity.object_identity == expected_identity.object_identity
            and displaced_identity.size == expected_identity.size
            and displaced_identity.modified_ns == expected_identity.modified_ns
            and displaced_identity.link_count == expected_identity.link_count
        )
        if displaced_bytes != expected_bytes or not identity_matches:
            raise RootRegistryError("watched_root_registry_changed")
        _entries, published_bytes, _published_identity = _read_registry_at(parent, path.name)
        if published_bytes != payload:
            raise RootRegistryError("watched_root_registry_changed")
        os.unlink(temporary_path.name, dir_fd=parent.handle)
    except (OSError, RootRegistryError):
        _rename_exchange_at(parent, temporary_path.name, path.name)
        raise RootRegistryError("watched_root_registry_changed") from None


def _publish_registry(
    path: Path,
    payload: bytes,
    *,
    expected_bytes: bytes | None,
    expected_identity: _FileIdentity | None,
    bound_data_root: _BoundDirectory | None = None,
) -> None:
    temporary_path: Path | None = None
    temporary_identity: _FileIdentity | None = None
    descriptor = -1
    backup_path: Path | None = None
    backup_identity: _FileIdentity | None = None
    _parse_registry(payload)
    try:
        if os.name == "nt" and bound_data_root is not None:
            temporary_name = f".{path.name}.{uuid4().hex}.tmp"
            handle = _win_open_relative(
                bound_data_root,
                temporary_name,
                directory=False,
                deny_mutation=False,
                prevent_rename=True,
                writable=True,
                allow_delete=True,
                disposition=_FILE_CREATE,
            )
            try:
                descriptor = msvcrt.open_osfhandle(
                    handle,
                    os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
                )
            except Exception:  # pragma: no cover - native descriptor conversion failure
                _win_close(handle)
                raise
            temporary_path = path.parent / temporary_name
        elif os.name != "nt" and bound_data_root is not None:  # pragma: no cover - POSIX
            temporary_name = f".{path.name}.{uuid4().hex}.tmp"
            descriptor = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=bound_data_root.handle,
            )
            temporary_path = path.parent / temporary_name
        else:  # pragma: no cover - legacy direct helper use
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temporary_path = Path(temporary_name)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("registry write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        temporary_identity = _file_identity(os.fstat(descriptor))
        _race_hook("registry_before_replace")
        _entries, current_bytes, current_identity = _read_registry(path)
        if current_bytes != expected_bytes or current_identity != expected_identity:
            raise RootRegistryError("watched_root_registry_changed")
        if os.name == "nt":
            if bound_data_root is None:
                raise RootRegistryError("watched_root_registry_unavailable")
            if expected_bytes is not None:
                os.close(descriptor)
                descriptor = -1
            backup_path, backup_identity = _replace_registry_windows(
                path,
                temporary_path,
                expected_bytes,
                expected_identity,
                descriptor,
                bound_data_root,
            )
            if expected_bytes is None:
                os.close(descriptor)
                descriptor = -1
        else:  # pragma: no cover - exercised on POSIX hosts
            if bound_data_root is None:
                raise RootRegistryError("watched_root_registry_unavailable")
            _replace_registry_posix(path, temporary_path, bound_data_root, expected_bytes, expected_identity, payload)
        temporary_path = None
        _entries, published_bytes, _published_identity = _read_registry(path)
        if published_bytes != payload:
            raise RootRegistryError("watched_root_registry_changed")
        if backup_path is not None and backup_identity is not None:
            _unlink_if_identity(backup_path, backup_identity)
            backup_path = None
            backup_identity = None
        _fsync_directory(path.parent)
    except RootRegistryError:
        if (
            os.name == "nt"
            and backup_path is not None
            and backup_path.exists()
            and _KERNEL32.ReplaceFileW(os.fspath(path), os.fspath(backup_path), None, 0, None, None)
        ):
            backup_path = None
            backup_identity = None
        raise
    except OSError:
        raise RootRegistryError("watched_root_registry_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None and temporary_identity is not None:
            try:
                _unlink_if_identity(temporary_path, temporary_identity)
            except OSError:
                pass
        if backup_path is not None and backup_identity is not None:
            try:
                _unlink_if_identity(backup_path, backup_identity)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class _ObjectIdentity:
    device: int
    inode: int
    file_type: int
    reparse_attributes: int


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    object_identity: _ObjectIdentity
    size: int
    modified_ns: int
    changed_ns: int
    link_count: int


@dataclass(slots=True)
class _BoundDirectory:
    handle: int = dataclass_field(repr=False)
    identity: _FileIdentity
    _windows: bool

    def close(self) -> None:
        if self.handle < 0:
            return
        if self._windows:
            _win_close(self.handle)
        else:  # pragma: no cover - exercised on POSIX hosts
            os.close(self.handle)
        self.handle = -1


@dataclass(slots=True)
class _OpenedSource:
    descriptor: int = dataclass_field(repr=False)
    directories: tuple[_BoundDirectory, ...] = dataclass_field(repr=False)
    initial_info: os.stat_result

    def close(self) -> None:
        try:
            os.close(self.descriptor)
        finally:
            for directory in reversed(self.directories):
                directory.close()


@dataclass(slots=True)
class _OwnedTemporary:
    descriptor: int = dataclass_field(repr=False)
    identity: _FileIdentity
    path: Path | None
    name: str | None
    anonymous: bool


@dataclass(slots=True)
class _SourceMutationGuard:
    descriptor: int = dataclass_field(repr=False)

    def assert_unchanged(self) -> None:
        if self.descriptor >= 0:  # pragma: no cover - exercised on Linux hosts
            _assert_linux_source_unchanged(self.descriptor)

    def close(self) -> None:
        if self.descriptor >= 0:  # pragma: no cover - exercised on Linux hosts
            os.close(self.descriptor)
            self.descriptor = -1


def _assert_linux_source_unchanged(descriptor: int) -> None:  # pragma: no cover - exercised on Linux hosts
    try:
        changed = os.read(descriptor, 4096)
    except BlockingIOError:
        return
    if changed:
        raise SnapshotIngressError("replay_source_changed")


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", wintypes.LPVOID), ("Information", ctypes.c_size_t)]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    class _FileLinkInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _NTDLL = ctypes.WinDLL("ntdll")
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_DATA = 0x0001
    _FILE_WRITE_DATA = 0x0002
    _FILE_WRITE_ATTRIBUTES = 0x0100
    _DELETE_ACCESS = 0x00010000
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _FILE_OPEN = 0x1
    _FILE_CREATE = 0x2
    _FILE_OPEN_IF = 0x3
    _FILE_DIRECTORY_FILE = 0x1
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x20
    _FILE_NON_DIRECTORY_FILE = 0x40
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x40
    _FILE_BASIC_INFO_CLASS = 0
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_LINK_INFO_CLASS = 11

    _KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    _KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _KERNEL32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _KERNEL32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.GetDriveTypeW.restype = wintypes.UINT
    _KERNEL32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _KERNEL32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _KERNEL32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _KERNEL32.SetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _KERNEL32.ReplaceFileW.restype = wintypes.BOOL
    _KERNEL32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _KERNEL32.FlushFileBuffers.restype = wintypes.BOOL
    _NTDLL.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    _NTDLL.NtCreateFile.restype = wintypes.LONG
    _NTDLL.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    _NTDLL.NtSetInformationFile.restype = wintypes.LONG
    _NTDLL.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    _NTDLL.RtlNtStatusToDosError.restype = wintypes.ULONG


def _win_raise_last_error() -> None:  # pragma: no cover - exercised only after a native API failure
    raise OSError(ctypes.get_last_error(), "secure Windows filesystem operation failed")


def _win_close(handle: int) -> None:
    if os.name == "nt" and not _KERNEL32.CloseHandle(handle):  # pragma: no cover - native API failure
        _win_raise_last_error()


def _win_file_identity(handle: int) -> _FileIdentity:
    information = _ByHandleFileInformation()
    basic = _FileBasicInfo()
    if not _KERNEL32.GetFileInformationByHandle(  # pragma: no cover - native API failure
        handle, ctypes.byref(information)
    ):
        _win_raise_last_error()
    if not _KERNEL32.GetFileInformationByHandleEx(
        handle,
        _FILE_BASIC_INFO_CLASS,
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):  # pragma: no cover - native API failure
        _win_raise_last_error()
    attributes = information.dwFileAttributes
    file_type = stat.S_IFDIR if attributes & _FILE_ATTRIBUTE_DIRECTORY else stat.S_IFREG
    inode = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    size = (information.nFileSizeHigh << 32) | information.nFileSizeLow
    return _FileIdentity(
        _ObjectIdentity(
            information.dwVolumeSerialNumber,
            inode,
            file_type,
            attributes & _FILE_ATTRIBUTE_REPARSE_POINT,
        ),
        size,
        basic.LastWriteTime,
        basic.ChangeTime,
        information.nNumberOfLinks,
    )


def _win_final_path(handle: int) -> str:
    final_path = ctypes.create_unicode_buffer(32768)
    final_size = _KERNEL32.GetFinalPathNameByHandleW(handle, final_path, len(final_path), 0)
    if final_size == 0 or final_size >= len(final_path):  # pragma: no cover - native API failure
        _win_raise_last_error()
    return final_path.value


def _win_mark_delete(handle: int) -> None:
    disposition = _FileDispositionInfo(True)
    if not _KERNEL32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        _win_raise_last_error()


def _win_link_handle(handle: int, target_directory: _BoundDirectory, target_name: str) -> None:
    encoded_name = target_name.encode("utf-16-le")
    allocation_size = _FileLinkInfo.FileName.offset + len(encoded_name) + ctypes.sizeof(wintypes.WCHAR)
    allocation = ctypes.create_string_buffer(allocation_size)
    information = ctypes.cast(allocation, ctypes.POINTER(_FileLinkInfo)).contents
    information.ReplaceIfExists = False
    information.RootDirectory = target_directory.handle
    information.FileNameLength = len(encoded_name)
    ctypes.memmove(ctypes.addressof(allocation) + _FileLinkInfo.FileName.offset, encoded_name, len(encoded_name))
    io_status = _IoStatusBlock()
    status = _NTDLL.NtSetInformationFile(
        handle,
        ctypes.byref(io_status),
        allocation,
        allocation_size,
        _FILE_LINK_INFO_CLASS,
    )
    if status < 0:
        error = int(_NTDLL.RtlNtStatusToDosError(status))
        if error in {80, 183}:
            raise FileExistsError(error, "immutable snapshot target exists")
        raise OSError(error, "secure Windows hardlink failed")


def _posix_link_handle(  # pragma: no cover - exercised on Linux hosts
    descriptor: int, target_directory: _BoundDirectory, target_name: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    target = os.fsencode(target_name)
    result = linkat(descriptor, b"", target_directory.handle, target, 0x1000)
    error = ctypes.get_errno()
    if result != 0 and error in {1, 2, 13}:
        descriptor_path = os.fsencode(f"/proc/self/fd/{descriptor}")
        result = linkat(-100, descriptor_path, target_directory.handle, target, 0x400)
        error = ctypes.get_errno()
    if result != 0:
        if error == 17:
            raise FileExistsError(error, "immutable snapshot target exists")
        raise OSError(error, "secure POSIX descriptor link failed")


def _win_open_drive_root(path: Path) -> _BoundDirectory:
    drive, tail = os.path.splitdrive(os.fspath(path))
    if not drive or not tail.startswith("\\") or drive.startswith("\\"):  # pragma: no cover - rejected alias
        raise OSError("unsupported Windows path identity")
    root = f"\\\\?\\{drive}\\"
    if _KERNEL32.GetDriveTypeW(f"{drive}\\") != 3:  # pragma: no cover - host-dependent drive type
        raise OSError("unsupported Windows drive identity")
    handle = _KERNEL32.CreateFileW(
        root,
        _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        3,
        0x02000000 | _FILE_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:  # pragma: no cover - native API failure
        _win_raise_last_error()
    value = int(handle)
    try:
        if _win_final_path(value).rstrip("\\").casefold() != root.rstrip("\\").casefold():  # pragma: no cover
            raise OSError("aliased Windows drive identity")
        identity = _win_file_identity(value)
        if (  # pragma: no cover - host-dependent drive reparse
            identity.object_identity.reparse_attributes
            or identity.object_identity.file_type != stat.S_IFDIR
        ):
            raise OSError("unsafe Windows drive root")
        return _BoundDirectory(value, identity, True)
    except Exception:  # pragma: no cover - cleanup for rejected/native-failure roots
        _win_close(value)
        raise


def _win_open_relative(
    parent: _BoundDirectory,
    name: str,
    *,
    directory: bool,
    deny_mutation: bool,
    prevent_rename: bool = False,
    writable: bool = False,
    allow_delete: bool = False,
    disposition: int = 1,
) -> int:
    buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UnicodeString(len(name.encode("utf-16-le")), len(buffer) * 2, ctypes.cast(buffer, wintypes.LPWSTR))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent.handle,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    result = wintypes.HANDLE()
    options = _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
    access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if directory:
        options |= _FILE_DIRECTORY_FILE
        access |= _FILE_LIST_DIRECTORY | _FILE_TRAVERSE
    else:
        options |= _FILE_NON_DIRECTORY_FILE
        access |= _FILE_READ_DATA
        if writable:
            access |= _FILE_WRITE_DATA
        if allow_delete:
            access |= _DELETE_ACCESS | _FILE_WRITE_ATTRIBUTES
    share = _FILE_SHARE_READ if deny_mutation else _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
    if prevent_rename:
        share &= ~_FILE_SHARE_DELETE
    status = _NTDLL.NtCreateFile(
        ctypes.byref(result),
        access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        share,
        disposition,
        options,
        None,
        0,
    )
    if status < 0:
        code = int(_NTDLL.RtlNtStatusToDosError(status))
        raise OSError(code, "secure Windows relative open failed")
    if result.value is None:  # pragma: no cover - defensive native API contract
        raise OSError("secure Windows relative open returned no handle")
    return int(result.value)


def _object_identity(info: os.stat_result) -> _ObjectIdentity:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return _ObjectIdentity(
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        getattr(info, "st_file_attributes", 0) & marker,
    )


def _file_identity(info: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        _object_identity(info),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _open_absolute_directory_chain(path: Path) -> tuple[_BoundDirectory, ...]:
    absolute = _absolute_without_following(path)
    directories: list[_BoundDirectory] = []
    try:
        if os.name == "nt":
            root = _win_open_drive_root(absolute)
            directories.append(root)
            parts = absolute.parts[1:]
            for part in parts:
                handle = _win_open_relative(
                    directories[-1],
                    part,
                    directory=True,
                    deny_mutation=False,
                    prevent_rename=True,
                )
                try:
                    identity = _win_file_identity(handle)
                    if (  # pragma: no cover - native reparse requires host privileges
                        identity.object_identity.reparse_attributes
                        or identity.object_identity.file_type != stat.S_IFDIR
                    ):
                        raise OSError("unsafe Windows directory component")
                    directories.append(_BoundDirectory(handle, identity, True))
                except Exception:  # pragma: no cover - cleanup for native reparse/API failure
                    _win_close(handle)
                    raise
        else:  # pragma: no cover - exercised on POSIX hosts
            descriptor = os.open(
                absolute.anchor,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            directories.append(_BoundDirectory(descriptor, _file_identity(os.fstat(descriptor)), False))
            for part in absolute.parts[1:]:
                descriptor = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directories[-1].handle,
                )
                identity = _file_identity(os.fstat(descriptor))
                if identity.object_identity.file_type != stat.S_IFDIR or identity.object_identity.reparse_attributes:
                    os.close(descriptor)
                    raise OSError("unsafe POSIX directory component")
                directories.append(_BoundDirectory(descriptor, identity, False))
        return tuple(directories)
    except Exception:  # pragma: no cover - cleanup for native traversal failure
        for directory in reversed(directories):
            directory.close()
        raise


def _revalidate_bound_directory_chain(path: Path, expected: Sequence[_BoundDirectory]) -> None:
    current = _open_absolute_directory_chain(path)
    try:
        if len(current) != len(expected) or any(
            actual.identity.object_identity != original.identity.object_identity
            for actual, original in zip(current, expected, strict=True)
        ):
            raise OSError("bound directory identity changed")
    finally:
        for directory in reversed(current):
            directory.close()


def _verified_root_alias_identity(path: Path) -> _ObjectIdentity | None:
    """Return opened-object identity only for simultaneous configured-root alias rejection."""
    directories: tuple[_BoundDirectory, ...] = ()
    try:
        directories = _open_absolute_directory_chain(path)
        return directories[-1].identity.object_identity
    except OSError:
        return None
    finally:
        for directory in reversed(directories):
            directory.close()


def _open_child_directory(parent: _BoundDirectory, name: str) -> _BoundDirectory:
    if os.name == "nt":
        handle = _win_open_relative(
            parent,
            name,
            directory=True,
            deny_mutation=False,
            prevent_rename=True,
        )
        try:
            identity = _win_file_identity(handle)
            if (  # pragma: no cover - native reparse requires host privileges
                identity.object_identity.reparse_attributes
                or identity.object_identity.file_type != stat.S_IFDIR
            ):
                raise OSError("unsafe Windows child directory")
            return _BoundDirectory(handle, identity, True)
        except Exception:  # pragma: no cover - cleanup for native reparse/API failure
            _win_close(handle)
            raise
    if os.name != "nt":  # pragma: no cover - exercised on POSIX hosts
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent.handle,
        )
        try:
            identity = _file_identity(os.fstat(descriptor))
            if identity.object_identity.file_type != stat.S_IFDIR or identity.object_identity.reparse_attributes:
                raise OSError("unsafe POSIX child directory")
            return _BoundDirectory(descriptor, identity, False)
        except Exception:  # pragma: no cover - native descriptor conversion failure
            os.close(descriptor)
            raise
    raise OSError("unsupported filesystem platform")  # pragma: no cover


def _ensure_owned_child_directory(
    parent: _BoundDirectory, name: str, *, enforcement_site: str = "ingress"
) -> _BoundDirectory:
    created = False
    child: _BoundDirectory | None = None
    try:
        try:
            child = _open_child_directory(parent, name)
        except OSError as error:
            if os.name == "nt":
                if error.errno not in {2, 3}:
                    raise
                handle = _win_open_relative(
                    parent,
                    name,
                    directory=True,
                    deny_mutation=False,
                    prevent_rename=True,
                    disposition=_FILE_OPEN_IF,
                )
                child = _BoundDirectory(handle, _win_file_identity(handle), True)
            else:  # pragma: no cover - exercised on POSIX hosts
                try:
                    os.mkdir(name, 0o700, dir_fd=parent.handle)
                    created = True
                except FileExistsError:
                    pass
                child = _open_child_directory(parent, name)
            if os.name == "nt":
                created = True
        if (
            _enforce_reparse_identity(
                enforcement_site, bool(child.identity.object_identity.reparse_attributes)
            )
            or child.identity.object_identity.file_type != stat.S_IFDIR
        ):
            raise OSError("unsafe owned directory")
        if created:
            _fsync_bound_directory(parent)
            _race_hook("owned_directory_parent_fsynced")
        return child
    except Exception:
        if child is not None:
            child.close()
        raise


def _revalidate_bound_child(parent: _BoundDirectory, name: str, expected: _BoundDirectory) -> None:
    current = _open_child_directory(parent, name)
    try:
        if current.identity.object_identity != expected.identity.object_identity:
            raise OSError("bound child identity changed")
    finally:
        current.close()


def _revalidate_ingress_lineage(
    data_root: Path,
    data_root_chain: Sequence[_BoundDirectory],
    ingress: _BoundDirectory,
) -> None:
    _revalidate_bound_directory_chain(data_root, data_root_chain)
    _revalidate_bound_child(data_root_chain[-1], "ingress", ingress)


def _require_snapshot_lineage(
    data_root: Path,
    data_root_chain: Sequence[_BoundDirectory],
    ingress: _BoundDirectory,
    *children: tuple[str, _BoundDirectory],
) -> None:
    try:
        _revalidate_ingress_lineage(data_root, data_root_chain, ingress)
        for name, child in children:
            _revalidate_bound_child(ingress, name, child)
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_changed") from None


def _unlink_bound_child_if_identity(
    parent: _BoundDirectory, name: str, expected: _FileIdentity
) -> None:
    descriptor = -1
    try:
        descriptor = _open_child_file_descriptor(parent, name, deny_mutation=True)
        current = os.fstat(descriptor)
        if (
            _file_identity(current).object_identity != expected.object_identity
            or not stat.S_ISREG(current.st_mode)
            or _is_reparse(current)
        ):
            return
        if os.name == "nt":
            _win_mark_delete(msvcrt.get_osfhandle(descriptor))
        else:  # pragma: no cover - exercised on POSIX hosts
            named = os.stat(name, dir_fd=parent.handle, follow_symlinks=False)
            if not _same_opened_identity(current, named):
                return
            os.unlink(name, dir_fd=parent.handle)
    except FileNotFoundError:
        return
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bound_child_file_identity(parent: _BoundDirectory, name: str) -> _FileIdentity:
    descriptor = _open_child_file_descriptor(parent, name, deny_mutation=True)
    try:
        return _file_identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _open_child_file_descriptor(parent: _BoundDirectory, name: str, *, deny_mutation: bool) -> int:
    if os.name == "nt":
        handle = _win_open_relative(parent, name, directory=False, deny_mutation=deny_mutation)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except Exception:  # pragma: no cover - native descriptor conversion failure
            _win_close(handle)
            raise
    if os.name != "nt":  # pragma: no cover - exercised on POSIX hosts
        return os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent.handle,
        )
    raise OSError("unsupported filesystem platform")  # pragma: no cover


def _source_mutation_guard(root: Path, components: Sequence[str]) -> _SourceMutationGuard:
    if sys.platform == "linux":  # pragma: no cover - exercised on Linux hosts
        return _linux_source_mutation_guard(root, components)
    return _SourceMutationGuard(-1)


def _linux_source_mutation_guard(  # pragma: no cover - exercised on Linux hosts
    root: Path, components: Sequence[str]
) -> _SourceMutationGuard:
    libc = ctypes.CDLL(None, use_errno=True)
    inotify_init1 = libc.inotify_init1
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int
    inotify_add_watch = libc.inotify_add_watch
    inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    inotify_add_watch.restype = ctypes.c_int
    descriptor = inotify_init1(getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    if descriptor < 0:
        raise SnapshotIngressError("replay_source_changed")
    directories: list[_BoundDirectory] = []
    mask = 0x00000004 | 0x00000008 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800 | 0x00000040 | 0x00000080
    try:
        directories.extend(_open_absolute_directory_chain(root))
        watched = [directories[-1]]
        for component in components[:-1]:
            directories.append(_open_child_directory(directories[-1], component))
            watched.append(directories[-1])
        for directory in watched:
            descriptor_path = os.fsencode(f"/proc/self/fd/{directory.handle}")
            if inotify_add_watch(descriptor, descriptor_path, mask) < 0:
                raise OSError(ctypes.get_errno(), "secure Linux source watch failed")
        return _SourceMutationGuard(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    finally:
        for directory in reversed(directories):
            directory.close()


def _open_source_securely(root: Path, components: Sequence[str], max_replay_bytes: int) -> _OpenedSource:
    directories = list(_open_absolute_directory_chain(root))
    descriptor = -1
    try:
        for component in components[:-1]:
            directories.append(_open_child_directory(directories[-1], component))
        _race_hook("source_parent_bound")
        descriptor = _open_child_file_descriptor(directories[-1], components[-1], deny_mutation=True)
        info = os.fstat(descriptor)
        identity = _file_identity(info)
        if _enforce_reparse_identity(  # pragma: no cover - descriptor defense in depth
            "source", bool(identity.object_identity.reparse_attributes)
        ):
            raise SnapshotIngressError("replay_source_unsafe")
        if identity.object_identity.file_type != stat.S_IFREG:  # pragma: no cover - descriptor defense in depth
            raise SnapshotIngressError("replay_source_not_regular")
        if identity.link_count != 1:  # pragma: no cover - descriptor defense in depth
            raise SnapshotIngressError("replay_source_hardlinked")
        if identity.size > max_replay_bytes:  # pragma: no cover - descriptor defense in depth
            raise SnapshotIngressError("replay_source_oversize")
        return _OpenedSource(descriptor, tuple(directories), info)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        for directory in reversed(directories):
            directory.close()
        raise


def _revalidate_secure_source(root: Path, components: Sequence[str], opened: _OpenedSource) -> None:
    fresh_directories: list[_BoundDirectory] = []
    fresh_descriptor = -1
    try:
        fresh_directories.extend(_open_absolute_directory_chain(root))
        for component in components[:-1]:
            fresh_directories.append(_open_child_directory(fresh_directories[-1], component))
        if len(fresh_directories) != len(opened.directories):
            raise SnapshotIngressError("replay_source_changed")
        root_index = len(_path_components(root))
        if any(
            fresh.identity.object_identity != original.identity.object_identity
            or (index >= root_index and fresh.identity != original.identity)
            for index, (fresh, original) in enumerate(zip(fresh_directories, opened.directories, strict=True))
        ):
            raise SnapshotIngressError("replay_source_changed")
        fresh_descriptor = _open_child_file_descriptor(fresh_directories[-1], components[-1], deny_mutation=True)
        if not _same_opened_identity(os.fstat(fresh_descriptor), os.fstat(opened.descriptor)):
            raise SnapshotIngressError("replay_source_changed")
    except SnapshotIngressError:
        raise
    except OSError:
        raise SnapshotIngressError("replay_source_changed") from None
    finally:
        if fresh_descriptor >= 0:
            os.close(fresh_descriptor)
        for directory in reversed(fresh_directories):
            directory.close()


def _validated_relative_name(value: str) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value.startswith(("/", "\\"))
        or value.endswith(("/", "\\"))
        or "\\" in value
        or ":" in value
        or "%" in value
        or unicodedata.normalize("NFC", value) != value
        or any(character in _WINDOWS_INVALID_COMPONENT_CHARACTERS for character in value)
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise SnapshotIngressError("replay_relative_name_invalid")
    components = value.split("/")
    if any(component in {"", ".", ".."} or component.endswith((".", " ")) for component in components):
        raise SnapshotIngressError("replay_relative_name_invalid")
    if any(
        component.split(".", 1)[0].rstrip(" .").casefold() in _WINDOWS_RESERVED_BASENAMES for component in components
    ):
        raise SnapshotIngressError("replay_relative_name_invalid")
    filename = components[-1]
    suffixes = filename.split(".")[1:]
    stem = filename[: -len(".rep")] if filename.endswith(".rep") else ""
    if len(suffixes) != 1 or suffixes[0] != "rep" or not stem:
        raise SnapshotIngressError("replay_relative_name_invalid")
    return tuple(components)


def _contained(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(root), os.fspath(candidate)))
    except ValueError:
        return False
    return os.path.normcase(common).casefold() == os.path.normcase(os.fspath(root)).casefold()


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        raise SnapshotIngressError("replay_source_unavailable") from None


def _capture_directory_chain(root: Path, relative_components: Sequence[str]) -> tuple[tuple[Path, _ObjectIdentity], ...]:
    paths = list(_path_components(root))
    current = root
    for component in relative_components[:-1]:
        current /= component
        paths.append(current)
    identities: list[tuple[Path, _ObjectIdentity]] = []
    for path in paths:
        info = _lstat(path)
        if (
            path.is_symlink()
            or _enforce_reparse_identity("source", _is_reparse(info))
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise SnapshotIngressError("replay_source_unsafe")
        identities.append((path, _object_identity(info)))
    return tuple(identities)


def _revalidate_directory_chain(chain: Sequence[tuple[Path, _ObjectIdentity]]) -> None:
    for path, expected in chain:
        try:
            info = path.lstat()
        except OSError:
            raise SnapshotIngressError("replay_source_changed") from None
        if (
            path.is_symlink()
            or _is_reparse(info)
            or not stat.S_ISDIR(info.st_mode)
            or _object_identity(info) != expected
        ):
            raise SnapshotIngressError("replay_source_changed")


def _require_source_leaf(path: Path, max_replay_bytes: int) -> tuple[os.stat_result, _FileIdentity]:
    info = _lstat(path)
    if path.is_symlink() or _enforce_reparse_identity("source", _is_reparse(info)):
        raise SnapshotIngressError("replay_source_unsafe")
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotIngressError("replay_source_not_regular")
    if info.st_nlink != 1:
        raise SnapshotIngressError("replay_source_hardlinked")
    if info.st_size > max_replay_bytes:
        raise SnapshotIngressError("replay_source_oversize")
    return info, _file_identity(info)


def _source_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _race_hook(_event: str) -> None:
    """Deterministic no-op seam used only to place real filesystem race mutations in tests."""


def _ensure_owned_directory(path: Path, *, enforcement_site: str = "ingress") -> None:
    created = False
    parent_directories: tuple[_BoundDirectory, ...] = ()
    child_directory: _BoundDirectory | None = None
    try:
        parent_directories = _open_absolute_directory_chain(path.parent)
        if os.name == "nt":
            try:
                handle = _win_open_relative(
                    parent_directories[-1],
                    path.name,
                    directory=True,
                    deny_mutation=False,
                    prevent_rename=True,
                )
            except OSError as error:
                if error.errno not in {2, 3}:
                    raise
                handle = _win_open_relative(
                    parent_directories[-1],
                    path.name,
                    directory=True,
                    deny_mutation=False,
                    prevent_rename=True,
                    disposition=_FILE_OPEN_IF,
                )
                created = True
            identity = _win_file_identity(handle)
            child_directory = _BoundDirectory(handle, identity, True)
            unsafe = _enforce_reparse_identity(
                enforcement_site, bool(identity.object_identity.reparse_attributes)
            ) or identity.object_identity.file_type != stat.S_IFDIR
        else:  # pragma: no cover - exercised on POSIX hosts
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent_directories[-1].handle)
                created = True
            except FileExistsError:
                pass
            child_directory = _open_child_directory(parent_directories[-1], path.name)
            unsafe = _enforce_reparse_identity(
                enforcement_site, bool(child_directory.identity.object_identity.reparse_attributes)
            )
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None
    finally:
        if child_directory is not None:
            child_directory.close()
        for directory in reversed(parent_directories):
            directory.close()
    if unsafe:
        raise SnapshotIngressError("ingress_snapshot_unavailable")
    if created:
        _fsync_directory(path.parent)
        _race_hook("owned_directory_parent_fsynced")


def _copy_descriptor(
    source_descriptor: int,
    staging_directory: Path,
    max_replay_bytes: int,
    *,
    bound_staging: _BoundDirectory | None = None,
) -> tuple[_OwnedTemporary, str, int]:
    temporary_path: Path | None = None
    temporary_name: str | None = None
    temporary_identity: _FileIdentity | None = None
    anonymous = False
    descriptor = -1
    try:
        if os.name == "nt" and bound_staging is not None:
            temporary_name = f".replay-{uuid4().hex}.tmp"
            handle = _win_open_relative(
                bound_staging,
                temporary_name,
                directory=False,
                deny_mutation=False,
                prevent_rename=True,
                writable=True,
                allow_delete=True,
                disposition=_FILE_CREATE,
            )
            try:
                descriptor = msvcrt.open_osfhandle(
                    handle,
                    os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
                )
            except Exception:  # pragma: no cover - native descriptor conversion failure
                _win_close(handle)
                raise
            temporary_path = staging_directory / temporary_name
        elif os.name != "nt" and bound_staging is not None:  # pragma: no cover - exercised on POSIX hosts
            temporary_flags = getattr(os, "O_TMPFILE", 0)
            if temporary_flags == 0:
                raise SnapshotIngressError("ingress_snapshot_unavailable")
            descriptor = os.open(
                ".",
                os.O_RDWR | temporary_flags | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=bound_staging.handle,
            )
            anonymous = True
        else:  # pragma: no cover - legacy direct helper calls
            descriptor, temporary_name = tempfile.mkstemp(prefix=".replay-", suffix=".tmp", dir=staging_directory)
            temporary_path = Path(temporary_name)
            temporary_name = temporary_path.name
        temporary_identity = _file_identity(os.fstat(descriptor))
        digest = hashlib.sha256()
        size = 0
        while True:
            read_limit = min(_READ_CHUNK_SIZE, max_replay_bytes - size + 1)
            chunk = os.read(source_descriptor, read_limit)
            if not chunk:
                break
            size += len(chunk)
            if size > max_replay_bytes:
                raise SnapshotIngressError("replay_source_oversize")
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written < 1:
                    raise OSError("immutable ingress write made no progress")
                view = view[written:]
            digest.update(chunk)
        os.fsync(descriptor)
        return (
            _OwnedTemporary(descriptor, temporary_identity, temporary_path, temporary_name, anonymous),
            digest.hexdigest(),
            size,
        )
    except SnapshotIngressError:  # pragma: no cover - POSIX concurrent growth path
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_identity is not None and temporary_name is not None and bound_staging is not None:
            _unlink_bound_child_if_identity(bound_staging, temporary_name, temporary_identity)
        elif temporary_path is not None and temporary_identity is not None:
            _unlink_if_identity(temporary_path, temporary_identity)
        raise
    except OSError:  # pragma: no cover - defensive low-level write failure cleanup
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_identity is not None and temporary_name is not None and bound_staging is not None:
            try:
                _unlink_bound_child_if_identity(bound_staging, temporary_name, temporary_identity)
            except OSError:
                pass
        elif temporary_path is not None and temporary_identity is not None:
            try:
                _unlink_if_identity(temporary_path, temporary_identity)
            except OSError:
                pass
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None


def _dispose_owned_temporary(temporary: _OwnedTemporary, staging: _BoundDirectory) -> None:
    if temporary.descriptor >= 0:
        if os.name == "nt":
            _win_mark_delete(msvcrt.get_osfhandle(temporary.descriptor))
        os.close(temporary.descriptor)
        temporary.descriptor = -1
    if temporary.name is not None:
        _unlink_bound_child_if_identity(staging, temporary.name, temporary.identity)
        _fsync_bound_directory(staging)
        temporary.name = None
        temporary.path = None


def _hash_open_descriptor(descriptor: int, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, max_bytes - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise SnapshotIngressError("ingress_snapshot_collision")
        digest.update(chunk)
    return digest.hexdigest(), size


def _same_opened_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare stable pathname/descriptor fields; Windows hardlink unlinking can report a stale ctime view."""
    return (
        _object_identity(left) == _object_identity(right)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_nlink == right.st_nlink
    )


def _verify_ingress_target(
    target: Path,
    expected_sha256: str,
    expected_size: int,
    *,
    bound_parent: _BoundDirectory | None = None,
) -> None:
    fresh_descriptor = -1
    try:
        if bound_parent is not None:
            descriptor = _open_child_file_descriptor(bound_parent, target.name, deny_mutation=True)
            before = os.fstat(descriptor)
        else:
            before = target.lstat()
            if target.is_symlink() or _is_reparse(before):
                raise SnapshotIngressError("ingress_snapshot_collision")
            descriptor = os.open(target, _source_open_flags())
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _is_reparse(opened)
                or not _same_opened_identity(opened, before)
            ):
                raise SnapshotIngressError("ingress_snapshot_collision")
            actual_sha256, actual_size = _hash_open_descriptor(descriptor, expected_size)
            after = os.fstat(descriptor)
            if bound_parent is not None:
                fresh_descriptor = _open_child_file_descriptor(bound_parent, target.name, deny_mutation=True)
                try:
                    named_after = os.fstat(fresh_descriptor)
                finally:
                    os.close(fresh_descriptor)
                    fresh_descriptor = -1
            else:
                named_after = target.lstat()
        finally:
            os.close(descriptor)
    except SnapshotIngressError:
        raise
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_collision") from None
    if (
        not _same_opened_identity(after, before)
        or not _same_opened_identity(after, named_after)
        or actual_sha256 != expected_sha256
        or actual_size != expected_size
    ):
        raise SnapshotIngressError("ingress_snapshot_collision")


def _read_verified_ingress_target(
    target: Path,
    expected_sha256: str,
    expected_size: int,
    *,
    data_root: Path,
    ingress_identity: _ObjectIdentity,
    shard_identity: _ObjectIdentity,
    target_identity: _ObjectIdentity,
) -> bytes:
    data_root_directories: tuple[_BoundDirectory, ...] = ()
    ingress: _BoundDirectory | None = None
    shard: _BoundDirectory | None = None
    descriptor = -1
    fresh_descriptor = -1
    try:
        canonical_target = data_root / "ingress" / expected_sha256[:2] / f"{expected_sha256}.rep"
        if target != canonical_target:
            raise SnapshotIngressError("ingress_snapshot_collision")
        data_root_directories = _open_absolute_directory_chain(data_root)
        ingress = _open_child_directory(data_root_directories[-1], "ingress")
        if ingress.identity.object_identity != ingress_identity:
            raise SnapshotIngressError("ingress_snapshot_collision")
        shard = _open_child_directory(ingress, expected_sha256[:2])
        if shard.identity.object_identity != shard_identity:
            raise SnapshotIngressError("ingress_snapshot_collision")
        descriptor = _open_child_file_descriptor(shard, target.name, deny_mutation=True)
        opened = os.fstat(descriptor)
        identity = _file_identity(opened)
        if (
            identity.object_identity.reparse_attributes
            or identity.object_identity.file_type != stat.S_IFREG
            or identity.object_identity != target_identity
            or identity.link_count != 1
            or identity.size != expected_size
        ):
            raise SnapshotIngressError("ingress_snapshot_collision")
        _race_hook("snapshot_reader_after_open")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, expected_size - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                raise SnapshotIngressError("ingress_snapshot_collision")
            chunks.append(chunk)
            digest.update(chunk)
        fresh_descriptor = _open_child_file_descriptor(shard, target.name, deny_mutation=True)
        if not _same_opened_identity(os.fstat(fresh_descriptor), os.fstat(descriptor)):
            raise SnapshotIngressError("ingress_snapshot_collision")
        _revalidate_bound_child(ingress, expected_sha256[:2], shard)
        _revalidate_ingress_lineage(data_root, data_root_directories, ingress)
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise SnapshotIngressError("ingress_snapshot_collision")
        return b"".join(chunks)
    except SnapshotIngressError:
        raise
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_collision") from None
    finally:
        if fresh_descriptor >= 0:
            os.close(fresh_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        if shard is not None:
            shard.close()
        if ingress is not None:
            ingress.close()
        for directory in reversed(data_root_directories):
            directory.close()


def _publish_snapshot(
    temporary_path: Path | None,
    target: Path,
    expected_sha256: str,
    expected_size: int,
    *,
    source_descriptor: int | None = None,
    target_directory: _BoundDirectory | None = None,
) -> bool:
    try:
        if os.name == "nt" and source_descriptor is not None and target_directory is not None:
            _win_link_handle(msvcrt.get_osfhandle(source_descriptor), target_directory, target.name)
        elif os.name != "nt" and source_descriptor is not None and target_directory is not None:  # pragma: no cover
            _posix_link_handle(source_descriptor, target_directory, target.name)
        else:  # pragma: no cover - exercised on POSIX hosts and legacy direct helper calls
            if temporary_path is None:
                raise OSError("named immutable ingress temp is unavailable")
            os.link(temporary_path, target)
    except FileExistsError:
        _verify_ingress_target(target, expected_sha256, expected_size, bound_parent=target_directory)
        return False
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None
    if target_directory is not None:
        _fsync_bound_directory(target_directory)
    else:  # pragma: no cover - legacy direct helper calls
        _fsync_directory(target.parent)
    return True


# TheSuperHackers @feature Leex 22/08/2026 Persist random watched-root IDs under a narrow atomic product lock. (#TBD)
class WatchedRootRegistry:
    """Reconcile configured watched folders without exposing their locators."""

    __slots__ = ("_current_roots", "_data_root", "_max_replay_bytes")

    def __init__(self, data_root: Path, *, max_replay_bytes: int = 64 * 1024 * 1024) -> None:
        if max_replay_bytes < 1:
            raise ValueError("maximum replay size must be positive")
        self._data_root = _absolute_without_following(Path(data_root))
        self._max_replay_bytes = max_replay_bytes
        self._current_roots: dict[str, _SelectableRoot] = {}

    def __repr__(self) -> str:
        return "WatchedRootRegistry()"

    @property
    def data_root(self) -> Path:
        """Return the private product root for Analytics composition only."""
        return self._data_root

    def reconcile(self, watched_folders: Sequence[Path]) -> tuple[WatchedRoot, ...]:
        """Persist new opaque identities and return only currently configured roots."""
        configured: list[tuple[str, Path]] = []
        configured_keys: set[str] = set()
        configured_aliases: set[_ObjectIdentity] = set()
        for folder in watched_folders:
            path = _absolute_without_following(Path(folder))
            key = _normalized_path_key(path)
            if key in configured_keys:
                raise RootRegistryError("watched_root_path_collision")
            alias_identity = _verified_root_alias_identity(path)
            if alias_identity is not None and alias_identity in configured_aliases:
                raise RootRegistryError("watched_root_path_collision")
            configured_keys.add(key)
            if alias_identity is not None:
                configured_aliases.add(alias_identity)
            configured.append((key, path))

        registry_path = self._data_root / _REGISTRY_NAME
        with _registry_lock(self._data_root) as bound_data_root:
            entries, original_bytes, original_identity = _read_registry(registry_path)
            _race_hook("registry_after_read")
            by_key = {entry.path_key_sha256: entry for entry in entries}
            next_label = max(
                (int(match.group(1)) for entry in entries if (match := _GENERIC_LABEL_PATTERN.fullmatch(entry.label))),
                default=0,
            )
            changed = original_bytes is None
            for key, _path in configured:
                if key not in by_key:
                    next_label += 1
                    entry = _RegistryEntry(key, str(uuid4()), f"Replay folder {next_label}")
                    entries.append(entry)
                    by_key[key] = entry
                    changed = True
            if changed:
                _publish_registry(
                    registry_path,
                    _serialized_registry(entries),
                    expected_bytes=original_bytes,
                    expected_identity=original_identity,
                    bound_data_root=bound_data_root,
                )

        selectable: dict[str, _SelectableRoot] = {}
        snapshots = []
        for key, path in configured:
            entry = by_key[key]
            available, reason_code = _root_availability(path)
            snapshots.append(WatchedRoot(entry.root_public_id, entry.label, available, reason_code))
            if available:
                selectable[entry.root_public_id] = _SelectableRoot(path, _capture_directory_chain(path, ()))
        self._current_roots = selectable
        return tuple(snapshots)

    # TheSuperHackers @feature Leex 22/08/2026 Copy a revalidated source descriptor into no-replace ingress storage. (#TBD)
    def snapshot_replay(self, root_public_id: str, relative_name: str) -> IngressSnapshot:
        """Copy one safely selected replay descriptor into immutable product-owned ingress."""
        selected_root = self._current_roots.get(root_public_id)
        if selected_root is None:
            raise SnapshotIngressError("watched_root_unknown")
        root = selected_root.path
        try:
            _revalidate_directory_chain(selected_root.directory_chain)
        except SnapshotIngressError:
            raise SnapshotIngressError("replay_source_changed") from None
        components = _validated_relative_name(relative_name)
        source_path = root.joinpath(*components)
        if not _contained(root, source_path):
            raise SnapshotIngressError("replay_relative_name_invalid")

        try:
            mutation_guard = _source_mutation_guard(root, components)
        except OSError:
            _capture_directory_chain(root, components)
            raise SnapshotIngressError("replay_source_changed") from None
        try:
            directory_chain = _capture_directory_chain(root, components)
            initial_info, _initial_identity = _require_source_leaf(source_path, self._max_replay_bytes)
        except SnapshotIngressError:
            mutation_guard.close()
            raise
        try:
            _race_hook("before_open")
            mutation_guard.assert_unchanged()
            opened_source = _open_source_securely(root, components, self._max_replay_bytes)
            mutation_guard.assert_unchanged()
        except SnapshotIngressError:
            mutation_guard.close()
            raise SnapshotIngressError("replay_source_changed") from None
        except OSError:
            mutation_guard.close()
            raise SnapshotIngressError("replay_source_changed") from None

        temporary: _OwnedTemporary | None = None
        created_target_identity: _FileIdentity | None = None
        data_root_bound: tuple[_BoundDirectory, ...] = ()
        ingress_bound: _BoundDirectory | None = None
        staging_bound: _BoundDirectory | None = None
        shard_bound: _BoundDirectory | None = None
        target: Path | None = None
        created = False
        try:
            opened_info = opened_source.initial_info
            expected_identity = _file_identity(opened_info)
            if not _same_opened_identity(opened_info, initial_info):
                raise SnapshotIngressError("replay_source_changed")
            _revalidate_directory_chain(directory_chain)
            _race_hook("after_open")
            if not _contained(root, source_path):
                raise SnapshotIngressError("replay_source_changed")
            _revalidate_secure_source(root, components, opened_source)

            ingress_root = self._data_root / "ingress"
            staging_directory = ingress_root / ".staging"
            data_root_bound = _open_absolute_directory_chain(self._data_root)
            ingress_bound = _ensure_owned_child_directory(data_root_bound[-1], "ingress")
            staging_bound = _ensure_owned_child_directory(ingress_bound, ".staging")
            try:
                temporary, digest, size = _copy_descriptor(
                    opened_source.descriptor,
                    staging_directory,
                    self._max_replay_bytes,
                    bound_staging=staging_bound,
                )
            except OSError:
                raise SnapshotIngressError("ingress_snapshot_changed") from None
            _require_snapshot_lineage(
                self._data_root, data_root_bound, ingress_bound, (".staging", staging_bound)
            )
            _race_hook("after_copy")
            mutation_guard.assert_unchanged()
            _require_snapshot_lineage(
                self._data_root, data_root_bound, ingress_bound, (".staging", staging_bound)
            )
            try:
                after_copy = os.fstat(opened_source.descriptor)
            except OSError:
                raise SnapshotIngressError("replay_source_changed") from None
            if _file_identity(after_copy) != _file_identity(opened_info) or size != expected_identity.size:
                raise SnapshotIngressError("replay_source_changed")
            if not _contained(root, source_path):
                raise SnapshotIngressError("replay_source_changed")
            _revalidate_secure_source(root, components, opened_source)

            lock_directory = ingress_root / ".locks"
            with _digest_lock(lock_directory, digest, bound_ingress=ingress_bound):
                _require_snapshot_lineage(self._data_root, data_root_bound, ingress_bound)
                shard = ingress_root / digest[:2]
                shard_bound = _ensure_owned_child_directory(ingress_bound, digest[:2])
                target = shard / f"{digest}.rep"
                try:
                    _race_hook("before_publish")
                except OSError:
                    raise SnapshotIngressError("ingress_snapshot_changed") from None
                _require_snapshot_lineage(
                    self._data_root, data_root_bound, ingress_bound, (digest[:2], shard_bound)
                )
                try:
                    created = _publish_snapshot(
                        temporary.path,
                        target,
                        digest,
                        size,
                        source_descriptor=temporary.descriptor,
                        target_directory=shard_bound,
                    )
                    if created:
                        created_target_identity = _file_identity(os.fstat(temporary.descriptor))
                    _dispose_owned_temporary(temporary, staging_bound)
                    temporary = None
                    created_target_identity = _bound_child_file_identity(shard_bound, target.name)
                    _require_snapshot_lineage(
                        self._data_root,
                        data_root_bound,
                        ingress_bound,
                        (".staging", staging_bound),
                        (digest[:2], shard_bound),
                    )
                    _verify_ingress_target(target, digest, size, bound_parent=shard_bound)
                    _require_snapshot_lineage(
                        self._data_root, data_root_bound, ingress_bound, (digest[:2], shard_bound)
                    )
                    _race_hook("after_final_verify")
                    _verify_ingress_target(target, digest, size, bound_parent=shard_bound)
                    _require_snapshot_lineage(
                        self._data_root, data_root_bound, ingress_bound, (digest[:2], shard_bound)
                    )
                except Exception as error:
                    if created and created_target_identity is not None:
                        try:
                            _unlink_bound_child_if_identity(shard_bound, target.name, created_target_identity)
                            _fsync_bound_directory(shard_bound)
                        except OSError:
                            pass
                        created_target_identity = None
                    if isinstance(error, OSError):
                        raise SnapshotIngressError("ingress_snapshot_changed") from None
                    raise
            if target is None or shard_bound is None or created_target_identity is None:
                raise SnapshotIngressError("ingress_snapshot_changed")
            return IngressSnapshot(
                target,
                digest,
                size,
                root_public_id,
                relative_name,
                created,
                self._data_root,
                ingress_bound.identity.object_identity,
                shard_bound.identity.object_identity,
                created_target_identity.object_identity,
            )
        except SnapshotIngressError:
            raise
        except OSError:
            raise SnapshotIngressError("ingress_snapshot_unavailable") from None
        finally:
            opened_source.close()
            mutation_guard.close()
            if temporary is not None:
                _race_hook("before_temporary_cleanup")
            if temporary is not None and staging_bound is not None:
                try:
                    _dispose_owned_temporary(temporary, staging_bound)
                except OSError:
                    pass
            for directory in (shard_bound, staging_bound, ingress_bound):
                if directory is not None:
                    directory.close()
            for directory in reversed(data_root_bound):
                directory.close()
