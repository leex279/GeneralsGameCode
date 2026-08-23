"""Canonical fixed comparison query and response behavior."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    ComparisonDTO,
    ComparisonMetricDTO,
    ComparisonValueDTO,
    ComparisonVersionDTO,
    DefinitionBindingDTO,
    FixedComparisonQueryDTO,
    FixedReportReferenceDTO,
    LongitudinalBindingDTO,
    MatchSubjectDTO,
    OpeningSubjectDTO,
    PlayerCohortSubjectDTO,
    SegmentBaselineSubjectDTO,
    StrategySubjectDTO,
    TimePeriodSubjectDTO,
)
from generals_replay_analyzer.web.routes.comparisons import _canonical_json, _fixed_query, _fixed_url

LEFT_ID = "123e4567-e89b-42d3-a456-426614174310"
RIGHT_ID = "123e4567-e89b-42d3-a456-426614174311"
LEFT_RUN = "123e4567-e89b-42d3-a456-426614174312"
RIGHT_RUN = "123e4567-e89b-42d3-a456-426614174313"
COMPARISON_ID = "123e4567-e89b-42d3-a456-426614174314"
DIGEST = "b" * 64
REPORT_LEFT = "123e4567-e89b-42d3-a456-426614174315"
REPORT_RIGHT = "123e4567-e89b-42d3-a456-426614174316"
REPLAY_LEFT = "123e4567-e89b-42d3-a456-426614174317"
REPLAY_RIGHT = "123e4567-e89b-42d3-a456-426614174318"
REPLAY_PLAYER_LEFT = "123e4567-e89b-42d3-a456-426614174319"
REPLAY_PLAYER_RIGHT = "123e4567-e89b-42d3-a456-42661417431a"
RESULT_LEFT = "123e4567-e89b-42d3-a456-42661417431b"
RESULT_RIGHT = "123e4567-e89b-42d3-a456-42661417431c"
BASELINE_ID = "123e4567-e89b-42d3-a456-42661417431d"


def _binding(player_id: str, run_id: str) -> LongitudinalBindingDTO:
    return LongitudinalBindingDTO(
        run_id=run_id,
        player_public_id=player_id,
        identity_revision=4,
        analyzer_name="longitudinal",
        analyzer_version="1.0",
        segment_schema_version="longitudinal-segment-v1",
        segment_digest=DIGEST,
        quality_policy_digest=DIGEST,
        input_digest=DIGEST,
        cache_key=DIGEST,
        statistics_algorithm_versions=("bootstrap-v1",),
    )


def _definition() -> DefinitionBindingDTO:
    return DefinitionBindingDTO(
        definition_kind="feature",
        definition_id="economy.collection_rate",
        definition_version="1.0",
        unit="credits_per_minute",
        scope_type="player",
        window_policy_version="whole-match-v1",
        faction_comparability="declared_cross_faction",
    )


def _query() -> FixedComparisonQueryDTO:
    return FixedComparisonQueryDTO(
        schema_version="replay-comparison-query-v1",
        kind="players",
        left=PlayerCohortSubjectDTO(
            subject_kind="player_cohort",
            player_public_id=LEFT_ID,
            identity_revision=4,
            longitudinal=_binding(LEFT_ID, LEFT_RUN),
        ),
        right=PlayerCohortSubjectDTO(
            subject_kind="player_cohort",
            player_public_id=RIGHT_ID,
            identity_revision=4,
            longitudinal=_binding(RIGHT_ID, RIGHT_RUN),
        ),
        metric_definition_ids=("economy.collection_rate",),
        definition_bindings=(_definition(),),
        minimum_sample_size=5,
    )


def _report(replay_id: str, replay_player_id: str, report_id: str) -> FixedReportReferenceDTO:
    return FixedReportReferenceDTO(
        replay_public_id=replay_id,
        replay_player_public_id=replay_player_id,
        report_public_id=report_id,
        document_schema_version="replay-report-document-v1",
        report_version="replay-report-v1",
        display_policy_version="replay-report-display-v1",
        input_digest=DIGEST,
    )


def _query_kind(kind: str) -> FixedComparisonQueryDTO:
    left_binding = _binding(LEFT_ID, LEFT_RUN)
    right_binding = _binding(RIGHT_ID, RIGHT_RUN)
    if kind == "players":
        left = PlayerCohortSubjectDTO(
            subject_kind="player_cohort",
            player_public_id=LEFT_ID,
            identity_revision=4,
            longitudinal=left_binding,
        )
        right = SegmentBaselineSubjectDTO(
            subject_kind="segment_baseline",
            baseline_public_id=BASELINE_ID,
            longitudinal=right_binding,
            population_definition_version="population-v1",
        )
    elif kind == "matches":
        left = MatchSubjectDTO(
            subject_kind="match",
            report=_report(REPLAY_LEFT, REPLAY_PLAYER_LEFT, REPORT_LEFT),
            feature_set_public_ids=(LEFT_RUN,),
        )
        right = MatchSubjectDTO(
            subject_kind="match",
            report=_report(REPLAY_RIGHT, REPLAY_PLAYER_RIGHT, REPORT_RIGHT),
            feature_set_public_ids=(RIGHT_RUN,),
        )
    elif kind == "openings":
        left = OpeningSubjectDTO(
            subject_kind="opening",
            player_public_id=LEFT_ID,
            identity_revision=4,
            longitudinal=left_binding,
            result_public_id=RESULT_LEFT,
            opening_definition_id="opening.fast_expand",
            opening_definition_version="1.0",
        )
        right = OpeningSubjectDTO(
            subject_kind="opening",
            player_public_id=RIGHT_ID,
            identity_revision=4,
            longitudinal=right_binding,
            result_public_id=RESULT_RIGHT,
            opening_definition_id="opening.fast_expand",
            opening_definition_version="1.0",
        )
    elif kind == "strategies":
        left = StrategySubjectDTO(
            subject_kind="strategy",
            player_public_id=LEFT_ID,
            identity_revision=4,
            longitudinal=left_binding,
            result_public_id=RESULT_LEFT,
            strategy_id="strategy.fast_expand",
            taxonomy_version="taxonomy-v1",
            rule_version="rules-v1",
            method="rule",
        )
        right = StrategySubjectDTO(
            subject_kind="strategy",
            player_public_id=RIGHT_ID,
            identity_revision=4,
            longitudinal=right_binding,
            result_public_id=RESULT_RIGHT,
            strategy_id="strategy.fast_expand",
            taxonomy_version="taxonomy-v1",
            rule_version="rules-v1",
            method="rule",
        )
    else:
        right_binding = _binding(LEFT_ID, RIGHT_RUN)
        left = TimePeriodSubjectDTO(
            subject_kind="time_period",
            player_public_id=LEFT_ID,
            identity_revision=4,
            start_inclusive_utc="2026-01-01T00:00:00Z",
            end_exclusive_utc="2026-02-01T00:00:00Z",
            longitudinal=left_binding,
        )
        right = TimePeriodSubjectDTO(
            subject_kind="time_period",
            player_public_id=LEFT_ID,
            identity_revision=4,
            start_inclusive_utc="2026-02-01T00:00:00Z",
            end_exclusive_utc="2026-03-01T00:00:00Z",
            longitudinal=right_binding,
        )
    return FixedComparisonQueryDTO(
        schema_version="replay-comparison-query-v1",
        kind=kind,
        left=left,
        right=right,
        metric_definition_ids=("economy.collection_rate",),
        definition_bindings=(_definition(),),
        minimum_sample_size=1 if kind == "matches" else 5,
    )


def _comparison() -> ComparisonDTO:
    query = _query()
    available = AvailabilityDTO(state="available")
    return ComparisonDTO(
        version=ComparisonVersionDTO(
            schema_version="replay-comparison-v1",
            comparison_definition_version="replay-comparison-definition-v1",
            display_policy_version="replay-comparison-display-v1",
            comparison_public_id=COMPARISON_ID,
            query_digest=DIGEST,
            input_digest=DIGEST,
            identity_bindings=((LEFT_ID, 4), (RIGHT_ID, 4)),
            longitudinal_bindings=(_binding(LEFT_ID, LEFT_RUN), _binding(RIGHT_ID, RIGHT_RUN)),
            report_bindings=(),
            definition_bindings=(_definition(),),
        ),
        query=query,
        state="comparable",
        reason_codes=(),
        metrics=(
            ComparisonMetricDTO(
                section="overview",
                metric_id="economy.collection_rate",
                label="Collection Rate",
                value_kind="scalar",
                definition=_definition(),
                left=ComparisonValueDTO(
                    raw_value=1200.0,
                    unit="credits_per_minute",
                    sample_count=8,
                    missing_count=0,
                    availability=available,
                ),
                right=ComparisonValueDTO(
                    raw_value=1100.0,
                    unit="credits_per_minute",
                    sample_count=9,
                    missing_count=0,
                    availability=available,
                ),
                derived_difference=100.0,
                state="comparable",
            ),
        ),
        availability=available,
    )


def test_fixed_comparison_url_round_trips_without_swapping_subjects() -> None:
    """Catch canonical URL mapping losing a binding or sorting the left/right subjects."""
    query = _query()
    url = _fixed_url(query)
    split = urlsplit(url)
    request = Request({"type": "http", "method": "GET", "path": split.path, "query_string": split.query.encode()})

    assert _fixed_query(request) == query
    assert url.index(LEFT_ID) < url.index(RIGHT_ID)


@pytest.mark.parametrize("kind", ["players", "matches", "openings", "strategies", "time_periods"])
def test_each_comparison_kind_round_trips_its_individually_named_fixed_subjects(kind: str) -> None:
    """Catch any mode falling back to a generic JSON token or losing a version binding."""
    query = _query_kind(kind)
    split = urlsplit(_fixed_url(query))
    request = Request({"type": "http", "method": "GET", "path": split.path, "query_string": split.query.encode()})

    assert _fixed_query(request) == query
    assert "fixed_query=" not in split.query and "base64" not in split.query


def test_comparison_json_bytes_are_compact_stable_and_semantic() -> None:
    """Catch locale, whitespace, or presentation clock changing immutable comparison bytes."""
    comparison = _comparison()
    first = _canonical_json(comparison)
    second = _canonical_json(comparison.model_copy(deep=True))
    changed = _canonical_json(
        ComparisonDTO.model_validate(
            {
                **comparison.model_dump(),
                "version": {**comparison.version.model_dump(), "input_digest": "c" * 64},
            }
        )
    )

    assert first == second
    assert first != changed
    assert b"generated_at" not in first
    assert b": " not in first


def test_not_comparable_metric_rejects_a_fake_delta_or_difference_evidence() -> None:
    """Catch mismatched definitions being visualized as an aligned numerical result."""
    available = AvailabilityDTO(state="available")
    with pytest.raises(ValidationError, match="unaligned comparison metric"):
        ComparisonMetricDTO(
            section="overview",
            metric_id="economy.collection_rate",
            label="Collection Rate",
            value_kind="scalar",
            definition=_definition(),
            left=ComparisonValueDTO(
                raw_value=1200.0,
                unit="credits_per_minute",
                sample_count=8,
                missing_count=0,
                availability=available,
            ),
            right=ComparisonValueDTO(
                raw_value=1100.0,
                unit="credits_per_minute",
                sample_count=9,
                missing_count=0,
                availability=available,
            ),
            derived_difference=100.0,
            state="not_comparable",
            reason_codes=("definition_version_mismatch",),
        )


@pytest.mark.parametrize(
    "reason_code",
    (
        "definition_id_mismatch",
        "definition_version_mismatch",
        "unit_mismatch",
        "scope_mismatch",
        "window_policy_mismatch",
        "algorithm_version_mismatch",
        "report_version_mismatch",
        "taxonomy_version_mismatch",
        "rule_version_mismatch",
        "quality_policy_mismatch",
        "faction_not_comparable",
        "evidence_membership_unknown",
        "minimum_sample_not_met",
    ),
)
def test_mismatch_matrix_never_licenses_an_unaligned_delta(reason_code: str) -> None:
    """Catch any stable mismatch reason being treated as permission to calculate in web code."""
    unavailable = AvailabilityDTO(state="unavailable", reason_codes=(reason_code,))
    metric = ComparisonMetricDTO(
        section="overview",
        metric_id="economy.collection_rate",
        label="Collection Rate",
        value_kind="scalar",
        definition=_definition(),
        left=ComparisonValueDTO(
            raw_value=None,
            unit="credits_per_minute",
            sample_count=0,
            missing_count=8,
            availability=unavailable,
        ),
        right=ComparisonValueDTO(
            raw_value=None,
            unit="credits_per_minute",
            sample_count=0,
            missing_count=9,
            availability=unavailable,
        ),
        derived_difference=None,
        difference_evidence=(),
        state="not_comparable",
        reason_codes=(reason_code,),
    )

    assert metric.derived_difference is None
    assert metric.difference_evidence == ()
    assert metric.reason_codes == (reason_code,)
