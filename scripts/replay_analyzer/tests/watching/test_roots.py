"""Persistent watched-root identity and immutable ingress contracts."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

import generals_replay_analyzer.watching.roots as roots_module
from generals_replay_analyzer.watching.roots import (
    IngressSnapshot,
    RootRegistryError,
    SnapshotIngressError,
    WatchedRootRegistry,
)


def test_watched_root_registry_api_is_importable() -> None:
    """Removing the registry package must break its public Analytics boundary."""
    assert WatchedRootRegistry is not None


def _registry_document(data_root: Path) -> dict[str, object]:
    return json.loads((data_root / "watched-roots-v1.json").read_text(encoding="utf-8"))


def _reconcile_in_spawned_process(arguments: tuple[str, str]) -> str:
    data_root, source = arguments
    return WatchedRootRegistry(Path(data_root)).reconcile((Path(source),))[0].root_public_id


def _hold_digest_lock_in_spawned_process(arguments: tuple[str, str, str]) -> None:
    lock_directory, digest, coordination_directory = arguments
    coordination = Path(coordination_directory)
    with roots_module._digest_lock(Path(lock_directory), digest):
        (coordination / "ready").write_bytes(b"ready")
        deadline = time.monotonic() + 10
        while not (coordination / "release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test digest lock release was not signaled")
            time.sleep(0.01)


def _hold_named_digest_lock_in_spawned_process(arguments: tuple[str, str, str, str]) -> None:
    lock_directory, digest, ready_path, release_path = arguments
    with roots_module._digest_lock(Path(lock_directory), digest):
        Path(ready_path).write_bytes(b"ready")
        deadline = time.monotonic() + 10
        while not Path(release_path).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("named digest lock release was not signaled")
            time.sleep(0.01)


def _snapshot_in_spawned_process(arguments: tuple[str, str, str, str | None]) -> tuple[str, bool]:
    data_root, source_root, relative_name, coordination_directory = arguments
    registry = WatchedRootRegistry(Path(data_root))
    root_public_id = registry.reconcile((Path(source_root),))[0].root_public_id
    if coordination_directory is not None:
        (Path(coordination_directory) / "publisher-started").write_bytes(b"started")
    snapshot = registry.snapshot_replay(root_public_id, relative_name)
    return snapshot.sha256, snapshot.created


def test_reconcile_persists_random_stable_ids_and_generic_labels(tmp_path: Path) -> None:
    """Changing processes or source spellings must not regenerate or disclose root identity."""
    data_root = tmp_path / "product"
    source = tmp_path / "customer-secret-replays"
    source.mkdir()

    first = WatchedRootRegistry(data_root).reconcile((source,))
    second = WatchedRootRegistry(data_root).reconcile((source,))

    assert first == second
    assert len(first) == 1
    assert UUID(first[0].root_public_id).version == 4
    assert str(UUID(first[0].root_public_id)) == first[0].root_public_id
    assert first[0].label == "Replay folder 1"
    assert first[0].available is True
    assert first[0].reason_code is None
    document = _registry_document(data_root)
    assert set(document) == {"version", "roots"}
    assert document["version"] == 1
    assert len(document["roots"]) == 1  # type: ignore[arg-type]
    entry = document["roots"][0]  # type: ignore[index]
    assert set(entry) == {"path_key_sha256", "root_public_id", "label"}
    persisted = json.dumps(document, sort_keys=True)
    assert str(source) not in persisted
    assert source.name not in persisted


def test_disabled_root_is_retained_and_reenabled_with_the_same_id(tmp_path: Path) -> None:
    """Removing a configured root temporarily must not destroy its opaque identity."""
    data_root = tmp_path / "product"
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    registry = WatchedRootRegistry(data_root)
    initial = registry.reconcile((first_source, second_source))

    registry.reconcile((second_source,))
    reenabled = registry.reconcile((first_source, second_source))

    assert [root.root_public_id for root in reenabled] == [root.root_public_id for root in initial]
    assert len(_registry_document(data_root)["roots"]) == 2  # type: ignore[arg-type]


def test_concurrent_reconciliation_serializes_read_modify_publish(tmp_path: Path) -> None:
    """Removing the narrow lock must cause one concurrent root registration to be lost."""
    data_root = tmp_path / "product"
    sources = (tmp_path / "source-a", tmp_path / "source-b")
    for source in sources:
        source.mkdir()
    arguments = tuple((str(data_root), str(source)) for source in sources)
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as executor:
        ids = tuple(executor.map(_reconcile_in_spawned_process, arguments))

    final = WatchedRootRegistry(data_root).reconcile(sources)
    assert {root.root_public_id for root in final} == set(ids)
    assert len(_registry_document(data_root)["roots"]) == 2  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "document",
    [
        b"not-json",
        b'{"version":2,"roots":[]}',
        b'{"version":1,"roots":"wrong"}',
        b'{"version":1,"roots":[],"unexpected":true}',
        b'{"version":1,"roots":["wrong"]}',
        (
            b'{"version":1,"roots":['
            b'{"path_key_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root_public_id":42,"label":"Replay folder 1"}]}'
        ),
        (
            b'{"version":1,"roots":['
            b'{"path_key_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root_public_id":"not-a-uuid","label":"Replay folder 1"}]}'
        ),
        (
            b'{"version":1,"roots":['
            b'{"path_key_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root_public_id":"123e4567-e89b-42d3-a456-426614174000","label":"Replay folder 1"},'
            b'{"path_key_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root_public_id":"123e4567-e89b-42d3-a456-426614174001","label":"Replay folder 2"}]}'
        ),
        (
            b'{"version":1,"roots":['
            b'{"path_key_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root_public_id":"123e4567-e89b-42d3-a456-426614174000","label":"Replay folder 1"},'
            b'{"path_key_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            b'"root_public_id":"123e4567-e89b-42d3-a456-426614174000","label":"Replay folder 2"}]}'
        ),
        (
            b'{"version":1,"roots":['
            b'{"path_key_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root_public_id":"123e4567-e89b-12d3-a456-426614174000","label":"Replay folder 1"}]}'
        ),
        (
            b'{"version":1,"roots":['
            b'{"path_key_sha256":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",'
            b'"root_public_id":"123E4567-E89B-42D3-A456-426614174000","label":"Private archives"}]}'
        ),
    ],
    ids=[
        "corrupt-json",
        "wrong-version",
        "wrong-roots-type",
        "unexpected-document-field",
        "wrong-entry-type",
        "wrong-id-type",
        "malformed-id",
        "duplicate-path-key",
        "duplicate-public-id",
        "non-v4-id",
        "unsafe-identity-fields",
    ],
)
def test_invalid_registry_fails_closed_without_replacing_caller_bytes(tmp_path: Path, document: bytes) -> None:
    """Relaxing registry validation must never repair or overwrite ambiguous caller bytes."""
    data_root = tmp_path / "product"
    data_root.mkdir()
    registry_path = data_root / "watched-roots-v1.json"
    registry_path.write_bytes(document)
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(RootRegistryError) as raised:
        WatchedRootRegistry(data_root).reconcile((source,))

    assert raised.value.code == "watched_root_registry_invalid"
    assert registry_path.read_bytes() == document
    assert str(source) not in str(raised.value)
    assert source.name not in repr(raised.value)


@pytest.mark.parametrize(
    "document",
    [
        b'{"version":1,"version":1,"roots":[]}',
        b'{"version":true,"roots":[]}',
        b'{"version":1,"roots":[],"roots":[]}',
        b'{"version":1,"roots":' + b"[" * 64 + b"]" * 64 + b"}",
        b"{" + b'"version":1,"roots":[],"padding":"' + b"x" * (1024 * 1024) + b'"}',
        (
            b'{"version":1,"roots":['
            b'{"path_key_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root_public_id":"123e4567-e89b-42d3-a456-426614174000",'
            b'"label":"Replay folder 999999999999999999999999999999999999999999999999"}]}'
        ),
    ],
    ids=["duplicate-version", "boolean-version", "duplicate-roots", "nested", "oversize", "oversize-label-index"],
)
def test_registry_rejects_ambiguous_or_unbounded_json_without_rewrite(tmp_path: Path, document: bytes) -> None:
    """Removing JSON bounds or duplicate-key rejection must admit ambiguous persistent identity state."""
    data_root = tmp_path / "product"
    data_root.mkdir()
    registry_path = data_root / "watched-roots-v1.json"
    registry_path.write_bytes(document)

    with pytest.raises(RootRegistryError) as raised:
        WatchedRootRegistry(data_root).reconcile(())

    assert raised.value.code == "watched_root_registry_invalid"
    assert registry_path.read_bytes() == document


def test_casefolded_configured_path_collision_is_rejected_without_registry_write(tmp_path: Path) -> None:
    """Treating case aliases as different roots must not create ambiguous public IDs."""
    data_root = tmp_path / "product"
    upper = tmp_path / "League"
    lower = tmp_path / "league"

    with pytest.raises(RootRegistryError) as raised:
        WatchedRootRegistry(data_root).reconcile((upper, lower))

    assert raised.value.code == "watched_root_path_collision"
    assert not (data_root / "watched-roots-v1.json").exists()
    assert str(upper) not in str(raised.value)
    assert str(lower) not in repr(raised.value)


def test_verified_native_root_aliases_cannot_receive_two_public_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hashing lexical root spellings must assign two IDs when native handles identify the same directory."""
    first = tmp_path / "alias-one"
    second = tmp_path / "alias-two"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        roots_module,
        "_native_root_identity_key",
        lambda _path, _directory: "fixed-volume:file-id:canonical-root",
    )
    registry = WatchedRootRegistry(tmp_path / "product")

    with pytest.raises(RootRegistryError) as raised:
        registry.reconcile((first, second))

    assert raised.value.code == "watched_root_path_collision"
    assert not (registry.data_root / "watched-roots-v1.json").exists()


