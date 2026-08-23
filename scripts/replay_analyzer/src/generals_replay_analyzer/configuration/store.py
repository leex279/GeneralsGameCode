"""Canonical external settings persistence with optimistic revision checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Literal, Self, TypeAlias, cast

from platformdirs import PlatformDirs

SettingScalar: TypeAlias = int | str
SettingSource: TypeAlias = Literal["default", "persisted", "environment", "composition"]
MutationResultCode: TypeAlias = Literal["updated", "unchanged"]
AffectedStageFamily: TypeAlias = Literal[
    "future_imports",
    "telemetry",
    "spatial",
    "features",
    "strategy",
    "longitudinal",
    "ollama",
    "report",
]

_SCHEMA_VERSION: Final = 1
_DOCUMENT_DIRECTORY: Final = "configuration"
_DOCUMENT_NAME: Final = "settings-v1.json"
_LOCK_NAME: Final = "settings-v1.json.lock"
_ENV_PREFIX: Final = "GENERALS_REPLAY_ANALYZER_"
_SETTING_KEYS: Final = (
    "import_mode",
    "minimum_longitudinal_sample_size",
    "movement_sample_frames",
    "ollama_model",
    "ollama_url",
)
_DEFAULT_VALUES: Final[dict[str, SettingScalar]] = {
    "import_mode": "copy",
    "minimum_longitudinal_sample_size": 5,
    "movement_sample_frames": 15,
    "ollama_model": "qwen3.6:27b",
    "ollama_url": "http://127.0.0.1:11434",
}
_ENV_NAMES: Final = {key: f"{_ENV_PREFIX}{key}".upper() for key in _SETTING_KEYS}
_ENDPOINT_PATTERN: Final = re.compile(r"http://(?P<host>127\.0\.0\.1|\[::1\]):(?P<port>[0-9]{1,5})/?", re.ASCII)
_MODEL_PATTERN: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?",
    re.ASCII,
)
_WINDOWS_RESERVED_NAMES: Final = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_REPARSE_ATTRIBUTE: Final = 0x400
_MAX_DOCUMENT_BYTES: Final = 65_536
_AFFECTED_STAGE_ORDER: Final[tuple[AffectedStageFamily, ...]] = (
    "future_imports",
    "telemetry",
    "spatial",
    "features",
    "strategy",
    "longitudinal",
    "ollama",
    "report",
)
_IMPACT_FAMILIES: Final[dict[str, tuple[AffectedStageFamily, ...]]] = {
    "import_mode": ("future_imports",),
    "minimum_longitudinal_sample_size": ("strategy", "longitudinal", "ollama", "report"),
    "movement_sample_frames": (
        "telemetry",
        "spatial",
        "features",
        "strategy",
        "longitudinal",
        "ollama",
        "report",
    ),
    "ollama_model": ("ollama", "report"),
    "ollama_url": ("ollama", "report"),
}


class SettingsStoreError(RuntimeError):
    """Stable public configuration failure with no private exception details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SecureDocumentMissing(Exception):
    """The document or an ordinary ancestor is absent without traversing an alias."""


@dataclass(frozen=True, slots=True)
class SettingChange:
    """One untrusted scalar candidate; the store performs closed validation."""

    key: str
    value: object


@dataclass(frozen=True, slots=True)
class EffectiveSettingsSnapshot:
    """Immutable safe-scalar settings identity for application adapters."""

    schema_version: Literal[1]
    revision: int
    effective_settings_digest: str
    values: tuple[tuple[str, SettingScalar], ...]
    sources: tuple[tuple[str, SettingSource], ...]

    def value(self, key: str) -> SettingScalar:
        """Return one closed value without publishing a mutable mapping."""
        for candidate, value in self.values:
            if candidate == key:
                return value
        raise KeyError(key)

    def source(self, key: str) -> SettingSource:
        """Return one internal source label without environment metadata."""
        for candidate, source in self.sources:
            if candidate == key:
                return source
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class SettingsMutation:
    """Result of one serialized semantic settings command."""

    result_code: MutationResultCode
    snapshot: EffectiveSettingsSnapshot


@dataclass(frozen=True, slots=True)
class SettingsImpact:
    """Versioned application-owned impact preview with no persistence side effect."""

    expected_revision: int
    impact_digest: str
    normalized_changes: tuple[SettingChange, ...]
    affected_stage_families: tuple[AffectedStageFamily, ...]
    invalidates_existing_results: bool
    requires_confirmation: bool
    restart_required: bool


@dataclass(frozen=True, slots=True)
class _Document:
    revision: int
    values: tuple[tuple[str, SettingScalar], ...]
    persisted: bool


