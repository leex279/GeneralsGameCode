"""Fail-closed presentation values for fixed authoritative map scenes."""

from __future__ import annotations

from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.web.ports import (
    MapOptionDTO,
    MapRasterDescriptorDTO,
    MapSceneDTO,
    MapSceneIndexPageDTO,
)


class MapIndexViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page: MapSceneIndexPageDTO
    canonical_url: str
    previous_url: str | None
    next_url: str | None
    total_pages: int


def map_index_url(page: MapSceneIndexPageDTO, target_page: int) -> str:
    values = page.query.model_dump(mode="json", exclude_none=True)
    values["page"] = target_page
    return "/maps?" + urlencode(
        tuple(
            (name, values[name])
            for name in ("page", "page_size", "search", "availability")
            if name in values
        )
    )


# TheSuperHackers @feature Leex 23/08/2026 Preserve fixed map pagination without resolving mutable reports. (#TBD)
def map_index_view(page: MapSceneIndexPageDTO) -> MapIndexViewModel:
    total_pages = max(1, (page.total_items + page.page_size - 1) // page.page_size)
    return MapIndexViewModel(
        page=page,
        canonical_url=map_index_url(page, page.page),
        previous_url=map_index_url(page, page.page - 1) if page.page > 1 else None,
        next_url=map_index_url(page, page.page + 1) if page.page < total_pages else None,
        total_pages=total_pages,
    )


class MapRasterViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    descriptor: MapRasterDescriptorDTO
    local_href: str
    label: str


class MapSampleSummaryViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_public_id: str
    replay_player_public_id: str | None
    first_frame: int
    last_frame: int
    observed_sample_count: int


class MapDetailViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scene: MapSceneDTO
    scene_url: str
    fixed_report_href: str
    rasters: tuple[MapRasterViewModel, ...]
    player_options: tuple[MapOptionDTO, ...]
    player_centric_options: tuple[MapOptionDTO, ...]
    entity_options: tuple[MapOptionDTO, ...]
    sample_summaries: tuple[MapSampleSummaryViewModel, ...]
    player_centric_available: bool
    player_centric_reason: str | None


def map_scene_url(scene: MapSceneDTO) -> str:
    query = scene.query
    parameters: list[tuple[str, object]] = [
        ("frame_start", query.frame_start),
        ("frame_end", query.frame_end),
    ]
    parameters.extend(("player", value) for value in query.replay_player_public_ids)
    parameters.extend(("entity", value) for value in query.entity_public_ids)
    parameters.extend(("family", value) for value in query.event_families)
    if query.locomotor_surface is not None:
        parameters.append(("surface", query.locomotor_surface))
    parameters.append(("coordinate", query.coordinate_display))
    if query.player_centric_subject_public_id is not None:
        parameters.append(("subject", query.player_centric_subject_public_id))
    parameters.append(("sample_budget", query.sample_budget))
    return (
        f"/api/replays/{scene.replay_public_id}/reports/{scene.report_public_id}/map/scene?"
        + urlencode(parameters)
    )


def _raster_label(item: MapRasterDescriptorDTO) -> str:
    if item.kind == "terrain_cell_type":
        return "Terrain cell types"
    if item.locomotor_surface is None:
        raise ValueError("pathability raster is missing its proven locomotor surface")
    return f"{item.locomotor_surface.capitalize()} pathability"


# TheSuperHackers @feature Leex 23/08/2026 Derive only same-origin routes and semantic summaries from a fixed scene. (#TBD)
def map_detail_view(scene: MapSceneDTO, option_scene: MapSceneDTO | None = None) -> MapDetailViewModel:
    if (scene.query.replay_public_id, scene.query.report_public_id) != (
        scene.replay_public_id,
        scene.report_public_id,
    ):
        raise ValueError("map detail identity is inconsistent")
    options = scene if option_scene is None else option_scene
    if (options.replay_public_id, options.report_public_id, options.map_public_id) != (
        scene.replay_public_id,
        scene.report_public_id,
        scene.map_public_id,
    ):
        raise ValueError("map option identity is inconsistent")
    rasters = tuple(
        MapRasterViewModel(
            descriptor=item,
            local_href=f"/api/maps/{scene.map_public_id}/rasters/{item.raster_public_id}",
            label=_raster_label(item),
        )
        for item in scene.rasters
    )
    player_ids = set(scene.query.replay_player_public_ids)
    player_ids.update(item.subject_replay_player_public_id for item in options.transforms.player_centric)
    for start in options.starts:
        player_ids.update(start.replay_player_public_ids)
    player_ids.update(
        item.replay_player_public_id for item in options.samples if item.replay_player_public_id is not None
    )
    player_ids.update(
        item.replay_player_public_id for item in options.routes if item.replay_player_public_id is not None
    )
    player_ids.update(
        item.replay_player_public_id for item in options.structures if item.replay_player_public_id is not None
    )
    player_ids.update(item.replay_player_public_id for item in options.orders)
    for engagement in options.engagements:
        player_ids.update(engagement.participant_replay_player_public_ids)
    entity_ids = set(scene.query.entity_public_ids)
    entity_ids.update(item.entity_public_id for item in options.samples)
    entity_ids.update(item.entity_public_id for item in options.routes)
    sample_groups: dict[tuple[str, str | None], list[int]] = {}
    for sample in scene.samples:
        sample_groups.setdefault((sample.entity_public_id, sample.replay_player_public_id), []).append(sample.frame)
    sample_summaries = tuple(
        MapSampleSummaryViewModel(
            entity_public_id=entity_id,
            replay_player_public_id=player_id,
            first_frame=min(frames),
            last_frame=max(frames),
            observed_sample_count=len(frames),
        )
        for (entity_id, player_id), frames in sorted(sample_groups.items())
    )
    player_centric_available = any(
        transform.availability.state == "available" for transform in scene.transforms.player_centric
    )
    unavailable_reasons = tuple(
        reason
        for transform in scene.transforms.player_centric
        for reason in transform.availability.reason_codes
        if transform.availability.state != "available"
    )
    player_options = tuple(
        MapOptionDTO(public_id=value, label=f"Player {index}")
        for index, value in enumerate(sorted(player_ids), 1)
    )
    entity_options = tuple(
        MapOptionDTO(public_id=value, label=f"Entity {index}")
        for index, value in enumerate(sorted(entity_ids), 1)
    )
    return MapDetailViewModel(
        scene=scene,
        scene_url=map_scene_url(scene),
        fixed_report_href=f"/replays/{scene.replay_public_id}/reports/{scene.report_public_id}",
        rasters=rasters,
        player_options=player_options,
        player_centric_options=player_options,
        entity_options=entity_options,
        sample_summaries=sample_summaries,
        player_centric_available=player_centric_available,
        player_centric_reason=(unavailable_reasons[0] if unavailable_reasons else "unresolved_player_transform"),
    )
