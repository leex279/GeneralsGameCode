"""Transactional normalization of fully validated telemetry observations."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import (
    CombatEvent,
    EconomyEvent,
    Entity,
    EntitySample,
    EvidenceItem,
    ManagedAsset,
    ParserRun,
    ProductionEvent,
    Replay,
    ReplayPlayer,
    ReplayQualityIssue,
    TelemetryEvent,
    TelemetryRun,
)
from ..telemetry import ValidatedTelemetryBundle, load_validated_telemetry_bundle
from .jobs import StageFailure
from .map_import import NormalizedMap, normalize_map_asset, persist_normalized_map
from .parser_import import ParserObservationImporter
from .service import FrozenJSONValue, StageDependencyOutput, StageExecutionContext
from .stages import canonical_json

_SHA256_HEX = frozenset("0123456789abcdef")
_PRODUCTION_TYPES = {
    "production_queued",
    "production_cancelled",
    "production_completed",
    "upgrade_queued",
    "upgrade_cancelled",
    "upgrade_completed",
    "science_purchased",
    "special_power_used",
}
_ECONOMY_TYPES = {"cash_changed", "supply_collected"}
_COMBAT_TYPES = {"damage_applied", "healing_applied"}
_FAILURE_ENVELOPE_TYPE = "telemetry_artifact_failure"
_FAILURE_ENVELOPE_VERSION = 1
_FAILURE_ISSUE_CODES = frozenset(
    {
        "asset_invalid",
        "exporter_failure",
        "invalid_catalog",
        "invalid_map_asset",
        "invalid_trace",
        "missing_telemetry",
        "version_mismatch",
    }
)
_FAILURE_ARTIFACT_KINDS = frozenset(
    {
        "telemetry_catalog",
        "telemetry_map_asset",
        "telemetry_outcome",
        "telemetry_stderr",
        "telemetry_stdout",
        "telemetry_trace",
    }
)


@dataclass(frozen=True)
class ManagedTelemetryArtifact:
    asset_public_id: str
    kind: str
    logical_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class TelemetryAttempt:
    run_id: str
    runner_status: str
    replay_quality: str
    strategy_analysis_scope: str
    process_exit_code: int | None
    engine_build: str | None
    engine_executable_sha256: str | None
    diagnostics: tuple[Mapping[str, object], ...]
    artifacts: tuple[ManagedTelemetryArtifact, ...]
    parser_run_id: str | None = None
    upstream_failure_code: str | None = None
    upstream_quality_issue_code: str | None = None
    upstream_failure_message: str | None = None


@dataclass(frozen=True)
class TelemetryImportResult:
    run_id: str
    status: str
    event_count: int
    cache_hit: bool


# TheSuperHackers @bugfix Leex 22/08/2026 Keep immutable attempt collisions typed through the worker boundary. (#TBD)
class _TelemetryAttemptCollisionError(ValueError):
    """An immutable telemetry run UUID was reused for different attempt evidence."""


class _ClassifiedValidationError(ValueError):
    """Validation failure with an exact persisted quality-issue classification."""

    def __init__(self, issue_code: str, message: str) -> None:
        super().__init__(message)
        self.issue_code = issue_code


@dataclass(frozen=True)
class _VerifiedArtifact:
    descriptor: ManagedTelemetryArtifact
    asset_id: int
    managed_path: Path
    registered_kind: str
    registered_sha256: str
    registered_size_bytes: int


@dataclass(frozen=True)
class _NormalizedTelemetry:
    bundle: ValidatedTelemetryBundle
    map_projection: NormalizedMap | None
    records: tuple[dict[str, Any], ...]
    payloads: tuple[dict[str, Any], ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("telemetry import clock must return an aware datetime")
    return value.astimezone(UTC)


def _require_sha256(value: str, label: str) -> str:
    if len(value) != 64 or value != value.lower() or any(character not in _SHA256_HEX for character in value):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _require_run_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError("telemetry run ID must be a lowercase hyphenated UUID") from error
    if str(parsed) != value:
        raise ValueError("telemetry run ID must be a lowercase hyphenated UUID")
    return value


def _safe_logical_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
    ):
        raise ValueError("telemetry artifact logical path is unsafe")
    return value


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _optional_int(value: object) -> int | None:
    return value if type(value) is int else None


def _optional_float(value: object) -> float | None:
    return float(cast(int | float, value)) if type(value) in {int, float} else None


def _position(payload: Mapping[str, object], key: str = "position") -> tuple[float | None, float | None, float | None]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        return None, None, None
    return _optional_float(value.get("x")), _optional_float(value.get("y")), _optional_float(value.get("z"))


def _artifact_manifest(artifacts: tuple[ManagedTelemetryArtifact, ...]) -> list[dict[str, object]]:
    return [
        {
            "asset_public_id": artifact.asset_public_id,
            "kind": artifact.kind,
            "logical_path": artifact.logical_path,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }
        for artifact in sorted(artifacts, key=lambda item: (item.logical_path.casefold(), item.kind, item.sha256))
    ]


def _attempt_settings(attempt: TelemetryAttempt, idempotency_key: str | None) -> dict[str, object]:
    return {
        "replay_quality": attempt.replay_quality,
        "attempt_engine_build": attempt.engine_build,
        "parser_run_id": attempt.parser_run_id,
        "artifact_manifest": _artifact_manifest(attempt.artifacts),
        "upstream_failure_code": attempt.upstream_failure_code,
        "upstream_quality_issue_code": attempt.upstream_quality_issue_code,
        "upstream_failure_message": attempt.upstream_failure_message,
        "import_observations_idempotency_key": idempotency_key,
    }


_PATHLIKE_TEXT = re.compile(
    r"(?i)(?:file:(?:/{2,3}|\\{2})[^\s\"']*|(?<![a-z0-9])[a-z]:[\\/][^\s\"'>)\]]*|"
    r"\\\\(?:[?.]\\)?[^\s\"'>)\]]*|(?:^|[^a-z0-9/])/(?!/)[^\s\"'>)\]]*)"
)


def _contains_pathlike_text(value: str) -> bool:
    return _PATHLIKE_TEXT.search(value) is not None


# TheSuperHackers @feature Leex 22/08/2026 Normalize validated engine evidence without importing runner or reader internals. (#TBD)
class TelemetryObservationImporter:
    """Validate managed bundle topology, then commit all observed telemetry rows at once."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        data_root: Path,
        *,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root.resolve(strict=False)
        self._clock = clock
        self._uuid_factory = uuid_factory

    def import_replay(
        self,
        replay_sha256: str,
        attempt: TelemetryAttempt,
        *,
        idempotency_key: str | None = None,
    ) -> TelemetryImportResult:
        sha256 = _require_sha256(replay_sha256, "replay SHA-256")
        run_id = _require_run_id(attempt.run_id)
        self._validate_attempt_metadata(attempt)
        if idempotency_key is not None and not idempotency_key:
            raise ValueError("import observation idempotency key must be nonempty")
        now = _utc(self._clock())
        cached = self._existing_run(run_id)
        if cached is not None:
            if cached.status == "succeeded" and self._run_matches(
                cached,
                sha256,
                attempt,
                idempotency_key,
            ):
                return TelemetryImportResult(run_id, "succeeded", self._event_count(cached.id), True)
            if (
                cached.status == "failed"
                and idempotency_key is not None
                and self._failed_attempt_is_reusable(
                    cached,
                    sha256,
                    attempt,
                    idempotency_key,
                )
            ):
                # TheSuperHackers @bugfix Leex 22/08/2026 Reuse one exact childless failed telemetry attempt after lease replay. (#TBD)
                return TelemetryImportResult(run_id, "failed", 0, True)
            raise _TelemetryAttemptCollisionError(
                "telemetry run UUID collides with another immutable attempt"
            )
        replay_id, verified = self._create_attempt(sha256, attempt, now, idempotency_key)
        if attempt.upstream_failure_code is not None:
            issue_code = attempt.upstream_quality_issue_code or "invalid_trace"
            self._commit_failure(replay_id, run_id, attempt, issue_code, now, None)
            return TelemetryImportResult(run_id, "failed", 0, False)
        if attempt.runner_status != "success":
            self._commit_failure(replay_id, run_id, attempt, "exporter_failure", now, None)
            return TelemetryImportResult(run_id, "failed", 0, False)
        try:
            self._validate_registered_artifacts(verified, attempt.artifacts)
            normalized = self._load_normalized_bundle(verified, attempt)
            self._commit_success(replay_id, run_id, attempt, verified, normalized, now)
        except Exception as error:  # noqa: BLE001 - invalid evidence must finish its durable attempt shell.
            issue_code = self._validation_issue(error)
            self._commit_failure(replay_id, run_id, attempt, issue_code, now, error)
            return TelemetryImportResult(run_id, "failed", 0, False)
        return TelemetryImportResult(run_id, "succeeded", len(normalized.records), False)

    @staticmethod
    def _validate_attempt_metadata(attempt: TelemetryAttempt) -> None:
        for label, value in (
            ("runner status", attempt.runner_status),
            ("replay quality", attempt.replay_quality),
            ("strategy analysis scope", attempt.strategy_analysis_scope),
        ):
            if not isinstance(value, str) or not value or _contains_pathlike_text(value):
                raise ValueError(f"telemetry {label} is invalid")
        if attempt.parser_run_id is not None:
            _require_run_id(attempt.parser_run_id)
        if attempt.engine_build is not None and _contains_pathlike_text(attempt.engine_build):
            raise ValueError("telemetry engine build contains path provenance")
        for failure_value in (
            attempt.upstream_failure_code,
            attempt.upstream_quality_issue_code,
            attempt.upstream_failure_message,
        ):
            if failure_value is not None and _contains_pathlike_text(failure_value):
                raise ValueError("telemetry failure evidence contains path provenance")
        for diagnostic in attempt.diagnostics:
            for diagnostic_value in diagnostic.values():
                if isinstance(diagnostic_value, str) and _contains_pathlike_text(diagnostic_value):
                    raise ValueError("telemetry diagnostic contains path provenance")

    def _existing_run(self, run_id: str) -> TelemetryRun | None:
        with self._session_factory() as session:
            return session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))

    def _run_matches(
        self,
        run: TelemetryRun,
        replay_sha256: str,
        attempt: TelemetryAttempt,
        idempotency_key: str | None,
    ) -> bool:
        with self._session_factory() as session:
            replay = session.get(Replay, run.replay_id)
            settings = run.settings_json if isinstance(run.settings_json, dict) else {}
            matches = bool(
                replay is not None
                and replay.sha256 == replay_sha256
                and run.runner_status == attempt.runner_status
                and run.strategy_analysis_scope == attempt.strategy_analysis_scope
                and run.process_exit_code == attempt.process_exit_code
                and run.engine_executable_sha256 == attempt.engine_executable_sha256
                and settings == _attempt_settings(attempt, idempotency_key)
                and run.diagnostics_json == [dict(diagnostic) for diagnostic in attempt.diagnostics]
            )
            if not matches or replay is None:
                return False
            try:
                self._reverify_managed_replay(session, replay, replay_sha256)
                verified = self._reverify_artifacts(session, attempt.artifacts)
            except (OSError, ValueError):
                return False
            return self._run_asset_links_match(run, verified)

    def _failed_attempt_is_reusable(
        self,
        run: TelemetryRun,
        replay_sha256: str,
        attempt: TelemetryAttempt,
        idempotency_key: str,
    ) -> bool:
        if (
            run.status != "failed"
            or run.schema_version != 0
            or run.map_id is not None
            or run.final_frame is not None
            or run.command_count is not None
            or run.completed_at is None
        ):
            return False
        with self._session_factory() as session:
            replay = session.get(Replay, run.replay_id)
            settings = run.settings_json if isinstance(run.settings_json, dict) else {}
            if not (
                replay is not None
                and replay.sha256 == replay_sha256
                and run.runner_status == attempt.runner_status
                and run.strategy_analysis_scope == attempt.strategy_analysis_scope
                and run.process_exit_code == attempt.process_exit_code
                and run.engine_build == (attempt.engine_build or "unavailable")
                and run.engine_executable_sha256 == attempt.engine_executable_sha256
                and settings == _attempt_settings(attempt, idempotency_key)
            ):
                return False
            assert replay is not None
            try:
                self._reverify_managed_replay(session, replay, replay_sha256)
                verified = self._reverify_artifacts(session, attempt.artifacts)
            except (OSError, ValueError):
                return False
            if not self._run_asset_links_match(run, verified):
                return False
            expected_diagnostics = [dict(diagnostic) for diagnostic in attempt.diagnostics]
            stored_diagnostics = run.diagnostics_json
            exception_type: str | None = None
            if stored_diagnostics == expected_diagnostics:
                issue_code = attempt.upstream_quality_issue_code or "exporter_failure"
            elif (
                isinstance(stored_diagnostics, list)
                and stored_diagnostics[: len(expected_diagnostics)] == expected_diagnostics
                and len(stored_diagnostics) == len(expected_diagnostics) + 1
                and isinstance(stored_diagnostics[-1], dict)
                and set(stored_diagnostics[-1]) == {"code", "exception_type"}
                and isinstance(stored_diagnostics[-1].get("code"), str)
                and stored_diagnostics[-1]["code"] in _FAILURE_ISSUE_CODES
                and isinstance(stored_diagnostics[-1].get("exception_type"), str)
                and stored_diagnostics[-1]["exception_type"]
            ):
                issue_code = cast(str, stored_diagnostics[-1]["code"])
                exception_type = cast(str, stored_diagnostics[-1]["exception_type"])
            else:
                return False
            child_count = sum(
                int(session.scalar(statement) or 0)
                for statement in (
                    select(func.count()).select_from(TelemetryEvent).where(TelemetryEvent.telemetry_run_id == run.id),
                    select(func.count()).select_from(Entity).where(Entity.telemetry_run_id == run.id),
                    select(func.count()).select_from(EntitySample).where(EntitySample.telemetry_run_id == run.id),
                    select(func.count()).select_from(ProductionEvent).where(ProductionEvent.telemetry_run_id == run.id),
                    select(func.count()).select_from(EconomyEvent).where(EconomyEvent.telemetry_run_id == run.id),
                    select(func.count()).select_from(CombatEvent).where(CombatEvent.telemetry_run_id == run.id),
                    select(func.count()).select_from(EvidenceItem).where(EvidenceItem.telemetry_run_id == run.id),
                )
            )
            if child_count != 0:
                return False
            issues = list(
                session.scalars(
                    select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == run.id)
                )
            )
            expected_issues = [
                (
                    issue_code,
                    "error",
                    {
                        "runner_status": attempt.runner_status,
                        "exception_type": exception_type,
                    },
                )
            ]
            if attempt.runner_status != "success" and issue_code != "exporter_failure":
                expected_issues.append(
                    (
                        "exporter_failure",
                        "error",
                        {"runner_status": attempt.runner_status},
                    )
                )
            actual_issues = [
                (
                    issue.issue_code,
                    issue.severity,
                    issue.details_json,
                )
                for issue in issues
                if issue.stage == "import_observations" and issue.resolved_at is None
            ]
            return len(actual_issues) == len(issues) and sorted(
                canonical_json(item) for item in actual_issues
            ) == sorted(canonical_json(item) for item in expected_issues)

    def _reverify_managed_replay(self, session: Session, replay: Replay, expected_sha256: str) -> None:
        if replay.sha256 != expected_sha256 or replay.managed_asset_id is None:
            raise ValueError("managed replay identity changed before telemetry commit")
        asset = session.get(ManagedAsset, replay.managed_asset_id)
        if asset is None or asset.kind != "replay" or asset.sha256 != expected_sha256:
            raise ValueError("managed replay registration changed before telemetry commit")
        path = self._data_root / Path(*asset.relative_path.split("/"))
        resolved = path.resolve(strict=True)
        if resolved != path or self._data_root not in resolved.parents:
            raise ValueError("managed replay path changed before telemetry commit")
        sha256, size = _file_sha256(resolved)
        if sha256 != expected_sha256 or size != asset.size_bytes:
            raise ValueError("managed replay bytes changed before telemetry commit")

    def _reverify_artifacts(
        self,
        session: Session,
        descriptors: tuple[ManagedTelemetryArtifact, ...],
    ) -> tuple[_VerifiedArtifact, ...]:
        verified: list[_VerifiedArtifact] = []
        for descriptor in sorted(descriptors, key=lambda item: (item.logical_path.casefold(), item.kind, item.sha256)):
            # TheSuperHackers @bugfix Leex 22/08/2026 Permit exact same-role content reuse across distinct map members. (#TBD)
            _safe_logical_path(descriptor.logical_path)
            asset = session.scalar(select(ManagedAsset).where(ManagedAsset.public_id == descriptor.asset_public_id))
            if asset is None:
                raise ValueError("telemetry managed descriptor registration is missing")
            if (
                asset.kind != descriptor.kind
                or asset.sha256 != descriptor.sha256
                or asset.size_bytes != descriptor.size_bytes
            ):
                raise ValueError("telemetry managed descriptor registration changed")
            path = self._data_root / Path(*asset.relative_path.split("/"))
            try:
                info = path.lstat()
                resolved = path.resolve(strict=True)
            except OSError as error:
                raise ValueError("telemetry managed descriptor is unreadable") from error
            if (
                not stat.S_ISREG(info.st_mode)
                or path.is_symlink()
                or _is_reparse(info)
                or resolved != path
                or self._data_root not in resolved.parents
            ):
                raise ValueError("telemetry managed descriptor path is unsafe")
            sha256, size = _file_sha256(resolved)
            if sha256 != descriptor.sha256 or size != descriptor.size_bytes:
                raise ValueError("telemetry managed descriptor bytes changed")
            verified.append(_VerifiedArtifact(descriptor, asset.id, path, asset.kind, asset.sha256, asset.size_bytes))
        return tuple(verified)

    @staticmethod
    def _run_asset_links_match(run: TelemetryRun, verified: tuple[_VerifiedArtifact, ...]) -> bool:
        traces = [item for item in verified if item.descriptor.kind == "telemetry_trace"]
        catalogs = [item for item in verified if item.descriptor.kind == "telemetry_catalog"]
        manifests = [
            item
            for item in verified
            if item.descriptor.kind == "telemetry_map_asset" and item.descriptor.logical_path.endswith("/manifest.json")
        ]
        expected_trace_id = traces[0].asset_id if len(traces) == 1 else None
        expected_catalog_id = catalogs[0].asset_id if len(catalogs) == 1 else None
        expected_map_id = manifests[0].asset_id if len(manifests) == 1 else None
        return (
            run.trace_asset_id == expected_trace_id
            and run.catalog_asset_id == expected_catalog_id
            and run.map_asset_id == expected_map_id
            and (
                run.status != "failed"
                or run.trace_sha256 == (traces[0].descriptor.sha256 if len(traces) == 1 else None)
            )
        )

    def _event_count(self, telemetry_run_id: int) -> int:
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == telemetry_run_id)
                )
                or 0
            )

    def _create_attempt(
        self,
        replay_sha256: str,
        attempt: TelemetryAttempt,
        now: datetime,
        idempotency_key: str | None,
    ) -> tuple[int, tuple[_VerifiedArtifact, ...]]:
        descriptors = tuple(sorted(attempt.artifacts, key=lambda item: (item.logical_path.casefold(), item.kind, item.sha256)))
        logical_keys = [artifact.logical_path.casefold() for artifact in descriptors]
        if len(logical_keys) != len(set(logical_keys)):
            raise ValueError("telemetry artifact logical paths are duplicated")
        for artifact in descriptors:
            _safe_logical_path(artifact.logical_path)
            _require_sha256(artifact.sha256, "artifact SHA-256")
            if artifact.size_bytes < 0:
                raise ValueError("telemetry artifact size is negative")
        if attempt.engine_executable_sha256 is not None:
            _require_sha256(attempt.engine_executable_sha256, "engine executable SHA-256")
        if attempt.process_exit_code is not None and attempt.process_exit_code < 0:
            raise ValueError("telemetry process exit code is negative")
        with self._session_factory.begin() as session:
            replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
            if replay is None:
                raise ValueError("replay identity is unavailable")
            verified: list[_VerifiedArtifact] = []
            for descriptor in descriptors:
                asset = session.scalar(select(ManagedAsset).where(ManagedAsset.public_id == descriptor.asset_public_id))
                if asset is None:
                    continue
                path = self._data_root / Path(*asset.relative_path.split("/"))
                verified.append(
                    _VerifiedArtifact(descriptor, asset.id, path, asset.kind, asset.sha256, asset.size_bytes)
                )
            if attempt.upstream_failure_code is not None:
                # TheSuperHackers @bugfix Leex 22/08/2026 Link only registered, byte-verified retained failure assets. (#TBD)
                self._validate_retained_failure_artifacts(tuple(verified), descriptors)
            trace = [artifact for artifact in verified if artifact.descriptor.kind == "telemetry_trace"]
            catalog = [artifact for artifact in verified if artifact.descriptor.kind == "telemetry_catalog"]
            manifests = [
                artifact
                for artifact in verified
                if artifact.descriptor.kind == "telemetry_map_asset"
                and artifact.descriptor.logical_path.endswith("/manifest.json")
            ]
            run = TelemetryRun(
                run_id=attempt.run_id,
                replay_id=replay.id,
                trace_asset_id=trace[0].asset_id if len(trace) == 1 else None,
                catalog_asset_id=catalog[0].asset_id if len(catalog) == 1 else None,
                map_asset_id=manifests[0].asset_id if len(manifests) == 1 else None,
                map_id=None,
                schema_version=0,
                engine_build=attempt.engine_build or "unavailable",
                engine_executable_sha256=attempt.engine_executable_sha256,
                settings_json=_attempt_settings(attempt, idempotency_key),
                status="pending",
                runner_status=attempt.runner_status,
                strategy_analysis_scope=attempt.strategy_analysis_scope,
                process_exit_code=attempt.process_exit_code,
                final_frame=None,
                command_count=None,
                trace_sha256=trace[0].descriptor.sha256 if len(trace) == 1 else None,
                diagnostics_json=[dict(diagnostic) for diagnostic in attempt.diagnostics],
                started_at=now,
                completed_at=None,
            )
            session.add(run)
            session.flush()
            run.status = "running"
            return replay.id, tuple(verified)

    def _validate_retained_failure_artifacts(
        self,
        verified: tuple[_VerifiedArtifact, ...],
        descriptors: tuple[ManagedTelemetryArtifact, ...],
    ) -> None:
        if len(verified) != len(descriptors):
            raise ValueError("telemetry retained managed asset is unregistered")
        for artifact in verified:
            if (
                artifact.registered_kind != artifact.descriptor.kind
                or artifact.registered_sha256 != artifact.descriptor.sha256
                or artifact.registered_size_bytes != artifact.descriptor.size_bytes
            ):
                raise ValueError("telemetry retained asset descriptor disagrees with registration")
            source = artifact.managed_path
            try:
                info = source.lstat()
            except OSError as error:
                raise ValueError("retained telemetry asset is unreadable") from error
            if not stat.S_ISREG(info.st_mode) or source.is_symlink() or _is_reparse(info):
                raise ValueError("retained telemetry asset is not an ordinary file")
            resolved = source.resolve(strict=True)
            if self._data_root not in resolved.parents:
                raise ValueError("retained telemetry asset escapes the product data root")
            actual_sha256, actual_size = _file_sha256(resolved)
            if (actual_sha256, actual_size) != (
                artifact.descriptor.sha256,
                artifact.descriptor.size_bytes,
            ):
                raise ValueError("retained telemetry asset content identity is invalid")

    @staticmethod
    def _validate_registered_artifacts(
        verified: tuple[_VerifiedArtifact, ...], descriptors: tuple[ManagedTelemetryArtifact, ...]
    ) -> None:
        if len(verified) != len(descriptors):
            raise ValueError("telemetry managed asset is unregistered")
        for artifact in verified:
            if (
                artifact.registered_kind != artifact.descriptor.kind
                or artifact.registered_sha256 != artifact.descriptor.sha256
                or artifact.registered_size_bytes != artifact.descriptor.size_bytes
            ):
                raise ValueError("telemetry managed asset descriptor disagrees with registration")
        if sum(artifact.descriptor.kind == "telemetry_trace" for artifact in verified) != 1:
            raise ValueError("successful telemetry attempt requires exactly one trace asset")

    def _load_normalized_bundle(
        self, verified: tuple[_VerifiedArtifact, ...], attempt: TelemetryAttempt
    ) -> _NormalizedTelemetry:
        with tempfile.TemporaryDirectory(prefix="replay-analyzer-telemetry-") as temporary:
            root = Path(temporary)
            trace_path: Path | None = None
            for artifact in verified:
                source = artifact.managed_path
                try:
                    info = source.lstat()
                except OSError as error:
                    raise ValueError("managed telemetry asset is unreadable") from error
                if not stat.S_ISREG(info.st_mode) or source.is_symlink() or _is_reparse(info):
                    raise ValueError("managed telemetry asset is not an ordinary file")
                resolved = source.resolve(strict=True)
                if self._data_root not in resolved.parents:
                    raise ValueError("managed telemetry asset escapes the product data root")
                actual_sha256, actual_size = _file_sha256(resolved)
                if (actual_sha256, actual_size) != (artifact.descriptor.sha256, artifact.descriptor.size_bytes):
                    raise ValueError("managed telemetry asset content identity is invalid")
                destination = root / Path(*artifact.descriptor.logical_path.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(resolved, destination)
                if artifact.descriptor.kind == "telemetry_trace":
                    trace_path = destination
            if trace_path is None:
                raise ValueError("missing telemetry trace")
            bundle = load_validated_telemetry_bundle(trace_path)
            if str(bundle.manifest.run_id) != attempt.run_id:
                raise ValueError("telemetry run ID differs from the selected artifact metadata")
            if attempt.engine_build is not None and bundle.manifest.payload.engine_build != attempt.engine_build:
                raise ValueError("telemetry engine build differs from the selected runner metadata")
            self._validate_bundle_topology(root, bundle, verified)
            map_projection = normalize_map_asset(bundle.map_asset) if bundle.map_asset is not None else None
            records = tuple(record.model_dump(mode="json") for record in bundle.records)
            payloads = tuple(record.payload.model_dump(mode="json") for record in bundle.records)
            return _NormalizedTelemetry(bundle, map_projection, records, payloads)

    @staticmethod
    def _validate_bundle_topology(
        root: Path, bundle: ValidatedTelemetryBundle, verified: tuple[_VerifiedArtifact, ...]
    ) -> None:
        def logical(path: Path) -> str:
            return path.relative_to(root).as_posix()

        registered = {
            kind: {artifact.descriptor.logical_path for artifact in verified if artifact.descriptor.kind == kind}
            for kind in ("telemetry_trace", "telemetry_catalog", "telemetry_map_asset")
        }
        if registered["telemetry_trace"] != {logical(bundle.trace_path)}:
            raise ValueError("validated trace path differs from the registered telemetry trace")
        catalog_paths = set() if bundle.catalog_path is None else {logical(bundle.catalog_path)}
        if registered["telemetry_catalog"] != catalog_paths:
            raise ValueError("validated catalog path set differs from registered telemetry catalog assets")
        map_paths = {logical(path) for path in bundle.map_member_paths}
        if registered["telemetry_map_asset"] != map_paths:
            raise ValueError("validated map member set differs from registered telemetry map assets")

    def _commit_success(
        self,
        replay_id: int,
        run_id: str,
        attempt: TelemetryAttempt,
        verified: tuple[_VerifiedArtifact, ...],
        normalized: _NormalizedTelemetry,
        now: datetime,
    ) -> None:
        with self._session_factory.begin() as session:
            replay = session.get(Replay, replay_id)
            run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
            if replay is None or run is None or run.status != "running":
                raise ValueError("telemetry attempt identity changed")
            settings = run.settings_json if isinstance(run.settings_json, dict) else {}
            idempotency_key = cast(str | None, settings.get("import_observations_idempotency_key"))
            if (
                run.replay_id != replay.id
                or run.runner_status != attempt.runner_status
                or run.strategy_analysis_scope != attempt.strategy_analysis_scope
                or run.process_exit_code != attempt.process_exit_code
                or run.engine_build != (attempt.engine_build or "unavailable")
                or run.engine_executable_sha256 != attempt.engine_executable_sha256
                or run.settings_json != _attempt_settings(attempt, idempotency_key)
                or run.diagnostics_json != [dict(diagnostic) for diagnostic in attempt.diagnostics]
            ):
                raise ValueError("telemetry immutable attempt facts changed before commit")
            self._reverify_managed_replay(session, replay, replay.sha256)
            fresh_verified = self._reverify_artifacts(session, attempt.artifacts)
            if fresh_verified != verified or not self._run_asset_links_match(run, fresh_verified):
                raise ValueError("telemetry managed descriptor identity changed before commit")
            map_row = None
            if normalized.map_projection is not None:
                if run.map_asset_id is None:
                    raise ValueError("validated v2 map has no registered manifest asset")
                map_row = persist_normalized_map(session, normalized.map_projection, run.map_asset_id, self._uuid_factory)
                run.map_id = map_row.id
                replay.map_id = map_row.id
            run.schema_version = normalized.bundle.manifest.schema_version
            run.engine_build = normalized.bundle.manifest.payload.engine_build
            run.final_frame = normalized.bundle.complete.payload.final_frame
            run.command_count = normalized.bundle.complete.payload.command_count
            run.trace_sha256 = normalized.bundle.complete.payload.trace_sha256
            run.completed_at = now
            session.flush()

            event_rows: dict[int, TelemetryEvent] = {}
            for record, raw_record, payload in zip(
                normalized.bundle.records, normalized.records, normalized.payloads, strict=True
            ):
                evidence = EvidenceItem(
                    public_id=str(self._uuid_factory()),
                    replay_id=replay.id,
                    parser_run_id=None,
                    telemetry_run_id=run.id,
                    tier="observed",
                    source_kind="telemetry_event",
                    source_key=f"telemetry:{run.run_id}:sequence:{record.sequence}",
                    schema_version=record.schema_version,
                    created_at=now,
                )
                session.add(evidence)
                session.flush()
                event = TelemetryEvent(
                    telemetry_run_id=run.id,
                    sequence=record.sequence,
                    frame=record.frame,
                    logic_time_seconds=record.logic_time_seconds,
                    schema_version=record.schema_version,
                    event_type=record.event_type,
                    payload_json=payload,
                    raw_record_json=raw_record,
                    evidence_item_id=evidence.id,
                )
                session.add(event)
                session.flush()
                event_rows[record.sequence] = event

            entities: dict[int, Entity] = {}
            for record, payload in zip(normalized.bundle.records, normalized.payloads, strict=True):
                if record.event_type != "object_created":
                    continue
                object_id = cast(int, payload["object_id"])
                if object_id in entities:
                    raise ValueError("duplicate object_created identity")
                entity = Entity(
                    public_id=str(self._uuid_factory()),
                    telemetry_run_id=run.id,
                    replay_id=replay.id,
                    object_id=object_id,
                    template_name=cast(str, payload["template_name"]),
                    initial_owner_player_index=_optional_int(payload.get("owner_player_index")),
                    initial_team_id=_optional_int(payload.get("team_id")),
                    kind_of_flags_json=list(payload.get("kind_of_flags") or []),
                    creation_sequence=record.sequence,
                    creation_frame=record.frame,
                    destruction_sequence=None,
                    destruction_frame=None,
                    observed_json=payload,
                )
                session.add(entity)
                entities[object_id] = entity
            session.flush()
            player_map = self._parser_player_map(session, replay.id, attempt.parser_run_id, normalized.payloads)
            unavailable: list[dict[str, object]] = []
            for record, payload in zip(normalized.bundle.records, normalized.payloads, strict=True):
                event = event_rows[record.sequence]
                if record.event_type == "entity_sample":
                    missing = [
                        field
                        for field in ("current_state_source", "sample_reason")
                        if not isinstance(payload.get(field), str) or not payload.get(field)
                    ]
                    if missing:
                        unavailable.append(
                            {"sequence": record.sequence, "event_type": record.event_type, "missing_fields": missing}
                        )
                    else:
                        self._add_sample(session, run, event, payload, entities)
                elif record.event_type in _PRODUCTION_TYPES:
                    missing = [
                        field
                        for field in ("quantity", "state")
                        if (type(payload.get(field)) is not int if field == "quantity" else not isinstance(payload.get(field), str))
                    ]
                    if missing:
                        unavailable.append(
                            {"sequence": record.sequence, "event_type": record.event_type, "missing_fields": missing}
                        )
                    else:
                        self._add_production(session, replay, run, event, payload, entities, player_map)
                elif record.event_type in _ECONOMY_TYPES:
                    self._add_economy(session, replay, run, event, payload, entities, player_map)
                elif record.event_type in _COMBAT_TYPES:
                    self._add_combat(session, replay, run, event, payload, entities, player_map)
            session.flush()
            if unavailable:
                self._add_issue(
                    session,
                    replay,
                    run,
                    "projection_unavailable",
                    "warning",
                    {"projections": unavailable},
                    now,
                )
            complete = normalized.bundle.complete.payload
            if complete.crc_mismatch:
                self._add_issue(session, replay, run, "crc_mismatch", "error", {"final_frame": complete.final_frame}, now)
            elif complete.replay_truncated or complete.terminal_reason == "replay_truncated":
                self._add_issue(
                    session, replay, run, "telemetry_truncated", "warning", {"final_frame": complete.final_frame}, now
                )
            replay.updated_at = now
            run.status = "succeeded"
            session.flush()
            self._recompute_lifecycle(session, replay)
            session.flush()

    @staticmethod
    def _parser_player_map(
        session: Session,
        replay_id: int,
        parser_run_id: str | None,
        payloads: tuple[dict[str, Any], ...],
    ) -> dict[int, ReplayPlayer]:
        if parser_run_id is None:
            return {}
        selected = session.scalar(
            select(ParserRun).where(
                ParserRun.replay_id == replay_id,
                ParserRun.run_id == parser_run_id,
                ParserRun.status == "succeeded",
            )
        )
        if selected is None:
            raise _ClassifiedValidationError(
                "invalid_trace",
                "selected parser run is unavailable for telemetry player mapping",
            )
        rows = list(
            session.scalars(
                select(ReplayPlayer).where(ReplayPlayer.parser_run_id == selected.id).order_by(ReplayPlayer.slot_index)
            )
        )
        by_slot = {row.slot_index: row for row in rows}
        if len(by_slot) != len(rows):
            raise _ClassifiedValidationError(
                "invalid_trace",
                "selected parser run has ambiguous replay slot evidence",
            )
        player_snapshots = [payload for payload in payloads if payload.get("slots") is not None]
        if not player_snapshots:
            return {}
        if len(player_snapshots) != 1:
            raise _ClassifiedValidationError(
                "invalid_trace",
                "telemetry player initialization evidence is ambiguous",
            )
        slots = player_snapshots[0].get("slots")
        if not isinstance(slots, list):
            raise _ClassifiedValidationError(
                "invalid_trace",
                "telemetry player initialization slots are invalid",
            )
        resolved: dict[int, ReplayPlayer] = {}
        resolved_slots: set[int] = set()
        for raw_slot in slots:
            if not isinstance(raw_slot, Mapping) or raw_slot.get("resolution_status") != "resolved":
                continue
            slot_index = raw_slot.get("slot_index")
            player_index = raw_slot.get("player_index")
            if type(slot_index) is not int or type(player_index) is not int or slot_index not in by_slot:
                raise _ClassifiedValidationError(
                    "invalid_trace",
                    "resolved telemetry player slot has no selected parser observation",
                )
            if slot_index in resolved_slots or player_index in resolved:
                raise _ClassifiedValidationError(
                    "invalid_trace",
                    "telemetry player initialization mapping is ambiguous",
                )
            resolved_slots.add(slot_index)
            resolved[player_index] = by_slot[slot_index]
        return resolved

    @staticmethod
    def _layer(payload: Mapping[str, object]) -> str | None:
        layer_name = payload.get("layer_name")
        if isinstance(layer_name, str):
            return layer_name
        layer = payload.get("layer")
        return str(layer) if type(layer) is int else None

    @staticmethod
    def _recompute_lifecycle(session: Session, replay: Replay) -> None:
        """Project evidence availability with the accepted closed precedence, independent of runner state."""
        candidates = {replay.lifecycle_state}
        issue_codes = set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(
                    ReplayQualityIssue.replay_id == replay.id,
                    ReplayQualityIssue.resolved_at.is_(None),
                )
            )
        )
        if issue_codes & {"parser_unsupported", "version_mismatch"}:
            candidates.add("unsupported")
        if "crc_mismatch" in issue_codes:
            candidates.add("desynced")
        succeeded_telemetry_ids = set(
            session.scalars(
                select(TelemetryRun.id).where(
                    TelemetryRun.replay_id == replay.id,
                    TelemetryRun.status == "succeeded",
                )
            )
        )
        degraded_telemetry_ids = set(
            session.scalars(
                select(ReplayQualityIssue.telemetry_run_id).where(
                    ReplayQualityIssue.replay_id == replay.id,
                    ReplayQualityIssue.telemetry_run_id.is_not(None),
                    ReplayQualityIssue.issue_code.in_({"crc_mismatch", "telemetry_truncated", "version_mismatch"}),
                    ReplayQualityIssue.resolved_at.is_(None),
                )
            )
        )
        if succeeded_telemetry_ids - degraded_telemetry_ids:
            candidates.add("engine_verified")
        if issue_codes & {"parser_truncated", "telemetry_truncated"}:
            candidates.add("partial")
        succeeded_parser = int(
            session.scalar(
                select(func.count(ParserRun.id)).where(
                    ParserRun.replay_id == replay.id,
                    ParserRun.status == "succeeded",
                )
            )
            or 0
        )
        if succeeded_parser:
            candidates.add("parsed")
        failed_attempts = sum(
            int(session.scalar(statement) or 0)
            for statement in (
                select(func.count(ParserRun.id)).where(
                    ParserRun.replay_id == replay.id,
                    ParserRun.status == "failed",
                ),
                select(func.count(TelemetryRun.id)).where(
                    TelemetryRun.replay_id == replay.id,
                    TelemetryRun.status == "failed",
                ),
            )
        )
        if failed_attempts:
            candidates.add("failed")
        precedence = (
            "unsupported",
            "desynced",
            "engine_verified",
            "partial",
            "parsed",
            "failed",
            "discovered",
        )
        replay.lifecycle_state = next(state for state in precedence if state in candidates)

    @staticmethod
    def _entity(entities: Mapping[int, Entity], value: object) -> Entity | None:
        if value is None:
            return None
        if type(value) is not int or value not in entities:
            raise ValueError("telemetry event references an unresolved required entity")
        return entities[value]

    def _add_sample(
        self,
        session: Session,
        run: TelemetryRun,
        event: TelemetryEvent,
        payload: Mapping[str, object],
        entities: Mapping[int, Entity],
    ) -> None:
        entity = self._entity(entities, payload.get("object_id"))
        assert entity is not None
        x, y, z = _position(payload)
        if x is None or y is None or z is None:
            raise ValueError("entity sample has no direct XYZ position")
        goal_x, goal_y, goal_z = _position(payload, "path_goal")
        orientation = _optional_float(payload.get("orientation"))
        if orientation is None:
            raise ValueError("entity sample has no direct orientation")
        session.add(
            EntitySample(
                telemetry_run_id=run.id,
                entity_id=entity.id,
                telemetry_event_id=event.id,
                sequence=event.sequence,
                frame=event.frame,
                x=x,
                y=y,
                z=z,
                orientation=orientation,
                speed=_optional_float(payload.get("speed")),
                layer=self._layer(payload),
                locomotor_name=cast(str | None, payload.get("current_locomotor_template_name")),
                order_type=cast(str | None, payload.get("current_order_message_name")),
                path_goal_x=goal_x,
                path_goal_y=goal_y,
                path_goal_z=goal_z,
                current_state=cast(str, payload["current_state"]),
                source=cast(str, payload["current_state_source"]),
                sample_reason=cast(str, payload["sample_reason"]),
                payload_json=dict(payload),
            )
        )

    def _add_production(
        self,
        session: Session,
        replay: Replay,
        run: TelemetryRun,
        event: TelemetryEvent,
        payload: Mapping[str, object],
        entities: Mapping[int, Entity],
        player_map: Mapping[int, ReplayPlayer],
    ) -> None:
        kind = "production"
        name = payload.get("template_name")
        if event.event_type.startswith("upgrade_"):
            kind, name = "upgrade", payload.get("upgrade_name")
        elif event.event_type == "science_purchased":
            kind, name = "science", payload.get("science_name")
        elif event.event_type == "special_power_used":
            kind, name = "special_power", payload.get("special_power_name")
        if not isinstance(name, str):
            raise TypeError("production event has no direct item name")
        producer_id = payload.get("producer_object_id", payload.get("source_object_id"))
        producer = self._entity(entities, producer_id) if producer_id is not None else None
        player_index = _optional_int(payload.get("player_index"))
        state = cast(str, payload["state"])
        session.add(
            ProductionEvent(
                telemetry_run_id=run.id,
                telemetry_event_id=event.id,
                replay_id=replay.id,
                replay_player_id=player_map[player_index].id
                if player_index is not None and player_index in player_map
                else None,
                producer_entity_id=producer.id if producer is not None else None,
                frame=event.frame,
                event_type=event.event_type,
                item_kind=kind,
                item_name=name,
                production_id=_optional_int(payload.get("production_id")),
                upgrade_id=_optional_int(payload.get("upgrade_queue_id")),
                queue_position=_optional_int(payload.get("queue_position")),
                queued_frame=_optional_int(payload.get("queued_frame")),
                terminal_frame=_optional_int(payload.get("terminal_frame")),
                cost=_optional_float(payload.get("cost", payload.get("purchase_cost_points"))),
                quantity=cast(int, payload["quantity"]),
                state=state,
                payload_json=dict(payload),
            )
        )

    def _add_economy(
        self,
        session: Session,
        replay: Replay,
        run: TelemetryRun,
        event: TelemetryEvent,
        payload: Mapping[str, object],
        entities: Mapping[int, Entity],
        player_map: Mapping[int, ReplayPlayer],
    ) -> None:
        player_index = _optional_int(payload.get("player_index"))
        collector = self._entity(entities, payload.get("collector_object_id")) if payload.get("collector_object_id") is not None else None
        source = self._entity(entities, payload.get("source_object_id")) if payload.get("source_object_id") is not None else None
        dropoff = self._entity(entities, payload.get("dropoff_object_id")) if payload.get("dropoff_object_id") is not None else None
        x, y, z = _position(payload, "location")
        session.add(
            EconomyEvent(
                telemetry_run_id=run.id,
                telemetry_event_id=event.id,
                replay_id=replay.id,
                replay_player_id=player_map[player_index].id
                if player_index is not None and player_index in player_map
                else None,
                collector_entity_id=collector.id if collector is not None else None,
                source_entity_id=source.id if source is not None else None,
                dropoff_entity_id=dropoff.id if dropoff is not None else None,
                frame=event.frame,
                event_type=event.event_type,
                balance_before=_optional_float(payload.get("before")),
                amount_delta=_optional_float(payload.get("delta")),
                balance_after=_optional_float(payload.get("after")),
                amount=_optional_float(payload.get("amount")),
                reason=cast(str | None, payload.get("reason")),
                location_x=x,
                location_y=y,
                location_z=z,
                payload_json=dict(payload),
            )
        )

    def _add_combat(
        self,
        session: Session,
        replay: Replay,
        run: TelemetryRun,
        event: TelemetryEvent,
        payload: Mapping[str, object],
        entities: Mapping[int, Entity],
        player_map: Mapping[int, ReplayPlayer],
    ) -> None:
        victim_id = payload.get("victim_object_id", payload.get("target_object_id"))
        attacker_id = payload.get("attacker_object_id", payload.get("source_object_id"))
        victim = self._entity(entities, victim_id)
        attacker = self._entity(entities, attacker_id) if attacker_id is not None else None
        attacker_player = _optional_int(payload.get("source_player_index"))
        source_player_indices = payload.get("source_player_indices")
        if attacker_player is None and isinstance(source_player_indices, list) and len(source_player_indices) == 1:
            attacker_player = _optional_int(source_player_indices[0])
        victim_player = _optional_int(payload.get("victim_player_index", payload.get("target_player_index")))
        x, y, z = _position(payload, "location")
        session.add(
            CombatEvent(
                telemetry_run_id=run.id,
                telemetry_event_id=event.id,
                replay_id=replay.id,
                frame=event.frame,
                event_type=event.event_type,
                attacker_entity_id=attacker.id if attacker is not None else None,
                victim_entity_id=victim.id if victim is not None else None,
                source_entity_id=attacker.id if attacker is not None else None,
                attacker_replay_player_id=player_map[attacker_player].id
                if attacker_player is not None and attacker_player in player_map
                else None,
                victim_replay_player_id=player_map[victim_player].id
                if victim_player is not None and victim_player in player_map
                else None,
                weapon_name=cast(str | None, payload.get("weapon_name")),
                damage_type=cast(str | None, payload.get("damage_type")),
                death_type=cast(str | None, payload.get("death_type")),
                attempted_amount=_optional_float(payload.get("attempted_amount")),
                calculated_amount=_optional_float(payload.get("calculated_amount")),
                applied_amount=_optional_float(payload.get("applied_amount")),
                health_before=_optional_float(payload.get("prior_health")),
                health_after=_optional_float(payload.get("new_health")),
                killing_blow=cast(bool | None, payload.get("killing_blow")),
                location_x=x,
                location_y=y,
                location_z=z,
                payload_json=dict(payload),
            )
        )

    @staticmethod
    def _validation_issue(error: Exception) -> str:
        if isinstance(error, _ClassifiedValidationError):
            return error.issue_code
        message = str(error).lower()
        if "catalog" in message:
            return "invalid_catalog"
        if re.search(r"\bmap(?:[_ -](?:asset|member|manifest))?\b", message):
            return "invalid_map_asset"
        if "schema_version" in message or "unsupported major" in message:
            return "version_mismatch"
        if "missing telemetry trace" in message:
            return "missing_telemetry"
        if "asset" in message or "descriptor" in message or "registration" in message:
            return "asset_invalid"
        return "invalid_trace"

    def _commit_failure(
        self,
        replay_id: int,
        run_id: str,
        attempt: TelemetryAttempt,
        issue_code: str,
        now: datetime,
        error: Exception | None,
    ) -> None:
        with self._session_factory.begin() as session:
            replay = session.get(Replay, replay_id)
            run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
            if replay is None or run is None:
                raise RuntimeError("telemetry attempt shell disappeared while recording failure")
            diagnostics = [dict(diagnostic) for diagnostic in attempt.diagnostics]
            if error is not None:
                diagnostics.append({"code": issue_code, "exception_type": type(error).__name__})
            run.diagnostics_json = diagnostics
            run.status = "failed"
            run.completed_at = now
            self._add_issue(
                session,
                replay,
                run,
                issue_code,
                "error",
                {"runner_status": attempt.runner_status, "exception_type": type(error).__name__ if error else None},
                now,
            )
            if attempt.runner_status != "success" and issue_code != "exporter_failure":
                self._add_issue(
                    session,
                    replay,
                    run,
                    "exporter_failure",
                    "error",
                    {"runner_status": attempt.runner_status},
                    now,
                )
            replay.updated_at = now
            session.flush()
            self._recompute_lifecycle(session, replay)

    def _add_issue(
        self,
        session: Session,
        replay: Replay,
        run: TelemetryRun,
        code: str,
        severity: str,
        details: dict[str, object],
        now: datetime,
    ) -> None:
        existing = session.scalar(
            select(ReplayQualityIssue).where(
                ReplayQualityIssue.replay_id == replay.id,
                ReplayQualityIssue.telemetry_run_id == run.id,
                ReplayQualityIssue.stage == "import_observations",
                ReplayQualityIssue.issue_code == code,
            )
        )
        if existing is None:
            session.add(
                ReplayQualityIssue(
                    public_id=str(self._uuid_factory()),
                    replay_id=replay.id,
                    parser_run_id=None,
                    telemetry_run_id=run.id,
                    evidence_item_id=None,
                    stage="import_observations",
                    issue_code=code,
                    severity=severity,
                    details_json=details,
                    detected_at=now,
                    resolved_at=None,
                )
            )


