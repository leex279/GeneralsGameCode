"""Revisioned, external, canonical settings persistence contracts."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from generals_replay_analyzer.configuration import (
    ConfigurationStore,
    SettingChange,
    SettingsImpact,
    SettingsStoreError,
    normalize_ollama_endpoint,
    validate_ollama_model_name,
)


def _store(tmp_path: Path, **kwargs: object) -> ConfigurationStore:
    return ConfigurationStore(configuration_root=tmp_path / "external-config", **kwargs)


def _directory_alias(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OSError("directory alias creation unavailable")


def test_missing_document_reads_revision_zero_without_creating_directories(tmp_path: Path) -> None:
    """Catch a GET-like read that creates product state or invents a persisted revision."""
    root = tmp_path / "external-config"
    store = ConfigurationStore(configuration_root=root)

    snapshot = store.read()

    assert snapshot.schema_version == 1
    assert snapshot.revision == 0
    assert snapshot.values == (
        ("import_mode", "copy"),
        ("minimum_longitudinal_sample_size", 5),
        ("movement_sample_frames", 15),
        ("ollama_model", "qwen3.6:27b"),
        ("ollama_url", "http://127.0.0.1:11434"),
    )
    assert snapshot.sources == tuple((key, "default") for key, _value in snapshot.values)
    assert len(snapshot.effective_settings_digest) == 64
    assert not root.exists()


def test_apply_writes_exact_canonical_document_and_one_revision(tmp_path: Path) -> None:
    """Catch noncanonical JSON, partial documents, or a revision increment other than one."""
    store = _store(tmp_path)

    mutation = store.apply(
        expected_revision=0,
        changes=(SettingChange("ollama_url", "http://[::1]:22434/"),),
    )

    assert mutation.result_code == "updated"
    assert mutation.snapshot.revision == 1
    assert mutation.snapshot.value("ollama_url") == "http://[::1]:22434"
    assert mutation.snapshot.source("ollama_url") == "persisted"
    assert store.document_path.read_bytes() == (
        b'{"revision":1,"schema_version":1,"values":{"import_mode":"copy",'
        b'"minimum_longitudinal_sample_size":5,"movement_sample_frames":15,'
        b'"ollama_model":"qwen3.6:27b","ollama_url":"http://[::1]:22434"}}\n'
    )


def test_noop_does_not_create_a_document_or_increment_revision(tmp_path: Path) -> None:
    """Catch a semantically unchanged update that dirties configuration identity."""
    store = _store(tmp_path)

    mutation = store.apply(
        expected_revision=0,
        changes=(SettingChange("ollama_url", "http://127.0.0.1:11434/"),),
    )

    assert mutation.result_code == "unchanged"
    assert mutation.snapshot.revision == 0
    assert not store.document_path.exists()


def test_preview_is_write_free_canonical_and_maps_exact_affected_families(tmp_path: Path) -> None:
    """Catch route-side impact calculation or a preview that creates persistent state."""
    store = _store(tmp_path)

    impact = store.preview(
        expected_revision=0,
        changes=(
            SettingChange("ollama_model", "model:2"),
            SettingChange("movement_sample_frames", 30),
        ),
    )

    assert isinstance(impact, SettingsImpact)
    assert impact.expected_revision == 0
    assert impact.normalized_changes == (
        SettingChange("movement_sample_frames", 30),
        SettingChange("ollama_model", "model:2"),
    )
    assert impact.affected_stage_families == (
        "telemetry",
        "spatial",
        "features",
        "strategy",
        "longitudinal",
        "ollama",
        "report",
    )
    assert impact.invalidates_existing_results is True
    assert impact.requires_confirmation is True
    assert impact.restart_required is True
    assert len(impact.impact_digest) == 64
    assert not store.document_path.exists()


def test_confirmed_apply_requires_exact_impact_digest_and_literal_confirmation(tmp_path: Path) -> None:
    """Catch a changed or unconfirmed impact being persisted after preview."""
    store = _store(tmp_path)
    changes = (SettingChange("import_mode", "reference"),)
    impact = store.preview(expected_revision=0, changes=changes)

    with pytest.raises(SettingsStoreError) as missing_confirmation:
        store.apply_confirmed(
            expected_revision=0,
            changes=changes,
            expected_impact_digest=impact.impact_digest,
            confirm_invalidating_change=False,
        )
    with pytest.raises(SettingsStoreError) as wrong_digest:
        store.apply_confirmed(
            expected_revision=0,
            changes=changes,
            expected_impact_digest="f" * 64,
            confirm_invalidating_change=True,
        )

    assert missing_confirmation.value.code == "settings_confirmation_required"
    assert wrong_digest.value.code == "settings_impact_conflict"
    assert not store.document_path.exists()


def test_confirmed_apply_recomputes_preview_under_lock(tmp_path: Path) -> None:
    """Catch an apply trusting browser impact fields instead of its own versioned policy."""
    store = _store(tmp_path)
    changes = (SettingChange("movement_sample_frames", 30),)
    impact = store.preview(expected_revision=0, changes=changes)

    mutation = store.apply_confirmed(
        expected_revision=0,
        changes=changes,
        expected_impact_digest=impact.impact_digest,
        confirm_invalidating_change=True,
    )

    assert mutation.result_code == "updated"
    assert mutation.snapshot.revision == 1
    assert store.read().value("movement_sample_frames") == 30


@pytest.mark.parametrize(
    "body",
    [
        b"not-json\n",
        b'{"schema_version":1,"revision":0,"values":{},"extra":true}\n',
        b'{"schema_version":1,"revision":true,"values":{}}\n',
        b'{"schema_version":2,"revision":0,"values":{}}\n',
        b'{"schema_version":1,"revision":0,"values":{"unknown":"value"}}\n',
        b'{"schema_version":1,"revision":0,"revision":1,"values":{}}\n',
        b'{"schema_version":1,"revision":0,"values":{"import_mode":"copy","import_mode":"reference"}}\n',
    ],
)
def test_invalid_existing_document_fails_closed(tmp_path: Path, body: bytes) -> None:
    """Catch malformed, duplicate, unknown, or noncanonical state being silently accepted."""
    store = _store(tmp_path)
    store.document_path.parent.mkdir(parents=True)
    store.document_path.write_bytes(body)

    with pytest.raises(SettingsStoreError) as caught:
        store.read()

    assert caught.value.code == "settings_document_invalid"
    assert str(caught.value) == "settings_document_invalid"
    assert store.document_path.read_bytes() == body


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("unknown", "value"),
        ("movement_sample_frames", True),
        ("movement_sample_frames", 0),
        ("movement_sample_frames", 3601),
        ("minimum_longitudinal_sample_size", 0),
        ("minimum_longitudinal_sample_size", 100_001),
        ("import_mode", "move"),
        ("ollama_url", "http://localhost:11434"),
        ("ollama_model", "../escape"),
    ],
)
def test_apply_rejects_every_value_outside_the_closed_scalar_contract(
    tmp_path: Path,
    key: str,
    value: object,
) -> None:
    """Catch path-bearing, bool-as-int, out-of-range, and open-ended setting mutations."""
    store = _store(tmp_path)

    with pytest.raises(SettingsStoreError) as caught:
        store.apply(expected_revision=0, changes=(SettingChange(key, value),))

    assert caught.value.code == "settings_change_invalid"
    assert not store.document_path.exists()


def test_duplicate_changes_are_rejected_before_persistence(tmp_path: Path) -> None:
    """Catch ambiguous last-write-wins behavior inside one optimistic command."""
    store = _store(tmp_path)

    with pytest.raises(SettingsStoreError) as caught:
        store.apply(
            expected_revision=0,
            changes=(SettingChange("import_mode", "copy"), SettingChange("import_mode", "reference")),
        )

    assert caught.value.code == "settings_change_invalid"


def test_stale_revision_cannot_overwrite_a_newer_document(tmp_path: Path) -> None:
    """Catch a second store instance applying against a stale optimistic revision."""
    first = _store(tmp_path)
    second = _store(tmp_path)
    first.apply(expected_revision=0, changes=(SettingChange("import_mode", "reference"),))

    with pytest.raises(SettingsStoreError) as caught:
        second.apply(expected_revision=0, changes=(SettingChange("movement_sample_frames", 30),))

    assert caught.value.code == "settings_revision_conflict"
    assert first.read().revision == 1
    assert first.read().value("movement_sample_frames") == 15


def test_two_store_instances_racing_one_revision_have_one_winner(tmp_path: Path) -> None:
    """Catch revision checks performed before rather than under the interprocess lock."""
    stores = (_store(tmp_path), _store(tmp_path))
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def apply(store: ConfigurationStore, frames: int) -> None:
        barrier.wait()
        try:
            store.apply(expected_revision=0, changes=(SettingChange("movement_sample_frames", frames),))
        except SettingsStoreError as exc:
            outcomes.append(exc.code)
        else:
            outcomes.append("updated")

    threads = (
        threading.Thread(target=apply, args=(stores[0], 20)),
        threading.Thread(target=apply, args=(stores[1], 30)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["settings_revision_conflict", "updated"]
    assert stores[0].read().revision == 1


def test_lock_contention_is_bounded_and_uses_a_stable_code(tmp_path: Path) -> None:
    """Catch an indefinitely blocking mutation when another process owns the lock."""
    store = _store(tmp_path, lock_timeout_seconds=0.01)
    store.lock_path.parent.mkdir(parents=True)
    store.lock_path.write_text("occupied", encoding="utf-8")

    with pytest.raises(SettingsStoreError) as caught:
        store.apply(expected_revision=0, changes=(SettingChange("import_mode", "reference"),))

    assert caught.value.code == "settings_busy"
    assert not store.document_path.exists()


def test_temp_is_exclusive_fsynced_before_replace_and_only_owned_temp_is_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch nonexclusive temporary writes, replace-before-flush, or broad cleanup."""
    store = _store(tmp_path)
    events: list[str] = []
    open_flags: list[int] = []
    real_open = os.open
    real_fsync = os.fsync
    real_replace = os.replace

    def tracked_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777) -> int:
        if str(path).endswith(".tmp"):
            open_flags.append(flags)
        return real_open(path, flags, mode)

    def tracked_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def tracked_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr("generals_replay_analyzer.configuration.store.os.open", tracked_open)
    monkeypatch.setattr("generals_replay_analyzer.configuration.store.os.fsync", tracked_fsync)
    monkeypatch.setattr("generals_replay_analyzer.configuration.store.os.replace", tracked_replace)

    store.apply(expected_revision=0, changes=(SettingChange("import_mode", "reference"),))

    assert open_flags and all(flags & os.O_EXCL for flags in open_flags)
    assert events.index("fsync") < events.index("replace")


