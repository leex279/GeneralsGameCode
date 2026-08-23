"""Loopback, request-forgery, headers, and safe-problem contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app, validate_loopback_host, validate_port
from generals_replay_analyzer.web.errors import PublicProblem

from .conftest import CountingPortFactory, RecordingBootstrapper


class TokenValidator:
    def accepts(self, token: str | None) -> bool:
        return token == "accepted-token"


def _secured_app(*, csrf_validator: object | None = None) -> FastAPI:
    return create_app(
        object(),
        port_factory=CountingPortFactory(),
        bootstrapper=RecordingBootstrapper(),
        csrf_validator=csrf_validator,
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_only_literal_loopback_bind_values_are_accepted(host: str) -> None:
    assert validate_loopback_host(host) == host


@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "::", "127.0.0.2", "example.test", " 127.0.0.1"])
def test_every_other_bind_value_is_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="literal loopback"):
        validate_loopback_host(host)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_tcp_ports_are_rejected_before_server_import(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        validate_port(port)


@pytest.mark.parametrize("host", ["example.test", "localhost.evil", "", "127.0.0.1@example.test"])
def test_nonlocal_or_malformed_host_header_returns_403_problem(host: str) -> None:
    with TestClient(_secured_app()) as client:
        response = client.get("/health/live", headers={"host": host})

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "host_rejected"


@pytest.mark.parametrize("host", ["localhost", "localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_local_host_headers_are_allowed(host: str) -> None:
    with TestClient(_secured_app()) as client:
        response = client.get("/health/live", headers={"host": host})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    ("http://example.test", "http://localhost:65534", "http://127.0.0.1:8765", "https://localhost:8765"),
)
def test_unsafe_request_rejects_nonexact_origin_before_csrf(origin: str) -> None:
    app = _secured_app(csrf_validator=TokenValidator())

    @app.post("/test-command")
    def command() -> dict[str, bool]:
        return {"accepted": True}

    with TestClient(app) as client:
        response = client.post(
            "/test-command",
            headers={"host": "localhost:8765", "origin": origin, "x-csrf-token": "accepted-token"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "origin_rejected"


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_unsafe_local_request_requires_future_csrf_validator_acceptance(token: str | None) -> None:
    app = _secured_app(csrf_validator=TokenValidator())

    @app.post("/test-command")
    def command() -> dict[str, bool]:
        return {"accepted": True}

    headers = {"host": "localhost:8765", "origin": "http://localhost:8765"}
    if token is not None:
        headers["x-csrf-token"] = token
    with TestClient(app) as client:
        response = client.post("/test-command", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"


def test_unsafe_local_request_can_pass_an_explicit_csrf_validator() -> None:
    app = _secured_app(csrf_validator=TokenValidator())

    @app.post("/test-command")
    def command() -> dict[str, bool]:
        return {"accepted": True}

    with TestClient(app) as client:
        response = client.post(
            "/test-command",
            headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1:8765",
                "x-csrf-token": "accepted-token",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}


def test_security_headers_apply_to_success_and_problem_responses() -> None:
    with TestClient(_secured_app()) as client:
        success = client.get("/health/live", headers={"host": "localhost"})
        problem = client.get("/missing", headers={"host": "localhost"})

    for response in (success, problem):
        assert response.headers["content-security-policy"] == (
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self'; connect-src 'self'; form-action 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert response.headers["permissions-policy"] == (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
    assert problem.status_code == 404
    assert problem.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_framework_documentation_routes_are_disabled_without_remote_assets(path: str) -> None:
    with TestClient(_secured_app()) as client:
        response = client.get(path, headers={"host": "localhost"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "cdn" not in response.text.casefold()
    assert "http://" not in response.text.casefold()
    assert "https://" not in response.text.casefold()


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422, 429, 503])
def test_expected_public_failures_keep_problem_media_type_and_status(status: int) -> None:
    app = _secured_app()

    @app.get("/expected-failure")
    def expected_failure() -> None:
        raise PublicProblem(status=status, code=f"public_{status}", detail="Safe public detail")

    with TestClient(app) as client:
        response = client.get("/expected-failure", headers={"host": "localhost"})

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == f"public_{status}"


def test_unhandled_failure_logs_correlation_id_but_leaks_no_path_secret_or_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    leaked_path = tmp_path / "private.sqlite3"
    app = _secured_app()

    @app.get("/unhandled")
    def unhandled() -> None:
        raise RuntimeError(f"database secret-token at {leaked_path}")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/unhandled", headers={"host": "localhost"})

    body = response.text
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["x-correlation-id"]
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self'; connect-src 'self'; form-action 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "secret-token" not in body
    assert str(leaked_path) not in body
    assert "RuntimeError" not in body
    assert response.headers["x-correlation-id"] in caplog.text
    assert "RuntimeError" in caplog.text
    assert "secret-token" not in caplog.text
    assert str(leaked_path) not in caplog.text
    assert "Traceback" not in caplog.text
