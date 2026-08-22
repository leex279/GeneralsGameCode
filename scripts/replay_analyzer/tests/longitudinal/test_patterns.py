from __future__ import annotations

import pytest

from generals_replay_analyzer.longitudinal.patterns import (
    change_point_candidate,
    consistency,
    map_position_habit,
    opponent_associated_difference,
    personal_baseline_difference,
    recurring_opening,
    timing_band,
    transition_preference,
    trend,
)
from generals_replay_analyzer.longitudinal.statistics import LongitudinalObservation
from generals_replay_analyzer.spatial.features import SPATIAL_REGISTRY


def _observation(key: str, value: object, *, quality: str = "complete", reason: str | None = None) -> LongitudinalObservation:
    return LongitudinalObservation(key, f"evidence-{key}", value, quality, reason)  # type: ignore[arg-type]


def test_recurring_opening_requires_canonical_observed_prefixes_and_threshold() -> None:
    result = recurring_opening(
        (
            _observation("a", ["ChinaPowerPlant", "ChinaBarracks", "ChinaSupplyCenter"]),
            _observation("b", ["ChinaPowerPlant", "ChinaBarracks", "ChinaWarFactory"]),
            _observation("c", ["ChinaPowerPlant", "ChinaBarracks"]),
        ),
        prefix_length=2,
        minimum_sample_size=3,
        confidence_level=0.95,
    )
    assert result.quality == "complete"
    assert result.statistics["recurring_prefix"] == ["ChinaPowerPlant", "ChinaBarracks"]
    assert result.statistics["share"] == 1.0

    unsupported = recurring_opening(
        (_observation("a", None, quality="unavailable", reason="missing_build_order"),),
        prefix_length=2,
        minimum_sample_size=2,
        confidence_level=0.95,
    )
    assert unsupported.reason == "minimum_sample_not_met"


def test_timing_band_retains_frames_and_rejects_wrong_definition_contract() -> None:
    supported = timing_band(
        "build.first_completed_frame",
        (_observation("a", 30), _observation("b", 60), _observation("c", 90)),
        input_digest="c" * 64,
        minimum_sample_size=3,
        bootstrap_resamples=100,
        confidence_level=0.95,
        source_unit="frames",
        source_scope="player",
        member_windows=((0, 900), (0, 900), (0, 900)),
    )
    assert supported.statistics["unit"] == "frames"
    assert supported.statistics["median"] == 60.0
    unsupported = timing_band(
        "build.first_completed_frame",
        (_observation("a", 30), _observation("b", 60), _observation("c", 90)),
        input_digest="c" * 64,
        minimum_sample_size=3,
        bootstrap_resamples=100,
        confidence_level=0.95,
        source_unit="seconds",
        source_scope="player",
        member_windows=((0, 900), (0, 900), (0, 900)),
    )
    assert unsupported.reason == "unsupported_metric_definition"


def test_timing_band_rejects_nonidentical_member_windows() -> None:
    result = timing_band(
        "build.first_completed_frame",
        (_observation("a", 30), _observation("b", 60)),
        input_digest="c" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=100,
        confidence_level=0.95,
        source_unit="frames",
        source_scope="player",
        member_windows=((0, 900), (0, 901)),
    )
    assert result.reason == "inconsistent_member_windows"
    assert result.statistics["member_windows"] == [[0, 900], [0, 901]]


def test_personal_baseline_and_opponent_comparisons_require_disjoint_supported_cohorts() -> None:
    focal = (_observation("f1", 10.0), _observation("f2", 20.0))
    baseline = (_observation("b1", 2.0), _observation("b2", 4.0))
    result = personal_baseline_difference(
        focal,
        baseline,
        input_digest="d" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=100,
        confidence_level=0.95,
    )
    assert result.statistics["comparison_kind"] == "personal_baseline"
    assert result.statistics["raw_difference"] == 12.0

    overlapping = personal_baseline_difference(
        focal,
        (_observation("f2", 4.0), _observation("b2", 8.0)),
        input_digest="d" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=100,
        confidence_level=0.95,
    )
    assert overlapping.reason == "cohorts_not_disjoint"

    unavailable = opponent_associated_difference(
        focal,
        baseline,
        input_digest="e" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=100,
        confidence_level=0.95,
        opponent_player_public_id=None,
        controls_exact=True,
    )
    assert unavailable.reason == "missing_explicit_opponent_identity"


