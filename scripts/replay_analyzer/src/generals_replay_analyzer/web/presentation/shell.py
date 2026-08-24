"""Immutable view contexts and package-resolved Jinja rendering."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlencode

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
    ReplayLibraryPageDTO,
    ReplayLibraryQueryDTO,
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
        return () if value in {"/", "/players", "/scouting", "/replays", "/maps", "/compare", "/jobs", "/settings"} else (value,)
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


NavigationPath = Literal["/", "/players", "/scouting", "/replays", "/maps", "/compare", "/jobs", "/settings"]


class ShellContextDTO(PresentationDTO):
    """Immutable values shared by every first-party shell page."""

    page_title: str
    current_path: NavigationPath
    navigation: tuple[NavigationItemDTO, ...]
    pipeline: PipelineStateDTO | None
    availability: AvailabilityDTO
    terminal_quality: TerminalQualityDTO | None
    correlation_id: str | None = None


_HTML_MEDIA_RANGE_PRECEDENCE = {"*/*": 0, "text/*": 1, "text/html": 2}
_QVALUE = re.compile(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)\Z")


def _navigation(current_path: NavigationPath) -> tuple[NavigationItemDTO, ...]:
    # TheSuperHackers @feature Leex 23/08/2026 Expose every installed evidence workspace through one canonical nav. (#TBD)
    return (
        NavigationItemDTO(
            label="Dashboard", href="/", active=current_path == "/", availability="available"
        ),
        NavigationItemDTO(
            label="Players", href="/players", active=current_path == "/players", availability="available"
        ),
        NavigationItemDTO(
            label="Scouting", href="/scouting", active=current_path == "/scouting", availability="available"
        ),
        NavigationItemDTO(
            label="Library", href="/replays", active=current_path == "/replays", availability="available"
        ),
        NavigationItemDTO(
            label="Maps", href="/maps", active=current_path == "/maps", availability="available"
        ),
        NavigationItemDTO(
            label="Compare", href="/compare", active=current_path == "/compare", availability="available"
        ),
        NavigationItemDTO(
            label="Jobs", href="/jobs", active=current_path == "/jobs", availability="available"
        ),
        NavigationItemDTO(
            label="Settings", href="/settings", active=current_path == "/settings", availability="available"
        ),
    )


def feature_shell(
    *,
    page_title: str,
    current_path: NavigationPath,
    availability: AvailabilityDTO,
    terminal_quality: TerminalQualityDTO | None = None,
) -> ShellContextDTO:
    """Build a canonical shell for an installed read-only feature surface."""
    return ShellContextDTO(
        page_title=page_title,
        current_path=current_path,
        navigation=_navigation(current_path),
        pipeline=None,
        availability=availability,
        terminal_quality=terminal_quality,
    )


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


class ReplayFilterChipDTO(BaseModel):
    """One deterministic removable replay-library filter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    value: str
    remove_url: str


# TheSuperHackers @feature Leex 22/08/2026 Keep replay pagination and canonical filters as a frozen presentation boundary. (#0)
class ReplayLibraryViewModel(BaseModel):
    """Immutable library display state with its canonical query URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: ReplayLibraryPageDTO
    canonical_url: str
    previous_url: str | None
    next_url: str | None
    total_pages: int
    active_filters: tuple[ReplayFilterChipDTO, ...]
    clear_url: str


_QUERY_ORDER = (
    "page",
    "page_size",
    "sort",
    "search",
    "player_public_id",
    "faction",
    "matchup",
    "map_public_id",
    "result",
    "patch",
    "strategy_id",
    "analysis_status",
    "evidence_tier",
    "lifecycle_state",
    "source_kind",
    "date_from_utc",
    "date_to_utc",
)


# TheSuperHackers @feature Leex 22/08/2026 Preserve replay filters in one deterministic public URL. (#0)
def replay_library_url(query: ReplayLibraryQueryDTO) -> str:
    values = query.model_dump(mode="json", exclude_none=True)
    return "/replays?" + urlencode(
        [
            (name, values[name])
            for name in _QUERY_ORDER
            if name in values and not (name == "sort" and values[name] == "observed_desc")
        ]
    )


_FILTER_LABELS = (
    ("search", "Search"),
    ("player_public_id", "Player"),
    ("faction", "Faction"),
    ("matchup", "Matchup"),
    ("map_public_id", "Map"),
    ("result", "Result"),
    ("patch", "Patch"),
    ("strategy_id", "Strategy"),
    ("analysis_status", "Analysis"),
    ("evidence_tier", "Evidence"),
    ("lifecycle_state", "Lifecycle"),
    ("source_kind", "Source"),
    ("date_from_utc", "Observed from"),
    ("date_to_utc", "Observed to"),
)


def _active_filter_chips(query: ReplayLibraryQueryDTO) -> tuple[ReplayFilterChipDTO, ...]:
    chips: list[ReplayFilterChipDTO] = []
    for field, label in _FILTER_LABELS:
        value = getattr(query, field)
        if value is None:
            continue
        replacement: object = None
        chips.append(
            ReplayFilterChipDTO(
                label=label,
                value=value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else str(value),
                remove_url=replay_library_url(query.model_copy(update={field: replacement, "page": 1})),
            )
        )
    return tuple(chips)


# TheSuperHackers @feature Leex 22/08/2026 Map library snapshots without deriving quality from pipeline state. (#0)
def replay_library_view(page: ReplayLibraryPageDTO) -> ReplayLibraryViewModel:
    total_pages = max(1, (page.total_items + page.page_size - 1) // page.page_size)
    previous_url = replay_library_url(page.query.model_copy(update={"page": page.page - 1})) if page.page > 1 else None
    next_url = (
        replay_library_url(page.query.model_copy(update={"page": page.page + 1})) if page.page < total_pages else None
    )
    return ReplayLibraryViewModel(
        page=page,
        canonical_url=replay_library_url(page.query),
        previous_url=previous_url,
        next_url=next_url,
        total_pages=total_pages,
        active_filters=_active_filter_chips(page.query),
        clear_url=replay_library_url(ReplayLibraryQueryDTO(page_size=page.page_size)),
    )


# TheSuperHackers @feature Leex 22/08/2026 Reuse the shell while keeping the Library navigation item current. (#0)
def replay_library_shell(availability: AvailabilityDTO) -> ShellContextDTO:
    return ShellContextDTO(
        page_title="Replay library | Generals Replay Analyzer",
        current_path="/replays",
        navigation=_navigation("/replays"),
        pipeline=None,
        availability=availability,
        terminal_quality=None,
    )


def jobs_shell(availability: AvailabilityDTO | None = None) -> ShellContextDTO:
    return ShellContextDTO(
        page_title="Analysis jobs | Generals Replay Analyzer",
        current_path="/jobs",
        navigation=_navigation("/jobs"),
        pipeline=None,
        availability=availability or AvailabilityDTO(state="available"),
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


def template_response(
    request: Request, name: str, shell: ShellContextDTO, *, context: dict[str, object] | None = None
) -> Response:
    """Render a package-owned template with no checkout-relative fallback."""
    values: dict[str, object] = {"shell": shell}
    if context is not None:
        values.update(context)
    return _TEMPLATES.TemplateResponse(request=request, name=name, context=values)
