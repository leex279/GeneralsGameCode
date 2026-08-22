"""Synthetic validated spatial projections used only by unit tests."""

from collections.abc import Callable

import pytest

from generals_replay_analyzer.features.evidence import EvidenceRef
from generals_replay_analyzer.spatial.assets import (
    GridSpec,
    Position3,
    SpatialMapProjection,
    StartPosition,
    StaticObjectCategory,
    StaticObjectFeature,
    WorldBounds,
)


@pytest.fixture
def evidence_ref() -> Callable[[int], EvidenceRef]:
    def build(sequence: int) -> EvidenceRef:
        return EvidenceRef(
            public_id=f"00000000-0000-4000-8000-{sequence:012d}",
            tier="observed",
            source_kind="telemetry",
            source_key=f"telemetry:{sequence:04d}",
            schema_version="telemetry-v2",
        )

    return build


@pytest.fixture
def projection_factory(evidence_ref: Callable[[int], EvidenceRef]) -> Callable[..., SpatialMapProjection]:
    def build(
        *,
        width: int = 4,
        height: int = 3,
        ground: tuple[bool, ...] | None = None,
        amphibious: tuple[bool, ...] | None = None,
        zones: tuple[int, ...] | None = None,
        digest: str = "a" * 64,
        schema_version: int = 2,
    ) -> SpatialMapProjection:
        count = width * height
        pathing = GridSpec(
            width=width,
            height=height,
            index_origin_x=0,
            index_origin_y=0,
            cell_size_x=10.0,
            cell_size_y=10.0,
            minimum_x=0.0,
            minimum_y=0.0,
            maximum_x=float(width * 10),
            maximum_y=float(height * 10),
        )
        starts = (
            StartPosition("Player_1_Start", 10, (0,), Position3(5.0, 5.0, 0.0), evidence_ref(900)),
            StartPosition(
                "Player_2_Start",
                20,
                (1,),
                Position3(float(width * 10 - 5), float(height * 10 - 5), 0.0),
                evidence_ref(901),
            ),
        )
        resource = StaticObjectFeature(
            object_id=77,
            template_name="SupplyDock",
            position=Position3(min(15.0, width * 10.0 - 5.0), min(15.0, height * 10.0 - 5.0), 0.0),
            categories=(
                StaticObjectCategory(
                    "supply_source", "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)"
                ),
            ),
            evidence=evidence_ref(902),
        )
        return SpatialMapProjection(
            schema_version=schema_version,
            content_sha256=digest,
            map_identity="maps/test/map.ini",
            engine_data_identity="catalog-v1:test",
            pathing=pathing,
            world_bounds=WorldBounds(Position3(0.0, 0.0, -10.0), Position3(float(width * 10), float(height * 10), 50.0)),
            ground_passable=ground if ground is not None else (True,) * count,
            amphibious_passable=amphibious if amphibious is not None else (True,) * count,
            zone_ids=zones if zones is not None else (1,) * count,
            start_positions=starts,
            static_objects=(resource,),
        )

    return build
