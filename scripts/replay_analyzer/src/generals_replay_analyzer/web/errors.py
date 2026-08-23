"""Safe RFC 9457-style public problem responses."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TypeVar
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from generals_replay_analyzer.web.ports import DiagnosticDTO

_LOGGER = logging.getLogger(__name__)
_ResponseT = TypeVar("_ResponseT", bound=Response)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self'; connect-src 'self'; form-action 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
_TITLES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    503: "Service Unavailable",
}


class PublicProblem(Exception):
    """An expected, allow-listed failure safe to expose to a local client."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        detail: str,
        title: str | None = None,
        diagnostics: Sequence[DiagnosticDTO] = (),
    ) -> None:
        if status not in _TITLES:
            raise ValueError("unsupported public problem status")
        super().__init__(detail)
        self.status = status
        self.title = title or _TITLES[status]
        self.code = code
        self.detail = detail
        self.diagnostics = tuple(diagnostics)


# TheSuperHackers @feature Leex 23/08/2026 Apply one browser-isolation policy to every local response. (#TBD)
def apply_security_headers(response: _ResponseT) -> _ResponseT:
    response.headers["content-security-policy"] = CONTENT_SECURITY_POLICY
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["x-frame-options"] = "DENY"
    response.headers["cross-origin-opener-policy"] = "same-origin"
    response.headers["permissions-policy"] = PERMISSIONS_POLICY
    return response


def problem_response(
    status: int,
    *,
    title: str,
    code: str,
    detail: str,
    diagnostics: Sequence[DiagnosticDTO] = (),
) -> JSONResponse:
    """Return an explicitly allow-listed problem body without internal context."""
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
    }
    if diagnostics:
        body["diagnostics"] = [diagnostic.model_dump(mode="json") for diagnostic in diagnostics]
    return apply_security_headers(JSONResponse(body, status_code=status, media_type="application/problem+json"))


def _correlation_id(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else uuid4().hex


# TheSuperHackers @feature Leex 22/08/2026 Expose allow-listed problems and keep internal failures server-side. (#TBD)
def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(PublicProblem)
    async def public_problem_handler(_request: Request, error: PublicProblem) -> JSONResponse:
        return problem_response(
            error.status,
            title=error.title,
            code=error.code,
            detail=error.detail,
            diagnostics=error.diagnostics,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return problem_response(
            422,
            title=_TITLES[422],
            code="validation_error",
            detail="The request did not satisfy the public contract",
        )

    @app.exception_handler(HTTPException)
    async def http_handler(_request: Request, error: HTTPException) -> JSONResponse:
        status = error.status_code if error.status_code in _TITLES else 400
        code = "resource_not_found" if status == 404 else "malformed_request"
        return problem_response(
            status,
            title=_TITLES[status],
            code=code,
            detail="The requested public resource was not found" if status == 404 else "The request could not be accepted",
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, error: Exception) -> JSONResponse:
        correlation_id = _correlation_id(request)
        _LOGGER.error(
            "event=web.unhandled correlation_id=%s exception_type=%s",
            correlation_id,
            type(error).__name__,
        )
        response = problem_response(
            500,
            title="Internal Server Error",
            code="internal_error",
            detail="An unexpected internal error occurred",
        )
        response.headers["x-correlation-id"] = correlation_id
        return response
