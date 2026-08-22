"""Frozen public longitudinal segmentation and result DTOs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal, Self, TypeAlias
from uuid import UUID

from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.evidence import freeze_canonical

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
        return cls(**dict(values))  # type: ignore[arg-type]

    def as_canonical(self) -> dict[str, object]:
        return asdict(self)


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
        object.__setattr__(self, "raw_value", freeze_canonical(self.raw_value))
        if self.frame_start < 0 or self.frame_end < self.frame_start:
            raise ValueError("member frame window must be inclusive")
        _sorted_unique_strings(self.active_issue_codes, "active_issue_codes")

    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.replay_sha256,
            self.replay_player_public_id,
            self.feature_set_public_id,
            self.feature_public_id or self.strategy_assessment_public_id or "",
            self.evidence_public_id,
        )

    def as_canonical(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LongitudinalInput:
    player_public_id: str
    identity_revision: int
    identity_cache_token: str
    segment: SegmentKey
    settings: LongitudinalSettings
    members: tuple[LongitudinalMemberDTO, ...]

    def __post_init__(self) -> None:
        _public_id(self.player_public_id, "player_public_id")
        if type(self.identity_revision) is not int or self.identity_revision < 0:
            raise ValueError("identity_revision must be nonnegative")
        _digest(self.identity_cache_token, "identity_cache_token")
        ordered = tuple(sorted(self.members, key=LongitudinalMemberDTO.sort_key))
        identities = tuple((member.replay_player_public_id, member.feature_public_id, member.strategy_assessment_public_id) for member in ordered)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate logical measurement")
        object.__setattr__(self, "members", ordered)


def canonical_longitudinal_input(value: LongitudinalInput) -> str:
    return canonical_json(
        {
            "schema": "longitudinal-input-v1",
            "player": {
                "public_id": value.player_public_id,
                "identity_revision": value.identity_revision,
                "identity_cache_token": value.identity_cache_token,
                "schema": "player-identity-cache-v1",
            },
            "segment": value.segment.as_canonical(),
            "settings": value.settings.as_canonical(),
            "members": [member.as_canonical() for member in value.members],
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