def normalize_ollama_endpoint(value: object) -> str:
    """Accept and canonicalize only explicit literal IPv4/IPv6 loopback HTTP endpoints."""
    if type(value) is not str or not value.isascii() or _ENDPOINT_PATTERN.fullmatch(value) is None:
        raise SettingsStoreError("settings_change_invalid")
    match = cast(re.Match[str], _ENDPOINT_PATTERN.fullmatch(value))
    port_text = match.group("port")
    if len(port_text) > 1 and port_text.startswith("0"):
        raise SettingsStoreError("settings_change_invalid")
    port = int(port_text)
    if not 1 <= port <= 65_535:
        raise SettingsStoreError("settings_change_invalid")
    return f"http://{match.group('host')}:{port}"


def validate_ollama_model_name(value: object) -> str:
    """Accept only the closed ASCII model identifier grammar."""
    if (
        type(value) is not str
        or not 1 <= len(value) <= 255
        or not value.isascii()
        or _MODEL_PATTERN.fullmatch(value) is None
    ):
        raise SettingsStoreError("settings_change_invalid")
    return value


def _validated_scalar(key: str, value: object, *, error_code: str) -> SettingScalar:
    try:
        if key == "import_mode":
            if type(value) is not str or value not in {"copy", "reference"}:
                raise SettingsStoreError(error_code)
            return value
        if key == "minimum_longitudinal_sample_size":
            if type(value) is not int or not 1 <= value <= 100_000:
                raise SettingsStoreError(error_code)
            return value
        if key == "movement_sample_frames":
            if type(value) is not int or not 1 <= value <= 3600:
                raise SettingsStoreError(error_code)
            return value
        if key == "ollama_url":
            return normalize_ollama_endpoint(value)
        if key == "ollama_model":
            return validate_ollama_model_name(value)
    except SettingsStoreError:
        raise SettingsStoreError(error_code) from None
    raise SettingsStoreError(error_code)


def _canonical_document_bytes(revision: int, values: Mapping[str, SettingScalar]) -> bytes:
    payload = {
        "revision": revision,
        "schema_version": _SCHEMA_VERSION,
        "values": {key: values[key] for key in _SETTING_KEYS},
    }
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _decode_document(data: bytes) -> _Document:
    try:
        decoded = data.decode("utf-8", errors="strict")
        payload = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_invalid_constant,
        )
        if type(payload) is not dict or set(payload) != {"revision", "schema_version", "values"}:
            raise ValueError("invalid document object")
        if payload["schema_version"] != _SCHEMA_VERSION or type(payload["schema_version"]) is not int:
            raise ValueError("unsupported schema")
        revision = payload["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid revision")
        raw_values = payload["values"]
        if type(raw_values) is not dict or set(raw_values) != set(_SETTING_KEYS):
            raise ValueError("invalid value keys")
        values = {key: _validated_scalar(key, raw_values[key], error_code="settings_document_invalid") for key in _SETTING_KEYS}
        if data != _canonical_document_bytes(revision, values):
            raise ValueError("noncanonical document")
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError, SettingsStoreError):
        raise SettingsStoreError("settings_document_invalid") from None
    return _Document(
        revision=revision,
        values=tuple((key, values[key]) for key in _SETTING_KEYS),
        persisted=True,
    )


