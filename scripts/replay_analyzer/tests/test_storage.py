"""Atomic content-addressed runtime storage contracts."""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from generals_replay_analyzer.storage import (
    ContentAddressedStore,
    ContentCollisionError,
    ContentStorageError,
    HashMismatchError,
)


def _temporary_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and path.name.endswith(".tmp")]


def test_store_bytes_publishes_lowercase_content_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the final sharded path to flushed exact bytes before it becomes public."""
    store = ContentAddressedStore(tmp_path / "store")
    payload = b"authoritative replay bytes"
    digest = hashlib.sha256(payload).hexdigest()
    real_fsync = os.fsync
    fsync_calls: list[int] = []
    real_link = os.link
    same_directory_publications: list[bool] = []

    def recording_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    def recording_link(source: Path, target: Path) -> None:
        same_directory_publications.append(Path(source).parent == Path(target).parent)
        real_link(source, target)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "link", recording_link)

    stored = store.store_bytes(payload)

    assert stored.sha256 == digest
    assert stored.path == store.root / digest[:2] / digest
    assert stored.path.read_bytes() == payload
    assert stored.size == len(payload)
    assert stored.created is True
    assert fsync_calls
    assert same_directory_publications == [True]
    assert _temporary_files(store.root) == []


def test_store_file_preserves_caller_source_bytes(tmp_path: Path) -> None:
    """Copy caller evidence into managed storage without mutating or removing its source."""
    source = tmp_path / "source.rep"
    source.write_bytes(b"source replay")
    original = source.read_bytes()
    store = ContentAddressedStore(tmp_path / "store")

    stored = store.store_file(source)

    assert stored.path.read_bytes() == original
    assert source.read_bytes() == original
    assert source.is_file()


def test_identical_content_is_deduplicated_after_exact_revalidation(tmp_path: Path) -> None:
    """Reuse an exact managed object without rewriting it or changing its timestamp."""
    store = ContentAddressedStore(tmp_path / "store")
    first = store.store_bytes(b"same content")
    first_timestamp = first.path.stat().st_mtime_ns

    second = store.store_bytes(b"same content")

    assert second.path == first.path
    assert second.sha256 == first.sha256
    assert second.created is False
    assert second.path.stat().st_mtime_ns == first_timestamp


def test_expected_hash_is_case_insensitive_and_normalized(tmp_path: Path) -> None:
    """Bridge the historical uppercase replay digest to lowercase managed identities."""
    payload = b"case-normalized digest"
    expected = hashlib.sha256(payload).hexdigest().upper()
    store = ContentAddressedStore(tmp_path / "store")

    stored = store.store_bytes(payload, expected_sha256=expected)

    assert stored.sha256 == expected.lower()
    assert stored.path.name == expected.lower()


def test_expected_hash_mismatch_publishes_nothing(tmp_path: Path) -> None:
    """Fail closed when caller identity and copied bytes disagree."""
    store = ContentAddressedStore(tmp_path / "store")

    with pytest.raises(HashMismatchError, match="expected SHA-256"):
        store.store_bytes(b"unexpected bytes", expected_sha256="0" * 64)

    assert [path for path in store.root.rglob("*") if path.is_file()] == []


def test_existing_valid_target_does_not_hide_input_hash_mismatch(tmp_path: Path) -> None:
    """Validate each caller's bytes even when its claimed hash already exists in the store."""
    store = ContentAddressedStore(tmp_path / "store")
    existing = store.store_bytes(b"existing valid bytes")

    with pytest.raises(HashMismatchError, match="expected SHA-256"):
        store.store_bytes(b"different caller bytes", expected_sha256=existing.sha256)

    assert existing.path.read_bytes() == b"existing valid bytes"
    assert _temporary_files(store.root) == []


def test_existing_corrupt_hash_target_is_rejected_not_overwritten(tmp_path: Path) -> None:
    """Never let a poisoned hash path be replaced or accepted as deduplicated content."""
    payload = b"wanted content"
    digest = hashlib.sha256(payload).hexdigest()
    store = ContentAddressedStore(tmp_path / "store")
    target = store.root / digest[:2] / digest
    target.parent.mkdir(parents=True)
    target.write_bytes(b"caller-owned corrupt bytes")

    with pytest.raises(ContentCollisionError, match="does not match"):
        store.store_bytes(payload)

    assert target.read_bytes() == b"caller-owned corrupt bytes"


def test_failed_publication_removes_only_owned_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean failed transaction bytes without deleting caller inputs or unrelated targets."""
    store = ContentAddressedStore(tmp_path / "store")
    source = tmp_path / "source.rep"
    source.write_bytes(b"preserve source")

    def fail_link(_source: Path, _target: Path) -> None:
        raise PermissionError("publication denied")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(ContentStorageError, match="publish"):
        store.store_file(source)

    assert source.read_bytes() == b"preserve source"
    assert _temporary_files(store.root) == []
    assert [path for path in store.root.rglob("*") if path.is_file()] == []


def test_store_file_rejects_non_regular_source(tmp_path: Path) -> None:
    """Avoid treating a directory or special input as immutable file evidence."""
    store = ContentAddressedStore(tmp_path / "store")
    source_directory = tmp_path / "source-directory"
    source_directory.mkdir()

    with pytest.raises(ContentStorageError, match="regular file"):
        store.store_file(source_directory)


def test_verify_rejects_missing_or_corrupt_content(tmp_path: Path) -> None:
    """Make later consumers re-establish exact managed identity before use."""
    store = ContentAddressedStore(tmp_path / "store")
    missing_digest = hashlib.sha256(b"missing").hexdigest()

    with pytest.raises(ContentStorageError, match="does not exist"):
        store.verify(missing_digest)

    stored = store.store_bytes(b"valid")
    stored.path.write_bytes(b"corrupt")
    with pytest.raises(ContentCollisionError, match="does not match"):
        store.verify(stored.sha256)


def test_concurrent_no_replace_publication_preserves_one_valid_winner(tmp_path: Path) -> None:
    """Use filesystem no-replace publication rather than a check-then-overwrite race."""
    store = ContentAddressedStore(tmp_path / "store")
    payload = b"concurrent immutable content" * 1024

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: store.store_bytes(payload), range(2)))

    assert sorted(result.created for result in results) == [False, True]
    assert results[0].path == results[1].path
    assert results[0].path.read_bytes() == payload
    assert _temporary_files(store.root) == []


def test_stored_content_result_is_frozen(tmp_path: Path) -> None:
    """Keep returned identity stable while database stages consume it."""
    stored = ContentAddressedStore(tmp_path / "store").store_bytes(b"immutable result")

    with pytest.raises(FrozenInstanceError):
        stored.created = False  # type: ignore[misc]
