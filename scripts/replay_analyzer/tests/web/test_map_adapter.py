from __future__ import annotations

from pathlib import Path

import pytest
from spatial.test_query import _seed_query_service, _uuid

from generals_replay_analyzer.web.adapters.map import AnalyticsMapSceneAdapter
from generals_replay_analyzer.web.dependencies import AnalyticsWebApplicationPort
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    MapRasterQueryDTO,
    MapSceneIndexQueryDTO,
    MapSceneQueryDTO,
)


def test_production_adapter_projects_migrated_fixed_scene_without_orm_or_paths(tmp_path: Path) -> None:
    # Break caught: adapter bypasses the fixed service or leaks persistence values into Web DTOs.
    service, ids = _seed_query_service(tmp_path)
    adapter = AnalyticsMapSceneAdapter(service)

    scene = adapter.get_scene(
        MapSceneQueryDTO(
            replay_public_id=ids["replay"],
            report_public_id=ids["report"],
            frame_start=0,
            frame_end=120,
            sample_budget=100,
        )
    )

    assert scene.replay_public_id == ids["replay"]
    assert scene.map_public_id == ids["map"]
    assert scene.samples[0].position.map_normalized.u == 0.25
    assert scene.samples[0].locomotor_surface is None
    assert scene.downsampling.returned_sample_count == 1
    serialized = scene.model_dump_json()
    assert "manifest.json" not in serialized
    assert "relative_path" not in serialized
    assert "_sa_instance_state" not in serialized


def test_production_adapter_serves_only_descriptor_bound_png(tmp_path: Path) -> None:
    # Break caught: raster adapter returns the wrong map/ID or skips digest validation.
    service, ids = _seed_query_service(tmp_path)
    adapter = AnalyticsMapSceneAdapter(service)
    raster_id = service.raster_public_id(ids["map"], "pathability", "ground")

    resource = adapter.get_raster(MapRasterQueryDTO(map_public_id=ids["map"], raster_public_id=raster_id))

    assert resource.map_public_id == ids["map"]
    assert resource.raster.raster_public_id == raster_id
    assert resource.raster.width == 2
    assert resource.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_production_adapter_sanitizes_unknown_fixed_membership(tmp_path: Path) -> None:
    # Break caught: internal report/storage exceptions cross the public boundary.
    service, ids = _seed_query_service(tmp_path)
    adapter = AnalyticsMapSceneAdapter(service)

    with pytest.raises(PublicProblem) as captured:
        adapter.get_scene(
            MapSceneQueryDTO(
                replay_public_id=ids["replay"],
                report_public_id=_uuid("missing-report"),
                frame_start=0,
                frame_end=120,
                sample_budget=100,
            )
        )

    assert captured.value.status == 404
    assert captured.value.code == "map_scene_not_found"
    assert "missing" not in captured.value.detail.casefold()


def test_index_adapter_keeps_query_identity_even_when_no_report_rows_exist(tmp_path: Path) -> None:
    # Break caught: index response changes normalized pagination or invents a latest scene.
    service, _ids = _seed_query_service(tmp_path)
    adapter = AnalyticsMapSceneAdapter(service)
    query = MapSceneIndexQueryDTO(page=2, page_size=25, search="Tournament")

    page = adapter.list_scenes(query)

    assert page.query == query
    assert page.page == 2
    assert page.items == ()
    assert page.availability.state == "unavailable"


def test_request_scoped_composite_wires_the_map_adapter_without_replacing_report_authority(tmp_path: Path) -> None:
    # Break caught: production routes see the report adapter but fail MapSceneQueryPort at runtime.
    service, ids = _seed_query_service(tmp_path)
    maps = AnalyticsMapSceneAdapter(service)
    reports = object()
    port = AnalyticsWebApplicationPort(object(), object(), reports, maps)  # type: ignore[arg-type]
    query = MapSceneQueryDTO(
        replay_public_id=ids["replay"],
        report_public_id=ids["report"],
        frame_start=0,
        frame_end=120,
        sample_budget=100,
    )

    assert port.get_scene(query).report_public_id == ids["report"]