class ObservationImportHandler:
    """Public Task 3 stage handler that composes parser and optional telemetry imports."""

    def __init__(
        self,
        parser_importer: ParserObservationImporter,
        telemetry_importer: TelemetryObservationImporter,
    ) -> None:
        self._parser_importer = parser_importer
        self._telemetry_importer = telemetry_importer

    def __call__(self, context: StageExecutionContext) -> Mapping[str, Any]:
        if context.stage != "import_observations" or context.component_version != "1":
            raise StageFailure(
                "dependency_contract_invalid",
                "observation import execution context is invalid",
                retryable=False,
            )
        if not isinstance(context.dependencies, tuple):
            raise StageFailure(
                "dependency_contract_invalid",
                "observation import dependencies are not frozen",
                retryable=False,
            )
        dependencies = _validated_dependencies(context.dependencies)
        parse_dependency = dependencies.get("parse")
        if parse_dependency is None:
            raise StageFailure("parser_dependency_missing", "parser dependency output is missing", retryable=False)
        if parse_dependency.status == "succeeded":
            parse_output = _succeeded_dependency_output(parse_dependency)
            parser_version = _validated_parser_dependency(parse_output, context.replay_sha256)
            parser_result = self._parser_importer.import_replay(
                context.replay_sha256,
                parser_version=parser_version,
                idempotency_key=context.idempotency_key,
            )
            if parser_result.status != "succeeded":
                raise StageFailure("parser_import_failed", "parser observations failed validation", retryable=False)
        elif parse_dependency.status == "failed":
            parser_version = _failed_parser_version(context)
            code, message, details = _failed_dependency_error(parse_dependency)
            parser_result = self._parser_importer.record_failed_dependency(
                context.replay_sha256,
                parser_version=parser_version,
                idempotency_key=context.idempotency_key,
                error_code=code,
                error_message=message,
                error_details=details,
            )
        else:
            raise StageFailure("parser_dependency_invalid", "parser dependency is not terminal", retryable=False)
        telemetry_result: TelemetryImportResult | None = None
        telemetry_dependency = dependencies.get("telemetry")
        if telemetry_dependency is not None and telemetry_dependency.status == "succeeded":
            attempt = _attempt_from_dependency(_succeeded_dependency_output(telemetry_dependency))
            attempt = replace(
                attempt,
                # TheSuperHackers @bugfix Leex 22/08/2026 Never treat a failed parser shell as player-mapping authority. (#TBD)
                parser_run_id=parser_result.run_id if parser_result.status == "succeeded" else None,
            )
            telemetry_result = self._import_telemetry(context, attempt)
            if telemetry_result.status != "succeeded":
                raise StageFailure("telemetry_import_failed", "telemetry observations failed validation", retryable=False)
        elif (
            telemetry_dependency is not None
            and telemetry_dependency.status == "failed"
            and telemetry_dependency.error_code != "dependency_failed"
        ):
            code, _message, details = _failed_dependency_error(telemetry_dependency)
            attempt = _attempt_from_failed_dependency(telemetry_dependency, code, details)
            attempt = replace(
                attempt,
                parser_run_id=parser_result.run_id if parser_result.status == "succeeded" else None,
            )
            telemetry_result = self._import_telemetry(context, attempt)
            if telemetry_result.status != "failed":
                raise StageFailure(
                    "telemetry_dependency_invalid",
                    "failed telemetry dependency produced successful observations",
                    retryable=False,
                )
        return {
            "idempotency_key": context.idempotency_key,
            "parser_run_id": parser_result.run_id,
            "parser_command_count": parser_result.command_count,
            "telemetry_run_id": telemetry_result.run_id if telemetry_result else None,
            "telemetry_event_count": telemetry_result.event_count if telemetry_result else 0,
        }

    def _import_telemetry(
        self,
        context: StageExecutionContext,
        attempt: TelemetryAttempt,
    ) -> TelemetryImportResult:
        try:
            return self._telemetry_importer.import_replay(
                context.replay_sha256,
                attempt,
                idempotency_key=context.idempotency_key,
            )
        except _TelemetryAttemptCollisionError as error:
            raise StageFailure(
                "telemetry_import_collision",
                "telemetry run identity collides with immutable attempt evidence",
                retryable=False,
            ) from error


