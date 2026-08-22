"""Atomic content-addressed runtime storage contracts."""

import hashlib
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event, Lock, Timer

import pytest

import generals_replay_analyzer.storage as storage_module
from generals_replay_analyzer.storage import (
    ContentAddressedStore,
    ContentCollisionError,
    ContentStorageError,
    HashMismatchError,
)


def _temporary_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and path.name.endswith(".tmp")]


def _make_directory_alias(alias: Path, target: Path) -> None:
    """Create a native directory alias or skip when the host forbids it."""
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(alias), os.fspath(target)],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode:
            pytest.skip("Windows junction creation is unavailable on this host")
        return
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")


def test_store_bytes_publishes_lowercase_content_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the final sharded path to flushed exact bytes before it becomes public."""
    store = ContentAddressedStore(tmp_path / "store")
    payload = b"authoritative replay bytes"
    digest = hashlib.sha256(payload).hexdigest()
    real_fsync = os.fsync
    fsync_calls: list[int] = []
    real_publish_link = storage_module._publish_link
    same_directory_publications: list[bool] = []

    def recording_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    def recording_link(descriptor: int, parent: object, temporary_name: str, target_name: str) -> None:
        same_directory_publications.append("/" not in temporary_name and "/" not in target_name)
        real_publish_link(descriptor, parent, temporary_name, target_name)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(storage_module, "_publish_link", recording_link)

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

    def fail_link(_descriptor: int, _parent: object, _temporary_name: str, _target_name: str) -> None:
        raise PermissionError("publication denied")

    monkeypatch.setattr(storage_module, "_publish_link", fail_link)

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


def test_concurrent_loser_settles_winner_temporary_hardlink_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not misclassify the winner's bounded two-link publication interval as tampering."""
    store = ContentAddressedStore(tmp_path / "store")
    payload = b"forced hardlink publication interval" * 1024
    real_publish = storage_module._publish_link
    first_linked = Event()
    release_winner = Event()
    calls_lock = Lock()
    calls = 0

    def paused_first_publication(
        descriptor: int,
        parent: object,
        temporary_name: str,
        target_name: str,
    ) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        real_publish(descriptor, parent, temporary_name, target_name)  # type: ignore[arg-type]
        if call_number == 1:
            first_linked.set()
            if not release_winner.wait(timeout=10):
                raise TimeoutError("winner publication release was not signaled")

    monkeypatch.setattr(storage_module, "_publish_link", paused_first_publication)

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(store.store_bytes, payload)
        assert first_linked.wait(timeout=10)
        release_timer = Timer(0.2, release_winner.set)
        release_timer.start()
        loser = executor.submit(store.store_bytes, payload)
        try:
            results = (winner.result(timeout=10), loser.result(timeout=10))
        finally:
            release_winner.set()
            release_timer.cancel()

    assert sorted(result.created for result in results) == [False, True]
    assert results[0].path == results[1].path
    assert results[0].path.read_bytes() == payload
    assert results[0].path.stat().st_nlink == 1
    assert _temporary_files(store.root) == []


def test_stored_content_result_is_frozen(tmp_path: Path) -> None:
    """Keep returned identity stable while database stages consume it."""
    stored = ContentAddressedStore(tmp_path / "store").store_bytes(b"immutable result")

    with pytest.raises(FrozenInstanceError):
        stored.created = False  # type: ignore[misc]


def test_store_rejects_root_directory_alias_instead_of_following_it(tmp_path: Path) -> None:
    """Never adopt a symlink, junction, or reparse point as the managed root."""
    real_root = tmp_path / "real-store"
    real_root.mkdir()
    alias = tmp_path / "aliased-store"
    _make_directory_alias(alias, real_root)

    with pytest.raises(ContentStorageError, match="unsafe managed storage lineage"):
        ContentAddressedStore(alias).store_bytes(b"must not cross alias")

    assert tuple(real_root.rglob("*")) == ()