def test_trend_change_point_and_consistency_apply_evidence_and_time_gates() -> None:
    observations = tuple(_observation(str(index), float(index * 10)) for index in range(1, 7))
    times = (100, 200, 300, 400, 500, 600)
    trend_result = trend(
        observations,
        times,
        input_digest="f" * 64,
        minimum_sample_size=3,
        bootstrap_resamples=100,
        confidence_level=0.9,
    )
    assert trend_result.statistics["trend_kind"] == "robust_chronological_slope"
    assert trend_result.statistics["slope_per_utc_unit"] == 0.1

    no_time = trend(
        observations,
        (100,) * 6,
        input_digest="f" * 64,
        minimum_sample_size=3,
        bootstrap_resamples=100,
        confidence_level=0.9,
    )
    assert no_time.reason == "insufficient_distinct_time_points"

    change = change_point_candidate(
        tuple(_observation(str(index), value) for index, value in enumerate((1.0, 1.0, 1.0, 10.0, 10.0, 10.0))),
        times,
        input_digest="1" * 64,
        minimum_sample_size=3,
        bootstrap_resamples=100,
        confidence_level=0.9,
    )
    assert change.statistics["result_label"] == "change_point_candidate"
    assert change.statistics["split_before_member_key"] == "3"

    stable = consistency(observations, minimum_sample_size=3)
    assert stable.statistics["measure_kind"] == "descriptive_within_segment_dispersion"


def test_nonfinite_negative_zero_and_fabricated_zero_are_never_usable() -> None:
    for value in (float("nan"), float("inf"), -0.0):
        try:
            _observation("bad", value)
        except ValueError:
            pass
        else:
            raise AssertionError("noncanonical numeric input was accepted")
    fabricated = _observation("missing", None, quality="unavailable", reason="missing_source")
    assert fabricated.raw_value is None


def test_transition_and_map_habits_require_accepted_registry_and_rule_provenance() -> None:
    transitions = transition_preference(
        "production.completed_composition",
        (_observation("a", {"Tank": 2}), _observation("b", {"Tank": 2})),
        minimum_sample_size=2,
        confidence_level=0.95,
        source_kind="feature",
        assessment_method=None,
        deterministic_feature_set_provenance=True,
        registry=SPATIAL_REGISTRY,
    )
    assert transitions.statistics["pattern_kind"] == "transition_preference"
    ambiguous = transition_preference(
        "strategy.rule_assessment",
        (_observation("a", "rush"), _observation("b", "rush")),
        minimum_sample_size=2,
        confidence_level=0.95,
        source_kind="strategy_assessment",
        assessment_method="rule",
        deterministic_feature_set_provenance=False,
        registry=SPATIAL_REGISTRY,
    )
    assert ambiguous.reason == "ambiguous_feature_set_provenance"

    missing_map = map_position_habit(
        "expansion.completed_structure_positions",
        (_observation("a", [[10.0, 20.0]]), _observation("b", [[10.0, 20.0]])),
        minimum_sample_size=2,
        confidence_level=0.95,
        map_public_id=None,
        registry=SPATIAL_REGISTRY,
    )
    assert missing_map.reason == "missing_map_identity"
    supported_map = map_position_habit(
        "expansion.completed_structure_positions",
        (_observation("a", [[10.0, 20.0]]), _observation("b", [[10.0, 20.0]])),
        minimum_sample_size=2,
        confidence_level=0.95,
        map_public_id="00000000-0000-4000-8000-000000000777",
        registry=SPATIAL_REGISTRY,
    )
    assert supported_map.statistics["pattern_kind"] == "map_position_habit"