def test_registry_path_must_be_an_ordinary_file(tmp_path: Path) -> None:
    """Accepting a directory at the private registry path would make identity state ambiguous."""
    data_root = tmp_path / "product"
    data_root.mkdir()
    (data_root / "watched-roots-v1.json").mkdir()

    with pytest.raises(RootRegistryError) as raised:
        WatchedRootRegistry(data_root).reconcile(())

    assert raised.value.code == "watched_root_registry_invalid"


@pytest.mark.parametrize("occupied_name", ["watched-roots-v1.json", ".watched-roots-v1.lock"])
def test_registry_and_lock_reject_hardlinked_identity(tmp_path: Path, occupied_name: str) -> None:
    """Accepting an alias to registry coordination state must permit out-of-band mutation through the second name."""
    data_root = tmp_path / "product"
    data_root.mkdir()
    occupied = data_root / occupied_name
    if occupied_name.endswith(".json"):
        occupied.write_bytes(b'{"roots":[],"version":1}\n')
    else:
        occupied.write_bytes(b"\0")
    alias = data_root / f"{occupied_name}.alias"
    try:
        os.link(occupied, alias)
    except OSError as error:
        pytest.skip(f"hardlink creation is unavailable: {error}")
    original = occupied.read_bytes()

    with pytest.raises(RootRegistryError) as raised:
        WatchedRootRegistry(data_root).reconcile(())

    assert raised.value.code == "watched_root_registry_invalid"
    assert occupied.read_bytes() == original
    assert alias.read_bytes() == original


