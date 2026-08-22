"""Immutable evidence and typed feature-value contracts."""

from dataclasses import FrozenInstanceError

import pytest

from generals_replay_analyzer.features.base import FeatureScope, FeatureValue, FeatureWindow, validate_feature_value
from generals_replay_analyzer.features.evidence import (
    DerivedEvidence,
    EvidenceRef,
    InferredEvidence,
    ObservedEvidence,
    fact,
    freeze_canonical,
    thaw_canonical,
)
from generals_replay_analyzer.features.registry import BASE_REGISTRY


def _observed_ref(public_id: str = "00000000-0000-4000-8000-000000000211") -> EvidenceRef:
    return EvidenceRef(public_id, "observed", "telemetry", f"telemetry:{public_id}:1", "telemetry-v2")


def _build_value(**overrides: object) -> FeatureValue:
    values: dict[str, object] = {
        "name": "build.completed_count",
        "value_type": "integer",
        "raw_value": 0,
        "unit": "count",
        "scope": FeatureScope("player", "00000000-0000-4000-8000-000000000212", "00000000-0000-4000-8000-000000000212"),
        "window": FeatureWindow(0, 90),
        "quality": "complete",
        "quality_reason": None,
        "input_evidence": (_observed_ref(),),
        "details": {"precision": 1.2345678901234567},
    }
    values.update(overrides)
    return FeatureValue(**values)  # type: ignore[arg-type]


def test_evidence_dtos_freeze_mutable_state_and_preserve_public_semantics() -> None:
    facts = {"nested": [2, 1], "label": "raw"}
    observed = ObservedEvidence(_observed_ref(), 8, "fixture", facts)
    derived = DerivedEvidence(
        EvidenceRef("00000000-0000-4000-8000-000000000213", "derived", "feature", "feature:fixture", "feature-v1"),
        "fixture",
        "v1",
        (_observed_ref().public_id,),
    )
    inferred = InferredEvidence(
        EvidenceRef("00000000-0000-4000-8000-000000000214", "inferred", "llm", "llm:fixture", "inference-v1"),
        "provider",
        "model",
        (derived.ref.public_id,),
    )
    facts["nested"].append(3)
    assert observed.facts == (("label", "raw"), ("nested", (2, 1)))
    assert derived.input_evidence_ids == (_observed_ref().public_id,)
    assert inferred.cited_evidence_ids == (derived.ref.public_id,)
    with pytest.raises(FrozenInstanceError):
        observed.frame = 9  # type: ignore[misc]


def test_feature_validation_distinguishes_measured_zero_unavailable_and_partial() -> None:
    measured_zero = validate_feature_value(_build_value(), BASE_REGISTRY)
    assert measured_zero.raw_value == 0
    assert measured_zero.quality == "complete"

    unavailable = validate_feature_value(
        _build_value(raw_value=None, quality="unavailable", quality_reason="no_observed_events", input_evidence=()),
        BASE_REGISTRY,
    )
    assert unavailable.raw_value is None
    assert unavailable.quality_reason == "no_observed_events"

    partial = validate_feature_value(
        _build_value(raw_value=3, quality="partial", quality_reason="omitted_unknown_evidence"), BASE_REGISTRY
    )
    assert partial.raw_value == 3
    with pytest.raises(ValueError, match="unavailable"):
        validate_feature_value(_build_value(raw_value=0, quality="unavailable", quality_reason="missing"), BASE_REGISTRY)
    with pytest.raises(ValueError, match="partial"):
        validate_feature_value(_build_value(quality="partial", quality_reason=None), BASE_REGISTRY)


def test_available_values_require_sorted_unique_direct_observed_inputs_and_exact_definition() -> None:
    first = _observed_ref("00000000-0000-4000-8000-000000000221")
    second = EvidenceRef(
        "00000000-0000-4000-8000-000000000222", "observed", "parser", "parser:fixture:2", "parser-v1"
    )
    validated = validate_feature_value(_build_value(input_evidence=(first, second)), BASE_REGISTRY)
    assert validated.input_evidence == (second, first)
    inferred = EvidenceRef(
        "00000000-0000-4000-8000-000000000223", "inferred", "llm", "llm:fixture", "inference-v1"
    )
    for overrides, message in (
        ({"input_evidence": (first, first)}, "duplicate"),
        ({"input_evidence": (inferred,)}, "observed"),
        ({"unit": "seconds"}, "unit"),
        ({"value_type": "real", "raw_value": 1.0}, "value_type"),
        ({"scope": FeatureScope("replay", "replay")}, "scope"),
        ({"window": FeatureWindow(5, 4)}, "window"),
    ):
        with pytest.raises(ValueError, match=message):
            validate_feature_value(_build_value(**overrides), BASE_REGISTRY)


def test_raw_float_precision_is_not_rounded() -> None:
    value = validate_feature_value(
        FeatureValue(
            name="combat.observed_damage_trade_ratio",
            value_type="real",
            raw_value=1.2345678901234567,
            unit="ratio",
            scope=FeatureScope("player", "00000000-0000-4000-8000-000000000224", "00000000-0000-4000-8000-000000000224"),
            window=FeatureWindow(0, 30),
            quality="complete",
            quality_reason=None,
            input_evidence=(_observed_ref(),),
        ),
        BASE_REGISTRY,
    )
    assert value.raw_value == 1.2345678901234567


def test_canonical_evidence_rejects_runtime_identity_and_invalid_tier_shapes() -> None:
    class FakeOrm:
        _sa_instance_state = object()

    assert thaw_canonical(freeze_canonical({"set": {3, 1, 2}})) == {"set": [1, 2, 3]}
    for invalid in (b"bytes", {1: "not-a-string-key"}, FakeOrm(), object(), float("nan"), -0.0):
        with pytest.raises((TypeError, ValueError)):
            freeze_canonical(invalid)
    with pytest.raises(ValueError, match="public_id"):
        EvidenceRef("", "observed", "telemetry", "key", "v1")
    with pytest.raises(ValueError, match="tier"):
        EvidenceRef("public", "bad", "telemetry", "key", "v1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="observed reference"):
        ObservedEvidence(EvidenceRef("public", "derived", "feature", "key", "v1"), 0, "event", {})
    with pytest.raises(ValueError, match="frame"):
        ObservedEvidence(_observed_ref(), -1, "event", {})
    with pytest.raises(ValueError, match="derived reference"):
        DerivedEvidence(_observed_ref(), "extractor", "v1", ())
    with pytest.raises(ValueError, match="inferred reference"):
        InferredEvidence(_observed_ref(), "provider", "model", ())
    assert fact(ObservedEvidence(_observed_ref(), None, "event", ()), "missing") is None
