"""Atomic content-addressed storage for analyzer-owned immutable bytes."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import secrets
import stat
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_READ_CHUNK_SIZE = 1024 * 1024
_PUBLICATION_SETTLE_ATTEMPTS = 50
_PUBLICATION_SETTLE_DELAY_SECONDS = 0.01


class ContentStorageError(RuntimeError):
    """Base failure for managed content that could not be safely stored or verified."""


class HashMismatchError(ContentStorageError):
    """Copied bytes differ from a caller-supplied content identity."""


class ContentCollisionError(ContentStorageError):
    """An occupied content path does not contain the bytes named by its digest."""


class _LineageError(OSError):
    """One opened storage ancestor no longer denotes its original object."""


@dataclass(frozen=True)
class StoredContent:
    """One immutable managed object and whether this call first published it."""

    sha256: str
    path: Path
    size: int
    created: bool


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
    handle: int = field(repr=False)
    identity: _FileIdentity
    windows: bool

    def close(self) -> None:
        if self.handle < 0:
            return
        if self.windows:
            _win_close(self.handle)
        else:  # pragma: no cover - exercised on POSIX hosts
            os.close(self.handle)
        self.handle = -1


@dataclass(slots=True)
class _BoundShard:
    root_chain: tuple[_BoundDirectory, ...]
    shard: _BoundDirectory
    shard_name: str

    @property
    def root(self) -> _BoundDirectory:
        return self.root_chain[-1]

    def close(self) -> None:
        self.shard.close()
        for directory in reversed(self.root_chain):
            directory.close()


def _race_hook(_site: str) -> None:
    """Test seam for deterministic storage-lineage substitutions."""


def _absolute_without_following(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _normalize_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise HashMismatchError("expected SHA-256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def _file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        chunk = source.read(_READ_CHUNK_SIZE)
        while chunk:
            yield chunk
            chunk = source.read(_READ_CHUNK_SIZE)


def _hash_chunks(chunks: Iterable[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


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
    _KERNEL32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _KERNEL32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _KERNEL32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _KERNEL32.SetFileInformationByHandle.restype = wintypes.BOOL
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


def _win_raise_last_error() -> None:  # pragma: no cover - native API failure
    raise OSError(ctypes.get_last_error(), "secure Windows storage operation failed")


def _win_close(handle: int) -> None:
    if os.name == "nt" and not _KERNEL32.CloseHandle(handle):  # pragma: no cover - native API failure
        _win_raise_last_error()


def _win_file_identity(handle: int) -> _FileIdentity:
    information = _ByHandleFileInformation()
    if not _KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(information)):  # pragma: no cover
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
        0,
        0,
        information.nNumberOfLinks,
    )


def _win_final_path(handle: int) -> str:
    value = ctypes.create_unicode_buffer(32768)
    size = _KERNEL32.GetFinalPathNameByHandleW(handle, value, len(value), 0)
    if size == 0 or size >= len(value):  # pragma: no cover - native API failure
        _win_raise_last_error()
    return value.value


def _win_expected_path(path: Path) -> str:
    value = os.fspath(_absolute_without_following(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _win_open_drive_root(path: Path) -> _BoundDirectory:
    drive, tail = os.path.splitdrive(os.fspath(path))
    if not drive or not tail.startswith("\\") or drive.startswith("\\"):
        raise OSError("unsupported Windows storage path")
    native_root = f"\\\\?\\{drive}\\"
    handle = _KERNEL32.CreateFileW(
        native_root,
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
    identity = _win_file_identity(value)
    if identity.object_identity.reparse_attributes or identity.object_identity.file_type != stat.S_IFDIR:
        _win_close(value)
        raise OSError("unsafe Windows storage drive")
    return _BoundDirectory(value, identity, True)


def _win_open_relative(
    parent: _BoundDirectory,
    name: str,
    *,
    directory: bool,
    writable: bool = False,
    allow_delete: bool = False,
    disposition: int = 1,
    prevent_mutation: bool = False,
) -> int:
    buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(encoded_length, len(buffer) * 2, ctypes.cast(buffer, wintypes.LPWSTR))
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
        options |= _FILE_NON_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT
        access |= _FILE_READ_DATA
        if writable:
            access |= _FILE_WRITE_DATA
        if allow_delete:
            access |= _DELETE_ACCESS | _FILE_WRITE_ATTRIBUTES
    if directory:
        share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        if not prevent_mutation:
            share |= _FILE_SHARE_DELETE
    else:
        share = _FILE_SHARE_READ
        if not prevent_mutation:
            share |= _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
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
        if code in {2, 3}:
            raise FileNotFoundError(code, "secure Windows relative object is missing")
        if code in {80, 183}:
            raise FileExistsError(code, "secure Windows relative object exists")
        raise OSError(code, "secure Windows relative open failed")
    if result.value is None:  # pragma: no cover - defensive native API contract
        raise OSError("secure Windows relative open returned no handle")
    return int(result.value)


def _win_mark_delete(handle: int) -> None:
    disposition = _FileDispositionInfo(True)
    if not _KERNEL32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):  # pragma: no cover - native API failure
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
        code = int(_NTDLL.RtlNtStatusToDosError(status))
        if code in {80, 183}:
            raise FileExistsError(code, "managed content target exists")
        raise OSError(code, "secure Windows hardlink failed")


def _open_absolute_directory_chain(path: Path, *, create: bool = False) -> tuple[_BoundDirectory, ...]:
    absolute = _absolute_without_following(path)
    directories: list[_BoundDirectory] = []
    try:
        if os.name == "nt":
            directories.append(_win_open_drive_root(absolute))
            for part in absolute.parts[1:]:
                handle = _win_open_relative(
                    directories[-1],
                    part,
                    directory=True,
                    disposition=_FILE_OPEN_IF if create else _FILE_OPEN,
                    prevent_mutation=True,
                )
                identity = _win_file_identity(handle)
                if identity.object_identity.reparse_attributes or identity.object_identity.file_type != stat.S_IFDIR:
                    _win_close(handle)
                    raise OSError("unsafe Windows storage directory")
                directories.append(_BoundDirectory(handle, identity, True))
        else:  # pragma: no cover - exercised on POSIX hosts
            descriptor = os.open(
                absolute.anchor,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            directories.append(_BoundDirectory(descriptor, _file_identity(os.fstat(descriptor)), False))
            for part in absolute.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=directories[-1].handle)
                    except FileExistsError:
                        pass
                descriptor = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directories[-1].handle,
                )
                identity = _file_identity(os.fstat(descriptor))
                if identity.object_identity.reparse_attributes or identity.object_identity.file_type != stat.S_IFDIR:
                    os.close(descriptor)
                    raise OSError("unsafe POSIX storage directory")
                directories.append(_BoundDirectory(descriptor, identity, False))
        return tuple(directories)
    except Exception:
        for directory in reversed(directories):
            directory.close()
        raise


def _open_child_directory(parent: _BoundDirectory, name: str, *, create: bool) -> _BoundDirectory:
    if os.name == "nt":
        handle = _win_open_relative(
            parent,
            name,
            directory=True,
            disposition=_FILE_OPEN_IF if create else _FILE_OPEN,
            prevent_mutation=True,
        )
        identity = _win_file_identity(handle)
        if identity.object_identity.reparse_attributes or identity.object_identity.file_type != stat.S_IFDIR:
            _win_close(handle)
            raise OSError("unsafe Windows storage shard")
        return _BoundDirectory(handle, identity, True)
    if create:  # pragma: no cover - exercised on POSIX hosts
        try:
            os.mkdir(name, 0o700, dir_fd=parent.handle)
        except FileExistsError:
            pass
    descriptor = os.open(  # pragma: no cover - exercised on POSIX hosts
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent.handle,
    )
    identity = _file_identity(os.fstat(descriptor))
    if identity.object_identity.reparse_attributes or identity.object_identity.file_type != stat.S_IFDIR:
        os.close(descriptor)
        raise OSError("unsafe POSIX storage shard")
    return _BoundDirectory(descriptor, identity, False)


def _bind_shard(root: Path, shard_name: str, *, create: bool) -> _BoundShard:
    try:
        chain = _open_absolute_directory_chain(root, create=create)
        try:
            shard = _open_child_directory(chain[-1], shard_name, create=create)
        except Exception:
            for directory in reversed(chain):
                directory.close()
            raise
        bound = _BoundShard(chain, shard, shard_name)
        try:
            _race_hook("storage_shard_bound")
            _revalidate_bound_shard(root, bound)
        except OSError as error:
            bound.close()
            raise _LineageError("managed storage lineage changed") from error
        return bound
    except FileNotFoundError:
        raise
    except _LineageError:
        raise
    except OSError as error:
        raise _LineageError("unsafe managed storage lineage") from error


def _revalidate_bound_shard(root: Path, bound: _BoundShard) -> None:
    current: tuple[_BoundDirectory, ...] = ()
    child: _BoundDirectory | None = None
    try:
        current = _open_absolute_directory_chain(root)
        if len(current) != len(bound.root_chain) or any(
            actual.identity.object_identity != expected.identity.object_identity
            for actual, expected in zip(current, bound.root_chain, strict=True)
        ):
            raise _LineageError("managed storage lineage changed")
        child = _open_child_directory(current[-1], bound.shard_name, create=False)
        if child.identity.object_identity != bound.shard.identity.object_identity:
            raise _LineageError("managed storage lineage changed")
    except _LineageError:
        raise
    except OSError as error:
        raise _LineageError("managed storage lineage changed") from error
    finally:
        if child is not None:
            child.close()
        for directory in reversed(current):
            directory.close()


def _open_child_file(
    parent: _BoundDirectory,
    name: str,
    *,
    writable: bool = False,
    create: bool = False,
    prevent_mutation: bool = True,
) -> int:
    if os.name == "nt":
        handle = _win_open_relative(
            parent,
            name,
            directory=False,
            writable=writable,
            allow_delete=create,
            disposition=_FILE_CREATE if create else _FILE_OPEN,
            prevent_mutation=prevent_mutation and not create,
        )
        flags = os.O_RDWR if writable else os.O_RDONLY
        try:
            return msvcrt.open_osfhandle(handle, flags | getattr(os, "O_BINARY", 0))
        except Exception:  # pragma: no cover - native descriptor conversion failure
            _win_close(handle)
            raise
    flags = os.O_RDWR if writable else os.O_RDONLY  # pragma: no cover - exercised on POSIX hosts
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    return os.open(name, flags, 0o600, dir_fd=parent.handle)


def _hash_descriptor(descriptor: int) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, _READ_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _unlink_temporary(descriptor: int, parent: _BoundDirectory, name: str) -> None:
    if os.name == "nt":
        _win_mark_delete(msvcrt.get_osfhandle(descriptor))
    else:  # pragma: no cover - exercised on POSIX hosts
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent.handle, follow_symlinks=False)
        if _object_identity(info) == _object_identity(named):
            os.unlink(name, dir_fd=parent.handle)


def _publish_link(descriptor: int, parent: _BoundDirectory, temporary_name: str, target_name: str) -> None:
    if os.name == "nt":
        _win_link_handle(msvcrt.get_osfhandle(descriptor), parent, target_name)
    else:  # pragma: no cover - exercised on POSIX hosts
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=parent.handle,
            dst_dir_fd=parent.handle,
            follow_symlinks=False,
        )


def _fsync_directory(directory: _BoundDirectory) -> None:
    if os.name == "nt":
        path = _win_final_path(directory.handle)
        handle = _KERNEL32.CreateFileW(
            path,
            0x80000000 | 0x40000000,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            3,
            0x02000000 | _FILE_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:  # pragma: no cover - native API failure
            _win_raise_last_error()
        try:
            if _win_file_identity(int(handle)).object_identity != directory.identity.object_identity:
                raise _LineageError("managed storage lineage changed")
            if not _KERNEL32.FlushFileBuffers(handle):  # pragma: no cover - native API failure
                _win_raise_last_error()
        finally:
            _win_close(int(handle))
    else:  # pragma: no cover - exercised on POSIX hosts
        os.fsync(directory.handle)


def _require_ordinary_single_link(info: os.stat_result, target: Path) -> _FileIdentity:
    identity = _file_identity(info)
    if identity.object_identity.reparse_attributes or identity.object_identity.file_type != stat.S_IFREG:
        raise ContentCollisionError(f"managed content path is not a regular file: {target}")
    if identity.link_count != 1:
        raise ContentCollisionError(f"managed content must be an ordinary single-link file: {target}")
    return identity


def _settle_concurrent_publication(root: Path, bound: _BoundShard, target: Path) -> None:
    """Wait a fixed bound for a winner to remove its publication temporary link."""
    for attempt in range(_PUBLICATION_SETTLE_ATTEMPTS):
        descriptor = -1
        try:
            descriptor = _open_child_file(
                bound.shard,
                target.name,
                prevent_mutation=False,
            )
            identity = _file_identity(os.fstat(descriptor))
            if identity.object_identity.reparse_attributes or identity.object_identity.file_type != stat.S_IFREG:
                raise ContentCollisionError(f"managed content path is not a regular file: {target}")
            if identity.link_count == 1:
                _revalidate_bound_shard(root, bound)
                return
        except ContentStorageError:
            raise
        except OSError as error:
            raise ContentStorageError("managed content identity changed during publication settlement") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        _race_hook("storage_multilink_settle")
        if attempt + 1 < _PUBLICATION_SETTLE_ATTEMPTS:
            time.sleep(_PUBLICATION_SETTLE_DELAY_SECONDS)
    raise ContentCollisionError(f"managed content must be an ordinary single-link file: {target}")


def _verify_open_target(
    root: Path,
    bound: _BoundShard,
    target: Path,
    expected_sha256: str,
    *,
    created: bool,
) -> StoredContent:
    descriptor = -1
    fresh_descriptor = -1
    try:
        descriptor = _open_child_file(bound.shard, target.name)
        opened = os.fstat(descriptor)
        identity = _require_ordinary_single_link(opened, target)
        if os.name == "nt" and _win_final_path(msvcrt.get_osfhandle(descriptor)).casefold() != _win_expected_path(
            target
        ).casefold():
            raise ContentStorageError("managed content escaped its bound storage target")
        _race_hook("storage_target_opened")
        actual_sha256, size = _hash_descriptor(descriptor)
        _race_hook("storage_target_hashed")
        if _file_identity(os.fstat(descriptor)) != identity:
            raise ContentStorageError("managed content identity changed during verification")
        fresh_descriptor = _open_child_file(bound.shard, target.name)
        fresh = os.fstat(fresh_descriptor)
        fresh_identity = _require_ordinary_single_link(fresh, target)
        if fresh_identity != identity:
            raise ContentStorageError("managed content identity changed during verification")
        _revalidate_bound_shard(root, bound)
        if actual_sha256 != expected_sha256:
            raise ContentCollisionError(
                f"managed content at '{target}' does not match its SHA-256 identity {expected_sha256}"
            )
        return StoredContent(expected_sha256, target, size, created)
    except ContentStorageError:
        raise
    except _LineageError as error:
        raise ContentStorageError("managed storage lineage changed") from error
    except FileNotFoundError as error:
        raise ContentStorageError(f"managed content does not exist: {target}") from error
    except OSError as error:
        raise ContentStorageError("managed content identity changed during verification") from error
    finally:
        if fresh_descriptor >= 0:
            os.close(fresh_descriptor)
        if descriptor >= 0:
            os.close(descriptor)


# TheSuperHackers @feature Leex 22/08/2026 Bind managed CAS publication and reads to ordinary opened lineage. (#TBD)
@dataclass(frozen=True)
class ContentAddressedStore:
    """Store immutable files below a two-character SHA-256 shard."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _absolute_without_following(Path(self.root)))

    def store_bytes(self, data: bytes, *, expected_sha256: str | None = None) -> StoredContent:
        """Atomically store exact in-memory bytes and return their lowercase identity."""
        if not isinstance(data, bytes):
            raise ContentStorageError("managed in-memory content must be bytes")
        actual_sha256 = hashlib.sha256(data).hexdigest()
        target_sha256 = _normalize_sha256(expected_sha256) if expected_sha256 is not None else actual_sha256
        return self._publish((data,), target_sha256)

    def store_file(self, source: Path, *, expected_sha256: str | None = None) -> StoredContent:
        """Atomically copy an ordinary caller file without mutating or removing it."""
        source_path = Path(source)
        self._require_regular_source(source_path)
        try:
            if expected_sha256 is None:
                target_sha256, _size = _hash_chunks(_file_chunks(source_path))
            else:
                target_sha256 = _normalize_sha256(expected_sha256)
            return self._publish(_file_chunks(source_path), target_sha256)
        except ContentStorageError:
            raise
        except OSError as error:
            raise ContentStorageError(f"cannot read source file '{source_path}': {error}") from error

    def verify(self, sha256: str) -> StoredContent:
        """Revalidate an existing object against the identity encoded in its managed path."""
        normalized = _normalize_sha256(sha256)
        target = self._target(normalized)
        bound: _BoundShard | None = None
        try:
            bound = _bind_shard(self.root, normalized[:2], create=False)
            return _verify_open_target(self.root, bound, target, normalized, created=False)
        except FileNotFoundError as error:
            raise ContentStorageError(f"managed content does not exist: {target}") from error
        except _LineageError as error:
            raise ContentStorageError(str(error)) from error
        finally:
            if bound is not None:
                bound.close()

    def _target(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    @staticmethod
    def _require_regular_source(source: Path) -> None:
        try:
            info = source.lstat()
        except OSError as error:
            raise ContentStorageError(f"source file cannot be inspected: {source}: {error}") from error
        if not stat.S_ISREG(info.st_mode) or source.is_symlink():
            raise ContentStorageError(f"source must be a regular file: {source}")

    def _publish(self, chunks: Iterable[bytes], expected_sha256: str) -> StoredContent:
        target = self._target(expected_sha256)
        bound: _BoundShard | None = None
        descriptor = -1
        temporary_name: str | None = None
        temporary_owned = False
        try:
            try:
                bound = _bind_shard(self.root, expected_sha256[:2], create=True)
            except _LineageError as error:
                message = str(error)
                if message == "unsafe managed storage lineage":
                    raise ContentStorageError(message) from error
                raise ContentStorageError("managed storage lineage changed") from error
            temporary_name = f".{expected_sha256}.{secrets.token_hex(12)}.tmp"
            descriptor = _open_child_file(bound.shard, temporary_name, writable=True, create=True)
            temporary_owned = True
            digest = hashlib.sha256()
            size = 0
            for chunk in chunks:
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("managed content write made no progress")
                    view = view[written:]
                digest.update(chunk)
                size += len(chunk)
            os.fsync(descriptor)
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise HashMismatchError(
                    f"expected SHA-256 {expected_sha256} but copied bytes have SHA-256 {actual_sha256}"
                )
            _revalidate_bound_shard(self.root, bound)
            try:
                _publish_link(descriptor, bound.shard, temporary_name, target.name)
                created = True
            except FileExistsError:
                created = False
            except OSError as error:
                raise ContentStorageError(f"cannot publish managed content '{target}': {error}") from error
            _unlink_temporary(descriptor, bound.shard, temporary_name)
            temporary_owned = False
            os.close(descriptor)
            descriptor = -1
            if created:
                _fsync_directory(bound.shard)
            else:
                _settle_concurrent_publication(self.root, bound, target)
            return _verify_open_target(self.root, bound, target, expected_sha256, created=created)
        except ContentStorageError:
            raise
        except _LineageError as error:
            raise ContentStorageError("managed storage lineage changed") from error
        except OSError as error:
            raise ContentStorageError(f"cannot write managed content '{target}': {error}") from error
        finally:
            if descriptor >= 0:
                if temporary_owned and bound is not None and temporary_name is not None:
                    try:
                        _unlink_temporary(descriptor, bound.shard, temporary_name)
                    except OSError:
                        pass
                os.close(descriptor)
            if bound is not None:
                bound.close()