def _validated_dependencies(
    raw_dependencies: tuple[StageDependencyOutput, ...],
) -> dict[str, StageDependencyOutput]:
    # TheSuperHackers @bugfix Leex 22/08/2026 Reject damaged public dependency snapshots before persistence. (#TBD)
    expected_versions = {"parse": "1", "telemetry": "1"}
    dependencies: dict[str, StageDependencyOutput] = {}
    public_ids: set[str] = set()
    for dependency in raw_dependencies:
        if type(dependency) is not StageDependencyOutput:
            raise StageFailure(
                "dependency_contract_invalid",
                "observation dependency snapshot type is invalid",
                retryable=False,
            )
        expected_version = expected_versions.get(dependency.stage)
        try:
            public_id = _require_run_id(dependency.job_public_id)
        except (TypeError, ValueError) as error:
            raise StageFailure(
                "dependency_contract_invalid",
                "observation dependency public identity is invalid",
                retryable=False,
            ) from error
        if (
            expected_version is None
            or dependency.component_version != expected_version
            or dependency.stage in dependencies
            or public_id in public_ids
        ):
            raise StageFailure(
                "dependency_contract_invalid",
                "observation dependency identity is invalid or duplicated",
                retryable=False,
            )
        if dependency.status == "succeeded":
            valid_terminal_shape = (
                isinstance(dependency.output, Mapping)
                and dependency.error_code is None
                and dependency.error_message is None
                and dependency.error_details is None
            )
        elif dependency.status == "failed":
            valid_terminal_shape = (
                dependency.output is None
                and isinstance(dependency.error_code, str)
                and bool(dependency.error_code)
                and isinstance(dependency.error_message, str)
                and bool(dependency.error_message)
                and isinstance(dependency.error_details, Mapping)
            )
        else:
            valid_terminal_shape = False
        if not valid_terminal_shape:
            raise StageFailure(
                "dependency_contract_invalid",
                "observation dependency terminal evidence is invalid",
                retryable=False,
            )
        dependencies[dependency.stage] = dependency
        public_ids.add(public_id)
    return dependencies


