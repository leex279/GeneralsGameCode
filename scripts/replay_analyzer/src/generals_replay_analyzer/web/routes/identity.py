"""Reserved identity-management landing route."""

from typing import Annotated

from fastapi import APIRouter, Depends

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.ports import IdentityLandingDTO, WebApplicationPort

router = APIRouter(tags=["identity"])


@router.get("/players", response_model=IdentityLandingDTO, summary="Identity management")
def identity_landing(port: Annotated[WebApplicationPort, Depends(application_port)]) -> IdentityLandingDTO:
    return port.identity_landing()
