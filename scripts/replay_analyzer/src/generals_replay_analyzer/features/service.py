"""Sole database boundary for deterministic feature extraction and caching."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeAlias, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
    Entity,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ManagedAsset,
    Map,
    MapResource,
    ParserRun,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.activity import ActivityExtractor
from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureScope,
    FeatureValue,
    FeatureWindow,
    validate_feature_value,
)
from generals_replay_analyzer.features.build_order import BuildOrderExtractor
from generals_replay_analyzer.features.combat import CombatExtractor
from generals_replay_analyzer.features.context import FeatureContext, cache_key, input_digest
from generals_replay_analyzer.features.economy import EconomyExtractor
from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    EvidenceRef,
    ObservedEvidence,
    freeze_canonical,
    thaw_canonical,
)
from generals_replay_analyzer.features.production import ProductionExtractor
from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureRegistry
from generals_replay_analyzer.telemetry.map_asset import GridSpec, StartPosition, StaticObjectFeature, WorldBounds


class RegisteredExtractor(Protocol):
    name: str
    version: str
    feature_names: tuple[str, ...]

    def extract(self, context: FeatureContext) -> FeatureBundle: ...


# TheSuperHackers @fix Leex 22/08/2026 Make extractor observation reach an explicit validated service policy. (#TBD)
ObservationPolicy: TypeAlias = Literal["target_player", "replay_wide_telemetry"]


class ExtractorObservationPolicy(Protocol):
    observation_policy: ObservationPolicy


class FeatureExtractionError(RuntimeError):
    """Typed feature request, invariant, or persistence failure."""


@dataclass(frozen=True)
class ExtractFeaturesRequest:
    replay_public_id: str
    replay_player_public_id: str | None
    extractor_names: tuple[str, ...]
    settings: CanonicalValue = ()

    def __post_init__(self) -> None:
        if not self.replay_public_id.strip():
            raise ValueError("replay_public_id must be nonempty")
        names = tuple(sorted(self.extractor_names))
        if not names or len(names) != len(set(names)):
            raise ValueError("extractor names must be nonempty and unique")
        object.__setattr__(self, "extractor_names", names)
        object.__setattr__(self, "settings", freeze_canonical(self.settings))


@dataclass(frozen=True)
class FeatureSetReceipt:
    feature_set_public_id: str
    extractor_name: str
    extractor_version: str
    input_digest: str
    cache_key: str
    cache_hit: bool
    features: tuple[FeatureValue, ...]


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, identity))


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if type(key) is str}


def _schema_label(source_kind: str, schema_version: int) -> str:
    return f"{source_kind}-v{schema_version}"


def _require_strict_canonical_tree(value: object) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
            raise ValueError("canonical floats must be finite and may not be negative zero")
        return
    if type(value) is list:
        for item in value:
            _require_strict_canonical_tree(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("canonical mapping keys must be built-in strings")
        for item in value.values():
            _require_strict_canonical_tree(item)
        return
    raise ValueError("persisted spatial projection must contain exact JSON values")


# TheSuperHackers @feature Leex 22/08/2026 Make one transactional service authoritative for feature cache persistence. (#TBD)
class FeatureExtractionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        extractors: tuple[RegisteredExtractor, ...] | None = None,
        registry: FeatureRegistry = BASE_REGISTRY,
    ) -> None:
        self._session_factory = session_factory
        configured = extractors if extractors is not None else cast(
            tuple[RegisteredExtractor, ...],
            (
                ActivityExtractor(),
                BuildOrderExtractor(),
                CombatExtractor(),
                EconomyExtractor(),
                ProductionExtractor(),
            ),
        )
        self._extractors = {extractor.name: extractor for extractor in configured}
        if len(self._extractors) != len(configured):
            raise ValueError("extractor names must be unique")
        self._registry = registry

    def extract(self, request: ExtractFeaturesRequest) -> tuple[FeatureSetReceipt, ...]:
        unknown = set(request.extractor_names) - set(self._extractors)
        if unknown:
            raise FeatureExtractionError(f"unknown extractor: {min(unknown)}")
        receipts: list[FeatureSetReceipt] = []
        for extractor_name in request.extractor_names:
            extractor = self._extractors[extractor_name]
            observation_policy = self._observation_policy(extractor)
            context = (
                self._build_context(request)
                if observation_policy == "target_player"
                else self._build_context_with_policy(request, observation_policy)
            )
            digest = input_digest(context)
            key = cache_key(context, extractor.name, extractor.version, registry_schema=self._registry.schema_version)
            hit = self._load_receipt(key, cache_hit=True)
            if hit is not None:
                receipts.append(hit)
                continue
            try:
                bundle = extractor.extract(context)
                values = self._validated_bundle(bundle, extractor, context)
                receipts.append(self._persist(context, extractor, digest, key, values))
            except FeatureExtractionError:
                raise
            except Exception as error:
                self._record_failure(context, extractor, digest, key, "feature_extraction_failed", str(error))
                raise FeatureExtractionError("feature extraction failed") from error
        return tuple(receipts)

    @staticmethod
    def _observation_policy(extractor: RegisteredExtractor) -> ObservationPolicy:
        provider = cast(ExtractorObservationPolicy, extractor)
        policy = getattr(provider, "observation_policy", "target_player")
        if policy not in ("target_player", "replay_wide_telemetry"):
            raise FeatureExtractionError("invalid extractor observation policy")
        return cast(ObservationPolicy, policy)

    def _build_context(self, request: ExtractFeaturesRequest) -> FeatureContext:
        return self._build_context_with_policy(request, "target_player")

    def _build_context_with_policy(
        self, request: ExtractFeaturesRequest, observation_policy: ObservationPolicy
    ) -> FeatureContext:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == request.replay_public_id))
            if replay is None:
                raise FeatureExtractionError("unknown replay")
            replay_player: ReplayPlayer | None = None
            parser: ParserRun | None = None
            if request.replay_player_public_id is not None:
                replay_player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == request.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if replay_player is None:
                    raise FeatureExtractionError("replay player does not belong to replay")
                parser = session.scalar(
                    select(ParserRun).where(ParserRun.id == replay_player.parser_run_id, ParserRun.status == "succeeded")
                )
            else:
                parser = session.scalar(
                    select(ParserRun)
                    .where(ParserRun.replay_id == replay.id, ParserRun.status == "succeeded")
                    .order_by(ParserRun.run_id.desc())
                    .limit(1)
                )
            telemetry = session.scalar(
                select(TelemetryRun)
                .where(TelemetryRun.replay_id == replay.id, TelemetryRun.status == "succeeded")
                .order_by(TelemetryRun.run_id.desc())
                .limit(1)
            )
            scope = (
                FeatureScope("player", replay_player.public_id, replay_player.public_id)
                if replay_player is not None
                else FeatureScope("replay", replay.public_id)
            )
            observations: list[ObservedEvidence] = []
            versions: list[tuple[str, str]] = []
            if parser is not None:
                versions.append(("parser", f"{parser.schema_version}:{parser.run_id}"))
                observations.extend(self._parser_observations(session, replay, replay_player, parser))
            if telemetry is not None:
                versions.append(("telemetry", f"{telemetry.schema_version}:{telemetry.run_id}"))
                observations.extend(
                    self._telemetry_observations(
                        session,
                        replay,
                        replay_player,
                        telemetry,
                        observation_policy=observation_policy,
                    )
                )
            catalog_identity = None
            if telemetry is not None and telemetry.catalog_asset_id is not None:
                catalog = session.get(ManagedAsset, telemetry.catalog_asset_id)
                catalog_identity = None if catalog is None else catalog.sha256
            return FeatureContext(
                cache_schema="feature-context-v1",
                replay_public_id=replay.public_id,
                replay_sha256=replay.sha256,
                replay_player_public_id=None if replay_player is None else replay_player.public_id,
                scope=scope,
                observation_schema_versions=tuple(versions),
                parser_completion_status=None if parser is None else parser.completion_status,
                telemetry_status=None if telemetry is None else telemetry.status,
                final_frame=None if telemetry is None else telemetry.final_frame,
                catalog_identity=catalog_identity,
                observed=tuple(observations),
                settings=request.settings,
            )

    def _parser_observations(
        self, session: Session, replay: Replay, replay_player: ReplayPlayer | None, parser: ParserRun
    ) -> tuple[ObservedEvidence, ...]:
        statement = (
            select(ReplayCommand, EvidenceItem)
            .join(EvidenceItem, EvidenceItem.id == ReplayCommand.evidence_item_id)
            .where(ReplayCommand.parser_run_id == parser.id, ReplayCommand.replay_id == replay.id)
            .order_by(ReplayCommand.frame, EvidenceItem.source_key, EvidenceItem.public_id)
        )
        observations = []
        for command, evidence in session.execute(statement):
            self._validate_observed_evidence(
                evidence,
                replay,
                parser_run_id=parser.id,
                telemetry_run_id=None,
            )
            if replay_player is not None and command.replay_player_id != replay_player.id:
                continue
            facts = {
                "arguments": command.arguments_json,
                "message_name": command.message_name,
                "message_type": command.message_type,
                "replay_player_public_id": None if replay_player is None else replay_player.public_id,
            }
            observations.append(
                ObservedEvidence(
                    EvidenceRef(
                        evidence.public_id,
                        "observed",
                        evidence.source_kind,
                        evidence.source_key,
                        _schema_label(evidence.source_kind, evidence.schema_version),
                    ),
                    command.frame,
                    "replay_command",
                    cast(CanonicalValue, facts),
                )
            )
        return tuple(observations)

    def _telemetry_observations(
        self,
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        telemetry: TelemetryRun,
        *,
        observation_policy: ObservationPolicy,
    ) -> tuple[ObservedEvidence, ...]:
        statement = (
            select(TelemetryEvent, EvidenceItem)
            .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
            .where(TelemetryEvent.telemetry_run_id == telemetry.id)
            .order_by(TelemetryEvent.frame, EvidenceItem.source_key, EvidenceItem.public_id)
        )
        event_rows = tuple(session.execute(statement))
        for _, evidence in event_rows:
            self._validate_observed_evidence(
                evidence,
                replay,
                parser_run_id=None,
                telemetry_run_id=telemetry.id,
            )
        requires_exact_player_mapping = observation_policy == "replay_wide_telemetry" or any(
            event.event_type in ("players_initialized", "entity_sample") for event, _ in event_rows
        )
        if requires_exact_player_mapping:
            players, projected_slots = self._selected_telemetry_players(session, replay, telemetry, event_rows)
            if replay_player is not None and replay_player.public_id not in players.values():
                raise FeatureExtractionError("telemetry player mapping does not include the requested parser player")
        else:
            players = {
                item.player_index: item.public_id
                for item in session.scalars(select(ReplayPlayer).where(ReplayPlayer.replay_id == replay.id)).all()
                if item.player_index is not None
            }
            projected_slots = None
        spatial_projection = self._validated_spatial_projection(session, replay, telemetry, event_rows)
        evidence_by_sequence = {event.sequence: evidence.public_id for event, evidence in event_rows}
        entity_rows = tuple(
            session.scalars(select(Entity).where(Entity.telemetry_run_id == telemetry.id)).all()
        )
        if projected_slots is not None and any(
            item.initial_owner_player_index is not None and item.initial_owner_player_index not in players
            for item in entity_rows
        ):
            raise FeatureExtractionError("telemetry entity owner mapping is invalid")
        entities = {
            item.object_id: (
                item.template_name,
                players.get(item.initial_owner_player_index)
                if item.initial_owner_player_index is not None
                else None,
                evidence_by_sequence.get(item.creation_sequence) if item.creation_sequence is not None else None,
            )
            for item in entity_rows
        }
        observations = []
        for event, evidence in event_rows:
            facts = self._event_facts(event, players, entities, replay_player, projected_slots)
            if event.event_type == "manifest" and spatial_projection is not None:
                facts["validated_spatial_projection"] = spatial_projection
            if (
                replay_player is not None
                and observation_policy == "target_player"
                and not self._belongs_to_player(event.event_type, facts, replay_player.public_id)
            ):
                continue
            observations.append(
                ObservedEvidence(
                    EvidenceRef(
                        evidence.public_id,
                        "observed",
                        evidence.source_kind,
                        evidence.source_key,
                        _schema_label(evidence.source_kind, evidence.schema_version),
                    ),
                    event.frame,
                    event.event_type,
                    cast(CanonicalValue, facts),
                )
            )
        return tuple(observations)

    # TheSuperHackers @fix Leex 22/08/2026 Resolve engine owners only through the telemetry attempt's selected parser. (#TBD)
    def _selected_telemetry_players(
        self,
        session: Session,
        replay: Replay,
        telemetry: TelemetryRun,
        event_rows: tuple[Row[tuple[TelemetryEvent, EvidenceItem]], ...],
    ) -> tuple[dict[int, str], list[dict[str, object]]]:
        parser_run_id = _mapping(telemetry.settings_json).get("parser_run_id")
        if type(parser_run_id) is not str or not parser_run_id:
            raise FeatureExtractionError("telemetry player mapping has no selected parser run")
        selected = session.scalar(
            select(ParserRun).where(
                ParserRun.replay_id == replay.id,
                ParserRun.run_id == parser_run_id,
                ParserRun.status == "succeeded",
            )
        )
        if selected is None:
            raise FeatureExtractionError("telemetry player mapping selected parser run is unavailable")
        rows = tuple(
            session.scalars(
                select(ReplayPlayer)
                .where(ReplayPlayer.parser_run_id == selected.id, ReplayPlayer.replay_id == replay.id)
                .order_by(ReplayPlayer.slot_index, ReplayPlayer.public_id)
            ).all()
        )
        by_slot = {row.slot_index: row for row in rows}
        if len(by_slot) != len(rows):
            raise FeatureExtractionError("telemetry player mapping selected parser slots are ambiguous")
        initialization_events = [event for event, _ in event_rows if event.event_type == "players_initialized"]
        if len(initialization_events) != 1:
            raise FeatureExtractionError("telemetry player mapping initialization evidence is ambiguous")
        raw_slots = _mapping(initialization_events[0].payload_json).get("slots")
        if type(raw_slots) is not list:
            raise FeatureExtractionError("telemetry player mapping initialization slots are invalid")
        players: dict[int, str] = {}
        projected_slots: list[dict[str, object]] = []
        resolved_slots: set[int] = set()
        for raw_slot in raw_slots:
            if type(raw_slot) is not dict:
                raise FeatureExtractionError("telemetry player mapping initialization slots are invalid")
            slot = cast(dict[str, object], raw_slot)
            if slot.get("resolution_status") != "resolved":
                continue
            slot_index = slot.get("slot_index")
            player_index = slot.get("player_index")
            if (
                type(slot_index) is not int
                or type(player_index) is not int
                or slot_index not in by_slot
            ):
                raise FeatureExtractionError("telemetry player mapping resolved slot has no selected parser observation")
            if slot_index in resolved_slots or player_index in players:
                raise FeatureExtractionError("telemetry player mapping initialization mapping is ambiguous")
            replay_player_public_id = by_slot[slot_index].public_id
            resolved_slots.add(slot_index)
            players[player_index] = replay_player_public_id
            projected_slots.append(
                {
                    "slot_index": slot_index,
                    "player_index": player_index,
                    "resolution_status": "resolved",
                    "owner_scope_key": replay_player_public_id,
                    "replay_player_public_id": replay_player_public_id,
                }
            )
        if not players:
            raise FeatureExtractionError("telemetry player mapping has no resolved selected parser slots")
        return players, sorted(projected_slots, key=lambda item: cast(int, item["slot_index"]))

    # TheSuperHackers @fix Leex 22/08/2026 Authorize spatial facts through one exact persisted telemetry identity graph. (#TBD)
    def _validated_spatial_projection(
        self,
        session: Session,
        replay: Replay,
        telemetry: TelemetryRun,
        event_rows: tuple[Row[tuple[TelemetryEvent, EvidenceItem]], ...],
    ) -> dict[str, object] | None:
        manifest_events = [event for event, _ in event_rows if event.event_type == "manifest"]
        has_map_identity = any(
            value is not None
            for value in (telemetry.map_id, telemetry.map_asset_id, telemetry.catalog_asset_id)
        )
        if not has_map_identity:
            if any(
                event.schema_version == 2
                and (
                    _mapping(event.payload_json).get("map_asset") is not None
                    or _mapping(event.payload_json).get("game_data_catalog") is not None
                )
                for event in manifest_events
            ):
                raise FeatureExtractionError("validated spatial identity mismatch")
            return None
        if (
            telemetry.map_id is None
            or telemetry.map_asset_id is None
            or telemetry.catalog_asset_id is None
            or replay.map_id != telemetry.map_id
        ):
            raise FeatureExtractionError("validated spatial identity mismatch")
        map_row = session.get(Map, telemetry.map_id)
        manifest_asset = session.get(ManagedAsset, telemetry.map_asset_id)
        catalog_asset = session.get(ManagedAsset, telemetry.catalog_asset_id)
        if (
            map_row is None
            or manifest_asset is None
            or catalog_asset is None
            or map_row.manifest_asset_id != telemetry.map_asset_id
            or manifest_asset.kind != "telemetry_map_asset"
            or catalog_asset.kind != "telemetry_catalog"
            or len(manifest_events) != 1
        ):
            raise FeatureExtractionError("validated spatial identity mismatch")
        manifest = _mapping(manifest_events[0].payload_json)
        map_reference = _mapping(manifest.get("map_asset"))
        catalog_reference = _mapping(manifest.get("game_data_catalog"))
        if (
            manifest.get("engine_build") != telemetry.engine_build
            or manifest.get("map_identity") != map_row.map_identity
            or map_reference.get("type") != "map_asset"
            or map_reference.get("schema_version") != map_row.schema_version
            or map_reference.get("content_sha256") != map_row.content_sha256
            or map_reference.get("engine_data_identity") != map_row.engine_data_identity
            or map_reference.get("map_identity") != map_row.map_identity
            or map_reference.get("sha256") != manifest_asset.sha256
            or catalog_reference.get("type") != "game_data_catalog"
            or catalog_reference.get("sha256") != catalog_asset.sha256
            or catalog_reference.get("engine_data_identity") != map_row.engine_data_identity
            or telemetry.engine_build != map_row.engine_data_identity
        ):
            raise FeatureExtractionError("validated spatial identity mismatch")
        metadata = _mapping(map_row.metadata_json)
        raw_projection = metadata.get("validated_spatial_projection")
        if raw_projection is None:
            return None
        try:
            return self._materialize_spatial_projection(session, map_row, raw_projection)
        except (TypeError, ValueError):
            raise FeatureExtractionError("malformed validated spatial projection") from None

    def _materialize_spatial_projection(
        self, session: Session, map_row: Map, raw_projection: object
    ) -> dict[str, object]:
        if type(raw_projection) is not dict:
            raise ValueError("projection must be an exact mapping")
        _require_strict_canonical_tree(raw_projection)
        projection = cast(dict[str, object], raw_projection)
        required = {
            "amphibious_passable",
            "content_sha256",
            "engine_data_identity",
            "ground_passable",
            "map_identity",
            "pathing",
            "schema_version",
            "world_bounds",
            "zone_ids",
        }
        if set(projection) != required:
            raise ValueError("projection keys differ from the closed schema")
        pathing = GridSpec.model_validate(projection["pathing"], strict=True)
        world_bounds = WorldBounds.model_validate(projection["world_bounds"], strict=True)
        count = pathing.width * pathing.height
        ground = projection["ground_passable"]
        amphibious = projection["amphibious_passable"]
        zones = projection["zone_ids"]
        if (
            projection["schema_version"] != map_row.schema_version
            or projection["content_sha256"] != map_row.content_sha256
            or projection["map_identity"] != map_row.map_identity
            or projection["engine_data_identity"] != map_row.engine_data_identity
            or pathing.width != map_row.pathing_width
            or pathing.height != map_row.pathing_height
            or pathing.cell_size.x != map_row.pathing_cell_size
            or world_bounds.minimum.x != map_row.min_x
            or world_bounds.minimum.y != map_row.min_y
            or world_bounds.minimum.z != map_row.min_z
            or world_bounds.maximum.x != map_row.max_x
            or world_bounds.maximum.y != map_row.max_y
            or world_bounds.maximum.z != map_row.max_z
            or type(ground) is not list
            or type(amphibious) is not list
            or type(zones) is not list
            or len(ground) != count
            or len(amphibious) != count
            or len(zones) != count
            or any(type(value) is not bool for value in (*ground, *amphibious))
            or any(type(value) is not int for value in zones)
            or any(left and not right for left, right in zip(ground, amphibious, strict=True))
        ):
            raise ValueError("projection values disagree with persisted map identity")
        starts: dict[tuple[int, str], dict[str, object]] = {}
        static_objects: list[dict[str, object]] = []
        resources = session.scalars(
            select(MapResource).where(MapResource.map_id == map_row.id).order_by(MapResource.stable_key)
        )
        for resource in resources:
            if resource.resource_kind == "start_position":
                _require_strict_canonical_tree(resource.payload_json)
                start = StartPosition.model_validate(resource.payload_json, strict=True)
                payload = start.model_dump(mode="json")
                if (
                    resource.stable_key != f"start:{resource.owner_player_index}"
                    or resource.source_object_id is not None
                    or resource.owner_player_index not in start.slot_indices
                    or resource.template_name != start.name
                    or (resource.x, resource.y, resource.z) != (start.position.x, start.position.y, start.position.z)
                ):
                    raise ValueError("start-position row disagrees with its observed payload")
                key = (start.waypoint_id, start.name)
                if key in starts and starts[key] != payload:
                    raise ValueError("start-position rows disagree")
                starts[key] = payload
            elif resource.resource_kind == "static_object":
                _require_strict_canonical_tree(resource.payload_json)
                static_object = StaticObjectFeature.model_validate(resource.payload_json, strict=True)
                payload = static_object.model_dump(mode="json")
                if (
                    resource.stable_key != f"static:{static_object.object_id}"
                    or resource.source_object_id != static_object.object_id
                    or resource.owner_player_index is not None
                    or resource.template_name != static_object.template_name
                    or (resource.x, resource.y, resource.z)
                    != (static_object.position.x, static_object.position.y, static_object.position.z)
                ):
                    raise ValueError("static-object row disagrees with its observed payload")
                static_objects.append(payload)
            else:
                raise ValueError("unsupported map resource kind")
        return {
            "schema_version": map_row.schema_version,
            "content_sha256": map_row.content_sha256,
            "map_identity": map_row.map_identity,
            "engine_data_identity": map_row.engine_data_identity,
            "pathing": pathing.model_dump(mode="json"),
            "world_bounds": world_bounds.model_dump(mode="json"),
            "ground_passable": list(ground),
            "amphibious_passable": list(amphibious),
            "zone_ids": list(zones),
            "start_positions": [starts[key] for key in sorted(starts)],
            "static_objects": sorted(static_objects, key=lambda item: cast(int, item["object_id"])),
        }

    def _validate_observed_evidence(
        self,
        evidence: EvidenceItem,
        replay: Replay,
        *,
        parser_run_id: int | None,
        telemetry_run_id: int | None,
    ) -> None:
        if (
            evidence.tier != "observed"
            or evidence.replay_id != replay.id
            or evidence.parser_run_id != parser_run_id
            or evidence.telemetry_run_id != telemetry_run_id
        ):
            raise FeatureExtractionError("malformed persisted evidence link")

    def _event_facts(
        self,
        event: TelemetryEvent,
        players: Mapping[int, str],
        entities: Mapping[int, tuple[str, str | None, str | None]],
        replay_player: ReplayPlayer | None,
        projected_slots: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        facts = _mapping(event.payload_json)
        event_type = event.event_type
        player_index = facts.get("player_index")
        if event_type.startswith("construction_"):
            owner_player_index = facts.get("owner_player_index")
            responsible_player_index = facts.get("responsible_player_index")
            object_id = facts.get("object_id")
            if projected_slots is not None and event_type == "construction_completed":
                if type(object_id) is not int or object_id < 0:
                    raise FeatureExtractionError("telemetry construction object identity is invalid")
                if object_id not in entities:
                    raise FeatureExtractionError("telemetry construction object identity is unknown")
                for owner_value in (owner_player_index, responsible_player_index):
                    if owner_value is not None and (type(owner_value) is not int or owner_value not in players):
                        raise FeatureExtractionError("telemetry construction owner mapping is invalid")
                if (
                    owner_player_index is not None
                    and responsible_player_index is not None
                    and owner_player_index != responsible_player_index
                ):
                    raise FeatureExtractionError("telemetry construction owner identity is ambiguous")
                owner = responsible_player_index if responsible_player_index is not None else owner_player_index
                owner_public_id = players[cast(int, owner)] if owner is not None else None
            else:
                owner = facts.get("responsible_player_index", owner_player_index)
                entity_owner = entities.get(object_id) if type(object_id) is int else None
                owner_public_id = players.get(owner) if type(owner) is int else None if entity_owner is None else entity_owner[1]
            entity = entities.get(object_id) if type(object_id) is int else None
            facts["replay_player_public_id"] = owner_public_id
            facts.pop("template_name", None)
            if entity is not None:
                facts["template_name"] = entity[0]
                facts["template_evidence_public_id"] = entity[2]
        elif event_type == "object_created":
            object_id = facts.get("object_id")
            entity = entities.get(object_id) if type(object_id) is int else None
            facts.pop("template_name", None)
            if entity is not None:
                facts["template_name"] = entity[0]
                facts["replay_player_public_id"] = entity[1]
        elif event_type.startswith(("production_", "upgrade_")):
            facts["replay_player_public_id"] = players.get(player_index) if type(player_index) is int else None
            if event_type.startswith("production_"):
                facts["item_kind"] = "unit"
                facts["item_name"] = facts.get("template_name")
                facts["production_identity"] = f"production:{facts.get('production_id')}"
            else:
                facts["item_kind"] = "upgrade"
                facts["item_name"] = facts.get("upgrade_name")
                facts["production_identity"] = f"upgrade:{facts.get('upgrade_queue_id', facts.get('upgrade_name'))}"
        elif event_type in ("cash_changed", "supply_collected"):
            if projected_slots is not None and (type(player_index) is not int or player_index not in players):
                raise FeatureExtractionError("telemetry economy owner mapping is invalid")
            if projected_slots is not None and event_type == "supply_collected":
                source_object_id = facts.get("source_object_id")
                if source_object_id is not None and (
                    type(source_object_id) is not int or source_object_id < 0 or source_object_id not in entities
                ):
                    raise FeatureExtractionError("telemetry supply source object identity is invalid")
            facts["replay_player_public_id"] = players.get(player_index) if type(player_index) is int else None
        elif event_type == "damage_applied":
            source_mask = facts.get("source_player_mask")
            source_indices = facts.get("source_player_indices")
            victim = facts.get("victim_player_index")
            if projected_slots is not None:
                if event.schema_version not in (1, 2):
                    raise FeatureExtractionError("telemetry damage schema is unsupported")
                if event.schema_version == 2:
                    if type(source_mask) is not int or not 0 <= source_mask <= 0xFFFFFFFF:
                        raise FeatureExtractionError("telemetry damage source player mask is invalid")
                    if type(source_indices) is not list:
                        raise FeatureExtractionError("telemetry damage source player mapping is invalid")
                elif source_mask is not None and (
                    type(source_mask) is not int or not 0 <= source_mask <= 0xFFFFFFFF
                ):
                    raise FeatureExtractionError("telemetry damage source player mask is invalid")
                if source_indices is None:
                    resolved_source_indices: list[int] = []
                elif type(source_indices) is not list:
                    raise FeatureExtractionError("telemetry damage source player mapping is invalid")
                else:
                    raw_source_indices = cast(list[object], source_indices)
                    if (
                        any(
                            type(index) is not int
                            or index < 0
                            or (event.schema_version == 2 and index > 31)
                            or index not in players
                            for index in raw_source_indices
                        )
                        or raw_source_indices != sorted(set(cast(list[int], raw_source_indices)))
                    ):
                        raise FeatureExtractionError("telemetry damage source player mapping is invalid")
                    resolved_source_indices = cast(list[int], raw_source_indices)
                if event.schema_version == 2 and cast(int, source_mask) != sum(
                    1 << index for index in resolved_source_indices
                ):
                    raise FeatureExtractionError("telemetry damage source player mask is inconsistent")
                if victim is not None and (type(victim) is not int or victim < 0 or victim not in players):
                    raise FeatureExtractionError("telemetry damage victim player mapping is invalid")
                facts["source_replay_player_public_ids"] = [players[index] for index in resolved_source_indices]
                facts["victim_replay_player_public_id"] = players.get(victim) if victim is not None else None
            else:
                facts["source_replay_player_public_ids"] = [
                    players[index]
                    for index in cast(list[object], source_indices or [])
                    if type(index) is int and index in players
                ]
                facts["victim_replay_player_public_id"] = players.get(victim) if type(victim) is int else None
        elif event_type == "order_issued":
            source = facts.get("source_player_index")
            facts["source_replay_player_public_id"] = players.get(source) if type(source) is int else None
        elif event_type == "entity_state_changed":
            owner = facts.get("owner_player_index")
            facts["replay_player_public_id"] = players.get(owner) if type(owner) is int else None
        elif event_type == "entity_sample":
            object_id = facts.get("object_id")
            owner = facts.get("owner_player_index")
            if type(object_id) is not int or object_id < 0:
                raise FeatureExtractionError("telemetry entity sample object identity is invalid")
            if projected_slots is not None and object_id not in entities:
                raise FeatureExtractionError("telemetry entity sample object identity is unknown")
            if owner is not None and (type(owner) is not int or owner not in players):
                raise FeatureExtractionError("telemetry entity sample owner mapping is invalid")
            owner_public_id = players.get(owner) if owner is not None else None
            facts["object_key"] = f"object:{object_id}"
            facts["owner_scope_key"] = owner_public_id
            facts["replay_player_public_id"] = owner_public_id
        elif event_type == "players_initialized":
            if projected_slots is None:
                raise FeatureExtractionError("telemetry player mapping initialization projection is unavailable")
            facts = {"slots": projected_slots}
        elif event_type == "manifest":
            exporter_settings = _mapping(facts.get("exporter_settings"))
            catalog_value = facts.get("game_data_catalog")
            map_value = facts.get("map_asset")
            catalog_reference = _mapping(catalog_value)
            map_reference = _mapping(map_value)
            # TheSuperHackers @fix Leex 22/08/2026 Project manifest evidence onto an exact path-free semantic contract. (#TBD)
            facts = {
                "engine_build": facts.get("engine_build"),
                "replay_version": facts.get("replay_version"),
                "map_identity": facts.get("map_identity"),
                "initial_seed": facts.get("initial_seed"),
                "audio_enabled": exporter_settings.get("audio_enabled"),
                "movement_sample_frames": exporter_settings.get("movement_sample_frames"),
                "order_coverage": exporter_settings.get("order_coverage"),
                "game_data_catalog": None
                if catalog_value is None
                else {
                    "type": catalog_reference.get("type"),
                    "sha256": catalog_reference.get("sha256"),
                    "engine_data_identity": catalog_reference.get("engine_data_identity"),
                },
                "map_asset": None
                if map_value is None
                else {
                    "type": map_reference.get("type"),
                    "schema_version": map_reference.get("schema_version"),
                    "sha256": map_reference.get("sha256"),
                    "content_sha256": map_reference.get("content_sha256"),
                    "engine_data_identity": map_reference.get("engine_data_identity"),
                    "map_identity": map_reference.get("map_identity"),
                },
            }
        elif event_type == "complete" and replay_player is not None:
            facts["replay_player_public_id"] = replay_player.public_id
            balances = cast(list[object], facts.get("final_cash_balances") or [])
            for balance_value in balances:
                balance = _mapping(balance_value)
                if balance.get("player_index") == replay_player.player_index and balance.get("has_money") is True:
                    facts["final_cash_balance"] = balance.get("balance")
        elif event_type == "match_outcome" and replay_player is not None:
            winners = cast(list[object], facts.get("winner_player_indices") or [])
            losers = cast(list[object], facts.get("loser_player_indices") or [])
            result = "won" if replay_player.player_index in winners else "lost" if replay_player.player_index in losers else None
            if result is not None:
                facts["replay_player_public_id"] = replay_player.public_id
                facts["result_payload"] = {"result": result, "source": facts.get("source"), "status": facts.get("status")}
        return facts

    def _belongs_to_player(self, event_type: str, facts: Mapping[str, object], player_public_id: str) -> bool:
        if event_type in ("manifest", "complete"):
            return True
        if event_type == "damage_applied":
            sources = cast(list[object], facts.get("source_replay_player_public_ids") or [])
            return player_public_id in sources or facts.get("victim_replay_player_public_id") == player_public_id
        if event_type == "order_issued":
            return facts.get("source_replay_player_public_id") == player_public_id
        return facts.get("replay_player_public_id") == player_public_id

    def _validated_bundle(
        self, bundle: FeatureBundle, extractor: RegisteredExtractor, context: FeatureContext
    ) -> tuple[FeatureValue, ...]:
        if bundle.extractor_name != extractor.name or bundle.extractor_version != extractor.version:
            raise FeatureExtractionError("extractor bundle identity mismatch")
        try:
            expected_names = tuple(
                sorted(
                    name
                    for name in extractor.feature_names
                    if context.scope.scope_type in self._registry.definition(name).scope_types
                )
            )
        except KeyError as error:
            raise FeatureExtractionError("extractor owns an unregistered feature") from error
        if tuple(sorted(value.name for value in bundle.values)) != expected_names:
            raise FeatureExtractionError("extractor must emit each owned feature exactly once")
        values = tuple(validate_feature_value(value, self._registry) for value in bundle.values)
        identities = {
            (value.name, value.scope.scope_type, value.scope.scope_key, value.window.frame_start, value.window.frame_end)
            for value in values
        }
        if len(identities) != len(values):
            raise FeatureExtractionError("duplicate logical feature identity")
        if any(value.scope != context.scope for value in values):
            raise FeatureExtractionError("feature scope does not match context")
        return tuple(sorted(values, key=lambda value: (value.name, value.window.frame_start, value.window.frame_end)))

    def _load_receipt(self, key: str, *, cache_hit: bool) -> FeatureSetReceipt | None:
        with self._session_factory() as session:
            feature_set = session.scalar(
                select(FeatureSet).where(FeatureSet.cache_key == key, FeatureSet.status == "succeeded")
            )
            if feature_set is None:
                return None
            values = self._receipt_values(session, feature_set)
            return FeatureSetReceipt(
                feature_set.public_id,
                feature_set.extractor_name,
                feature_set.extractor_version,
                feature_set.input_digest,
                feature_set.cache_key,
                cache_hit,
                values,
            )

    def _receipt_values(self, session: Session, feature_set: FeatureSet) -> tuple[FeatureValue, ...]:
        rows = session.scalars(
            select(Feature)
            .where(Feature.feature_set_id == feature_set.id)
            .order_by(Feature.name, Feature.scope_type, Feature.scope_key, Feature.frame_start, Feature.frame_end)
        ).all()
        values = []
        for row in rows:
            role_refs: dict[str, list[EvidenceRef]] = {"input": [], "supporting": [], "contradicting": []}
            links = session.execute(
                select(FeatureEvidence.role, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == FeatureEvidence.evidence_item_id)
                .where(FeatureEvidence.feature_id == row.id)
                .order_by(EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id)
            )
            for role, evidence in links:
                role_refs[role].append(
                    EvidenceRef(
                        evidence.public_id,
                        cast(object, evidence.tier),  # type: ignore[arg-type]
                        evidence.source_kind,
                        evidence.source_key,
                        _schema_label(evidence.source_kind, evidence.schema_version),
                    )
                )
            raw_value = self._row_raw_value(row)
            replay_player_public_id = None
            entity_public_id = None
            if row.replay_player_id is not None:
                replay_player = session.get(ReplayPlayer, row.replay_player_id)
                replay_player_public_id = None if replay_player is None else replay_player.public_id
            if row.entity_id is not None:
                entity = session.get(Entity, row.entity_id)
                entity_public_id = None if entity is None else entity.public_id
            scope = FeatureScope(
                cast(object, row.scope_type),  # type: ignore[arg-type]
                row.scope_key,
                replay_player_public_id,
                row.team_id,
                entity_public_id,
            )
            values.append(
                validate_feature_value(
                    FeatureValue(
                        row.name,
                        cast(object, row.value_type),  # type: ignore[arg-type]
                        raw_value,
                        row.unit,
                        scope,
                        FeatureWindow(row.frame_start, row.frame_end),
                        "complete" if row.quality == "available" else cast(object, row.quality),  # type: ignore[arg-type]
                        row.quality_reason,
                        tuple(role_refs["input"]),
                        tuple(role_refs["supporting"]),
                        tuple(role_refs["contradicting"]),
                        cast(CanonicalValue, row.details_json),
                    ),
                    self._registry,
                )
            )
        return tuple(values)

    def _row_raw_value(self, row: Feature) -> CanonicalValue:
        return cast(CanonicalValue, {
            "integer": row.integer_value,
            "real": row.real_value,
            "text": row.text_value,
            "boolean": row.boolean_value,
            "json": row.json_value,
        }[row.value_type])

    def _persist(
        self,
        context: FeatureContext,
        extractor: RegisteredExtractor,
        digest: str,
        key: str,
        values: tuple[FeatureValue, ...],
    ) -> FeatureSetReceipt:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            winner = session.scalar(select(FeatureSet).where(FeatureSet.cache_key == key, FeatureSet.status == "succeeded"))
            if winner is not None:
                receipt = FeatureSetReceipt(
                    winner.public_id,
                    winner.extractor_name,
                    winner.extractor_version,
                    winner.input_digest,
                    winner.cache_key,
                    True,
                    self._receipt_values(session, winner),
                )
                session.commit()
                return receipt
            replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
            if replay is None:
                raise FeatureExtractionError("replay disappeared before persistence")
            replay_player = None
            if context.replay_player_public_id is not None:
                replay_player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == context.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if replay_player is None:
                    raise FeatureExtractionError("replay player disappeared before persistence")
            feature_set = FeatureSet(
                public_id=_uuid(f"feature-set:{key}"),
                replay_id=replay.id,
                replay_player_id=None if replay_player is None else replay_player.id,
                extractor_name=extractor.name,
                extractor_version=extractor.version,
                input_digest=digest,
                cache_key=key,
                status="pending",
                settings_json=thaw_canonical(context.settings),
                created_at=_now(),
            )
            session.add(feature_set)
            session.flush()
            self._persist_values(
                session,
                replay,
                replay_player,
                feature_set,
                values,
                {item.ref.public_id: item.ref for item in context.observed},
            )
            feature_set.status = "succeeded"
            feature_set.completed_at = _now()
            session.commit()
            return FeatureSetReceipt(
                feature_set.public_id,
                extractor.name,
                extractor.version,
                digest,
                key,
                False,
                values,
            )
        except FeatureExtractionError:
            session.rollback()
            raise
        except (IntegrityError, OperationalError, ValueError, TypeError) as error:
            session.rollback()
            self._record_failure(context, extractor, digest, key, "feature_persistence_failed", str(error))
            raise FeatureExtractionError("feature persistence failed") from error
        finally:
            session.close()

    def _persist_values(
        self,
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        feature_set: FeatureSet,
        values: tuple[FeatureValue, ...],
        authorized_evidence: Mapping[str, EvidenceRef],
    ) -> None:
        original_refs = tuple(
            ref
            for value in values
            for ref in value.input_evidence + value.supporting_evidence + value.contradicting_evidence
        )
        if any(authorized_evidence.get(ref.public_id) != ref for ref in original_refs):
            raise ValueError("feature evidence is not authorized by exact feature context")
        for value in values:
            supporting_ids = {ref.public_id for ref in value.supporting_evidence}
            contradicting_ids = {ref.public_id for ref in value.contradicting_evidence}
            if not supporting_ids.isdisjoint(contradicting_ids):
                raise FeatureExtractionError(
                    "feature evidence cannot be both supporting and contradicting"
                )
        refs = {
            ref.public_id: ref
            for ref in original_refs
        }
        evidence_rows = {
            item.public_id: item
            for item in session.scalars(select(EvidenceItem).where(EvidenceItem.public_id.in_(tuple(refs)))).all()
        }
        if set(evidence_rows) != set(refs):
            raise ValueError("feature evidence reference does not exist")
        for public_id, ref in refs.items():
            row = evidence_rows[public_id]
            if row.replay_id != replay.id:
                raise ValueError("cross-replay feature evidence is forbidden")
            if (
                row.tier != ref.tier
                or row.source_kind != ref.source_kind
                or row.source_key != ref.source_key
                or _schema_label(row.source_kind, row.schema_version) != ref.schema_version
            ):
                raise ValueError("feature evidence identity mismatch")
        for value in values:
            source_key = (
                f"feature:{feature_set.public_id}:{value.name}:{value.scope.scope_type}:{value.scope.scope_key}:"
                f"{value.window.frame_start}:{value.window.frame_end}"
            )
            derived = EvidenceItem(
                public_id=_uuid(f"evidence:{source_key}"),
                replay_id=replay.id,
                tier="derived",
                source_kind="feature",
                source_key=source_key,
                schema_version=1,
                created_at=_now(),
            )
            session.add(derived)
            session.flush()
            typed = self._typed_columns(value)
            feature = Feature(
                public_id=_uuid(f"feature-value:{source_key}"),
                feature_set_id=feature_set.id,
                evidence_item_id=derived.id,
                name=value.name,
                value_type=value.value_type,
                unit=value.unit,
                scope_type=value.scope.scope_type,
                scope_key=value.scope.scope_key,
                replay_player_id=None if replay_player is None else replay_player.id,
                team_id=value.scope.team_id,
                frame_start=value.window.frame_start,
                frame_end=value.window.frame_end,
                quality="available" if value.quality == "complete" else value.quality,
                quality_reason=value.quality_reason,
                details_json=thaw_canonical(value.details),
                **typed,
            )
            session.add(feature)
            session.flush()
            for role, role_refs in (
                ("input", value.input_evidence),
                ("supporting", value.supporting_evidence),
                ("contradicting", value.contradicting_evidence),
            ):
                for ref in role_refs:
                    session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=evidence_rows[ref.public_id].id, role=role))

    def _typed_columns(self, value: FeatureValue) -> dict[str, object]:
        columns: dict[str, object] = {
            "integer_value": None,
            "real_value": None,
            "text_value": None,
            "boolean_value": None,
            "json_value": None,
        }
        if value.quality == "unavailable":
            return columns
        key = {
            "integer": "integer_value",
            "real": "real_value",
            "text": "text_value",
            "boolean": "boolean_value",
            "json": "json_value",
        }[value.value_type]
        columns[key] = thaw_canonical(value.raw_value)
        return columns

    def _record_failure(
        self,
        context: FeatureContext,
        extractor: RegisteredExtractor,
        digest: str,
        key: str,
        code: str,
        message: str,
    ) -> None:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
            if replay is None:
                session.rollback()
                return
            replay_player_id = None
            if context.replay_player_public_id is not None:
                replay_player_id = session.scalar(
                    select(ReplayPlayer.id).where(
                        ReplayPlayer.public_id == context.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
            session.add(
                FeatureSet(
                    public_id=str(uuid4()),
                    replay_id=replay.id,
                    replay_player_id=replay_player_id,
                    extractor_name=extractor.name,
                    extractor_version=extractor.version,
                    input_digest=digest,
                    cache_key=key,
                    status="failed",
                    settings_json=thaw_canonical(context.settings),
                    completed_at=_now(),
                    error_json={"code": code, "message": message},
                    created_at=_now(),
                )
            )
            session.commit()
        except (IntegrityError, OperationalError):
            session.rollback()
        finally:
            session.close()