def test_registry_change_after_read_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the pre-replace identity/byte check must overwrite a concurrent registry replacement."""
    data_root = tmp_path / "product"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    registry = WatchedRootRegistry(data_root)
    registry.reconcile((first,))
    registry_path = data_root / "watched-roots-v1.json"
    caller_bytes = b'{"roots":[],"version":1}\n'
    real_publish = roots_module._publish_registry

    def replace_before_publish(
        path: Path,
        payload: bytes,
        *,
        expected_bytes: bytes | None,
        expected_identity: object,
        bound_data_root: object | None = None,
    ) -> None:
        path.write_bytes(caller_bytes)
        real_publish(
            path,
            payload,
            expected_bytes=expected_bytes,
            expected_identity=expected_identity,  # type: ignore[arg-type]
            bound_data_root=bound_data_root,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(roots_module, "_publish_registry", replace_before_publish)
    with pytest.raises(RootRegistryError) as raised:
        registry.reconcile((first, second))

    assert raised.value.code == "watched_root_registry_changed"
    assert registry_path.read_bytes() == caller_bytes
    assert not list(data_root.glob(".watched-roots-v1.json.*.tmp"))


def test_registry_change_at_final_replace_seam_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping an identity-and-byte comparison at the final replace seam must overwrite caller bytes."""
    data_root = tmp_path / "product"
    first = tmp_path / "first-final"
    second = tmp_path / "second-final"
    first.mkdir()
    second.mkdir()
    registry = WatchedRootRegistry(data_root)
    registry.reconcile((first,))
    registry_path = data_root / "watched-roots-v1.json"
    caller_bytes = b'{"roots":[],"version":1}\n'
    seam_seen: list[bool] = []

    def replace_at_seam(event: str) -> None:
        if event == "registry_before_replace":
            seam_seen.append(True)
            registry_path.write_bytes(caller_bytes)

    monkeypatch.setattr(roots_module, "_race_hook", replace_at_seam)
    with pytest.raises(RootRegistryError) as raised:
        registry.reconcile((first, second))

    assert seam_seen == [True]
    assert raised.value.code == "watched_root_registry_changed"
    assert registry_path.read_bytes() == caller_bytes


def test_registry_change_after_final_compare_is_restored_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the verified descriptor before replace must overwrite bytes installed after the final comparison."""
    data_root = tmp_path / "product"
    first = tmp_path / "compare-first"
    second = tmp_path / "compare-second"
    first.mkdir()
    second.mkdir()
    registry = WatchedRootRegistry(data_root)
    registry.reconcile((first,))
    registry_path = data_root / "watched-roots-v1.json"
    caller_bytes = b'{"roots":[],"version":1}\n'
    seam_seen: list[bool] = []

    def replace_after_compare(event: str) -> None:
        if event == "registry_after_final_compare":
            seam_seen.append(True)
            registry_path.write_bytes(caller_bytes)

    monkeypatch.setattr(roots_module, "_race_hook", replace_after_compare)
    with pytest.raises(RootRegistryError) as raised:
        registry.reconcile((first, second))

    assert seam_seen == [True]
    assert raised.value.code == "watched_root_registry_changed"
    assert registry_path.read_bytes() == caller_bytes


def test_outgoing_label_overflow_preserves_valid_registry(tmp_path: Path) -> None:
    """Publishing an unparseable next generic label must corrupt a previously valid registry."""
    data_root = tmp_path / "product"
    first = tmp_path / "label-first"
    second = tmp_path / "label-second"
    first.mkdir()
    second.mkdir()
    registry = WatchedRootRegistry(data_root)
    registry.reconcile((first,))
    registry_path = data_root / "watched-roots-v1.json"
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    document["roots"][0]["label"] = "Replay folder 999999999"
    original = (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()
    registry_path.write_bytes(original)

    with pytest.raises(RootRegistryError) as raised:
        registry.reconcile((first, second))

    assert raised.value.code == "watched_root_registry_invalid"
    assert registry_path.read_bytes() == original


def test_maximum_registry_entry_count_fits_registry_byte_bound() -> None:
    """Allowing more entries than the byte bound can serialize must make the declared capacity self-invalidating."""
    entries = [
        roots_module._RegistryEntry(f"{index:064x}", str(uuid4()), f"Replay folder {index + 1}")
        for index in range(roots_module._MAX_REGISTRY_ENTRIES)
    ]

    payload = roots_module._serialized_registry(entries)

    assert len(payload) <= roots_module._MAX_REGISTRY_BYTES
    assert roots_module._parse_registry(payload) == entries


def test_public_root_snapshot_is_frozen_and_path_free(tmp_path: Path) -> None:
    """Adding a source locator or path-key digest to the public snapshot must fail its disclosure contract."""
    source = tmp_path / "private-root"
    source.mkdir()
    root = WatchedRootRegistry(tmp_path / "product").reconcile((source,))[0]

    assert set(root.__dataclass_fields__) == {"root_public_id", "label", "available", "reason_code"}
    assert str(source) not in repr(root)
    with pytest.raises(FrozenInstanceError):
        root.label = "changed"  # type: ignore[misc]


def test_missing_or_unsafe_root_is_reported_without_locator_disclosure(tmp_path: Path) -> None:
    """Unavailable and symlink roots must degrade safely instead of leaking their configured paths."""
    data_root = tmp_path / "product"
    missing = tmp_path / "customer-missing-root"
    missing_snapshot = WatchedRootRegistry(data_root).reconcile((missing,))[0]

    assert missing_snapshot.available is False
    assert missing_snapshot.reason_code == "watched_root_unavailable"
    assert str(missing) not in repr(missing_snapshot)

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "customer-linked-root"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink/reparse creation is unavailable: {error}")
    linked_snapshot = WatchedRootRegistry(data_root).reconcile((linked,))[0]
    assert linked_snapshot.available is False
    assert linked_snapshot.reason_code == "watched_root_unsafe"
    assert str(linked) not in repr(linked_snapshot)


def test_configured_file_is_not_reported_as_an_available_root(tmp_path: Path) -> None:
    """Treating a regular file as a directory root must degrade the public root status."""
    configured_file = tmp_path / "not-a-root"
    configured_file.write_bytes(b"not a directory")

    snapshot = WatchedRootRegistry(tmp_path / "product").reconcile((configured_file,))[0]

    assert snapshot.available is False
    assert snapshot.reason_code == "watched_root_unsafe"


def test_filesystem_anchor_is_never_an_available_watched_root(tmp_path: Path) -> None:
    """Permitting a whole filesystem anchor would broaden selection beyond a bounded replay folder."""
    filesystem_anchor = Path(tmp_path.anchor)

    snapshot = WatchedRootRegistry(tmp_path / "product").reconcile((filesystem_anchor,))[0]

    assert snapshot.available is False
    assert snapshot.reason_code == "watched_root_unsafe"


def test_unavailable_filesystem_anchor_cannot_select_a_descendant(tmp_path: Path) -> None:
    """Keeping an unavailable anchor in the selectable map must expose any descendant as an import candidate."""
    selected = tmp_path / "anchor-selection.rep"
    selected.write_bytes(b"must remain outside an unavailable root")
    filesystem_anchor = Path(tmp_path.anchor)
    relative_name = selected.relative_to(filesystem_anchor).as_posix()
    registry = WatchedRootRegistry(tmp_path / "product")
    root_public_id = registry.reconcile((filesystem_anchor,))[0].root_public_id

    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, relative_name)

    assert raised.value.code in {"watched_root_unknown", "watched_root_unsafe"}
    assert not (registry.data_root / "ingress").exists()


def _configured_registry(
    tmp_path: Path, *, max_replay_bytes: int = 64 * 1024 * 1024
) -> tuple[WatchedRootRegistry, Path, str]:
    source_root = tmp_path / "private-source-root"
    source_root.mkdir()
    registry = WatchedRootRegistry(tmp_path / "product", max_replay_bytes=max_replay_bytes)
    public_id = registry.reconcile((source_root,))[0].root_public_id
    return registry, source_root, public_id


def _owned_temporary_files(data_root: Path) -> list[Path]:
    ingress = data_root / "ingress"
    if not ingress.exists():
        return []
    return [path for path in ingress.rglob("*.tmp") if path.is_file()]


def test_snapshot_copies_verified_descriptor_to_immutable_owned_content(tmp_path: Path) -> None:
    """Reopening the source path or returning it must break exact immutable ingress identity."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "league" / "week-1" / "match.rep"
    source.parent.mkdir(parents=True)
    payload = b"retail replay bytes" * 257
    source.write_bytes(payload)
    source_before = source.stat()

    snapshot = registry.snapshot_replay(root_public_id, "league/week-1/match.rep")

    digest = hashlib.sha256(payload).hexdigest()
    assert snapshot.sha256 == digest
    assert snapshot.size_bytes == len(payload)
    assert snapshot.root_public_id == root_public_id
    assert snapshot.relative_name == "league/week-1/match.rep"
    assert snapshot.created is True
    assert snapshot.snapshot_path == registry.data_root / "ingress" / digest[:2] / f"{digest}.rep"
    assert snapshot.snapshot_path.read_bytes() == payload
    assert snapshot.read_verified_bytes() == payload
    assert source.read_bytes() == payload
    assert source.stat().st_ino == source_before.st_ino
    assert source.stat().st_mtime_ns == source_before.st_mtime_ns
    assert str(source_root) not in repr(snapshot)
    assert str(registry.data_root) not in repr(snapshot)
    assert set(snapshot.__dataclass_fields__) == {
        "snapshot_path",
        "sha256",
        "size_bytes",
        "root_public_id",
        "relative_name",
        "created",
    }
    with pytest.raises(FrozenInstanceError):
        snapshot.size_bytes = 0  # type: ignore[misc]


