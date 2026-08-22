from __future__ import annotations

from dataclasses import replace

import pytest

from generals_replay_analyzer.longitudinal.segments import (
    LongitudinalDefinitionDTO,
    LongitudinalInput,
    LongitudinalRequest,
    LongitudinalResultDTO,
    LongitudinalSettings,
    QualityPolicy,
    SegmentKey,
    canonical_longitudinal_input,
    chronological_sort_key,
    longitudinal_input_digest,
)


def _input(**changes: object) -> LongitudinalInput:
    base = LongitudinalInput(
        player_public_id="11111111-1111-1111-1111-111111111111",
        identity_revision=3,
        identity_cache_token="a" * 64,
        segment=SegmentKey(),
        settings=LongitudinalSettings(
            minimum_sample_size=2,
            bootstrap_resamples=100,
            confidence_level=0.95,
            enabled_metrics=("economy.cash_change_total",),
        ),
        metric_names=("economy.cash_change_total",),
        definitions=(
            LongitudinalDefinitionDTO(
                public_name="economy.cash_change_total",
                result_kind="metric",
                definition_version="economy.cash-change-total-v1",
                source_feature_names=("economy.cash_change_total",),
                algorithm_kind="continuous_summary",
            ),
        ),
        members=(),
    )
    return replace(base, **changes)


def test_segment_defaults_are_exact_and_opt_ins_are_explicit() -> None:
    segment = SegmentKey()

    assert segment.as_canonical() == {
        "schema": "longitudinal-segment-v1",
        "subject_faction": None,
        "subject_subfaction": None,
        "opponent_faction": None,
        "opponent_player_public_id": None,
        "map_public_id": None,
        "start_position": None,
        "replay_version": None,
        "replay_patch": None,
        "start_inclusive": None,
        "end_exclusive": None,
        "quality_policy": {
            "quality_floor": "complete",
            "allowed_lifecycle_states": ["engine_verified"],
            "include_issue_codes": [],
        },
    }
    assert segment.quality_policy.excluded_issue_codes == (
        "crc_mismatch",
        "exporter_failure",
        "missing_telemetry",
        "truncated",
        "version_mismatch",
    )
    opted_in = QualityPolicy(
        quality_floor="partial",
        allowed_lifecycle_states=("desynced", "engine_verified", "partial"),
        include_issue_codes=("crc_mismatch", "truncated"),
    )
    assert opted_in.includes(lifecycle_state="desynced", active_issue_codes=("crc_mismatch",))
    assert not opted_in.includes(lifecycle_state="failed", active_issue_codes=())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_lifecycle_states": ("partial", "engine_verified")},
        {"allowed_lifecycle_states": ("engine_verified", "engine_verified")},
        {"include_issue_codes": ("truncated", "crc_mismatch")},
        {"quality_floor": "available"},
    ],
)
def test_quality_policy_rejects_noncanonical_or_unknown_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        QualityPolicy(**kwargs)  # type: ignore[arg-type]


def test_segment_validates_nullable_exact_filters_and_half_open_time_range() -> None:
    segment = SegmentKey(
        subject_faction="USA",
        subject_subfaction="Laser",
        opponent_faction="GLA",
        opponent_player_public_id="22222222-2222-2222-2222-222222222222",
        map_public_id="33333333-3333-3333-3333-333333333333",
        start_position=0,
        replay_version="1.04",
        replay_patch="retail-1.04",
        start_inclusive=1_700_000_000,
        end_exclusive=1_700_000_100,
    )
    assert segment.start_position == 0
    with pytest.raises(ValueError, match="both be set"):
        SegmentKey(start_inclusive=1)
    with pytest.raises(ValueError, match="later"):
        SegmentKey(start_inclusive=2, end_exclusive=2)
    with pytest.raises(ValueError, match="exact nonempty"):
        SegmentKey(subject_faction=" USA ")


