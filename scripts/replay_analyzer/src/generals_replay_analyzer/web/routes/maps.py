"""Read-only fixed-report map scene routes."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    MapRasterQueryDTO,
    MapSceneIndexQueryDTO,
    MapSceneQueryDTO,
    MapSceneQueryPort,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import accepts_html, feature_shell, template_response
from generals_replay_analyzer.web.viewmodels.map import map_detail_view, map_index_view

router = APIRouter(tags=["maps"])


def _map_port(port: WebApplicationPort) -> MapSceneQueryPort:
    if not isinstance(port, MapSceneQueryPort):
        raise PublicProblem(status=503, code="map_scene_adapter_pending", detail="Map scenes are unavailable")
    return cast(MapSceneQueryPort, port)


def _public_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("invalid public ID")
    return value


def _accepts_json(value: str | None) -> bool:
    if value is None or not value.strip():
        return True
    for candidate in value.split(","):
        media_type, *parameters = (part.strip() for part in candidate.split(";"))
        if media_type.casefold() not in {"application/json", "application/*", "*/*"}:
            continue
        quality = 1.0
        valid = True
        for parameter in parameters:
            name, separator, raw_value = parameter.partition("=")
            if name.casefold() != "q" or not separator:
                continue
            try:
                quality = float(raw_value)
            except ValueError:
                valid = False
            if not 0.0 <= quality <= 1.0:
                valid = False
        if valid and quality > 0.0:
            return True
    return False


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@router.get("/maps", summary="Completed map scenes")
# TheSuperHackers @feature Leex 23/08/2026 List only fixed completed map scenes through the immutable query port. (#TBD)
def maps_index(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
        )
    allowed = {"page", "page_size", "search", "availability"}
    if set(request.query_params).difference(allowed) or any(
        len(request.query_params.getlist(name)) > 1 for name in allowed
    ):
        return problem_response(
            422, title="Invalid map query", code="invalid_map_query", detail="Map query is invalid"
        )
    try:
        query = MapSceneIndexQueryDTO.model_validate(
            {
                "page": request.query_params.get("page", 1),
                "page_size": request.query_params.get("page_size", 25),
                "search": request.query_params.get("search") or None,
                "availability": request.query_params.get("availability") or None,
            }
        )
    except ValidationError:
        return problem_response(
            422, title="Invalid map query", code="invalid_map_query", detail="Map query is invalid"
        )
    page = _map_port(port).list_scenes(query)
    if page.query != query:
        raise PublicProblem(status=409, code="map_scene_identity_mismatch", detail="Map page identity is inconsistent")
    shell = feature_shell(
        page_title="Map scenes | Generals Replay Analyzer",
        current_path="/maps",
        availability=page.availability,
    )
    return template_response(
        request,
        "maps/index.html",
        shell,
        context={"maps": map_index_view(page)},
    )


@router.get("/replays/{replay_public_id}/reports/{report_public_id}/map", summary="Fixed report map")
# TheSuperHackers @feature Leex 23/08/2026 Render one immutable report-scoped map without latest fall-forward. (#TBD)
def fixed_map(
    replay_public_id: str,
    report_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
        )
    allowed = {
        "frame_start", "frame_end", "player", "entity", "family", "surface", "coordinate", "subject", "sample_budget"
    }
    scalar = allowed.difference({"player", "entity", "family"})
    if set(request.query_params).difference(allowed) or any(
        len(request.query_params.getlist(name)) > 1 for name in scalar
    ):
        return problem_response(
            422, title="Invalid map scene query", code="invalid_map_scene_query", detail="Map scene query is invalid"
        )
    try:
        frame_start = request.query_params.get("frame_start", 0)
        values = {
            "replay_public_id": _public_id(replay_public_id),
            "report_public_id": _public_id(report_public_id),
            "frame_start": frame_start,
            "frame_end": request.query_params.get("frame_end", frame_start),
            "replay_player_public_ids": tuple(request.query_params.getlist("player")),
            "entity_public_ids": tuple(request.query_params.getlist("entity")),
            "event_families": tuple(request.query_params.getlist("family")),
            "locomotor_surface": request.query_params.get("surface") or None,
            "coordinate_display": request.query_params.get("coordinate") or "raw",
            "player_centric_subject_public_id": request.query_params.get("subject") or None,
            "sample_budget": request.query_params.get("sample_budget", 5000),
        }
        query = MapSceneQueryDTO.model_validate(values)
    except (ValueError, ValidationError):
        return problem_response(
            422, title="Invalid map scene query", code="invalid_map_scene_query", detail="Map scene query is invalid"
        )
    map_port = _map_port(port)
    scene = map_port.get_scene(query)
    if "frame_end" not in request.query_params:
        values["frame_end"] = scene.available_frame_window.frame_end
        query = MapSceneQueryDTO.model_validate(values)
        scene = map_port.get_scene(query)
    if scene.query != query or (scene.replay_public_id, scene.report_public_id) != (
        query.replay_public_id,
        query.report_public_id,
    ):
        raise PublicProblem(status=409, code="map_scene_identity_mismatch", detail="Map scene identity is inconsistent")
    option_scene = scene
    if (
        query.replay_player_public_ids
        or query.entity_public_ids
        or query.event_families
        or query.locomotor_surface is not None
    ):
        option_query = query.model_copy(
            update={
                "replay_player_public_ids": (),
                "entity_public_ids": (),
                "event_families": (),
                "locomotor_surface": None,
                "coordinate_display": "raw",
                "player_centric_subject_public_id": None,
                "sample_budget": 20_000,
            }
        )
        option_scene = map_port.get_scene(option_query)
        if (
            option_scene.query != option_query
            or option_scene.replay_public_id != scene.replay_public_id
            or option_scene.report_public_id != scene.report_public_id
            or option_scene.map_public_id != scene.map_public_id
        ):
            raise PublicProblem(
                status=409,
                code="map_scene_identity_mismatch",
                detail="Map option scene identity is inconsistent",
            )
    shell = feature_shell(
        page_title=f"{scene.map_display_name} | Generals Replay Analyzer",
        current_path="/maps",
        availability=scene.availability,
        terminal_quality=scene.terminal_quality,
    )
    return template_response(
        request,
        "maps/detail.html",
        shell,
        context={"map_view": map_detail_view(scene, option_scene)},
    )


@router.get(
    "/api/replays/{replay_public_id}/reports/{report_public_id}/map/scene",
    summary="Fixed authoritative map scene",
)
# TheSuperHackers @feature Leex 23/08/2026 Return canonical fixed-report map evidence without recomputation. (#TBD)
def map_scene(
    replay_public_id: str,
    report_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not _accepts_json(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only application/json"
        )
    allowed = {
        "frame_start",
        "frame_end",
        "player",
        "entity",
        "family",
        "surface",
        "coordinate",
        "subject",
        "sample_budget",
    }
    scalar = allowed.difference({"player", "entity", "family"})
    if set(request.query_params).difference(allowed) or any(
        len(request.query_params.getlist(name)) > 1 for name in scalar
    ):
        return problem_response(
            422, title="Invalid map scene query", code="invalid_map_scene_query", detail="Map scene query is invalid"
        )
    try:
        query = MapSceneQueryDTO.model_validate(
            {
                "replay_public_id": _public_id(replay_public_id),
                "report_public_id": _public_id(report_public_id),
                "frame_start": request.query_params["frame_start"],
                "frame_end": request.query_params["frame_end"],
                "replay_player_public_ids": tuple(request.query_params.getlist("player")),
                "entity_public_ids": tuple(request.query_params.getlist("entity")),
                "event_families": tuple(request.query_params.getlist("family")),
                "locomotor_surface": request.query_params.get("surface") or None,
                "coordinate_display": request.query_params.get("coordinate") or "raw",
                "player_centric_subject_public_id": request.query_params.get("subject") or None,
                "sample_budget": request.query_params.get("sample_budget", 5000),
            }
        )
    except (KeyError, ValueError, ValidationError):
        return problem_response(
            422, title="Invalid map scene query", code="invalid_map_scene_query", detail="Map scene query is invalid"
        )
    scene = _map_port(port).get_scene(query)
    if scene.query != query or (scene.replay_public_id, scene.report_public_id) != (
        query.replay_public_id,
        query.report_public_id,
    ):
        raise PublicProblem(status=409, code="map_scene_identity_mismatch", detail="Map scene identity is inconsistent")
    body = _canonical_json(scene.model_dump(mode="json"))
    etag = f'"{sha256(body).hexdigest()}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"etag": etag})
    return Response(
        body,
        media_type="application/json",
        headers={"etag": etag, "cache-control": "private, max-age=0, must-revalidate"},
    )


@router.get("/api/maps/{map_public_id}/rasters/{raster_public_id}", summary="Immutable map raster")
# TheSuperHackers @feature Leex 23/08/2026 Serve only digest-validated opaque raster bytes selected by public IDs. (#TBD)
def map_raster(
    map_public_id: str,
    raster_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        query = MapRasterQueryDTO(
            map_public_id=_public_id(map_public_id),
            raster_public_id=_public_id(raster_public_id),
        )
    except (ValueError, ValidationError):
        return problem_response(
            422, title="Invalid raster ID", code="invalid_raster_id", detail="Raster ID is invalid"
        )
    resource = _map_port(port).get_raster(query)
    if resource.map_public_id != query.map_public_id or resource.raster.raster_public_id != query.raster_public_id:
        raise PublicProblem(status=409, code="map_raster_identity_mismatch", detail="Raster identity is inconsistent")
    etag = f'"{resource.raster.content_sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"etag": etag})
    return Response(
        resource.content,
        media_type="image/png",
        headers={
            "etag": etag,
            "cache-control": "private, max-age=31536000, immutable",
            "x-content-type-options": "nosniff",
        },
    )
