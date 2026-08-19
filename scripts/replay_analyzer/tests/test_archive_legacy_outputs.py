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


def _invoke_with_move_failure(
    inventory: Path, destination: Path, failing_source: Path
) -> subprocess.CompletedProcess[str]:
    script = str(ARCHIVE_SCRIPT).replace("'", "''")
    inventory_path = str(inventory).replace("'", "''")
    destination_path = str(destination).replace("'", "''")
    failing_path = str(failing_source).replace("'", "''")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            f"$failurePath = '{failing_path}'; "
            "function Move-Item { "
            "param([string]$LiteralPath, [string]$Destination); "
            'if ($LiteralPath -eq $failurePath) { throw "forced move failure" }; '
            "Microsoft.PowerShell.Management\\Move-Item -LiteralPath $LiteralPath -Destination $Destination -ErrorAction Stop "
            "}; "
            f"& '{script}' -InventoryPath '{inventory_path}' -Destination '{destination_path}' -Apply; "
            "exit $LASTEXITCODE"
        ),
    ]
    return _run(command, inventory.parent)


def _invoke_with_manifest_finalization_failure(
    inventory: Path, destination: Path
) -> subprocess.CompletedProcess[str]:
    script = str(ARCHIVE_SCRIPT).replace("'", "''")
    inventory_path = str(inventory).replace("'", "''")
    destination_path = str(destination).replace("'", "''")
    manifest_path = str(destination / "archive-manifest.json").replace("'", "''")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            f"$manifestPath = '{manifest_path}'; $global:moveCount = 0; "
            "function Move-Item { "
            "param([string]$LiteralPath, [string]$Destination); "
            "Microsoft.PowerShell.Management\\Move-Item -LiteralPath $LiteralPath -Destination $Destination -ErrorAction Stop; "
            "$global:moveCount += 1; "
            "if ($global:moveCount -eq 1) { [IO.Directory]::CreateDirectory($manifestPath) | Out-Null } "
            "}; "
            f"& '{script}' -InventoryPath '{inventory_path}' -Destination '{destination_path}' -Apply; "
            "exit $LASTEXITCODE"
        ),
    ]
    return _run(command, inventory.parent)


def _invoke_in_culture(
    inventory: Path, destination: Path, culture: str
) -> subprocess.CompletedProcess[str]:
    script = str(ARCHIVE_SCRIPT).replace("'", "''")
    inventory_path = str(inventory).replace("'", "''")
    destination_path = str(destination).replace("'", "''")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            f"[Threading.Thread]::CurrentThread.CurrentCulture = [Globalization.CultureInfo]'{culture}'; "
            f"& '{script}' -InventoryPath '{inventory_path}' -Destination '{destination_path}' -Apply; "
            "exit $LASTEXITCODE"
        ),
    ]
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


