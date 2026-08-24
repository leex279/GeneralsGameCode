"""Deterministic player-page presentation values."""

from __future__ import annotations

from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.web.ports import (
    PlayerIndexPageDTO,
    PlayerIndexQueryDTO,
    PlayerInsightDTO,
    PlayerProfileDTO,
)


class PlayerIndexViewModel(BaseModel):
    """Pagination values derived only from the immutable query snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: PlayerIndexPageDTO
    canonical_url: str
    previous_url: str | None
    next_url: str | None
    total_pages: int


class PlayerProfileViewModel(BaseModel):
    """Profile snapshot plus its fixed canonical JSON endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: PlayerProfileDTO
    supported_insights: tuple[PlayerInsightDTO, ...]
    canonical_json_url: str
    # TheSuperHackers @feature Leex 24/08/2026 Project fixed player evidence into a bounded opponent scouting dossier. (#TBD)
    opponent_dossier_openings: tuple[PlayerInsightDTO, ...] = ()
    opponent_dossier_factions: tuple[str, ...] = ()
    opponent_dossier_maps: tuple[str, ...] = ()
    opponent_dossier_sample_count: int = 0
    opponent_dossier_threat: str = "Unavailable"
    evidence_report_public_id: str | None = None


_INDEX_QUERY_ORDER = (
    "page",
    "page_size",
    "search",
    "faction",
    "opponent_faction",
    "map_public_id",
    "patch",
    "active_only",
    "sort",
)


def player_index_url(query: PlayerIndexQueryDTO) -> str:
    """Return one stable, public-only native-filter URL."""
    values = query.model_dump(mode="json", exclude_none=True)
    defaults = PlayerIndexQueryDTO().model_dump(mode="json", exclude_none=True)
    pairs = [
        (name, str(values[name]).lower() if isinstance(values[name], bool) else values[name])
        for name in _INDEX_QUERY_ORDER
        if name in values
        and (name == "page" and values[name] != 1 or name != "page" and values[name] != defaults.get(name))
    ]
    return "/players" + ("?" + urlencode(pairs) if pairs else "")


def player_index_view(page: PlayerIndexPageDTO) -> PlayerIndexViewModel:
    """Build deterministic pagination without re-sorting service results."""
    total_pages = max(1, (page.total_items + page.page_size - 1) // page.page_size)
    return PlayerIndexViewModel(
        page=page,
        canonical_url=player_index_url(page.query),
        previous_url=(
            player_index_url(page.query.model_copy(update={"page": page.page - 1})) if page.page > 1 else None
        ),
        next_url=(
            player_index_url(page.query.model_copy(update={"page": page.page + 1})) if page.page < total_pages else None
        ),
        total_pages=total_pages,
    )


def profile_json_url(profile: PlayerProfileDTO) -> str:
    """Expose the exact profile binding as an ordinary same-origin link."""
    query = profile.query.model_dump(mode="json", exclude_none=True)
    pairs: list[tuple[str, object]] = []
    repeated_names = {
        "longitudinal_run_ids": "longitudinal_run_id",
        "report_public_ids": "report_public_id",
    }
    for name, value in query.items():
        if name == "player_public_id":
            continue
        if name in repeated_names:
            # TheSuperHackers @fix Leex 23/08/2026 Preserve the fixed profile API's exact repeated binding names. (#TBD)
            pairs.extend((repeated_names[name], item) for item in value)
        else:
            pairs.append((name, value))
    return f"/api/players/{profile.player.player_public_id}/profile?{urlencode(pairs)}"


def player_profile_view(profile: PlayerProfileDTO) -> PlayerProfileViewModel:
    # TheSuperHackers @fix Leex 23/08/2026 Keep zero-sample placeholders out of player-facing tendency claims. (#TBD)
    supported = tuple(
        insight
        for insight in profile.insights
        if insight.availability.state in {"available", "partial"}
        and insight.raw_value is not None
        and insight.sample_count > 0
    )
    openings = tuple(item for item in supported if item.insight_kind == "recurring_opening")
    factions = tuple(
        sorted(
            {
                item.faction
                for item in profile.replay_history
                if item.faction and not item.faction.isdigit()
            }
        )
    )
    maps = tuple(sorted({item.map_display_name for item in profile.replay_history if item.map_display_name}))
    # TheSuperHackers @fix Leex 24/08/2026 Link profile evidence only when one fixed report owns the boundary. (#TBD)
    evidence_report_public_id = (
        profile.version.fixed_reports[0].report_public_id
        if len(profile.version.fixed_reports) == 1
        else None
    )
    return PlayerProfileViewModel(
        profile=profile,
        supported_insights=supported,
        canonical_json_url=profile_json_url(profile),
        opponent_dossier_openings=openings,
        opponent_dossier_factions=factions,
        opponent_dossier_maps=maps,
        opponent_dossier_sample_count=max((item.sample_count for item in openings), default=0),
        evidence_report_public_id=evidence_report_public_id,
    )