def _is_reparse(stat_result: os.stat_result) -> bool:
    return bool(getattr(stat_result, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def _plain_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not path.is_symlink() and not _is_reparse(metadata)


def _has_unsafe_windows_name(path: Path) -> bool:
    for part in path.parts[1:]:
        if not part or part.endswith((" ", ".")) or ":" in part:
            return True
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            return True
    return False


def _inside_git_checkout(path: Path) -> bool:
    return any((candidate / ".git").is_file() or (candidate / ".git").is_dir() for candidate in (path, *path.parents))


def _bounded_descriptor_read(descriptor: int) -> bytes:
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        data = stream.read(_MAX_DOCUMENT_BYTES + 1)
    if len(data) > _MAX_DOCUMENT_BYTES:
        raise SettingsStoreError("settings_document_invalid")
    return data


def _secure_posix_read(path: Path) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    parts = path.parts
    if not path.is_absolute() or not parts:
        raise SettingsStoreError("settings_document_invalid")
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = os.open(path.anchor, directory_flags)
        for component in parts[1:-1]:
            try:
                next_descriptor = os.open(component, directory_flags, dir_fd=directory_descriptor)
            except FileNotFoundError:
                raise _SecureDocumentMissing from None
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_descriptor)
                raise SettingsStoreError("settings_document_invalid")
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        try:
            file_descriptor = os.open(parts[-1], file_flags, dir_fd=directory_descriptor)
        except FileNotFoundError:
            raise _SecureDocumentMissing from None
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SettingsStoreError("settings_document_invalid")
        owned_descriptor = file_descriptor
        file_descriptor = None
        return _bounded_descriptor_read(owned_descriptor)
    except _SecureDocumentMissing:
        raise
    except SettingsStoreError:
        raise
    except OSError:
        raise SettingsStoreError("settings_document_invalid") from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _secure_windows_read(path: Path) -> bytes:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
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
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    file_information = kernel32.GetFileInformationByHandle
    file_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
    file_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    generic_read = 0x80000000
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    open_reparse_point = 0x00200000
    backup_semantics = 0x02000000
    directory_attribute = 0x10
    reparse_attribute = 0x400
    invalid_handle = wintypes.HANDLE(-1).value
    missing_errors = {2, 3}
    held_handles: list[int] = []
    final_handle: int | None = None

    def open_handle(candidate: Path, access: int, flags: int) -> int:
        handle = create_file(
            str(candidate),
            access,
            share_read_write,
            None,
            open_existing,
            flags | open_reparse_point,
            None,
        )
        if handle == invalid_handle:
            error_code = ctypes.get_last_error()
            if error_code in missing_errors:
                raise _SecureDocumentMissing
            raise SettingsStoreError("settings_document_invalid")
        return int(handle)

    def attributes(handle: int) -> int:
        information = _ByHandleFileInformation()
        if not file_information(handle, ctypes.byref(information)):
            raise SettingsStoreError("settings_document_invalid")
        return int(information.dwFileAttributes)

    try:
        for directory in reversed((path.parent, *path.parent.parents)):
            handle = open_handle(directory, file_read_attributes, backup_semantics)
            held_handles.append(handle)
            flags = attributes(handle)
            if flags & reparse_attribute or not flags & directory_attribute:
                raise SettingsStoreError("settings_document_invalid")
        final_handle = open_handle(path, generic_read, 0)
        flags = attributes(final_handle)
        if flags & reparse_attribute or flags & directory_attribute:
            raise SettingsStoreError("settings_document_invalid")
        descriptor = msvcrt.open_osfhandle(final_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        final_handle = None
        return _bounded_descriptor_read(descriptor)
    except _SecureDocumentMissing:
        raise
    except SettingsStoreError:
        raise
    except (OSError, ValueError):
        raise SettingsStoreError("settings_document_invalid") from None
    finally:
        if final_handle is not None:
            close_handle(final_handle)
        for handle in reversed(held_handles):
            close_handle(handle)


# TheSuperHackers @fix Leex 23/08/2026 Read settings through held non-alias ancestors and one verified file descriptor. (#TBD)
def _secure_document_read(path: Path) -> bytes:
    if os.name == "nt":
        return _secure_windows_read(path)
    return _secure_posix_read(path)


class _ExclusiveFileLock:
    """Small application-owned lock based on exclusive creation of one known file."""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._fd: int | None = None

    def __enter__(self) -> Self:
        deadline = self._monotonic() + self._timeout_seconds
        while True:
            try:
                self._fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                return self
            except FileExistsError:
                if self._monotonic() >= deadline:
                    raise SettingsStoreError("settings_busy") from None
                self._sleep(min(0.01, max(0.0, deadline - self._monotonic())))
            except OSError:
                raise SettingsStoreError("settings_location_unsafe") from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


# TheSuperHackers @feature Leex 23/08/2026 Persist safe analyzer settings as one atomic revisioned external document. (#TBD)
class ConfigurationStore:
    """Load and update the one application-owned settings document."""

    def __init__(
        self,
        *,
        configuration_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
        composition_overrides: Mapping[str, object] | None = None,
        version_identities: Sequence[tuple[str, str]] = (),
        lock_timeout_seconds: float = 0.5,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if configuration_root is None:
            configuration_root = Path(
                PlatformDirs("GeneralsReplayAnalyzer", appauthor=False, roaming=False).user_config_path
            )
        self._configuration_root = configuration_root.expanduser().absolute()
        self._document_path = self._configuration_root / _DOCUMENT_DIRECTORY / _DOCUMENT_NAME
        self._lock_path = self._document_path.with_name(_LOCK_NAME)
        self._environment = dict(os.environ if environment is None else environment)
        self._composition_overrides = self._normalize_overrides(composition_overrides or {})
        self._version_identities = tuple(sorted(version_identities))
        self._lock_timeout_seconds = lock_timeout_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    @property
    def document_path(self) -> Path:
        """Return the private infrastructure locator for composition and tests only."""
        return self._document_path

    @property
    def lock_path(self) -> Path:
        """Return the exact application lock locator for composition and tests only."""
        return self._lock_path

    def read(self) -> EffectiveSettingsSnapshot:
        """Read immutable effective state without creating files or directories."""
        document = self._read_document()
        return self._effective_snapshot(document)

    def preview(self, *, expected_revision: int, changes: Sequence[SettingChange]) -> SettingsImpact:
        """Validate one immutable impact against the current revision without writing."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise SettingsStoreError("settings_change_invalid")
        normalized_changes = self._normalize_changes(changes)
        document = self._read_document()
        if document.revision != expected_revision:
            raise SettingsStoreError("settings_revision_conflict")
        self._reject_overridden_changes(normalized_changes)
        return self._impact(expected_revision, normalized_changes)

    def apply(self, *, expected_revision: int, changes: Sequence[SettingChange]) -> SettingsMutation:
        """Apply a trusted local command through the same confirmed impact path."""
        impact = self.preview(expected_revision=expected_revision, changes=changes)
        return self.apply_confirmed(
            expected_revision=expected_revision,
            changes=changes,
            expected_impact_digest=impact.impact_digest,
            confirm_invalidating_change=True,
        )

    def apply_confirmed(
        self,
        *,
        expected_revision: int,
        changes: Sequence[SettingChange],
        expected_impact_digest: str,
        confirm_invalidating_change: bool,
    ) -> SettingsMutation:
        """Recompute an exact confirmed impact under the write lock before replacement."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise SettingsStoreError("settings_change_invalid")
        if confirm_invalidating_change is not True:
            raise SettingsStoreError("settings_confirmation_required")
        if re.fullmatch(r"[0-9a-f]{64}", expected_impact_digest, re.ASCII) is None:
            raise SettingsStoreError("settings_impact_conflict")
        normalized_changes = self._normalize_changes(changes)
        self._prepare_write_directory()
        with _ExclusiveFileLock(
            self._lock_path,
            timeout_seconds=self._lock_timeout_seconds,
            monotonic=self._monotonic,
            sleep=self._sleep,
        ):
            self._validate_existing_directory_chain()
            document = self._read_document()
            if document.revision != expected_revision:
                raise SettingsStoreError("settings_revision_conflict")
            self._reject_overridden_changes(normalized_changes)
            impact = self._impact(expected_revision, normalized_changes)
            if not secrets.compare_digest(impact.impact_digest, expected_impact_digest):
                raise SettingsStoreError("settings_impact_conflict")
            persisted = dict(document.values)
            updated = dict(persisted)
            updated.update(normalized_changes)
            if updated == persisted:
                return SettingsMutation("unchanged", self._effective_snapshot(document))
            next_document = _Document(
                revision=document.revision + 1,
                values=tuple((key, updated[key]) for key in _SETTING_KEYS),
                persisted=True,
            )
            self._atomic_write(next_document)
            return SettingsMutation("updated", self._effective_snapshot(next_document))

    def _reject_overridden_changes(self, changes: tuple[tuple[str, SettingScalar], ...]) -> None:
        overridden_environment_keys = self._environment_keys()
        if any(key in overridden_environment_keys for key, _value in changes):
            raise SettingsStoreError("settings_overridden_by_environment")
        if any(key in self._composition_overrides for key, _value in changes):
            raise SettingsStoreError("settings_overridden_by_composition")

    def _impact(self, expected_revision: int, changes: tuple[tuple[str, SettingScalar], ...]) -> SettingsImpact:
        family_set = {family for key, _value in changes for family in _IMPACT_FAMILIES[key]}
        families = tuple(family for family in _AFFECTED_STAGE_ORDER if family in family_set)
        normalized = tuple(SettingChange(key, value) for key, value in changes)
        digest_payload = {
            "expected_revision": expected_revision,
            "normalized_changes": changes,
            "policy_version": "settings-impact-v1",
            "affected_stage_families": families,
            "invalidates_existing_results": any(key != "import_mode" for key, _value in changes),
            "requires_confirmation": True,
            "restart_required": True,
        }
        impact_digest = hashlib.sha256(
            json.dumps(digest_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return SettingsImpact(
            expected_revision=expected_revision,
            impact_digest=impact_digest,
            normalized_changes=normalized,
            affected_stage_families=families,
            invalidates_existing_results=digest_payload["invalidates_existing_results"] is True,
            requires_confirmation=True,
            restart_required=True,
        )

    def _normalize_overrides(self, values: Mapping[str, object]) -> dict[str, SettingScalar]:
        if any(key not in _SETTING_KEYS for key in values):
            raise SettingsStoreError("settings_change_invalid")
        return {key: _validated_scalar(key, value, error_code="settings_change_invalid") for key, value in values.items()}

    def _normalize_changes(self, changes: Sequence[SettingChange]) -> tuple[tuple[str, SettingScalar], ...]:
        normalized: dict[str, SettingScalar] = {}
        if not changes:
            raise SettingsStoreError("settings_change_invalid")
        for change in changes:
            if change.key in normalized:
                raise SettingsStoreError("settings_change_invalid")
            normalized[change.key] = _validated_scalar(
                change.key,
                change.value,
                error_code="settings_change_invalid",
            )
        return tuple((key, normalized[key]) for key in sorted(normalized))

    def _environment_keys(self) -> frozenset[str]:
        return frozenset(key for key, name in _ENV_NAMES.items() if self._environment.get(name, "") != "")

    def _environment_value(self, key: str) -> SettingScalar | None:
        name = _ENV_NAMES[key]
        raw = self._environment.get(name)
        if raw is None or raw == "":
            return None
        candidate: object = raw
        if key in {"movement_sample_frames", "minimum_longitudinal_sample_size"}:
            if not raw.isascii() or not raw.isdecimal():
                raise SettingsStoreError("settings_environment_invalid")
            candidate = int(raw)
        try:
            return _validated_scalar(key, candidate, error_code="settings_environment_invalid")
        except SettingsStoreError:
            raise SettingsStoreError("settings_environment_invalid") from None

    def _read_document(self) -> _Document:
        try:
            data = _secure_document_read(self._document_path)
        except _SecureDocumentMissing:
            return _Document(
                0,
                tuple((key, _DEFAULT_VALUES[key]) for key in _SETTING_KEYS),
                False,
            )
        except SettingsStoreError:
            raise
        except OSError:
            raise SettingsStoreError("settings_document_invalid") from None
        return _decode_document(data)

    def _effective_snapshot(self, document: _Document) -> EffectiveSettingsSnapshot:
        values = dict(document.values)
        sources: dict[str, SettingSource] = {
            key: "persisted" if document.persisted else "default" for key in _SETTING_KEYS
        }
        for key in _SETTING_KEYS:
            environment_value = self._environment_value(key)
            if environment_value is not None:
                values[key] = environment_value
                sources[key] = "environment"
        for key, value in self._composition_overrides.items():
            values[key] = value
            sources[key] = "composition"
        canonical_values = tuple((key, values[key]) for key in _SETTING_KEYS)
        canonical_sources = tuple((key, sources[key]) for key in _SETTING_KEYS)
        digest_payload = {
            "schema_version": _SCHEMA_VERSION,
            "values": tuple((key, values[key], sources[key]) for key in _SETTING_KEYS),
            "versions": self._version_identities,
        }
        digest = hashlib.sha256(
            json.dumps(digest_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return EffectiveSettingsSnapshot(
            schema_version=1,
            revision=document.revision,
            effective_settings_digest=digest,
            values=canonical_values,
            sources=canonical_sources,
        )

    def _prepare_write_directory(self) -> None:
        if _has_unsafe_windows_name(self._configuration_root) or _inside_git_checkout(self._configuration_root):
            raise SettingsStoreError("settings_location_unsafe")
        for candidate in reversed((self._document_path.parent, *self._document_path.parent.parents)):
            if candidate.exists() and not _plain_directory(candidate):
                raise SettingsStoreError("settings_location_unsafe")
        try:
            self._document_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SettingsStoreError("settings_location_unsafe") from None
        self._validate_existing_directory_chain()

    def _validate_existing_directory_chain(self) -> None:
        if _inside_git_checkout(self._configuration_root):
            raise SettingsStoreError("settings_location_unsafe")
        for candidate in (self._document_path.parent, *self._document_path.parent.parents):
            if candidate.exists() and not _plain_directory(candidate):
                raise SettingsStoreError("settings_location_unsafe")

    def _atomic_write(self, document: _Document) -> None:
        values = dict(document.values)
        payload = _canonical_document_bytes(document.revision, values)
        temporary_path = self._document_path.with_name(
            f"settings-v1.{secrets.token_hex(16)}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._document_path)
            self._best_effort_directory_sync()
        except OSError:
            raise SettingsStoreError("settings_write_failed") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _best_effort_directory_sync(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(self._document_path.parent, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
