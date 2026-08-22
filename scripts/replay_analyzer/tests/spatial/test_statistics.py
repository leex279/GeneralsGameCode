"""Deterministic spatial settings, heatmap, presence, and bootstrap tests."""

import random
from dataclasses import replace

import numpy as np
import pytest
import scipy

from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.spatial.assets import Position3, SpatialSample, SpatialUnavailable
from generals_replay_analyzer.spatial.statistics import (
    PCG64_BOOTSTRAP_VERSION,
    SpatialAlgorithmSettings,
    bootstrap_sample_count_heatmap_interval,
    build_cell_presence_shares,
    build_sample_count_heatmap,
    derive_bootstrap_seed,
)


def _sample(evidence_ref, sequence, *, frame, x, owner="player:1") -> SpatialSample:
    return SpatialSample(
        evidence=evidence_ref(sequence),
        frame=frame,
        object_key=f"entity:{sequence}",
        owner_scope_key=owner,
        position=Position3(x, 5.0, 0.0),
        position_bounds_policy="pathfinder_xy_closed",
        is_mobile=True,
        is_structure=False,
        is_disabled=False,
        is_engine_moving=True,
        locomotor_surface="ground",
        path_goal=None,
    )


def test_settings_accept_only_the_exact_closed_positive_mapping() -> None:
    mapping = {
        "route_algorithm_version": "grid-route-v1",
        "max_sample_gap_frames": 300,
        "algorithm_schema": "spatial-algorithm-settings-v1",
        "bootstrap_resamples": 1000,
        "heatmap_algorithm_version": "sample-count-heatmap-v1",
        "engagement_gap_frames": 90,
        "bootstrap_algorithm_version": "pcg64-bootstrap-v1",
        "engagement_reachable_radius_cells": 12,
        "engagement_algorithm_version": "engagement-cluster-v1",
    }

    settings = SpatialAlgorithmSettings.from_mapping(mapping)

    assert settings == SpatialAlgorithmSettings()
    assert settings.canonical_items() == tuple(sorted(mapping.items()))
    for key, value in mapping.items():
        changed = dict(mapping)
        changed[key] = 0 if type(value) is int else "wrong"
        with pytest.raises(ValueError, match="spatial settings"):
            SpatialAlgorithmSettings.from_mapping(changed)
    with pytest.raises(ValueError, match="spatial settings"):
        SpatialAlgorithmSettings.from_mapping({**mapping, "unknown": 1})


def test_heatmap_is_sparse_sorted_and_permutation_invariant(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=3, height=1)
    samples = (
        _sample(evidence_ref, 3, frame=30, x=25.0),
        _sample(evidence_ref, 1, frame=10, x=5.0),
        _sample(evidence_ref, 2, frame=20, x=5.0),
    )
    expected = None
    shuffler = random.Random(20260822)

    for _ in range(100):
        order = list(samples)
        shuffler.shuffle(order)
        heatmap = build_sample_count_heatmap(projection, tuple(order), owner_scope_key="player:1")
        encoded = canonical_json(heatmap)
        expected = encoded if expected is None else expected
        assert encoded == expected

    assert heatmap.sample_count == 3
    assert tuple((cell.cell.x, cell.cell.y, cell.count) for cell in heatmap.cells) == ((0, 0, 2), (2, 0, 1))
    assert tuple(ref.public_id for ref in heatmap.cells[0].evidence) == tuple(sorted(ref.public_id for ref in heatmap.cells[0].evidence))
    assert heatmap.quality == "complete"


def test_heatmap_reports_partial_oob_and_unavailable_absence(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=2, height=1)
    bounded = _sample(evidence_ref, 1, frame=10, x=5.0)
    oob = replace(_sample(evidence_ref, 2, frame=20, x=15.0), position=Position3(25.0, 5.0, 0.0))

    partial = build_sample_count_heatmap(projection, (oob, bounded), owner_scope_key="player:1")
    missing = build_sample_count_heatmap(projection, (), owner_scope_key="player:1")

    assert partial.quality == "partial"
    assert partial.reason == "grid_coordinate_out_of_bounds"
    assert partial.sample_count == 1
    assert partial.omitted_evidence == (oob.evidence,)
    assert missing.quality == "unavailable"
    assert missing.reason == "missing_bounded_entity_samples"
    assert missing.sample_count == 0


