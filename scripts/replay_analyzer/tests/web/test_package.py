"""Checkout-independent packaged-resource policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from generals_replay_analyzer.web.resources import PackagedResourceError, package_resource


def test_package_resource_reads_package_owned_migration_without_checkout_fallback() -> None:
    resource = package_resource("db/migrations/env.py")

    assert resource.is_file()
    assert "run_migrations_online" in resource.read_text(encoding="utf-8")


def test_missing_package_resource_is_controlled_even_when_checkout_has_same_relative_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout_fallback = tmp_path / "web" / "templates" / "missing.html"
    checkout_fallback.parent.mkdir(parents=True)
    checkout_fallback.write_text("must not be read", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PackagedResourceError, match="packaged resource is unavailable") as caught:
        package_resource("web/templates/missing.html")

    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("relative_name", [".", "./", "../secret", "/absolute", "web/../../secret", "C:/secret"])
def test_resource_locator_rejects_absolute_or_parent_traversal(relative_name: str) -> None:
    with pytest.raises(PackagedResourceError, match="invalid packaged resource name"):
        package_resource(relative_name)
