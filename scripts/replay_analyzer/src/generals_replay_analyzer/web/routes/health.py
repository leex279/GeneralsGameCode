"""Database-free liveness and port-backed readiness routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import problem_response
from generals_replay_analyzer.web.ports import WebApplicationPort

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def liveness() -> dict[str, str]:
    return {"status": "live"}


@router.get("/ready")
def readiness(port: Annotated[WebApplicationPort, Depends(application_port)]) -> JSONResponse:
    snapshot = port.readiness()
    if not snapshot.ready:
        return problem_response(
            503,
            title="Service Unavailable",
            code="not_ready",
            detail="Replay Analyzer is not ready",
            diagnostics=snapshot.diagnostics,
        )
    return JSONResponse(snapshot.model_dump(mode="json"))
