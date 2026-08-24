"""Adapter from validated engine runs to import-stage telemetry artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import AnalyzerSettings
from ..engine.config import EngineRunConfig, require_plain_directory_input
from ..engine.result import EngineRunResult, EngineRunStatus
from ..engine.runner import export_telemetry
from ..parser import parse_replay
from .service import AcquisitionDiagnostic, TelemetryArtifact

_HASH_BLOCK_BYTES = 1024 * 1024
_PARTIAL_TRACE_STATUSES = frozenset(
    {
        EngineRunStatus.VALID_CRC_MISMATCH,
        EngineRunStatus.REPLAY_TRUNCATED,
        EngineRunStatus.INTERRUPTED,
    }
)
_PATHLIKE_TEXT = re.compile(r"(?i)(?:[a-z]:[\\/][^\s]+|\\\\[^\s]+|/(?:[^\s]+))")
_SAFE_MAP_LEAF = re.compile(r"^[A-Za-z0-9 _()\[\].-]{1,120}$")
_WINDOWS_RESERVED_LEAVES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

EngineTelemetryExporter = Callable[[Path, EngineRunConfig], EngineRunResult]


def _sha256_file(path: Path) -> str:
    """Hash one input with bounded reads so replay size does not affect memory use."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _race_hook(_event: str) -> None:
    """Give tests a deterministic boundary without weakening production behavior."""


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


@dataclass(frozen=True, slots=True)
class _Identity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    links: int
    attributes: int


@dataclass(frozen=True, slots=True)
class _TreeSnapshot:
    chain: tuple[tuple[Path, _Identity], ...]
    directories: tuple[tuple[str, _Identity], ...]
    files: tuple[tuple[str, _Identity], ...]


@dataclass(slots=True)
class _OwnedTree:
    root: Path
    parent: Path
    parent_identity: _Identity
    root_identity: _Identity
    directories: dict[Path, _Identity]
    files: dict[Path, _Identity]


_MapManifest = tuple[tuple[str, ...], tuple[tuple[str, int, str], ...]]


def _identity(info: os.stat_result, *, link_count: int | None = None) -> _Identity:
    return _Identity(
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_nlink if link_count is None else link_count,
        getattr(info, "st_file_attributes", 0),
    )


def _object_matches(info: os.stat_result, expected: _Identity) -> bool:
    current = _identity(info)
    return (
        current.device,
        current.inode,
        stat.S_IFMT(current.mode),
        current.attributes,
    ) == (
        expected.device,
        expected.inode,
        stat.S_IFMT(expected.mode),
        expected.attributes,
    )


def _ordinary_directory_identity(path: Path, label: str) -> _Identity:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} must be an existing ordinary non-reparse directory") from error
    if path.is_symlink() or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be an existing ordinary non-reparse directory")
    return _identity(info)


def _ordinary_file_identity(path: Path, label: str, *, single_link: bool) -> _Identity:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} must be an ordinary non-reparse file") from error
    if path.is_symlink() or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be an ordinary non-reparse file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        link_count = _descriptor_link_count(descriptor, opened)
        opened_identity = _identity(opened, link_count=link_count)
    finally:
        os.close(descriptor)
    if not _same_identity_ignoring_links(_identity(info), opened_identity):
        raise ValueError(f"{label} changed while its identity was captured")
    if single_link and link_count != 1:
        raise ValueError(f"{label} must be a single-link ordinary file")
    return opened_identity


def _descriptor_link_count(descriptor: int, info: os.stat_result) -> int:
    del descriptor
    return info.st_nlink


def _descriptor_identity(descriptor: int) -> _Identity:
    info = os.fstat(descriptor)
    return _identity(info, link_count=_descriptor_link_count(descriptor, info))


def _same_identity_ignoring_links(left: _Identity, right: _Identity) -> bool:
    return (
        left.device,
        left.inode,
        left.mode,
        left.size,
        left.modified_ns,
        left.attributes,
    ) == (
        right.device,
        right.inode,
        right.mode,
        right.size,
        right.modified_ns,
        right.attributes,
    )


def _contained(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(parent)), os.path.normcase(str(child)))) == os.path.normcase(
            str(parent)
        )
    except ValueError:
        return False


