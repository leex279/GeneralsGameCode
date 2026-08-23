"""Lazy FastAPI composition root for the loopback-only product."""

from __future__ import annotations

import hmac
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from generals_replay_analyzer.web.bootstrap import BootstrapSettings
from generals_replay_analyzer.web.dependencies import WebApplicationPortFactory
from generals_replay_analyzer.web.errors import apply_security_headers, install_problem_handlers, problem_response
from generals_replay_analyzer.web.resources import package_resource
from generals_replay_analyzer.web.routes import (
    comparisons,
    dashboard,
    evidence,
    health,
    imports,
    jobs,
    maps,
    players,
    replays,
    reports,
)
from generals_replay_analyzer.web.routes import (
    settings as settings_routes,
)

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


@dataclass(frozen=True)
class IssuedFormToken:
    """One action nonce carried under a signed native-form session cookie."""

    cookie_value: str
    hidden_value: str


@dataclass(frozen=True)
class _RegisteredFormToken:
    expires_at: float
    session_nonce: str
    action_path: str


# TheSuperHackers @fix Leex 23/08/2026 Bind bounded one-time form nonces to exact actions under one signed session. (#TBD)
class OneTimeFormTokenRegistry:
    """Concurrency-safe registry for short-lived form nonces with atomic one-time consumption."""

    _MAX_TOKENS = 256
    _TTL_SECONDS = 600

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._tokens: OrderedDict[str, _RegisteredFormToken] = OrderedDict()
        from threading import Lock

        self._lock = Lock()

    def _signature(self, nonce: str) -> str:
        return hmac.new(self._secret, nonce.encode("ascii"), sha256).hexdigest()

    def _discard_expired(self, now: float) -> None:
        while self._tokens:
            nonce, token = next(iter(self._tokens.items()))
            if token.expires_at > now:
                return
            self._tokens.pop(nonce)

    def _valid_session_nonce(self, cookie_value: str | None) -> str | None:
        if cookie_value is None:
            return None
        session_nonce, separator, supplied_signature = cookie_value.partition(".")
        if (
            not separator
            or not session_nonce
            or not supplied_signature
            or not session_nonce.isascii()
            or not supplied_signature.isascii()
            or not hmac.compare_digest(supplied_signature, self._signature(session_nonce))
        ):
            return None
        return session_nonce

    def issue(self, action_path: str, cookie_value: str | None = None) -> IssuedFormToken:
        if not action_path.startswith("/") or not action_path.isascii():
            raise ValueError("form action must be an absolute local path")
        session_nonce = self._valid_session_nonce(cookie_value) if self.has_valid_cookie(cookie_value) else None
        session_nonce = session_nonce or secrets.token_urlsafe(32)
        hidden_nonce = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._discard_expired(now)
            self._tokens[hidden_nonce] = _RegisteredFormToken(
                expires_at=now + self._TTL_SECONDS,
                session_nonce=session_nonce,
                action_path=action_path,
            )
            while len(self._tokens) > self._MAX_TOKENS:
                self._tokens.popitem(last=False)
        return IssuedFormToken(
            cookie_value=f"{session_nonce}.{self._signature(session_nonce)}",
            hidden_value=hidden_nonce,
        )

    def has_valid_cookie(self, cookie_value: str | None) -> bool:
        session_nonce = self._valid_session_nonce(cookie_value)
        if session_nonce is None:
            return False
        with self._lock:
            self._discard_expired(time.monotonic())
            return any(token.session_nonce == session_nonce for token in self._tokens.values())

    def consume(
        self,
        cookie_value: str | None,
        hidden_value: str | None,
        action_path: str,
    ) -> bool:
        session_nonce = self._valid_session_nonce(cookie_value)
        if session_nonce is None or hidden_value is None or not hidden_value.isascii() or not action_path.isascii():
            return False
        with self._lock:
            self._discard_expired(time.monotonic())
            token = self._tokens.get(hidden_value)
            if (
                token is None
                or not hmac.compare_digest(token.session_nonce, session_nonce)
                or not hmac.compare_digest(token.action_path, action_path)
            ):
                return False
            self._tokens.pop(hidden_value)
            return True


def _form_csrf_token(request: Request) -> str | None:
    """Accept a header token or the strict same-origin form-token cookie without consuming request bodies."""
    return request.headers.get("x-csrf-token") or request.cookies.get("_csrf")


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


# TheSuperHackers @fix Leex 23/08/2026 Require mutation origins to match the exact loopback authority. (#TBD)
def _valid_local_origin(value: str | None, *, expected_host: str) -> bool:
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
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or (port is not None and not 1 <= port <= 65535):
        return False
    return parsed.netloc.casefold() == expected_host.casefold()


# TheSuperHackers @fix Leex 23/08/2026 Reject missing or duplicate raw Host fields before all other request checks. (#TBD)
class LocalRequestSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, csrf_validator: CsrfValidator, form_token_registry: OneTimeFormTokenRegistry) -> None:
        super().__init__(app)
        self._csrf_validator = csrf_validator
        self._form_token_registry = form_token_registry

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.correlation_id = uuid4().hex
        host_values = [value.decode("latin-1") for name, value in request.scope.get("headers", ()) if name.lower() == b"host"]
        host = host_values[0] if len(host_values) == 1 else ""
        response: Response
        if len(host_values) != 1 or not _valid_local_host(host):
            response = problem_response(
                403,
                title="Forbidden",
                code="host_rejected",
                detail="The request Host is not allowed",
            )
        elif request.method.upper() not in _SAFE_METHODS and not _valid_local_origin(
            request.headers.get("origin"), expected_host=host
        ):
            response = problem_response(
                403,
                title="Forbidden",
                code="origin_rejected",
                detail="The request Origin is not allowed",
            )
        elif request.method.upper() not in _SAFE_METHODS and not self._csrf_accepts(request):
            response = problem_response(
                403,
                title="Forbidden",
                code="csrf_rejected",
                detail="The request CSRF token was rejected",
            )
        else:
            response = await call_next(request)
        return apply_security_headers(response)

    def _csrf_accepts(self, request: Request) -> bool:
        header_token = request.headers.get("x-csrf-token")
        if header_token is not None:
            return self._csrf_validator.accepts(header_token)
        return self._form_token_registry.has_valid_cookie(request.cookies.get("_csrf"))


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
    security_validator = csrf_validator or RejectAllCsrfValidator()
    app.state.form_csrf_token_registry = OneTimeFormTokenRegistry()
    install_problem_handlers(app)
    app.include_router(health.router)
    app.include_router(dashboard.router)
    # TheSuperHackers @feature Leex 22/08/2026 Register replay-library routes through the existing request-scoped port seam. (#0)
    app.include_router(replays.router)
    app.include_router(reports.router)
    app.include_router(evidence.router)
    # TheSuperHackers @feature Leex 23/08/2026 Register installed map, player, comparison, and settings workspaces. (#TBD)
    app.include_router(maps.router)
    app.include_router(players.router)
    app.include_router(comparisons.router)
    app.include_router(settings_routes.router)
    app.include_router(imports.router)
    app.include_router(jobs.router)
    # TheSuperHackers @feature Leex 22/08/2026 Serve only package-owned local shell assets. (#TBD)
    app.mount("/static", StaticFiles(directory=str(package_resource("web/static"))), name="static")
    app.add_middleware(
        LocalRequestSecurityMiddleware,
        csrf_validator=security_validator,
        form_token_registry=app.state.form_csrf_token_registry,
    )
    return app
