"""Immutable public-contract tests for fixed map scenes."""

import math
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import generals_replay_analyzer.report.query as report_query_module
import generals_replay_analyzer.spatial.query as spatial_query_module
import generals_replay_analyzer.web.adapters.map as map_adapter_module
import generals_replay_analyzer.web.adapters.report as report_adapter_module
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import upgrade_database
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory
from generals_replay_analyzer.web.errors import install_problem_handlers
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    CoordinateTransformsDTO,
    DownsamplingDTO,
    FrameWindowDTO,
    MapNormalizedTransformDTO,
    MapRasterDescriptorDTO,
    MapRasterQueryDTO,
    MapRasterResourceDTO,
    MapSampleDTO,
    MapSceneDTO,
    MapSceneIndexPageDTO,
    MapSceneIndexQueryDTO,
    MapSceneQueryDTO,
    NormalizedPositionDTO,
    RasterPlacementDTO,
    RawCoordinateSystemDTO,
    RawPositionDTO,
    SpatialEvidenceReferenceDTO,
    SpatialPositionDTO,
    TerminalQualityDTO,
)
from generals_replay_analyzer.web.routes.maps import router

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
REPORT_ID = "20000000-0000-4000-8000-000000000001"
PLAYER_A = "30000000-0000-4000-8000-000000000001"
PLAYER_B = "30000000-0000-4000-8000-000000000002"
ENTITY_A = "40000000-0000-4000-8000-000000000001"
MAP_ID = "50000000-0000-4000-8000-000000000001"
SAMPLE_ID = "60000000-0000-4000-8000-000000000001"
RASTER_ID = "70000000-0000-4000-8000-000000000001"
EVIDENCE_ID = "80000000-0000-4000-8000-000000000001"
OTHER_REPORT_ID = "20000000-0000-4000-8000-000000000002"
OTHER_MAP_ID = "50000000-0000-4000-8000-000000000002"
PNG = b"\x89PNG\r\n\x1a\nmap-grid-raster-v1"


def test_map_scene_query_normalizes_repeated_filters() -> None:
    """A missing normalization branch would make equivalent URLs produce distinct scenes."""
    query = MapSceneQueryDTO(
        replay_public_id=REPLAY_ID,
        report_public_id=REPORT_ID,
        frame_start=10,
        frame_end=20,
        replay_player_public_ids=(PLAYER_B, PLAYER_A, PLAYER_B),
        entity_public_ids=(ENTITY_A, ENTITY_A),
        event_families=("routes", "samples", "routes"),
    )

    assert query.replay_player_public_ids == (PLAYER_A, PLAYER_B)
    assert query.entity_public_ids == (ENTITY_A,)
    assert query.event_families == ("routes", "samples")


@pytest.mark.parametrize("frame_start, frame_end", [(-1, 0), (2, 1)])
def test_frame_windows_reject_negative_or_reversed_bounds(frame_start: int, frame_end: int) -> None:
    """Removing the inclusive-window validation would admit impossible scene filters."""
    with pytest.raises(ValidationError):
        FrameWindowDTO(frame_start=frame_start, frame_end=frame_end)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, -0.0])
def test_raw_positions_reject_noncanonical_floats(value: float) -> None:
    """Weak float validation would make canonical scene JSON unstable or invalid."""
    with pytest.raises(ValidationError):
        RawPositionDTO(x=value, y=1.0, z=2.0)


def test_spatial_evidence_requires_a_stable_public_identity() -> None:
    """Dropping public-ID validation would leak internal evidence locators into map URLs."""
    with pytest.raises(ValidationError):
        SpatialEvidenceReferenceDTO(evidence_public_id="row-42", tier="observed")


def test_availability_remains_the_shared_truthful_state_contract() -> None:
    availability = AvailabilityDTO(
        state="partial",
        reason_codes=("partial_trace",),
        evidence_references=("evidence:derived",),
    )

    assert availability.state == "partial"


def _position(x: float = 10.0, y: float = 20.0) -> SpatialPositionDTO:
    return SpatialPositionDTO(
        raw=RawPositionDTO(x=x, y=y, z=0.0),
        map_normalized=NormalizedPositionDTO(u=x / 100.0, v=y / 100.0),
    )


