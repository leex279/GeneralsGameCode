"""Bounded immutable evidence passed to an optional structured provider."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Literal, TypeAlias, cast
from uuid import UUID

from generals_replay_analyzer.features.base import FeatureValue
from generals_replay_analyzer.features.evidence import CanonicalValue, FrozenMapping, thaw_canonical
from generals_replay_analyzer.features.evidence import EvidenceRef as FeatureEvidenceRef
from generals_replay_analyzer.longitudinal.segments import (
    FrozenJSONMapping as LongitudinalFrozenJSONMapping,
)
from generals_replay_analyzer.longitudinal.segments import LongitudinalEvidenceDTO, LongitudinalResultDTO
from generals_replay_analyzer.strategy.rules import RuleAssessment

EvidenceKind: TypeAlias = Literal["replay_context", "quality", "feature", "rule_candidate", "longitudinal"]
EvidenceQuality: TypeAlias = Literal["complete", "partial", "unavailable"]
EvidenceRef: TypeAlias = str

MAX_CLAIMS = 256
MAX_CITATIONS_PER_CLAIM = 32
MAX_SCALAR_BYTES = 4096
MAX_BUNDLE_BYTES = 262144
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 16384
EVIDENCE_BUNDLE_VERSION: Literal["evidence-bundle-v1"] = "evidence-bundle-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"replay_context", "quality", "feature", "rule_candidate", "longitudinal"}
_QUALITIES = {"complete", "partial", "unavailable"}
_OBSERVED_SOURCE_KINDS = {"catalog", "map_manifest", "parser", "telemetry"}
_DERIVED_SOURCE_KINDS = {"feature", "longitudinal_corpus", "strategy_rule"}
_FORBIDDEN_VALUE_KEYS = {
    "absolute_path",
    "created_at",
    "database_id",
    "managed_path",
    "manual_correction",
    "invented",
    "llm_output",
    "path",
    "raw_replay_bytes",
    "row_id",
    "source_key",
    "sourceKey",
    "source_path",
    "timestamp",
    "updated_at",
}


class EvidenceBundleError(ValueError):
    """A stable path-free evidence-bundle rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"EvidenceBundleError(code={self.code!r})"


def _public_id(value: str, label: str) -> None:
    if type(value) is not str:
        raise EvidenceBundleError(f"invalid_{label}")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise EvidenceBundleError(f"invalid_{label}") from None
    if str(parsed) != value:
        raise EvidenceBundleError(f"invalid_{label}")


def _scalar_size(value: str) -> None:
    if len(value.encode("utf-8")) > MAX_SCALAR_BYTES:
        raise EvidenceBundleError("evidence_bundle_oversize")


def _own_canonical(
    value: object,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> CanonicalValue:
    if depth > MAX_JSON_DEPTH:
        raise EvidenceBundleError("evidence_bundle_oversize")
    budget = [0] if nodes is None else nodes
    budget[0] += 1
    if budget[0] > MAX_JSON_NODES:
        raise EvidenceBundleError("evidence_bundle_oversize")
    if value is None or type(value) in (bool, int):
        return cast(None | bool | int, value)
    if type(value) is float:
        number = value
        if not math.isfinite(number) or (number == 0.0 and math.copysign(1.0, number) < 0):
            raise EvidenceBundleError("invalid_evidence_value")
        return number
    if type(value) is str:
        text = value
        _scalar_size(text)
        return text
    if type(value) is FrozenMapping:
        raw_items: object = tuple(value)
    elif type(value) is LongitudinalFrozenJSONMapping:
        raw_items = tuple((key, value[key]) for key in value)
    elif type(value) is dict:
        raw_items = tuple(cast(dict[object, object], value).items())
    else:
        raw_items = None
    if raw_items is not None:
        owned_items: list[tuple[str, object]] = []
        for raw_item in cast(tuple[object, ...], raw_items):
            if type(raw_item) is not tuple or len(cast(tuple[object, ...], raw_item)) != 2:
                raise EvidenceBundleError("invalid_evidence_value")
            key, item = cast(tuple[object, object], raw_item)
            if type(key) is not str:
                raise EvidenceBundleError("invalid_evidence_value")
            _scalar_size(key)
            if key in _FORBIDDEN_VALUE_KEYS:
                raise EvidenceBundleError("nonpublic_evidence_value")
            owned_items.append((key, _own_canonical(item, depth=depth + 1, nodes=budget)))
        if len({key for key, _ in owned_items}) != len(owned_items):
            raise EvidenceBundleError("invalid_evidence_value")
        return FrozenMapping(tuple(sorted(owned_items)))
    if type(value) in (list, tuple):
        return tuple(_own_canonical(item, depth=depth + 1, nodes=budget) for item in cast(list[object] | tuple[object, ...], value))
    raise EvidenceBundleError("invalid_evidence_value")


_CLAIM_AUTHORITY = object()


def _safe_source_key(value: str) -> bool:
    forbidden_markers = ("invented", "llm", "manual", "path", "timestamp")
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and not any(marker in value for marker in ("/", "\\", "\x00"))
        and not any(marker in value.casefold() for marker in forbidden_markers)
        and len(value.encode("utf-8")) <= MAX_SCALAR_BYTES
    )


