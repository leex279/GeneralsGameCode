from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from generals_replay_analyzer.features.base import FeatureScope, FeatureValue, FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef, FrozenMapping, thaw_canonical
from generals_replay_analyzer.llm.evidence_bundle import (
    EvidenceBundle,
    EvidenceBundleError,
    EvidenceClaim,
    build_evidence_bundle,
)
from generals_replay_analyzer.longitudinal.segments import LongitudinalEvidenceDTO, LongitudinalResultDTO
from generals_replay_analyzer.strategy.rules import RuleAssessment


def _claim(public_id: str, **changes: object) -> EvidenceClaim:
    values: dict[str, object] = {
        "claim_id": "cash_float",
        "kind": "feature",
        "value": {"amount": 1250, "unit": "credits"},
        "quality": "complete",
        "quality_reason": None,
        "evidence_ids": (public_id,),
    }
    values.update(changes)
    claim_id = values["claim_id"]
    kind = values["kind"]
    raw_value = values["value"]
    quality = values["quality"]
    reason = values["quality_reason"]
    evidence_ids = values["evidence_ids"]
    if type(evidence_ids) is not tuple:
        raise EvidenceBundleError("invalid_claims")
    refs = tuple(
        EvidenceRef(
            item,
            "observed",
            "telemetry_event",
            f"event:{index}:{item}",
            "telemetry-v2",
        )
        for index, item in enumerate(evidence_ids)
    )
    value_type = (
        "boolean"
        if type(raw_value) is bool
        else "integer"
        if type(raw_value) is int
        else "real"
        if type(raw_value) is float
        else "text"
        if type(raw_value) is str
        else "json"
    )
    feature = FeatureValue(
        name=claim_id,  # type: ignore[arg-type]
        value_type=value_type,  # type: ignore[arg-type]
        raw_value=raw_value,  # type: ignore[arg-type]
        unit=None,
        scope=FeatureScope("player", public_id, replay_player_public_id=public_id),
        window=FeatureWindow(0, 1),
        quality=quality,  # type: ignore[arg-type]
        quality_reason=reason,  # type: ignore[arg-type]
        input_evidence=refs,
    )
    if kind == "feature":
        return EvidenceClaim.from_feature(feature, authorized_evidence=refs)
    if kind == "quality":
        return EvidenceClaim.from_feature_quality(feature, authorized_evidence=refs)
    if kind == "rule_candidate":
        assessment_quality = {"complete": "available", "partial": "partial", "unavailable": "unavailable"}.get(
            quality
        )
        if assessment_quality is None:
            raise EvidenceBundleError("invalid_claim_quality")
        details = {"payload": raw_value}
        if reason is not None:
            details["reason"] = reason
        assessment = RuleAssessment(
            strategy_id=claim_id,  # type: ignore[arg-type]
            phase="opening",
            window=FeatureWindow(0, 1),
            quality=assessment_quality,  # type: ignore[arg-type]
            rule_score=0.5 if assessment_quality != "unavailable" else None,
            supporting_evidence=refs,
            contradicting_evidence=(),
            details=details,
        )
        return EvidenceClaim.from_rule_assessment(assessment, authorized_evidence=refs)
    raise EvidenceBundleError("invalid_claim_kind")


def _bundle(public_ids: tuple[str, ...], claims: tuple[EvidenceClaim, ...]):
    return build_evidence_bundle(
        replay_public_id=public_ids[10],
        replay_sha256="a" * 64,
        claims=claims,
    )