def test_pattern_provenance_boundaries_reject_noncanonical_map_and_unsupported_sources() -> None:
    observations = (_observation("a", {"Tank": 2}), _observation("b", {"Tank": 2}))
    try:
        map_position_habit(
            "expansion.completed_structure_positions",
            observations,
            minimum_sample_size=2,
            confidence_level=0.95,
            map_public_id="Tournament Desert",
            registry=SPATIAL_REGISTRY,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("noncanonical map identity was accepted")

    manual = transition_preference(
        "strategy.rule_assessment",
        observations,
        minimum_sample_size=2,
        confidence_level=0.95,
        source_kind="strategy_assessment",
        assessment_method="manual",
        deterministic_feature_set_provenance=True,
        registry=SPATIAL_REGISTRY,
    )
    unknown_feature = transition_preference(
        "invented.transition",
        observations,
        minimum_sample_size=2,
        confidence_level=0.95,
        source_kind="feature",
        assessment_method=None,
        deterministic_feature_set_provenance=True,
        registry=SPATIAL_REGISTRY,
    )
    unknown_source = transition_preference(
        "production.completed_composition",
        observations,
        minimum_sample_size=2,
        confidence_level=0.95,
        source_kind="llm",
        assessment_method=None,
        deterministic_feature_set_provenance=True,
        registry=SPATIAL_REGISTRY,
    )
    unknown_spatial = map_position_habit(
        "invented.position",
        observations,
        minimum_sample_size=2,
        confidence_level=0.95,
        map_public_id="00000000-0000-4000-8000-000000000777",
        registry=SPATIAL_REGISTRY,
    )
    assert manual.reason == "unsupported_assessment_method"
    assert unknown_feature.reason == "unsupported_metric_definition"
    assert unknown_source.reason == "unsupported_metric_definition"
    assert unknown_spatial.reason == "unsupported_metric_definition"


def test_comparison_requires_two_usable_per_cohort_and_reports_each_missing_count() -> None:
    result = personal_baseline_difference(
        (_observation("f1", 10.0), _observation("fm", None, quality="unavailable", reason="missing")),
        (_observation("b1", 2.0), _observation("bm", None, quality="unavailable", reason="missing")),
        input_digest="8" * 64,
        minimum_sample_size=1,
        bootstrap_resamples=20,
        confidence_level=0.9,
    )
    assert result.reason == "insufficient_resample_observations"
    assert result.statistics["focal_usable_count"] == 1
    assert result.statistics["focal_missing_count"] == 1
    assert result.statistics["reference_usable_count"] == 1
    assert result.statistics["reference_missing_count"] == 1


def test_trend_and_comparison_use_central_scipy_bootstrap_metadata() -> None:
    observations = tuple(_observation(str(index), float(index)) for index in range(4))
    trend_result = trend(
        observations,
        (100, 200, 300, 400),
        input_digest="9" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=30,
        confidence_level=0.9,
    )
    comparison = personal_baseline_difference(
        observations[:2],
        observations[2:],
        input_digest="7" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=30,
        confidence_level=0.9,
    )
    assert trend_result.statistics["interval_method"] == "scipy-bootstrap-percentile-v1"
    assert trend_result.statistics["statistic"] == "theil_sen_pairwise_slope_median"
    assert comparison.statistics["interval_method"] == "scipy-bootstrap-percentile-v1"
    assert comparison.statistics["statistic"] == "median_difference"


def test_change_point_preserves_chronology_instead_of_resorting_member_keys() -> None:
    observations = tuple(
        _observation(key, value)
        for key, value in zip(("z", "y", "x", "c", "b", "a"), (1.0, 1.0, 1.0, 10.0, 10.0, 10.0), strict=True)
    )
    result = change_point_candidate(
        observations,
        (100, 200, 300, 400, 500, 600),
        input_digest="6" * 64,
        minimum_sample_size=3,
        bootstrap_resamples=30,
        confidence_level=0.9,
    )
    assert result.statistics["split_before_member_key"] == "c"
    assert result.statistics["pre_evidence_ids"] == ["evidence-z", "evidence-y", "evidence-x"]


def test_trend_bootstrap_never_fabricates_zero_for_single_rare_timestamp() -> None:
    observations = tuple(
        _observation(str(index), 1.0 if index < 99 else 101.0) for index in range(100)
    )
    result = trend(
        observations,
        (100,) * 99 + (200,),
        input_digest="5" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=100,
        confidence_level=0.9,
    )
    assert result.quality == "complete"
    assert result.statistics["slope_interval"] == [1.0, 1.0]
    assert result.statistics["statistic"] == "theil_sen_pairwise_slope_median"


def test_pure_patterns_reject_duplicate_logical_observations() -> None:
    duplicate = _observation("same", ["ChinaPowerPlant", "ChinaBarracks", "ChinaSupplyCenter"])
    with pytest.raises(ValueError, match="duplicate logical observation"):
        recurring_opening(
            (duplicate, duplicate),
            prefix_length=3,
            minimum_sample_size=2,
            confidence_level=0.9,
        )