def _authorize_refs(
    refs: tuple[FeatureEvidenceRef, ...],
    authorized: tuple[FeatureEvidenceRef, ...],
) -> tuple[str, ...]:
    if type(refs) is not tuple or type(authorized) is not tuple or not refs:
        raise EvidenceBundleError("unauthorized_evidence")
    if any(type(ref) is not FeatureEvidenceRef for ref in refs + authorized):
        raise EvidenceBundleError("unauthorized_evidence")

    def validate_ref(ref: FeatureEvidenceRef) -> None:
        _public_id(ref.public_id, "evidence_id")
        allowed_kind = (
            ref.tier == "observed"
            and ref.source_kind in _OBSERVED_SOURCE_KINDS
            or ref.tier == "derived"
            and ref.source_kind in _DERIVED_SOURCE_KINDS
        )
        if (
            not allowed_kind
            or not _safe_source_key(ref.source_key)
            or type(ref.schema_version) is not str
            or not ref.schema_version
            or ref.schema_version != ref.schema_version.strip()
        ):
            raise EvidenceBundleError("unauthorized_evidence")

    authorized_by_id: dict[str, FeatureEvidenceRef] = {}
    for ref in authorized:
        validate_ref(ref)
        existing = authorized_by_id.get(ref.public_id)
        if existing is not None and existing != ref:
            raise EvidenceBundleError("conflicting_evidence")
        authorized_by_id[ref.public_id] = ref

    unique_refs: dict[str, FeatureEvidenceRef] = {}
    for ref in refs:
        validate_ref(ref)
        existing = unique_refs.get(ref.public_id)
        if existing is not None and existing != ref:
            raise EvidenceBundleError("conflicting_evidence")
        if authorized_by_id.get(ref.public_id) != ref:
            raise EvidenceBundleError("unauthorized_evidence")
        unique_refs[ref.public_id] = ref
    if len(unique_refs) > MAX_CITATIONS_PER_CLAIM:
        raise EvidenceBundleError("evidence_bundle_oversize")
    return tuple(sorted(unique_refs))


def _feature_quality(value: FeatureValue) -> tuple[EvidenceQuality, str | None]:
    if value.quality == "complete":
        if value.quality_reason is not None or value.raw_value is None:
            raise EvidenceBundleError("invalid_source_dto")
        return "complete", None
    if value.quality in ("partial", "unavailable"):
        reason = value.quality_reason
        if type(reason) is not str or not reason or reason != reason.strip():
            raise EvidenceBundleError("invalid_source_dto")
        if value.quality == "unavailable" and value.raw_value is not None:
            raise EvidenceBundleError("invalid_source_dto")
        return value.quality, reason
    raise EvidenceBundleError("invalid_source_dto")


