"""Request-scoped web application port dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Protocol

from fastapi import Request

from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DashboardDTO,
    DiagnosticDTO,
    IdentityLandingDTO,
    ReadinessDTO,
    WebApplicationPort,
)


class WebApplicationPortFactory(Protocol):
    def __call__(self) -> AbstractContextManager[WebApplicationPort]: ...


class ReadinessState(Protocol):
    def schema_revision(self) -> str | None: ...


class UnavailableWebApplicationPort:
    """Expose honest unavailable snapshots until an Analytics adapter is accepted."""

    def __init__(self, readiness: ReadinessState) -> None:
        self._readiness = readiness

    def readiness(self) -> ReadinessDTO:
        revision = self._readiness.schema_revision()
        if revision is None:
            return ReadinessDTO(
                ready=False,
                diagnostics=(DiagnosticDTO(code="bootstrap_incomplete", message="Bootstrap is incomplete"),),
            )
        return ReadinessDTO(ready=True, schema_revision=revision)

    def dashboard(self) -> DashboardDTO:
        return DashboardDTO(
            generated_at=datetime.now(UTC),
            availability=AvailabilityDTO(
                state="unavailable",
                reason_codes=("analytics_adapter_pending",),
            ),
        )

    def identity_landing(self) -> IdentityLandingDTO:
        return IdentityLandingDTO(
            generated_at=datetime.now(UTC),
            availability=AvailabilityDTO(
                state="unavailable",
                reason_codes=("identity_adapter_pending",),
            ),
        )


class UnavailablePortFactory:
    """Create one immutable null adapter for each request scope."""

    def __init__(self, readiness: ReadinessState) -> None:
        self._readiness = readiness

    @contextmanager
    def __call__(self) -> Iterator[WebApplicationPort]:
        yield UnavailableWebApplicationPort(self._readiness)


# TheSuperHackers @feature Leex 22/08/2026 Close every application scope at the request boundary. (#TBD)
def application_port(request: Request) -> Iterator[WebApplicationPort]:
    factory: WebApplicationPortFactory = request.app.state.port_factory
    with factory() as port:
        yield port
