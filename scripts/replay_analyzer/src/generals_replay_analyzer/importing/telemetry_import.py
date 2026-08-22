"""Transactional normalization of fully validated telemetry observations."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TelemetryImportResult:
    run_id: str
    status: str
    event_count: int
    cache_hit: bool


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


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("telemetry dependency field must be an object")
    return cast(Mapping[str, object], value)


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

    def import_replay(self, replay_sha256: str, attempt: TelemetryAttempt) -> TelemetryImportResult:
        sha256 = _require_sha256(replay_sha256, "replay SHA-256")
        run_id = _require_run_id(attempt.run_id)
        now = _utc(self._clock())
        cached = self._existing_run(run_id)
        if cached is not None:
            if cached.status == "succeeded" and self._run_matches(cached, sha256, attempt):
                return TelemetryImportResult(run_id, "succeeded", self._event_count(cached.id), True)
            raise ValueError("telemetry run UUID collides with another immutable attempt")
        replay_id, verified = self._create_attempt(sha256, attempt, now)
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

    def _existing_run(self, run_id: str) -> TelemetryRun | None:
        with self._session_factory() as session:
            return session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))

    def _run_matches(self, run: TelemetryRun, replay_sha256: str, attempt: TelemetryAttempt) -> bool:
        with self._session_factory() as session:
            replay = session.get(Replay, run.replay_id)
            settings = run.settings_json if isinstance(run.settings_json, dict) else {}
            return bool(
                replay is not None
                and replay.sha256 == replay_sha256
                and run.runner_status == attempt.runner_status
                and run.strategy_analysis_scope == attempt.strategy_analysis_scope
                and run.process_exit_code == attempt.process_exit_code
                and run.engine_executable_sha256 == attempt.engine_executable_sha256
                and settings.get("replay_quality") == attempt.replay_quality
                and settings.get("artifact_manifest") == _artifact_manifest(attempt.artifacts)
                and run.diagnostics_json == [dict(diagnostic) for diagnostic in attempt.diagnostics]
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
        self, replay_sha256: str, attempt: TelemetryAttempt, now: datetime
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
                settings_json={
                    "replay_quality": attempt.replay_quality,
                    "artifact_manifest": _artifact_manifest(attempt.artifacts),
                },
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
            records = tuple(cast(dict[str, Any], record.model_dump(mode="json")) for record in bundle.records)
            payloads = tuple(cast(dict[str, Any], record.payload.model_dump(mode="json")) for record in bundle.records)
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
            trace_descriptor = next(item.descriptor for item in verified if item.descriptor.kind == "telemetry_trace")
            trace_asset = session.get(ManagedAsset, run.trace_asset_id)
            if trace_asset is None or trace_asset.sha256 != trace_descriptor.sha256:
                raise ValueError("telemetry trace asset changed before commit")
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
            player_map = self._parser_player_map(session, replay.id)
            for record, payload in zip(normalized.bundle.records, normalized.payloads, strict=True):
                event = event_rows[record.sequence]
                if record.event_type == "entity_sample":
                    self._add_sample(session, run, event, payload, entities)
                elif record.event_type in _PRODUCTION_TYPES:
                    self._add_production(session, replay, run, event, payload, entities, player_map)
                elif record.event_type in _ECONOMY_TYPES:
                    self._add_economy(session, replay, run, event, payload, entities, player_map)
                elif record.event_type in _COMBAT_TYPES:
                    self._add_combat(session, replay, run, event, payload, entities, player_map)
            session.flush()
            complete = normalized.bundle.complete.payload
            if complete.crc_mismatch:
                self._add_issue(session, replay, run, "crc_mismatch", "error", {"final_frame": complete.final_frame}, now)
                if replay.lifecycle_state != "unsupported":
                    replay.lifecycle_state = "desynced"
            elif complete.replay_truncated or complete.terminal_reason == "replay_truncated":
                self._add_issue(
                    session, replay, run, "telemetry_truncated", "warning", {"final_frame": complete.final_frame}, now
                )
                if replay.lifecycle_state not in {"unsupported", "desynced"}:
                    replay.lifecycle_state = "partial"
            elif replay.lifecycle_state not in {"unsupported", "desynced"}:
                replay.lifecycle_state = "engine_verified"
            replay.updated_at = now
            run.status = "succeeded"
            session.flush()

    @staticmethod
    def _parser_player_map(session: Session, replay_id: int) -> dict[int, ReplayPlayer]:
        latest = session.scalar(
            select(ParserRun)
            .where(ParserRun.replay_id == replay_id, ParserRun.status == "succeeded")
            .order_by(ParserRun.id.desc())
        )
        if latest is None:
            return {}
        rows = list(
            session.scalars(
                select(ReplayPlayer).where(
                    ReplayPlayer.parser_run_id == latest.id,
                    ReplayPlayer.player_index.is_not(None),
                )
            )
        )
        return {cast(int, row.player_index): row for row in rows}

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
                layer=cast(str | None, payload.get("layer_name") or payload.get("layer")),
                locomotor_name=cast(str | None, payload.get("current_locomotor_template_name")),
                order_type=cast(str | None, payload.get("current_order_message_name")),
                path_goal_x=goal_x,
                path_goal_y=goal_y,
                path_goal_z=goal_z,
                current_state=cast(str, payload["current_state"]),
                source=cast(str, payload.get("current_state_source") or "telemetry"),
                sample_reason=cast(str, payload.get("sample_reason") or "observed"),
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
        state = payload.get("state")
        if not isinstance(state, str):
            state = event.event_type.rsplit("_", 1)[-1]
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
                quantity=_optional_int(payload.get("quantity")) or 1,
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
        message = str(error).lower()
        if "catalog" in message:
            return "invalid_catalog"
        if "map" in message:
            return "invalid_map_asset"
        if "schema_version" in message or "unsupported major" in message:
            return "version_mismatch"
        if "missing telemetry trace" in message:
            return "missing_telemetry"
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
            if replay.lifecycle_state == "discovered":
                replay.lifecycle_state = "failed"
            replay.updated_at = now

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
        dependencies = {dependency.stage: dependency for dependency in context.dependencies}
        parse_dependency = dependencies.get("parse")
        if parse_dependency is None:
            raise StageFailure("parser_dependency_missing", "parser dependency output is missing", retryable=False)
        if parse_dependency.status == "succeeded":
            parse_output = _succeeded_dependency_output(parse_dependency)
            parser_version = parse_output.get("parser_version")
            if not isinstance(parser_version, str):
                raise StageFailure("parser_dependency_invalid", "parser dependency version is invalid", retryable=False)
            parser_result = self._parser_importer.import_replay(
                context.replay_sha256,
                parser_version=parser_version,
            )
            if parser_result.status != "succeeded":
                raise StageFailure("parser_import_failed", "parser observations failed validation", retryable=False)
        elif parse_dependency.status == "failed":
            parser_version = _failed_parser_version(context)
            code, message, details = _failed_dependency_error(parse_dependency)
            parser_result = self._parser_importer.record_failed_dependency(
                context.replay_sha256,
                parser_version=parser_version,
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
            telemetry_result = self._telemetry_importer.import_replay(context.replay_sha256, attempt)
            if telemetry_result.status != "succeeded":
                raise StageFailure("telemetry_import_failed", "telemetry observations failed validation", retryable=False)
        elif (
            telemetry_dependency is not None
            and telemetry_dependency.status == "failed"
            and telemetry_dependency.error_code != "dependency_failed"
        ):
            _code, _message, details = _failed_dependency_error(telemetry_dependency)
            attempt = _attempt_from_dependency(details)
            telemetry_result = self._telemetry_importer.import_replay(context.replay_sha256, attempt)
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


def _attempt_from_dependency(output: Mapping[str, FrozenJSONValue]) -> TelemetryAttempt:
    raw_artifacts = output.get("artifacts")
    if not isinstance(raw_artifacts, tuple):
        raise StageFailure("telemetry_dependency_invalid", "telemetry artifact manifest is invalid", retryable=False)
    artifacts: list[ManagedTelemetryArtifact] = []
    for raw in raw_artifacts:
        item = _mapping(raw)
        try:
            artifacts.append(
                ManagedTelemetryArtifact(
                    cast(str, item["asset_public_id"]),
                    cast(str, item["kind"]),
                    cast(str, item["logical_path"]),
                    cast(str, item["sha256"]),
                    cast(int, item["size_bytes"]),
                )
            )
        except KeyError as error:
            raise StageFailure(
                "telemetry_dependency_invalid", "telemetry artifact descriptor is incomplete", retryable=False
            ) from error
    raw_diagnostics = output.get("diagnostics")
    diagnostics = tuple(_mapping(item) for item in raw_diagnostics) if isinstance(raw_diagnostics, tuple) else ()
    return TelemetryAttempt(
        run_id=cast(str, output.get("run_id")),
        runner_status=cast(str, output.get("runner_status")),
        replay_quality=cast(str, output.get("replay_quality")),
        strategy_analysis_scope=cast(str, output.get("strategy_analysis_scope")),
        process_exit_code=_optional_int(output.get("exit_code")),
        engine_build=cast(str | None, output.get("engine_build")),
        engine_executable_sha256=cast(str | None, output.get("engine_executable_sha256")),
        diagnostics=diagnostics,
        artifacts=tuple(artifacts),
    )
