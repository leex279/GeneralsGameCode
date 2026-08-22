"""Deterministic validated-grid navigation tests."""

from dataclasses import replace

import pytest

from generals_replay_analyzer.spatial.assets import Position3, SpatialSample
from generals_replay_analyzer.spatial.coordinates import GridCell, SpatialUnavailable
from generals_replay_analyzer.spatial.navigation import (
    GRID_ROUTE_VERSION,
    NavigationCache,
    build_navigation_map,
    route_between,
    route_sample_segments,
)


def test_astar_uses_fixed_cardinal_tie_path_and_exact_axis_world_distance(projection_factory) -> None:
    projection = projection_factory(width=3, height=3)
    navigation = build_navigation_map(projection, "ground")
    assert not isinstance(navigation, SpatialUnavailable)

    route = route_between(navigation, GridCell(0, 0), GridCell(2, 2))

    assert not isinstance(route, SpatialUnavailable)
    assert route.cells == (
        GridCell(0, 0),
        GridCell(1, 0),
        GridCell(2, 0),
        GridCell(2, 1),
        GridCell(2, 2),
    )
    assert route.distance_cells == 4
    assert route.distance_world == 40.0
    assert route.algorithm_version == GRID_ROUTE_VERSION


def test_ground_and_amphibious_select_only_their_validated_flags(projection_factory) -> None:
    projection = projection_factory(
        width=3,
        height=1,
        ground=(True, False, True),
        amphibious=(True, True, True),
        zones=(0, 0, 0),
    )
    ground = build_navigation_map(projection, "ground")
    amphibious = build_navigation_map(projection, "amphibious")

    assert isinstance(route_between(ground, GridCell(0, 0), GridCell(2, 0)), SpatialUnavailable)
    wet_route = route_between(amphibious, GridCell(0, 0), GridCell(2, 0))
    assert wet_route.distance_cells == 2
    assert wet_route.cells == (GridCell(0, 0), GridCell(1, 0), GridCell(2, 0))


@pytest.mark.parametrize("surface", ("ground", "amphibious"))
def test_route_never_bridges_a_validated_zone_through_another_nonzero_zone(
    projection_factory, surface
) -> None:
    projection = projection_factory(width=3, height=1, zones=(1, 2, 1))

    result = route_between(build_navigation_map(projection, surface), GridCell(0, 0), GridCell(2, 0))

    assert isinstance(result, SpatialUnavailable)
    assert result.reason == "unreachable_validated_grid_path"


@pytest.mark.parametrize(
    "zones,available",
    (
        ((1, 0, 1), True),
        ((0, 1, 1), True),
        ((0, 1, 0), False),
    ),
)
def test_route_zero_zone_is_only_a_wildcard_for_a_nonzero_endpoint_zone(
    projection_factory, zones, available
) -> None:
    projection = projection_factory(width=3, height=1, zones=zones)

    result = route_between(build_navigation_map(projection, "ground"), GridCell(0, 0), GridCell(2, 0))

    assert (not isinstance(result, SpatialUnavailable)) is available


@pytest.mark.parametrize("surface", ["air", "cliff", "rubble", "bridge", "unknown", None])
def test_unsupported_surface_never_projects_to_ground(projection_factory, surface) -> None:
    result = build_navigation_map(projection_factory(width=1, height=1), surface)
    assert isinstance(result, SpatialUnavailable)
    assert result.reason == "unsupported_or_unproven_locomotor_surface"


@pytest.mark.parametrize(
    "ground,zones,start,end,reason",
    [
        ((False,), (1,), GridCell(0, 0), GridCell(0, 0), "unreachable_validated_grid_path"),
        ((True, True), (1, 2), GridCell(0, 0), GridCell(1, 0), "unreachable_validated_grid_path"),
        ((True,), (1,), GridCell(-1, 0), GridCell(0, 0), "grid_coordinate_out_of_bounds"),
        ((True,), (1,), GridCell(0, 0), GridCell(1, 0), "grid_coordinate_out_of_bounds"),
    ],
)
def test_route_rejects_blocked_distinct_zone_and_oob_endpoints(
    projection_factory, ground, zones, start, end, reason
) -> None:
    projection = projection_factory(width=len(ground), height=1, ground=ground, amphibious=ground, zones=zones)
    navigation = build_navigation_map(projection, "ground")
    result = route_between(navigation, start, end)
    assert isinstance(result, SpatialUnavailable)
    assert result.reason == reason


@pytest.mark.parametrize("width,height", [(1, 1), (1, 7), (9, 1), (16_384, 1)])
def test_edge_grid_sizes_have_no_diagonal_or_procedural_steps(projection_factory, width, height) -> None:
    projection = projection_factory(width=width, height=height, zones=(16_383,) * (width * height))
    navigation = build_navigation_map(projection, "ground")
    route = route_between(navigation, GridCell(0, 0), GridCell(width - 1, height - 1))

    assert not isinstance(route, SpatialUnavailable)
    assert route.distance_cells == width + height - 2
    assert all(abs(left.x - right.x) + abs(left.y - right.y) == 1 for left, right in zip(route.cells, route.cells[1:]))


