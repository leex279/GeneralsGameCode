"""Production adapter from immutable spatial read models to frozen Web DTOs."""

from __future__ import annotations

from pydantic import ValidationError

from generals_replay_analyzer.features.evidence import thaw_canonical
from generals_replay_analyzer.report.query import ReportGraphNotFoundError
from generals_replay_analyzer.spatial.query import (
    MapRasterReadQuery,
    MapSceneContractError,
    MapSceneIndexReadQuery,
    MapSceneNotFoundError,
    MapSceneQueryService,
    MapSceneReadQuery,
    RasterUnavailableError,
)
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    MapRasterDescriptorDTO,
    MapRasterQueryDTO,
    MapRasterResourceDTO,
    MapSceneDTO,
    MapSceneIndexPageDTO,
    MapSceneIndexQueryDTO,
    MapSceneQueryDTO,
)


class AnalyticsMapSceneAdapter:
    """Read-only path-free projection for completed map scenes and rasters."""

    def __init__(self, service: MapSceneQueryService) -> None:
        if type(service) is not MapSceneQueryService:
            raise TypeError("service must be a MapSceneQueryService")
        self._service = service

    @staticmethod
    def _not_found(error: Exception) -> PublicProblem:
        return PublicProblem(
            status=404,
            code="map_scene_not_found",
            detail="The requested fixed map scene was not found",
        )

    @staticmethod
    def _unavailable(error: Exception, *, code: str = "map_scene_unavailable") -> PublicProblem:
        return PublicProblem(
            status=503,
            code=code,
            detail="The authoritative map evidence is temporarily unavailable",
        )

    # TheSuperHackers @feature Leex 23/08/2026 Map only validated immutable scene payloads into the public Web contract. (#TBD)
    def get_scene(self, query: MapSceneQueryDTO) -> MapSceneDTO:
        try:
            result = self._service.get_scene(
                MapSceneReadQuery(
                    query.replay_public_id,
                    query.report_public_id,
                    query.frame_start,
                    query.frame_end,
                    query.replay_player_public_ids,
                    query.entity_public_ids,
                    query.event_families,
                    query.locomotor_surface,
                    query.coordinate_display,
                    query.player_centric_subject_public_id,
                    query.sample_budget,
                    query.include_engine_heuristics,
                )
            )
            return MapSceneDTO.model_validate(thaw_canonical(result.payload))
        except (ReportGraphNotFoundError, MapSceneNotFoundError) as error:
            raise self._not_found(error) from error
        except ValueError as error:
            raise PublicProblem(
                status=422,
                code="invalid_map_scene_query",
                detail="The map scene query is not valid for this fixed report",
            ) from error
        except (MapSceneContractError, ValidationError) as error:
            raise self._unavailable(error) from error

    # TheSuperHackers @feature Leex 23/08/2026 Preserve normalized pagination while listing only completed fixed scenes. (#TBD)
    def list_scenes(self, query: MapSceneIndexQueryDTO) -> MapSceneIndexPageDTO:
        try:
            result = self._service.list_scenes(
                MapSceneIndexReadQuery(query.page, query.page_size, query.search, query.availability)
            )
            return MapSceneIndexPageDTO.model_validate(thaw_canonical(result.payload))
        except (MapSceneContractError, ValidationError) as error:
            raise self._unavailable(error, code="map_index_unavailable") from error

    # TheSuperHackers @feature Leex 23/08/2026 Bind opaque PNG bytes to one validated same-map raster descriptor. (#TBD)
    def get_raster(self, query: MapRasterQueryDTO) -> MapRasterResourceDTO:
        try:
            result = self._service.get_raster(MapRasterReadQuery(query.map_public_id, query.raster_public_id))
            return MapRasterResourceDTO(
                map_public_id=result.map_public_id,
                raster=MapRasterDescriptorDTO.model_validate(result.descriptor.as_mapping()),
                content=result.content,
            )
        except MapSceneNotFoundError as error:
            raise PublicProblem(
                status=404,
                code="map_raster_not_found",
                detail="The requested map raster was not found",
            ) from error
        except RasterUnavailableError as error:
            raise self._unavailable(error, code="map_raster_unavailable") from error
        except (MapSceneContractError, ValidationError, ValueError) as error:
            raise self._unavailable(error, code="map_raster_unavailable") from error


__all__ = ["AnalyticsMapSceneAdapter"]
