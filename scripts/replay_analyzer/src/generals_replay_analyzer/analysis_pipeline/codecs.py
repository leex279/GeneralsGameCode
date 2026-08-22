"""Strict path-free codecs for direct production analysis dependencies."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import unquote
from uuid import UUID

from ..llm.evidence_bundle import EvidenceBundle, EvidenceClaim, build_evidence_bundle
from ..web.ports import iter_diagnostic_candidates

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PipelineCodecError(ValueError):
    """A direct stage output does not match the closed path-free contract."""


def _uuid(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise PipelineCodecError(f"{label} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise PipelineCodecError(f"{label} must be a canonical UUID") from None
    if str(parsed) != value:
        raise PipelineCodecError(f"{label} must be a canonical UUID")
    return value


def _mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(cast(Mapping[object, object], value)) != keys:
        raise PipelineCodecError(f"{label} uses an unknown or missing field")
    _path_free(value)
    return cast(Mapping[str, object], value)


def _array(value: object, label: str) -> tuple[object, ...]:
    if type(value) not in (list, tuple):
        raise PipelineCodecError(f"{label} must be an array")
    return tuple(cast(list[object] | tuple[object, ...], value))


def _path_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in cast(Mapping[object, object], value).items():
            if type(key) is not str or any(marker in key.casefold() for marker in ("path", "endpoint", "url", "host")):
                raise PipelineCodecError("pipeline data contains a forbidden field")
            _path_free(child)
        return
    if type(value) in (list, tuple):
        for child in cast(list[object] | tuple[object, ...], value):
            _path_free(child)
        return
    if type(value) is str and _contains_pathlike(value):
        raise PipelineCodecError("pipeline data contains path-like text")
    if value is not None and type(value) not in (str, bool, int, float):
        raise PipelineCodecError("pipeline data contains a non-JSON value")


def _contains_pathlike(value: str) -> bool:
    current = value
    for _pass in range(4):
        if any(True for _candidate in iter_diagnostic_candidates(current)):
            return True
        decoded = unquote(current)
        if decoded == current:
            return False
        current = decoded
    return True


def _validated_output(value: dict[str, object]) -> dict[str, object]:
    _path_free(value)
    return value


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {cast(str, key): _plain_json(child) for key, child in value.items()}
    if type(value) in (list, tuple):
        return [_plain_json(child) for child in cast(list[object] | tuple[object, ...], value)]
    return value


def _sorted_uuids(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    values = tuple(cast(str, _uuid(item, label)) for item in _array(value, label))
    if values != tuple(sorted(set(values))) or (not values and not allow_empty):
        raise PipelineCodecError(f"{label} must be a sorted unique nonempty array")
    return values


@dataclass(frozen=True, slots=True)
class PlayerFeatureSelection:
    replay_player_public_id: str | None
    canonical_player_public_id: str | None
    feature_set_public_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid(self.replay_player_public_id, "replay_player_public_id", optional=True)
        _uuid(self.canonical_player_public_id, "canonical_player_public_id", optional=True)
        if self.replay_player_public_id is None and self.canonical_player_public_id is not None:
            raise PipelineCodecError("replay-wide feature selection cannot carry canonical player identity")
        if (
            self.feature_set_public_ids != tuple(sorted(set(self.feature_set_public_ids)))
            or not self.feature_set_public_ids
        ):
            raise PipelineCodecError("feature set public IDs must be sorted, unique, and nonempty")
        for public_id in self.feature_set_public_ids:
            _uuid(public_id, "feature_set_public_id")


def encode_feature_output(players: tuple[PlayerFeatureSelection, ...]) -> dict[str, object]:
    ordered = tuple(sorted(players, key=lambda item: item.replay_player_public_id or ""))
    if len({item.replay_player_public_id for item in ordered}) != len(ordered):
        raise PipelineCodecError("duplicate replay player selection")
    return _validated_output(
        {
            "schema_version": "feature-output-v1",
            "players": [
                {
                    "replay_player_public_id": item.replay_player_public_id,
                    "canonical_player_public_id": item.canonical_player_public_id,
                    "feature_set_public_ids": list(item.feature_set_public_ids),
                }
                for item in ordered
            ],
        }
    )


def decode_feature_output(value: object) -> tuple[PlayerFeatureSelection, ...]:
    root = _mapping(value, {"schema_version", "players"}, "feature output")
    if root["schema_version"] != "feature-output-v1":
        raise PipelineCodecError("feature output schema is invalid")
    players: list[PlayerFeatureSelection] = []
    for raw in _array(root["players"], "players"):
        item = _mapping(
            raw,
            {"replay_player_public_id", "canonical_player_public_id", "feature_set_public_ids"},
            "player feature selection",
        )
        players.append(
            PlayerFeatureSelection(
                _uuid(item["replay_player_public_id"], "replay_player_public_id", optional=True),
                _uuid(item["canonical_player_public_id"], "canonical_player_public_id", optional=True),
                _sorted_uuids(item["feature_set_public_ids"], "feature_set_public_ids"),
            )
        )
    ordered = tuple(sorted(players, key=lambda item: item.replay_player_public_id or ""))
    if tuple(players) != ordered or len({item.replay_player_public_id for item in ordered}) != len(ordered):
        raise PipelineCodecError("players must be sorted and unique")
    return ordered


def encode_bundle(bundle: EvidenceBundle) -> dict[str, object]:
    import json

    document = json.loads(bundle.canonical_json)
    if type(document) is not dict:
        raise PipelineCodecError("evidence bundle document must be an object")
    document["digest"] = bundle.digest
    return _validated_output(cast(dict[str, object], document))


def decode_bundle(value: object) -> EvidenceBundle:
    root = _mapping(
        value,
        {"schema_version", "replay_public_id", "replay_sha256", "claims", "unknown_or_missing", "digest"},
        "evidence bundle",
    )
    claims: list[EvidenceClaim] = []
    for raw in _array(root["claims"], "claims"):
        item = _mapping(
            raw,
            {"claim_id", "kind", "value", "quality", "quality_reason", "evidence_ids"},
            "evidence claim",
        )
        evidence_ids = _sorted_uuids(item["evidence_ids"], "evidence_ids")
        try:
            claim = EvidenceClaim._create(
                cast(str, item["claim_id"]),
                cast(Any, item["kind"]),
                _plain_json(item["value"]),
                cast(Any, item["quality"]),
                cast(str | None, item["quality_reason"]),
                evidence_ids,
            )
        except (TypeError, ValueError) as error:
            raise PipelineCodecError("evidence claim is invalid") from error
        claims.append(claim)
    try:
        bundle = build_evidence_bundle(
            replay_public_id=cast(str, root["replay_public_id"]),
            replay_sha256=cast(str, root["replay_sha256"]),
            claims=tuple(claims),
        )
    except (TypeError, ValueError) as error:
        raise PipelineCodecError("evidence bundle is invalid") from error
    if (
        root["schema_version"] != bundle.schema_version
        or tuple(_array(root["unknown_or_missing"], "unknown_or_missing")) != bundle.unknown_or_missing
        or root["digest"] != bundle.digest
        or encode_bundle(bundle) != _plain_json(value)
    ):
        raise PipelineCodecError("evidence bundle canonical identity is invalid")
    return bundle


LongitudinalStatus = Literal["succeeded", "unavailable", "canonical_player_unresolved", "not_applicable"]


@dataclass(frozen=True, slots=True)
class PlayerAssessmentSelection:
    replay_player_public_id: str | None
    canonical_player_public_id: str | None
    feature_set_public_ids: tuple[str, ...]
    strategy_cache_key: str
    longitudinal_status: LongitudinalStatus
    longitudinal_run_id: str | None
    evidence_bundle: EvidenceBundle

    def __post_init__(self) -> None:
        _uuid(self.replay_player_public_id, "replay_player_public_id", optional=True)
        _uuid(self.canonical_player_public_id, "canonical_player_public_id", optional=True)
        if self.replay_player_public_id is None:
            if self.canonical_player_public_id is not None or self.longitudinal_status != "not_applicable":
                raise PipelineCodecError("replay-wide assessment must be exactly not_applicable")
        elif self.canonical_player_public_id is None:
            if self.longitudinal_status != "canonical_player_unresolved":
                raise PipelineCodecError("unresolved canonical player status is required")
        elif self.longitudinal_status not in {"succeeded", "unavailable"}:
            raise PipelineCodecError("resolved canonical player requires an exact longitudinal outcome")
        if (
            self.feature_set_public_ids != tuple(sorted(set(self.feature_set_public_ids)))
            or not self.feature_set_public_ids
        ):
            raise PipelineCodecError("feature set public IDs must be sorted, unique, and nonempty")
        for public_id in self.feature_set_public_ids:
            _uuid(public_id, "feature_set_public_id")
        if type(self.strategy_cache_key) is not str or re.fullmatch(r"[0-9a-f]{64}", self.strategy_cache_key) is None:
            raise PipelineCodecError("strategy cache key is invalid")
        if self.longitudinal_status not in {
            "succeeded",
            "unavailable",
            "canonical_player_unresolved",
            "not_applicable",
        }:
            raise PipelineCodecError("longitudinal status is invalid")
        _uuid(self.longitudinal_run_id, "longitudinal_run_id", optional=True)
        if (self.longitudinal_status == "succeeded") != (self.longitudinal_run_id is not None):
            raise PipelineCodecError("longitudinal run identity is inconsistent")
        if type(self.evidence_bundle) is not EvidenceBundle:
            raise PipelineCodecError("evidence bundle must use the accepted frozen DTO")


def encode_assessment_output(players: tuple[PlayerAssessmentSelection, ...]) -> dict[str, object]:
    ordered = tuple(sorted(players, key=lambda item: item.replay_player_public_id or ""))
    return _validated_output(
        {
            "schema_version": "assessment-output-v1",
            "players": [
                {
                    "replay_player_public_id": item.replay_player_public_id,
                    "canonical_player_public_id": item.canonical_player_public_id,
                    "feature_set_public_ids": list(item.feature_set_public_ids),
                    "strategy_cache_key": item.strategy_cache_key,
                    "longitudinal_status": item.longitudinal_status,
                    "longitudinal_run_id": item.longitudinal_run_id,
                    "evidence_bundle": encode_bundle(item.evidence_bundle),
                }
                for item in ordered
            ],
        }
    )


def decode_assessment_output(value: object) -> tuple[PlayerAssessmentSelection, ...]:
    root = _mapping(value, {"schema_version", "players"}, "assessment output")
    if root["schema_version"] != "assessment-output-v1":
        raise PipelineCodecError("assessment output schema is invalid")
    players: list[PlayerAssessmentSelection] = []
    for raw in _array(root["players"], "players"):
        item = _mapping(
            raw,
            {
                "replay_player_public_id",
                "canonical_player_public_id",
                "feature_set_public_ids",
                "strategy_cache_key",
                "longitudinal_status",
                "longitudinal_run_id",
                "evidence_bundle",
            },
            "player assessment selection",
        )
        cache_key = item["strategy_cache_key"]
        status = item["longitudinal_status"]
        if type(cache_key) is not str or not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise PipelineCodecError("strategy cache key is invalid")
        if status not in {"succeeded", "unavailable", "canonical_player_unresolved", "not_applicable"}:
            raise PipelineCodecError("longitudinal status is invalid")
        run_id = _uuid(item["longitudinal_run_id"], "longitudinal_run_id", optional=True)
        if (status == "succeeded") != (run_id is not None):
            raise PipelineCodecError("longitudinal run identity is inconsistent")
        players.append(
            PlayerAssessmentSelection(
                _uuid(item["replay_player_public_id"], "replay_player_public_id", optional=True),
                _uuid(item["canonical_player_public_id"], "canonical_player_public_id", optional=True),
                _sorted_uuids(item["feature_set_public_ids"], "feature_set_public_ids"),
                cache_key,
                status,
                run_id,
                decode_bundle(item["evidence_bundle"]),
            )
        )
    ordered = tuple(sorted(players, key=lambda item: item.replay_player_public_id or ""))
    if tuple(players) != ordered or len({item.replay_player_public_id for item in ordered}) != len(ordered):
        raise PipelineCodecError("assessment players must be sorted and unique")
    return ordered


@dataclass(frozen=True, slots=True)
class PlayerLLMSelection:
    replay_player_public_id: str | None
    analysis_run_id: str
    llm_status: str
    code: str

    def __post_init__(self) -> None:
        _uuid(self.replay_player_public_id, "replay_player_public_id", optional=True)
        _uuid(self.analysis_run_id, "analysis_run_id")
        if self.llm_status not in {"succeeded", "failed", "invalid", "unavailable"}:
            raise PipelineCodecError("LLM status is invalid")
        if type(self.code) is not str or _SAFE_CODE.fullmatch(self.code) is None:
            raise PipelineCodecError("LLM code is invalid")


def encode_llm_output(players: tuple[PlayerLLMSelection, ...]) -> dict[str, object]:
    ordered = tuple(sorted(players, key=lambda item: item.replay_player_public_id or ""))
    return _validated_output(
        {
            "schema_version": "llm-output-v1",
            "players": [
                {
                    "replay_player_public_id": item.replay_player_public_id,
                    "analysis_run_id": item.analysis_run_id,
                    "llm_status": item.llm_status,
                    "code": item.code,
                }
                for item in ordered
            ],
        }
    )


def decode_llm_output(value: object) -> tuple[PlayerLLMSelection, ...]:
    root = _mapping(value, {"schema_version", "players"}, "LLM output")
    if root["schema_version"] != "llm-output-v1":
        raise PipelineCodecError("LLM output schema is invalid")
    players: list[PlayerLLMSelection] = []
    for raw in _array(root["players"], "players"):
        item = _mapping(raw, {"replay_player_public_id", "analysis_run_id", "llm_status", "code"}, "LLM player")
        status, code = item["llm_status"], item["code"]
        if (
            status not in {"succeeded", "failed", "invalid", "unavailable"}
            or type(code) is not str
            or not _SAFE_CODE.fullmatch(code)
        ):
            raise PipelineCodecError("LLM status is invalid")
        players.append(
            PlayerLLMSelection(
                _uuid(item["replay_player_public_id"], "replay_player_public_id", optional=True),
                cast(str, _uuid(item["analysis_run_id"], "analysis_run_id")),
                status,
                code,
            )
        )
    ordered = tuple(sorted(players, key=lambda item: item.replay_player_public_id or ""))
    if tuple(players) != ordered or len({item.replay_player_public_id for item in ordered}) != len(ordered):
        raise PipelineCodecError("LLM players must be sorted and unique")
    return ordered
