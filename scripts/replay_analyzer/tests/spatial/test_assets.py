"""Immutable validated semantic map projection tests."""

from dataclasses import FrozenInstanceError, replace

import pytest

from generals_replay_analyzer.spatial.assets import (
    GridSpec,
    Position3,
    SpatialCombatObservation,
    SpatialProjectionUnavailable,
    SpatialSample,
    StaticObjectCategory,
    validate_map_projection,
)


def test_validated_projection_is_frozen_sorted_and_retains_only_semantic_values(projection_factory) -> None:
    projection = projection_factory(schema_version=1)

    assert validate_map_projection(projection) is projection
    assert projection.schema_version == 1
    assert projection.pathing.row_major_index(3, 2) == 11
    assert projection.static_objects[0].categories[0].name == "supply_source"
    assert not hasattr(projection, "path")
    assert not hasattr(projection, "manifest_path")
    with pytest.raises(FrozenInstanceError):
        projection.map_identity = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda item: replace(item, schema_version=3),
        lambda item: replace(item, content_sha256="A" * 64),
        lambda item: replace(item, engine_data_identity=""),
        lambda item: replace(item, ground_passable=item.ground_passable[:-1]),
        lambda item: replace(item, amphibious_passable=item.amphibious_passable[:-1]),
        lambda item: replace(item, zone_ids=item.zone_ids[:-1]),
        lambda item: replace(item, zone_ids=(16_384,) + item.zone_ids[1:]),
        lambda item: replace(item, ground_passable=(True,), amphibious_passable=(False,)),
    ],
)
def test_projection_disagreement_is_typed_unavailable(projection_factory, mutator) -> None:
    projection = projection_factory(width=1, height=1)

    result = validate_map_projection(mutator(projection))

    assert isinstance(result, SpatialProjectionUnavailable)
    assert result.reason == "invalid_validated_map_projection"


def test_static_resource_category_requires_exact_source_grounding() -> None:
    with pytest.raises(ValueError, match="category source"):
        StaticObjectCategory("supply_source", "guessed_from_template_name")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -0.0])
def test_position_rejects_noncanonical_floats(value: float) -> None:
    with pytest.raises(ValueError, match="finite|negative zero"):
        Position3(value, 0.0, 0.0)


def test_validation_never_reloads_a_map_asset(monkeypatch, projection_factory) -> None:
    import generals_replay_analyzer.telemetry.map_asset as loader

    def forbidden(*args, **kwargs):
        raise AssertionError("spatial validation must not call load_map_asset")

    monkeypatch.setattr(loader, "load_map_asset", forbidden)
    assert validate_map_projection(projection_factory()).content_sha256 == "a" * 64


@pytest.mark.parametrize(
    "arguments",
    [
        (0, 1, 0, 0, 10.0, 10.0, 0.0, 0.0, 0.0, 10.0),
        (1, 1, 0, 0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0),
        (1, 1, 0, 0, 10.0, 10.0, 1.0, 0.0, 10.0, 10.0),
        (16_384, 1025, 0, 0, 1.0, 1.0, 0.0, 0.0, 16_384.0, 1025.0),
    ],
)
def test_grid_spec_rejects_incoherent_or_unbounded_geometry(arguments) -> None:
    with pytest.raises(ValueError, match="grid|cell"):
        GridSpec(*arguments)


def test_projection_rejects_duplicate_slot_object_and_out_of_bounds_semantics(projection_factory) -> None:
    projection = projection_factory()
    duplicate_slot = replace(
        projection,
        start_positions=(projection.start_positions[0], replace(projection.start_positions[1], slot_indices=(0,))),
    )
    duplicate_object = replace(projection, static_objects=(projection.static_objects[0], projection.static_objects[0]))
    outside_start = replace(
        projection,
        start_positions=(replace(projection.start_positions[0], position=Position3(45.0, 5.0, 0.0)),),
    )

    assert validate_map_projection(duplicate_slot).reason == "invalid_validated_map_projection"
    assert validate_map_projection(duplicate_object).reason == "invalid_validated_map_projection"
    assert validate_map_projection(outside_start).reason == "invalid_validated_map_projection"


@pytest.mark.parametrize("field", ["is_mobile", "is_structure", "is_disabled", "is_engine_moving"])
def test_spatial_sample_requires_exact_boolean_facts(evidence_ref, field) -> None:
    values = {
        "evidence": evidence_ref(1),
        "frame": 1,
        "object_key": "object:1",
        "owner_scope_key": "player:1",
        "position": Position3(5.0, 5.0, 0.0),
        "position_bounds_policy": "pathfinder_xy_closed",
        "is_mobile": True,
        "is_structure": False,
        "is_disabled": False,
        "is_engine_moving": True,
        "locomotor_surface": "ground",
        "path_goal": None,
    }
    values[field] = 1

    with pytest.raises(ValueError, match="boolean"):
        SpatialSample(**values)


def test_combat_observation_requires_exact_killing_blow_boolean(evidence_ref) -> None:
    with pytest.raises(ValueError, match="killing blow"):
        SpatialCombatObservation(evidence_ref(1), 1, Position3(5.0, 5.0, 0.0), "player:1", "player:2", 1.0, 1)
