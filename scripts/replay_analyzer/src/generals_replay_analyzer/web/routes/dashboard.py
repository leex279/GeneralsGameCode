"""Reserved dashboard route backed only by the application port."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import problem_response
from generals_replay_analyzer.web.ports import WebApplicationPort
from generals_replay_analyzer.web.presentation.shell import accepts_html, dashboard_shell, template_response

router = APIRouter(tags=["dashboard"])


@router.get("/", summary="Dashboard")
# TheSuperHackers @feature Leex 22/08/2026 Render immutable dashboard snapshots through the package shell. (#TBD)
def dashboard(request: Request, port: Annotated[WebApplicationPort, Depends(application_port)]) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406,
            title="Not Acceptable",
            code="not_acceptable",
            detail="This route provides only text/html",
        )
    snapshot = port.dashboard()
    return template_response(
        request,
        "dashboard.html",
        dashboard_shell(snapshot),
        context={"dashboard": snapshot},
    )
