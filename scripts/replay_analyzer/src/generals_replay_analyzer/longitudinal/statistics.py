"""Pure deterministic longitudinal statistics over finite raw values."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
import scipy  # type: ignore[import-untyped]
from scipy import stats

from generals_replay_analyzer.features.context import canonical_json

ObservationQuality = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class LongitudinalObservation:
    member_key: str
    evidence_public_id: str
    raw_value: object
    quality: ObservationQuality
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.member_key) is not str or not self.member_key:
            raise ValueError("member_key must be nonempty")
        if type(self.evidence_public_id) is not str or not self.evidence_public_id:
            raise ValueError("evidence_public_id must be nonempty")
        if self.quality not in ("complete", "partial", "unavailable"):
            raise ValueError("unsupported observation quality")
        if type(self.raw_value) is float and (
            not math.isfinite(self.raw_value)
            or (self.raw_value == 0.0 and math.copysign(1.0, self.raw_value) < 0)
        ):
            raise ValueError("raw values must be finite and may not be negative zero")
        if self.quality == "unavailable" and (self.raw_value is not None or not self.reason):
            raise ValueError("unavailable observations must be null with a reason")
        if self.quality == "complete" and self.reason is not None:
            raise ValueError("complete observations may not have a reason")


@dataclass(frozen=True)
class StatisticalResult:
    name: str
    sample_count: int
    missing_count: int
    quality: ObservationQuality
    reason: str | None
    statistics: Mapping[str, object]
    ordered_member_keys: tuple[str, ...]


def canonical_statistical_result(result: StatisticalResult) -> bytes:
    """Encode one result for permutation and persistence equivalence checks."""
    return canonical_json(result).encode("utf-8")


def bootstrap_seed(input_digest: str) -> str:
    if len(input_digest) != 64 or any(character not in "0123456789abcdef" for character in input_digest):
        raise ValueError("input_digest must be a lower-case SHA-256")
    return hashlib.sha256((input_digest + ":longitudinal-bootstrap-v1").encode("utf-8")).hexdigest()


def _ordered(observations: tuple[LongitudinalObservation, ...]) -> tuple[LongitudinalObservation, ...]:
    ordered = tuple(sorted(observations, key=lambda item: (item.member_key, item.evidence_public_id)))
    identities = tuple((item.member_key, item.evidence_public_id) for item in ordered)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate logical observation")
    return ordered


def _result_quality(observations: tuple[LongitudinalObservation, ...]) -> tuple[ObservationQuality, str | None]:
    if all(item.quality == "complete" for item in observations):
        return "complete", None
    return "partial", "partial_input_quality"


# TheSuperHackers @feature Leex 22/08/2026 Bootstrap finite sorted observations with one digest-seeded local PCG64. (#0)
def summarize_continuous(
    name: str,
    observations: tuple[LongitudinalObservation, ...],
    *,
    input_digest: str,
    minimum_sample_size: int,
    bootstrap_resamples: int,
    confidence_level: float,
    algorithm_version: str,
) -> StatisticalResult:
    ordered = _ordered(observations)
    usable = tuple(item for item in ordered if type(item.raw_value) in (int, float) and item.quality != "unavailable")
    missing_count = len(ordered) - len(usable)
    if len(usable) < minimum_sample_size:
        return StatisticalResult(name, len(usable), missing_count, "unavailable", "minimum_sample_not_met", {}, tuple(item.member_key for item in ordered))
    if len(usable) < 2:
        return StatisticalResult(name, len(usable), missing_count, "unavailable", "insufficient_resample_observations", {}, tuple(item.member_key for item in ordered))

    values = np.asarray([float(cast(int | float, item.raw_value)) for item in usable], dtype=np.float64)
    if not bool(np.all(np.isfinite(values))):
        raise ValueError("continuous observations must be finite")
    seed_hex = bootstrap_seed(input_digest)
    generator = np.random.Generator(np.random.PCG64(int(seed_hex[:32], 16)))
    interval = stats.bootstrap(
        (values,),
        np.median,
        confidence_level=confidence_level,
        n_resamples=bootstrap_resamples,
        method="percentile",
        rng=generator,
    ).confidence_interval
    percentile_25, percentile_75 = np.percentile(values, (25, 75), method="linear")
    percentile_10, percentile_90 = np.percentile(values, (10, 90), method="linear")
    quality, reason = _result_quality(ordered)
    details: dict[str, object] = {
        "median": float(np.median(values)),
        "iqr": float(percentile_75 - percentile_25),
        "percentile_10": float(percentile_10),
        "percentile_90": float(percentile_90),
        "median_confidence_interval": [float(interval.low), float(interval.high)],
        "algorithm_version": algorithm_version,
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "bit_generator": "PCG64",
        "seed_hex": seed_hex,
        "resample_count": bootstrap_resamples,
        "confidence_level": confidence_level,
        "statistic": "median",
        "ordered_member_evidence_ids": [item.evidence_public_id for item in usable],
        "interval_method": "scipy-bootstrap-percentile-v1",
        "quantile_method": "numpy-linear-v1",
    }
    return StatisticalResult(name, len(usable), missing_count, quality, reason, details, tuple(item.member_key for item in ordered))


def _wilson(successes: int, total: int, confidence_level: float) -> list[float]:
    z_score = float(stats.norm.ppf(0.5 + confidence_level / 2.0))
    share = successes / total
    denominator = 1.0 + z_score * z_score / total
    center = (share + z_score * z_score / (2.0 * total)) / denominator
    half = z_score * math.sqrt(share * (1.0 - share) / total + z_score * z_score / (4.0 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def summarize_categorical(
    name: str,
    observations: tuple[LongitudinalObservation, ...],
    *,
    minimum_sample_size: int,
    confidence_level: float,
    algorithm_version: str,
) -> StatisticalResult:
    ordered = _ordered(observations)
    usable = tuple(item for item in ordered if item.raw_value is not None and item.quality != "unavailable")
    missing_count = len(ordered) - len(usable)
    if len(usable) < minimum_sample_size:
        return StatisticalResult(name, len(usable), missing_count, "unavailable", "minimum_sample_not_met", {}, tuple(item.member_key for item in ordered))
    categories: dict[str, int] = {}
    for item in usable:
        if type(item.raw_value) is not str or not item.raw_value:
            raise ValueError("categorical observations require nonempty string values")
        categories[item.raw_value] = categories.get(item.raw_value, 0) + 1
    ordered_counts = dict(sorted(categories.items()))
    shares = {category: count / len(usable) for category, count in ordered_counts.items()}
    intervals = {category: _wilson(count, len(usable), confidence_level) for category, count in ordered_counts.items()}
    quality, reason = _result_quality(ordered)
    return StatisticalResult(
        name,
        len(usable),
        missing_count,
        quality,
        reason,
        {
            "category_counts": ordered_counts,
            "category_shares": shares,
            "wilson_intervals": intervals,
            "algorithm_version": algorithm_version,
            "confidence_level": confidence_level,
            "interval_method": "wilson-score-v1",
            "ordered_member_evidence_ids": [item.evidence_public_id for item in usable],
        },
        tuple(item.member_key for item in ordered),
    )