def test_each_new_ingress_directory_fsyncs_its_parent_before_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating ingress directories without parent fsync must leave four namespace updates crash-ambiguous."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    (source_root / "durable.rep").write_bytes(b"durable replay")
    durable_events: list[str] = []

    def collect_durability(event: str) -> None:
        if event == "owned_directory_parent_fsynced":
            durable_events.append(event)

    monkeypatch.setattr(roots_module, "_race_hook", collect_durability)
    registry.snapshot_replay(root_public_id, "durable.rep")

    assert durable_events == ["owned_directory_parent_fsynced"] * 4


@pytest.mark.parametrize(
    "relative_name",
    [
        "",
        ".",
        "..",
        "../match.rep",
        "/absolute.rep",
        r"C:\private.rep",
        r"\\server\share\private.rep",
        "folder//match.rep",
        "folder/./match.rep",
        "folder/../match.rep",
        "match.REP",
        "match.rep.txt",
        "match.rep.rep",
        ".rep",
        "folder./match.rep",
        "folder /match.rep",
        "folder/name.rep:stream",
        "C:match.rep",
        "CON.rep",
        "NUL/match.rep",
        "folder/COM1.backup/match.rep",
        "match?.rep",
        "star*/match.rep",
        "less</match.rep",
        "greater>.rep",
        "pipe|.rep",
        "quote\"/match.rep",
        "control\x1f.rep",
        "%2e%2e/match.rep",
        "match.rep ",
        "match.rep.",
        "CON .rep",
        "COM\u00b9.rep",
        "LPT\u00b2.rep",
        "format\u202e.rep",
        "decomposed-e\u0301.rep",
    ],
)
def test_snapshot_rejects_nonportable_or_unsafe_relative_names(tmp_path: Path, relative_name: str) -> None:
    """Removing any closed-name check must allow a path with traversal or Windows alias semantics."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)

    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, relative_name)

    assert raised.value.code == "replay_relative_name_invalid"
    assert str(source_root) not in str(raised.value)
    if len(relative_name) > 3:
        assert relative_name not in repr(raised.value)


def test_formatted_traceback_never_contains_source_locator(tmp_path: Path) -> None:
    """Chained filesystem exceptions must not disclose the selected absolute path in formatted diagnostics."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    missing = source_root / "customer-secret-missing.rep"

    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, missing.name)

    formatted = "".join(traceback.format_exception(raised.value))
    assert str(source_root) not in formatted
    assert str(missing) not in formatted
    assert "customer-secret-missing.rep" not in formatted


