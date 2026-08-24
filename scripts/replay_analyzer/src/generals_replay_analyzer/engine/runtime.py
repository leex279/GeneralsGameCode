"""Fail-closed binding of an analyzer executable into an installed game runtime."""

from __future__ import annotations

import ctypes
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from generals_replay_analyzer.engine.config import (
    EngineRunConfigurationError,
    require_no_reparse_components,
    require_plain_directory_input,
    require_regular_input,
)


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


@dataclass(frozen=True)
class _WindowsFileIdentity:
    volume_serial: int
    file_index: int
    attributes: int
    size: int


_FILE_READ_DATA = 0x0001
_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_DELETE_ACCESS = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ACCESS_DENIED = 5
_DELETE_RETRY_COUNT = 50
_DELETE_RETRY_SECONDS = 0.1

_KERNEL32: Any = None
if os.name == "nt":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    _KERNEL32.CreateHardLinkW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID]
    _KERNEL32.CreateHardLinkW.restype = wintypes.BOOL
    _KERNEL32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    _KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
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
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _ordinary_file(path: Path, label: str) -> Path:
    try:
        inspected = require_regular_input(path, label)
        info = inspected.lstat()
    except (EngineRunConfigurationError, OSError) as error:
        raise EngineRunConfigurationError(f"{label} is not an ordinary non-reparse file") from error
    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        raise EngineRunConfigurationError(f"{label} is not an ordinary non-reparse file")
    return inspected


@dataclass(frozen=True)
class RuntimeExecutableBinding:
    """Launch identity, deliberately separate from configured executable provenance."""

    configured_executable: Path
    launch_executable: Path
    staged: bool


def _win_error(operation: str) -> OSError:
    return OSError(ctypes.get_last_error(), f"secure Windows runtime binding {operation} failed")


def _win_open(path: Path, *, access: int, share: int, directory: bool) -> int:
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | (_FILE_FLAG_BACKUP_SEMANTICS if directory else 0)
    handle = _KERNEL32.CreateFileW(str(path), access, share, None, _OPEN_EXISTING, flags, None)
    if handle in (None, _INVALID_HANDLE_VALUE):
        raise _win_error(f"open of {path.name}")
    return int(handle)


def _win_close(handle: int) -> None:
    if not _KERNEL32.CloseHandle(handle):  # pragma: no cover - native API failure
        raise _win_error("handle close")


def _win_identity(handle: int) -> _WindowsFileIdentity:
    information = _ByHandleFileInformation()
    if not _KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise _win_error("identity query")
    return _WindowsFileIdentity(
        volume_serial=int(information.dwVolumeSerialNumber),
        file_index=int((information.nFileIndexHigh << 32) | information.nFileIndexLow),
        attributes=int(information.dwFileAttributes),
        size=int((information.nFileSizeHigh << 32) | information.nFileSizeLow),
    )


def _win_final_path(handle: int) -> str:
    path = ctypes.create_unicode_buffer(32768)
    length = int(_KERNEL32.GetFinalPathNameByHandleW(handle, path, len(path), 0))
    if length == 0 or length >= len(path):
        raise _win_error("final-path query")
    return path.value


def _win_expected_path(path: Path) -> str:
    raw = str(path)
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _win_require_exact_path(handle: int, path: Path, label: str) -> None:
    if os.path.normcase(_win_final_path(handle)) != os.path.normcase(_win_expected_path(path)):
        raise EngineRunConfigurationError(f"{label} handle resolved to an unexpected path")


def _win_require_file(identity: _WindowsFileIdentity, label: str) -> None:
    if identity.attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT):
        raise EngineRunConfigurationError(f"{label} handle is not an ordinary non-reparse file")


def _win_require_directory(identity: _WindowsFileIdentity, label: str) -> None:
    if not identity.attributes & _FILE_ATTRIBUTE_DIRECTORY or identity.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise EngineRunConfigurationError(f"{label} handle is not an ordinary non-reparse directory")


def _win_mark_delete(handle: int) -> None:
    disposition = _FileDispositionInfo(True)
    if not _KERNEL32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise _win_error("owned-link deletion")


def _win_cleanup_staged_link(
    destination: Path,
    expected: _WindowsFileIdentity,
    staged_handle: int | None,
) -> None:
    """Delete an owned link only after reopening and revalidating its identity."""
    handle = staged_handle
    try:
        if handle is None:
            handle = _win_open_staged_lock(destination, expected)
        # TheSuperHackers @bugfix Leex 24/08/2026 Let a settled child release its executable image mapping before exact-handle deletion. (#TBD)
        for attempt in range(_DELETE_RETRY_COUNT):
            try:
                _win_mark_delete(handle)
                break
            except OSError as error:
                if error.errno != _ACCESS_DENIED or attempt + 1 == _DELETE_RETRY_COUNT:
                    raise
                time.sleep(_DELETE_RETRY_SECONDS)
    finally:
        if handle is not None:
            _win_close(handle)


def _win_same_object(left: _WindowsFileIdentity, right: _WindowsFileIdentity) -> bool:
    return left.volume_serial == right.volume_serial and left.file_index == right.file_index


def _win_require_same_volume(source: _WindowsFileIdentity, runtime: _WindowsFileIdentity) -> None:
    if source.volume_serial != runtime.volume_serial:
        raise EngineRunConfigurationError(
            "engine executable and engine runtime directory are on different volumes; safe hardlink staging is unavailable"
        )


def _win_open_verified_source(path: Path, *, staged: bool) -> tuple[int, _WindowsFileIdentity]:
    share = _FILE_SHARE_READ | (_FILE_SHARE_DELETE if staged else 0)
    access = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES
    handle = _win_open(path, access=access, share=share, directory=False)
    try:
        identity = _win_identity(handle)
        _win_require_file(identity, "configured engine executable")
        _win_require_exact_path(handle, path, "configured engine executable")
        return handle, identity
    except BaseException:
        _win_close(handle)
        raise