def _scene(query: MapSceneQueryDTO) -> MapSceneDTO:
    evidence = (SpatialEvidenceReferenceDTO(evidence_public_id=EVIDENCE_ID, tier="observed"),)
    placement = RasterPlacementDTO(
        raw_minimum_x=0.0,
        raw_minimum_y=0.0,
        raw_maximum_x=100.0,
        raw_maximum_y=100.0,
        grid_width=2,
        grid_height=2,
        source_storage_order="row_major_y_then_x_x_fastest",
        source_row_zero="minimum_world_y",
        png_row_zero="maximum_world_y",
        display_interpolation="nearest",
    )
    raster = MapRasterDescriptorDTO(
        raster_public_id=RASTER_ID,
        kind="terrain_cell_type",
        locomotor_surface=None,
        rasterization_version="map-grid-raster-v1",
        media_type="image/png",
        width=2,
        height=2,
        content_sha256=sha256(PNG).hexdigest(),
        placement=placement,
        availability=AvailabilityDTO(state="available", evidence_references=(EVIDENCE_ID,)),
    )
    sample = MapSampleDTO(
        sample_public_id=SAMPLE_ID,
        entity_public_id=ENTITY_A,
        replay_player_public_id=PLAYER_A,
        frame=query.frame_start,
        position=_position(),
        orientation=1.0,
        sample_reason="lifecycle_forced",
        locomotor_surface="ground",
        evidence=evidence,
    )
    return MapSceneDTO(
        schema_version="replay-map-scene-v1",
        replay_public_id=query.replay_public_id,
        report_public_id=query.report_public_id,
        report_version="report-v1",
        map_public_id=MAP_ID,
        map_display_name="Tournament Desert",
        map_content_sha256="a" * 64,
        map_schema_version=2,
        engine_data_identity="zero-hour-1.04",
        query=query,
        available_frame_window=FrameWindowDTO(frame_start=0, frame_end=1800),
        transforms=CoordinateTransformsDTO(
            raw=RawCoordinateSystemDTO(
                coordinate_version="engine-world-xyz-v1",
                axes=("engine_world_x", "engine_world_y", "engine_world_z"),
                units="engine_world_unit",
                minimum=RawPositionDTO(x=0.0, y=0.0, z=0.0),
                maximum=RawPositionDTO(x=100.0, y=100.0, z=10.0),
                minimum_inclusive=True,
                maximum_inclusive=True,
            ),
            map_normalized=MapNormalizedTransformDTO(
                transform_version="map-normalized-v1",
                formula="u=(x-min_x)/(max_x-min_x);v=(y-min_y)/(max_y-min_y)",
                availability=AvailabilityDTO(state="available", evidence_references=(EVIDENCE_ID,)),
            ),
            player_centric=(),
        ),
        rasters=(raster,),
        starts=(),
        resources=(),
        structures=(),
        samples=(sample,),
        orders=(),
        routes=(),
        engagements=(),
        casualties=(),
        control_windows=(),
        downsampling=DownsamplingDTO(
            algorithm_version="event-forced-stratified-v1",
            requested_sample_budget=query.sample_budget,
            original_sample_count=1,
            mandatory_sample_count=1,
            returned_sample_count=1,
            budget_exceeded_by_mandatory=False,
        ),
        availability=AvailabilityDTO(state="available", evidence_references=(EVIDENCE_ID,)),
        terminal_quality=TerminalQualityDTO(lifecycle="completed"),
    )


