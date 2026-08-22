"""Reserved identity-management landing route."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import problem_response
from generals_replay_analyzer.web.ports import WebApplicationPort
from generals_replay_analyzer.web.presentation.shell import accepts_html, identity_shell, template_response

router = APIRouter(tags=["identity"])


@router.get("/players", summary="Identity management")
# TheSuperHackers @feature Leex 22/08/2026 Render immutable identity snapshots through the package shell. (#TBD)
def identity_landing(request: Request, port: Annotated[WebApplicationPort, Depends(application_port)]) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406,
            title="Not Acceptable",
            code="not_acceptable",
            detail="This route provides only text/html",
        )
    return template_response(request, "identity/landing.html", identity_shell(port.identity_landing()))