def test_bundle_is_deeply_immutable_canonical_and_stably_sorted(public_ids: tuple[str, ...]) -> None:
    mutable = {"nested": [3, 1], "labels": {"z": 2, "a": 1}}
    left = _bundle(
        public_ids,
        (
            _claim(public_ids[2], claim_id="opening", kind="rule_candidate", value=mutable),
            _claim(public_ids[1], claim_id="quality", kind="quality", value={"state": "partial"}),
        ),
    )
    mutable["nested"] = [99]
    right = _bundle(
        public_ids,
        (
            _claim(public_ids[1], claim_id="quality", kind="quality", value={"state": "partial"}),
            _claim(
                public_ids[2],
                claim_id="opening",
                kind="rule_candidate",
                value={"labels": {"a": 1, "z": 2}, "nested": [3, 1]},
            ),
        ),
    )
    expected = {
        "claims": [
            {
                "claim_id": "quality",
                "evidence_ids": [public_ids[1]],
                "kind": "quality",
                "quality": "complete",
                "quality_reason": None,
                "value": {
                    "feature_name": "quality",
                    "quality": "complete",
                    "quality_reason": None,
                    "window": {"frame_end": 1, "frame_start": 0},
                },
            },
            {
                "claim_id": "opening",
                "evidence_ids": [public_ids[2]],
                "kind": "rule_candidate",
                "quality": "complete",
                "quality_reason": None,
                "value": {
                    "details": {
                        "payload": {"labels": {"a": 1, "z": 2}, "nested": [3, 1]}
                    },
                    "phase": "opening",
                    "rule_score": 0.5,
                    "strategy_id": "opening",
                    "window": {"frame_end": 1, "frame_start": 0},
                },
            },
        ],
        "replay_public_id": public_ids[10],
        "replay_sha256": "a" * 64,
        "schema_version": "evidence-bundle-v1",
        "unknown_or_missing": [],
    }
    expected_bytes = json.dumps(
        expected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    assert left == right
    assert left.canonical_json == expected_bytes
    assert left.digest == hashlib.sha256(expected_bytes).hexdigest()
    assert isinstance(left.claims, tuple)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        left.digest = "0" * 64  # type: ignore[misc]


def test_forged_frozen_mapping_is_recursively_reowned(public_ids: tuple[str, ...]) -> None:
    attacker_list: list[object] = [{"score": 1}]
    forged = FrozenMapping((("nested", attacker_list),))
    claim = _claim(public_ids[0], value=forged)
    bundle = _bundle(public_ids, (claim,))
    before = bundle.canonical_json
    attacker_list[0] = {"score": 999}
    projected = thaw_canonical(claim.value)
    assert isinstance(projected, dict)
    assert projected["raw_value"] == {"nested": [{"score": 1}]}
    assert bundle.canonical_json == before
    assert bundle.digest == hashlib.sha256(before).hexdigest()


@pytest.mark.parametrize(
    "value",
    [
        FrozenMapping((("value", -0.0),)),
        FrozenMapping((("value", Path("C:/private/manual-correction.json")),)),
        FrozenMapping((("path", "secret.rep"),)),
        FrozenMapping((("manual_correction", True),)),
        FrozenMapping((("llm_output", "invented"),)),
        FrozenMapping((("timestamp", "2026-08-22"),)),
        FrozenMapping((("sourceKey", "internal"),)),
        FrozenMapping((("invented", True),)),
    ],
)
def test_forged_frozen_mapping_cannot_bypass_value_validation(
    public_ids: tuple[str, ...], value: FrozenMapping
) -> None:
    with pytest.raises(EvidenceBundleError):
        _claim(public_ids[0], value=value)


def test_unavailable_claim_preserves_reason_and_sorted_public_citations(public_ids: tuple[str, ...]) -> None:
    bundle = _bundle(
        public_ids,
        (
            _claim(
                public_ids[3],
                claim_id="missing_income",
                value=None,
                quality="unavailable",
                quality_reason="missing_economy_telemetry",
                evidence_ids=(public_ids[3], public_ids[1]),
            ),
        ),
    )
    assert bundle.claims[0].evidence_ids == tuple(sorted((public_ids[3], public_ids[1])))
    assert bundle.claims[0].quality_reason == "missing_economy_telemetry"
    assert bundle.unknown_or_missing == ("missing_income",)


def test_partial_claim_is_explicitly_listed_as_unknown_or_missing(public_ids: tuple[str, ...]) -> None:
    bundle = _bundle(
        public_ids,
        (_claim(public_ids[0], claim_id="incomplete_income", quality="partial", quality_reason="missing_terminal"),),
    )
    assert bundle.unknown_or_missing == ("incomplete_income",)


@pytest.mark.parametrize(
    "changes",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": -0.0},
        {"value": Path("secret.rep")},
        {"value": {"row_id": 7}},
        {"evidence_ids": ("not-a-uuid",)},
        {"claim_id": " bad"},
        {"kind": "unknown"},
        {"quality": "unknown"},
        {"quality_reason": "unexpected"},
        {"quality": "partial", "quality_reason": None},
        {"evidence_ids": ()},
    ],
)
def test_claim_rejects_nonpublic_or_ambiguous_values(public_ids: tuple[str, ...], changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _claim(public_ids[0], **changes)


def test_bundle_rejects_duplicate_claims_and_citations(public_ids: tuple[str, ...]) -> None:
    claim = _claim(public_ids[0])
    with pytest.raises(EvidenceBundleError, match="duplicate_claim"):
        _bundle(public_ids, (claim, claim))
    with pytest.raises(EvidenceBundleError, match="conflicting_evidence"):
        _claim(public_ids[0], evidence_ids=(public_ids[0], public_ids[0]))
    with pytest.raises(EvidenceBundleError, match="duplicate_claim"):
        _bundle(
            public_ids,
            (
                _claim(public_ids[0], claim_id="same", kind="feature"),
                _claim(public_ids[1], claim_id="same", kind="quality"),
            ),
        )


def test_bundle_dto_rejects_forged_digest_or_canonical_bytes(public_ids: tuple[str, ...]) -> None:
    valid = _bundle(public_ids, (_claim(public_ids[0]),))
    with pytest.raises(EvidenceBundleError, match="invalid_bundle_metadata"):
        EvidenceBundle(
            schema_version=valid.schema_version,
            replay_public_id=valid.replay_public_id,
            replay_sha256=valid.replay_sha256,
            claims=valid.claims,
            unknown_or_missing=valid.unknown_or_missing,
            canonical_json=b"{}",
            digest=valid.digest,
        )


def test_direct_bundle_rejects_object_new_forged_claim(public_ids: tuple[str, ...]) -> None:
    valid = _bundle(public_ids, (_claim(public_ids[0]),))
    forged = object.__new__(EvidenceClaim)
    for field in dataclasses.fields(EvidenceClaim):
        if field.name != "_factory_authority":
            object.__setattr__(forged, field.name, getattr(valid.claims[0], field.name))
    with pytest.raises(EvidenceBundleError, match="invalid_claims"):
        EvidenceBundle(
            valid.schema_version,
            valid.replay_public_id,
            valid.replay_sha256,
            (forged,),
            valid.unknown_or_missing,
            valid.canonical_json,
            valid.digest,
        )
    with pytest.raises(EvidenceBundleError, match="invalid_claims"):
        build_evidence_bundle(
            replay_public_id=valid.replay_public_id,
            replay_sha256=valid.replay_sha256,
            claims=(forged,),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"claims": []},
        {"schema_version": 7},
        {"replay_sha256": 7},
        {"unknown_or_missing": []},
        {"unknown_or_missing": ("forged",)},
        {"canonical_json": "not-bytes"},
    ],
)
def test_direct_bundle_requires_exact_builtin_metadata(
    public_ids: tuple[str, ...], changes: dict[str, object]
) -> None:
    valid = _bundle(public_ids, (_claim(public_ids[0]),))
    with pytest.raises(EvidenceBundleError):
        dataclasses.replace(valid, **changes)  # type: ignore[arg-type]


