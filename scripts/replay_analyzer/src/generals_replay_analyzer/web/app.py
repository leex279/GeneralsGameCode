"""Lazy FastAPI composition root for the loopback-only product."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from generals_replay_analyzer.web.bootstrap import BootstrapSettings
from generals_replay_analyzer.web.dependencies import WebApplicationPortFactory
from generals_replay_analyzer.web.errors import CONTENT_SECURITY_POLICY, install_problem_handlers, problem_response
from generals_replay_analyzer.web.routes import dashboard, health, identity

_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1"})
_LOCAL_HOST = re.compile(r"^(?:localhost|127\.0\.0\.1)(?::([0-9]{1,5}))?$|^\[::1\](?::([0-9]{1,5}))?$")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class Bootstrapper(Protocol):
    def prepare(self, settings: BootstrapSettings) -> object: ...


class CsrfValidator(Protocol):
    def accepts(self, token: str | None) -> bool: ...


class RejectAllCsrfValidator:
    def accepts(self, token: str | None) -> bool:
        return False


def validate_loopback_host(host: str) -> str:
    if host not in _LOOPBACK_BINDS:
        raise ValueError("web host must be a literal loopback address")
    return host


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("web port must be between 1 and 65535")
    return port


def _valid_local_host(value: str) -> bool:
    match = _LOCAL_HOST.fullmatch(value)
    if match is None:
        return False
    port_text = match.group(1) or match.group(2)
    return port_text is None or 1 <= int(port_text) <= 65535


def _valid_local_origin(value: str | None) -> bool:
    if value is None:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path or parsed.query or parsed.fragment:
        return False
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    return port is None or 1 <= port <= 65535


class LocalRequestSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, csrf_validator: CsrfValidator) -> None:
        super().__init__(app)
        self._csrf_validator = csrf_validator

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.correlation_id = uuid4().hex
        host = request.headers.get("host", "")
        response: Response
        if not _valid_local_host(host):
            response = problem_response(
                403,
                title="Forbidden",
                code="host_rejected",
                detail="The request Host is not allowed",
            )
        elif request.method.upper() not in _SAFE_METHODS and not _valid_local_origin(request.headers.get("origin")):
            response = problem_response(
                403,
                title="Forbidden",
                code="origin_rejected",
                detail="The request Origin is not allowed",
            )
        elif request.method.upper() not in _SAFE_METHODS and not self._csrf_validator.accepts(
            request.headers.get("x-csrf-token")
        ):
            response = problem_response(
                403,
                title="Forbidden",
                code="csrf_rejected",
                detail="The request CSRF token was rejected",
            )
        else:
            response = await call_next(request)
        response.headers["content-security-policy"] = CONTENT_SECURITY_POLICY
        response.headers["x-content-type-options"] = "nosniff"
        return response


# TheSuperHackers @feature Leex 22/08/2026 Bootstrap explicitly before serving without starting background work. (#TBD)
def create_app(
    settings: BootstrapSettings,
    *,
    port_factory: WebApplicationPortFactory,
    bootstrapper: Bootstrapper,
    csrf_validator: CsrfValidator | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        bootstrapper.prepare(settings)
        yield

    app = FastAPI(
        title="Generals Replay Analyzer",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.port_factory = port_factory
    install_problem_handlers(app)
    app.include_router(health.router)
    app.include_router(dashboard.router)
    app.include_router(identity.router)
    app.add_middleware(
        LocalRequestSecurityMiddleware,
        csrf_validator=csrf_validator or RejectAllCsrfValidator(),
    )
    return app
