"""Atomic content-addressed storage for analyzer-owned immutable bytes."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_READ_CHUNK_SIZE = 1024 * 1024


class ContentStorageError(RuntimeError):
    """Base failure for managed content that could not be safely stored or verified."""


class HashMismatchError(ContentStorageError):
    """Copied bytes differ from a caller-supplied content identity."""


class ContentCollisionError(ContentStorageError):
    """An occupied content path does not contain the bytes named by its digest."""


@dataclass(frozen=True)
class StoredContent:
    """One immutable managed object and whether this call first published it."""

    sha256: str
    path: Path
    size: int
    created: bool


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


# TheSuperHackers @feature Leex 21/08/2026 Publish analyzer assets by exact lowercase content identity without overwrite. (#TBD)
@dataclass(frozen=True)
class ContentAddressedStore:
    """Store immutable files below a two-character SHA-256 shard."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve(strict=False))

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
        return self._verify_target(self._target(normalized), normalized)

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

    def _verify_target(self, target: Path, expected_sha256: str) -> StoredContent:
        try:
            info = target.lstat()
        except FileNotFoundError as error:
            raise ContentStorageError(f"managed content does not exist: {target}") from error
        except OSError as error:
            raise ContentStorageError(f"managed content cannot be inspected: {target}: {error}") from error
        if not stat.S_ISREG(info.st_mode) or target.is_symlink():
            raise ContentCollisionError(f"managed content path is not a regular file: {target}")
        try:
            actual_sha256, size = _hash_chunks(_file_chunks(target))
        except OSError as error:
            raise ContentStorageError(f"managed content cannot be read: {target}: {error}") from error
        if actual_sha256 != expected_sha256:
            raise ContentCollisionError(
                f"managed content at '{target}' does not match its SHA-256 identity {expected_sha256}"
            )
        return StoredContent(expected_sha256, target, size, False)

    def _publish(self, chunks: Iterable[bytes], expected_sha256: str) -> StoredContent:
        target = self._target(expected_sha256)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ContentStorageError(f"cannot create managed content directory '{target.parent}': {error}") from error

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{expected_sha256}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary_path = Path(temporary_name)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "wb") as destination:
                for chunk in chunks:
                    destination.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise HashMismatchError(
                    f"expected SHA-256 {expected_sha256} but copied bytes have SHA-256 {actual_sha256}"
                )
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                return self._verify_target(target, expected_sha256)
            except OSError as error:
                raise ContentStorageError(f"cannot publish managed content '{target}': {error}") from error
            return StoredContent(expected_sha256, target, size, True)
        except ContentStorageError:
            raise
        except OSError as error:
            raise ContentStorageError(f"cannot write managed content '{target}': {error}") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
