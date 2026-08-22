"""Immutable view contexts and package-resolved Jinja rendering."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal, Self

from fastapi import Request
from jinja2 import BaseLoader, Environment, TemplateNotFound, select_autoescape
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DashboardDTO,
    IdentityLandingDTO,
    PipelineStateDTO,
    TerminalQualityDTO,
    _contains_filesystem_locator,
)
from generals_replay_analyzer.web.resources import PackagedResourceError, package_resource


# TheSuperHackers @feature Leex 22/08/2026 Apply Task 1 locator redaction to every shell-facing value. (#TBD)
class PresentationDTO(BaseModel):
    """Frozen view values that preserve the Task 1 public-locator boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _reject_private_locators(self) -> Self:
        if any(_contains_filesystem_locator(value) for value in _presentation_strings(self)):
            raise ValueError("public DTOs cannot contain filesystem paths")
        return self


def _presentation_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return () if value in {"/", "/players"} else (value,)
    if isinstance(value, BaseModel):
        return tuple(text for field in type(value).model_fields for text in _presentation_strings(getattr(value, field)))
    if isinstance(value, tuple):
        return tuple(text for item in value for text in _presentation_strings(item))
    return ()


class NavigationItemDTO(PresentationDTO):
    """One view-safe primary or upcoming navigation item."""

    label: str
    href: str | None
    active: bool
    availability: Literal["available", "unavailable"]
    unavailable_reason_code: str | None = None


class ShellContextDTO(PresentationDTO):
    """Immutable values shared by every first-party shell page."""

    page_title: str
    current_path: Literal["/", "/players"]
    navigation: tuple[NavigationItemDTO, ...]
    pipeline: PipelineStateDTO | None
    availability: AvailabilityDTO
    terminal_quality: TerminalQualityDTO | None
    correlation_id: str | None = None


_UPCOMING_ITEMS = ("Library", "Compare", "Jobs", "Settings")
_HTML_MEDIA_RANGE_PRECEDENCE = {"*/*": 0, "text/*": 1, "text/html": 2}
_QVALUE = re.compile(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)\Z")


def _navigation(current_path: Literal["/", "/players"]) -> tuple[NavigationItemDTO, ...]:
    available = (
        NavigationItemDTO(
            label="Dashboard", href="/", active=current_path == "/", availability="available"
        ),
        NavigationItemDTO(
            label="Players", href="/players", active=current_path == "/players", availability="available"
        ),
    )
    upcoming = tuple(
        NavigationItemDTO(
            label=label,
            href=None,
            active=False,
            availability="unavailable",
            unavailable_reason_code="feature_not_installed",
        )
        for label in _UPCOMING_ITEMS
    )
    return available + upcoming


def _first_pipeline(snapshot: DashboardDTO) -> PipelineStateDTO | None:
    """Select a stable shell summary without treating it as terminal quality."""
    if not snapshot.pipeline_states:
        return None
    return min(
        snapshot.pipeline_states,
        key=lambda pipeline: (
            pipeline.stage,
            pipeline.state,
            pipeline.attempt,
            pipeline.job_public_id or "",
            pipeline.replay_public_id or "",
        ),
    )


# TheSuperHackers @feature Leex 22/08/2026 Keep dashboard rendering behind immutable public snapshots. (#TBD)
def dashboard_shell(snapshot: DashboardDTO) -> ShellContextDTO:
    """Map the immutable dashboard snapshot without opening another dependency."""
    return ShellContextDTO(
        page_title="Replay dashboard | Generals Replay Analyzer",
        current_path="/",
        navigation=_navigation("/"),
        pipeline=_first_pipeline(snapshot),
        availability=snapshot.availability,
        terminal_quality=None,
    )


# TheSuperHackers @feature Leex 22/08/2026 Keep identity rendering behind immutable public snapshots. (#TBD)
def identity_shell(snapshot: IdentityLandingDTO) -> ShellContextDTO:
    """Map the immutable identity snapshot without inventing analysis state."""
    return ShellContextDTO(
        page_title="Player identity | Generals Replay Analyzer",
        current_path="/players",
        navigation=_navigation("/players"),
        pipeline=None,
        availability=snapshot.availability,
        terminal_quality=None,
    )


# TheSuperHackers @info Leex 22/08/2026 Resolve Jinja templates only from installed package resources. (#TBD)
class _PackageTemplateLoader(BaseLoader):
    """Load Jinja sources through ``importlib.resources`` only."""

    def get_source(
        self, _environment: Environment, template: str
    ) -> tuple[str, str | None, Callable[[], bool] | None]:
        if not template.endswith(".html"):
            raise TemplateNotFound(template)
        try:
            resource = package_resource(f"web/templates/{template}")
        except PackagedResourceError as error:
            raise TemplateNotFound(template) from error
        return resource.read_text(encoding="utf-8"), template, lambda: True


_TEMPLATES = Jinja2Templates(
    env=Environment(loader=_PackageTemplateLoader(), autoescape=select_autoescape(("html", "xml")))
)


# TheSuperHackers @info Leex 22/08/2026 Limit the shell to negotiated HTML without widening route dependencies. (#TBD)
def accepts_html(accept: str | None) -> bool:
    """Return whether an Accept header permits this HTML-only shell."""
    if accept is None or not accept.strip():
        return True
    qualities_by_precedence: dict[int, list[float]] = {}
    for candidate in accept.split(","):
        media_type, *parameters = (part.strip() for part in candidate.split(";"))
        precedence = _HTML_MEDIA_RANGE_PRECEDENCE.get(media_type.casefold())
        if precedence is None:
            continue
        quality = 1.0
        invalid_quality = False
        qvalue_seen = False
        for parameter in parameters:
            name, separator, value = parameter.partition("=")
            if name.strip().casefold() != "q" or not separator:
                invalid_quality = True
                continue
            normalized = value.strip()
            if qvalue_seen or _QVALUE.fullmatch(normalized) is None:
                invalid_quality = True
                continue
            qvalue_seen = True
            quality = float(normalized)
        qualities_by_precedence.setdefault(precedence, []).append(0.0 if invalid_quality else quality)
    if not qualities_by_precedence:
        return False
    most_specific = max(qualities_by_precedence)
    return min(qualities_by_precedence[most_specific]) > 0


def template_response(request: Request, name: str, shell: ShellContextDTO) -> Response:
    """Render a package-owned template with no checkout-relative fallback."""
    return _TEMPLATES.TemplateResponse(request=request, name=name, context={"shell": shell})
