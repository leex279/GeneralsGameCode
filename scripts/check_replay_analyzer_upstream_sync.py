"""Validate and print a no-side-effect upstream compatibility sync plan."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ManifestError(ValueError):
    """Raised when the pinned upstream manifest is not closed and safe."""


EXPECTED_REPOSITORY = "https://github.com/GeneralsOnlineDevelopmentTeam/GameClient.git"
EXPECTED_COMMIT = "b7cfeaf08e53044240c8674ea1960738d49776b7"
EXPECTED_REPLAY_SHA256 = "EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8"
EXPECTED_FRAME100_CRC = "0x582083DA"
EXACT_COMPARISON_SCOPE_PREFIXES = (
    "Core/GameEngine/",
    "Core/GameEngineDevice/",
    "GeneralsMD/Code/GameEngine/",
    "GeneralsMD/Code/GameEngineDevice/",
    "GeneralsMD/Code/Main/",
)
EXPECTED_RECORDER = {
    "name": "GeneralsOnlineZH_60.exe",
    "sha256": "15619eba088abd6a24e790d8203b95f84925fcf0ea04c7b8203a29c9fdfe5ca6",
    "exe_crc": "0x48D67663",
    "build_time": "Jun 20 2026 23:29:03",
    "simulation_profile": "GENERALS_ONLINE_HIGH_FPS_SERVER",
    "tested_non_causal_profile": "GENERALS_ONLINE_IBRA_STARTING_POS_LOGIC",
    "provenance": "GeneralsOnlineDevelopmentTeam/GameClient at pinned commit",
    "purpose": "Reference recorder executable for replay capture compatibility checks",
}


_TOP_KEYS = {
    "schema_version",
    "repository",
    "commit",
    "recorder_executable",
    "replay_gate",
    "comparison_scope_prefixes",
    "sync_mode",
}
_RECORDER_KEYS = set(EXPECTED_RECORDER)
_REPLAY_KEYS = {"expected_frame100_crc", "replay_sha256"}
_ALLOWED_PREFIXES = frozenset(EXACT_COMPARISON_SCOPE_PREFIXES)

# TheSuperHackers @feature Leex 24/08/2026 Keep upstream replay compatibility pinned to immutable evidence and fetch-only planning. (#TBD)


def _closed(value: Any, expected: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ManifestError(f"{name} must contain exactly: {sorted(expected)}")


def validate_manifest(manifest: Any) -> None:
    """Validate the closed, immutable values used by the fetch-only plan."""
    _closed(manifest, _TOP_KEYS, "manifest")
    if manifest["schema_version"] != 1:
        raise ManifestError("schema_version must be 1")
    repository = manifest["repository"]
    parsed = urlparse(repository) if isinstance(repository, str) else None
    if repository != EXPECTED_REPOSITORY:
        raise ManifestError("repository does not match the pinned HTTPS GitHub URL")
    if parsed is None or parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ManifestError("repository must be an HTTPS GitHub URL")
    commit = manifest["commit"]
    if commit != EXPECTED_COMMIT:
        raise ManifestError("commit does not match the pinned immutable commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ManifestError("commit must be a lowercase 40-hex immutable commit")
    _closed(manifest["recorder_executable"], _RECORDER_KEYS, "recorder_executable")
    if manifest["recorder_executable"] != EXPECTED_RECORDER:
        raise ManifestError("recorder executable provenance does not match the pinned evidence")
    _closed(manifest["replay_gate"], _REPLAY_KEYS, "replay_gate")
    gate = manifest["replay_gate"]
    if gate["expected_frame100_crc"] != EXPECTED_FRAME100_CRC:
        raise ManifestError("expected_frame100_crc does not match the pinned frame-100 CRC")
    if not isinstance(gate["expected_frame100_crc"], str) or not re.fullmatch(r"0x[0-9A-F]{8}", gate["expected_frame100_crc"]):
        raise ManifestError("expected_frame100_crc must match 0x plus eight uppercase hex digits")
    if gate["replay_sha256"] != EXPECTED_REPLAY_SHA256:
        raise ManifestError("replay_sha256 does not match the pinned replay evidence")
    if not isinstance(gate["replay_sha256"], str) or not re.fullmatch(r"[0-9A-Fa-f]{64}", gate["replay_sha256"]):
        raise ManifestError("replay_sha256 must be exactly 64 hexadecimal characters")
    prefixes = manifest["comparison_scope_prefixes"]
    if prefixes != list(EXACT_COMPARISON_SCOPE_PREFIXES):
        raise ManifestError("comparison scope prefixes must match the complete ordered set")
    if manifest["sync_mode"] != "fetch-only":
        raise ManifestError("sync_mode must be fetch-only; live branch, force, and push modes are forbidden")


def build_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    validate_manifest(manifest)
    return {
        "commit": manifest["commit"],
        "comparison_scope_prefixes": list(EXACT_COMPARISON_SCOPE_PREFIXES),
        "fetch": {"remote": "generals-online", "ref": manifest["commit"], "depth": 1},
        "integration_branch": "integration/upstream-compat",
        "merge_policy": "never-auto-merge",
        "network": False,
        "mutation": False,
        "repository": manifest["repository"],
        "scope_policy": "over-inclusive-review-only",
        "steps": ["verify-manifest", "verify-clean-tree-if-sync-requested", "fetch-commit-only"],
    }


def require_clean_worktree(repository_root: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ManifestError("unable to inspect git worktree") from exc
    if result.stdout.strip():
        raise ManifestError("explicit sync mode requires a clean worktree")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--plan", action="store_true", help="print the deterministic plan")
    parser.add_argument("--sync", action="store_true", help="validate clean-tree prerequisite; never fetch or mutate")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        plan = build_plan(manifest)
        if args.sync:
            require_clean_worktree(args.manifest.resolve().parents[2])
        if args.plan or not args.sync:
            print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    except (ManifestError, OSError, json.JSONDecodeError) as exc:
        print(f"upstream-sync: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