@dataclass(frozen=True, init=False)
class EvidenceClaim:
    """One deterministic claim with exact public evidence citations."""

    claim_id: str
    kind: EvidenceKind
    value: CanonicalValue
    quality: EvidenceQuality
    quality_reason: str | None
    evidence_ids: tuple[EvidenceRef, ...]
    _factory_authority: object = field(repr=False, compare=False)

    def __init__(
        self,
        claim_id: str,
        kind: EvidenceKind,
        value: object,
        quality: EvidenceQuality,
        quality_reason: str | None,
        evidence_ids: tuple[EvidenceRef, ...],
        *,
        _authority: object,
    ) -> None:
        if _authority is not _CLAIM_AUTHORITY:
            raise TypeError("EvidenceClaim is created only from accepted public DTO factories")
        object.__setattr__(self, "claim_id", claim_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", cast(CanonicalValue, value))
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "quality_reason", quality_reason)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "_factory_authority", _CLAIM_AUTHORITY)
        self.__post_init__()

    @classmethod
    def _create(
        cls,
        claim_id: str,
        kind: EvidenceKind,
        value: object,
        quality: EvidenceQuality,
        quality_reason: str | None,
        evidence_ids: tuple[str, ...],
    ) -> EvidenceClaim:
        return cls(
            claim_id,
            kind,
            value,
            quality,
            quality_reason,
            evidence_ids,
            _authority=_CLAIM_AUTHORITY,
        )

    @classmethod
    def from_feature(
        cls,
        feature: FeatureValue,
        *,
        authorized_evidence: tuple[FeatureEvidenceRef, ...],
    ) -> EvidenceClaim:
        if type(feature) is not FeatureValue:
            raise EvidenceBundleError("invalid_source_dto")
        quality, reason = _feature_quality(feature)
        if any(ref.tier != "observed" for ref in feature.input_evidence):
            raise EvidenceBundleError("unauthorized_evidence")
        refs = feature.input_evidence + feature.supporting_evidence + feature.contradicting_evidence
        evidence_ids = _authorize_refs(refs, authorized_evidence)
        projection = {
            "name": feature.name,
            "value_type": feature.value_type,
            "raw_value": feature.raw_value,
            "unit": feature.unit,
            "scope": {"scope_type": feature.scope.scope_type, "scope_key": feature.scope.scope_key},
            "window": {"frame_start": feature.window.frame_start, "frame_end": feature.window.frame_end},
        }
        return cls._create(feature.name, "feature", projection, quality, reason, evidence_ids)

    @classmethod
    def from_feature_quality(
        cls,
        feature: FeatureValue,
        *,
        authorized_evidence: tuple[FeatureEvidenceRef, ...],
    ) -> EvidenceClaim:
        if type(feature) is not FeatureValue:
            raise EvidenceBundleError("invalid_source_dto")
        quality, reason = _feature_quality(feature)
        if any(ref.tier != "observed" for ref in feature.input_evidence):
            raise EvidenceBundleError("unauthorized_evidence")
        refs = feature.input_evidence + feature.supporting_evidence + feature.contradicting_evidence
        evidence_ids = _authorize_refs(refs, authorized_evidence)
        projection = {
            "feature_name": feature.name,
            "quality": quality,
            "quality_reason": reason,
            "window": {"frame_start": feature.window.frame_start, "frame_end": feature.window.frame_end},
        }
        return cls._create(feature.name, "quality", projection, quality, reason, evidence_ids)

    @classmethod
    def from_rule_assessment(
        cls,
        assessment: RuleAssessment,
        *,
        authorized_evidence: tuple[FeatureEvidenceRef, ...],
    ) -> EvidenceClaim:
        if type(assessment) is not RuleAssessment:
            raise EvidenceBundleError("invalid_source_dto")
        quality_map: dict[str, EvidenceQuality] = {
            "available": "complete",
            "partial": "partial",
            "unavailable": "unavailable",
        }
        if assessment.quality not in quality_map:
            raise EvidenceBundleError("invalid_source_dto")
        quality = quality_map[assessment.quality]
        details = assessment.details
        if type(details) is not FrozenMapping:
            raise EvidenceBundleError("invalid_source_dto")
        raw_reason = next((item for key, item in details if key == "reason"), None)
        reason = None if quality == "complete" else raw_reason
        if quality != "complete" and (type(reason) is not str or not reason or reason != reason.strip()):
            raise EvidenceBundleError("invalid_source_dto")
        refs = assessment.supporting_evidence + assessment.contradicting_evidence
        evidence_ids = _authorize_refs(refs, authorized_evidence)
        projection = {
            "strategy_id": assessment.strategy_id,
            "phase": assessment.phase,
            "window": {
                "frame_start": assessment.window.frame_start,
                "frame_end": assessment.window.frame_end,
            },
            "rule_score": assessment.rule_score,
            "details": details,
        }
        return cls._create(
            assessment.strategy_id,
            "rule_candidate",
            projection,
            quality,
            cast(str | None, reason),
            evidence_ids,
        )

    @classmethod
    def from_longitudinal_result(
        cls,
        result: LongitudinalResultDTO,
        *,
        evidence: LongitudinalEvidenceDTO,
    ) -> EvidenceClaim:
        if type(result) is not LongitudinalResultDTO or type(evidence) is not LongitudinalEvidenceDTO:
            raise EvidenceBundleError("invalid_source_dto")
        if (
            evidence.public_id != result.evidence_public_id
            or evidence.tier != "derived"
            or evidence.source_kind != "longitudinal_corpus"
            or not _safe_source_key(evidence.source_key)
        ):
            raise EvidenceBundleError("unauthorized_evidence")
        _public_id(evidence.public_id, "evidence_id")
        quality = result.quality
        reason = result.reason
        if quality == "complete" and reason is not None:
            raise EvidenceBundleError("invalid_source_dto")
        if quality != "complete" and (type(reason) is not str or not reason or reason != reason.strip()):
            raise EvidenceBundleError("invalid_source_dto")
        projection = {
            "result_name": result.result_name,
            "result_kind": result.result_kind,
            "sample_count": result.sample_count,
            "missing_count": result.missing_count,
            "statistics": result.statistics,
        }
        return cls._create(
            result.result_name,
            "longitudinal",
            projection,
            quality,
            reason,
            (evidence.public_id,),
        )

    def __post_init__(self) -> None:
        if getattr(self, "_factory_authority", None) is not _CLAIM_AUTHORITY:
            raise EvidenceBundleError("invalid_claims")
        if type(self.claim_id) is not str or not self.claim_id or self.claim_id != self.claim_id.strip():
            raise EvidenceBundleError("invalid_claim_id")
        _scalar_size(self.claim_id)
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise EvidenceBundleError("invalid_claim_kind")
        if type(self.quality) is not str or self.quality not in _QUALITIES:
            raise EvidenceBundleError("invalid_claim_quality")
        if self.quality == "complete" and self.quality_reason is not None:
            raise EvidenceBundleError("invalid_quality_reason")
        if self.quality in ("partial", "unavailable") and (
            type(self.quality_reason) is not str
            or not self.quality_reason
            or self.quality_reason != self.quality_reason.strip()
        ):
            raise EvidenceBundleError("invalid_quality_reason")
        if self.quality_reason is not None:
            _scalar_size(self.quality_reason)
        if type(self.evidence_ids) is not tuple or not self.evidence_ids:
            raise EvidenceBundleError("missing_citation")
        if len(self.evidence_ids) > MAX_CITATIONS_PER_CLAIM:
            raise EvidenceBundleError("evidence_bundle_oversize")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise EvidenceBundleError("duplicate_citation")
        for public_id in self.evidence_ids:
            _public_id(public_id, "evidence_id")
        try:
            frozen = _own_canonical(self.value)
        except RecursionError:
            raise EvidenceBundleError("evidence_bundle_oversize") from None
        object.__setattr__(self, "value", frozen)
        object.__setattr__(self, "evidence_ids", tuple(sorted(self.evidence_ids)))


