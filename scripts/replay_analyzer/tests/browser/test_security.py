"""Installed-server security, loopback, mutation, and redaction release checks."""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from typing import Any

import pytest
from playwright.sync_api import Browser

from .populated_fixture import PopulatedFixtureResult
from .support import (
    assert_browser_clean,
    deterministic_context_options,
    install_browser_error_guard,
    install_same_origin_guard,
)

REQUIRED_CSP_DIRECTIVES = (
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "connect-src 'self'",
)
FORBIDDEN_CSP_TOKENS = ("unsafe-inline", "unsafe-eval", "data:", "blob:", "*", "http:", "https:")


def _response(origin: str, path: str, *, accept: str = "text/html") -> tuple[int, Any, bytes]:
    request = urllib.request.Request(f"{origin}{path}", headers={"Accept": accept})
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()
    with response:
        return response.status, response.headers, response.read()


def _unsafe_response(origin: str, path: str, request_origin: str | None) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/problem+json"}
    if request_origin is not None:
        headers["Origin"] = request_origin
    request = urllib.request.Request(
        f"{origin}{path}",
        data=b"_csrf=task9-secret-canary",
        method="POST",
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())
    with response:
        return response.status, json.loads(response.read())


@pytest.mark.browser
@pytest.mark.parametrize(
    ("path", "expected_status"),
    (("/", 200), ("/health/ready", 200), ("/static/css/app.css", 200), ("/static/js/app.js", 200), ("/missing", 404)),
)
def test_security_headers_cover_html_json_static_and_problem_responses(
    installed_server: object,
    path: str,
    expected_status: int,
) -> None:
    status, headers, _body = _response(installed_server.origin, path)
    assert status == expected_status
    assert headers["X-Content-Type-Options"] == "nosniff"
    if path.endswith((".css", ".js")):
        return
    csp = headers["Content-Security-Policy"]
    assert all(directive in csp for directive in REQUIRED_CSP_DIRECTIVES)
    assert not any(token in csp for token in FORBIDDEN_CSP_TOKENS)
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    permissions = headers["Permissions-Policy"]
    assert all(f"{capability}=()" in permissions for capability in ("camera", "microphone", "geolocation", "payment", "usb"))