class _ScenePort:
    def __init__(self) -> None:
        self.scene_queries: list[MapSceneQueryDTO] = []
        self.raster_queries: list[MapRasterQueryDTO] = []

    def list_scenes(self, query: MapSceneIndexQueryDTO) -> MapSceneIndexPageDTO:
        return MapSceneIndexPageDTO(
            query=query,
            items=(),
            page=query.page,
            page_size=query.page_size,
            total_items=0,
            availability=AvailabilityDTO(state="available"),
        )

    def get_scene(self, query: MapSceneQueryDTO) -> MapSceneDTO:
        self.scene_queries.append(query)
        normalized = query.model_copy(update={"frame_end": 1800}) if query.frame_end > 1800 else query
        return _scene(normalized)

    def get_raster(self, query: MapRasterQueryDTO) -> MapRasterResourceDTO:
        self.raster_queries.append(query)
        descriptor = _scene(
            MapSceneQueryDTO(
                replay_public_id=REPLAY_ID,
                report_public_id=REPORT_ID,
                frame_start=0,
                frame_end=1800,
            )
        ).rasters[0]
        return MapRasterResourceDTO(map_public_id=MAP_ID, raster=descriptor, content=PNG)


def _scene_client(port: _ScenePort) -> TestClient:
    app = FastAPI()

    @contextmanager
    def factory() -> Iterator[_ScenePort]:
        yield port

    app.state.port_factory = factory
    install_problem_handlers(app)
    app.include_router(router)
    return TestClient(app)