@dataclass(frozen=True)
class EvidenceBundle:
    """Exact canonical provider input and its provenance identity."""

    schema_version: Literal["evidence-bundle-v1"]
    replay_public_id: str
    replay_sha256: str
    claims: tuple[EvidenceClaim, ...]
    unknown_or_missing: tuple[str, ...]
    canonical_json: bytes
    digest: str

    def __post_init__(self) -> None:
        if type(self.claims) is not tuple:
            raise EvidenceBundleError("invalid_bundle_metadata")
        if len(self.claims) > MAX_CLAIMS:
            raise EvidenceBundleError("evidence_bundle_oversize")
        if type(self.schema_version) is not str or self.schema_version != EVIDENCE_BUNDLE_VERSION:
            raise EvidenceBundleError("invalid_bundle_metadata")
        _public_id(self.replay_public_id, "replay_public_id")
        if type(self.replay_sha256) is not str or not _SHA256.fullmatch(self.replay_sha256):
            raise EvidenceBundleError("invalid_bundle_metadata")
        if any(
            type(claim) is not EvidenceClaim
            or getattr(claim, "_factory_authority", None) is not _CLAIM_AUTHORITY
            for claim in self.claims
        ):
            raise EvidenceBundleError("invalid_claims")
        if self.claims != tuple(
            sorted(self.claims, key=lambda claim: (claim.kind, claim.claim_id))
        ):
            raise EvidenceBundleError("invalid_bundle_metadata")
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise EvidenceBundleError("duplicate_claim")
        if type(self.unknown_or_missing) is not tuple or any(
            type(item) is not str for item in self.unknown_or_missing
        ):
            raise EvidenceBundleError("invalid_bundle_metadata")
        expected_missing = tuple(sorted(claim.claim_id for claim in self.claims if claim.quality == "unavailable"))
        if self.unknown_or_missing != expected_missing:
            raise EvidenceBundleError("invalid_bundle_metadata")
        if type(self.canonical_json) is not bytes or type(self.digest) is not str:
            raise EvidenceBundleError("invalid_bundle_metadata")
        document = {
            "schema_version": self.schema_version,
            "replay_public_id": self.replay_public_id,
            "replay_sha256": self.replay_sha256,
            "claims": [_claim_document(claim) for claim in self.claims],
            "unknown_or_missing": list(self.unknown_or_missing),
        }
        try:
            expected = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError):
            raise EvidenceBundleError("invalid_bundle_metadata") from None
        if (
            self.canonical_json != expected
            or self.digest != hashlib.sha256(expected).hexdigest()
            or len(expected) > MAX_BUNDLE_BYTES
        ):
            raise EvidenceBundleError("invalid_bundle_metadata")