def _capture_tree(root: Path, chain: tuple[Path, ...]) -> _TreeSnapshot:
    chain_identities = tuple((path, _ordinary_directory_identity(path, "declared replay map path")) for path in chain)
    directories: list[tuple[str, _Identity]] = []
    files: list[tuple[str, _Identity]] = []

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as error:
            raise ValueError("declared replay map source changed during capture") from error
        for entry in entries:
            candidate = Path(entry.path)
            try:
                # DirEntry.stat reports zero inode/link fields on some Windows Python builds.
                info = candidate.lstat()
            except OSError as error:
                raise ValueError("declared replay map source changed during capture") from error
            if entry.is_symlink() or _is_reparse(info):
                raise ValueError("declared replay map contains a non-regular or reparse entry")
            relative = candidate.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                directories.append((relative, _identity(info)))
                visit(candidate)
            elif stat.S_ISREG(info.st_mode):
                files.append((relative, _ordinary_file_identity(candidate, "declared replay map file", single_link=True)))
            else:
                raise ValueError("declared replay map contains a non-regular or reparse entry")

    visit(root)
    return _TreeSnapshot(chain_identities, tuple(sorted(directories)), tuple(sorted(files)))


def _revalidate_tree(root: Path, snapshot: _TreeSnapshot) -> None:
    try:
        for path, expected in snapshot.chain:
            if _identity(path.lstat()) != expected:
                raise ValueError("declared replay map source changed during staging")
        if _capture_tree(root, tuple(path for path, _expected in snapshot.chain)) != snapshot:
            raise ValueError("declared replay map source changed during staging")
    except OSError as error:
        raise ValueError("declared replay map source changed during staging") from error


