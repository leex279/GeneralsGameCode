import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[1]
CHECKER_PATH = ROOT / ".." / "check_replay_analyzer_upstream_sync.py"
MANIFEST_PATH = ROOT.parent.parent / "docs" / "replay-analyzer" / "upstream-sync-manifest-v1.json"


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("upstream_sync_checker", CHECKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))


def test_pinned_manifest_validates_and_plan_is_deterministic() -> None:
    checker = _checker()
    manifest = _manifest()

    checker.validate_manifest(manifest)
    assert checker.build_plan(manifest) == checker.build_plan(copy.deepcopy(manifest))
    assert checker.build_plan(manifest)["network"] is False
    assert checker.build_plan(manifest)["integration_branch"] == "integration/upstream-compat"
    assert checker.build_plan(manifest)["comparison_scope_prefixes"] == manifest["comparison_scope_prefixes"]
    assert checker.build_plan(manifest)["scope_policy"] == "over-inclusive-review-only"
    assert checker.build_plan(manifest)["merge_policy"] == "never-auto-merge"


def test_manifest_pins_exact_recorder_provenance_and_ordered_prefixes() -> None:
    checker = _checker()
    manifest = _manifest()

    recorder = manifest["recorder_executable"]
    assert recorder == {
        "name": "GeneralsOnlineZH_60.exe",
        "sha256": "15619eba088abd6a24e790d8203b95f84925fcf0ea04c7b8203a29c9fdfe5ca6",
        "exe_crc": "0x48D67663",
        "build_time": "Jun 20 2026 23:29:03",
        "simulation_profile": "GENERALS_ONLINE_HIGH_FPS_SERVER",
        "tested_non_causal_profile": "GENERALS_ONLINE_IBRA_STARTING_POS_LOGIC",
        "provenance": "GeneralsOnlineDevelopmentTeam/GameClient at pinned commit",
        "purpose": "Reference recorder executable for replay capture compatibility checks",
    }
    assert manifest["comparison_scope_prefixes"] == list(checker.EXACT_COMPARISON_SCOPE_PREFIXES)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("recorder_executable", "name", "other.exe"),
        ("recorder_executable", "sha256", "0" * 64),
        ("recorder_executable", "exe_crc", "0x00000000"),
        ("recorder_executable", "build_time", "later"),
        ("recorder_executable", "simulation_profile", "debug"),
        ("recorder_executable", "tested_non_causal_profile", "debug"),
    ],
)
def test_manifest_rejects_wrong_exact_recorder_values(section: str, field: str, value: str) -> None:
    checker = _checker()
    manifest = _manifest()
    manifest[section][field] = value

    with pytest.raises(checker.ManifestError):
        checker.validate_manifest(manifest)


def test_manifest_rejects_incomplete_reordered_or_extra_scope_prefixes() -> None:
    checker = _checker()
    manifest = _manifest()
    manifest["comparison_scope_prefixes"] = manifest["comparison_scope_prefixes"][1:]
    with pytest.raises(checker.ManifestError):
        checker.validate_manifest(manifest)

    manifest = _manifest()
    manifest["comparison_scope_prefixes"].reverse()
    with pytest.raises(checker.ManifestError):
        checker.validate_manifest(manifest)

    manifest = _manifest()
    manifest["comparison_scope_prefixes"].append("Core/Libraries/")
    with pytest.raises(checker.ManifestError):
        checker.validate_manifest(manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("repository", "http://github.com/example/repo.git"),
        ("commit", "main"),
        ("replay_gate.expected_frame100_crc", "582083DA"),
        ("replay_gate.replay_sha256", "EA0857"),
        ("sync_mode", "force"),
    ],
)
def test_manifest_rejects_hostile_or_malformed_values(field: str, value: object) -> None:
    checker = _checker()
    manifest = _manifest()
    if "." in field:
        section, key = field.split(".", 1)
        manifest[section][key] = value
    else:
        manifest[field] = value

    with pytest.raises(checker.ManifestError):
        checker.validate_manifest(manifest)


def test_explicit_sync_mode_requires_clean_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    checker = _checker()

    class Result:
        stdout = " M unrelated-file.cpp\n"

    monkeypatch.setattr(checker.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(checker.ManifestError, match="clean worktree"):
        checker.require_clean_worktree(tmp_path)


def test_manifest_rejects_unknown_keys_and_live_sync_controls() -> None:
    checker = _checker()
    manifest = _manifest()
    manifest["unexpected"] = True
    with pytest.raises(checker.ManifestError):
        checker.validate_manifest(manifest)

    manifest = _manifest()
    manifest["sync_mode"] = "push"
    with pytest.raises(checker.ManifestError):
        checker.validate_manifest(manifest)


def test_cli_emits_json_plan_and_concise_errors(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(CHECKER_PATH),
        "--manifest",
        str(MANIFEST_PATH),
        "--plan",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["integration_branch"] == "integration/upstream-compat"
    assert result.stderr == ""

    missing = subprocess.run(
        [
            sys.executable,
            str(CHECKER_PATH),
            "--manifest",
            str(MANIFEST_PATH.with_name("missing.json")),
            "--plan",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "Traceback" not in missing.stderr
    assert "upstream-sync: error:" in missing.stderr

    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("{", encoding="utf-8")
    malformed = subprocess.run(
        [sys.executable, str(CHECKER_PATH), "--manifest", str(malformed_path), "--plan"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode != 0
    assert "Traceback" not in malformed.stderr
    assert "upstream-sync: error:" in malformed.stderr

    dirty = subprocess.run(
        [sys.executable, str(CHECKER_PATH), "--manifest", str(MANIFEST_PATH), "--sync"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dirty.returncode != 0
    assert "clean worktree" in dirty.stderr
    assert "Traceback" not in dirty.stderr
