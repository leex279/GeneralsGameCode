"""Frozen public longitudinal segmentation and result DTOs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal, Self, TypeAlias, cast
from uuid import UUID

from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.evidence import CanonicalValue, FrozenMapping, freeze_canonical, thaw_canonical

ResultQuality: TypeAlias = Literal["complete", "partial", "unavailable"]
RunStatus: TypeAlias = Literal["succeeded", "failed"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_EXCLUDED_ISSUES = (
    "crc_mismatch",
    "exporter_failure",
    "missing_telemetry",
    "truncated",
    "version_mismatch",
)
_LIFECYCLE_STATES = {"discovered", "parsed", "engine_verified", "partial", "desynced", "unsupported", "failed"}


def _stable_string(value: str | None, label: str) -> None:
    if value is not None and (type(value) is not str or not value or value != value.strip()):
        raise ValueError(f"{label} must be an exact nonempty string")


def _public_id(value: str, label: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID")


def _digest(value: str, label: str) -> None:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lower-case SHA-256")


def _sorted_unique_strings(values: tuple[str, ...], label: str) -> None:
    if type(values) is not tuple or values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be a sorted unique tuple")
    if any(type(value) is not str or not value.strip() for value in values):
        raise ValueError(f"{label} values must be nonempty strings")


class FrozenJSONMapping(Mapping[str, object]):
    """Small tuple-backed public mapping with recursively immutable values."""

    __slots__ = ("_items",)

    def __init__(self, value: Mapping[str, object]) -> None:
        frozen = freeze_canonical(value)
        if not isinstance(frozen, FrozenMapping):
            raise TypeError("statistics must be a canonical mapping")
        self._items = tuple((key, _public_frozen(item)) for key, item in frozen)

    def __getitem__(self, key: str) -> object:
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def as_plain(self) -> dict[str, object]:
        return {key: _public_thawed(value) for key, value in self._items}

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and self.as_plain() == {
            key: _public_thawed(value) for key, value in other.items()
        }


def _public_frozen(value: object) -> object:
    if isinstance(value, FrozenMapping):
        return FrozenJSONMapping({key: thaw_canonical(cast(CanonicalValue, item)) for key, item in value})
    if isinstance(value, tuple):
        return tuple(_public_frozen(item) for item in value)
    return value


def _public_thawed(value: object) -> object:
    if isinstance(value, FrozenJSONMapping):
        return value.as_plain()
    if isinstance(value, tuple):
        return [_public_thawed(item) for item in value]
    return value


def public_mapping(value: Mapping[str, object]) -> FrozenJSONMapping:
    return value if isinstance(value, FrozenJSONMapping) else FrozenJSONMapping(value)


def thaw_public_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return public_mapping(value).as_plain()


def _tuple_from_json(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be an array")
    return tuple(value)


# TheSuperHackers @feature Leex 22/08/2026 Freeze exact quality opt-ins and default replay exclusions. (#0)
@dataclass(frozen=True)
class QualityPolicy:
    quality_floor: Literal["complete", "partial"] = "complete"
    allowed_lifecycle_states: tuple[str, ...] = ("engine_verified",)
    include_issue_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.quality_floor not in ("complete", "partial"):
            raise ValueError("quality_floor must be complete or partial")
        _sorted_unique_strings(self.allowed_lifecycle_states, "allowed_lifecycle_states")
        _sorted_unique_strings(self.include_issue_codes, "include_issue_codes")
        if not self.allowed_lifecycle_states or not set(self.allowed_lifecycle_states) <= _LIFECYCLE_STATES:
            raise ValueError("allowed_lifecycle_states contains an unsupported state")

    @property
    def excluded_issue_codes(self) -> tuple[str, ...]:
        return tuple(code for code in _DEFAULT_EXCLUDED_ISSUES if code not in self.include_issue_codes)

    def includes(self, *, lifecycle_state: str, active_issue_codes: tuple[str, ...]) -> bool:
        _sorted_unique_strings(active_issue_codes, "active_issue_codes")
        return lifecycle_state in self.allowed_lifecycle_states and not (
            set(active_issue_codes) & set(self.excluded_issue_codes)
        )

    def as_canonical(self) -> dict[str, object]:
        return {
            "quality_floor": self.quality_floor,
            "allowed_lifecycle_states": list(self.allowed_lifecycle_states),
            "include_issue_codes": list(self.include_issue_codes),
        }


# TheSuperHackers @feature Leex 22/08/2026 Bind segment filters to canonical public semantic values. (#0)
@dataclass(frozen=True)
class SegmentKey:
    subject_faction: str | None = None
    subject_subfaction: str | None = None
    opponent_faction: str | None = None
    opponent_player_public_id: str | None = None
    map_public_id: str | None = None
    start_position: int | None = None
    replay_version: str | None = None
    replay_patch: str | None = None
    start_inclusive: int | None = None
    end_exclusive: int | None = None
    quality_policy: QualityPolicy = field(default_factory=QualityPolicy)

    def __post_init__(self) -> None:
        for value, label in (
            (self.subject_faction, "subject_faction"),
            (self.subject_subfaction, "subject_subfaction"),
            (self.opponent_faction, "opponent_faction"),
            (self.replay_version, "replay_version"),
            (self.replay_patch, "replay_patch"),
        ):
            _stable_string(value, label)
        for value, label in (
            (self.opponent_player_public_id, "opponent_player_public_id"),
            (self.map_public_id, "map_public_id"),
        ):
            if value is not None:
                _public_id(value, label)
        if self.start_position is not None and (type(self.start_position) is not int or self.start_position < 0):
            raise ValueError("start_position must be a nonnegative integer")
        if (self.start_inclusive is None) != (self.end_exclusive is None):
            raise ValueError("start_inclusive and end_exclusive must both be set")
        if self.start_inclusive is not None:
            if type(self.start_inclusive) is not int or type(self.end_exclusive) is not int:
                raise ValueError("replay start bounds must be integer UTC instants")
            if self.end_exclusive <= self.start_inclusive:
                raise ValueError("end_exclusive must be later than start_inclusive")

    def as_canonical(self) -> dict[str, object]:
        return {
            "schema": "longitudinal-segment-v1",
            "subject_faction": self.subject_faction,
            "subject_subfaction": self.subject_subfaction,
            "opponent_faction": self.opponent_faction,
            "opponent_player_public_id": self.opponent_player_public_id,
            "map_public_id": self.map_public_id,
            "start_position": self.start_position,
            "replay_version": self.replay_version,
            "replay_patch": self.replay_patch,
            "start_inclusive": self.start_inclusive,
            "end_exclusive": self.end_exclusive,
            "quality_policy": self.quality_policy.as_canonical(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Self:
        values = dict(value)
        if values.pop("schema", None) != "longitudinal-segment-v1":
            raise ValueError("unsupported segment schema")
        policy = values.get("quality_policy")
        if not isinstance(policy, Mapping):
            raise TypeError("missing quality policy")
        values["quality_policy"] = QualityPolicy(
            quality_floor=cast(Literal["complete", "partial"], policy.get("quality_floor", "complete")),
            allowed_lifecycle_states=cast(tuple[str, ...], _tuple_from_json(policy.get("allowed_lifecycle_states", ()), "allowed_lifecycle_states")),
            include_issue_codes=cast(tuple[str, ...], _tuple_from_json(policy.get("include_issue_codes", ()), "include_issue_codes")),
        )
        return cls(**values)  # type: ignore[arg-type]


# TheSuperHackers @feature Leex 22/08/2026 Expose only versioned longitudinal controls and configured sample limits. (#0)
@dataclass(frozen=True)
class LongitudinalSettings:
    minimum_sample_size: int
    bootstrap_resamples: int
    confidence_level: float
    bootstrap_algorithm_version: str = "median-bootstrap-v1"
    trend_algorithm_version: str = "theil-sen-bootstrap-v1"
    change_point_algorithm_version: str = "median-difference-bootstrap-v1"
    consistency_algorithm_version: str = "iqr-over-median-v1"
    enabled_metrics: tuple[str, ...] = ()
    enabled_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.minimum_sample_size) is not int or self.minimum_sample_size <= 0:
            raise ValueError("minimum_sample_size must be positive")
        if type(self.bootstrap_resamples) is not int or self.bootstrap_resamples <= 0:
            raise ValueError("bootstrap_resamples must be positive")
        if type(self.confidence_level) is not float or not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be between zero and one")
        for value, label in (
            (self.bootstrap_algorithm_version, "bootstrap_algorithm_version"),
            (self.trend_algorithm_version, "trend_algorithm_version"),
            (self.change_point_algorithm_version, "change_point_algorithm_version"),
            (self.consistency_algorithm_version, "consistency_algorithm_version"),
        ):
            _stable_string(value, label)
        _sorted_unique_strings(self.enabled_metrics, "enabled_metrics")
        _sorted_unique_strings(self.enabled_patterns, "enabled_patterns")

    @classmethod
    def from_minimum_sample_size(cls, minimum_sample_size: int, **values: object) -> Self:
        return cls(minimum_sample_size=minimum_sample_size, **values)  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> Self:
        allowed = {field.name for field in __import__("dataclasses").fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise TypeError(f"unknown longitudinal settings: {', '.join(sorted(unknown))}")
        normalized = dict(values)
        normalized["enabled_metrics"] = _tuple_from_json(normalized.get("enabled_metrics", ()), "enabled_metrics")
        normalized["enabled_patterns"] = _tuple_from_json(normalized.get("enabled_patterns", ()), "enabled_patterns")
        return cls(**normalized)  # type: ignore[arg-type]

    def as_canonical(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LongitudinalDefinitionDTO:
    public_name: str
    result_kind: Literal["metric", "pattern"]
    definition_version: str
    source_feature_names: tuple[str, ...]
    algorithm_kind: str
    value_type: str | None = None
    unit: str | None = None
    scope_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _stable_string(self.public_name, "public_name")
        _stable_string(self.definition_version, "definition_version")
        _stable_string(self.algorithm_kind, "algorithm_kind")
        if self.result_kind not in ("metric", "pattern"):
            raise ValueError("result_kind must be metric or pattern")
        _sorted_unique_strings(self.source_feature_names, "source_feature_names")
        _sorted_unique_strings(self.scope_types, "scope_types")
        _stable_string(self.value_type, "value_type")
        _stable_string(self.unit, "unit")

    def as_canonical(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LongitudinalEvidenceDTO:
    public_id: str
    tier: str
    source_kind: str
    source_key: str
    schema_version: int

    def __post_init__(self) -> None:
        _public_id(self.public_id, "evidence public_id")
        for value, label in ((self.tier, "tier"), (self.source_kind, "source_kind"), (self.source_key, "source_key")):
            _stable_string(value, label)
        if type(self.schema_version) is not int or self.schema_version < 0:
            raise ValueError("schema_version must be nonnegative")


@dataclass(frozen=True)
class LongitudinalQualityIssueDTO:
    public_id: str
    stage: str
    issue_code: str
    severity: str
    resolved: bool
    evidence_public_id: str | None
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        _public_id(self.public_id, "issue public_id")
        if self.evidence_public_id is not None:
            _public_id(self.evidence_public_id, "issue evidence_public_id")
        for value, label in ((self.stage, "stage"), (self.issue_code, "issue_code"), (self.severity, "severity")):
            _stable_string(value, label)
        if type(self.resolved) is not bool:
            raise ValueError("resolved must be boolean")
        object.__setattr__(self, "details", public_mapping(self.details))


@dataclass(frozen=True)
class LongitudinalExclusionDTO:
    replay_public_id: str
    replay_player_public_id: str
    reason: str
    chronology_key: tuple[int | None, str, str]

    def __post_init__(self) -> None:
        _public_id(self.replay_public_id, "excluded replay_public_id")
        _public_id(self.replay_player_public_id, "excluded replay_player_public_id")
        _stable_string(self.reason, "exclusion reason")
        if type(self.chronology_key) is not tuple or len(self.chronology_key) != 3:
            raise ValueError("chronology_key must be explicit")


@dataclass(frozen=True)
class LongitudinalMemberDTO:
    replay_public_id: str
    replay_sha256: str
    replay_player_public_id: str
    feature_set_public_id: str
    evidence_public_id: str
    feature_public_id: str | None = None
    strategy_assessment_public_id: str | None = None
    feature_name: str | None = None
    raw_value: object = None
    unit: str | None = None
    frame_start: int = 0
    frame_end: int = 0
    quality: ResultQuality = "unavailable"
    reason: str | None = None
    replay_start_time: int | None = None
    replay_version: str | None = None
    replay_patch: str | None = None
    subject_faction: str | None = None
    subject_subfaction: str | None = None
    opponent_faction: str | None = None
    opponent_player_public_id: str | None = None
    map_public_id: str | None = None
    start_position: int | None = None
    lifecycle_state: str = "engine_verified"
    active_issue_codes: tuple[str, ...] = ()
    feature_set_extractor_name: str = "unknown"
    feature_set_extractor_version: str = "unknown"
    feature_set_input_digest: str = "0" * 64
    feature_set_settings: Mapping[str, object] = field(default_factory=dict)
    feature_value_type: str = "json"
    feature_scope_type: str = "player"
    feature_scope_key: str = "unknown"
    feature_details: Mapping[str, object] = field(default_factory=dict)
    derived_evidence_source_kind: str = "feature"
    derived_evidence_source_key: str = "unknown"
    derived_evidence_schema_version: int = 0
    direct_evidence: tuple[LongitudinalEvidenceDTO, ...] = ()
    quality_issues: tuple[LongitudinalQualityIssueDTO, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.replay_public_id, "replay_public_id"),
            (self.replay_player_public_id, "replay_player_public_id"),
            (self.feature_set_public_id, "feature_set_public_id"),
            (self.evidence_public_id, "evidence_public_id"),
        ):
            _public_id(value, label)
        for optional_value, label in (
            (self.feature_public_id, "feature_public_id"),
            (self.strategy_assessment_public_id, "strategy_assessment_public_id"),
        ):
            if optional_value is not None:
                _public_id(optional_value, label)
        _digest(self.replay_sha256, "replay_sha256")
        _digest(self.feature_set_input_digest, "feature_set_input_digest")
        object.__setattr__(self, "raw_value", freeze_canonical(self.raw_value))
        if self.frame_start < 0 or self.frame_end < self.frame_start:
            raise ValueError("member frame window must be inclusive")
        _sorted_unique_strings(self.active_issue_codes, "active_issue_codes")
        for value, label in (
            (self.feature_set_extractor_name, "feature_set_extractor_name"),
            (self.feature_set_extractor_version, "feature_set_extractor_version"),
            (self.feature_value_type, "feature_value_type"),
            (self.feature_scope_type, "feature_scope_type"),
            (self.feature_scope_key, "feature_scope_key"),
            (self.derived_evidence_source_kind, "derived_evidence_source_kind"),
            (self.derived_evidence_source_key, "derived_evidence_source_key"),
        ):
            _stable_string(value, label)
        if type(self.derived_evidence_schema_version) is not int or self.derived_evidence_schema_version < 0:
            raise ValueError("derived_evidence_schema_version must be nonnegative")
        direct = tuple(sorted(self.direct_evidence, key=lambda item: (item.source_kind, item.source_key, item.public_id)))
        if len({item.public_id for item in direct}) != len(direct) or len({(item.source_kind, item.source_key) for item in direct}) != len(direct):
            raise ValueError("duplicate source evidence")
        issues = tuple(sorted(self.quality_issues, key=lambda item: (item.issue_code, item.public_id)))
        if len({item.public_id for item in issues}) != len(issues):
            raise ValueError("duplicate quality issue")
        object.__setattr__(self, "direct_evidence", direct)
        object.__setattr__(self, "quality_issues", issues)
        object.__setattr__(self, "feature_set_settings", public_mapping(self.feature_set_settings))
        object.__setattr__(self, "feature_details", public_mapping(self.feature_details))

    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.replay_sha256,
            self.replay_player_public_id,
            self.feature_set_public_id,
            self.derived_evidence_source_key,
            self.evidence_public_id,
        )

    def as_canonical(self) -> dict[str, object]:
        return {
            field.name: (
                thaw_public_mapping(value)
                if isinstance(value, FrozenJSONMapping)
                else [asdict(item) | ({"details": thaw_public_mapping(item.details)} if isinstance(item, LongitudinalQualityIssueDTO) else {}) for item in value]
                if field.name in {"direct_evidence", "quality_issues"}
                else thaw_canonical(value)
                if field.name == "raw_value"
                else value
            )
            for field in __import__("dataclasses").fields(self)
            for value in (getattr(self, field.name),)
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Self:
        values = dict(value)
        values["active_issue_codes"] = _tuple_from_json(values.get("active_issue_codes", ()), "active_issue_codes")
        direct_values = _tuple_from_json(values.get("direct_evidence", ()), "direct_evidence")
        direct_items = []
        for item in direct_values:
            direct = cast(Mapping[str, object], item)
            direct_items.append(
                LongitudinalEvidenceDTO(
                    public_id=cast(str, direct["public_id"]),
                    tier=cast(str, direct["tier"]),
                    source_kind=cast(str, direct["source_kind"]),
                    source_key=cast(str, direct["source_key"]),
                    schema_version=cast(int, direct["schema_version"]),
                )
            )
        values["direct_evidence"] = tuple(direct_items)
        issues = []
        for item in _tuple_from_json(values.get("quality_issues", ()), "quality_issues"):
            issue = dict(cast(Mapping[str, object], item))
            issues.append(
                LongitudinalQualityIssueDTO(
                    public_id=cast(str, issue["public_id"]),
                    stage=cast(str, issue["stage"]),
                    issue_code=cast(str, issue["issue_code"]),
                    severity=cast(str, issue["severity"]),
                    resolved=cast(bool, issue["resolved"]),
                    evidence_public_id=cast(str | None, issue["evidence_public_id"]),
                    details=cast(Mapping[str, object], issue["details"]),
                )
            )
        values["quality_issues"] = tuple(issues)
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class LongitudinalInput:
    player_public_id: str
    identity_revision: int
    identity_cache_token: str
    segment: SegmentKey
    settings: LongitudinalSettings
    members: tuple[LongitudinalMemberDTO, ...]
    metric_names: tuple[str, ...] = ()
    pattern_names: tuple[str, ...] = ()
    definitions: tuple[LongitudinalDefinitionDTO, ...] = ()
    exclusions: tuple[LongitudinalExclusionDTO, ...] = ()

    def __post_init__(self) -> None:
        _public_id(self.player_public_id, "player_public_id")
        if type(self.identity_revision) is not int or self.identity_revision < 0:
            raise ValueError("identity_revision must be nonnegative")
        _digest(self.identity_cache_token, "identity_cache_token")
        _sorted_unique_strings(self.metric_names, "metric_names")
        _sorted_unique_strings(self.pattern_names, "pattern_names")
        if self.metric_names != self.settings.enabled_metrics or self.pattern_names != self.settings.enabled_patterns:
            raise ValueError("requested names must exactly equal enabled definition sets")
        definitions = tuple(sorted(self.definitions, key=lambda item: (item.result_kind, item.public_name)))
        expected = tuple(sorted(("metric", name) for name in self.metric_names) + sorted(("pattern", name) for name in self.pattern_names))
        actual = tuple((item.result_kind, item.public_name) for item in definitions)
        if actual != expected:
            raise ValueError("definitions must exactly cover requested names")
        ordered = tuple(sorted(self.members, key=LongitudinalMemberDTO.sort_key))
        identities = tuple((member.replay_player_public_id, member.feature_public_id, member.strategy_assessment_public_id) for member in ordered)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate logical measurement")
        evidence_identities = tuple((member.derived_evidence_source_kind, member.derived_evidence_source_key) for member in ordered)
        if len(evidence_identities) != len(set(evidence_identities)):
            raise ValueError("duplicate source evidence")
        object.__setattr__(self, "members", ordered)
        object.__setattr__(self, "definitions", definitions)
        object.__setattr__(self, "exclusions", tuple(sorted(self.exclusions, key=lambda item: (item.chronology_key, item.reason))))


def canonical_longitudinal_input(value: LongitudinalInput) -> str:
    return canonical_json(
        {
            "schema": "longitudinal-input-v2",
            "player": {
                "public_id": value.player_public_id,
                "identity_revision": value.identity_revision,
                "identity_cache_token": value.identity_cache_token,
                "schema": "player-identity-cache-v1",
            },
            "segment": value.segment.as_canonical(),
            "settings": value.settings.as_canonical(),
            "requested": {"metric_names": list(value.metric_names), "pattern_names": list(value.pattern_names)},
            "definitions": [item.as_canonical() for item in value.definitions],
            "members": [member.as_canonical() for member in value.members],
            "exclusions": [asdict(item) for item in value.exclusions],
        }
    )


def longitudinal_input_digest(value: LongitudinalInput) -> str:
    return hashlib.sha256(canonical_longitudinal_input(value).encode("utf-8")).hexdigest()


def chronological_sort_key(start_time: int | None, replay_sha256: str, replay_player_public_id: str) -> tuple[int, str, str]:
    if start_time is None:
        raise ValueError("missing_replay_start_time")
    if type(start_time) is not int:
        raise ValueError("replay start time must be an integer UTC instant")
    _digest(replay_sha256, "replay_sha256")
    _public_id(replay_player_public_id, "replay_player_public_id")
    return (start_time, replay_sha256, replay_player_public_id)


@dataclass(frozen=True)
class LongitudinalResultDTO:
    public_id: str
    result_name: str
    result_kind: str
    sample_count: int
    missing_count: int
    quality: ResultQuality
    reason: str | None
    statistics: Mapping[str, object]
    members: tuple[LongitudinalMemberDTO, ...]
    evidence_public_id: str

    def __post_init__(self) -> None:
        _public_id(self.public_id, "result public_id")
        _public_id(self.evidence_public_id, "result evidence_public_id")
        _stable_string(self.result_name, "result_name")
        if self.result_kind not in ("metric", "pattern"):
            raise ValueError("result_kind must be metric or pattern")
        if type(self.sample_count) is not int or self.sample_count < 0 or type(self.missing_count) is not int or self.missing_count < 0:
            raise ValueError("result counts must be nonnegative integers")
        if self.quality not in ("complete", "partial", "unavailable"):
            raise ValueError("unsupported result quality")
        if self.quality == "unavailable" and not self.reason:
            raise ValueError("unavailable result requires reason")
        ordered = tuple(sorted(self.members, key=LongitudinalMemberDTO.sort_key))
        if len({member.evidence_public_id for member in ordered}) != len(ordered):
            raise ValueError("duplicate pattern members")
        object.__setattr__(self, "members", ordered)
        object.__setattr__(self, "statistics", public_mapping(self.statistics))


@dataclass(frozen=True)
class LongitudinalRunReceipt:
    run_id: str
    player_public_id: str
    identity_revision: int
    segment: SegmentKey
    settings: LongitudinalSettings
    input_digest: str
    cache_key: str
    status: RunStatus
    results: tuple[LongitudinalResultDTO, ...]
    error: Mapping[str, object] | None = None


@dataclass(frozen=True)
class LongitudinalUnavailable:
    name: str
    reason: str
    sample_count: int = 0
    missing_count: int = 0


@dataclass(frozen=True)
class LongitudinalError:
    code: str
    message: str


@dataclass(frozen=True)
class LongitudinalRequest:
    player_public_id: str
    segment: SegmentKey
    metric_names: tuple[str, ...]
    pattern_names: tuple[str, ...]
    settings: LongitudinalSettings

    def __post_init__(self) -> None:
        _public_id(self.player_public_id, "player_public_id")
        _sorted_unique_strings(self.metric_names, "metric_names")
        _sorted_unique_strings(self.pattern_names, "pattern_names")
        if not self.metric_names and not self.pattern_names:
            raise ValueError("at least one metric or pattern is required")
        if self.metric_names != self.settings.enabled_metrics or self.pattern_names != self.settings.enabled_patterns:
            raise ValueError("requested names must exactly equal enabled definition sets")