def test_replace_failure_retains_old_document_and_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch loss of the last valid document or deletion of an unowned neighboring file."""
    store = _store(tmp_path)
    store.apply(expected_revision=0, changes=(SettingChange("import_mode", "reference"),))
    original = store.document_path.read_bytes()
    innocent = store.document_path.parent / "settings-v1.keep.tmp"
    innocent.write_text("keep", encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("C:/private/secret replace failure")

    monkeypatch.setattr("generals_replay_analyzer.configuration.store.os.replace", fail_replace)

    with pytest.raises(SettingsStoreError) as caught:
        store.apply(expected_revision=1, changes=(SettingChange("movement_sample_frames", 30),))

    assert caught.value.code == "settings_write_failed"
    assert str(caught.value) == "settings_write_failed"
    assert caught.value.__cause__ is None
    assert store.document_path.read_bytes() == original
    assert innocent.read_text(encoding="utf-8") == "keep"
    assert tuple(path for path in store.document_path.parent.iterdir() if path.name.endswith(".tmp")) == (innocent,)


def test_environment_precedence_is_visible_and_read_only(tmp_path: Path) -> None:
    """Catch persisted settings overriding environment or a web write pretending to take effect."""
    environment = {
        "GENERALS_REPLAY_ANALYZER_OLLAMA_MODEL": "environment-model:1",
        "GENERALS_REPLAY_ANALYZER_MOVEMENT_SAMPLE_FRAMES": "45",
    }
    store = _store(tmp_path, environment=environment)

    snapshot = store.read()

    assert snapshot.value("ollama_model") == "environment-model:1"
    assert snapshot.source("ollama_model") == "environment"
    assert snapshot.value("movement_sample_frames") == 45
    with pytest.raises(SettingsStoreError) as caught:
        store.apply(expected_revision=0, changes=(SettingChange("ollama_model", "other:1"),))
    assert caught.value.code == "settings_overridden_by_environment"
    assert "environment-model" not in str(caught.value)


def test_stale_revision_precedes_environment_override_refusal(tmp_path: Path) -> None:
    """Catch a stale command learning current environment override state before concurrency validation."""
    store = _store(
        tmp_path,
        environment={"GENERALS_REPLAY_ANALYZER_OLLAMA_MODEL": "environment-model:1"},
    )
    writer = _store(tmp_path)
    writer.apply(expected_revision=0, changes=(SettingChange("import_mode", "reference"),))

    with pytest.raises(SettingsStoreError) as caught:
        store.apply(expected_revision=0, changes=(SettingChange("ollama_model", "other:1"),))

    assert caught.value.code == "settings_revision_conflict"


def test_configuration_root_inside_git_checkout_is_rejected_without_creation(tmp_path: Path) -> None:
    """Catch canonical settings being written into any Git working tree."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    target = checkout / "runtime-config"
    store = ConfigurationStore(configuration_root=target)

    with pytest.raises(SettingsStoreError) as caught:
        store.apply(expected_revision=0, changes=(SettingChange("import_mode", "reference"),))

    assert caught.value.code == "settings_location_unsafe"
    assert not target.exists()
    assert str(checkout) not in str(caught.value)


