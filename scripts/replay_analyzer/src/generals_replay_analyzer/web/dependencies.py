"""Request-scoped web application port dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import Depends, Request

from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DashboardDTO,
    DiagnosticDTO,
    IdentityLandingDTO,
    ImportRootDTO,
    ImportSubmissionDTO,
    ReadinessDTO,
    ReplayLibraryPageDTO,
    ReplayLibraryQueryDTO,
    RootImportCommandDTO,
    UploadImportCommandDTO,
    WebApplicationPort,
)


class WebApplicationPortFactory(Protocol):
    def __call__(self) -> AbstractContextManager[WebApplicationPort]: ...


@dataclass(frozen=True)
class RootImportForm:
    """Exactly the scalar public fields accepted by a configured-root selection."""

    root_public_id: str
    relative_path: str
    csrf_token: str | None


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

    # TheSuperHackers @feature Leex 22/08/2026 Keep the null library adapter immutable until Analytics accepts a real port. (#0)
    def list_replays(self, query: ReplayLibraryQueryDTO) -> ReplayLibraryPageDTO:
        return ReplayLibraryPageDTO(
            query=query,
            items=(),
            page=query.page,
            page_size=query.page_size,
            total_items=0,
            availability=AvailabilityDTO(state="unavailable", reason_codes=("replay_library_adapter_pending",)),
        )

    def import_roots(self) -> tuple[ImportRootDTO, ...]:
        return ()

    def submit_upload(self, _command: UploadImportCommandDTO) -> ImportSubmissionDTO:
        return ImportSubmissionDTO(
            submission_public_id="123e4567-e89b-42d3-a456-426614174098",
            availability=AvailabilityDTO(state="unavailable", reason_codes=("opaque_ingress_handoff_pending",)),
            problem_code="opaque_ingress_handoff_pending",
        )

    def submit_root_selection(self, _command: RootImportCommandDTO) -> ImportSubmissionDTO:
        return ImportSubmissionDTO(
            submission_public_id="123e4567-e89b-42d3-a456-426614174099",
            availability=AvailabilityDTO(state="unavailable", reason_codes=("import_adapter_pending",)),
            problem_code="dependency_unavailable",
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


# TheSuperHackers @feature Leex 22/08/2026 Validate the full configured-root form shape before any CSRF or port dependency. (#0)
async def validated_root_import_form(request: Request) -> RootImportForm:
    """Reject unknown or repeated form values before CSRF consumption and command scope construction."""
    form = await request.form()
    fields = tuple(form.multi_items())
    allowed = frozenset({"root_public_id", "relative_path", "_csrf"})
    if any(key not in allowed for key, _value in fields):
        raise PublicProblem(status=422, code="invalid_root_selection", detail="Replay selection is invalid")

    def exactly_one(name: str, *, required: bool) -> str | None:
        values = tuple(value for key, value in fields if key == name)
        if (required and len(values) != 1) or (not required and len(values) > 1):
            raise PublicProblem(status=422, code="invalid_root_selection", detail="Replay selection is invalid")
        if not values:
            return None
        value = values[0]
        if not isinstance(value, str):
            raise PublicProblem(status=422, code="invalid_root_selection", detail="Replay selection is invalid")
        return value

    root_public_id = exactly_one("root_public_id", required=True)
    relative_path = exactly_one("relative_path", required=True)
    csrf_token = exactly_one("_csrf", required=False)
    assert isinstance(root_public_id, str)
    assert isinstance(relative_path, str)
    return RootImportForm(root_public_id=root_public_id, relative_path=relative_path, csrf_token=csrf_token)


# TheSuperHackers @feature Leex 22/08/2026 Consume one native-form CSRF token before creating an import command port scope. (#0)
def native_form_csrf_guard(
    request: Request,
    root_import_form: Annotated[RootImportForm, Depends(validated_root_import_form)],
) -> None:
    """Reject a native form before any downstream command dependency can open a port."""
    if request.headers.get("x-csrf-token") is not None:
        return
    registry = request.app.state.form_csrf_token_registry
    if registry.consume(request.cookies.get("_csrf"), root_import_form.csrf_token):
        return
    raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")


# TheSuperHackers @feature Leex 22/08/2026 Make native CSRF validation an explicit prerequisite of the command-port dependency DAG. (#0)
def csrf_guarded_application_port(
    request: Request,
    _csrf_guard: Annotated[None, Depends(native_form_csrf_guard)],
) -> Iterator[WebApplicationPort]:
    factory: WebApplicationPortFactory = request.app.state.port_factory
    with factory() as port:
        yield port