def _succeeded_dependency_output(
    dependency: StageDependencyOutput,
) -> Mapping[str, FrozenJSONValue]:
    if dependency.output is None:
        raise StageFailure(
            f"{dependency.stage}_dependency_invalid",
            f"{dependency.stage} dependency output is missing",
            retryable=False,
        )
    return dependency.output


def _validated_parser_dependency(
    output: Mapping[str, FrozenJSONValue], replay_sha256: str
) -> str:
    expected_keys = {
        "command_count",
        "command_stream_offset",
        "completion_status",
        "content_sha256",
        "end_offset",
        "parser_version",
        "warning_codes",
    }
    if set(output) != expected_keys:
        raise StageFailure(
            "parser_dependency_invalid",
            "parser dependency fields are incomplete",
            retryable=False,
        )
    parser_version = output.get("parser_version")
    warning_codes = output.get("warning_codes")
    if not isinstance(warning_codes, tuple) or any(
        not isinstance(code, str) or not code or _contains_pathlike_text(code) for code in warning_codes
    ):
        raise StageFailure(
            "parser_dependency_invalid",
            "parser dependency evidence is invalid",
            retryable=False,
        )
    canonical_warning_codes = cast(tuple[str, ...], warning_codes)
    if (
        not isinstance(parser_version, str)
        or not parser_version
        or _contains_pathlike_text(parser_version)
        or output.get("content_sha256") != replay_sha256
        or output.get("completion_status") not in {"complete", "truncated"}
        or type(output.get("command_count")) is not int
        or cast(int, output["command_count"]) < 0
        or type(output.get("command_stream_offset")) is not int
        or cast(int, output["command_stream_offset"]) < 0
        or type(output.get("end_offset")) is not int
        or cast(int, output["end_offset"]) < cast(int, output["command_stream_offset"])
        or tuple(sorted(set(canonical_warning_codes))) != canonical_warning_codes
    ):
        raise StageFailure(
            "parser_dependency_invalid",
            "parser dependency evidence is invalid",
            retryable=False,
        )
    return parser_version