def test_read_rejects_broken_document_alias_instead_of_returning_defaults(tmp_path: Path) -> None:
    """A broken leaf symlink is an unsafe configured identity, not a missing document."""

    store = _store(tmp_path)
    store.document_path.parent.mkdir(parents=True)
    try:
        store.document_path.symlink_to(tmp_path / "missing-settings.json")
    except OSError as error:
        try:
            _directory_alias(store.document_path, tmp_path / "missing-settings-directory")
        except OSError:
            pytest.skip(f"broken alias creation unavailable: {error}")

    with pytest.raises(SettingsStoreError) as caught:
        store.read()

    assert caught.value.code == "settings_document_invalid"


def test_read_rejects_aliased_ancestor_even_for_canonical_document(tmp_path: Path) -> None:
    """Document reads must not traverse a directory symlink or junction outside the configured identity."""

    actual_root = tmp_path / "actual-configuration"
    writer = ConfigurationStore(configuration_root=actual_root, environment={})
    writer.apply(expected_revision=0, changes=(SettingChange("import_mode", "reference"),))
    alias_root = tmp_path / "aliased-configuration"
    try:
        _directory_alias(alias_root, actual_root)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")

    with pytest.raises(SettingsStoreError) as caught:
        ConfigurationStore(configuration_root=alias_root, environment={}).read()

    assert caught.value.code == "settings_document_invalid"