def test_snapshot_rejects_unknown_or_disabled_root_id(tmp_path: Path) -> None:
    """Using an unconfigured opaque ID must never become a filesystem lookup."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    (source_root / "match.rep").write_bytes(b"replay")
    unknown = "123e4567-e89b-42d3-a456-426614174099"

    with pytest.raises(SnapshotIngressError) as unknown_error:
        registry.snapshot_replay(unknown, "match.rep")
    assert unknown_error.value.code == "watched_root_unknown"

    registry.reconcile(())
    with pytest.raises(SnapshotIngressError) as disabled_error:
        registry.snapshot_replay(root_public_id, "match.rep")
    assert disabled_error.value.code == "watched_root_unknown"


def test_snapshot_rejects_missing_leaf_and_non_directory_ancestor(tmp_path: Path) -> None:
    """Missing entries and file-valued ancestors must fail without path traversal or publication."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    with pytest.raises(SnapshotIngressError) as missing:
        registry.snapshot_replay(root_public_id, "missing.rep")
    assert missing.value.code == "replay_source_unavailable"

    (source_root / "round").write_bytes(b"not a directory")
    with pytest.raises(SnapshotIngressError) as ancestor:
        registry.snapshot_replay(root_public_id, "round/match.rep")
    assert ancestor.value.code == "replay_source_unsafe"


def test_snapshot_rejects_symlink_or_reparse_ancestor_without_copy(tmp_path: Path) -> None:
    """Following an ancestor alias must not let a relative selection leave the configured tree."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "match.rep").write_bytes(b"outside replay")
    linked = source_root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink/reparse creation is unavailable: {error}")

    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "linked/match.rep")

    assert raised.value.code == "replay_source_unsafe"
    assert not (registry.data_root / "ingress").exists()
    assert (outside / "match.rep").read_bytes() == b"outside replay"


def test_snapshot_rejects_symlink_leaf_and_revalidates_before_dedup(tmp_path: Path) -> None:
    """Returning an existing snapshot before rechecking the selected source must fail this test."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "match.rep"
    source.write_bytes(b"first replay")
    first = registry.snapshot_replay(root_public_id, "match.rep")
    source.unlink()
    outside = tmp_path / "outside.rep"
    outside.write_bytes(b"first replay")
    try:
        source.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink/reparse creation is unavailable: {error}")

    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "match.rep")

    assert raised.value.code == "replay_source_unsafe"
    assert first.snapshot_path.read_bytes() == b"first replay"


def test_snapshot_rejects_nonregular_and_hardlinked_leaf(tmp_path: Path) -> None:
    """Accepting a directory or multiply named inode must not produce replay evidence."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    (source_root / "directory.rep").mkdir()
    with pytest.raises(SnapshotIngressError) as nonregular:
        registry.snapshot_replay(root_public_id, "directory.rep")
    assert nonregular.value.code == "replay_source_not_regular"

    original = source_root / "original.rep"
    linked = source_root / "linked.rep"
    original.write_bytes(b"hardlinked replay")
    try:
        os.link(original, linked)
    except OSError as error:
        pytest.skip(f"hardlink creation is unavailable: {error}")
    with pytest.raises(SnapshotIngressError) as hardlink:
        registry.snapshot_replay(root_public_id, "linked.rep")
    assert hardlink.value.code == "replay_source_hardlinked"
    assert original.read_bytes() == b"hardlinked replay"


def test_snapshot_enforces_bound_before_and_during_copy(tmp_path: Path) -> None:
    """Reading beyond the configured replay bound must never publish an oversized snapshot."""
    registry, source_root, root_public_id = _configured_registry(tmp_path, max_replay_bytes=8)
    source = source_root / "large.rep"
    source.write_bytes(b"123456789")

    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "large.rep")

    assert raised.value.code == "replay_source_oversize"
    assert _owned_temporary_files(registry.data_root) == []
    assert source.read_bytes() == b"123456789"


@pytest.mark.skipif(os.name == "nt", reason="Windows source handles deliberately deny concurrent writers")
def test_snapshot_rejects_growth_beyond_bound_after_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source that grows after initial lstat must hit the streaming bound before publication."""
    registry, source_root, root_public_id = _configured_registry(tmp_path, max_replay_bytes=8)
    source = source_root / "growing.rep"
    source.write_bytes(b"12345678")

    def grow_after_open(event: str) -> None:
        if event == "after_open":
            with source.open("ab") as destination:
                destination.write(b"9")

    monkeypatch.setattr(roots_module, "_race_hook", grow_after_open)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "growing.rep")

    assert raised.value.code == "replay_source_oversize"
    assert _owned_temporary_files(registry.data_root) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows source handles deliberately deny concurrent writers")
def test_short_read_after_verified_open_fails_without_partial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing EOF before the lstat size must fail as a changed source, not a valid short replay."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "short.rep"
    source.write_bytes(b"authoritative bytes")

    def truncate_after_open(event: str) -> None:
        if event == "after_open":
            source.write_bytes(b"")

    monkeypatch.setattr(roots_module, "_race_hook", truncate_after_open)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "short.rep")

    assert raised.value.code == "replay_source_changed"
    assert _owned_temporary_files(registry.data_root) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode enforcement is platform specific")
def test_windows_source_handle_denies_same_size_write_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening with write/delete sharing must let another writer mutate selected bytes during copying."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "share-mode.rep"
    original = b"original replay"
    replacement = b"attacker replay"
    assert len(original) == len(replacement)
    source.write_bytes(original)
    blocked: list[bool] = []

    def attempt_write(event: str) -> None:
        if event == "after_open":
            try:
                source.write_bytes(replacement)
            except PermissionError:
                blocked.append(True)

    monkeypatch.setattr(roots_module, "_race_hook", attempt_write)
    snapshot = registry.snapshot_replay(root_public_id, "share-mode.rep")

    assert blocked == [True]
    assert snapshot.snapshot_path.read_bytes() == original
    assert source.read_bytes() == original


def test_transient_parent_swap_and_restore_after_traversal_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pathname-only walk must miss a parent swap that is restored before later lstat checks."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    parent = source_root / "round"
    parent.mkdir()
    source = parent / "match.rep"
    source.write_bytes(b"authorized replay")
    displaced = source_root / "round-displaced"
    attacker = source_root / "round-attacker"
    attacker.mkdir()
    (attacker / "match.rep").write_bytes(b"attacker replay")
    seam_seen: list[bool] = []

    def swap_and_restore(event: str) -> None:
        if event == "source_parent_bound":
            seam_seen.append(True)
            parent.rename(displaced)
            attacker.rename(parent)
            parent.rename(attacker)
            displaced.rename(parent)

    monkeypatch.setattr(roots_module, "_race_hook", swap_and_restore)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "round/match.rep")

    assert seam_seen == [True]
    assert raised.value.code == "replay_source_changed"
    assert not (registry.data_root / "ingress").exists()


