from __future__ import annotations

import random

import numpy as np

from generals_replay_analyzer.longitudinal.statistics import (
    LongitudinalObservation,
    canonical_statistical_result,
    summarize_categorical,
    summarize_continuous,
)


def _observation(key: str, value: object, *, quality: str = "complete", reason: str | None = None) -> LongitudinalObservation:
    return LongitudinalObservation(
        member_key=key,
        evidence_public_id=f"evidence-{key}",
        raw_value=value,
        quality=quality,
        reason=reason,
    )


def test_continuous_summary_preserves_zero_missingness_distribution_and_seed_metadata() -> None:
    observations = (
        _observation("d", None, quality="unavailable", reason="missing_source"),
        _observation("b", 10.0),
        _observation("a", 0.0),
        _observation("c", 20.0, quality="partial", reason="telemetry_truncated"),
    )
    before = np.random.get_state()
    result = summarize_continuous(
        "economy.cash_change_total",
        observations,
        input_digest="a" * 64,
        minimum_sample_size=3,
        bootstrap_resamples=100,
        confidence_level=0.95,
        algorithm_version="median-bootstrap-v1",
    )
    after = np.random.get_state()

    assert result.sample_count == 3
    assert result.missing_count == 1
    assert result.quality == "partial"
    assert result.statistics["median"] == 10.0
    assert result.statistics["iqr"] == 10.0
    assert result.statistics["percentile_10"] == 2.0
    assert result.statistics["percentile_90"] == 18.0
    assert result.statistics["bit_generator"] == "PCG64"
    assert result.statistics["seed_hex"] == "2baf46211de9b26af33bfcaa829cf080c03fe2d12cecc07d52b97c1a1c6e3706"
    assert result.statistics["resample_count"] == 100
    assert result.statistics["ordered_member_evidence_ids"] == ["evidence-a", "evidence-b", "evidence-c"]
    assert all(np.array_equal(left, right) for left, right in zip(before[1:], after[1:], strict=True))


def test_continuous_summary_suppresses_below_configured_minimum_and_too_small_interval() -> None:
    one = summarize_continuous(
        "metric",
        (_observation("a", 0.0),),
        input_digest="b" * 64,
        minimum_sample_size=1,
        bootstrap_resamples=10,
        confidence_level=0.9,
        algorithm_version="median-bootstrap-v1",
    )
    assert one.sample_count == 1
    assert one.quality == "unavailable"
    assert one.reason == "insufficient_resample_observations"
    assert "median_confidence_interval" not in one.statistics

    suppressed = summarize_continuous(
        "metric",
        (_observation("a", 0.0), _observation("b", None, quality="unavailable", reason="missing")),
        input_digest="b" * 64,
        minimum_sample_size=2,
        bootstrap_resamples=10,
        confidence_level=0.9,
        algorithm_version="median-bootstrap-v1",
    )
    assert suppressed.reason == "minimum_sample_not_met"
    assert suppressed.statistics == {}


def test_categorical_summary_uses_wilson_interval_and_preserves_missingness() -> None:
    result = summarize_categorical(
        "opening",
        (_observation("a", "rax"), _observation("b", "rax"), _observation("c", "wf"), _observation("d", None, quality="unavailable", reason="missing")),
        minimum_sample_size=3,
        confidence_level=0.95,
        algorithm_version="wilson-v1",
    )
    assert result.sample_count == 3
    assert result.missing_count == 1
    assert result.statistics["category_counts"] == {"rax": 2, "wf": 1}
    assert result.statistics["category_shares"] == {"rax": 2 / 3, "wf": 1 / 3}
    assert set(result.statistics["wilson_intervals"]) == {"rax", "wf"}


def test_one_hundred_shuffled_generated_corpora_are_byte_identical() -> None:
    source = [
        _observation("c", 20.0, quality="partial", reason="telemetry_truncated"),
        _observation("a", 0.0),
        _observation("d", None, quality="unavailable", reason="missing_source"),
        _observation("b", 10.0),
    ]
    expected: bytes | None = None
    for seed in range(100):
        shuffled = source[:]
        random.Random(seed).shuffle(shuffled)
        result = summarize_continuous(
            "economy.cash_change_total",
            tuple(shuffled),
            input_digest="a" * 64,
            minimum_sample_size=3,
            bootstrap_resamples=100,
            confidence_level=0.95,
            algorithm_version="median-bootstrap-v1",
        )
        current = canonical_statistical_result(result)
        expected = current if expected is None else expected
        assert current == expected
