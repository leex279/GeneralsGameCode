"""Persistent opaque watched-root identities and immutable replay ingress."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import UUID, uuid4

_REGISTRY_NAME = "watched-roots-v1.json"
_LOCK_NAME = ".watched-roots-v1.lock"
_REGISTRY_VERSION = 1
_MAX_REGISTRY_BYTES = 256 * 1024
_MAX_REGISTRY_ENTRIES = 4096
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
def _registry_lock(data_root: Path) -> Iterator[None]:
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
                    yield
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
def _digest_lock(lock_directory: Path, digest: str) -> Iterator[None]:
    lock_path = lock_directory / f"{digest}.lock"
    try:
        lock_directory.mkdir(parents=True, exist_ok=True)
        directory_info = lock_directory.lstat()
        if (
            lock_directory.is_symlink()
            or _is_reparse(directory_info)
            or not stat.S_ISDIR(directory_info.st_mode)
        ):
            raise SnapshotIngressError("ingress_snapshot_unavailable")
        with _thread_lock(lock_path):
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOINHERIT", 0), 0o600)
            try:
                opened = os.fstat(descriptor)
                named = lock_path.lstat()
                if (
                    lock_path.is_symlink()
                    or _is_reparse(named)
                    or not stat.S_ISREG(named.st_mode)
                    or named.st_nlink != 1
                    or not _same_opened_identity(opened, named)
                ):
                    raise SnapshotIngressError("ingress_snapshot_collision")
                if opened.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                    _fsync_directory(lock_directory)
                _lock_descriptor(descriptor)
                try:
                    yield
                finally:
                    current = lock_path.lstat()
                    if current.st_nlink != 1 or not _same_opened_identity(os.fstat(descriptor), current):
                        raise SnapshotIngressError("ingress_snapshot_changed")
                    _unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)
    except SnapshotIngressError:
        raise
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None


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
    if path.is_symlink() or _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
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


def _publish_registry(
    path: Path,
    payload: bytes,
    *,
    expected_bytes: bytes | None,
    expected_identity: _FileIdentity | None,
) -> None:
    temporary_path: Path | None = None
    temporary_identity: _FileIdentity | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        temporary_identity = _file_identity(temporary_path.lstat())
        _race_hook("registry_before_replace")
        _entries, current_bytes, current_identity = _read_registry(path)
        if current_bytes != expected_bytes or current_identity != expected_identity:
            raise RootRegistryError("watched_root_registry_changed")
        os.replace(temporary_path, path)
        temporary_path = None
        _entries, published_bytes, _published_identity = _read_registry(path)
        if published_bytes != payload:
            raise RootRegistryError("watched_root_registry_changed")
        _fsync_directory(path.parent)
    except RootRegistryError:
        raise
    except OSError:
        raise RootRegistryError("watched_root_registry_unavailable") from None
    finally:
        if temporary_path is not None and temporary_identity is not None:
            try:
                _unlink_if_identity(temporary_path, temporary_identity)
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

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _NTDLL = ctypes.WinDLL("ntdll")
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_DATA = 0x0001
    _FILE_WRITE_DATA = 0x0002
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _FILE_OPEN = 0x1
    _FILE_OPEN_IF = 0x3
    _FILE_DIRECTORY_FILE = 0x1
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x20
    _FILE_NON_DIRECTORY_FILE = 0x40
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x40
    _FILE_BASIC_INFO_CLASS = 0

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
        final_path = ctypes.create_unicode_buffer(32768)
        final_size = _KERNEL32.GetFinalPathNameByHandleW(handle, final_path, len(final_path), 0)
        if (  # pragma: no cover - host-dependent volume alias
            final_size == 0
            or final_size >= len(final_path)
            or final_path.value.rstrip("\\").casefold() != root.rstrip("\\").casefold()
        ):
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
        if identity.object_identity.reparse_attributes:  # pragma: no cover - descriptor defense in depth
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
        if any(
            fresh.identity != original.identity
            for fresh, original in zip(fresh_directories, opened.directories, strict=True)
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
        if path.is_symlink() or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
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
    if path.is_symlink() or _is_reparse(info):
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


def _ensure_owned_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None
    if path.is_symlink() or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise SnapshotIngressError("ingress_snapshot_unavailable")


def _copy_descriptor(
    source_descriptor: int,
    staging_directory: Path,
    max_replay_bytes: int,
) -> tuple[Path, str, int, int]:
    temporary_path: Path | None = None
    temporary_identity: _FileIdentity | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".replay-", suffix=".tmp", dir=staging_directory)
        temporary_path = Path(temporary_name)
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
        return temporary_path, digest.hexdigest(), size, descriptor
    except SnapshotIngressError:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None and temporary_identity is not None:
            _unlink_if_identity(temporary_path, temporary_identity)
        raise
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None and temporary_identity is not None:
            try:
                _unlink_if_identity(temporary_path, temporary_identity)
            except OSError:
                pass
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None


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


def _verify_ingress_target(target: Path, expected_sha256: str, expected_size: int) -> None:
    try:
        before = target.lstat()
        if target.is_symlink() or _is_reparse(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SnapshotIngressError("ingress_snapshot_collision")
        descriptor = os.open(target, _source_open_flags())
        try:
            opened = os.fstat(descriptor)
            if not _same_opened_identity(opened, before):
                raise SnapshotIngressError("ingress_snapshot_collision")
            actual_sha256, actual_size = _hash_open_descriptor(descriptor, expected_size)
            after = os.fstat(descriptor)
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


def _publish_snapshot(temporary_path: Path, target: Path, expected_sha256: str, expected_size: int) -> bool:
    try:
        os.link(temporary_path, target)
    except FileExistsError:
        _verify_ingress_target(target, expected_sha256, expected_size)
        return False
    except OSError:
        raise SnapshotIngressError("ingress_snapshot_unavailable") from None
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
        for folder in watched_folders:
            path = _absolute_without_following(Path(folder))
            key = _normalized_path_key(path)
            if key in configured_keys:
                raise RootRegistryError("watched_root_path_collision")
            configured_keys.add(key)
            configured.append((key, path))

        registry_path = self._data_root / _REGISTRY_NAME
        with _registry_lock(self._data_root):
            entries, original_bytes, original_identity = _read_registry(registry_path)
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

        directory_chain = _capture_directory_chain(root, components)
        initial_info, _initial_identity = _require_source_leaf(source_path, self._max_replay_bytes)
        _race_hook("before_open")
        try:
            opened_source = _open_source_securely(root, components, self._max_replay_bytes)
        except SnapshotIngressError:
            raise SnapshotIngressError("replay_source_changed") from None
        except OSError:
            raise SnapshotIngressError("replay_source_changed") from None

        temporary_path: Path | None = None
        temporary_identity: _FileIdentity | None = None
        temporary_descriptor = -1
        created_target_identity: _FileIdentity | None = None
        ingress_bound: tuple[_BoundDirectory, ...] = ()
        staging_bound: tuple[_BoundDirectory, ...] = ()
        shard_bound: tuple[_BoundDirectory, ...] = ()
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
            _ensure_owned_directory(ingress_root)
            _ensure_owned_directory(staging_directory)
            ingress_bound = _open_absolute_directory_chain(ingress_root)
            staging_bound = _open_absolute_directory_chain(staging_directory)
            staging_identity = _object_identity(staging_directory.lstat())
            try:
                temporary_path, digest, size, temporary_descriptor = _copy_descriptor(
                    opened_source.descriptor,
                    staging_directory,
                    self._max_replay_bytes,
                )
            except OSError:
                raise SnapshotIngressError("ingress_snapshot_changed") from None
            temporary_identity = _file_identity(temporary_path.lstat())
            _revalidate_bound_directory_chain(ingress_root, ingress_bound)
            _revalidate_bound_directory_chain(staging_directory, staging_bound)
            if _object_identity(staging_directory.lstat()) != staging_identity:
                raise SnapshotIngressError("ingress_snapshot_changed")
            _race_hook("after_copy")
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
            with _digest_lock(lock_directory, digest):
                shard = ingress_root / digest[:2]
                _ensure_owned_directory(shard)
                shard_bound = _open_absolute_directory_chain(shard)
                shard_identity = _object_identity(shard.lstat())
                target = shard / f"{digest}.rep"
                try:
                    _race_hook("before_publish")
                except OSError:
                    raise SnapshotIngressError("ingress_snapshot_changed") from None
                created = _publish_snapshot(temporary_path, target, digest, size)
                os.close(temporary_descriptor)
                temporary_descriptor = -1
                if created:
                    created_target_identity = _file_identity(target.lstat())
                    _unlink_if_identity(temporary_path, temporary_identity)
                    _fsync_directory(staging_directory)
                    temporary_path = None
                    temporary_identity = None
                    created_target_identity = _file_identity(target.lstat())
                if _object_identity(shard.lstat()) != shard_identity:
                    if created_target_identity is not None:
                        _unlink_if_identity(target, created_target_identity)
                        _fsync_directory(shard)
                        created_target_identity = None
                    raise SnapshotIngressError("ingress_snapshot_changed")
                _revalidate_bound_directory_chain(shard, shard_bound)
                _verify_ingress_target(target, digest, size)
                _revalidate_bound_directory_chain(shard, shard_bound)
            return IngressSnapshot(target, digest, size, root_public_id, relative_name, created)
        except SnapshotIngressError:
            raise
        except OSError:
            raise SnapshotIngressError("ingress_snapshot_unavailable") from None
        finally:
            opened_source.close()
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            for chain in (shard_bound, staging_bound, ingress_bound):
                for directory in reversed(chain):
                    directory.close()
            if temporary_path is not None and temporary_identity is not None:
                try:
                    _unlink_if_identity(temporary_path, temporary_identity)
                    _fsync_directory(temporary_path.parent)
                except OSError:
                    pass