def _failed_dependency_error(
    dependency: StageDependencyOutput,
) -> tuple[str, str, Mapping[str, FrozenJSONValue]]:
    if (
        not dependency.error_code
        or not dependency.error_message
        or dependency.error_details is None
    ):
        raise StageFailure(
            f"{dependency.stage}_dependency_invalid",
            f"{dependency.stage} dependency failure evidence is incomplete",
            retryable=False,
        )
    return dependency.error_code, dependency.error_message, dependency.error_details


def _failed_parser_version(context: StageExecutionContext) -> str:
    branch_recipe = context.input.get("branch_recipe")
    if not isinstance(branch_recipe, Mapping):
        raise StageFailure("parser_dependency_invalid", "parser branch recipe is missing", retryable=False)
    parse_recipe = branch_recipe.get("parse")
    if not isinstance(parse_recipe, Mapping):
        raise StageFailure("parser_dependency_invalid", "parser branch recipe is invalid", retryable=False)
    parser_version = parse_recipe.get("parser_version")
    if not isinstance(parser_version, str) or not parser_version:
        raise StageFailure("parser_dependency_invalid", "parser dependency version is invalid", retryable=False)
    return parser_version


def _failure_mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StageFailure("telemetry_dependency_invalid", message, retryable=False)
    return cast(Mapping[str, object], value)


