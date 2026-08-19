from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

ARCHIVE_SCRIPT = Path(__file__).parents[1] / "tools" / "archive_legacy_outputs.ps1"


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, check=False, text=True, encoding="utf-8", timeout=15)


def _make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    assert _run(["git", "init", "--quiet"], repository).returncode == 0
    assert _run(["git", "config", "user.email", "tests@example.invalid"], repository).returncode == 0
    assert _run(["git", "config", "user.name", "Replay Analyzer Tests"], repository).returncode == 0
    return repository


def _inventory_entry(repository: Path, relative_path: str, category: str = "video") -> dict[str, Any]:
    source = repository / Path(relative_path.replace("/", "\\"))
    data = source.read_bytes()
    return {
        "path": relative_path,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest().upper(),
        "tracked": False,
        "proposedArchiveCategory": category,
    }


def _write_inventory(repository: Path, entries: list[dict[str, Any]], **overrides: Any) -> Path:
    inventory: dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAtUtc": "2026-08-19T00:00:00Z",
        "repositoryRoot": str(repository),
        "files": entries,
    }
    inventory.update(overrides)
    path = repository / "inventory.json"
    path.write_text(json.dumps(inventory), encoding="utf-8")
    return path


def _invoke(inventory: Path, destination: Path, *, apply: bool = False) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(ARCHIVE_SCRIPT),
        "-InventoryPath",
        str(inventory),
        "-Destination",
        str(destination),
    ]
    if apply:
        command.append("-Apply")
    return _run(command, inventory.parent)


def test_dry_run_prints_every_move_without_creating_or_moving(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    (repository / "clip.mp4").write_bytes(b"video")
    nested = repository / "captures"
    nested.mkdir()
    (nested / "frame.png").write_bytes(b"image")
    inventory = _write_inventory(
        repository,
        [
            _inventory_entry(repository, "clip.mp4", "video"),
            _inventory_entry(repository, "captures/frame.png", "image"),
        ],
    )
    destination = tmp_path / "archive"

    result = _invoke(inventory, destination)

    assert result.returncode == 0, result.stderr
    assert "Planned files: 2" in result.stdout
    assert "clip.mp4" in result.stdout
    assert "video" in result.stdout
    assert "captures" in result.stdout
    assert "frame.png" in result.stdout
    assert "image" in result.stdout
    assert (repository / "clip.mp4").read_bytes() == b"video"
    assert (nested / "frame.png").read_bytes() == b"image"
    assert not destination.exists()


def test_apply_moves_only_inventory_files_and_writes_hash_manifest(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    (repository / "clip.mp4").write_bytes(b"video")
    (repository / "keep.txt").write_text("keep", encoding="utf-8")
    inventory = _write_inventory(repository, [_inventory_entry(repository, "clip.mp4", "video")])
    destination = tmp_path / "archive"

    result = _invoke(inventory, destination, apply=True)

    assert result.returncode == 0, result.stderr
    assert not (repository / "clip.mp4").exists()
    assert (repository / "keep.txt").read_text(encoding="utf-8") == "keep"
    archived = destination / "video" / "clip.mp4"
    assert archived.read_bytes() == b"video"
    manifest = json.loads((destination / "archive-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest == {
        "schemaVersion": 1,
        "files": [
            {
                "originalPath": "clip.mp4",
                "archivedPath": "video/clip.mp4",
                "bytes": 5,
                "sha256": "0CAB1C9617404FAF2B24E221E189CA5945813E14D3F766345B09CA13BBE28FFC",
            }
        ],
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda entry, repository: entry.update(path="../outside.mp4"), "relative path"),
        (lambda entry, repository: entry.update(path=str(repository / "clip.mp4")), "relative path"),
        (lambda entry, repository: entry.update(tracked=True), "recorded as tracked"),
        (lambda entry, repository: entry.update(bytes=999), "byte length"),
        (lambda entry, repository: entry.update(sha256="0" * 64), "SHA-256"),
        (lambda entry, repository: entry.update(proposedArchiveCategory="../video"), "category"),
    ],
)
def test_preflight_rejects_unsafe_or_drifted_entries_before_mutation(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    repository = _make_repository(tmp_path)
    (repository / "clip.mp4").write_bytes(b"video")
    entry = _inventory_entry(repository, "clip.mp4", "video")
    mutate(entry, repository)
    inventory = _write_inventory(repository, [entry])
    destination = tmp_path / "archive"

    result = _invoke(inventory, destination, apply=True)

    assert result.returncode != 0
    assert message.lower() in (result.stdout + result.stderr).lower()
    assert (repository / "clip.mp4").read_bytes() == b"video"
    assert not destination.exists()


def test_preflight_rejects_missing_duplicate_and_currently_tracked_sources(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    (repository / "tracked.mp4").write_bytes(b"tracked")
    assert _run(["git", "add", "tracked.mp4"], repository).returncode == 0
    assert _run(["git", "commit", "--quiet", "-m", "test: Add tracked fixture"], repository).returncode == 0
    (repository / "duplicate.mp4").write_bytes(b"duplicate")
    tracked_entry = _inventory_entry(repository, "tracked.mp4", "video")
    missing_entry = dict(tracked_entry, path="missing.mp4")
    duplicate_entry = _inventory_entry(repository, "duplicate.mp4", "video")

    for entries, message in (
        ([tracked_entry], "currently tracked"),
        ([missing_entry], "missing"),
        ([duplicate_entry, dict(duplicate_entry, path="DUPLICATE.mp4")], "duplicate"),
    ):
        inventory = _write_inventory(repository, entries)
        destination = tmp_path / f"archive-{message.replace(' ', '-')}"
        result = _invoke(inventory, destination, apply=True)
        assert result.returncode != 0
        assert message in (result.stdout + result.stderr).lower()
        assert (repository / "tracked.mp4").read_bytes() == b"tracked"
        assert not destination.exists()


@pytest.mark.parametrize(
    "inventory",
    [
        {},
        {"schemaVersion": 2, "repositoryRoot": "x", "files": []},
        {"schemaVersion": 1, "repositoryRoot": "x", "files": "not-an-array"},
        {"schemaVersion": 1, "repositoryRoot": "x", "files": [{}]},
    ],
)
def test_preflight_rejects_invalid_inventory_schema(tmp_path: Path, inventory: dict[str, Any]) -> None:
    repository = _make_repository(tmp_path)
    inventory_path = repository / "inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    destination = tmp_path / "archive"

    result = _invoke(inventory_path, destination, apply=True)

    assert result.returncode != 0
    assert "inventory schema" in (result.stdout + result.stderr).lower()
    assert not destination.exists()


@pytest.mark.parametrize("collision", ["archive-manifest.json", "video/clip.mp4"])
def test_preflight_rejects_destination_collisions(tmp_path: Path, collision: str) -> None:
    repository = _make_repository(tmp_path)
    (repository / "clip.mp4").write_bytes(b"video")
    inventory = _write_inventory(repository, [_inventory_entry(repository, "clip.mp4", "video")])
    destination = tmp_path / "archive"
    collision_path = destination / Path(collision.replace("/", "\\"))
    collision_path.parent.mkdir(parents=True, exist_ok=True)
    collision_path.write_text("collision", encoding="utf-8")

    result = _invoke(inventory, destination, apply=True)

    assert result.returncode != 0
    assert "destination" in (result.stdout + result.stderr).lower()
    assert (repository / "clip.mp4").read_bytes() == b"video"
    assert collision_path.read_text(encoding="utf-8") == "collision"