def test_scene_json_is_fixed_canonical_and_normalizes_filters() -> None:
    """Unsorted duplicate filters would make equivalent evidence scenes hash differently."""
    port = _ScenePort()
    url = (
        f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/map/scene"
        f"?frame_start=0&frame_end=1800&player={PLAYER_B}&player={PLAYER_A}&player={PLAYER_B}"
        "&family=routes&family=samples&family=routes"
    )

    response = _scene_client(port).get(url, headers={"accept": "application/json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["etag"] == f'"{sha256(response.content).hexdigest()}"'
    assert response.json()["schema_version"] == "replay-map-scene-v1"
    assert response.json()["query"]["replay_player_public_ids"] == [PLAYER_A, PLAYER_B]
    assert response.json()["query"]["event_families"] == ["routes", "samples"]
    assert b"locator" not in response.content
    assert port.scene_queries[0].report_public_id == REPORT_ID


def test_scene_json_rejects_unknown_or_cross_identity_inputs() -> None:
    port = _ScenePort()
    response = _scene_client(port).get(
        f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/map/scene?frame_start=0&frame_end=10&path=x"
    )

    assert response.status_code == 422
    assert port.scene_queries == []


def test_scene_json_negotiation_validation_and_conditional_read() -> None:
    port = _ScenePort()
    client = _scene_client(port)
    url = f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/map/scene?frame_start=0&frame_end=1800"

    assert client.get(url, headers={"accept": "text/html"}).status_code == 406
    assert client.get(url, headers={"accept": "application/json;q=0.5"}).status_code == 200
    assert client.get(f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/map/scene").status_code == 422
    first = client.get(url)
    conditional = client.get(url, headers={"if-none-match": first.headers["etag"]})

    assert first.status_code == 200
    assert conditional.status_code == 304
    assert conditional.content == b""


def test_scene_dto_canonicalizes_semantically_identical_sample_order() -> None:
    """Port iteration order must not perturb canonical scene bytes or sample identity ordering."""
    query = MapSceneQueryDTO(
        replay_public_id=REPLAY_ID,
        report_public_id=REPORT_ID,
        frame_start=0,
        frame_end=1800,
    )
    first = _scene(query)
    second_sample = first.samples[0].model_copy(
        update={"sample_public_id": "60000000-0000-4000-8000-000000000002", "frame": 10}
    )
    payload = first.model_dump(mode="python")
    payload["samples"] = (second_sample, first.samples[0])
    payload["downsampling"] = first.downsampling.model_copy(
        update={"original_sample_count": 2, "mandatory_sample_count": 2, "returned_sample_count": 2}
    )

    reordered = MapSceneDTO.model_validate(payload)

    assert tuple(item.sample_public_id for item in reordered.samples) == (
        SAMPLE_ID,
        "60000000-0000-4000-8000-000000000002",
    )
    assert reordered.model_dump_json() == MapSceneDTO.model_validate(reordered.model_dump()).model_dump_json()


class _CrossIdentityScenePort(_ScenePort):
    def get_scene(self, query: MapSceneQueryDTO) -> MapSceneDTO:
        other = query.model_copy(update={"report_public_id": OTHER_REPORT_ID, "frame_end": min(query.frame_end, 1800)})
        return _scene(other)

    def get_raster(self, query: MapRasterQueryDTO) -> MapRasterResourceDTO:
        resource = super().get_raster(query)
        return resource.model_copy(update={"map_public_id": OTHER_MAP_ID})


def test_scene_and_raster_routes_reject_cross_report_or_map_membership() -> None:
    client = _scene_client(_CrossIdentityScenePort())

    detail_response = client.get(f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map")
    scene_response = client.get(
        f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/map/scene?frame_start=0&frame_end=1800"
    )
    raster_response = client.get(f"/api/maps/{MAP_ID}/rasters/{RASTER_ID}")

    assert detail_response.status_code == 409
    assert detail_response.json()["code"] == "map_scene_identity_mismatch"
    assert scene_response.status_code == 409
    assert scene_response.json()["code"] == "map_scene_identity_mismatch"
    assert raster_response.status_code == 409
    assert raster_response.json()["code"] == "map_raster_identity_mismatch"


class _StrictAvailableWindowPort(_ScenePort):
    def get_scene(self, query: MapSceneQueryDTO) -> MapSceneDTO:
        self.scene_queries.append(query)
        if query.frame_end > 1800:
            raise AssertionError("fixed page requested a frame outside the accepted report window")
        return _scene(query)


def test_fixed_page_resolves_its_default_to_the_accepted_available_window() -> None:
    """A sentinel max-int default is not a valid fixed-report scene query."""
    port = _StrictAvailableWindowPort()

    response = _scene_client(port).get(f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map")

    assert response.status_code == 200
    assert tuple((item.frame_start, item.frame_end) for item in port.scene_queries) == ((0, 0), (0, 1800))


def test_production_factory_wires_fixed_page_to_the_same_report_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "map-product"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    port = _StrictAvailableWindowPort()
    report_service = object()
    captured: dict[str, object] = {}

    def report_query_service(session_factory: object, *, settings: object) -> object:
        captured["report_sessions"] = session_factory
        captured["settings"] = settings
        return report_service

    def map_scene_service(session_factory: object, authority: object) -> object:
        captured["map_sessions"] = session_factory
        captured["authority"] = authority
        return object()

    monkeypatch.setattr(report_query_module, "ReportQueryService", report_query_service)
    monkeypatch.setattr(report_adapter_module, "AnalyticsReportAdapter", lambda _service: object())
    monkeypatch.setattr(spatial_query_module, "MapSceneQueryService", map_scene_service)
    monkeypatch.setattr(map_adapter_module, "AnalyticsMapSceneAdapter", lambda _service: port)

    app = FastAPI()
    app.state.port_factory = AnalyticsPortFactory(settings, object())  # type: ignore[arg-type]
    install_problem_handlers(app)
    app.include_router(router)
    response = TestClient(app).get(f"/replays/{REPLAY_ID}/reports/{REPORT_ID}/map")

    assert response.status_code == 200
    assert tuple((item.frame_start, item.frame_end) for item in port.scene_queries) == ((0, 0), (0, 1800))
    assert captured["authority"] is report_service
    assert captured["map_sessions"] is captured["report_sessions"]


def test_raster_resource_uses_only_public_ids_and_exact_immutable_bytes() -> None:
    port = _ScenePort()
    response = _scene_client(port).get(f"/api/maps/{MAP_ID}/rasters/{RASTER_ID}")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["etag"] == f'"{sha256(PNG).hexdigest()}"'
    assert port.raster_queries == [MapRasterQueryDTO(map_public_id=MAP_ID, raster_public_id=RASTER_ID)]


def test_raster_rejects_invalid_ids_and_supports_conditional_reads() -> None:
    port = _ScenePort()
    client = _scene_client(port)
    url = f"/api/maps/{MAP_ID}/rasters/{RASTER_ID}"

    assert client.get(f"/api/maps/not-a-uuid/rasters/{RASTER_ID}").status_code == 422
    first = client.get(url)
    conditional = client.get(url, headers={"if-none-match": first.headers["etag"]})

    assert conditional.status_code == 304
    assert conditional.content == b""