def test_cache_is_explicit_bounded_and_keyed_by_map_schema_digest_surface(projection_factory) -> None:
    cache = NavigationCache(max_entries=1)
    first = projection_factory(width=1, height=1, digest="a" * 64)
    second = projection_factory(width=1, height=1, digest="b" * 64)

    one = build_navigation_map(first, "ground", cache=cache)
    assert build_navigation_map(first, "ground", cache=cache) is one
    build_navigation_map(first, "amphibious", cache=cache)
    assert len(cache) == 1
    build_navigation_map(second, "ground", cache=cache)
    assert len(cache) == 1


def _sample(evidence_ref, sequence, *, frame, x, surface="ground") -> SpatialSample:
    return SpatialSample(
        evidence=evidence_ref(sequence),
        frame=frame,
        object_key="entity:1",
        owner_scope_key="player:1",
        position=Position3(x, 5.0, 0.0),
        position_bounds_policy="pathfinder_xy_closed",
        is_mobile=True,
        is_structure=False,
        is_disabled=False,
        is_engine_moving=True,
        locomotor_surface=surface,
        path_goal=None,
    )


def test_sample_segments_sort_input_and_accept_the_inclusive_maximum_gap(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=3, height=1)
    start = _sample(evidence_ref, 2, frame=10, x=5.0)
    end = _sample(evidence_ref, 1, frame=310, x=25.0)

    result = route_sample_segments(projection, (end, start), max_sample_gap_frames=300)

    assert result.quality == "complete"
    assert result.reason is None
    assert len(result.segments) == 1
    assert result.segments[0].route.distance_world == 20.0
    assert result.segments[0].evidence == (start.evidence, end.evidence)


@pytest.mark.parametrize(
    "second_change, reason",
    [
        ({"frame": 311}, "sample_gap_exceeds_policy"),
        ({"frame": 10}, "non_increasing_sample_frames"),
        ({"locomotor_surface": None}, "unsupported_or_unproven_locomotor_surface"),
        ({"locomotor_surface": "amphibious"}, "unsupported_or_unproven_locomotor_surface"),
        ({"position": Position3(35.0, 5.0, 0.0)}, "grid_coordinate_out_of_bounds"),
    ],
)
def test_sample_segments_keep_omission_reason_instead_of_interpolating(
    evidence_ref, projection_factory, second_change, reason
) -> None:
    projection = projection_factory(width=3, height=1)
    first = _sample(evidence_ref, 1, frame=10, x=5.0)
    second = replace(_sample(evidence_ref, 2, frame=20, x=25.0), **second_change)

    result = route_sample_segments(projection, (first, second), max_sample_gap_frames=300)

    assert result.quality == "unavailable"
    assert result.reason == reason
    assert result.segments == ()
    assert result.omissions[0].reason == reason


def test_sample_segments_expose_partial_subset_and_never_use_path_goal(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=4, height=1)
    first = replace(_sample(evidence_ref, 1, frame=0, x=5.0), path_goal=Position3(35.0, 5.0, 0.0))
    second = _sample(evidence_ref, 2, frame=10, x=15.0)
    third = _sample(evidence_ref, 3, frame=311, x=35.0)

    result = route_sample_segments(projection, (third, first, second), max_sample_gap_frames=300)

    assert result.quality == "partial"
    assert result.reason == "sample_gap_exceeds_policy"
    assert len(result.segments) == 1
    assert result.segments[0].route.cells[-1] == GridCell(1, 0)
    assert result.omissions[0].evidence == (second.evidence, third.evidence)


def test_sample_segments_preserve_position_exempt_sample_as_an_exact_omission(
    evidence_ref, projection_factory
) -> None:
    first = _sample(evidence_ref, 1, frame=0, x=5.0)
    second = _sample(evidence_ref, 2, frame=10, x=15.0)
    exempt = replace(
        _sample(evidence_ref, 3, frame=20, x=25.0),
        position_bounds_policy="position_exempt",
    )

    result = route_sample_segments(projection_factory(width=3, height=1), (exempt, second, first))

    assert result.quality == "partial"
    assert result.reason == "position_exempt_spatial_sample"
    assert len(result.segments) == 1
    assert result.omissions[0].evidence == (exempt.evidence,)
    assert result.omissions[0].reason == "position_exempt_spatial_sample"


def test_navigation_cache_and_gap_policy_reject_nonpositive_bounds(projection_factory) -> None:
    with pytest.raises(ValueError, match="cache maximum"):
        NavigationCache(0)
    with pytest.raises(ValueError, match="sample gap"):
        route_sample_segments(projection_factory(), (), max_sample_gap_frames=0)