def test_store_rejects_aliased_root_ancestor_before_creating_children(tmp_path: Path) -> None:
    """Create missing root components only relative to already-bound ordinary ancestors."""
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "aliased-parent"
    _make_directory_alias(alias, outside)

    with pytest.raises(ContentStorageError, match="unsafe managed storage lineage"):
        ContentAddressedStore(alias / "nested" / "store").store_bytes(b"no outside root creation")

    assert tuple(outside.rglob("*")) == ()


def test_store_rejects_aliased_shard_without_writing_outside_root(tmp_path: Path) -> None:
    """Bind each digest shard as an ordinary child of the already-bound root."""
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"must remain inside managed root"
    digest = hashlib.sha256(payload).hexdigest()
    _make_directory_alias(root / digest[:2], outside)

    with pytest.raises(ContentStorageError, match="unsafe managed storage lineage"):
        ContentAddressedStore(root).store_bytes(payload)

    assert tuple(outside.rglob("*")) == ()


def test_verify_rejects_hardlinked_managed_object(tmp_path: Path) -> None:
    """A managed object must have one unambiguous ordinary-file identity."""
    store = ContentAddressedStore(tmp_path / "store")
    stored = store.store_bytes(b"single link required")
    alias = tmp_path / "alias.rep"
    try:
        os.link(stored.path, alias)
    except OSError:
        pytest.skip("hard links are unavailable on this host")

    with pytest.raises(ContentCollisionError, match="single-link"):
        store.verify(stored.sha256)
    with pytest.raises(ContentCollisionError, match="single-link"):
        store.store_bytes(b"single link required")

    assert alias.read_bytes() == b"single link required"


def test_store_fails_closed_when_bound_shard_is_substituted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shard rename or replacement between bind and publication cannot redirect bytes."""
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"shard lineage race"
    digest = hashlib.sha256(payload).hexdigest()
    moved = root / f"{digest[:2]}-moved"

    def substitute(site: str) -> None:
        if site != "storage_shard_bound":
            return
        shard = root / digest[:2]
        shard.rename(moved)
        _make_directory_alias(shard, outside)

    monkeypatch.setattr(storage_module, "_race_hook", substitute, raising=False)

    with pytest.raises(ContentStorageError, match="managed storage lineage changed"):
        ContentAddressedStore(root).store_bytes(payload)

    assert not (outside / digest).exists()


def test_verify_fails_closed_when_named_target_is_swapped_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hash an opened object and then prove the name still denotes that same object."""
    store = ContentAddressedStore(tmp_path / "store")
    stored = store.store_bytes(b"opened target identity")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"opened target identity")

    def swap(site: str) -> None:
        if site != "storage_target_opened":
            return
        stored.path.unlink()
        shutil.move(replacement, stored.path)

    monkeypatch.setattr(storage_module, "_race_hook", swap, raising=False)

    with pytest.raises(ContentStorageError, match="managed content identity changed"):
        store.verify(stored.sha256)


def test_verify_fails_closed_when_open_target_is_rewritten_with_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching digest is insufficient when the opened object changed during its read."""
    payload = b"identical rewrite must still change identity"
    store = ContentAddressedStore(tmp_path / "store")
    stored = store.store_bytes(payload)

    def rewrite(site: str) -> None:
        if site == "storage_target_hashed":
            stored.path.write_bytes(payload)

    monkeypatch.setattr(storage_module, "_race_hook", rewrite)

    with pytest.raises(ContentStorageError, match="managed content identity changed"):
        store.verify(stored.sha256)


def test_store_file_preserves_source_when_shard_lineage_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed managed publication never consumes or mutates caller evidence."""
    source = tmp_path / "source.rep"
    source.write_bytes(b"preserved during storage race")
    original = source.read_bytes()

    def fail_after_bind(site: str) -> None:
        if site == "storage_shard_bound":
            raise OSError("simulated lineage mutation")

    monkeypatch.setattr(storage_module, "_race_hook", fail_after_bind, raising=False)

    with pytest.raises(ContentStorageError, match="managed storage lineage changed"):
        ContentAddressedStore(tmp_path / "store").store_file(source)

    assert source.read_bytes() == original
    assert source.is_file()