def test_currently_tracked_check_treats_inventory_name_as_a_literal_git_pathspec(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    (repository / "clipa.mp4").write_bytes(b"tracked")
    assert _run(["git", "add", "clipa.mp4"], repository).returncode == 0
    assert _run(["git", "commit", "--quiet", "-m", "test: Add tracked wildcard match"], repository).returncode == 0
    literal_name = "clip[ab].mp4"
    (repository / literal_name).write_bytes(b"untracked")
    inventory = _write_inventory(repository, [_inventory_entry(repository, literal_name)])

    result = _invoke(inventory, tmp_path / "archive")

    assert result.returncode == 0, result.stderr
    assert (repository / literal_name).read_bytes() == b"untracked"


def test_currently_tracked_check_rejects_a_tracked_literal_metacharacter_name(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    literal_name = "clip[ab].mp4"
    (repository / literal_name).write_bytes(b"tracked")
    assert _run(["git", "add", literal_name], repository).returncode == 0
    assert _run(["git", "commit", "--quiet", "-m", "test: Add tracked literal metacharacters"], repository).returncode == 0
    inventory = _write_inventory(repository, [_inventory_entry(repository, literal_name)])

    result = _invoke(inventory, tmp_path / "archive", apply=True)

    assert result.returncode != 0
    assert "currently tracked" in (result.stdout + result.stderr).lower()
    assert (repository / literal_name).read_bytes() == b"tracked"


def test_apply_rolls_back_after_move_failure_and_retains_pending_manifest(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository / "first.mp4"
    second = repository / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    inventory = _write_inventory(
        repository,
        [_inventory_entry(repository, "first.mp4"), _inventory_entry(repository, "second.mp4")],
    )
    destination = tmp_path / "archive"

    result = _invoke_with_move_failure(inventory, destination, second)

    assert result.returncode != 0, result.stderr
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    pending = json.loads((destination / "archive-pending.json").read_text(encoding="utf-8-sig"))
    assert [entry["originalPath"] for entry in pending["files"]] == ["first.mp4", "second.mp4"]
    assert not (destination / "archive-manifest.json").exists()


def test_preflight_rejects_later_destination_parent_file_before_pending_or_moves(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    source = repository / "nested" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    inventory = _write_inventory(repository, [_inventory_entry(repository, "nested/clip.mp4")])
    destination = tmp_path / "archive"
    blocked_parent = destination / "video" / "nested"
    blocked_parent.parent.mkdir(parents=True)
    blocked_parent.write_text("not a directory", encoding="utf-8")

    result = _invoke(inventory, destination, apply=True)

    assert result.returncode != 0
    assert "destination" in (result.stdout + result.stderr).lower()
    assert source.read_bytes() == b"video"
    assert not (destination / "archive-pending.json").exists()
    assert not (destination / "archive-manifest.json").exists()


def test_apply_rolls_back_when_atomic_manifest_finalization_fails(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository / "first.mp4"
    second = repository / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    inventory = _write_inventory(
        repository,
        [_inventory_entry(repository, "first.mp4"), _inventory_entry(repository, "second.mp4")],
    )
    destination = tmp_path / "archive"

    result = _invoke_with_manifest_finalization_failure(inventory, destination)

    assert result.returncode != 0, result.stderr
    assert first.exists(), result.stderr
    assert second.exists(), result.stderr
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert (destination / "archive-pending.json").is_file()
    assert not (destination / "archive-manifest.json").is_file()


def test_preflight_rejects_destination_reparse_ancestor_before_any_move(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    (repository / "clip.mp4").write_bytes(b"video")
    inventory = _write_inventory(repository, [_inventory_entry(repository, "clip.mp4")])
    target = tmp_path / "reparse-target"
    target.mkdir()
    redirect = tmp_path / "reparse-redirect"
    try:
        redirect.symlink_to(target, target_is_directory=True)
    except OSError as error:
        junction = _run(["cmd.exe", "/c", "mklink", "/J", str(redirect), str(target)], tmp_path)
        if junction.returncode != 0:
            pytest.skip(f"Cannot create a directory reparse point: {error}; {junction.stderr}")

    result = _invoke(inventory, redirect / "archive", apply=True)

    assert result.returncode != 0
    assert "reparse" in (result.stdout + result.stderr).lower()
    assert (repository / "clip.mp4").read_bytes() == b"video"
    assert not (target / "archive").exists()


def test_manifest_uses_ordinal_path_order_independent_of_current_culture(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    (repository / "a.mp4").write_bytes(b"a")
    (repository / "Z.mp4").write_bytes(b"z")
    inventory = _write_inventory(
        repository,
        [_inventory_entry(repository, "a.mp4"), _inventory_entry(repository, "Z.mp4")],
    )
    destination = tmp_path / "archive"

    result = _invoke_in_culture(inventory, destination, "tr-TR")

    assert result.returncode == 0, result.stderr
    manifest = json.loads((destination / "archive-manifest.json").read_text(encoding="utf-8-sig"))
    assert [entry["originalPath"] for entry in manifest["files"]] == ["Z.mp4", "a.mp4"]
