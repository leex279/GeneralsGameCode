"""Registry-gated recurring patterns and conservative chronological claims."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import numpy as np
from scipy import stats  # type: ignore[import-untyped]

from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureRegistry
from generals_replay_analyzer.longitudinal.statistics import (
    LongitudinalObservation,
    scipy_bootstrap_interval,
    summarize_categorical,
    summarize_continuous,
)


@dataclass(frozen=True)
class PatternResult:
    sample_count: int
    missing_count: int
    quality: str
    reason: str | None
    statistics: Mapping[str, object]


def _unavailable(reason: str, sample_count: int = 0, missing_count: int = 0) -> PatternResult:
    return PatternResult(sample_count, missing_count, "unavailable", reason, {})


def _ordered(observations: tuple[LongitudinalObservation, ...]) -> tuple[LongitudinalObservation, ...]:
    ordered = tuple(sorted(observations, key=lambda item: (item.member_key, item.evidence_public_id)))
    identities = tuple((item.member_key, item.evidence_public_id) for item in ordered)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate logical observation")
    return ordered


def _usable_numeric(observations: tuple[LongitudinalObservation, ...]) -> tuple[LongitudinalObservation, ...]:
    return tuple(
        item
        for item in _ordered(observations)
        if item.quality != "unavailable" and type(item.raw_value) in (int, float)
    )


def _quality(observations: tuple[LongitudinalObservation, ...]) -> tuple[str, str | None]:
    if all(item.quality == "complete" for item in observations):
        return "complete", None
    return "partial", "partial_input_quality"


# TheSuperHackers @feature Leex 22/08/2026 Report only registry-backed recurring prefixes with retained evidence. (#0)
def recurring_opening(
    observations: tuple[LongitudinalObservation, ...],
    *,
    prefix_length: int,
    minimum_sample_size: int,
    confidence_level: float,
) -> PatternResult:
    if type(prefix_length) is not int or prefix_length <= 0:
        raise ValueError("prefix_length must be positive")
    ordered = _ordered(observations)
    prefixes: list[tuple[str, ...]] = []
    selected: list[LongitudinalObservation] = []
    for item in ordered:
        value = item.raw_value
        if item.quality == "unavailable" or not isinstance(value, (list, tuple)) or len(value) < prefix_length:
            continue
        prefix = tuple(value[:prefix_length])
        if any(type(name) is not str or not name for name in prefix):
            continue
        prefixes.append(prefix)
        selected.append(item)
    missing = len(ordered) - len(prefixes)
    if len(prefixes) < minimum_sample_size:
        return _unavailable("minimum_sample_not_met", len(prefixes), missing)
    counts: dict[tuple[str, ...], int] = {}
    for prefix in prefixes:
        counts[prefix] = counts.get(prefix, 0) + 1
    recurring_prefix, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
    share = count / len(prefixes)
    z_score = float(stats.norm.ppf(0.5 + confidence_level / 2.0))
    denominator = 1.0 + z_score * z_score / len(prefixes)
    center = (share + z_score * z_score / (2.0 * len(prefixes))) / denominator
    half = z_score * np.sqrt(
        share * (1.0 - share) / len(prefixes) + z_score * z_score / (4.0 * len(prefixes) ** 2)
    ) / denominator
    quality, reason = _quality(tuple(selected))
    return PatternResult(
        len(prefixes),
        missing,
        quality,
        reason,
        {
            "pattern_kind": "recurring_opening",
            "source_feature": "build.completed_sequence",
            "recurring_prefix": list(recurring_prefix),
            "share": share,
            "wilson_interval": [max(0.0, float(center - half)), min(1.0, float(center + half))],
            "algorithm_version": "recurring-opening-prefix-wilson-v1",
            "confidence_level": confidence_level,
            "interval_method": "wilson-score-v1",
            "ordered_member_evidence_ids": [item.evidence_public_id for item in selected],
        },
    )


def timing_band(
    feature_name: str,
    observations: tuple[LongitudinalObservation, ...],
    *,
    input_digest: str,
    minimum_sample_size: int,
    bootstrap_resamples: int,
    confidence_level: float,
    source_unit: str,
    source_scope: str,
    member_windows: tuple[tuple[int, int], ...],
) -> PatternResult:
    try:
        definition = BASE_REGISTRY.definition(feature_name)
    except KeyError:
        return _unavailable("unsupported_metric_definition")
    if len(member_windows) != len(observations):
        raise ValueError("each timing observation requires one exact member window")
    exact_windows = tuple(sorted(set(member_windows)))
    if len(exact_windows) > 1:
        return PatternResult(
            0,
            len(observations),
            "unavailable",
            "inconsistent_member_windows",
            {"member_windows": [list(window) for window in exact_windows]},
        )
    frame_start, frame_end = exact_windows[0] if exact_windows else (0, 0)
    if (
        definition.unit != "frames"
        or source_unit != definition.unit
        or source_scope not in definition.scope_types
        or definition.window_policy != "inclusive"
        or frame_start < 0
        or frame_end < frame_start
    ):
        return _unavailable("unsupported_metric_definition")
    result = summarize_continuous(
        feature_name,
        observations,
        input_digest=input_digest,
        minimum_sample_size=minimum_sample_size,
        bootstrap_resamples=bootstrap_resamples,
        confidence_level=confidence_level,
        algorithm_version="timing-band-median-bootstrap-v1",
    )
    details = dict(result.statistics)
    if details:
        details.update({"pattern_kind": "timing_band", "unit": "frames", "frame_window": [frame_start, frame_end]})
    return PatternResult(result.sample_count, result.missing_count, result.quality, result.reason, details)


def _canonical_category_observations(
    observations: tuple[LongitudinalObservation, ...],
) -> tuple[LongitudinalObservation, ...]:
    return tuple(
        LongitudinalObservation(
            item.member_key,
            item.evidence_public_id,
            None if item.raw_value is None else canonical_json(item.raw_value),
            item.quality,
            item.reason,
        )
        for item in observations
    )


# TheSuperHackers @feature Leex 22/08/2026 Gate transition habits on accepted features or rule-only provenance. (#0)
def transition_preference(
    feature_name: str,
    observations: tuple[LongitudinalObservation, ...],
    *,
    minimum_sample_size: int,
    confidence_level: float,
    source_kind: str,
    assessment_method: str | None,
    deterministic_feature_set_provenance: bool,
    registry: FeatureRegistry,
) -> PatternResult:
    if source_kind == "strategy_assessment":
        if assessment_method != "rule":
            return _unavailable("unsupported_assessment_method")
        if not deterministic_feature_set_provenance:
            return _unavailable("ambiguous_feature_set_provenance")
    elif source_kind == "feature":
        try:
            definition = registry.definition(feature_name)
        except KeyError:
            return _unavailable("unsupported_metric_definition")
        if definition.value_type != "json" or "player" not in definition.scope_types:
            return _unavailable("unsupported_metric_definition")
    else:
        return _unavailable("unsupported_metric_definition")
    summary = summarize_categorical(
        feature_name,
        _canonical_category_observations(observations),
        minimum_sample_size=minimum_sample_size,
        confidence_level=confidence_level,
        algorithm_version="transition-preference-wilson-v1",
    )
    details = dict(summary.statistics)
    if details:
        details.update({"pattern_kind": "transition_preference", "source_kind": source_kind})
    return PatternResult(summary.sample_count, summary.missing_count, summary.quality, summary.reason, details)


# TheSuperHackers @feature Leex 22/08/2026 Bind map-position habits to accepted spatial definitions and map identity. (#0)
def map_position_habit(
    feature_name: str,
    observations: tuple[LongitudinalObservation, ...],
    *,
    minimum_sample_size: int,
    confidence_level: float,
    map_public_id: str | None,
    registry: FeatureRegistry,
) -> PatternResult:
    if map_public_id is None:
        return _unavailable("missing_map_identity")
    try:
        parsed_map_public_id = UUID(map_public_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("map_public_id must be a canonical UUID") from error
    if str(parsed_map_public_id) != map_public_id:
        raise ValueError("map_public_id must be a canonical UUID")
    try:
        definition = registry.definition(feature_name)
    except KeyError:
        return _unavailable("unsupported_metric_definition")
    if (
        definition.owner_namespace not in {"expansion", "map_control", "movement_density"}
        or definition.value_type != "json"
        or "player" not in definition.scope_types
    ):
        return _unavailable("unsupported_metric_definition")
    summary = summarize_categorical(
        feature_name,
        _canonical_category_observations(observations),
        minimum_sample_size=minimum_sample_size,
        confidence_level=confidence_level,
        algorithm_version="map-position-habit-wilson-v1",
    )
    details = dict(summary.statistics)
    if details:
        details.update(
            {
                "pattern_kind": "map_position_habit",
                "map_public_id": map_public_id,
                "source_feature": feature_name,
            }
        )
    return PatternResult(summary.sample_count, summary.missing_count, summary.quality, summary.reason, details)


def _bootstrap_difference(
    focal_values: np.ndarray,
    reference_values: np.ndarray,
    *,
    input_digest: str,
    bootstrap_resamples: int,
    confidence_level: float,
) -> tuple[list[float], dict[str, object]]:
    return scipy_bootstrap_interval(
        (focal_values, reference_values),
        lambda focal, reference: float(np.median(focal) - np.median(reference)),
        statistic_name="median_difference",
        algorithm_version="median-difference-bootstrap-v1",
        input_digest=input_digest,
        bootstrap_resamples=bootstrap_resamples,
        confidence_level=confidence_level,
    )


def _comparison(
    focal: tuple[LongitudinalObservation, ...],
    reference: tuple[LongitudinalObservation, ...],
    *,
    comparison_kind: str,
    input_digest: str,
    minimum_sample_size: int,
    bootstrap_resamples: int,
    confidence_level: float,
) -> PatternResult:
    focal_usable = _usable_numeric(focal)
    reference_usable = _usable_numeric(reference)
    focal_missing = len(focal) - len(focal_usable)
    reference_missing = len(reference) - len(reference_usable)
    if {item.member_key for item in focal_usable} & {item.member_key for item in reference_usable}:
        return _unavailable("cohorts_not_disjoint", len(focal_usable), len(reference_usable))
    counts = {
        "focal_usable_count": len(focal_usable),
        "focal_missing_count": focal_missing,
        "reference_usable_count": len(reference_usable),
        "reference_missing_count": reference_missing,
    }
    if len(focal_usable) < minimum_sample_size or len(reference_usable) < minimum_sample_size:
        return PatternResult(len(focal_usable), focal_missing, "unavailable", "minimum_sample_not_met", counts)
    if len(focal_usable) < 2 or len(reference_usable) < 2:
        return PatternResult(len(focal_usable), focal_missing, "unavailable", "insufficient_resample_observations", counts)
    focal_values = np.asarray([float(cast(int | float, item.raw_value)) for item in focal_usable])
    reference_values = np.asarray([float(cast(int | float, item.raw_value)) for item in reference_usable])
    interval, metadata = _bootstrap_difference(
        focal_values,
        reference_values,
        input_digest=input_digest,
        bootstrap_resamples=bootstrap_resamples,
        confidence_level=confidence_level,
    )
    focal_median = float(np.median(focal_values))
    reference_median = float(np.median(reference_values))
    quality, reason = _quality(focal_usable + reference_usable)
    return PatternResult(
        len(focal_usable),
        focal_missing,
        quality,
        reason,
        {
            "comparison_kind": comparison_kind,
            "raw_difference": focal_median - reference_median,
            "focal_summary": {"count": len(focal_usable), "median": focal_median},
            "reference_summary": {"count": len(reference_usable), "median": reference_median},
            "difference_interval": interval,
            "focal_evidence_ids": [item.evidence_public_id for item in focal_usable],
            "reference_evidence_ids": [item.evidence_public_id for item in reference_usable],
            **counts,
            **metadata,
        },
    )


def personal_baseline_difference(
    focal: tuple[LongitudinalObservation, ...],
    baseline: tuple[LongitudinalObservation, ...],
    **kwargs: object,
) -> PatternResult:
    return _comparison(focal, baseline, comparison_kind="personal_baseline", **kwargs)  # type: ignore[arg-type]


def opponent_associated_difference(
    focal: tuple[LongitudinalObservation, ...],
    reference: tuple[LongitudinalObservation, ...],
    *,
    opponent_player_public_id: str | None,
    controls_exact: bool,
    **kwargs: object,
) -> PatternResult:
    if opponent_player_public_id is None:
        return _unavailable("missing_explicit_opponent_identity")
    if controls_exact is not True:
        return _unavailable("nonidentical_comparison_controls")
    return _comparison(focal, reference, comparison_kind="opponent_associated_difference", **kwargs)  # type: ignore[arg-type]


def trend(
    observations: tuple[LongitudinalObservation, ...],
    replay_start_times: tuple[int, ...],
    *,
    input_digest: str,
    minimum_sample_size: int,
    bootstrap_resamples: int,
    confidence_level: float,
) -> PatternResult:
    if len(observations) != len(replay_start_times):
        raise ValueError("each observation requires one persisted replay start time")
    pairs = sorted(zip(replay_start_times, observations, strict=True), key=lambda item: (item[0], item[1].member_key))
    usable = tuple((time, item) for time, item in pairs if type(item.raw_value) in (int, float) and item.quality != "unavailable")
    if len(usable) < minimum_sample_size:
        return _unavailable("minimum_sample_not_met", len(usable), len(observations) - len(usable))
    if len({time for time, _ in usable}) < 2:
        return _unavailable("insufficient_distinct_time_points", len(usable), len(observations) - len(usable))
    times = np.asarray([float(time) for time, _ in usable])
    values = np.asarray([float(cast(int | float, item.raw_value)) for _, item in usable])
    pairwise_slopes = np.asarray(
        [
            (values[right] - values[left]) / (times[right] - times[left])
            for left in range(len(usable))
            for right in range(left + 1, len(usable))
            if times[right] != times[left]
        ],
        dtype=np.float64,
    )
    if len(pairwise_slopes) < 2:
        return _unavailable("insufficient_resample_observations", len(usable), len(observations) - len(usable))
    slope = float(np.median(pairwise_slopes))
    interval, metadata = scipy_bootstrap_interval(
        (pairwise_slopes,),
        lambda sampled_slopes: float(np.median(sampled_slopes)),
        statistic_name="theil_sen_pairwise_slope_median",
        algorithm_version="theil-sen-bootstrap-v1",
        input_digest=input_digest,
        bootstrap_resamples=bootstrap_resamples,
        confidence_level=confidence_level,
    )
    quality, reason = _quality(tuple(item for _, item in usable))
    return PatternResult(
        len(usable),
        len(observations) - len(usable),
        quality,
        reason,
        {
            "trend_kind": "robust_chronological_slope",
            "slope_per_utc_unit": slope,
            "slope_interval": interval,
            **metadata,
            "ordered_member_evidence_ids": [item.evidence_public_id for _, item in usable],
        },
    )


def change_point_candidate(
    observations: tuple[LongitudinalObservation, ...],
    replay_start_times: tuple[int, ...],
    *,
    input_digest: str,
    minimum_sample_size: int,
    bootstrap_resamples: int,
    confidence_level: float,
) -> PatternResult:
    if len(observations) != len(replay_start_times):
        raise ValueError("each observation requires one persisted replay start time")
    ordered = tuple(item for _, item in sorted(zip(replay_start_times, observations, strict=True), key=lambda pair: (pair[0], pair[1].member_key)))
    usable = tuple(item for item in ordered if type(item.raw_value) in (int, float) and item.quality != "unavailable")
    if len(usable) < minimum_sample_size * 2:
        return _unavailable("minimum_sample_not_met", len(usable), len(ordered) - len(usable))
    candidates: list[tuple[float, int, str]] = []
    for split in range(minimum_sample_size, len(usable) - minimum_sample_size + 1):
        before = np.asarray([float(cast(int | float, item.raw_value)) for item in usable[:split]])
        after = np.asarray([float(cast(int | float, item.raw_value)) for item in usable[split:]])
        difference = float(np.median(after) - np.median(before))
        candidates.append((abs(difference), split, usable[split].member_key))
    _, split, first_after_key = min(candidates, key=lambda item: (-item[0], item[2]))
    before_values = np.asarray([float(cast(int | float, item.raw_value)) for item in usable[:split]])
    after_values = np.asarray([float(cast(int | float, item.raw_value)) for item in usable[split:]])
    interval, metadata = _bootstrap_difference(
        after_values,
        before_values,
        input_digest=input_digest,
        bootstrap_resamples=bootstrap_resamples,
        confidence_level=confidence_level,
    )
    quality, reason = _quality(usable)
    return PatternResult(
        len(usable),
        len(ordered) - len(usable),
        quality,
        reason,
        {
            "result_label": "change_point_candidate",
            "split_before_member_key": first_after_key,
            "pre_count": split,
            "post_count": len(usable) - split,
            "raw_difference": float(np.median(after_values) - np.median(before_values)),
            "difference_interval": interval,
            "pre_evidence_ids": [item.evidence_public_id for item in usable[:split]],
            "post_evidence_ids": [item.evidence_public_id for item in usable[split:]],
            **metadata,
        },
    )


def consistency(observations: tuple[LongitudinalObservation, ...], *, minimum_sample_size: int) -> PatternResult:
    usable = _usable_numeric(observations)
    if len(usable) < minimum_sample_size:
        return _unavailable("minimum_sample_not_met", len(usable), len(observations) - len(usable))
    values = np.asarray([float(cast(int | float, item.raw_value)) for item in usable])
    lower, upper = np.percentile(values, (25, 75), method="linear")
    median = float(np.median(values))
    iqr = float(upper - lower)
    dispersion = iqr if median == 0.0 else iqr / abs(median)
    quality, reason = _quality(usable)
    return PatternResult(
        len(usable),
        len(observations) - len(usable),
        quality,
        reason,
        {
            "measure_kind": "descriptive_within_segment_dispersion",
            "algorithm_version": "iqr-over-median-v1",
            "median": median,
            "iqr": iqr,
            "dispersion": dispersion,
            "ordered_member_evidence_ids": [item.evidence_public_id for item in usable],
        },
    )