def test_ancestor_swap_between_lstat_and_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping ancestor identity checks must allow a replacement tree into immutable ingress."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    ancestor = source_root / "round"
    ancestor.mkdir()
    source = ancestor / "match.rep"
    source.write_bytes(b"authorized replay")
    displaced = source_root / "displaced"

    def swap_ancestor(event: str) -> None:
        if event == "before_open":
            ancestor.rename(displaced)
            ancestor.mkdir()
            (ancestor / "match.rep").write_bytes(b"replacement replay")

    monkeypatch.setattr(roots_module, "_race_hook", swap_ancestor)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "round/match.rep")

    assert raised.value.code == "replay_source_changed"
    assert _owned_temporary_files(registry.data_root) == []
    assert (displaced / "match.rep").read_bytes() == b"authorized replay"


def test_watched_root_replacement_after_reconcile_is_rejected(tmp_path: Path) -> None:
    """Retaining only a watched-root pathname must authorize a different directory installed after reconciliation."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    displaced = tmp_path / "source-displaced"
    source_root.rename(displaced)
    source_root.mkdir()
    (source_root / "match.rep").write_bytes(b"replacement replay")

    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "match.rep")

    assert raised.value.code == "replay_source_changed"
    assert not (registry.data_root / "ingress").exists()


def test_file_replacement_between_lstat_and_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comparing only size or suffix must not accept a different inode opened under the same name."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "match.rep"
    source.write_bytes(b"first inode bytes")

    def replace_file(event: str) -> None:
        if event == "before_open":
            source.unlink()
            source.write_bytes(b"second inode byte")

    monkeypatch.setattr(roots_module, "_race_hook", replace_file)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "match.rep")

    assert raised.value.code == "replay_source_changed"
    assert _owned_temporary_files(registry.data_root) == []


def test_source_disappearance_before_open_and_after_copy_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing pre-open or post-copy name revalidation must not publish an unbound descriptor."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "vanishing.rep"
    source.write_bytes(b"vanishing replay")

    def remove_before_open(event: str) -> None:
        if event == "before_open":
            source.unlink()

    monkeypatch.setattr(roots_module, "_race_hook", remove_before_open)
    with pytest.raises(SnapshotIngressError) as before_open:
        registry.snapshot_replay(root_public_id, "vanishing.rep")
    assert before_open.value.code == "replay_source_changed"

    source.write_bytes(b"vanishing replay")

    def remove_after_copy(event: str) -> None:
        if event == "after_copy":
            try:
                source.unlink()
            except PermissionError as error:
                pytest.skip(f"open-file replacement is unavailable on this host: {error}")

    monkeypatch.setattr(roots_module, "_race_hook", remove_after_copy)
    with pytest.raises(SnapshotIngressError) as after_copy:
        registry.snapshot_replay(root_public_id, "vanishing.rep")
    assert after_copy.value.code in {"replay_source_unavailable", "replay_source_changed"}
    assert _owned_temporary_files(registry.data_root) == []


def test_identical_snapshot_deduplicates_only_after_source_revalidation(tmp_path: Path) -> None:
    """No-replace publication must reuse exact bytes without rewriting the immutable target."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "same.rep"
    source.write_bytes(b"same replay bytes")
    first = registry.snapshot_replay(root_public_id, "same.rep")
    first_timestamp = first.snapshot_path.stat().st_mtime_ns

    second = registry.snapshot_replay(root_public_id, "same.rep")

    assert second == IngressSnapshot(
        snapshot_path=first.snapshot_path,
        sha256=first.sha256,
        size_bytes=first.size_bytes,
        root_public_id=root_public_id,
        relative_name="same.rep",
        created=False,
    )
    assert second.snapshot_path.stat().st_mtime_ns == first_timestamp
    assert _owned_temporary_files(registry.data_root) == []


def test_snapshot_publication_waits_for_same_digest_cross_process_lock(tmp_path: Path) -> None:
    """Removing digest-lock use from snapshot publication must let it complete while another process owns the lock."""
    registry, source_root, _root_public_id = _configured_registry(tmp_path)
    source = source_root / "serialized.rep"
    payload = b"serialized replay bytes"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    lock_directory = registry.data_root / "ingress" / ".locks"
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        holder = executor.submit(
            _hold_digest_lock_in_spawned_process,
            (str(lock_directory), digest, str(coordination)),
        )
        deadline = time.monotonic() + 10
        while not (coordination / "ready").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        publisher = executor.submit(
            _snapshot_in_spawned_process,
            (str(registry.data_root), str(source_root), source.name, str(coordination)),
        )
        deadline = time.monotonic() + 10
        while not (coordination / "publisher-started").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.2)
        was_blocked = publisher.done() is False
        (coordination / "release").write_bytes(b"release")
        holder.result(timeout=10)
        published_digest, created = publisher.result(timeout=10)

    assert was_blocked is True
    assert published_digest == digest
    assert created is True


def test_concurrent_identical_snapshots_have_one_created_winner(tmp_path: Path) -> None:
    """Removing per-digest serialization must make publication outcome ambiguous under two processes."""
    registry, source_root, _root_public_id = _configured_registry(tmp_path)
    source = source_root / "same-processes.rep"
    payload = b"same bytes from two processes"
    source.write_bytes(payload)
    arguments = (str(registry.data_root), str(source_root), source.name, None)

    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as executor:
        results = tuple(executor.map(_snapshot_in_spawned_process, (arguments, arguments)))

    assert {digest for digest, _created in results} == {hashlib.sha256(payload).hexdigest()}
    assert sorted(created for _digest, created in results) == [False, True]