def test_snapshot_and_changes_are_immutable(tmp_path: Path) -> None:
    """Catch mutable settings values crossing the application boundary."""
    snapshot = _store(tmp_path).read()
    change = SettingChange("import_mode", "copy")

    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 7  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        change.value = "reference"  # type: ignore[misc]


@pytest.mark.parametrize("value", ["http://127.0.0.1:1", "http://127.0.0.1:65535/", "http://[::1]:11434/"])
def test_loopback_endpoint_normalization_accepts_only_literal_supported_forms(value: str) -> None:
    """Catch rejection of a valid literal endpoint or preservation of a trailing slash."""
    assert normalize_ollama_endpoint(value) == value.removesuffix("/")


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:11434",
        "http://localhost:11434",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:11434/path",
        "http://127.0.0.1:11434?query=x",
        "http://user@127.0.0.1:11434",
        "http://2130706433:11434",
        "http://0177.0.0.1:11434",
        "http://0x7f000001:11434",
        "http://[::ffff:127.0.0.1]:11434",
        "http://[::1%25zone]:11434",
        "http://0.0.0.0:11434",
        "http://127.0.0.1:11434\\escape",
        "http://127.0.0.1:11434\n",
    ],
)
def test_loopback_endpoint_normalization_rejects_aliases_and_url_features(value: str) -> None:
    """Catch endpoint syntax that could resolve remotely or smuggle request state."""
    with pytest.raises(SettingsStoreError) as caught:
        normalize_ollama_endpoint(value)
    assert caught.value.code == "settings_change_invalid"


@pytest.mark.parametrize("value", ["qwen3.6:27b", "namespace/model-name:latest", "model_1.2"])
def test_model_name_accepts_the_closed_ascii_identifier_grammar(value: str) -> None:
    """Catch accidental rejection of accepted opaque local model identifiers."""
    assert validate_ollama_model_name(value) == value


@pytest.mark.parametrize("value", ["", "../model", " model", "model ", "model;calc", "mödel", "a" * 256])
def test_model_name_rejects_path_shell_control_and_oversize_values(value: str) -> None:
    """Catch a model field being treated as a path, shell fragment, or unbounded value."""
    with pytest.raises(SettingsStoreError) as caught:
        validate_ollama_model_name(value)
    assert caught.value.code == "settings_change_invalid"
