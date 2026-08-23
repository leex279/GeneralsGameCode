"""Release checks for local browser tooling and packaged application resources."""

from __future__ import annotations

from pathlib import Path

import pytest

from .support import (
    EXPECTED_BROWSER_DISTRIBUTIONS,
    assert_vendor_manifest,
    browser_distribution_versions,
    external_artifact_inventory,
    local_axe_script,
    packaged_web_sources,
    validate_external_artifact_root,
)

PROJECT_ROOT = Path(__file__).parents[2]


def test_exact_browser_test_distributions_and_local_axe_payload_are_available() -> None:
    assert browser_distribution_versions() == EXPECTED_BROWSER_DISTRIBUTIONS
    payload = local_axe_script()
    assert len(payload) > 100_000
    assert b"axe" in payload.lower()


def test_accepted_application_web_resources_are_local_and_vendor_bytes_are_frozen() -> None:
    resources = packaged_web_sources(PROJECT_ROOT)
    assert resources
    assert all(payload for payload in resources.values())
    assert not any(name.endswith("axe.min.js") for name in resources)
    assert_vendor_manifest(PROJECT_ROOT)


def test_browser_artifact_root_rejects_every_repository_descendant(tmp_path: Path) -> None:
    worktree_root = PROJECT_ROOT.parents[1]
    repository_root = worktree_root.parents[1]
    with pytest.raises(ValueError, match="external"):
        validate_external_artifact_root(
            worktree_root / "docs" / "browser-artifacts", worktree_root, repository_root
        )
    with pytest.raises(ValueError, match="external"):
        validate_external_artifact_root(
            repository_root / "browser-artifacts", worktree_root, repository_root
        )
    assert validate_external_artifact_root(tmp_path, worktree_root, repository_root) == tmp_path.resolve()


def test_external_artifact_inventory_uses_logical_names_and_content_digests(tmp_path: Path) -> None:
    (tmp_path / "run-manifest.json").write_text("private manifest", encoding="utf-8")
    (tmp_path / "report--desktop.png").write_bytes(b"png evidence")
    (tmp_path / "axe").mkdir()
    (tmp_path / "axe" / "report.json").write_bytes(b"axe evidence")

    inventory = external_artifact_inventory(tmp_path)

    assert tuple(item["logical_name"] for item in inventory) == (
        "axe/report.json",
        "report--desktop.png",
    )
    assert all(len(str(item["sha256"])) == 64 for item in inventory)
    assert all("Users" not in str(item) for item in inventory)