def test_different_digest_publication_is_not_blocked_cross_process(tmp_path: Path) -> None:
    """Using one global publication lock must unnecessarily block an unrelated replay digest."""
    registry, source_root, _root_public_id = _configured_registry(tmp_path)
    held_payload = b"held digest"
    published_payload = b"independent digest"
    held_digest = hashlib.sha256(held_payload).hexdigest()
    source = source_root / "independent.rep"
    source.write_bytes(published_payload)
    lock_directory = registry.data_root / "ingress" / ".locks"
    coordination = tmp_path / "different-coordination"
    coordination.mkdir()
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        holder = executor.submit(
            _hold_digest_lock_in_spawned_process,
            (str(lock_directory), held_digest, str(coordination)),
        )
        deadline = time.monotonic() + 10
        while not (coordination / "ready").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        publisher = executor.submit(
            _snapshot_in_spawned_process,
            (str(registry.data_root), str(source_root), source.name, str(coordination)),
        )
        published_digest, created = publisher.result(timeout=10)
        (coordination / "release").write_bytes(b"release")
        holder.result(timeout=10)

    assert published_digest == hashlib.sha256(published_payload).hexdigest()
    assert created is True


@pytest.mark.skipif(os.name != "nt", reason="Windows directory share-mode enforcement is platform specific")
def test_digest_lock_directory_cannot_be_replaced_to_split_cross_process_lock(tmp_path: Path) -> None:
    """Opening the digest lock only by pathname must let a replacement `.locks` directory split serialization."""
    lock_directory = tmp_path / "ingress" / ".locks"
    displaced = tmp_path / "ingress" / ".locks-displaced"
    digest = hashlib.sha256(b"split lock").hexdigest()
    first_ready = tmp_path / "first-ready"
    first_release = tmp_path / "first-release"
    second_ready = tmp_path / "second-ready"
    second_release = tmp_path / "second-release"
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        holder = executor.submit(
            _hold_named_digest_lock_in_spawned_process,
            (str(lock_directory), digest, str(first_ready), str(first_release)),
        )
        deadline = time.monotonic() + 10
        while not first_ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        replacement_blocked = False
        try:
            lock_directory.rename(displaced)
        except PermissionError:
            replacement_blocked = True
        second = None
        if not replacement_blocked:
            lock_directory.mkdir()
            second = executor.submit(
                _hold_named_digest_lock_in_spawned_process,
                (str(lock_directory), digest, str(second_ready), str(second_release)),
            )
            deadline = time.monotonic() + 2
            while not second_ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        second_release.write_bytes(b"release")
        first_release.write_bytes(b"release")
        if second is not None:
            try:
                second.result(timeout=10)
            except SnapshotIngressError:
                pass
        try:
            holder.result(timeout=10)
        except SnapshotIngressError:
            pass

    assert replacement_blocked is True
    assert lock_directory.is_dir()
    assert not displaced.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory share-mode enforcement is platform specific")
def test_digest_lock_binds_locks_directory_before_opening_lock_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pathname open after directory validation must follow a replacement installed before the lock open."""
    lock_directory = tmp_path / "ingress" / ".locks"
    displaced = tmp_path / "ingress" / ".locks-displaced"
    seam_seen: list[bool] = []
    replacement_blocked: list[bool] = []

    def replace_after_bind(event: str) -> None:
        if event == "digest_lock_directory_bound":
            seam_seen.append(True)
            try:
                lock_directory.rename(displaced)
            except PermissionError:
                replacement_blocked.append(True)

    monkeypatch.setattr(roots_module, "_race_hook", replace_after_bind)
    with roots_module._digest_lock(lock_directory, hashlib.sha256(b"bound lock").hexdigest()):
        pass

    assert seam_seen == [True]
    assert replacement_blocked == [True]
    assert lock_directory.is_dir()
    assert not displaced.exists()


def test_corrupt_snapshot_collision_is_preserved_and_owned_temp_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overwriting an occupied digest path or leaving transaction bytes must fail this test."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "collision.rep"
    payload = b"wanted replay bytes"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    target = registry.data_root / "ingress" / digest[:2] / f"{digest}.rep"

    def occupy_target(event: str) -> None:
        if event == "before_publish":
            target.write_bytes(b"caller-owned collision")

    monkeypatch.setattr(roots_module, "_race_hook", occupy_target)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "collision.rep")

    assert raised.value.code == "ingress_snapshot_collision"
    assert target.read_bytes() == b"caller-owned collision"
    assert source.read_bytes() == payload
    assert _owned_temporary_files(registry.data_root) == []


def test_staging_directory_replacement_before_copy_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reopening staging by pathname must let a replacement directory receive owned temporary bytes."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "staging-race.rep"
    source.write_bytes(b"staging race replay")
    real_copy = roots_module._copy_descriptor
    displaced = registry.data_root / "ingress" / ".staging-displaced"

    def replace_staging(
        source_descriptor: int,
        staging: Path,
        maximum: int,
        **kwargs: object,
    ) -> tuple[Path, str, int, int]:
        staging.rename(displaced)
        staging.mkdir()
        return real_copy(source_descriptor, staging, maximum, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(roots_module, "_copy_descriptor", replace_staging)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "staging-race.rep")

    assert raised.value.code == "ingress_snapshot_changed"
    assert _owned_temporary_files(registry.data_root) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows directory share-mode enforcement is platform specific")
def test_failure_cleanup_keeps_verified_staging_parent_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing staging handles before temp cleanup must let cleanup follow a replacement parent pathname."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    (source_root / "cleanup-race.rep").write_bytes(b"cleanup race replay")
    staging = registry.data_root / "ingress" / ".staging"
    displaced = registry.data_root / "ingress" / ".staging-displaced"
    replacement_blocked: list[bool] = []

    def fail_then_replace(event: str) -> None:
        if event == "after_copy":
            raise SnapshotIngressError("ingress_snapshot_changed")
        if event == "before_temporary_cleanup":
            try:
                staging.rename(displaced)
            except PermissionError:
                replacement_blocked.append(True)

    monkeypatch.setattr(roots_module, "_race_hook", fail_then_replace)
    with pytest.raises(SnapshotIngressError):
        registry.snapshot_replay(root_public_id, "cleanup-race.rep")

    assert replacement_blocked == [True]
    assert staging.is_dir()
    assert not displaced.exists()
    assert _owned_temporary_files(registry.data_root) == []


def test_shard_replacement_before_publish_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing through a replaced shard pathname must not return a falsely verified snapshot."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "shard-race.rep"
    payload = b"shard race replay"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    shard = registry.data_root / "ingress" / digest[:2]
    displaced = registry.data_root / "ingress" / f"{digest[:2]}-displaced"

    def replace_shard(event: str) -> None:
        if event == "before_publish":
            shard.rename(displaced)
            shard.mkdir()

    monkeypatch.setattr(roots_module, "_race_hook", replace_shard)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "shard-race.rep")

    assert raised.value.code == "ingress_snapshot_changed"
    assert not (shard / f"{digest}.rep").exists()
    assert _owned_temporary_files(registry.data_root) == []


def test_final_target_replacement_after_publish_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning without a final bound reopen must accept bytes swapped after publication verification."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "final-race.rep"
    payload = b"verified snapshot bytes"
    source.write_bytes(payload)
    real_publish = roots_module._publish_snapshot

    def replace_after_publish(
        temporary: Path,
        target: Path,
        digest: str,
        size: int,
        **kwargs: object,
    ) -> bool:
        created = real_publish(temporary, target, digest, size, **kwargs)  # type: ignore[arg-type]
        target.unlink()
        target.write_bytes(b"attacker replacement")
        return created

    monkeypatch.setattr(roots_module, "_publish_snapshot", replace_after_publish)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "final-race.rep")

    assert raised.value.code in {"ingress_snapshot_changed", "ingress_snapshot_collision"}
    assert _owned_temporary_files(registry.data_root) == []


def test_snapshot_consumer_reopens_and_reverifies_owned_bytes_after_return(tmp_path: Path) -> None:
    """A consumer that trusts the returned pathname must accept replacement bytes installed after publication."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "consumer.rep"
    source.write_bytes(b"verified consumer replay")
    snapshot = registry.snapshot_replay(root_public_id, source.name)
    snapshot.snapshot_path.unlink()
    snapshot.snapshot_path.write_bytes(b"replacement consumer bytes")

    with pytest.raises(SnapshotIngressError) as raised:
        snapshot.read_verified_bytes()

    assert raised.value.code == "ingress_snapshot_collision"


