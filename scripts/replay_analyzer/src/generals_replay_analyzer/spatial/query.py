"""Read-only immutable map scenes over accepted persisted spatial evidence."""

from __future__ import annotations

import binascii
import hashlib
import math
import struct
import zlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from generals_replay_analyzer.db.models import (
    CombatEvent,
    Entity,
    EntitySample,
    EvidenceItem,
    Map,
    MapResource,
    ParserRun,
    Replay,
    ReplayPlayer,
    Report,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    EvidenceRef,
    freeze_canonical,
)
from generals_replay_analyzer.report.model import thaw_report_value
from generals_replay_analyzer.report.query import (
    FixedReportQuery,
    ReportGraphAmbiguousError,
    ReportGraphContractError,
    ReportGraphNotFoundError,
)
from generals_replay_analyzer.spatial.assets import (
    GridSpec,
    Position3,
    SpatialMapProjection,
    SpatialUnavailable,
    StartPosition,
    StaticObjectCategory,
    StaticObjectFeature,
    WorldBounds,
    validate_map_projection,
)
from generals_replay_analyzer.spatial.coordinates import (
    PlayerTransform,
    player_centric_transform,
    world_to_map_normalized,
)

_PUBLIC_NAMESPACE = uuid5(NAMESPACE_URL, "generals-replay-analyzer:map-scene-v2")
_SAMPLE_REASONS = frozenset(
    {"lifecycle_forced", "order_forced", "state_forced", "changed", "periodic_moving_heartbeat"}
)
_FORCED_REASONS = frozenset({"lifecycle_forced", "order_forced", "state_forced"})
_MAP_SCENE_SEARCH_CANDIDATE_LIMIT = 1000
_RESOURCE_KINDS = frozenset(
    {"supply_source", "supply_warehouse", "capturable", "tech_building", "cash_generator", "oil_income"}
)


class ReportAuthority(Protocol):
    def get_report(self, query: FixedReportQuery) -> object: ...


class MapSceneContractError(RuntimeError):
    """Accepted immutable graph is missing or internally inconsistent."""


class MapSceneNotFoundError(MapSceneContractError):
    """Requested replay, report, map, or public member does not exist."""


class RasterUnavailableError(MapSceneContractError):
    """Requested raster has no accepted persisted semantic source."""


class FrozenRecord(Mapping[str, CanonicalValue]):
    """Small tuple-backed immutable record used by the read-model boundary."""

    def __init__(self, value: Mapping[str, object]) -> None:
        frozen = freeze_canonical(value)
        if not isinstance(frozen, tuple):
            raise TypeError("record must be a mapping")
        self._items = cast(tuple[tuple[str, CanonicalValue], ...], frozen)

    def __getitem__(self, key: str) -> CanonicalValue:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def as_mapping(self) -> dict[str, object]:
        return {key: _thaw(value) for key, value in self._items}


def _thaw(value: CanonicalValue) -> object:
    from generals_replay_analyzer.features.evidence import FrozenMapping

    if isinstance(value, FrozenMapping):
        return {key: _thaw(cast(CanonicalValue, item)) for key, item in value}
    if isinstance(value, tuple):
        return [_thaw(cast(CanonicalValue, item)) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class MapSceneReadQuery:
    replay_public_id: str
    report_public_id: str
    frame_start: int
    frame_end: int
    replay_player_public_ids: tuple[str, ...] = ()
    entity_public_ids: tuple[str, ...] = ()
    event_families: tuple[str, ...] = ()
    locomotor_surface: Literal["ground", "amphibious"] | None = None
    coordinate_display: Literal["raw", "map_normalized", "player_centric"] = "raw"
    player_centric_subject_public_id: str | None = None
    sample_budget: int = 5000
    include_engine_heuristics: bool = False

    def __post_init__(self) -> None:
        if type(self.frame_start) is not int or type(self.frame_end) is not int or not 0 <= self.frame_start <= self.frame_end:
            raise ValueError("map scene frame window must be ordered and nonnegative")
        if type(self.sample_budget) is not int or not 100 <= self.sample_budget <= 20_000:
            raise ValueError("sample budget must be from 100 through 20000")
        if type(self.include_engine_heuristics) is not bool:
            raise ValueError("engine heuristic overlay selection must be boolean")
        object.__setattr__(self, "replay_player_public_ids", tuple(sorted(set(self.replay_player_public_ids))))
        object.__setattr__(self, "entity_public_ids", tuple(sorted(set(self.entity_public_ids))))
        object.__setattr__(self, "event_families", tuple(sorted(set(self.event_families))))


@dataclass(frozen=True, slots=True)
class MapSceneIndexReadQuery:
    page: int = 1
    page_size: int = 25
    search: str | None = None
    availability: Literal["available", "partial", "unavailable"] | None = None

    def __post_init__(self) -> None:
        if type(self.page) is not int or self.page < 1:
            raise ValueError("map scene index page must be positive")
        if type(self.page_size) is not int or not 1 <= self.page_size <= 100:
            raise ValueError("map scene index page size must be from 1 through 100")
        if self.search is not None and (
            type(self.search) is not str or not 1 <= len(self.search) <= 256
        ):
            raise ValueError("map scene index search must contain from 1 through 256 characters")


@dataclass(frozen=True, slots=True)
class MapRasterReadQuery:
    map_public_id: str
    raster_public_id: str


@dataclass(frozen=True, slots=True)
class SceneSample:
    sample_public_id: str
    entity_public_id: str
    replay_player_public_id: str | None
    frame: int
    raw_position: tuple[float, float, float]
    orientation: float
    sample_reason: str
    locomotor_surface: Literal["ground", "amphibious"] | None
    evidence_public_id: str

    def __post_init__(self) -> None:
        if self.sample_reason not in _SAMPLE_REASONS:
            raise ValueError("unsupported sample reason")
        if type(self.frame) is not int or self.frame < 0:
            raise ValueError("sample frame must be nonnegative")
        if any(not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0) for value in (*self.raw_position, self.orientation)):
            raise ValueError("sample spatial values must be finite and cannot use negative zero")


@dataclass(frozen=True, slots=True)
class MapSceneReadModel:
    payload: CanonicalValue

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_canonical(self.payload))


@dataclass(frozen=True, slots=True)
class MapSceneIndexReadModel:
    payload: CanonicalValue

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_canonical(self.payload))


@dataclass(frozen=True, slots=True)
class MapRasterReadModel:
    map_public_id: str
    descriptor: FrozenRecord
    content: bytes

    def __post_init__(self) -> None:
        if not self.content.startswith(b"\x89PNG\r\n\x1a\n") or len(self.content) > 16 * 1024 * 1024:
            raise ValueError("raster must be a bounded PNG")
        if hashlib.sha256(self.content).hexdigest() != self.descriptor["content_sha256"]:
            raise ValueError("raster digest mismatch")


def _sample_key(item: SceneSample) -> tuple[int, str, str]:
    return (item.frame, item.entity_public_id, item.sample_public_id)


