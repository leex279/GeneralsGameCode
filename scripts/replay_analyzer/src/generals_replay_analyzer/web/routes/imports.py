"""Safe replay import presentation and configured-root command boundary."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response

from generals_replay_analyzer.web.dependencies import (
    RootImportForm,
    application_port,
    validated_root_import_form,
)
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import ImportSubmissionDTO, RootImportCommandDTO, WebApplicationPort
from generals_replay_analyzer.web.presentation.shell import accepts_html, replay_library_shell, template_response

router = APIRouter(tags=["imports"])


async def _csrf_guarded_root_import_form(
    request: Request,
    root_import_form: Annotated[RootImportForm, Depends(validated_root_import_form)],
) -> RootImportForm:
    if request.headers.get("x-csrf-token") is None and not request.app.state.form_csrf_token_registry.consume(
        request.cookies.get("_csrf"), root_import_form.csrf_token, request.url.path
    ):
        raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")
    return root_import_form


# TheSuperHackers @fix Leex 23/08/2026 Consume the unavailable upload form's exact token before route dispatch. (#TBD)
async def _csrf_guarded_upload_form(request: Request) -> None:
    if request.headers.get("x-csrf-token") is not None:
        return
    form = await request.form()
    if set(form) != {"_csrf"} or len(form.getlist("_csrf")) != 1:
        raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")
    token = form.get("_csrf")
    if not isinstance(token, str) or not request.app.state.form_csrf_token_registry.consume(
        request.cookies.get("_csrf"), token, request.url.path
    ):
        raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")


@router.get("/imports/dialog", summary="Replay import dialog")
# TheSuperHackers @feature Leex 22/08/2026 Show configured roots without exposing their filesystem locators. (#0)
def import_dialog(request: Request, port: Annotated[WebApplicationPort, Depends(application_port)]) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html")
    roots = port.import_roots()
    availability = next((root.availability for root in roots if root.availability.state != "available"), None)
    shell_availability = availability or (roots[0].availability if roots else _NO_ROOTS_AVAILABILITY)
    upload_token = request.app.state.form_csrf_token_registry.issue(
        "/imports/uploads", request.cookies.get("_csrf")
    )
    root_token = request.app.state.form_csrf_token_registry.issue(
        "/imports/root-selections", upload_token.cookie_value
    )
    response = template_response(
        request,
        "imports/dialog.html",
        replay_library_shell(shell_availability),
        context={
            "roots": roots,
            "csrf_token": root_token.hidden_value,
            "upload_csrf_token": upload_token.hidden_value,
            "has_available_roots": any(root.availability.state == "available" for root in roots),
        },
    )
    response.set_cookie("_csrf", upload_token.cookie_value, max_age=600, httponly=True, samesite="strict", path="/")
    return response


@router.post("/imports/uploads", summary="Replay upload unavailable")
# TheSuperHackers @feature Leex 22/08/2026 Keep multipart bytes outside web routes until opaque ingress composition is accepted. (#0)
def upload_without_ingress(
    _csrf_guard: Annotated[None, Depends(_csrf_guarded_upload_form)],
) -> Response:
    return problem_response(
        503,
        title="Import upload unavailable",
        code="opaque_ingress_handoff_pending",
        detail="Upload requires an accepted opaque ingress handoff",
    )


@router.post("/imports/root-selections", summary="Import configured replay")
# TheSuperHackers @feature Leex 22/08/2026 Submit only root public IDs and safe relative names through the command port. (#0)
def submit_root_selection(
    request: Request,
    root_import_form: Annotated[RootImportForm, Depends(_csrf_guarded_root_import_form)],
    port: Annotated[WebApplicationPort, Depends(application_port)],
) -> Response:
    try:
        command = RootImportCommandDTO(
            root_public_id=root_import_form.root_public_id,
            relative_path=root_import_form.relative_path,
        )
    except ValidationError:
        return problem_response(422, title="Invalid replay selection", code="invalid_root_selection", detail="Replay selection is invalid")
    roots = {root.root_public_id: root for root in port.import_roots()}
    root = roots.get(command.root_public_id)
    if root is None:
        return problem_response(404, title="Replay root not found", code="unknown_import_root", detail="The selected replay root is unavailable")
    if root.availability.state != "available":
        return problem_response(422, title="Replay root unavailable", code="import_root_unavailable", detail="The selected replay root is unavailable")
    submission = port.submit_root_selection(command)
    return _submission_response(submission)


def _submission_response(submission: ImportSubmissionDTO) -> Response:
    """Render only immutable port output without inferring import completion."""
    if submission.problem_code is not None:
        status = 429 if submission.problem_code == "queue_full" else 503 if submission.problem_code == "dependency_unavailable" else 422
    elif submission.duplicate_of_replay_public_id is not None:
        status = 200
    else:
        status = 201
    return JSONResponse(status_code=status, content=submission.model_dump(mode="json"))


from generals_replay_analyzer.web.ports import AvailabilityDTO

_NO_ROOTS_AVAILABILITY = AvailabilityDTO(state="unavailable", reason_codes=("import_roots_unavailable",))