def test_presence_share_requires_two_players_and_keeps_exact_evidence(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=2, height=1)
    samples = (
        _sample(evidence_ref, 3, frame=30, x=15.0, owner="player:1"),
        _sample(evidence_ref, 1, frame=10, x=5.0, owner="player:1"),
        _sample(evidence_ref, 2, frame=20, x=5.0, owner="player:2"),
        _sample(evidence_ref, 4, frame=40, x=5.0, owner="player:1"),
    )

    result = build_cell_presence_shares(projection, samples)

    assert len(result.cells) == 1
    assert result.cells[0].cell.x == 0
    assert tuple((share.scope_key, share.count, share.share) for share in result.cells[0].shares) == (
        ("player:1", 2, 2 / 3),
        ("player:2", 1, 1 / 3),
    )
    assert len(result.cells[0].evidence) == 3


def test_disjoint_bounded_players_are_a_complete_zero_shared_cell_observation(
    evidence_ref, projection_factory
) -> None:
    samples = (
        _sample(evidence_ref, 1, frame=10, x=5.0, owner="player:1"),
        _sample(evidence_ref, 2, frame=20, x=15.0, owner="player:2"),
    )

    result = build_cell_presence_shares(projection_factory(width=2, height=1), samples)

    assert result.quality == "complete"
    assert result.reason is None
    assert result.cells == ()
    assert result.input_evidence == tuple(sample.evidence for sample in samples)


def test_statistics_preserve_position_exempt_samples_as_exact_partial_omissions(
    evidence_ref, projection_factory
) -> None:
    projection = projection_factory(width=2, height=1)
    bounded = (
        _sample(evidence_ref, 1, frame=10, x=5.0, owner="player:1"),
        _sample(evidence_ref, 2, frame=20, x=5.0, owner="player:2"),
        _sample(evidence_ref, 3, frame=30, x=15.0, owner="player:1"),
    )
    exempt = replace(
        _sample(evidence_ref, 4, frame=40, x=15.0, owner="player:1"),
        position_bounds_policy="position_exempt",
    )

    heatmap = build_sample_count_heatmap(projection, (*bounded, exempt), owner_scope_key="player:1")
    presence = build_cell_presence_shares(projection, (*bounded, exempt))
    interval = bootstrap_sample_count_heatmap_interval(
        projection,
        (*bounded, exempt),
        input_digest="a" * 64,
        settings=replace(SpatialAlgorithmSettings(), bootstrap_resamples=32),
        owner_scope_key="player:1",
    )

    assert heatmap.quality == "partial"
    assert heatmap.reason == "position_exempt_spatial_sample"
    assert heatmap.omitted_evidence == (exempt.evidence,)
    assert presence.quality == "partial"
    assert presence.reason == "position_exempt_spatial_sample"
    assert presence.omitted_evidence == (exempt.evidence,)
    assert interval.quality == "partial"
    assert interval.reason == "position_exempt_spatial_sample"
    assert interval.omitted_evidence == (exempt.evidence,)


def test_seed_is_local_stable_and_changes_for_input_digest() -> None:
    first = derive_bootstrap_seed("a" * 64)
    second = derive_bootstrap_seed("b" * 64)
    assert first == "a06878c336db1ca44d234c05d42fd71031e0ea9a651c111b1b702812f7f5c027"
    assert first != second


def test_pcg64_bootstrap_is_deterministic_metadata_complete_and_does_not_touch_global_rng(
    evidence_ref, projection_factory, monkeypatch
) -> None:
    projection = projection_factory(width=2, height=1)
    samples = tuple(
        _sample(evidence_ref, sequence, frame=sequence * 10, x=5.0 if sequence < 4 else 15.0)
        for sequence in range(1, 6)
    )
    python_global_state = random.getstate()
    numpy_global_state = np.random.get_state()
    for name in ("choice", "randint", "random", "seed"):
        monkeypatch.setattr(
            np.random,
            name,
            lambda *args, _name=name, **kwargs: pytest.fail(f"module-level numpy.random.{_name} was called"),
        )
    settings = replace(SpatialAlgorithmSettings(), bootstrap_resamples=64)
    expected = None
    shuffler = random.Random(7)

    for _ in range(100):
        order = list(samples)
        shuffler.shuffle(order)
        interval = bootstrap_sample_count_heatmap_interval(
            projection, tuple(order), input_digest="a" * 64, settings=settings, owner_scope_key="player:1"
        )
        encoded = canonical_json(interval)
        expected = encoded if expected is None else expected
        assert encoded == expected

    assert random.getstate() == python_global_state
    after_numpy_state = np.random.get_state()
    assert after_numpy_state[0] == numpy_global_state[0]
    assert np.array_equal(after_numpy_state[1], numpy_global_state[1])
    assert after_numpy_state[2:] == numpy_global_state[2:]
    assert interval.algorithm_version == PCG64_BOOTSTRAP_VERSION
    assert interval.seed_hex == derive_bootstrap_seed("a" * 64)
    assert interval.resample_count == 64
    assert interval.bit_generator == "PCG64"
    assert interval.numpy_version == np.__version__
    assert interval.scipy_version == scipy.__version__
    assert interval.quantile_policy == "scipy-scoreatpercentile-fraction-v1:p025-p975"
    assert interval.statistic_name == "occupied_cell_sample_proportion"
    assert len(interval.intervals) == 2
    assert tuple(reference.public_id for reference in interval.input_evidence) == tuple(
        sorted(reference.public_id for reference in interval.input_evidence)
    )
    assert interval.quality == "complete"
    assert interval.reason is None
    assert interval.omitted_evidence == ()