def test_settings_use_the_configured_minimum_and_reject_unknown_or_invalid_controls() -> None:
    settings = LongitudinalSettings.from_minimum_sample_size(
        7,
        bootstrap_resamples=200,
        confidence_level=0.9,
        enabled_metrics=("build.first_completed_frame",),
    )
    assert settings.minimum_sample_size == 7
    with pytest.raises(TypeError):
        LongitudinalSettings.from_mapping({"minimum_sample_size": 7, "surprise": 1})
    with pytest.raises(ValueError):
        replace(settings, bootstrap_resamples=0)


def test_chronological_sort_never_uses_insertion_or_database_identity() -> None:
    first = chronological_sort_key(100, "b" * 64, "22222222-2222-2222-2222-222222222222")
    tied = chronological_sort_key(100, "a" * 64, "33333333-3333-3333-3333-333333333333")
    assert tuple(sorted((first, tied))) == (tied, first)
    with pytest.raises(ValueError, match="missing_replay_start_time"):
        chronological_sort_key(None, "a" * 64, "33333333-3333-3333-3333-333333333333")


def test_cache_identity_changes_for_semantics_not_member_input_order() -> None:
    one = _input()
    assert canonical_longitudinal_input(one) == canonical_longitudinal_input(replace(one, members=tuple(reversed(one.members))))
    assert longitudinal_input_digest(one) != longitudinal_input_digest(
        replace(one, identity_revision=4, identity_cache_token="b" * 64)
    )
    assert longitudinal_input_digest(one) != longitudinal_input_digest(
        replace(one, segment=SegmentKey(subject_faction="USA"))
    )
    assert longitudinal_input_digest(one) != longitudinal_input_digest(
        replace(one, settings=replace(one.settings, bootstrap_resamples=101))
    )


def test_request_names_must_exactly_equal_enabled_definition_sets() -> None:
    settings = LongitudinalSettings(
        minimum_sample_size=2,
        bootstrap_resamples=10,
        confidence_level=0.95,
        enabled_metrics=("economy.cash_change_total", "economy.tracked_income_total"),
    )
    with pytest.raises(ValueError, match="enabled definition sets"):
        LongitudinalRequest(
            player_public_id="11111111-1111-1111-1111-111111111111",
            segment=SegmentKey(),
            metric_names=("economy.cash_change_total",),
            pattern_names=(),
            settings=settings,
        )


def test_public_result_freezes_nested_statistics_and_validates_public_identity() -> None:
    statistics = {"nested": {"value": 1}}
    result = LongitudinalResultDTO(
        public_id="11111111-1111-1111-1111-111111111111",
        result_name="metric",
        result_kind="metric",
        sample_count=1,
        missing_count=0,
        quality="complete",
        reason=None,
        statistics=statistics,
        members=(),
        evidence_public_id="22222222-2222-2222-2222-222222222222",
    )
    statistics["nested"]["value"] = 2
    assert result.statistics["nested"]["value"] == 1  # type: ignore[index]
    with pytest.raises(ValueError, match="canonical UUID"):
        replace(result, public_id="not-public")


def test_input_identity_requires_explicit_requested_definitions_and_exclusions() -> None:
    definition = LongitudinalDefinitionDTO(
        public_name="economy.cash_change_total",
        result_kind="metric",
        definition_version="economy.cash-change-total-v1",
        source_feature_names=("economy.cash_change_total",),
        algorithm_kind="continuous_summary",
    )
    value = LongitudinalInput(
        player_public_id="11111111-1111-1111-1111-111111111111",
        identity_revision=3,
        identity_cache_token="a" * 64,
        segment=SegmentKey(),
        settings=LongitudinalSettings(
            minimum_sample_size=2,
            bootstrap_resamples=10,
            confidence_level=0.95,
            enabled_metrics=("economy.cash_change_total",),
        ),
        metric_names=("economy.cash_change_total",),
        pattern_names=(),
        definitions=(definition,),
        members=(),
        exclusions=(),
    )
    digest = longitudinal_input_digest(value)
    assert definition.definition_version in canonical_longitudinal_input(value)
    assert digest != longitudinal_input_digest(replace(value, definitions=(replace(definition, definition_version="v2"),)))
