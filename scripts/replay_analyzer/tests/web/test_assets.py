"""Local asset and vendored-byte contracts for the web shell."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.resources import package_resource

from .conftest import CountingPortFactory, RecordingBootstrapper

_VENDOR_FIXTURES = Path(__file__).parent / "fixtures" / "vendor"
_FIXTURE_SHA256 = {
    "htmx-LICENSE": "d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38",
    "echarts-LICENSE": "634293835b43a6dd2094fa39182a3d9a6b9ca43b7fdb9ac354e8037af2a3093a",
    "echarts-NOTICE": "d491d358344f842685c1b1585970999db65fe30ecf7ef3867af8814f4016c016",
    "echarts-LICENSE-d3": "e1211892da0b0e0585b7aebe8f98c1274fba15bafe47fa1f4ee8a7a502c06304",
}


def _resource(name: str) -> str:
    return package_resource(name).read_text(encoding="utf-8")


def test_shell_references_only_package_local_assets_and_keeps_vendor_bytes_pinned() -> None:
    base = _resource("web/templates/base.html")
    stylesheet = _resource("web/static/css/app.css")
    script = _resource("web/static/js/app.js")
    manifest = json.loads(_resource("web/static/vendor/vendor-manifest.json"))
    licenses = _resource("web/static/vendor/THIRD_PARTY_LICENSES.md")

    assert 'href="/static/css/app.css?v=17"' in base
    assert 'src="/static/js/app.js"' in base
    assert "http://" not in base and "https://" not in base
    assert "@import" not in stylesheet
    assert "eval(" not in script and "Function(" not in script
    assert "createElement('script')" not in script and 'createElement("script")' not in script
    assert {entry["filename"] for entry in manifest["assets"]} == {"htmx.min.js", "echarts.min.js"}
    for entry in manifest["assets"]:
        asset = package_resource(f"web/static/vendor/{entry['filename']}").read_bytes()
        assert hashlib.sha256(asset).hexdigest() == entry["sha256"]
        assert licenses.count(f"Filename: {entry['filename']}") == 1
        assert licenses.count(f"Version: {entry['version']}") == 1
        assert licenses.count(f"SHA-256: {entry['sha256']}") == 1
        assert entry["license_spdx"] in licenses
    assert "THE SOFTWARE IS PROVIDED \u201cAS IS\u201d" in licenses


def test_license_record_contains_byte_faithful_tagged_license_and_notice_fixtures() -> None:
    licenses = _resource("web/static/vendor/THIRD_PARTY_LICENSES.md")
    expected = {
        "htmx-LICENSE": "htmx.min.js",
        "echarts-LICENSE": "echarts.min.js",
        "echarts-NOTICE": "echarts.min.js",
        "echarts-LICENSE-d3": "echarts.min.js",
    }

    for fixture_name, asset_name in expected.items():
        fixture_path = _VENDOR_FIXTURES / fixture_name
        assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == _FIXTURE_SHA256[fixture_name]
        fixture = fixture_path.read_text(encoding="utf-8")
        assert fixture in licenses
        assert f"Filename: {asset_name}" in licenses
    assert "THE SOFTWARE IS PROVIDED \u201cAS IS\u201d" in licenses


def test_every_first_party_template_and_asset_is_local_and_has_no_inline_or_dynamic_code() -> None:
    root = package_resource("web")
    first_party = _walk_resources(root.joinpath("templates")) + _walk_resources(root.joinpath("static", "css")) + _walk_resources(
        root.joinpath("static", "js")
    )

    assert first_party
    for resource in first_party:
        content = resource.read_text(encoding="utf-8")
        assert "http://" not in content and "https://" not in content
        assert "@import" not in content
        assert not re.search(r"<script(?![^>]*\bsrc=)", content, flags=re.IGNORECASE)
        assert not re.search(r"\\son[a-z]+\\s*=", content, flags=re.IGNORECASE)
        assert not re.search(r"\\sstyle\\s*=", content, flags=re.IGNORECASE)
        assert "eval(" not in content and "Function(" not in content
        assert "createElement('script')" not in content and 'createElement("script")' not in content


# TheSuperHackers @bugfix Leex 24/08/2026 Keep packaged user-facing resources free of UTF-8 mojibake. (#TBD)
def test_packaged_user_facing_resources_have_no_encoding_corruption_markers() -> None:
    root = package_resource("web")
    first_party = _walk_resources(root.joinpath("templates")) + _walk_resources(root.joinpath("static", "css")) + _walk_resources(
        root.joinpath("static", "js")
    )
    markers = ("\u00c2", "\u00e2", "\ufffd")

    corrupted = {
        str(resource.relative_to(root)): marker
        for resource in first_party
        for content in (resource.read_text(encoding="utf-8"),)
        for marker in markers
        if marker in content
    }

    assert corrupted == {}


def _walk_resources(directory: object) -> tuple[object, ...]:
    children = tuple(directory.iterdir())  # type: ignore[union-attr]
    return tuple(
        item
        for child in children
        for item in (_walk_resources(child) if child.is_dir() else (child,))  # type: ignore[union-attr]
    )


def test_reusable_empty_state_does_not_hard_code_a_duplicate_heading_identifier() -> None:
    macro = _resource("web/templates/components/empty_state.html")

    assert 'id="empty-state-heading"' not in macro
    assert 'aria-labelledby="empty-state-heading"' not in macro


def test_shell_csp_allows_local_assets_and_rejects_remote_script_sources() -> None:
    app = create_app(object(), port_factory=CountingPortFactory(), bootstrapper=RecordingBootstrapper())

    with TestClient(app) as client:
        response = client.get("/", headers={"host": "localhost"})

    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "https://cdn.example.test" not in csp
    assert "'unsafe-inline'" not in csp


def test_package_static_mount_serves_local_assets_and_rejects_traversal_without_eager_vendors() -> None:
    app = create_app(object(), port_factory=CountingPortFactory(), bootstrapper=RecordingBootstrapper())

    with TestClient(app) as client:
        stylesheet = client.get("/static/css/app.css", headers={"host": "localhost"})
        traversal = client.get("/static/../ports.py", headers={"host": "localhost"})
        encoded_traversal = client.get("/static/%2e%2e/ports.py", headers={"host": "localhost"})
        missing = client.get("/static/not-a-resource.js", headers={"host": "localhost"})
        dashboard = client.get("/", headers={"host": "localhost"})

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self'; connect-src 'self'; form-action 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    assert stylesheet.headers["x-content-type-options"] == "nosniff"
    assert stylesheet.headers["referrer-policy"] == "same-origin"
    assert stylesheet.headers["x-frame-options"] == "DENY"
    assert traversal.status_code == 404
    assert encoded_traversal.status_code == 404
    assert missing.status_code == 404
    assert 'src="/static/vendor/htmx.min.js"' not in dashboard.text
    assert 'src="/static/vendor/echarts.min.js"' not in dashboard.text
