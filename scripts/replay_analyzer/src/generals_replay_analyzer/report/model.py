"""Deeply immutable public report values independent of ORM and filesystem identity."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Self, TypeAlias, cast
from uuid import UUID

ReportEvidenceTier: TypeAlias = Literal["observed", "derived", "inferred"]
ReportAvailability: TypeAlias = Literal["available", "partial", "unavailable"]
ReportFormat: TypeAlias = Literal["json", "html", "text"]
OllamaStatus: TypeAlias = Literal[
    "not_requested", "disabled", "succeeded", "unavailable", "failed", "invalid", "cancelled"
]


class FrozenReportMapping(tuple[tuple[str, object], ...]):
    """Tuple-backed marker that preserves the distinction between objects and arrays."""

    def __new__(cls, items: tuple[tuple[str, object], ...]) -> Self:
        return super().__new__(cls, items)


CanonicalValue: TypeAlias = None | bool | int | float | str | tuple["CanonicalValue", ...] | FrozenReportMapping

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ABSOLUTE_PATH = re.compile(
    r"(?:^|[=\s(\"'\[:])(?:[A-Za-z]:[\\/]|\\\\|[A-Za-z][A-Za-z0-9+.-]*://|/[^\s,;)\]}\"']+)",
    re.IGNORECASE,
)
_FORBIDDEN_KEYS = {
    "absolute_path",
    "created_at",
    "database_id",
    "managed_path",
    "relative_path",
    "row_id",
    "source_locator",
    "source_path",
    "updated_at",
}


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a nonempty built-in string")
    return value


def _public_uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical public_id UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label} must be a canonical public_id UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical public_id UUID")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def freeze_report_value(value: object) -> CanonicalValue:
    """Own one JSON-like semantic value while rejecting runtime and persistence identities."""
    if value is None or type(value) in (bool, int):
        return cast(None | bool | int, value)
    if type(value) is str:
        if _ABSOLUTE_PATH.search(value) is not None:
            raise ValueError("absolute paths are not canonical report values")
        return value
    if type(value) is float:
        if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
            raise ValueError("canonical floats must be finite and may not be negative zero")
        return value
    if isinstance(value, (bytes, bytearray, memoryview, Path, date, datetime)):
        raise TypeError("paths, times, and byte sequences are not canonical report values")
    if hasattr(value, "_sa_instance_state"):
        raise TypeError("ORM rows are not canonical report values")
    if isinstance(value, FrozenReportMapping):
        keys: set[str] = set()
        items: list[tuple[str, object]] = []
        for entry in value:
            if type(entry) is not tuple or len(entry) != 2 or type(entry[0]) is not str:
                raise TypeError("canonical frozen mapping entries must be string pairs")
            key, item = entry
            if key in keys:
                raise ValueError(f"duplicate canonical mapping key: {key}")
            if key in _FORBIDDEN_KEYS:
                raise ValueError(f"nonsemantic persistence key is forbidden: {key}")
            keys.add(key)
            items.append((key, freeze_report_value(item)))
        return FrozenReportMapping(tuple(sorted(items, key=lambda pair: pair[0])))
    if isinstance(value, Mapping):
        mapping_items: list[tuple[str, object]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("canonical mapping keys must be built-in strings")
            if key in _FORBIDDEN_KEYS:
                raise ValueError(f"nonsemantic persistence key is forbidden: {key}")
            mapping_items.append((key, freeze_report_value(item)))
        return FrozenReportMapping(tuple(sorted(mapping_items, key=lambda pair: pair[0])))
    if type(value) in (list, tuple):
        return tuple(freeze_report_value(item) for item in cast(list[object] | tuple[object, ...], value))
    raise TypeError(f"unsupported canonical report value: {type(value).__name__}")


def thaw_report_value(value: CanonicalValue) -> object:
    """Return a detached plain JSON projection of one immutable report value."""
    if isinstance(value, FrozenReportMapping):
        return {key: thaw_report_value(cast(CanonicalValue, item)) for key, item in value}
    if isinstance(value, tuple):
        return [thaw_report_value(item) for item in cast(tuple[CanonicalValue, ...], value)]
    return value


@dataclass(frozen=True)
class ReportEvidenceRef:
    public_id: str
    tier: ReportEvidenceTier

    def __post_init__(self) -> None:
        _public_uuid(self.public_id, "public_id")
        if self.tier not in ("observed", "derived", "inferred"):
            raise ValueError("invalid report evidence tier")


# TheSuperHackers @feature Leex 22/08/2026 Preserve raw, missing, and provenance semantics in immutable report claims. (#TBD)
@dataclass(frozen=True)
class ReportValue:
    claim_id: str
    section: str
    label: str
    raw_value: CanonicalValue | None
    unit: str | None
    availability: ReportAvailability
    unavailable_reason: str | None
    scope: CanonicalValue
    frame_window: tuple[int, int] | None
    evidence: tuple[ReportEvidenceRef, ...]
    details: CanonicalValue

    def __post_init__(self) -> None:
        _text(self.claim_id, "claim_id")
        _text(self.section, "section")
        _text(self.label, "label")
        if self.unit is not None:
            _text(self.unit, "unit")
        if self.availability not in ("available", "partial", "unavailable"):
            raise ValueError("invalid report availability")
        raw = freeze_report_value(self.raw_value)
        reason = self.unavailable_reason
        if raw is None:
            if self.availability != "unavailable" or reason is None:
                raise ValueError("null report value must be unavailable with a stable reason")
        elif self.availability == "unavailable":
            raise ValueError("unavailable report value must be null")
        elif not self.evidence:
            raise ValueError("non-null report value requires public evidence")
        if self.availability == "available" and reason is not None:
            raise ValueError("available report value cannot have an unavailable reason")
        if self.availability == "partial" and reason is None:
            raise ValueError("partial report value requires a stable reason")
        if reason is not None:
            _text(reason, "unavailable_reason")
        if self.frame_window is not None and (
            type(self.frame_window) is not tuple
            or len(self.frame_window) != 2
            or type(self.frame_window[0]) is not int
            or type(self.frame_window[1]) is not int
            or self.frame_window[0] < 0
            or self.frame_window[1] < self.frame_window[0]
        ):
            raise ValueError("report frame window must be inclusive and nonnegative")
        if type(self.evidence) is not tuple or any(type(item) is not ReportEvidenceRef for item in self.evidence):
            raise TypeError("report evidence must be an immutable typed tuple")
        by_identity = {(item.public_id, item.tier): item for item in self.evidence}
        object.__setattr__(self, "raw_value", raw)
        object.__setattr__(self, "scope", freeze_report_value(self.scope))
        object.__setattr__(self, "details", freeze_report_value(self.details))
        object.__setattr__(self, "evidence", tuple(by_identity[key] for key in sorted(by_identity)))


@dataclass(frozen=True)
class ReportLifecycle:
    lifecycle_state: str
    parser_completion_status: str | None
    telemetry_status: str | None
    telemetry_runner_status: str | None

    def __post_init__(self) -> None:
        _text(self.lifecycle_state, "lifecycle_state")
        for label, value in (
            ("parser_completion_status", self.parser_completion_status),
            ("telemetry_status", self.telemetry_status),
            ("telemetry_runner_status", self.telemetry_runner_status),
        ):
            if value is not None:
                _text(value, label)


@dataclass(frozen=True)
class ReportQualityIssue:
    public_id: str
    stage: str
    issue_code: str
    severity: str
    details: CanonicalValue
    resolved: bool

    def __post_init__(self) -> None:
        _public_uuid(self.public_id, "public_id")
        _text(self.stage, "stage")
        _text(self.issue_code, "issue_code")
        _text(self.severity, "severity")
        if type(self.resolved) is not bool:
            raise TypeError("resolved must be a built-in boolean")
        object.__setattr__(self, "details", freeze_report_value(self.details))


@dataclass(frozen=True)
class OllamaReportStatus:
    requested: bool
    status: OllamaStatus
    analysis_run_id: str | None
    provider: str | None
    model_name: str | None
    model_digest: str | None
    prompt_version: str | None
    response_schema_version: str | None
    diagnostic_codes: tuple[str, ...]
    validated_prose: CanonicalValue | None

    @classmethod
    def not_requested(cls) -> OllamaReportStatus:
        return cls(False, "not_requested", None, None, None, None, None, None, (), None)

    def __post_init__(self) -> None:
        if type(self.requested) is not bool:
            raise TypeError("requested must be a built-in boolean")
        if self.status not in (
            "not_requested",
            "disabled",
            "succeeded",
            "unavailable",
            "failed",
            "invalid",
            "cancelled",
        ):
            raise ValueError("invalid Ollama report status")
        if self.analysis_run_id is not None:
            _public_uuid(self.analysis_run_id, "analysis_run_id")
        for label, value in (
            ("provider", self.provider),
            ("model_name", self.model_name),
            ("prompt_version", self.prompt_version),
            ("response_schema_version", self.response_schema_version),
        ):
            if value is not None:
                _text(value, label)
        if self.model_digest is not None:
            _sha256(self.model_digest, "model_digest")
        if type(self.diagnostic_codes) is not tuple:
            raise TypeError("diagnostic_codes must be an immutable tuple")
        codes = tuple(sorted({_text(item, "diagnostic_code") for item in self.diagnostic_codes}))
        prose = None if self.validated_prose is None else freeze_report_value(self.validated_prose)
        if not self.requested:
            if (
                self.status != "not_requested"
                or any(
                    value is not None
                    for value in (
                        self.analysis_run_id,
                        self.provider,
                        self.model_name,
                        self.model_digest,
                        self.prompt_version,
                        self.response_schema_version,
                        prose,
                    )
                )
                or codes
            ):
                raise ValueError("not_requested Ollama state must not contain analysis data")
        elif self.status == "not_requested":
            raise ValueError("requested Ollama state cannot be not_requested")
        if self.status == "succeeded":
            required = (
                self.analysis_run_id,
                self.provider,
                self.model_name,
                self.model_digest,
                self.prompt_version,
                self.response_schema_version,
                prose,
            )
            if any(value is None for value in required):
                raise ValueError("succeeded Ollama state requires complete validated identity and prose")
        elif prose is not None:
            raise ValueError("only succeeded Ollama state may contain validated prose")
        object.__setattr__(self, "diagnostic_codes", codes)
        object.__setattr__(self, "validated_prose", prose)


@dataclass(frozen=True)
class ReportAssemblyInput:
    replay_public_id: str
    replay_sha256: str
    replay_player_public_id: str | None
    header_identity: CanonicalValue
    component_identity: CanonicalValue
    lifecycle: ReportLifecycle
    evidence_availability: tuple[ReportValue, ...]
    quality_issues: tuple[ReportQualityIssue, ...]
    observed: tuple[ReportValue, ...]
    derived: tuple[ReportValue, ...]
    inferred: tuple[ReportValue, ...]
    ollama: OllamaReportStatus
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        _public_uuid(self.replay_public_id, "replay_public_id")
        _sha256(self.replay_sha256, "replay_sha256")
        if self.replay_player_public_id is not None:
            _public_uuid(self.replay_player_public_id, "replay_player_public_id")
        if type(self.lifecycle) is not ReportLifecycle or type(self.ollama) is not OllamaReportStatus:
            raise TypeError("report assembly lifecycle and Ollama state must use accepted DTOs")
        for name, values, expected in (
            ("evidence_availability", self.evidence_availability, ReportValue),
            ("quality_issues", self.quality_issues, ReportQualityIssue),
            ("observed", self.observed, ReportValue),
            ("derived", self.derived, ReportValue),
            ("inferred", self.inferred, ReportValue),
        ):
            if type(values) is not tuple or any(type(value) is not expected for value in values):
                raise TypeError(f"{name} must be an immutable typed tuple")
        if type(self.warnings) is not tuple:
            raise TypeError("warnings must be an immutable tuple")
        object.__setattr__(self, "header_identity", freeze_report_value(self.header_identity))
        object.__setattr__(self, "component_identity", freeze_report_value(self.component_identity))
        object.__setattr__(self, "warnings", tuple(_text(value, "warning") for value in self.warnings))


# TheSuperHackers @feature Leex 22/08/2026 Expose a path-free versioned report document with explicit evidence tiers. (#TBD)
@dataclass(frozen=True)
class ReportDocument:
    schema_version: Literal["replay-report-v1"]
    report_public_id: str
    report_version: str
    input_digest: str
    cache_key: str
    replay_public_id: str
    replay_sha256: str
    replay_player_public_id: str | None
    lifecycle: ReportLifecycle
    evidence_availability: tuple[ReportValue, ...]
    quality_issues: tuple[ReportQualityIssue, ...]
    observed: tuple[ReportValue, ...]
    derived: tuple[ReportValue, ...]
    inferred: tuple[ReportValue, ...]
    ollama: OllamaReportStatus
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "replay-report-v1" or self.report_version != "replay-report-v1":
            raise ValueError("unsupported report version")
        _public_uuid(self.report_public_id, "report_public_id")
        _public_uuid(self.replay_public_id, "replay_public_id")
        if self.replay_player_public_id is not None:
            _public_uuid(self.replay_player_public_id, "replay_player_public_id")
        _sha256(self.input_digest, "input_digest")
        _sha256(self.cache_key, "cache_key")
        _sha256(self.replay_sha256, "replay_sha256")
        if type(self.lifecycle) is not ReportLifecycle or type(self.ollama) is not OllamaReportStatus:
            raise TypeError("report lifecycle and Ollama state must use accepted DTOs")
        availability = _ordered_values(self.evidence_availability, "evidence_availability")
        observed = _ordered_tier(self.observed, "observed")
        derived = _ordered_tier(self.derived, "derived")
        inferred = _ordered_tier(self.inferred, "inferred")
        evidence_tiers: dict[str, ReportEvidenceTier] = {}
        for value in (*availability, *observed, *derived, *inferred):
            for reference in value.evidence:
                prior = evidence_tiers.setdefault(reference.public_id, reference.tier)
                if prior != reference.tier:
                    raise ValueError("one public evidence ID cannot claim multiple tiers")
        issues = _ordered_issues(self.quality_issues)
        warnings = tuple(sorted({_text(item, "warning") for item in self.warnings}))
        object.__setattr__(self, "evidence_availability", availability)
        object.__setattr__(self, "quality_issues", issues)
        object.__setattr__(self, "observed", observed)
        object.__setattr__(self, "derived", derived)
        object.__setattr__(self, "inferred", inferred)
        object.__setattr__(self, "warnings", warnings)


def _ordered_values(values: tuple[ReportValue, ...], label: str) -> tuple[ReportValue, ...]:
    if type(values) is not tuple or any(type(value) is not ReportValue for value in values):
        raise TypeError(f"{label} must be an immutable ReportValue tuple")
    identities = [(value.section, value.claim_id) for value in values]
    if len(identities) != len(set(identities)):
        raise ValueError(f"duplicate {label} claim")
    return tuple(sorted(values, key=lambda value: (value.section, value.claim_id)))


def _ordered_tier(values: tuple[ReportValue, ...], tier: ReportEvidenceTier) -> tuple[ReportValue, ...]:
    ordered = _ordered_values(values, tier)
    ranks = {"observed": 0, "derived": 1, "inferred": 2}
    for value in ordered:
        highest = None if not value.evidence else max(value.evidence, key=lambda ref: ranks[ref.tier]).tier
        if value.raw_value is not None and highest != tier:
            raise ValueError(f"{tier} report claim requires {tier} evidence")
    return ordered


def _ordered_issues(values: tuple[ReportQualityIssue, ...]) -> tuple[ReportQualityIssue, ...]:
    if type(values) is not tuple or any(type(value) is not ReportQualityIssue for value in values):
        raise TypeError("quality_issues must be an immutable ReportQualityIssue tuple")
    identities = [(value.stage, value.issue_code, value.public_id) for value in values]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate quality issue")
    return tuple(sorted(values, key=lambda value: (value.stage, value.issue_code, value.public_id)))


@dataclass(frozen=True)
class ReportRequest:
    replay_public_id: str
    replay_player_public_id: str | None = None
    include_validated_ollama: bool = False
    publish: bool = True

    def __post_init__(self) -> None:
        _public_uuid(self.replay_public_id, "replay_public_id")
        if self.replay_player_public_id is not None:
            _public_uuid(self.replay_player_public_id, "replay_player_public_id")
        if type(self.include_validated_ollama) is not bool or type(self.publish) is not bool:
            raise TypeError("report request switches must be built-in booleans")


@dataclass(frozen=True)
class ReportAssetDTO:
    public_id: str
    sha256: str
    kind: str
    size_bytes: int

    def __post_init__(self) -> None:
        _public_uuid(self.public_id, "public_id")
        _sha256(self.sha256, "sha256")
        _text(self.kind, "kind")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")


@dataclass(frozen=True)
class ReportReceipt:
    document: ReportDocument
    cache_hit: bool
    structured_asset: ReportAssetDTO | None
    presentation_bundle_asset: ReportAssetDTO | None

    def __post_init__(self) -> None:
        if type(self.document) is not ReportDocument or type(self.cache_hit) is not bool:
            raise TypeError("report receipt fields use closed public types")
        for value in (self.structured_asset, self.presentation_bundle_asset):
            if value is not None and type(value) is not ReportAssetDTO:
                raise TypeError("report receipt assets use ReportAssetDTO")


def evidence_ref_to_mapping(value: ReportEvidenceRef) -> dict[str, object]:
    return {"public_id": value.public_id, "tier": value.tier}


def report_value_to_mapping(value: ReportValue) -> dict[str, object]:
    return {
        "claim_id": value.claim_id,
        "section": value.section,
        "label": value.label,
        "raw_value": thaw_report_value(value.raw_value),
        "unit": value.unit,
        "availability": value.availability,
        "unavailable_reason": value.unavailable_reason,
        "scope": thaw_report_value(value.scope),
        "frame_window": None if value.frame_window is None else list(value.frame_window),
        "evidence": [evidence_ref_to_mapping(item) for item in value.evidence],
        "details": thaw_report_value(value.details),
    }


def lifecycle_to_mapping(value: ReportLifecycle) -> dict[str, object]:
    return {
        "lifecycle_state": value.lifecycle_state,
        "parser_completion_status": value.parser_completion_status,
        "telemetry_status": value.telemetry_status,
        "telemetry_runner_status": value.telemetry_runner_status,
    }


def quality_issue_to_mapping(value: ReportQualityIssue) -> dict[str, object]:
    return {
        "public_id": value.public_id,
        "stage": value.stage,
        "issue_code": value.issue_code,
        "severity": value.severity,
        "details": thaw_report_value(value.details),
        "resolved": value.resolved,
    }


def ollama_to_mapping(value: OllamaReportStatus) -> dict[str, object]:
    return {
        "requested": value.requested,
        "status": value.status,
        "analysis_run_id": value.analysis_run_id,
        "provider": value.provider,
        "model_name": value.model_name,
        "model_digest": value.model_digest,
        "prompt_version": value.prompt_version,
        "response_schema_version": value.response_schema_version,
        "diagnostic_codes": list(value.diagnostic_codes),
        "validated_prose": None if value.validated_prose is None else thaw_report_value(value.validated_prose),
    }


def document_to_mapping(value: ReportDocument) -> dict[str, object]:
    """Return a detached JSON-compatible report document without presentation rounding."""
    return {
        "schema_version": value.schema_version,
        "report_public_id": value.report_public_id,
        "report_version": value.report_version,
        "input_digest": value.input_digest,
        "cache_key": value.cache_key,
        "replay_public_id": value.replay_public_id,
        "replay_sha256": value.replay_sha256,
        "replay_player_public_id": value.replay_player_public_id,
        "lifecycle": lifecycle_to_mapping(value.lifecycle),
        "evidence_availability": [report_value_to_mapping(item) for item in value.evidence_availability],
        "quality_issues": [quality_issue_to_mapping(item) for item in value.quality_issues],
        "observed": [report_value_to_mapping(item) for item in value.observed],
        "derived": [report_value_to_mapping(item) for item in value.derived],
        "inferred": [report_value_to_mapping(item) for item in value.inferred],
        "ollama": ollama_to_mapping(value.ollama),
        "warnings": list(value.warnings),
    }