def _typed_failure_attempt(
    raw_attempt: Mapping[str, object],
    *,
    failure_code: str,
    failure_message: str,
    quality_issue_code: str,
) -> TelemetryAttempt:
    expected_attempt_keys = {
        "artifacts",
        "diagnostics",
        "engine_build",
        "engine_executable_sha256",
        "exit_code",
        "replay_quality",
        "run_id",
        "runner_status",
        "strategy_analysis_scope",
    }
    if set(raw_attempt) != expected_attempt_keys:
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure attempt fields are incomplete",
            retryable=False,
        )
    text_values: dict[str, str] = {}
    for key in ("run_id", "runner_status", "replay_quality", "strategy_analysis_scope"):
        value = raw_attempt.get(key)
        if (
            not isinstance(value, str)
            or not value
            or not value.isprintable()
            or _contains_pathlike_text(value)
        ):
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure attempt metadata is invalid",
                retryable=False,
            )
        text_values[key] = value
    try:
        _require_run_id(text_values["run_id"])
    except ValueError as error:
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure run ID is invalid",
            retryable=False,
        ) from error
    raw_exit_code = raw_attempt.get("exit_code")
    if raw_exit_code is not None and (
        not isinstance(raw_exit_code, int)
        or isinstance(raw_exit_code, bool)
        or raw_exit_code < 0
    ):
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure exit code is invalid",
            retryable=False,
        )
    raw_engine_build = raw_attempt.get("engine_build")
    if raw_engine_build is not None and (
        not isinstance(raw_engine_build, str)
        or not raw_engine_build
        or not raw_engine_build.isprintable()
        or _contains_pathlike_text(raw_engine_build)
    ):
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure engine build is invalid",
            retryable=False,
        )
    raw_engine_hash = raw_attempt.get("engine_executable_sha256")
    if raw_engine_hash is not None:
        if not isinstance(raw_engine_hash, str):
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure engine hash is invalid",
                retryable=False,
            )
        try:
            _require_sha256(raw_engine_hash, "engine executable SHA-256")
        except ValueError as error:
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure engine hash is invalid",
                retryable=False,
            ) from error

    raw_artifacts = raw_attempt.get("artifacts")
    if not isinstance(raw_artifacts, tuple):
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure artifact manifest is invalid",
            retryable=False,
        )
    artifacts: list[ManagedTelemetryArtifact] = []
    logical_paths: set[str] = set()
    for raw in raw_artifacts:
        item = _failure_mapping(raw, "telemetry failure artifact descriptor is invalid")
        if set(item) != {"asset_public_id", "kind", "logical_path", "sha256", "size_bytes"}:
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure artifact descriptor fields are invalid",
                retryable=False,
            )
        public_id = item.get("asset_public_id")
        kind = item.get("kind")
        logical_path = item.get("logical_path")
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        if (
            not isinstance(public_id, str)
            or not isinstance(kind, str)
            or kind not in _FAILURE_ARTIFACT_KINDS
            or not isinstance(logical_path, str)
            or not isinstance(sha256, str)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure artifact descriptor types are invalid",
                retryable=False,
            )
        try:
            _require_run_id(public_id)
            _safe_logical_path(logical_path)
            _require_sha256(sha256, "artifact SHA-256")
        except ValueError as error:
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure artifact descriptor values are invalid",
                retryable=False,
            ) from error
        logical_key = logical_path.casefold()
        if logical_key in logical_paths:
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure artifact logical paths are duplicated",
                retryable=False,
            )
        logical_paths.add(logical_key)
        artifacts.append(ManagedTelemetryArtifact(public_id, kind, logical_path, sha256, size_bytes))
    if artifacts != sorted(
        artifacts,
        key=lambda item: (item.logical_path.casefold(), item.kind, item.sha256),
    ):
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure artifact manifest is not canonical",
            retryable=False,
        )

    raw_diagnostics = raw_attempt.get("diagnostics")
    if not isinstance(raw_diagnostics, tuple):
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure diagnostics are invalid",
            retryable=False,
        )
    diagnostics: list[Mapping[str, object]] = []
    for raw in raw_diagnostics:
        diagnostic = _failure_mapping(raw, "telemetry failure diagnostic is invalid")
        if set(diagnostic) != {"code", "message"} or not all(
            isinstance(diagnostic.get(key), str)
            and bool(diagnostic[key])
            and cast(str, diagnostic[key]).isprintable()
            and not _contains_pathlike_text(cast(str, diagnostic[key]))
            for key in ("code", "message")
        ):
            raise StageFailure(
                "telemetry_dependency_invalid",
                "telemetry failure diagnostic is invalid",
                retryable=False,
            )
        diagnostics.append(dict(diagnostic))
    return TelemetryAttempt(
        run_id=text_values["run_id"],
        runner_status=text_values["runner_status"],
        replay_quality=text_values["replay_quality"],
        strategy_analysis_scope=text_values["strategy_analysis_scope"],
        process_exit_code=raw_exit_code,
        engine_build=raw_engine_build,
        engine_executable_sha256=raw_engine_hash,
        diagnostics=tuple(diagnostics),
        artifacts=tuple(artifacts),
        upstream_failure_code=failure_code,
        upstream_quality_issue_code=quality_issue_code,
        upstream_failure_message=failure_message,
    )