def _open_captured_file(path: Path, expected: _Identity, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{label} source changed before open") from error
    try:
        opened = os.fstat(descriptor)
        opened_identity = _identity(opened, link_count=_descriptor_link_count(descriptor, opened))
        if opened_identity != expected or not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
            raise ValueError(f"{label} source changed before open")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _stream_descriptor(source_descriptor: int, destination_descriptor: int | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        block = os.read(source_descriptor, _HASH_BLOCK_BYTES)
        if not block:
            break
        digest.update(block)
        size += len(block)
        if destination_descriptor is not None:
            view = memoryview(block)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("short write while staging engine input")
                view = view[written:]
    return size, digest.hexdigest()


def _manifest_from_snapshot(root: Path, snapshot: _TreeSnapshot) -> _MapManifest:
    files: list[tuple[str, int, str]] = []
    for relative, expected in snapshot.files:
        _revalidate_tree(root, snapshot)
        _race_hook("map_before_file_open")
        descriptor = _open_captured_file(root / Path(relative), expected, "declared replay map")
        try:
            size, digest = _stream_descriptor(descriptor)
            if _descriptor_identity(descriptor) != expected or size != expected.size:
                raise ValueError("declared replay map source changed during staging")
        finally:
            os.close(descriptor)
        files.append((relative, size, digest))
    _revalidate_tree(root, snapshot)
    return tuple(relative for relative, _identity_value in snapshot.directories), tuple(files)


def _existing_manifest(root: Path) -> _MapManifest:
    snapshot = _capture_tree(root, (root,))
    return _manifest_from_snapshot(root, snapshot)


def _create_owned_tree(parent: Path, leaf: str) -> _OwnedTree:
    token = uuid.uuid4().hex
    root = parent / f".{leaf}.{token}.tmp"
    root.mkdir()
    root_identity = _ordinary_directory_identity(root, "map staging temporary")
    return _OwnedTree(
        root,
        parent,
        _ordinary_directory_identity(parent, "isolated Maps directory"),
        root_identity,
        {},
        {},
    )


def _owned_lineage_matches(owned: _OwnedTree) -> bool:
    if owned.root.parent != owned.parent or not _contained(owned.parent, owned.root):
        return False
    try:
        parent_info = owned.parent.lstat()
        if (
            not _object_matches(parent_info, owned.parent_identity)
            or _is_reparse(parent_info)
            or not stat.S_ISDIR(parent_info.st_mode)
        ):
            return False
        if not _object_matches(owned.root.lstat(), owned.root_identity) or _is_reparse(owned.root.lstat()):
            return False
        for directory, expected in owned.directories.items():
            info = directory.lstat()
            if not _object_matches(info, expected) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                return False
    except OSError:
        return False
    return True


def _cleanup_owned_tree(owned: _OwnedTree) -> None:
    """Remove only captured objects while their complete owned lineage remains bound."""
    if not _owned_lineage_matches(owned):
        return
    for path, expected in sorted(owned.files.items(), key=lambda item: len(item[0].parts), reverse=True):
        if not _owned_lineage_matches(owned):
            return
        try:
            info = path.lstat()
            if not _object_matches(info, expected) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
                return
            path.unlink()
        except OSError:
            return
    directories = [*owned.directories.items(), (owned.root, owned.root_identity)]
    for path, expected in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
        try:
            parent_info = owned.parent.lstat()
            if (
                not _object_matches(parent_info, owned.parent_identity)
                or _is_reparse(parent_info)
                or not stat.S_ISDIR(parent_info.st_mode)
            ):
                return
            info = path.lstat()
            if not _object_matches(info, expected) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                return
            path.rmdir()
        except OSError:
            return


def _copy_snapshot_to_owned(root: Path, snapshot: _TreeSnapshot, owned: _OwnedTree) -> _MapManifest:
    for relative, _expected in snapshot.directories:
        destination = owned.root / Path(relative)
        destination.mkdir()
        owned.directories[destination] = _ordinary_directory_identity(destination, "map staging directory")
    files: list[tuple[str, int, str]] = []
    for relative, expected in snapshot.files:
        _revalidate_tree(root, snapshot)
        _race_hook("map_before_file_open")
        source_descriptor = _open_captured_file(root / Path(relative), expected, "declared replay map")
        destination = owned.root / Path(relative)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        destination_descriptor = os.open(destination, flags, 0o600)
        owned.files[destination] = _identity(os.fstat(destination_descriptor))
        try:
            size, digest = _stream_descriptor(source_descriptor, destination_descriptor)
            if _descriptor_identity(source_descriptor) != expected or size != expected.size:
                raise ValueError("declared replay map source changed during staging")
        finally:
            os.close(destination_descriptor)
            os.close(source_descriptor)
        files.append((relative, size, digest))
    return tuple(relative for relative, _identity_value in snapshot.directories), tuple(files)


def _safe_custom_map_leaf(map_identity: str) -> str | None:
    normalized = map_identity.replace("\\", "/")
    prefix = "userdata/maps/"
    if not normalized.casefold().startswith(prefix):
        components = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise ValueError("replay map identity is unsafe")
        return None
    leaf = normalized[len(prefix) :]
    stem = leaf.split(".", 1)[0].upper()
    if (
        "/" in leaf
        or not _SAFE_MAP_LEAF.fullmatch(leaf)
        or leaf in {".", ".."}
        or leaf.endswith((" ", "."))
        or stem in _WINDOWS_RESERVED_LEAVES
    ):
        raise ValueError("replay map identity contains an unsafe map leaf")
    return leaf


# TheSuperHackers @bugfix Leex 24/08/2026 Bind replay staging reads to captured file identities before copying. (#TBD)
def _stage_replay_for_engine(settings: AnalyzerSettings, replay: Path, replay_sha256: str) -> tuple[Path, Path]:
    """Place identity-bound replay bytes under a short analyzer-owned engine user-data root."""
    captured = _ordinary_file_identity(replay, "replay", single_link=False)
    _race_hook("replay_after_capture")
    user_data_root = settings.data_root / "engine-user-data" / replay_sha256[:16]
    replay_directory = user_data_root / "Replays"
    replay_directory.mkdir(parents=True, exist_ok=True)
    require_plain_directory_input(replay_directory.resolve(), "isolated replay directory")
    staged_replay = replay_directory / f"replay-{replay_sha256[:16]}.rep"
    temporary = replay_directory / f".{staged_replay.name}.{uuid.uuid4().hex}.tmp"
    source_descriptor = _open_captured_file(replay, captured, "replay")
    temporary_descriptor: int | None = None
    temporary_identity: _Identity | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        temporary_descriptor = os.open(temporary, flags, 0o600)
        temporary_identity = _identity(os.fstat(temporary_descriptor))
        size, digest = _stream_descriptor(source_descriptor, temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        if (
            _descriptor_identity(source_descriptor) != captured
            or _ordinary_file_identity(replay, "replay", single_link=False) != captured
        ):
            raise ValueError("replay source changed during staging")
        if size != captured.size or digest != replay_sha256:
            raise ValueError("staged replay content does not match imported replay SHA-256")
        try:
            os.link(temporary, staged_replay)
        except FileExistsError:
            existing = _ordinary_file_identity(staged_replay, "staged replay", single_link=True)
            descriptor = _open_captured_file(staged_replay, existing, "staged replay")
            try:
                existing_size, existing_digest = _stream_descriptor(descriptor)
            finally:
                os.close(descriptor)
            if existing_size != size or existing_digest != digest:
                raise ValueError("owned replay staging path contains different content") from None
        staged = _ordinary_file_identity(staged_replay, "staged replay", single_link=False)
        descriptor = _open_captured_file(staged_replay, staged, "staged replay")
        try:
            staged_size, staged_digest = _stream_descriptor(descriptor)
        finally:
            os.close(descriptor)
        if staged_size != size or staged_digest != digest:
            raise ValueError("staged replay content does not match imported replay SHA-256")
    finally:
        os.close(source_descriptor)
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_identity is not None:
            try:
                if _object_matches(temporary.lstat(), temporary_identity):
                    temporary.unlink()
            except OSError:
                pass
    return staged_replay, user_data_root


# TheSuperHackers @bugfix Leex 24/08/2026 Publish identity-bound custom maps without replacing concurrent destinations. (#TBD)
def _stage_declared_map(settings: AnalyzerSettings, map_identity: str, user_data_root: Path) -> None:
    """Copy one safe ``userdata/maps/<leaf>`` map from an explicit retail data root."""
    leaf = _safe_custom_map_leaf(map_identity)
    if leaf is None:
        return
    source_root = settings.engine_user_data_directory
    if source_root is None:
        raise ValueError("replay map requires a configured engine user-data directory")
    source_root = require_plain_directory_input(source_root, "engine user-data directory")
    maps_root = require_plain_directory_input(source_root / "Maps", "engine user-data Maps directory")
    source = require_plain_directory_input(maps_root / leaf, "declared replay map directory")
    if not _contained(source_root, maps_root) or not _contained(maps_root, source):
        raise ValueError("declared replay map source escapes the configured user-data root")
    snapshot = _capture_tree(source, (source_root, maps_root, source))
    _race_hook("map_after_capture")

    expected_root = settings.data_root / "engine-user-data"
    if not _contained(expected_root, user_data_root):
        raise ValueError("isolated replay user-data root escapes product staging")
    maps_destination = user_data_root / "Maps"
    maps_destination.mkdir(parents=True, exist_ok=True)
    maps_destination = require_plain_directory_input(maps_destination.resolve(), "isolated Maps directory")
    destination = maps_destination / leaf
    source_manifest = _manifest_from_snapshot(source, snapshot)
    if destination.exists() or destination.is_symlink():
        if _existing_manifest(destination) != source_manifest:
            raise ValueError("isolated replay map destination does not match source manifest")
        return

    owned = _create_owned_tree(maps_destination, leaf)
    published = False
    try:
        copied_manifest = _copy_snapshot_to_owned(source, snapshot, owned)
        _race_hook("map_after_copy")
        _revalidate_tree(source, snapshot)
        if copied_manifest != source_manifest or _existing_manifest(owned.root) != source_manifest:
            raise ValueError("staged replay map content does not match source manifest")
        _race_hook("map_before_publish")
        try:
            # Windows rename is an atomic no-replace directory publication; no prior existence decision is trusted.
            os.rename(owned.root, destination)
            published = True
        except OSError as error:
            if destination.exists() and _existing_manifest(destination) == source_manifest:
                return
            raise ValueError("isolated replay map destination appeared during publication") from error
        if _existing_manifest(destination) != source_manifest:
            raise ValueError("published replay map content does not match source manifest")
    finally:
        if not published:
            _cleanup_owned_tree(owned)


def _replace_path_variants(message: str, path: Path, replacement: str) -> str:
    redacted = message
    for source_text in (str(path), path.as_posix()):
        if source_text:
            redacted = re.sub(re.escape(source_text), replacement, redacted, flags=re.IGNORECASE)
    return redacted


def _redact_diagnostic_message(message: str, replay: Path, result: EngineRunResult) -> str:
    """Retain diagnostic meaning while removing input and runner filesystem provenance."""
    redacted = _replace_path_variants(message, replay, "[replay]")
    redacted = _replace_path_variants(redacted, replay.parent, "[replay-root]")
    paths = (
        result.trace_path,
        result.catalog_path,
        result.outcome_path,
        result.stdout_path,
        result.stderr_path,
        *result.map_assets,
    )
    for path in paths:
        if path is not None:
            redacted = _replace_path_variants(redacted, path, "[engine-artifact]")
            redacted = _replace_path_variants(redacted, path.parent, "[engine-artifact-root]")
    redacted = _replace_path_variants(redacted, result.run_dir, "[engine-run]")
    redacted = _replace_path_variants(redacted, result.run_dir.parent, "[engine-run-root]")
    return _PATHLIKE_TEXT.sub("[redacted-path]", redacted)


def _diagnostics(replay: Path, result: EngineRunResult) -> tuple[AcquisitionDiagnostic, ...]:
    return tuple(
        AcquisitionDiagnostic(
            code=diagnostic.code,
            message=_redact_diagnostic_message(diagnostic.message, replay, result),
        )
        for diagnostic in result.diagnostics
    )


# TheSuperHackers @feature Leex 23/08/2026 Adapt validated engine telemetry runs into import-safe acquisition artifacts. (#TBD)
@dataclass(frozen=True, slots=True)
class EngineTelemetryAcquirer:
    """Bind managed replay content to one validated engine telemetry attempt."""

    settings: AnalyzerSettings
    exporter: EngineTelemetryExporter = export_telemetry

    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
        """Run a content-bound replay and translate its closed runner outcome."""
        if _sha256_file(replay) != replay_sha256:
            raise ValueError("replay SHA-256 does not match the imported replay content")
        executable = self.settings.engine_executable
        if executable is None:
            raise ValueError("engine telemetry acquisition requires a configured engine executable")
        staged_replay, replay_user_data_root = _stage_replay_for_engine(self.settings, replay, replay_sha256)
        if self.settings.engine_user_data_directory is not None:
            map_identity = parse_replay(replay).header.map
            _stage_declared_map(self.settings, map_identity, replay_user_data_root)
        config = EngineRunConfig(
            executable=executable,
            runtime_directory=self.settings.engine_runtime_directory,
            data_root=self.settings.data_root,
            movement_sample_frames=self.settings.movement_sample_frames,
            replay_user_data_root=replay_user_data_root,
        )
        executable_sha256 = _sha256_file(config.executable)
        result = self.exporter(staged_replay, config)
        diagnostics = _diagnostics(replay, result)
        if result.status is EngineRunStatus.SUCCESS:
            runner_status = "success"
            replay_quality = "complete"
            strategy_analysis_scope = "full"
        elif result.status in _PARTIAL_TRACE_STATUSES:
            runner_status = "success"
            replay_quality = "partial"
            strategy_analysis_scope = "observed_boundary_only"
            diagnostics += (AcquisitionDiagnostic("engine_terminal_status", result.status.value),)
        else:
            runner_status = result.status.value
            replay_quality = "failed"
            strategy_analysis_scope = "none"
        return TelemetryArtifact(
            run_id=result.run_id,
            runner_status=runner_status,
            replay_quality=replay_quality,
            strategy_analysis_scope=strategy_analysis_scope,
            trace_path=result.trace_path,
            catalog_path=result.catalog_path,
            map_asset_paths=result.map_assets,
            outcome_path=result.outcome_path,
            stdout_path=result.stdout_path,
            stderr_path=result.stderr_path,
            exit_code=result.exit_code,
            engine_build=None,
            engine_executable_sha256=executable_sha256,
            diagnostics=diagnostics,
        )