@pytest.mark.browser
def test_populated_fixed_json_and_real_map_png_are_hardened_and_path_free(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    status, _headers, map_html = _response(origin, manifest.map.fixed_url)
    assert status == 200
    raster_match = re.search(rb'href="(/api/maps/[^"]+/rasters/[^"]+)"', map_html)
    assert raster_match is not None
    raster_path = raster_match.group(1).decode("ascii")
    timeline_path = (
        f"/api/replays/{manifest.replay_public_id}/reports/"
        f"{manifest.replay_report.report_public_id}/charts/timeline"
    )
    resources = (
        (timeline_path, "application/json", "application/json"),
        (manifest.map.api_url, "application/json", "application/json"),
        (raster_path, "image/png", "image/png"),
    )
    for path, accept, expected_media_type in resources:
        status, headers, body = _response(origin, path, accept=accept)
        assert status == 200, (path, status, body)
        assert headers["Content-Type"].startswith(expected_media_type)
        assert headers["X-Content-Type-Options"] == "nosniff"
        csp = headers["Content-Security-Policy"]
        assert all(directive in csp for directive in REQUIRED_CSP_DIRECTIVES)
        assert not any(token in csp for token in FORBIDDEN_CSP_TOKENS)
        assert headers["Referrer-Policy"] == "same-origin"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert body.find(str(populated_server.runtime_root).encode()) == -1
        assert body.find(b"_sa_instance_state") == -1
        assert body.find(b"Traceback") == -1


@pytest.mark.browser
@pytest.mark.parametrize(
    "host",
    (
        "",
        "0.0.0.0",
        "[::]",
        "remote.invalid",
        "127.0.0.1.evil",
        "evil127.0.0.1",
        "user@127.0.0.1",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "[::1",
    ),
)
def test_nonlocal_and_malformed_host_authorities_are_rejected(installed_server: object, host: str) -> None:
    origin = installed_server.origin
    authority = origin.removeprefix("http://")
    connection = http.client.HTTPConnection(authority, timeout=5)
    connection.putrequest("GET", "/health/live", skip_host=True)
    connection.putheader("Host", host)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert response.status == 403
    if host:
        assert host.encode() not in body


@pytest.mark.browser
@pytest.mark.parametrize("host_kind", ("exact", "localhost", "ipv6"))
def test_accepted_loopback_host_authorities_reach_health_route(
    installed_server: object,
    host_kind: str,
) -> None:
    authority = installed_server.origin.removeprefix("http://")
    port = authority.rsplit(":", 1)[1]
    hosts = {"exact": authority, "localhost": f"localhost:{port}", "ipv6": f"[::1]:{port}"}
    connection = http.client.HTTPConnection(authority, timeout=5)
    connection.putrequest("GET", "/health/live", skip_host=True)
    connection.putheader("Host", hosts[host_kind])
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert response.status == 200
    assert json.loads(body)["status"] == "live"


@pytest.mark.browser
def test_duplicate_conflicting_host_headers_are_rejected(installed_server: object) -> None:
    authority = installed_server.origin.removeprefix("http://")
    connection = http.client.HTTPConnection(authority, timeout=5)
    connection.putrequest("GET", "/health/live", skip_host=True)
    connection.putheader("Host", authority)
    connection.putheader("Host", "remote.invalid")
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert response.status in {400, 403}
    assert b"remote.invalid" not in body


@pytest.mark.browser
def test_missing_host_header_is_rejected_before_routing(installed_server: object) -> None:
    authority = installed_server.origin.removeprefix("http://")
    connection = http.client.HTTPConnection(authority, timeout=5)
    connection.putrequest("GET", "/health/live", skip_host=True)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert response.status in {400, 403}
    assert b'"ready"' not in body


@pytest.mark.browser
@pytest.mark.parametrize(
    "path",
    (
        "/imports/uploads",
        "/imports/root-selections",
        "/jobs/00000000-0000-4000-8000-000000000001/retry",
        "/jobs/00000000-0000-4000-8000-000000000001/cancel",
        "/players/identity/previews/merge",
        "/players/identity/previews/split",
        "/players/identity/previews/inverse",
        "/players/identity/merge",
        "/players/identity/split",
        "/players/identity/inverse",
        "/settings/preview",
        "/settings/apply",
        "/settings/diagnostics/ollama",
    ),
)
def test_every_unsafe_route_family_rejects_missing_origin_without_echo(
    installed_server: object,
    path: str,
) -> None:
    origin = installed_server.origin
    request = urllib.request.Request(
        f"{origin}{path}",
        data=b"csrf_token=task9-secret-canary",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 403
    assert b"task9-secret-canary" not in caught.value.read()


@pytest.mark.browser
@pytest.mark.parametrize(
    "request_origin",
    (
        None,
        "null",
        "https://127.0.0.1",
        "http://127.0.0.1.evil",
        "http://remote.invalid",
        "http://localhost:65534",
        "http://127.0.0.1:65534",
        "http://127.0.0.1:8765, http://remote.invalid",
        "http://[::1",
        "http://user@127.0.0.1:8765",
    ),
)
def test_unsafe_requests_reject_every_nonexact_origin_before_csrf(
    installed_server: object,
    request_origin: str | None,
) -> None:
    status, problem = _unsafe_response(installed_server.origin, "/imports/root-selections", request_origin)
    assert status == 403
    assert problem["code"] == "origin_rejected"
    assert "task9-secret-canary" not in json.dumps(problem)


@pytest.mark.browser
def test_exact_origin_reaches_csrf_rejection_stage(installed_server: object) -> None:
    status, problem = _unsafe_response(
        installed_server.origin,
        "/imports/root-selections",
        installed_server.origin,
    )
    assert status == 403
    assert problem["code"] == "csrf_rejected"


@pytest.mark.browser
def test_native_csrf_token_is_bound_to_the_form_that_issued_it(
    installed_server: object,
    browser: Browser,
) -> None:
    context = browser.new_context(**deterministic_context_options({"width": 1440, "height": 900}))
    page = context.new_page()
    page.goto(f"{installed_server.origin}/settings", wait_until="domcontentloaded", timeout=30_000)
    token = page.locator('input[name="_csrf"]').first.get_attribute("value")
    assert token
    response = context.request.post(
        f"{installed_server.origin}/imports/root-selections",
        headers={"Origin": installed_server.origin, "Accept": "application/problem+json"},
        form={
            "_csrf": token,
            "root_public_id": "00000000-0000-4000-8000-000000000001",
            "relative_path": "fixture.rep",
        },
        timeout=30_000,
    )
    assert response.status == 403
    problem = response.json()
    assert problem["code"] == "csrf_rejected"
    assert token not in response.text()
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize("invalid_kind", ("missing", "empty", "altered", "duplicated"))
def test_native_upload_form_rejects_invalid_csrf_shapes_without_echo(
    installed_server: object,
    browser: Browser,
    invalid_kind: str,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options({"width": 1440, "height": 900}))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    failures = install_same_origin_guard(page, origin)
    page.goto(f"{origin}/imports/dialog", wait_until="domcontentloaded", timeout=30_000)
    token = page.locator('form[action="/imports/uploads"] input[name="_csrf"]').get_attribute("value")
    assert token
    if invalid_kind == "missing":
        body = ""
    elif invalid_kind == "empty":
        body = "_csrf="
    elif invalid_kind == "altered":
        body = f"_csrf={token}altered-canary"
    else:
        body = f"_csrf={token}&_csrf=conflicting-canary"
    response = context.request.post(
        f"{origin}/imports/uploads",
        headers={
            "Accept": "application/problem+json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
        },
        data=body,
        timeout=30_000,
    )
    assert response.status == 403
    assert response.json()["code"] == "csrf_rejected"
    assert token not in response.text()
    assert "canary" not in response.text()
    assert failures == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_native_upload_token_is_session_bound_and_one_time(
    installed_server: object,
    browser: Browser,
) -> None:
    origin = installed_server.origin
    first = browser.new_context(**deterministic_context_options({"width": 1440, "height": 900}))
    second = browser.new_context(**deterministic_context_options({"width": 1440, "height": 900}))
    first_page = first.new_page()
    second_page = second.new_page()
    first_errors = install_browser_error_guard(first_page)
    second_errors = install_browser_error_guard(second_page)
    first_failures = install_same_origin_guard(first_page, origin)
    second_failures = install_same_origin_guard(second_page, origin)
    first_page.goto(f"{origin}/imports/dialog", wait_until="domcontentloaded", timeout=30_000)
    second_page.goto(f"{origin}/imports/dialog", wait_until="domcontentloaded", timeout=30_000)
    first_token = first_page.locator(
        'form[action="/imports/uploads"] input[name="_csrf"]'
    ).get_attribute("value")
    second_token = second_page.locator(
        'form[action="/imports/uploads"] input[name="_csrf"]'
    ).get_attribute("value")
    assert first_token and second_token and first_token != second_token

    cross_session = first.request.post(
        f"{origin}/imports/uploads",
        headers={"Origin": origin, "Accept": "application/problem+json"},
        form={"_csrf": second_token},
        timeout=30_000,
    )
    assert cross_session.status == 403
    assert cross_session.json()["code"] == "csrf_rejected"

    accepted = first.request.post(
        f"{origin}/imports/uploads",
        headers={"Origin": origin, "Accept": "application/problem+json"},
        form={"_csrf": first_token},
        timeout=30_000,
    )
    assert accepted.status == 503
    assert accepted.json()["code"] == "opaque_ingress_handoff_pending"
    replayed = first.request.post(
        f"{origin}/imports/uploads",
        headers={"Origin": origin, "Accept": "application/problem+json"},
        form={"_csrf": first_token},
        timeout=30_000,
    )
    assert replayed.status == 403
    assert replayed.json()["code"] == "csrf_rejected"
    assert first_failures == []
    assert second_failures == []
    assert_browser_clean(*first_errors)
    assert_browser_clean(*second_errors)
    first.close()
    second.close()


@pytest.mark.browser
def test_problem_response_does_not_echo_internal_locator_canaries(installed_server: object) -> None:
    canaries = ("C:%5Cprivate%5Clibrary.sqlite3", "_sa_instance_state", "Traceback", "lease-token-canary")
    path = "/missing?value=" + "%20".join(canaries)
    status, headers, body = _response(installed_server.origin, path)
    assert status == 404
    combined = body + str(headers).encode()
    assert not any(canary.replace("%5C", "\\").encode() in combined for canary in canaries)