def test_bootstrap_is_unavailable_below_two_samples_and_changes_with_semantic_inputs(
    evidence_ref, projection_factory
) -> None:
    projection = projection_factory(width=2, height=1)
    one = _sample(evidence_ref, 1, frame=10, x=5.0)
    unavailable = bootstrap_sample_count_heatmap_interval(
        projection, (one,), input_digest="a" * 64, settings=SpatialAlgorithmSettings(), owner_scope_key="player:1"
    )
    assert isinstance(unavailable, SpatialUnavailable)
    assert unavailable.reason == "insufficient_resample_observations"

    two = _sample(evidence_ref, 2, frame=20, x=15.0)
    base = bootstrap_sample_count_heatmap_interval(
        projection, (one, two), input_digest="a" * 64, settings=replace(SpatialAlgorithmSettings(), bootstrap_resamples=32)
    )
    changed_digest = bootstrap_sample_count_heatmap_interval(
        projection, (one, two), input_digest="b" * 64, settings=replace(SpatialAlgorithmSettings(), bootstrap_resamples=32)
    )
    changed_count = bootstrap_sample_count_heatmap_interval(
        projection, (one, two), input_digest="a" * 64, settings=replace(SpatialAlgorithmSettings(), bootstrap_resamples=33)
    )
    assert canonical_json(base) != canonical_json(changed_digest)
    assert canonical_json(base) != canonical_json(changed_count)


def test_bootstrap_marks_eligible_out_of_bounds_evidence_as_partial(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=2, height=1)
    bounded = (
        _sample(evidence_ref, 1, frame=10, x=5.0),
        _sample(evidence_ref, 2, frame=20, x=15.0),
    )
    out_of_bounds = replace(_sample(evidence_ref, 3, frame=30, x=15.0), position=Position3(25.0, 5.0, 0.0))

    result = bootstrap_sample_count_heatmap_interval(
        projection,
        (*bounded, out_of_bounds),
        input_digest="a" * 64,
        settings=replace(SpatialAlgorithmSettings(), bootstrap_resamples=32),
        owner_scope_key="player:1",
    )

    assert result.quality == "partial"
    assert result.reason == "grid_coordinate_out_of_bounds"
    assert tuple(reference.public_id for reference in result.input_evidence) == (
        bounded[0].evidence.public_id,
        bounded[1].evidence.public_id,
    )
    assert result.omitted_evidence == (out_of_bounds.evidence,)


def test_statistics_reject_invalid_digest_and_map_projection(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=2, height=1)
    invalid = replace(projection, content_sha256="A" * 64)
    samples = (
        _sample(evidence_ref, 1, frame=10, x=5.0),
        _sample(evidence_ref, 2, frame=20, x=15.0),
    )

    assert build_sample_count_heatmap(invalid, samples).reason == "invalid_validated_map_projection"
    assert build_cell_presence_shares(invalid, samples).reason == "invalid_validated_map_projection"
    unavailable = bootstrap_sample_count_heatmap_interval(
        invalid, samples, input_digest="a" * 64, settings=SpatialAlgorithmSettings()
    )
    assert isinstance(unavailable, SpatialUnavailable)
    assert unavailable.reason == "invalid_validated_map_projection"
    with pytest.raises(ValueError, match="SHA-256"):
        derive_bootstrap_seed("A" * 64)