def _claim_document(claim: EvidenceClaim) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "kind": claim.kind,
        "value": thaw_canonical(claim.value),
        "quality": claim.quality,
        "quality_reason": claim.quality_reason,
        "evidence_ids": list(claim.evidence_ids),
    }


# TheSuperHackers @feature Leex 22/08/2026 Bound provider input to canonical public evidence only. (#TBD)
def build_evidence_bundle(
    *, replay_public_id: str, replay_sha256: str, claims: tuple[EvidenceClaim, ...]
) -> EvidenceBundle:
    """Validate, sort, and encode one evidence bundle without truncation."""
    _public_id(replay_public_id, "replay_public_id")
    if type(replay_sha256) is not str or not _SHA256.fullmatch(replay_sha256):
        raise EvidenceBundleError("invalid_replay_sha256")
    if type(claims) is not tuple:
        raise EvidenceBundleError("invalid_claims")
    if len(claims) > MAX_CLAIMS:
        raise EvidenceBundleError("evidence_bundle_oversize")
    if any(
        type(claim) is not EvidenceClaim
        or getattr(claim, "_factory_authority", None) is not _CLAIM_AUTHORITY
        for claim in claims
    ):
        raise EvidenceBundleError("invalid_claims")
    identities = tuple(claim.claim_id for claim in claims)
    if len(set(identities)) != len(identities):
        raise EvidenceBundleError("duplicate_claim")
    ordered = tuple(sorted(claims, key=lambda claim: (claim.kind, claim.claim_id)))
    missing = tuple(sorted(claim.claim_id for claim in ordered if claim.quality == "unavailable"))
    document = {
        "schema_version": EVIDENCE_BUNDLE_VERSION,
        "replay_public_id": replay_public_id,
        "replay_sha256": replay_sha256,
        "claims": [_claim_document(claim) for claim in ordered],
        "unknown_or_missing": list(missing),
    }
    try:
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        raise EvidenceBundleError("invalid_claims") from None
    if len(canonical) > MAX_BUNDLE_BYTES:
        raise EvidenceBundleError("evidence_bundle_oversize")
    return EvidenceBundle(
        schema_version=EVIDENCE_BUNDLE_VERSION,
        replay_public_id=replay_public_id,
        replay_sha256=replay_sha256,
        claims=ordered,
        unknown_or_missing=missing,
        canonical_json=canonical,
        digest=hashlib.sha256(canonical).hexdigest(),
    )
