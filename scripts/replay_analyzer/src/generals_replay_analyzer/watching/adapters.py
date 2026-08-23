"""Private filesystem adapters for the external watched-folder scheduler."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Protocol

from ..ingress_contract import IngressIdentityError, ReplayIngressIdentity
from .roots import RootRegistryError, SnapshotIngressError, WatchedRoot, WatchedRootRegistry
from .service import WatchDiscoveryError, WatchedEntryDTO, WatchedRootSnapshotDTO

if TYPE_CHECKING:
    from ..importing import ImportService

LOGGER = logging.getLogger(__name__)


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


class _IngressSnapshot(Protocol):
    @property
    def sha256(self) -> str: ...

    def read_verified_bytes(self) -> bytes: ...


class _SnapshotRegistry(Protocol):
    def snapshot_replay(self, root_public_id: str, relative_name: str) -> _IngressSnapshot: ...


# TheSuperHackers @feature Leex 22/08/2026 Reopen verified ingress bytes before a product-owned durable import handoff. (#TBD)
class VerifiedIngressImportAdapter:
    """Submit verified bytes through Analytics without exposing snapshot locators or paths."""

    def __init__(
        self,
        registry: _SnapshotRegistry,
        submit_verified: Callable[[bytes, str, str, str], str],
    ) -> None:
        self._registry = registry
        self._submit_verified = submit_verified

    def submit_stable(self, root_public_id: str, relative_name: str) -> str:
        try:
            ReplayIngressIdentity(root_public_id, relative_name)
        except IngressIdentityError as error:
            raise WatchDiscoveryError(error.code.value) from None
        try:
            snapshot = self._registry.snapshot_replay(root_public_id, relative_name)
            content = snapshot.read_verified_bytes()
        except SnapshotIngressError as error:
            raise WatchDiscoveryError(error.code) from None
        except OSError:
            raise WatchDiscoveryError("replay_source_changed") from None
        digest = hashlib.sha256(content).hexdigest()
        if digest != snapshot.sha256:
            raise WatchDiscoveryError("ingress_snapshot_changed")
        try:
            return self._submit_verified(content, digest, root_public_id, relative_name)
        except WatchDiscoveryError:
            raise
        except Exception:  # noqa: BLE001 - external import failures must not terminate the worker scheduler.
            raise WatchDiscoveryError("watched_import_unavailable") from None

    def record_discovery_problem(self, root_public_id: str, problem_code: str) -> None:
        """Emit only the stable code and opaque root identifier, never a source locator."""
        LOGGER.warning(
            "watched replay discovery problem root_public_id=%s code=%s",
            root_public_id,
            problem_code,
        )


# TheSuperHackers @feature Leex 22/08/2026 Keep watched replay paths inside the production import adapter boundary. (#TBD)
def create_analytics_watched_import_adapter(
    registry: _SnapshotRegistry,
    service: ImportService,
    *,
    request_telemetry: bool,
) -> VerifiedIngressImportAdapter:
    """Bind verified watcher handoffs directly to the Analytics import application service."""
    from ..importing import VerifiedReplaySubmission

    if type(request_telemetry) is not bool:
        raise TypeError("request_telemetry must be bool")

    def submit_verified_handoff(
        content: bytes,
        expected_sha256: str,
        root_public_id: str,
        relative_name: str,
    ) -> str:
        submission = service.submit_verified(
            VerifiedReplaySubmission(
                content,
                expected_sha256,
                root_public_id,
                relative_name,
                request_telemetry=request_telemetry,
            )
        )
        return str(submission.discovery_job.public_id)

    return VerifiedIngressImportAdapter(registry, submit_verified_handoff)


class RegistryWatchedRootAdapter:
    """Enumerate configured roots into opaque, stabilization-only snapshots."""

    def __init__(self, registry: WatchedRootRegistry, watched_folders: Sequence[Path]) -> None:
        self._registry = registry
        self._folders = tuple(Path(folder) for folder in watched_folders)
        self._last_roots: tuple[WatchedRoot, ...] = ()

    def snapshots(self) -> tuple[WatchedRootSnapshotDTO, ...]:
        try:
            roots = self._registry.reconcile(self._folders)
        except RootRegistryError as error:
            if not self._last_roots:
                raise WatchDiscoveryError(error.code) from None
            scanned_at = datetime.now(UTC)
            return tuple(
                WatchedRootSnapshotDTO(
                    root.root_public_id,
                    scanned_at,
                    (),
                    (error.code,),
                    label=root.label,
                )
                for root in self._last_roots
            )
        self._last_roots = roots
        return tuple(
            self._snapshot(path, root)
            for path, root in zip(self._folders, roots, strict=True)
        )

    @staticmethod
    def _snapshot(path: Path, root: WatchedRoot) -> WatchedRootSnapshotDTO:
        problems: list[str] = []
        entries: list[WatchedEntryDTO] = []
        if not root.available:
            problems.append(root.reason_code or "watched_root_unavailable")
        else:
            try:
                def record_walk_error(_error: OSError) -> None:
                    problems.append("watched_root_scan_failed")

                for directory, names, files in os.walk(path, followlinks=False, onerror=record_walk_error):
                    safe_names: list[str] = []
                    for name in names:
                        candidate_directory = Path(directory) / name
                        try:
                            info = candidate_directory.lstat()
                            if candidate_directory.is_symlink() or _is_reparse(info):
                                problems.append("entry_not_regular")
                            elif stat.S_ISDIR(info.st_mode):
                                safe_names.append(name)
                            else:
                                problems.append("entry_not_regular")
                        except OSError:
                            problems.append("entry_inaccessible")
                    names[:] = safe_names
                    for name in files:
                        candidate = Path(directory) / name
                        relative = candidate.relative_to(path).as_posix()
                        if not relative.endswith(".rep"):
                            problems.append("entry_extension_rejected")
                            continue
                        try:
                            ReplayIngressIdentity(root.root_public_id, relative)
                        except IngressIdentityError as error:
                            problems.append(error.code.value)
                            continue
                        try:
                            stat_result = candidate.lstat()
                            if candidate.is_symlink() or _is_reparse(stat_result) or not S_ISREG(stat_result.st_mode):
                                problems.append("entry_not_regular")
                                continue
                        except OSError:
                            problems.append("entry_inaccessible")
                            continue
                        identity = f"{stat_result.st_dev:x}-{stat_result.st_ino:x}"
                        entries.append(
                            WatchedEntryDTO(
                                root.root_public_id,
                                relative,
                                stat_result.st_size,
                                stat_result.st_mtime_ns,
                                identity,
                            )
                        )
            except (OSError, ValueError):
                entries.clear()
                problems.append("watched_root_scan_failed")
        return WatchedRootSnapshotDTO(
            root.root_public_id,
            datetime.now(UTC),
            tuple(sorted(entries, key=lambda entry: entry.relative_name)),
            tuple(sorted(set(problems))),
            label=root.label,
        )
