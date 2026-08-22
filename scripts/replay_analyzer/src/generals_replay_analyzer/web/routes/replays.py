"""Immutable replay-library rendering through the web application port."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import problem_response
from generals_replay_analyzer.web.ports import ReplayLibraryQueryDTO, WebApplicationPort
from generals_replay_analyzer.web.presentation.shell import (
    accepts_html,
    replay_library_shell,
    replay_library_view,
    template_response,
)

router = APIRouter(tags=["replays"])


class ReplayQueryError(ValueError):
    """A controlled query error that does not let duplicate scalar keys collapse."""


def _query(request: Request) -> ReplayLibraryQueryDTO:
    """Validate one scalar value per URL key and reject unknown filter fields."""
    values = list(request.query_params.multi_items())
    if len({name for name, _value in values}) != len(values):
        raise ReplayQueryError("replay query keys must not repeat")
    return ReplayLibraryQueryDTO.model_validate(dict(values))


@router.get("/replays", summary="Replay library")
# TheSuperHackers @feature Leex 22/08/2026 Render deterministic replay snapshots without database or path access. (#0)
def replays(request: Request, port: Annotated[WebApplicationPort, Depends(application_port)]) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html")
    try:
        query = _query(request)
    except ReplayQueryError:
        return problem_response(400, title="Invalid replay filters", code="repeated_replay_query_key", detail="Replay filters must not repeat")
    except ValidationError:
        return problem_response(422, title="Invalid replay filters", code="invalid_replay_query", detail="Replay filters are invalid")
    page = port.list_replays(query)
    view = replay_library_view(page)
    shell = replay_library_shell(page.availability)
    template = "replays/_table.html" if request.headers.get("hx-request", "").casefold() == "true" else "replays/index.html"
    return template_response(request, template, shell, context={"library": view})