# TheSuperHackers @feature Leex 23/08/2026 Preserve event-forced observations with deterministic integer midpoint sampling. (#TBD)
def downsample_samples(samples: Sequence[SceneSample], budget: int) -> tuple[tuple[SceneSample, ...], dict[str, object]]:
    if type(budget) is not int or not 100 <= budget <= 20_000:
        raise ValueError("sample budget must be from 100 through 20000")
    by_id: dict[str, SceneSample] = {}
    for item in samples:
        if type(item) is not SceneSample:
            raise TypeError("samples must use SceneSample")
        prior = by_id.setdefault(item.sample_public_id, item)
        if prior != item:
            raise MapSceneContractError("duplicate sample identity has conflicting evidence")
    ordered = tuple(sorted(by_id.values(), key=_sample_key))
    by_entity: dict[str, list[SceneSample]] = {}
    for item in ordered:
        by_entity.setdefault(item.entity_public_id, []).append(item)
    mandatory_ids = {
        item.sample_public_id
        for values in by_entity.values()
        for item in (values[0], values[-1])
    }
    mandatory_ids.update(item.sample_public_id for item in ordered if item.sample_reason in _FORCED_REASONS)
    mandatory = tuple(item for item in ordered if item.sample_public_id in mandatory_ids)
    candidates = tuple(item for item in ordered if item.sample_public_id not in mandatory_ids)
    if len(mandatory) >= budget:
        selected = mandatory
    else:
        count = min(budget - len(mandatory), len(candidates))
        if count == len(candidates):
            extras = candidates
        else:
            extras = tuple(candidates[((2 * rank + 1) * len(candidates)) // (2 * count)] for rank in range(count))
        selected = tuple(sorted((*mandatory, *extras), key=_sample_key))
    metadata = {
        "algorithm_version": "event-forced-stratified-v1",
        "requested_sample_budget": budget,
        "original_sample_count": len(ordered),
        "mandatory_sample_count": len(mandatory),
        "returned_sample_count": len(selected),
        "budget_exceeded_by_mandatory": len(mandatory) > budget,
    }
    return selected, metadata


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MapSceneContractError(f"{label} is not an accepted mapping")
    return cast(Mapping[str, object], value)


def _number(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise MapSceneContractError(f"{label} is not numeric")
    result = float(cast(int | float, value))
    if not math.isfinite(result) or (result == 0.0 and math.copysign(1.0, result) < 0):
        raise MapSceneContractError(f"{label} is not canonical")
    return result


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise MapSceneContractError(f"{label} is not an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise MapSceneContractError(f"{label} is not boolean")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MapSceneContractError(f"{label} is not a nonempty string")
    return value


def _public_id(*parts: object) -> str:
    return str(uuid5(_PUBLIC_NAMESPACE, ":".join(str(part) for part in parts)))


def _availability(state: str, reasons: Sequence[str] = (), evidence: Sequence[str] = ()) -> dict[str, object]:
    return {
        "state": state,
        "reason_codes": sorted(set(reasons)),
        "evidence_references": sorted(set(evidence)),
    }


def _evidence(public_id: str, tier: str = "observed") -> list[dict[str, str]]:
    return [{"evidence_public_id": public_id, "tier": tier}]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def _pathability_png(width: int, height: int, values: tuple[bool, ...]) -> bytes:
    rows = []
    for y in reversed(range(height)):
        row = bytearray((0,))
        for x in range(width):
            row.extend((126, 231, 135) if values[y * width + x] else (31, 41, 55))
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + _png_chunk(b"IEND", b"")


def _assert_evidence_membership(value: object, accepted: frozenset[str]) -> None:
    if isinstance(value, Mapping):
        for key, member in value.items():
            if key == "evidence_public_id" and isinstance(member, str) and member not in accepted:
                raise MapSceneContractError("emitted evidence is outside the fixed report")
            if (
                key == "evidence_references"
                and isinstance(member, Sequence)
                and any(isinstance(item, str) and item not in accepted for item in member)
            ):
                raise MapSceneContractError("emitted evidence is outside the fixed report")
            _assert_evidence_membership(member, accepted)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for member in value:
            _assert_evidence_membership(member, accepted)


class MapSceneQueryService:
    """Validate a fixed report and project only its selected persisted observation graph."""

    def __init__(self, session_factory: sessionmaker[Session], report_authority: ReportAuthority) -> None:
        self._session_factory = session_factory
        self._report_authority = report_authority

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    @staticmethod
    def raster_public_id(map_public_id: str, kind: str, surface: str | None) -> str:
        return _public_id("raster", map_public_id, kind, surface or "none", "map-grid-raster-v1")

    def _report_graph(self, query: MapSceneReadQuery) -> Any:
        graph = self._report_authority.get_report(FixedReportQuery(query.replay_public_id, query.report_public_id))
        selected = getattr(graph, "selected", None)
        document = getattr(selected, "document", None)
        if document is None or (document.replay_public_id, document.report_public_id) != (
            query.replay_public_id,
            query.report_public_id,
        ):
            raise MapSceneContractError("fixed report membership is inconsistent")
        return graph

    @staticmethod
    def _report_evidence(document: object) -> dict[str, str]:
        references = [
            reference
            for family in ("evidence_availability", "observed", "derived", "inferred")
            for value in getattr(document, family, ())
            for reference in getattr(value, "evidence", ())
        ]
        tiers: dict[str, str] = {}
        for reference in references:
            existing = tiers.setdefault(reference.public_id, reference.tier)
            if existing != reference.tier:
                raise MapSceneContractError("report evidence membership is internally inconsistent")
        return tiers

    @staticmethod
    def _camera_combat_anchors(
        document: object,
    ) -> dict[str, tuple[int, str, int]]:
        values = tuple(
            value
            for value in getattr(document, "evidence_availability", ())
            if getattr(value, "claim_id", None)
            == "availability:camera_combat_anchors"
        )
        if not values:
            return {}
        if len(values) != 1:
            raise MapSceneContractError("camera combat anchor authority is ambiguous")
        value = values[0]
        raw = thaw_report_value(getattr(value, "raw_value", None))
        payload = _mapping(raw, "camera combat anchor payload")
        if payload.get("schema_version") != "camera-combat-anchors-v1":
            raise MapSceneContractError("camera combat anchor schema is unsupported")
        pairs = payload.get("pairs")
        if not isinstance(pairs, list) or not pairs or len(pairs) > 256:
            raise MapSceneContractError("camera combat anchor pairs are not bounded")
        output: dict[str, tuple[int, str, int]] = {}
        used_samples: set[str] = set()
        ordered_keys: list[tuple[int, str, int, str]] = []
        referenced_ids: set[str] = set()
        for raw_pair in pairs:
            pair = _mapping(raw_pair, "camera combat anchor pair")
            combat_evidence_id = _string(
                pair.get("combat_evidence_public_id"),
                "camera combat evidence identity",
            )
            combat_frame = _integer(
                pair.get("combat_frame"), "camera combat frame"
            )
            sample_evidence_id = _string(
                pair.get("attacker_sample_evidence_public_id"),
                "camera attacker sample evidence identity",
            )
            sample_frame = _integer(
                pair.get("attacker_sample_frame"), "camera attacker sample frame"
            )
            if (
                sample_frame > combat_frame
                or combat_evidence_id in output
                or sample_evidence_id in used_samples
            ):
                raise MapSceneContractError("camera combat anchor causality is invalid")
            output[combat_evidence_id] = (
                combat_frame,
                sample_evidence_id,
                sample_frame,
            )
            used_samples.add(sample_evidence_id)
            referenced_ids.update((combat_evidence_id, sample_evidence_id))
            ordered_keys.append(
                (
                    combat_frame,
                    combat_evidence_id,
                    sample_frame,
                    sample_evidence_id,
                )
            )
        if ordered_keys != sorted(ordered_keys):
            raise MapSceneContractError("camera combat anchors are not deterministic")
        evidence = tuple(getattr(value, "evidence", ()))
        if (
            {getattr(reference, "public_id", None) for reference in evidence}
            != referenced_ids
            or any(getattr(reference, "tier", None) != "observed" for reference in evidence)
        ):
            raise MapSceneContractError(
                "camera combat anchor evidence membership is inconsistent"
            )
        return output

    @staticmethod
    def _available_frame_end(replay: Replay, telemetry: TelemetryRun) -> int:
        return min(
            replay.frame_count,
            telemetry.final_frame if telemetry.final_frame is not None else replay.frame_count,
        )

    @staticmethod
    def _bounded_page_offset(total: int, page: int, page_size: int) -> int:
        if total == 0 or page > ((total - 1) // page_size) + 1:
            return total
        return (page - 1) * page_size

    @staticmethod
    def _evidence_rows(session: Session, evidence_ids: Collection[str]) -> tuple[EvidenceItem, ...]:
        """Load the complete fixed evidence set without exceeding SQLite bind limits."""
        ordered_ids = tuple(evidence_ids)
        rows_by_id: dict[str, EvidenceItem] = {}
        # TheSuperHackers @bugfix Leex 25/08/2026 Batch fixed-report evidence lookups while preserving the complete authority set. (#TBD)
        for offset in range(0, len(ordered_ids), 900):
            rows = session.scalars(
                select(EvidenceItem).where(EvidenceItem.public_id.in_(ordered_ids[offset : offset + 900]))
            )
            rows_by_id.update({row.public_id: row for row in rows})
        return tuple(rows_by_id[public_id] for public_id in ordered_ids if public_id in rows_by_id)

    def _authority(
        self, session: Session, query: MapSceneReadQuery, graph: Any
    ) -> tuple[Replay, Map, ParserRun, TelemetryRun, Any, frozenset[str]]:
        replay = session.scalar(select(Replay).where(Replay.public_id == query.replay_public_id))
        if replay is None or replay.map_id is None:
            raise MapSceneNotFoundError("replay has no accepted map projection")
        map_row = session.get(Map, replay.map_id)
        if map_row is None:
            raise MapSceneNotFoundError("replay map is missing")
        players = tuple(getattr(getattr(graph, "identity", None), "players", ()))
        player_ids = tuple(item.public_id for item in players)
        player_rows = tuple(
            session.scalars(
                select(ReplayPlayer).where(
                    ReplayPlayer.replay_id == replay.id,
                    ReplayPlayer.public_id.in_(player_ids),
                )
            )
        )
        parser_ids = {item.parser_run_id for item in player_rows}
        if len(player_rows) != len(player_ids) or len(parser_ids) != 1:
            raise MapSceneContractError("report players do not bind one parser authority")
        parser = session.get(ParserRun, next(iter(parser_ids)))
        if parser is None or parser.status != "succeeded" or parser.completion_status != "complete":
            raise MapSceneContractError("parser authority is not completed")
        document = graph.selected.document
        report_evidence = self._report_evidence(document)
        evidence_rows = self._evidence_rows(session, report_evidence)
        resolved = {item.public_id: item.tier for item in evidence_rows}
        if resolved != report_evidence:
            raise MapSceneContractError("report evidence membership is unresolved or inconsistent")
        replay_evidence = tuple(item for item in evidence_rows if item.replay_id == replay.id)
        telemetry_ids = {item.telemetry_run_id for item in replay_evidence if item.telemetry_run_id is not None}
        if len(telemetry_ids) != 1:
            raise MapSceneContractError("fixed report does not bind one telemetry authority")
        telemetry = session.get(TelemetryRun, next(iter(telemetry_ids)))
        if (
            telemetry is None
            or telemetry.status != "succeeded"
            or telemetry.trace_sha256 is None
            or telemetry.replay_id != replay.id
            or telemetry.map_id != map_row.id
            or telemetry.engine_build != map_row.engine_data_identity
            or not isinstance(telemetry.settings_json, Mapping)
            or telemetry.settings_json.get("parser_run_id") != parser.run_id
        ):
            raise MapSceneContractError("telemetry, parser, report, and map authorities disagree")
        manifest = session.scalar(
            select(TelemetryEvent)
            .where(TelemetryEvent.telemetry_run_id == telemetry.id, TelemetryEvent.event_type == "manifest")
        )
        if manifest is None:
            raise MapSceneContractError("selected telemetry has no manifest evidence")
        return replay, map_row, parser, telemetry, document, frozenset(report_evidence)

    # TheSuperHackers @performance Leex 25/08/2026 Validate index authority without hydrating a full map scene. (#TBD)
    def _index_authority(
        self, session: Session, query: MapSceneReadQuery, graph: Any
    ) -> dict[str, int]:
        replay, map_row, parser, telemetry, _, _ = self._authority(session, query, graph)
        manifest = self._manifest_evidence(session, telemetry)
        self._projection(session, map_row, manifest)
        self._player_map(session, replay, parser, telemetry)
        return {"frame_start": 0, "frame_end": self._available_frame_end(replay, telemetry)}

    @staticmethod
    def _manifest_evidence(session: Session, telemetry: TelemetryRun) -> EvidenceItem:
        rows = tuple(
            session.scalars(
                select(EvidenceItem)
                .join(TelemetryEvent, TelemetryEvent.evidence_item_id == EvidenceItem.id)
                .where(TelemetryEvent.telemetry_run_id == telemetry.id, TelemetryEvent.event_type == "manifest")
            )
        )
        # TheSuperHackers @fix Leex 25/08/2026 Accept only the unique manifest bound to the telemetry and replay already fixed by report evidence. (#TBD)
        if (
            len(rows) != 1
            or rows[0].tier != "observed"
            or rows[0].telemetry_run_id != telemetry.id
            or rows[0].replay_id != telemetry.replay_id
        ):
            raise MapSceneContractError("selected manifest evidence is ambiguous")
        return rows[0]

    @staticmethod
    def _movement_sample_frames(
        session: Session,
        telemetry: TelemetryRun,
        manifest: EvidenceItem,
    ) -> int:
        event = session.scalar(
            select(TelemetryEvent).where(
                TelemetryEvent.telemetry_run_id == telemetry.id,
                TelemetryEvent.event_type == "manifest",
                TelemetryEvent.evidence_item_id == manifest.id,
            )
        )
        if event is None:
            raise MapSceneContractError("selected manifest event is unavailable")
        exporter_settings = _mapping(
            _mapping(event.payload_json, "manifest payload").get("exporter_settings"),
            "manifest exporter settings",
        )
        interval = _integer(
            exporter_settings.get("movement_sample_frames"),
            "movement sample interval",
        )
        if not 1 <= interval <= 3600:
            raise MapSceneContractError("movement sample interval is outside the accepted domain")
        return interval

    def _projection(self, session: Session, map_row: Map, manifest: EvidenceItem) -> SpatialMapProjection:
        metadata = _mapping(map_row.metadata_json, "map metadata")
        raw = _mapping(metadata.get("validated_spatial_projection"), "validated spatial projection")
        pathing = _mapping(raw.get("pathing"), "pathing projection")
        bounds = _mapping(pathing.get("bounds"), "pathing bounds")
        minimum = _mapping(bounds.get("minimum_inclusive"), "pathing minimum")
        maximum = _mapping(bounds.get("maximum_exclusive"), "pathing maximum")
        cell = _mapping(pathing.get("cell_size"), "pathing cell size")
        origin = _mapping(pathing.get("index_origin"), "pathing origin")
        width = _integer(pathing.get("width"), "pathing width")
        height = _integer(pathing.get("height"), "pathing height")
        grid = GridSpec(
            width,
            height,
            _integer(origin.get("x"), "pathing origin x"),
            _integer(origin.get("y"), "pathing origin y"),
            _number(cell.get("x"), "pathing cell x"),
            _number(cell.get("y"), "pathing cell y"),
            _number(minimum.get("x"), "pathing minimum x"),
            _number(minimum.get("y"), "pathing minimum y"),
            _number(maximum.get("x"), "pathing maximum x"),
            _number(maximum.get("y"), "pathing maximum y"),
        )
        raw_world = _mapping(raw.get("world_bounds"), "world bounds")
        world_min = _mapping(raw_world.get("minimum"), "world minimum")
        world_max = _mapping(raw_world.get("maximum"), "world maximum")
        world = WorldBounds(
            Position3(*(_number(world_min.get(axis), f"world minimum {axis}") for axis in ("x", "y", "z"))),
            Position3(*(_number(world_max.get(axis), f"world maximum {axis}") for axis in ("x", "y", "z"))),
        )
        evidence = EvidenceRef(
            manifest.public_id,
            "observed",
            manifest.source_kind,
            manifest.source_key,
            str(manifest.schema_version),
        )
        starts: list[StartPosition] = []
        static_objects: list[StaticObjectFeature] = []
        rows = tuple(session.scalars(select(MapResource).where(MapResource.map_id == map_row.id)))
        for row in rows:
            if row.x is None or row.y is None or row.z is None:
                continue
            position = Position3(row.x, row.y, row.z)
            payload = _mapping(row.payload_json, "map resource payload")
            if row.resource_kind == "start_position":
                slots = payload.get("slot_indices")
                if not isinstance(slots, list) or any(type(item) is not int for item in slots):
                    raise MapSceneContractError("start position slots are malformed")
                starts.append(
                    StartPosition(
                        str(payload.get("name") or row.template_name or row.stable_key),
                        _integer(payload.get("waypoint_id"), "start waypoint"),
                        tuple(slots),
                        position,
                        evidence,
                    )
                )
            elif row.resource_kind == "static_object" and row.source_object_id is not None and row.template_name:
                categories_raw = payload.get("categories")
                if not isinstance(categories_raw, list):
                    continue
                categories = tuple(
                    StaticObjectCategory(str(_mapping(item, "static category").get("name")), str(_mapping(item, "static category").get("source")))
                    for item in categories_raw
                )
                static_objects.append(StaticObjectFeature(row.source_object_id, row.template_name, position, categories, evidence))
        ground_raw = raw.get("ground_passable")
        amphibious_raw = raw.get("amphibious_passable")
        zones_raw = raw.get("zone_ids")
        if not isinstance(ground_raw, list) or not isinstance(amphibious_raw, list) or not isinstance(zones_raw, list):
            raise MapSceneContractError("persisted pathability projection is missing")
        projection = SpatialMapProjection(
            _integer(raw.get("schema_version"), "map schema"),
            str(raw.get("content_sha256")),
            str(raw.get("map_identity")),
            str(raw.get("engine_data_identity")),
            grid,
            world,
            tuple(ground_raw),
            tuple(amphibious_raw),
            tuple(zones_raw),
            tuple(starts),
            tuple(static_objects),
        )
        checked = validate_map_projection(projection)
        if isinstance(checked, SpatialUnavailable):
            raise MapSceneContractError(checked.reason)
        if (
            projection.content_sha256 != map_row.content_sha256
            or projection.schema_version != map_row.schema_version
            or projection.engine_data_identity != map_row.engine_data_identity
            or projection.map_identity != map_row.map_identity
        ):
            raise MapSceneContractError("persisted map row and semantic projection disagree")
        return projection

    @staticmethod
    def _position(position: Position3, projection: SpatialMapProjection, transform: PlayerTransform | None) -> dict[str, object]:
        normalized = world_to_map_normalized(position, projection.world_bounds)
        if isinstance(normalized, SpatialUnavailable):
            raise MapSceneContractError(normalized.reason)
        centered = transform.apply(position) if transform is not None else None
        return {
            "raw": {"x": position.x, "y": position.y, "z": position.z},
            "map_normalized": {"u": normalized.u, "v": normalized.v},
            "player_centric": None
            if centered is None
            else {"forward": centered.x, "left": centered.y, "z": centered.z},
        }

    @staticmethod
    # TheSuperHackers @bugfix Leex 25/08/2026 Resolve scene identities from the selected engine player slots without mutating parser rows. (#TBD)
    def _player_map(
        session: Session, replay: Replay, parser: ParserRun, telemetry: TelemetryRun
    ) -> dict[int, ReplayPlayer]:
        rows = tuple(
            session.scalars(
                select(ReplayPlayer).where(
                    ReplayPlayer.replay_id == replay.id, ReplayPlayer.parser_run_id == parser.id
                )
            )
        )
        by_slot = {item.slot_index: item for item in rows}
        if len(by_slot) != len(rows):
            raise MapSceneContractError("replay player slots are ambiguous")
        initialization = tuple(
            session.scalars(
                select(TelemetryEvent).where(
                    TelemetryEvent.telemetry_run_id == telemetry.id,
                    TelemetryEvent.event_type == "players_initialized",
                )
            )
        )
        if len(initialization) != 1:
            raise MapSceneContractError("telemetry player initialization is missing or ambiguous")
        raw_slots = _mapping(initialization[0].payload_json, "players_initialized payload").get("slots")
        if not isinstance(raw_slots, list):
            raise MapSceneContractError("telemetry player initialization slots are invalid")
        output: dict[int, ReplayPlayer] = {}
        resolved_slots: set[int] = set()
        for raw_slot in raw_slots:
            if not isinstance(raw_slot, Mapping) or raw_slot.get("resolution_status") != "resolved":
                continue
            slot_index = raw_slot.get("slot_index")
            player_index = raw_slot.get("player_index")
            if type(slot_index) is not int or type(player_index) is not int:
                raise MapSceneContractError("telemetry player initialization identity is invalid")
            player = by_slot.get(slot_index)
            if player is None or slot_index in resolved_slots or player_index in output:
                raise MapSceneContractError("telemetry player initialization identity is ambiguous")
            resolved_slots.add(slot_index)
            output[player_index] = player
        if not output:
            raise MapSceneContractError("telemetry player initialization identities are unresolved")
        return output

    def _transforms(
        self,
        projection: SpatialMapProjection,
        players: Mapping[int, ReplayPlayer],
        manifest: EvidenceItem,
        subject: str | None,
    ) -> tuple[list[dict[str, object]], PlayerTransform | None, list[str]]:
        output: list[dict[str, object]] = []
        selected: PlayerTransform | None = None
        reasons: list[str] = []
        for player in sorted(players.values(), key=lambda item: item.public_id):
            own = next((start for start in projection.start_positions if player.slot_index in start.slot_indices), None)
            if own is None:
                reasons.append("unresolved_player_transform")
                continue
            enemies = tuple(start for start in projection.start_positions if start != own)
            transform = player_centric_transform(own, enemies)
            if isinstance(transform, SpatialUnavailable):
                reasons.append(transform.reason)
                continue
            own_id = _public_id("start", projection.content_sha256, own.waypoint_id, own.name)
            enemy_id = _public_id(
                "start", projection.content_sha256, transform.enemy_start.waypoint_id, transform.enemy_start.name
            )
            output.append(
                {
                    "transform_version": "player-centric-v1",
                    "subject_replay_player_public_id": player.public_id,
                    "own_start_public_id": own_id,
                    "reference_enemy_start_public_id": enemy_id,
                    "angle_radians": transform.angle_radians,
                    "availability": _availability("available", evidence=(manifest.public_id,)),
                    "evidence": _evidence(manifest.public_id),
                }
            )
            if player.public_id == subject:
                selected = transform
        return output, selected, reasons

    def _raster_descriptor(self, map_row: Map, projection: SpatialMapProjection, surface: str) -> dict[str, object]:
        values = projection.ground_passable if surface == "ground" else projection.amphibious_passable
        png = _pathability_png(projection.pathing.width, projection.pathing.height, values)
        return {
            "raster_public_id": self.raster_public_id(map_row.public_id, "pathability", surface),
            "kind": "pathability",
            "locomotor_surface": surface,
            "rasterization_version": "map-grid-raster-v1",
            "media_type": "image/png",
            "width": projection.pathing.width,
            "height": projection.pathing.height,
            "content_sha256": hashlib.sha256(png).hexdigest(),
            "placement": {
                "raw_minimum_x": projection.pathing.minimum_x,
                "raw_minimum_y": projection.pathing.minimum_y,
                "raw_maximum_x": projection.pathing.maximum_x,
                "raw_maximum_y": projection.pathing.maximum_y,
                "grid_width": projection.pathing.width,
                "grid_height": projection.pathing.height,
                "source_storage_order": "row_major_y_then_x_x_fastest",
                "source_row_zero": "minimum_world_y",
                "png_row_zero": "maximum_world_y",
                "display_interpolation": "nearest",
            },
            "availability": _availability("available"),
        }

    def _samples(
        self,
        session: Session,
        telemetry: TelemetryRun,
        players: Mapping[int, ReplayPlayer],
        query: MapSceneReadQuery,
        projection: SpatialMapProjection,
        report_evidence_ids: frozenset[str],
    ) -> tuple[tuple[SceneSample, ...], tuple[str, ...]]:
        if query.event_families and "samples" not in query.event_families:
            return (), ()
        rows = tuple(
            session.execute(
                select(EntitySample, Entity, EvidenceItem)
                .join(Entity, Entity.id == EntitySample.entity_id)
                .join(TelemetryEvent, TelemetryEvent.id == EntitySample.telemetry_event_id)
                .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                .where(
                    EntitySample.telemetry_run_id == telemetry.id,
                    EntitySample.frame >= query.frame_start,
                    EntitySample.frame <= query.frame_end,
                )
            )
        )
        output = []
        reasons: set[str] = set()
        for row, entity, evidence in rows:
            if (
                row.sample_reason not in _SAMPLE_REASONS
                or evidence.tier != "observed"
                or evidence.public_id not in report_evidence_ids
            ):
                continue
            player = players.get(entity.initial_owner_player_index) if entity.initial_owner_player_index is not None else None
            if query.replay_player_public_ids and (player is None or player.public_id not in query.replay_player_public_ids):
                continue
            if query.entity_public_ids and entity.public_id not in query.entity_public_ids:
                continue
            if query.locomotor_surface is not None:
                reasons.add("unsupported_or_unproven_locomotor_surface")
                continue
            raw_position = Position3(row.x, row.y, row.z)
            if isinstance(world_to_map_normalized(raw_position, projection.world_bounds), SpatialUnavailable):
                reasons.add("map_coordinate_out_of_bounds")
                continue
            output.append(
                SceneSample(
                    _public_id("sample", telemetry.run_id, entity.public_id, row.sequence),
                    entity.public_id,
                    None if player is None else player.public_id,
                    row.frame,
                    (raw_position.x, raw_position.y, raw_position.z),
                    row.orientation,
                    row.sample_reason,
                    None,
                    evidence.public_id,
                )
            )
        return tuple(sorted(output, key=_sample_key)), tuple(sorted(reasons))

    # TheSuperHackers @feature Leex 23/08/2026 Query one completed report-scoped map scene without sidecar or latest-report fallback. (#TBD)
    def get_scene(self, query: MapSceneReadQuery) -> MapSceneReadModel:
        if type(query) is not MapSceneReadQuery:
            raise TypeError("query must be a MapSceneReadQuery")
        graph = self._report_graph(query)
        return self._get_scene(query, graph)

    # TheSuperHackers @feature Leex 24/08/2026 Expose the report-authoritative map horizon to offline broadcast consumers. (#TBD)
    def get_canonical_scene(self, query: MapSceneReadQuery) -> MapSceneReadModel:
        """Return the fixed report scene across exactly its available telemetry window."""
        if type(query) is not MapSceneReadQuery:
            raise TypeError("query must be a MapSceneReadQuery")
        graph = self._report_graph(query)
        return self._get_scene(query, graph, canonical_index_window=True)

    def _get_scene(
        self,
        query: MapSceneReadQuery,
        graph: Any,
        *,
        canonical_index_window: bool = False,
    ) -> MapSceneReadModel:
        with self._session_factory() as session:
            replay, map_row, parser, telemetry, document, report_evidence_ids = self._authority(
                session, query, graph
            )
            camera_combat_anchors = self._camera_combat_anchors(document)
            available_end = self._available_frame_end(replay, telemetry)
            if canonical_index_window:
                query = replace(query, frame_start=0, frame_end=available_end)
            elif query.frame_end > available_end:
                raise ValueError("map scene query exceeds the available frame window")
            manifest = self._manifest_evidence(session, telemetry)
            movement_sample_frames = self._movement_sample_frames(
                session, telemetry, manifest
            )
            scene_evidence_ids = report_evidence_ids | {manifest.public_id}
            projection = self._projection(session, map_row, manifest)
            players = self._player_map(session, replay, parser, telemetry)
            available_player_ids = {item.public_id for item in players.values()}
            if not set(query.replay_player_public_ids).issubset(available_player_ids):
                raise MapSceneNotFoundError("player filter is outside the fixed scene")
            subject = query.player_centric_subject_public_id
            transforms, selected_transform, transform_reasons = self._transforms(
                projection, players, manifest, subject
            )
            if query.coordinate_display == "player_centric" and selected_transform is None:
                raise ValueError("player-centric transform is unavailable")
            raw_samples, sample_reasons = self._samples(
                session, telemetry, players, query, projection, report_evidence_ids
            )
            entity_rows = tuple(
                session.scalars(select(Entity).where(Entity.telemetry_run_id == telemetry.id))
            )
            entity_ids_by_public = {item.public_id: item.id for item in entity_rows}
            entities_by_id = {item.id: item for item in entity_rows}
            entities_by_object_id = {item.object_id: item for item in entity_rows}
            if not set(query.entity_public_ids).issubset(entity_ids_by_public):
                raise MapSceneNotFoundError("entity filter is outside the fixed scene")
            samples, downsampling = downsample_samples(raw_samples, query.sample_budget)
            sample_values = []
            omitted_reasons = [
                "terrain_grid_not_persisted",
                "normalized_order_target_not_persisted",
                "fixed_report_route_projection_unavailable",
                "fixed_report_engagement_projection_unavailable",
                "presence_eligibility_not_persisted",
                *sample_reasons,
            ]
            for item in samples:
                position = Position3(*item.raw_position)
                semantic_position = self._position(position, projection, selected_transform)
                sample_values.append(
                    {
                        "sample_public_id": item.sample_public_id,
                        "entity_public_id": item.entity_public_id,
                        "replay_player_public_id": item.replay_player_public_id,
                        "frame": item.frame,
                        "position": semantic_position,
                        "orientation": item.orientation,
                        "sample_reason": item.sample_reason,
                        "locomotor_surface": None,
                        "evidence": _evidence(item.evidence_public_id),
                    }
                )

            # TheSuperHackers @feature Leex 24/08/2026 Project engine-native visibility and bounded AI grid evidence into the fixed replay map scene. (#TBD)
            include_visibility = not query.event_families or "visibility" in query.event_families
            include_engine_heuristics = query.include_engine_heuristics and (
                not query.event_families or "engine_heuristics" in query.event_families
            )
            native_event_types: list[str] = []
            if include_visibility:
                native_event_types.extend(("object_visibility_changed", "visibility_sampling_summary"))
            if include_engine_heuristics:
                native_event_types.append("partition_engine_grid_sample")
            native_rows = (
                tuple(
                    session.execute(
                        select(TelemetryEvent, EvidenceItem)
                        .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                        .where(
                            TelemetryEvent.telemetry_run_id == telemetry.id,
                            TelemetryEvent.frame >= query.frame_start,
                            TelemetryEvent.frame <= query.frame_end,
                            TelemetryEvent.event_type.in_(native_event_types),
                        )
                        .order_by(TelemetryEvent.frame, TelemetryEvent.sequence)
                    )
                )
                if native_event_types
                else ()
            )
            visibility_values: list[dict[str, object]] = []
            visibility_summary_values: list[dict[str, object]] = []
            latest_heuristic_rows: dict[str, tuple[TelemetryEvent, EvidenceItem]] = {}
            native_evidence_ids: list[str] = []
            for event, evidence in native_rows:
                if evidence.tier != "observed" or evidence.public_id not in report_evidence_ids:
                    continue
                payload = _mapping(event.payload_json, f"{event.event_type} payload")
                if event.event_type == "object_visibility_changed":
                    player = players.get(_integer(payload.get("player_index"), "visibility player index"))
                    entity = entities_by_object_id.get(
                        _integer(payload.get("object_id"), "visibility object id")
                    )
                    if player is None or entity is None:
                        raise MapSceneContractError("visibility identities are unresolved")
                    if query.replay_player_public_ids and player.public_id not in query.replay_player_public_ids:
                        continue
                    if query.entity_public_ids and entity.public_id not in query.entity_public_ids:
                        continue
                    position_payload = _mapping(payload.get("position"), "visibility position")
                    raw_position = Position3(
                        _number(position_payload.get("x"), "visibility x"),
                        _number(position_payload.get("y"), "visibility y"),
                        _number(position_payload.get("z"), "visibility z"),
                    )
                    previous_status = _string(payload.get("previous_status"), "previous visibility status")
                    status = _string(payload.get("status"), "visibility status")
                    if previous_status not in {"unseen", "clear", "fogged", "shrouded"} or status not in {
                        "clear",
                        "fogged",
                        "shrouded",
                    }:
                        raise MapSceneContractError("visibility status is outside the accepted domain")
                    first_observed_clear = _boolean(
                        payload.get("first_observed_clear"), "first observed clear"
                    )
                    sampling_cycle_id = _integer(
                        payload.get("sampling_cycle_id"), "visibility sampling cycle"
                    )
                    template_name = _string(payload.get("template_name"), "visibility template")
                    # TheSuperHackers @bugfix Leex 25/08/2026 Preserve valid visibility evidence while omitting engine observations outside the map bounds. (#TBD)
                    if isinstance(world_to_map_normalized(raw_position, projection.world_bounds), SpatialUnavailable):
                        omitted_reasons.append("map_coordinate_out_of_bounds")
                        continue
                    visibility_values.append(
                        {
                            "visibility_public_id": _public_id(
                                "visibility", telemetry.run_id, event.sequence
                            ),
                            "replay_player_public_id": player.public_id,
                            "entity_public_id": entity.public_id,
                            "frame": event.frame,
                            "template_name": template_name,
                            "previous_status": previous_status,
                            "status": status,
                            "first_observed_clear": first_observed_clear,
                            "position": self._position(raw_position, projection, selected_transform),
                            "sampling_cycle_id": sampling_cycle_id,
                            "evidence": _evidence(evidence.public_id),
                        }
                    )
                    native_evidence_ids.append(evidence.public_id)
                elif event.event_type == "visibility_sampling_summary":
                    cycle_complete = _boolean(payload.get("cycle_complete"), "visibility cycle complete")
                    visibility_summary_values.append(
                        {
                            "summary_public_id": _public_id(
                                "visibility-summary", telemetry.run_id, event.sequence
                            ),
                            "frame": event.frame,
                            "eligible_pair_count": _integer(
                                payload.get("eligible_pair_count"), "eligible visibility pairs"
                            ),
                            "sampled_pair_count": _integer(
                                payload.get("sampled_pair_count"), "sampled visibility pairs"
                            ),
                            "maximum_pairs_per_pass": _integer(
                                payload.get("maximum_pairs_per_pass"), "maximum visibility pairs"
                            ),
                            "sampling_cycle_id": _integer(
                                payload.get("sampling_cycle_id"), "visibility summary cycle"
                            ),
                            "cycle_complete": cycle_complete,
                            "coverage_state": "complete" if cycle_complete else "incomplete",
                            "evidence": _evidence(evidence.public_id),
                        }
                    )
                    native_evidence_ids.append(evidence.public_id)
                elif include_engine_heuristics:
                    player = players.get(_integer(payload.get("player_index"), "heuristic player index"))
                    if player is None:
                        raise MapSceneContractError("heuristic player identity is unresolved")
                    if query.replay_player_public_ids and player.public_id not in query.replay_player_public_ids:
                        continue
                    latest_heuristic_rows[player.public_id] = (event, evidence)

            heuristic_values: list[dict[str, object]] = []
            for player_public_id, (event, evidence) in sorted(latest_heuristic_rows.items()):
                payload = _mapping(event.payload_json, "partition heuristic payload")
                grid = _mapping(payload.get("grid"), "partition heuristic grid")
                raw_cells = payload.get("cells")
                if not isinstance(raw_cells, list) or not raw_cells or len(raw_cells) > 128:
                    raise MapSceneContractError("partition heuristic cells are not bounded")
                cell_values: list[dict[str, object]] = []
                for raw_cell in raw_cells:
                    cell = _mapping(raw_cell, "partition heuristic cell")
                    position_payload = _mapping(cell.get("world_position"), "partition heuristic position")
                    raw_position = Position3(
                        _number(position_payload.get("x"), "partition heuristic x"),
                        _number(position_payload.get("y"), "partition heuristic y"),
                        _number(position_payload.get("z"), "partition heuristic z"),
                    )
                    cell_values.append(
                        {
                            "cell_x": _integer(cell.get("cell_x"), "partition cell x"),
                            "cell_y": _integer(cell.get("cell_y"), "partition cell y"),
                            "position": self._position(raw_position, projection, selected_transform),
                            "shroud_status": _string(cell.get("shroud_status"), "partition shroud status"),
                            "threat_value": _integer(cell.get("threat_value"), "partition threat value"),
                            "cash_value": _integer(cell.get("cash_value"), "partition cash value"),
                            "evidence": _evidence(evidence.public_id),
                        }
                    )
                heuristic_values.append(
                    {
                        "overlay_public_id": _public_id(
                            "engine-heuristic-overlay", telemetry.run_id, event.sequence
                        ),
                        "replay_player_public_id": player_public_id,
                        "frame": event.frame,
                        "sampling_scheme": _string(
                            payload.get("sampling_scheme"), "partition sampling scheme"
                        ),
                        "grid_complete": _boolean(grid.get("complete"), "partition grid complete"),
                        "threat_label": "Engine AI threat heuristic",
                        "cash_label": "Engine AI cash-value heuristic",
                        "cells": cell_values,
                        "evidence": _evidence(evidence.public_id),
                    }
                )
                native_evidence_ids.append(evidence.public_id)
            if include_visibility and not visibility_values:
                omitted_reasons.append("visibility_observations_unavailable")
            if include_visibility and any(
                item["coverage_state"] == "incomplete" for item in visibility_summary_values
            ):
                omitted_reasons.append("visibility_sampling_incomplete")
            if include_engine_heuristics and not heuristic_values:
                omitted_reasons.append("engine_heuristic_samples_unavailable")
            starts = [
                {
                    "start_public_id": _public_id("start", projection.content_sha256, item.waypoint_id, item.name),
                    "name": item.name,
                    "replay_player_public_ids": sorted(
                        player.public_id for player in players.values() if player.slot_index in item.slot_indices
                    ),
                    "position": self._position(item.position, projection, selected_transform),
                    "evidence": _evidence(manifest.public_id),
                }
                for item in projection.start_positions
            ]
            resources = []
            structures = []
            for static_item in projection.static_objects:
                position_value = self._position(static_item.position, projection, selected_transform)
                structure_id = _public_id("structure", projection.content_sha256, static_item.object_id)
                structures.append(
                    {
                        "structure_public_id": structure_id,
                        "source_kind": "map_static",
                        "replay_player_public_id": None,
                        "template_name": static_item.template_name,
                        "frame": None,
                        "position": position_value,
                        "availability": _availability("available", evidence=(manifest.public_id,)),
                        "evidence": _evidence(manifest.public_id),
                    }
                )
                for category in static_item.categories:
                    if category.name in _RESOURCE_KINDS:
                        resources.append(
                            {
                                "resource_public_id": _public_id(
                                    "resource", projection.content_sha256, static_item.object_id, category.name
                                ),
                                "resource_kind": category.name,
                                "label": static_item.template_name,
                                "position": position_value,
                                "amount": None,
                                "availability": _availability("available", evidence=(manifest.public_id,)),
                                "evidence": _evidence(manifest.public_id),
                            }
                        )
            # TheSuperHackers @feature Leex 25/08/2026 Project report-cited completed buildings at their engine-observed creation positions for early broadcast focus. (#TBD)
            if not query.event_families or "structures" in query.event_families:
                completion_rows = tuple(
                    session.execute(
                        select(TelemetryEvent, EvidenceItem)
                        .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                        .where(
                            TelemetryEvent.telemetry_run_id == telemetry.id,
                            TelemetryEvent.event_type == "construction_completed",
                            TelemetryEvent.frame >= query.frame_start,
                            TelemetryEvent.frame <= query.frame_end,
                        )
                        .order_by(TelemetryEvent.frame, TelemetryEvent.sequence)
                    )
                )
                creation_rows = tuple(
                    session.execute(
                        select(TelemetryEvent, EvidenceItem)
                        .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                        .where(
                            TelemetryEvent.telemetry_run_id == telemetry.id,
                            TelemetryEvent.event_type == "object_created",
                        )
                        .order_by(TelemetryEvent.sequence)
                    )
                )
                creations_by_sequence = {
                    event.sequence: (event, evidence) for event, evidence in creation_rows
                }
                for completion, completion_evidence in completion_rows:
                    payload = _mapping(
                        completion.payload_json, "construction completed payload"
                    )
                    object_id = _integer(payload.get("object_id"), "construction object id")
                    entity = entities_by_object_id.get(object_id)
                    if entity is None or entity.creation_sequence is None:
                        continue
                    creation = creations_by_sequence.get(entity.creation_sequence)
                    if (
                        completion_evidence.tier != "observed"
                        or creation is None
                        or completion_evidence.public_id not in report_evidence_ids
                    ):
                        continue
                    creation_event, creation_evidence = creation
                    if (
                        creation_evidence.tier != "observed"
                        or creation_evidence.public_id not in report_evidence_ids
                    ):
                        continue
                    owner_player_index = payload.get("owner_player_index")
                    responsible_player_index = payload.get("responsible_player_index")
                    if owner_player_index is not None and type(owner_player_index) is not int:
                        raise MapSceneContractError("construction owner identity is invalid")
                    if responsible_player_index is not None and type(responsible_player_index) is not int:
                        raise MapSceneContractError("construction responsible identity is invalid")
                    if (
                        owner_player_index is not None
                        and responsible_player_index is not None
                        and owner_player_index != responsible_player_index
                    ):
                        raise MapSceneContractError("construction attribution is ambiguous")
                    attributed_player_index = (
                        responsible_player_index
                        if responsible_player_index is not None
                        else owner_player_index
                    )
                    player = (
                        players.get(attributed_player_index)
                        if attributed_player_index is not None
                        else None
                    )
                    if attributed_player_index is not None and player is None:
                        raise MapSceneContractError("construction attribution is unresolved")
                    if query.replay_player_public_ids and (
                        player is None
                        or player.public_id not in query.replay_player_public_ids
                    ):
                        continue
                    if query.entity_public_ids and entity.public_id not in query.entity_public_ids:
                        continue
                    creation_payload = _mapping(
                        creation_event.payload_json, "object created payload"
                    )
                    if (
                        creation_payload.get("object_id") != object_id
                        or creation_event.frame != entity.creation_frame
                    ):
                        raise MapSceneContractError("construction creation identity is inconsistent")
                    if creation_payload.get("position_status") != "placed":
                        continue
                    construction_position = creation_payload.get("position")
                    if not isinstance(construction_position, Mapping):
                        continue
                    position = Position3(
                        _number(construction_position.get("x"), "construction position x"),
                        _number(construction_position.get("y"), "construction position y"),
                        _number(construction_position.get("z"), "construction position z"),
                    )
                    if isinstance(
                        world_to_map_normalized(position, projection.world_bounds),
                        SpatialUnavailable,
                    ):
                        omitted_reasons.append("map_coordinate_out_of_bounds")
                        continue
                    milestone_evidence_ids = tuple(
                        sorted(
                            (
                                creation_evidence.public_id,
                                completion_evidence.public_id,
                            )
                        )
                    )
                    structures.append(
                        {
                            "structure_public_id": entity.public_id,
                            "source_kind": "construction_completed",
                            "replay_player_public_id": (
                                None if player is None else player.public_id
                            ),
                            "template_name": entity.template_name,
                            "frame": completion.frame,
                            "position": self._position(
                                position, projection, selected_transform
                            ),
                            "availability": _availability(
                                "available", evidence=milestone_evidence_ids
                            ),
                            "evidence": [
                                {
                                    "evidence_public_id": creation_evidence.public_id,
                                    "tier": "observed",
                                    "support_role": "position",
                                    "observed_frame": creation_event.frame,
                                },
                                {
                                    "evidence_public_id": completion_evidence.public_id,
                                    "tier": "observed",
                                    "support_role": "event_timing",
                                    "observed_frame": completion.frame,
                                },
                            ],
                        }
                    )
            combat_rows = tuple(
                session.execute(
                    select(CombatEvent, EvidenceItem)
                    .join(TelemetryEvent, TelemetryEvent.id == CombatEvent.telemetry_event_id)
                    .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                    .where(
                        CombatEvent.telemetry_run_id == telemetry.id,
                        CombatEvent.frame >= query.frame_start,
                        CombatEvent.frame <= query.frame_end,
                    )
                    .order_by(CombatEvent.frame, CombatEvent.id)
                )
            )
            authorized_sample_evidence_ids = {
                sample_evidence_id
                for _, sample_evidence_id, _ in camera_combat_anchors.values()
            }
            attacker_sample_rows = (
                tuple(
                    session.execute(
                        select(EntitySample, EvidenceItem)
                        .join(
                            TelemetryEvent,
                            TelemetryEvent.id == EntitySample.telemetry_event_id,
                        )
                        .join(
                            EvidenceItem,
                            EvidenceItem.id == TelemetryEvent.evidence_item_id,
                        )
                        .where(
                            EntitySample.telemetry_run_id == telemetry.id,
                            EvidenceItem.public_id.in_(authorized_sample_evidence_ids),
                        )
                    )
                )
                if authorized_sample_evidence_ids
                else ()
            )
            attacker_samples: dict[str, tuple[EntitySample, EvidenceItem]] = {}
            for sample, sample_evidence in attacker_sample_rows:
                if sample_evidence.public_id in attacker_samples:
                    raise MapSceneContractError(
                        "camera attacker sample evidence is ambiguous"
                    )
                attacker_samples[sample_evidence.public_id] = (
                    sample,
                    sample_evidence,
                )
            if set(attacker_samples) != authorized_sample_evidence_ids:
                raise MapSceneContractError(
                    "camera attacker sample evidence is unresolved"
                )
            casualties = []
            player_by_id = {item.id: item for item in players.values()}
            include_casualties = not query.event_families or "casualties" in query.event_families
            selected_entity_ids = {
                entity_ids_by_public[public_id] for public_id in query.entity_public_ids
            }
            for combat, evidence in combat_rows:
                if (
                    combat.location_x is None
                    or combat.location_y is None
                    or combat.location_z is None
                    or evidence.tier != "observed"
                    or evidence.public_id not in report_evidence_ids
                ):
                    continue
                attacker = player_by_id.get(combat.attacker_replay_player_id)
                victim = player_by_id.get(combat.victim_replay_player_id)
                participant_ids = {
                    item.public_id for item in (attacker, victim) if item is not None
                }
                if query.replay_player_public_ids and not participant_ids.intersection(
                    query.replay_player_public_ids
                ):
                    continue
                if selected_entity_ids and not selected_entity_ids.intersection(
                    (combat.attacker_entity_id, combat.victim_entity_id)
                ):
                    continue
                location = Position3(combat.location_x, combat.location_y, combat.location_z)
                if combat.killing_blow is True and include_casualties:
                    if isinstance(
                        world_to_map_normalized(location, projection.world_bounds),
                        SpatialUnavailable,
                    ):
                        omitted_reasons.append("map_coordinate_out_of_bounds")
                        continue
                    opposing_position = None
                    casualty_evidence: list[dict[str, object]] = [
                        dict(item) for item in _evidence(evidence.public_id)
                    ]
                    anchor = camera_combat_anchors.get(evidence.public_id)
                    if anchor is not None:
                        anchor_frame, sample_evidence_id, anchor_sample_frame = anchor
                        sample, sample_evidence = attacker_samples[sample_evidence_id]
                        attacker_entity = (
                            None
                            if combat.attacker_entity_id is None
                            else entities_by_id.get(combat.attacker_entity_id)
                        )
                        if (
                            anchor_frame != combat.frame
                            or sample.frame != anchor_sample_frame
                            or sample.frame > combat.frame
                            or sample.entity_id != combat.attacker_entity_id
                            or attacker_entity is None
                            or (
                                attacker_entity.destruction_frame is not None
                                and attacker_entity.destruction_frame < combat.frame
                            )
                        ):
                            raise MapSceneContractError(
                                "camera combat anchor no longer matches causal telemetry"
                            )
                        sample_payload = _mapping(
                            sample.payload_json, "attacker sample payload"
                        )
                        sample_is_moving = sample_payload.get("is_engine_moving")
                        sample_is_current = sample_is_moving is False or (
                            combat.frame - sample.frame <= movement_sample_frames
                        )
                        if (
                            sample_evidence.tier != "observed"
                            or sample_evidence.public_id not in report_evidence_ids
                        ):
                            raise MapSceneContractError(
                                "camera attacker sample is outside fixed report evidence"
                            )
                        if not sample_is_current:
                            raise MapSceneContractError(
                                "camera attacker sample is no longer causally current"
                            )
                        raw_opposing = Position3(sample.x, sample.y, sample.z)
                        if (
                            not isinstance(
                                world_to_map_normalized(
                                    raw_opposing, projection.world_bounds
                                ),
                                SpatialUnavailable,
                            )
                        ):
                            opposing_position = self._position(
                                raw_opposing, projection, selected_transform
                            )
                            # TheSuperHackers @feature Leex 25/08/2026 Cite the causal attacker sample separately from the killing-blow timing used for two-sided combat framing. (#TBD)
                            casualty_evidence = [
                                {
                                    "evidence_public_id": sample_evidence.public_id,
                                    "tier": "observed",
                                    "support_role": "position",
                                    "observed_frame": sample.frame,
                                },
                                {
                                    "evidence_public_id": evidence.public_id,
                                    "tier": "observed",
                                    "support_role": "event_timing",
                                    "observed_frame": combat.frame,
                                },
                            ]
                    casualties.append(
                        {
                            "casualty_public_id": _public_id("casualty", telemetry.run_id, evidence.public_id),
                            "frame": combat.frame,
                            "victim_replay_player_public_id": None if victim is None else victim.public_id,
                            "attacker_replay_player_public_id": None if attacker is None else attacker.public_id,
                            "position": self._position(location, projection, selected_transform),
                            "opposing_position": opposing_position,
                            "evidence": casualty_evidence,
                        }
                    )
            engagements: list[dict[str, object]] = []
            if transform_reasons:
                omitted_reasons.extend(transform_reasons)
            raster_values = [
                self._raster_descriptor(map_row, projection, "ground"),
                self._raster_descriptor(map_row, projection, "amphibious"),
            ]
            issue_values = [
                {
                    "code": issue.issue_code,
                    "message": issue.issue_code.replace("_", " "),
                    "evidence_references": (),
                }
                for issue in getattr(document, "quality_issues", ())
            ]
            query_value = {
                "replay_public_id": query.replay_public_id,
                "report_public_id": query.report_public_id,
                "frame_start": query.frame_start,
                "frame_end": query.frame_end,
                "replay_player_public_ids": list(query.replay_player_public_ids),
                "entity_public_ids": list(query.entity_public_ids),
                "event_families": list(query.event_families),
                "locomotor_surface": query.locomotor_surface,
                "coordinate_display": query.coordinate_display,
                "player_centric_subject_public_id": query.player_centric_subject_public_id,
                "sample_budget": query.sample_budget,
                "include_engine_heuristics": query.include_engine_heuristics,
            }
            evidence_ids = [
                manifest.public_id,
                *(item.evidence_public_id for item in samples),
                *native_evidence_ids,
            ]
            state = "partial" if omitted_reasons else "available"
            payload = {
                "schema_version": "replay-map-scene-v2",
                "replay_public_id": replay.public_id,
                "report_public_id": document.report_public_id,
                "report_version": document.report_version,
                "telemetry_run_public_id": telemetry.run_id,
                "telemetry_trace_sha256": telemetry.trace_sha256,
                "map_public_id": map_row.public_id,
                "map_display_name": map_row.display_name or replay.map_name,
                "map_content_sha256": map_row.content_sha256,
                "map_schema_version": map_row.schema_version,
                "engine_data_identity": map_row.engine_data_identity,
                "query": query_value,
                "available_frame_window": {"frame_start": 0, "frame_end": available_end},
                "transforms": {
                    "raw": {
                        "coordinate_version": "engine-world-xyz-v1",
                        "axes": ["engine_world_x", "engine_world_y", "engine_world_z"],
                        "units": "engine_world_unit",
                        "minimum": {
                            "x": projection.world_bounds.minimum.x,
                            "y": projection.world_bounds.minimum.y,
                            "z": projection.world_bounds.minimum.z,
                        },
                        "maximum": {
                            "x": projection.world_bounds.maximum.x,
                            "y": projection.world_bounds.maximum.y,
                            "z": projection.world_bounds.maximum.z,
                        },
                        "minimum_inclusive": True,
                        "maximum_inclusive": True,
                    },
                    "map_normalized": {
                        "transform_version": "map-normalized-v1",
                        "formula": "u=(x-min_x)/(max_x-min_x);v=(y-min_y)/(max_y-min_y)",
                        "availability": _availability("available", evidence=(manifest.public_id,)),
                    },
                    "player_centric": transforms,
                },
                "rasters": raster_values,
                "starts": starts,
                "resources": resources,
                "structures": structures if not query.event_families or "structures" in query.event_families else [],
                "samples": sample_values,
                "orders": [],
                "routes": [],
                "engagements": engagements,
                "casualties": casualties,
                "control_windows": [],
                "visibility_transitions": visibility_values,
                "visibility_sampling_summaries": visibility_summary_values,
                "engine_heuristic_overlays": heuristic_values,
                "downsampling": downsampling,
                "availability": _availability(state, omitted_reasons, evidence_ids),
                "terminal_quality": {
                    "lifecycle": document.lifecycle.lifecycle_state,
                    "issues": issue_values,
                    "engine_run_status": document.lifecycle.telemetry_runner_status,
                    "strategy_analysis_scope": "replay" if document.replay_player_public_id is None else "player",
                },
            }
            _assert_evidence_membership(payload, frozenset(scene_evidence_ids))
            return MapSceneReadModel(freeze_canonical(payload))

    # TheSuperHackers @feature Leex 23/08/2026 Regenerate only accepted persisted pathability rasters by stable public identity. (#TBD)
    def get_raster(self, query: MapRasterReadQuery) -> MapRasterReadModel:
        if type(query) is not MapRasterReadQuery:
            raise TypeError("query must be a MapRasterReadQuery")
        with self._session_factory() as session:
            map_row = session.scalar(select(Map).where(Map.public_id == query.map_public_id))
            if map_row is None:
                raise MapSceneNotFoundError("map was not found")
            terrain_id = self.raster_public_id(map_row.public_id, "terrain_cell_type", None)
            if query.raster_public_id == terrain_id:
                raise RasterUnavailableError("terrain_grid_not_persisted")
            manifest = EvidenceItem(
                public_id=_public_id("map-projection-evidence", map_row.public_id),
                replay_id=0,
                tier="observed",
                source_kind="persisted_map_projection",
                source_key=f"map:{map_row.public_id}",
                schema_version=map_row.schema_version,
            )
            projection = self._projection(session, map_row, manifest)
            for surface in ("ground", "amphibious"):
                descriptor = self._raster_descriptor(map_row, projection, surface)
                if descriptor["raster_public_id"] == query.raster_public_id:
                    values = projection.ground_passable if surface == "ground" else projection.amphibious_passable
                    content = _pathability_png(projection.pathing.width, projection.pathing.height, values)
                    return MapRasterReadModel(map_row.public_id, FrozenRecord(descriptor), content)
            raise MapSceneNotFoundError("raster does not belong to the requested map")

    def list_scenes(self, query: MapSceneIndexReadQuery) -> MapSceneIndexReadModel:
        if type(query) is not MapSceneIndexReadQuery:
            raise TypeError("query must be a MapSceneIndexReadQuery")
        rows: tuple[tuple[Report, Replay, Map], ...] = ()
        total = 0
        if query.availability in (None, "partial"):
            with self._session_factory() as session:
                # TheSuperHackers @bugfix Leex 25/08/2026 Select only the newest replay-wide report so superseded invalid reports cannot poison the map index. (#TBD)
                newer_report = aliased(Report)
                latest_report = ~select(newer_report.id).where(
                    newer_report.replay_id == Report.replay_id,
                    newer_report.replay_player_id.is_(None),
                    (newer_report.created_at > Report.created_at)
                    | ((newer_report.created_at == Report.created_at) & (newer_report.public_id > Report.public_id)),
                ).exists()
                # TheSuperHackers @fix Leex 25/08/2026 Index replay-wide map authority instead of player reports without manifest evidence. (#TBD)
                if query.search:
                    candidates = tuple(
                        session.execute(
                            select(
                                Report.id,
                                Map.display_name,
                                Replay.map_name,
                                Replay.replay_name,
                            )
                            .select_from(Report)
                            .join(Replay, Replay.id == Report.replay_id)
                            .join(Map, Map.id == Replay.map_id)
                            .where(Report.replay_player_id.is_(None), latest_report)
                            .order_by(Replay.public_id, Report.public_id)
                            .limit(_MAP_SCENE_SEARCH_CANDIDATE_LIMIT + 1)
                        ).tuples()
                    )
                    if len(candidates) > _MAP_SCENE_SEARCH_CANDIDATE_LIMIT:
                        raise MapSceneContractError(
                            "map scene search candidate limit exceeded"
                        )
                    term = query.search.casefold()
                    matching_ids = [
                        report_id
                        for report_id, display_name, map_name, replay_name in candidates
                        if term in f"{display_name or map_name} {replay_name}".casefold()
                    ]
                    total = len(matching_ids)
                    offset = self._bounded_page_offset(total, query.page, query.page_size)
                    selected_ids = matching_ids[offset : offset + query.page_size]
                    if selected_ids:
                        rows = tuple(
                            session.execute(
                                select(Report, Replay, Map)
                                .join(Replay, Replay.id == Report.replay_id)
                                .join(Map, Map.id == Replay.map_id)
                                .where(Report.id.in_(selected_ids), Report.replay_player_id.is_(None), latest_report)
                                .order_by(Replay.public_id, Report.public_id)
                            ).tuples()
                        )
                else:
                    total = session.scalar(
                        select(func.count(Report.id))
                        .select_from(Report)
                        .join(Replay, Replay.id == Report.replay_id)
                        .join(Map, Map.id == Replay.map_id)
                        .where(Report.replay_player_id.is_(None), latest_report)
                    ) or 0
                    offset = self._bounded_page_offset(total, query.page, query.page_size)
                    if offset < total:
                        rows = tuple(
                            session.execute(
                                select(Report, Replay, Map)
                                .join(Replay, Replay.id == Report.replay_id)
                                .join(Map, Map.id == Replay.map_id)
                                .where(Report.replay_player_id.is_(None), latest_report)
                                .order_by(Replay.public_id, Report.public_id)
                                .offset(offset)
                                .limit(query.page_size)
                            ).tuples()
                        )
        items: list[dict[str, object]] = []
        for report, replay, map_row in rows:
            scene_query = MapSceneReadQuery(replay.public_id, report.public_id, 0, 0)
            try:
                graph = self._report_graph(scene_query)
            except (
                ReportGraphNotFoundError,
                ReportGraphAmbiguousError,
                ReportGraphContractError,
            ) as error:
                raise MapSceneContractError(
                    "selected map index candidate fixed report is unresolved"
                ) from error
            try:
                with self._session_factory() as session:
                    frame_window = self._index_authority(session, scene_query, graph)
            except (MapSceneContractError, ValueError) as error:
                raise MapSceneContractError(
                    "selected map index candidate scene is unresolved"
                ) from error
            identity_players = tuple(getattr(getattr(graph, "identity", None), "players", ()))
            item: dict[str, object] = {
                "replay_public_id": replay.public_id,
                "report_public_id": report.public_id,
                "map_public_id": map_row.public_id,
                "map_display_name": map_row.display_name or replay.map_name,
                "report_version": report.report_version,
                "frame_window": frame_window,
                "players": [
                    {"public_id": player.public_id, "label": player.display_name} for player in identity_players
                ],
                "availability": _availability("partial", ("terrain_grid_not_persisted",)),
            }
            items.append(item)
        if items:
            availability = _availability("partial", ("terrain_grid_not_persisted",))
        elif total:
            availability = _availability("partial", ("requested_page_empty",))
        else:
            availability = _availability("unavailable", ("no_completed_map_scenes",))
        payload = {
            "query": {
                "page": query.page,
                "page_size": query.page_size,
                "search": query.search,
                "availability": query.availability,
            },
            "items": items,
            "page": query.page,
            "page_size": query.page_size,
            "total_items": total,
            "availability": availability,
        }
        return MapSceneIndexReadModel(freeze_canonical(payload))


__all__ = [
    "FrozenRecord",
    "MapRasterReadModel",
    "MapRasterReadQuery",
    "MapSceneContractError",
    "MapSceneIndexReadModel",
    "MapSceneIndexReadQuery",
    "MapSceneNotFoundError",
    "MapSceneQueryService",
    "MapSceneReadModel",
    "MapSceneReadQuery",
    "RasterUnavailableError",
    "SceneSample",
    "downsample_samples",
]
