"""Safe watched-root identity and immutable replay ingress."""

from .adapters import create_analytics_watched_import_adapter
from .roots import IngressSnapshot, RootRegistryError, SnapshotIngressError, WatchedRoot, WatchedRootRegistry
from .service import (
    WatchDiscoveryError,
    WatchedEntryDTO,
    WatchedImportPort,
    WatchedRootPort,
    WatchedRootSnapshotDTO,
    WatchScheduler,
    WatchStatusStorePort,
)
from .status import FileWatchStatusStore, WatchFolderStatusRecord

__all__ = [
    "FileWatchStatusStore",
    "IngressSnapshot",
    "RootRegistryError",
    "SnapshotIngressError",
    "WatchDiscoveryError",
    "WatchFolderStatusRecord",
    "WatchScheduler",
    "WatchStatusStorePort",
    "WatchedEntryDTO",
    "WatchedImportPort",
    "WatchedRoot",
    "WatchedRootPort",
    "WatchedRootRegistry",
    "WatchedRootSnapshotDTO",
    "create_analytics_watched_import_adapter",
]
