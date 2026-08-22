"""Reserved dashboard route backed only by the application port."""

from typing import Annotated

from fastapi import APIRouter, Depends

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.ports import DashboardDTO, WebApplicationPort

router = APIRouter(tags=["dashboard"])


@router.get("/", response_model=DashboardDTO)
def dashboard(port: Annotated[WebApplicationPort, Depends(application_port)]) -> DashboardDTO:
    return port.dashboard()
