"""Fake immutable application port used by web-boundary tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DashboardDTO,
    DiagnosticDTO,
    IdentityLandingDTO,
    ReadinessDTO,
)


class FakeWebApplicationPort:
    """Return view-safe snapshots without importing persistence code."""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def readiness(self) -> ReadinessDTO:
        diagnostics = () if self.ready else (DiagnosticDTO(code="schema_unavailable", message="Schema unavailable"),)
        return ReadinessDTO(ready=self.ready, schema_revision="accepted-head" if self.ready else None, diagnostics=diagnostics)

    def dashboard(self) -> DashboardDTO:
        return DashboardDTO(
            generated_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
            availability=AvailabilityDTO(
                state="unavailable",
                reason_codes=("analytics_adapter_pending",),
                evidence_references=(),
            ),
            pipeline_states=(),
        )

    def identity_landing(self) -> IdentityLandingDTO:
        return IdentityLandingDTO(
            generated_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
            availability=AvailabilityDTO(
                state="unavailable",
                reason_codes=("identity_adapter_pending",),
                evidence_references=(),
            ),
        )


class CountingPortFactory:
    """Count complete request scopes while yielding a fresh fake port."""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.created = 0
        self.closed = 0

    @contextmanager
    def __call__(self) -> Iterator[FakeWebApplicationPort]:
        self.created += 1
        try:
            yield FakeWebApplicationPort(ready=self.ready)
        finally:
            self.closed += 1


class RecordingBootstrapper:
    """Record the explicit lifespan boundary without touching a database."""

    def __init__(self) -> None:
        self.settings: list[object] = []

    def prepare(self, settings: object) -> None:
        self.settings.append(settings)


@pytest.fixture
def port_factory() -> CountingPortFactory:
    return CountingPortFactory()


@pytest.fixture
def bootstrapper() -> RecordingBootstrapper:
    return RecordingBootstrapper()
