"""Contract tests for fixed authoritative map pages."""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.errors import install_problem_handlers
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    FrameWindowDTO,
    MapOptionDTO,
    MapRasterQueryDTO,
    MapRasterResourceDTO,
    MapSceneDTO,
    MapSceneIndexPageDTO,
    MapSceneIndexQueryDTO,
    MapSceneQueryDTO,
    MapSceneSummaryDTO,
)
from generals_replay_analyzer.web.routes.maps import router

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
REPORT_ID = "20000000-0000-4000-8000-000000000001"
MAP_ID = "50000000-0000-4000-8000-000000000001"
PLAYER_ID = "30000000-0000-4000-8000-000000000001"


def test_maps_router_is_available_for_serialized_registration() -> None:
    """Removing the Task 6 router must break its explicit registration seam."""
    assert router.routes


class _IndexPort:
    def __init__(self) -> None:
        self.queries: list[MapSceneIndexQueryDTO] = []

    def list_scenes(self, query: MapSceneIndexQueryDTO) -> MapSceneIndexPageDTO:
        self.queries.append(query)
        return MapSceneIndexPageDTO(
            query=query,
            items=(
                MapSceneSummaryDTO(
                    replay_public_id=REPLAY_ID,
                    report_public_id=REPORT_ID,
                    map_public_id=MAP_ID,
                    map_display_name="Tournament Desert",
                    report_version="report-v1",
                    frame_window=FrameWindowDTO(frame_start=0, frame_end=1800),
                    players=(MapOptionDTO(public_id=PLAYER_ID, label="Player X"),),
                    availability=AvailabilityDTO(state="available"),
                ),
            ),
            page=query.page,
            page_size=query.page_size,
            total_items=1,
            availability=AvailabilityDTO(state="available"),
        )

    def get_scene(self, _query: MapSceneQueryDTO) -> MapSceneDTO:
        raise AssertionError("index must not resolve a scene")

    def get_raster(self, _query: MapRasterQueryDTO) -> MapRasterResourceDTO:
        raise AssertionError("index must not resolve raster bytes")


def _client(port: _IndexPort) -> TestClient:
    app = FastAPI()

    @contextmanager
    def factory() -> Iterator[_IndexPort]:
        yield port

    app.state.port_factory = factory
    install_problem_handlers(app)
    app.include_router(router)
    return TestClient(app)


def test_maps_index_renders_only_fixed_completed_scene_links() -> None:
    """A latest-report fallback would make a published map URL mutable."""
    port = _IndexPort()

    response = _client(port).get("/maps?page=1&page_size=25&availability=available")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Tournament Desert" in response.text
    assert f'/replays/{REPLAY_ID}/reports/{REPORT_ID}/map' in response.text
    assert f'/replays/{REPLAY_ID}"' not in response.text
    assert port.queries == [
        MapSceneIndexQueryDTO(page=1, page_size=25, availability="available")
    ]


def test_maps_index_rejects_unknown_query_capabilities() -> None:
    port = _IndexPort()

    response = _client(port).get("/maps?sql_order=created_at")

    assert response.status_code == 422
    assert port.queries == []


def test_maps_index_rejects_invalid_values_and_non_html_accept() -> None:
    port = _IndexPort()
    client = _client(port)

    assert client.get("/maps?page=0").status_code == 422
    assert client.get("/maps", headers={"accept": "application/json"}).status_code == 406
    assert port.queries == []


def test_maps_index_fails_closed_without_a_map_query_port() -> None:
    app = FastAPI()

    @contextmanager
    def factory() -> Iterator[object]:
        yield object()

    app.state.port_factory = factory
    install_problem_handlers(app)
    app.include_router(router)

    response = TestClient(app).get("/maps")

    assert response.status_code == 503
    assert response.json()["code"] == "map_scene_adapter_pending"