def _attempt_from_failed_dependency(
    dependency: StageDependencyOutput,
    failure_code: str,
    details: Mapping[str, FrozenJSONValue],
) -> TelemetryAttempt:
    raw_envelope = details.get("failure_envelope")
    if raw_envelope is None:
        issue_code = "exporter_failure" if failure_code == "exporter_failure" else "invalid_trace"
        return _attempt_from_dependency(
            details,
            upstream_failure_code=failure_code,
            upstream_quality_issue_code=issue_code,
            upstream_failure_message=dependency.error_message,
        )
    envelope = _failure_mapping(raw_envelope, "telemetry failure envelope is invalid")
    expected_envelope_keys = {
        "attempt",
        "failure_code",
        "failure_message",
        "quality_issue_code",
        "type",
        "version",
    }
    if set(envelope) != expected_envelope_keys:
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure envelope fields are invalid",
            retryable=False,
        )
    if envelope.get("type") != _FAILURE_ENVELOPE_TYPE or envelope.get("version") != _FAILURE_ENVELOPE_VERSION:
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure envelope type is invalid",
            retryable=False,
        )
    envelope_failure_code = envelope.get("failure_code")
    failure_message = envelope.get("failure_message")
    quality_issue_code = envelope.get("quality_issue_code")
    if (
        envelope_failure_code != failure_code
        or dependency.error_code != failure_code
        or not isinstance(failure_message, str)
        or not failure_message
        or failure_message != dependency.error_message
        or not failure_message.isprintable()
        or _contains_pathlike_text(failure_message)
        or not isinstance(quality_issue_code, str)
        or quality_issue_code not in _FAILURE_ISSUE_CODES
    ):
        raise StageFailure(
            "telemetry_dependency_invalid",
            "telemetry failure envelope evidence is inconsistent",
            retryable=False,
        )
    raw_attempt = _failure_mapping(
        envelope.get("attempt"),
        "telemetry failure attempt is invalid",
    )
    return _typed_failure_attempt(
        raw_attempt,
        failure_code=failure_code,
        failure_message=failure_message,
        quality_issue_code=quality_issue_code,
    )


def _attempt_from_dependency(
    output: Mapping[str, FrozenJSONValue],
    *,
    upstream_failure_code: str | None = None,
    upstream_quality_issue_code: str | None = None,
    upstream_failure_message: str | None = None,
) -> TelemetryAttempt:
    attempt = _typed_failure_attempt(
        cast(Mapping[str, object], output),
        failure_code=upstream_failure_code or "validated_telemetry",
        failure_message=upstream_failure_message or "validated telemetry dependency",
        quality_issue_code=upstream_quality_issue_code or "invalid_trace",
    )
    return replace(
        attempt,
        upstream_failure_code=upstream_failure_code,
        upstream_quality_issue_code=upstream_quality_issue_code,
        upstream_failure_message=upstream_failure_message,
    )