def test_direct_bundle_construction_rejects_257_claims_before_encoding(public_ids: tuple[str, ...]) -> None:
    claims = tuple(
        _claim(public_ids[index % len(public_ids)], claim_id=f"claim-{index}", value=index)
        for index in range(257)
    )
    document = {
        "schema_version": "evidence-bundle-v1",
        "replay_public_id": public_ids[10],
        "replay_sha256": "a" * 64,
        "claims": [
            {
                "claim_id": claim.claim_id,
                "kind": claim.kind,
                "value": thaw_canonical(claim.value),
                "quality": claim.quality,
                "quality_reason": claim.quality_reason,
                "evidence_ids": list(claim.evidence_ids),
            }
            for claim in claims
        ],
        "unknown_or_missing": [],
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    with pytest.raises(EvidenceBundleError) as caught:
        EvidenceBundle(
            "evidence-bundle-v1",
            public_ids[10],
            "a" * 64,
            claims,
            (),
            canonical,
            hashlib.sha256(canonical).hexdigest(),
        )
    assert caught.value.code == "evidence_bundle_oversize"


def test_deep_value_is_rejected_with_typed_error_before_python_recursion(public_ids: tuple[str, ...]) -> None:
    value: object = "leaf"
    for _ in range(2000):
        value = [value]
    feature, ref = _feature(public_ids[0])
    object.__setattr__(feature, "raw_value", value)
    with pytest.raises(EvidenceBundleError) as caught:
        EvidenceClaim.from_feature(feature, authorized_evidence=(ref,))
    assert caught.value.code in {"evidence_bundle_oversize", "invalid_evidence_value"}


def _observed_ref(
    public_id: str,
    *,
    source_kind: str = "telemetry_event",
    source_key: str | None = None,
) -> EvidenceRef:
    return EvidenceRef(
        public_id=public_id,
        tier="observed",
        source_kind=source_kind,
        source_key=source_key or f"event:{public_id}",
        schema_version="telemetry-v2",
    )


@pytest.mark.parametrize("source_kind", ["parser_command", "telemetry_event"])
def test_feature_factory_authorizes_persisted_observed_source_kinds(
    public_ids: tuple[str, ...], source_kind: str
) -> None:
    ref = _observed_ref(
        public_ids[0],
        source_kind=source_kind,
        source_key=f"observed-evidence:{source_kind}:v1:stable-identity",
    )
    feature, _ = _feature(public_ids[0])
    feature = dataclasses.replace(feature, input_evidence=(ref,))

    claim = EvidenceClaim.from_feature(feature, authorized_evidence=(ref,))

    assert claim.evidence_ids == (public_ids[0],)


@pytest.mark.parametrize("source_kind", ["parser", "telemetry", "unknown_observation"])
def test_feature_factory_rejects_legacy_and_unknown_observed_source_kinds(
    public_ids: tuple[str, ...], source_kind: str
) -> None:
    ref = _observed_ref(public_ids[0], source_kind=source_kind)
    feature, _ = _feature(public_ids[0])
    feature = dataclasses.replace(feature, input_evidence=(ref,))

    with pytest.raises(EvidenceBundleError, match="unauthorized_evidence"):
        EvidenceClaim.from_feature(feature, authorized_evidence=(ref,))


def _feature(public_id: str, raw_value: object = 1250) -> tuple[FeatureValue, EvidenceRef]:
    ref = _observed_ref(public_id)
    return (
        FeatureValue(
            name="cash_float",
            value_type="integer" if type(raw_value) is int else "json",
            raw_value=raw_value,  # type: ignore[arg-type]
            unit="credits" if type(raw_value) is int else None,
            scope=FeatureScope("player", public_id, replay_player_public_id=public_id),
            window=FeatureWindow(0, 900),
            quality="complete",
            quality_reason=None,
            input_evidence=(ref,),
            details={},
        ),
        ref,
    )


def test_kind_specific_factories_project_real_task6_task8_task9_dtos(public_ids: tuple[str, ...]) -> None:
    feature, observed = _feature(public_ids[0])
    feature_claim = EvidenceClaim.from_feature(feature, authorized_evidence=(observed,))
    rule_ref = EvidenceRef(public_ids[1], "derived", "feature", "feature:cash", "feature-v1")
    rule = RuleAssessment(
        strategy_id="oil_grab",
        phase="opening",
        window=FeatureWindow(0, 900),
        quality="available",
        rule_score=0.75,
        supporting_evidence=(rule_ref,),
        contradicting_evidence=(),
        details={"formula_version": "strategy-rule-score-v1"},
    )
    rule_claim = EvidenceClaim.from_rule_assessment(rule, authorized_evidence=(rule_ref,))
    longitudinal_evidence = LongitudinalEvidenceDTO(
        public_id=public_ids[2],
        tier="derived",
        source_kind="longitudinal_corpus",
        source_key="longitudinal:cash",
        schema_version=1,
    )
    result = LongitudinalResultDTO(
        public_id=public_ids[3],
        result_name="cash_float_median",
        result_kind="metric",
        sample_count=12,
        missing_count=2,
        quality="complete",
        reason=None,
        statistics={"median": 1250.0},
        members=(),
        evidence_public_id=public_ids[2],
    )
    longitudinal_claim = EvidenceClaim.from_longitudinal_result(result, evidence=longitudinal_evidence)
    assert feature_claim.kind == "feature"
    assert thaw_canonical(feature_claim.value) == {
        "name": "cash_float",
        "raw_value": 1250,
        "scope": {"scope_key": public_ids[0], "scope_type": "player"},
        "unit": "credits",
        "value_type": "integer",
        "window": {"frame_end": 900, "frame_start": 0},
    }
    assert rule_claim.kind == "rule_candidate"
    assert rule_claim.evidence_ids == (public_ids[1],)
    assert longitudinal_claim.kind == "longitudinal"
    assert longitudinal_claim.evidence_ids == (public_ids[2],)


@pytest.mark.parametrize(
    ("ref", "authorized"),
    [
        (
            EvidenceRef(
                "00000000-0000-0000-0000-000000000020",
                "observed",
                "manual",
                "manual:correction",
                "manual-v1",
            ),
            None,
        ),
        (
            EvidenceRef(
                "00000000-0000-0000-0000-000000000021",
                "inferred",
                "llm",
                "llm:claim",
                "inference-v1",
            ),
            None,
        ),
        (
            EvidenceRef(
                "00000000-0000-0000-0000-000000000022",
                "observed",
                "telemetry",
                "C:/private/replay.rep",
                "telemetry-v2",
            ),
            None,
        ),
    ],
)
def test_feature_factory_rejects_manual_llm_and_path_provenance(
    public_ids: tuple[str, ...], ref: EvidenceRef, authorized: object
) -> None:
    feature, _ = _feature(public_ids[0])
    forged = dataclasses.replace(feature, input_evidence=(ref,))
    with pytest.raises(EvidenceBundleError, match="unauthorized_evidence"):
        EvidenceClaim.from_feature(forged, authorized_evidence=(ref,))


def test_feature_factory_rejects_unrelated_authorization(public_ids: tuple[str, ...]) -> None:
    feature, observed = _feature(public_ids[0])
    unrelated = _observed_ref(public_ids[1])
    with pytest.raises(EvidenceBundleError, match="unauthorized_evidence"):
        EvidenceClaim.from_feature(feature, authorized_evidence=(unrelated,))
    assert observed != unrelated


def test_feature_factory_rejects_self_authorized_invented_reference(public_ids: tuple[str, ...]) -> None:
    invented = _observed_ref(public_ids[0], source_key="invented:telemetry")
    feature = FeatureValue(
        name="cash_float",
        value_type="integer",
        raw_value=1250,
        unit="credits",
        scope=FeatureScope("player", public_ids[0], replay_player_public_id=public_ids[0]),
        window=FeatureWindow(0, 900),
        quality="complete",
        quality_reason=None,
        input_evidence=(invented,),
    )
    with pytest.raises(EvidenceBundleError, match="unauthorized_evidence"):
        EvidenceClaim.from_feature(feature, authorized_evidence=(invented,))


def test_feature_factory_deduplicates_identical_ref_reused_across_roles(
    public_ids: tuple[str, ...]
) -> None:
    ref = _observed_ref(public_ids[0])
    feature, _ = _feature(public_ids[0])
    feature = dataclasses.replace(feature, input_evidence=(ref,), supporting_evidence=(ref,))
    claim = EvidenceClaim.from_feature(feature, authorized_evidence=(ref,))
    assert claim.evidence_ids == (public_ids[0],)


def test_feature_factory_rejects_conflicting_semantics_for_same_public_id(
    public_ids: tuple[str, ...]
) -> None:
    observed = _observed_ref(public_ids[0], source_key="event:one")
    conflicting = _observed_ref(public_ids[0], source_key="event:two")
    feature, _ = _feature(public_ids[0])
    feature = dataclasses.replace(
        feature,
        input_evidence=(observed,),
        supporting_evidence=(conflicting,),
    )
    with pytest.raises(EvidenceBundleError, match="conflicting_evidence"):
        EvidenceClaim.from_feature(
            feature,
            authorized_evidence=(observed, conflicting),
        )


@pytest.mark.parametrize(("unique_count", "accepted"), [(32, True), (33, False)])
def test_citation_cap_applies_after_cross_role_deduplication(
    public_ids: tuple[str, ...], unique_count: int, accepted: bool
) -> None:
    refs = tuple(_observed_ref(public_ids[index]) for index in range(unique_count))
    feature, _ = _feature(public_ids[0])
    feature = dataclasses.replace(feature, input_evidence=refs, supporting_evidence=refs)
    if accepted:
        claim = EvidenceClaim.from_feature(feature, authorized_evidence=refs)
        assert len(claim.evidence_ids) == 32
    else:
        with pytest.raises(EvidenceBundleError, match="evidence_bundle_oversize"):
            EvidenceClaim.from_feature(feature, authorized_evidence=refs)


def test_oversize_feature_can_cite_its_persisted_derived_evidence(
    public_ids: tuple[str, ...],
) -> None:
    refs = tuple(_observed_ref(public_ids[index]) for index in range(33))
    feature, _ = _feature(public_ids[0])
    feature = dataclasses.replace(feature, input_evidence=refs)
    derived = EvidenceRef(
        public_ids[33],
        "derived",
        "feature",
        "feature:cash-change-total",
        "feature-v1",
    )

    claim = EvidenceClaim.from_derived_feature(feature, evidence=derived)

    assert claim.evidence_ids == (derived.public_id,)
    assert thaw_canonical(claim.value)["name"] == feature.name  # type: ignore[index]


def test_oversize_rule_can_cite_its_persisted_derived_evidence(
    public_ids: tuple[str, ...],
) -> None:
    refs = tuple(_observed_ref(public_ids[index]) for index in range(33))
    assessment = RuleAssessment(
        strategy_id="scouted_pressure",
        phase="early",
        window=FeatureWindow(0, 900),
        quality="available",
        rule_score=1.0,
        supporting_evidence=refs,
        contradicting_evidence=(),
        details={"formula_version": "strategy-rule-score-v1"},
    )
    derived = EvidenceRef(
        public_ids[33],
        "derived",
        "strategy_rule",
        "strategy-rule:scouted-pressure",
        "strategy_rule-v1",
    )

    claim = EvidenceClaim.from_derived_rule_assessment(assessment, evidence=derived)

    assert claim.evidence_ids == (derived.public_id,)
    assert thaw_canonical(claim.value)["strategy_id"] == assessment.strategy_id  # type: ignore[index]


def test_feature_factory_reowns_forged_task6_value(public_ids: tuple[str, ...]) -> None:
    attacker: list[object] = [1]
    feature, observed = _feature(public_ids[0], FrozenMapping((("samples", attacker),)))
    claim = EvidenceClaim.from_feature(feature, authorized_evidence=(observed,))
    attacker[0] = 999
    assert thaw_canonical(claim.value)["raw_value"] == {"samples": [1]}  # type: ignore[index]


def test_raw_claim_construction_is_not_a_public_json_uuid_bypass(public_ids: tuple[str, ...]) -> None:
    with pytest.raises(TypeError):
        EvidenceClaim("invented", "feature", {"path": "C:/secret.rep"}, "complete", None, (public_ids[0],))


def test_semantic_change_changes_digest(public_ids: tuple[str, ...]) -> None:
    first = _bundle(public_ids, (_claim(public_ids[0], value=1),))
    second = _bundle(public_ids, (_claim(public_ids[0], value=2),))
    assert first.digest != second.digest


@pytest.mark.parametrize(
    "changes",
    [
        {"replay_public_id": "NOT-CANONICAL"},
        {"replay_sha256": "A" * 64},
        {"claims": []},
    ],
)
def test_bundle_rejects_noncanonical_public_identity_or_container(
    public_ids: tuple[str, ...], changes: dict[str, object]
) -> None:
    values: dict[str, object] = {
        "replay_public_id": public_ids[10],
        "replay_sha256": "a" * 64,
        "claims": (_claim(public_ids[0]),),
    }
    values.update(changes)
    with pytest.raises(EvidenceBundleError):
        build_evidence_bundle(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        (lambda ids: tuple(_claim(ids[index % len(ids)], claim_id=f"c{index}") for index in range(257)), "evidence_bundle_oversize"),
        (lambda ids: (_claim(ids[0], evidence_ids=tuple(ids[:33])),), "evidence_bundle_oversize"),
        (lambda ids: (_claim(ids[0], value="x" * 4097),), "evidence_bundle_oversize"),
        (
            lambda ids: tuple(
                _claim(ids[index % len(ids)], claim_id=f"c{index}", value="x" * 4096) for index in range(64)
            ),
            "evidence_bundle_oversize",
        ),
    ],
)
def test_bundle_limits_fail_typed_without_truncation(
    public_ids: tuple[str, ...], claims: object, code: str
) -> None:
    with pytest.raises(EvidenceBundleError) as caught:
        _bundle(public_ids, claims(public_ids))  # type: ignore[operator,arg-type]
    assert caught.value.code == code
    assert str(caught.value) == code
    assert "x" not in repr(caught.value)