def _win_open_verified_runtime(path: Path) -> tuple[int, _WindowsFileIdentity]:
    handle = _win_open(
        path,
        access=_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
        directory=True,
    )
    try:
        identity = _win_identity(handle)
        _win_require_directory(identity, "engine runtime directory")
        _win_require_exact_path(handle, path, "engine runtime directory")
        return handle, identity
    except BaseException:
        _win_close(handle)
        raise


def _win_open_staged_lock(path: Path, expected: _WindowsFileIdentity) -> int:
    handle = _win_open(
        path,
        access=_FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _DELETE_ACCESS,
        share=_FILE_SHARE_READ,
        directory=False,
    )
    try:
        identity = _win_identity(handle)
        _win_require_file(identity, "staged engine executable")
        _win_require_exact_path(handle, path, "staged engine executable")
        if not _win_same_object(identity, expected):
            raise EngineRunConfigurationError("runtime executable binding did not retain configured executable identity")
        return handle
    except BaseException:
        _win_close(handle)
        raise


def _win_revalidate_source(path: Path, expected: _WindowsFileIdentity) -> None:
    handle = _win_open(
        path,
        access=_FILE_READ_ATTRIBUTES,
        share=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        directory=False,
    )
    try:
        identity = _win_identity(handle)
        _win_require_file(identity, "configured engine executable")
        _win_require_exact_path(handle, path, "configured engine executable")
        if not _win_same_object(identity, expected):
            raise EngineRunConfigurationError("configured engine executable changed while creating runtime binding")
    finally:
        _win_close(handle)


def _win_create_hardlink(source: Path, destination: Path) -> None:
    if _KERNEL32.CreateHardLinkW(str(destination), str(source), None):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise EngineRunConfigurationError("unique runtime executable binding unexpectedly already exists")
    raise EngineRunConfigurationError(
        f"could not create exclusive runtime executable binding: {OSError(error, os.strerror(error))}"
    )


@contextmanager
def _bind_windows(source: Path, runtime: Path) -> Iterator[RuntimeExecutableBinding]:
    staged = source.parent != runtime
    source_handle: int | None = None
    runtime_handle: int | None = None
    staged_handle: int | None = None
    owns_staged_link = False
    destination: Path | None = None
    primary_error: BaseException | None = None
    try:
        runtime_handle, runtime_identity = _win_open_verified_runtime(runtime)
        source_handle, source_identity = _win_open_verified_source(source, staged=staged)
        _win_require_same_volume(source_identity, runtime_identity)
        if not staged:
            yield RuntimeExecutableBinding(source, source, False)
            return

        destination = runtime / f"generalszh_replay_analyzer_{uuid4()}.exe"
        _win_create_hardlink(source, destination)
        # Ownership begins at the successful CreateHardLinkW boundary.  If the
        # first protected open fails, cleanup must still reopen and verify this
        # exact object before marking it for deletion.
        owns_staged_link = True
        staged_handle = _win_open_staged_lock(destination, source_identity)
        _win_revalidate_source(source, source_identity)
        protected_source_handle, protected_source_identity = _win_open_verified_source(source, staged=False)
        if not _win_same_object(protected_source_identity, source_identity):
            _win_close(protected_source_handle)
            raise EngineRunConfigurationError("configured engine executable changed while locking runtime binding")
        _win_close(source_handle)
        source_handle = protected_source_handle
        yield RuntimeExecutableBinding(source, destination, True)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: BaseException | None = None

        def cleanup(action: Any) -> None:
            nonlocal cleanup_error
            try:
                action()
            except BaseException as error:  # noqa: BLE001 - cleanup must not mask cancellation or launch failures
                if cleanup_error is None:
                    cleanup_error = error

        # TheSuperHackers @bugfix Leex 24/08/2026 Release the source share lock before marking the still handle-verified staged link for deletion. (#TBD)
        if source_handle is not None:
            cleanup(lambda: _win_close(source_handle))
            source_handle = None

        try:
            if owns_staged_link and staged_handle is not None:
                assert destination is not None
                owned_destination = destination
                owned_identity = source_identity
                cleanup(lambda: _win_cleanup_staged_link(owned_destination, owned_identity, staged_handle))
                staged_handle = None
            elif owns_staged_link and destination is not None:
                owned_destination = destination
                owned_identity = source_identity
                cleanup(lambda: _win_cleanup_staged_link(owned_destination, owned_identity, None))
        finally:
            if runtime_handle is not None:
                cleanup(lambda: _win_close(runtime_handle))

        if cleanup_error is not None:
            if primary_error is not None:
                primary_error.add_note(f"runtime binding cleanup failed: {cleanup_error!r}")
            else:
                raise cleanup_error


# TheSuperHackers @fix Leex 24/08/2026 Hold the exact runtime executable object against mutation and delete its owned link by handle. (#TBD)
@contextmanager
def bind_runtime_executable(executable: Path, runtime_directory: Path) -> Iterator[RuntimeExecutableBinding]:
    """Hold one immutable launch object through child settlement and clean up only by identity."""
    if os.name != "nt":  # pragma: no cover - production runtime binding targets Windows
        raise EngineRunConfigurationError("secure runtime executable binding is unavailable on this platform")
    source = _ordinary_file(executable, "configured engine executable")
    runtime = require_plain_directory_input(runtime_directory, "engine runtime directory")
    require_no_reparse_components(runtime, "engine runtime directory")
    with _bind_windows(source, runtime) as binding:
        yield binding