def test_target_replacement_after_final_verify_is_rejected_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning immediately after verification must expose a target replaced at the final return boundary."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    payload = b"final boundary replay"
    (source_root / "final-boundary.rep").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    target = registry.data_root / "ingress" / digest[:2] / f"{digest}.rep"
    seam_seen: list[bool] = []

    def replace_at_return(event: str) -> None:
        if event == "after_final_verify":
            seam_seen.append(True)
            target.unlink()
            target.write_bytes(b"return-boundary replacement")

    monkeypatch.setattr(roots_module, "_race_hook", replace_at_return)
    with pytest.raises(SnapshotIngressError) as raised:
        registry.snapshot_replay(root_public_id, "final-boundary.rep")

    assert seam_seen == [True]
    assert raised.value.code == "ingress_snapshot_collision"


def test_poisoned_ingress_directory_and_nonregular_target_fail_closed(tmp_path: Path) -> None:
    """Owned storage aliases and occupied non-files must never be replaced during publication."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    source = source_root / "poison.rep"
    payload = b"poison test replay"
    source.write_bytes(payload)
    ingress = registry.data_root / "ingress"
    ingress.parent.mkdir(parents=True, exist_ok=True)
    ingress.write_bytes(b"not a directory")
    with pytest.raises(SnapshotIngressError) as poisoned:
        registry.snapshot_replay(root_public_id, "poison.rep")
    assert poisoned.value.code == "ingress_snapshot_unavailable"
    assert ingress.read_bytes() == b"not a directory"

    ingress.unlink()
    digest = hashlib.sha256(payload).hexdigest()
    target = ingress / digest[:2] / f"{digest}.rep"
    target.mkdir(parents=True)
    with pytest.raises(SnapshotIngressError) as occupied:
        registry.snapshot_replay(root_public_id, "poison.rep")
    assert occupied.value.code == "ingress_snapshot_collision"
    assert target.is_dir()


def test_registry_rejects_nonpositive_replay_bound_without_creating_state(tmp_path: Path) -> None:
    """A zero byte bound must not create a registry that can never admit a valid replay."""
    data_root = tmp_path / "product"
    with pytest.raises(ValueError, match="positive"):
        WatchedRootRegistry(data_root, max_replay_bytes=0)
    assert not data_root.exists()


def test_windows_reparse_attribute_is_recognized_synthetically() -> None:
    """Ignoring FILE_ATTRIBUTE_REPARSE_POINT must fail even when symlink privileges are unavailable."""
    info = SimpleNamespace(st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    assert roots_module._is_reparse(cast(os.stat_result, info)) is True


@pytest.mark.parametrize(
    ("site", "expected_code"),
    [
        ("registry", "watched_root_registry_invalid"),
        ("source", "replay_source_unsafe"),
        ("ingress", "ingress_snapshot_unavailable"),
        ("locks", "ingress_snapshot_unavailable"),
    ],
)
def test_synthetic_reparse_identity_fails_at_each_enforcement_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    expected_code: str,
) -> None:
    """Calling the reparse helper without wiring every security site must allow one synthetic alias through."""
    registry, source_root, root_public_id = _configured_registry(tmp_path)
    (source_root / "synthetic.rep").write_bytes(b"synthetic reparse replay")

    monkeypatch.setattr(
        roots_module,
        "_enforce_reparse_identity",
        lambda enforcement_site, marked: marked or enforcement_site == site,
    )

    if site == "registry":
        operation = lambda: registry.reconcile((source_root,))
    else:
        operation = lambda: registry.snapshot_replay(root_public_id, "synthetic.rep")

    error_type = RootRegistryError if site == "registry" else SnapshotIngressError
    with pytest.raises(error_type) as raised:
        operation()

    assert raised.value.code == expected_code
