"""Strict NDJSON reader for observed Replay Analyzer telemetry traces."""

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from generals_replay_analyzer.contracts import message_name_for
from generals_replay_analyzer.telemetry.map_asset import MapAsset, MapAssetValidationError, load_map_asset
from generals_replay_analyzer.telemetry.model import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    CompleteRecord,
    EntitySampleRecord,
    FinalCashBalance,
    GameDataCatalogReference,
    ManifestRecord,
    MapAssetReference,
    MatchOutcomeRecord,
    PlayersInitializedRecord,
    TelemetryRecord,
)
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage

_RECORD_ADAPTER: TypeAdapter[TelemetryRecord] = TypeAdapter(TelemetryRecord)
_UINT32_MODULUS = 1 << 32


class TelemetryTraceValidationError(ValueError):
    """Raised with the trace path and record identity for invalid observed evidence."""


def _schema(version: int) -> dict[str, object]:
    """Load the same schema file from installed package data or an editable checkout."""
    filename = f"telemetry-v{version}.schema.json"
    source = Path(__file__).resolve().parents[3] / "contracts" / filename
    if source.is_file():
        return cast(dict[str, object], json.loads(source.read_text(encoding="utf-8")))
    packaged = resources.files("generals_replay_analyzer").joinpath("data", filename)
    if packaged.is_file():
        return cast(dict[str, object], json.loads(packaged.read_text(encoding="utf-8")))
    return cast(dict[str, object], json.loads(source.read_text(encoding="utf-8")))


def _catalog_schema() -> dict[str, object]:
    """Load the required semantic catalog schema from package data or source."""
    filename = "game-data-catalog-v1.schema.json"
    packaged = resources.files("generals_replay_analyzer").joinpath("data", filename)
    if packaged.is_file():
        return cast(dict[str, object], json.loads(packaged.read_text(encoding="utf-8")))
    source = Path(__file__).resolve().parents[3] / "contracts" / filename
    return cast(dict[str, object], json.loads(source.read_text(encoding="utf-8")))


_SCHEMAS = {version: _schema(version) for version in SUPPORTED_SCHEMA_VERSIONS}
_DEFINITIONS = {
    version: cast(dict[str, object], schema["$defs"])
    for version, schema in _SCHEMAS.items()
}
_FORMAT_CHECKER = FormatChecker()
_CATALOG_VALIDATOR = Draft202012Validator(_catalog_schema(), format_checker=_FORMAT_CHECKER)


@dataclass
class _EntityLifecycleState:
    """Trace-local object identity and irreversible lifecycle state."""

    template_name: str
    owner_player_index: int | None
    team_id: int | None
    construction_state: str
    sold: bool = False
    destroyed: bool = False
    current_veterancy_level_id: int | None = None


@dataclass(frozen=True)
class _CatalogIdentities:
    """Stable semantic names available to task-specific observations."""

    thing_templates: frozenset[str]
    thing_template_kind_of_flags: Mapping[str, frozenset[str]]
    upgrades: frozenset[str]
    sciences: frozenset[str]


@dataclass
class _QueueState:
    """Immutable queued identity and its optional mutually-exclusive terminal."""

    identity: tuple[object, ...]
    terminal: str | None = None


@dataclass(frozen=True)
class _PendingSupplyCashPair:
    """Authoritative supply handoff awaiting its immediately following cash observation."""

    sequence: int
    frame: int
    player_index: int
    amount: int


@dataclass(frozen=True)
class _ObservedOrder:
    """One validated historical command identity referenced by later entity samples."""

    message_type: int
    message_name: str
    selected_object_ids: tuple[int, ...]


@dataclass(frozen=True)
class _ObservedEngineState:
    """Overlapping source-grounded state facts shared by transitions and samples."""

    classification: str
    classification_source: str
    ai_state_id: int | None
    ai_state_name: str | None
    locomotor_set_id: int | None
    locomotor_set_name: str | None
    is_engine_moving: bool


@dataclass(frozen=True)
class _LastEntitySample:
    """Prior per-entity sample facts used for density and duplicate validation."""

    frame: int
    facts: dict[str, object]
    is_engine_moving: bool
    interval_eligible: bool


@dataclass(frozen=True)
class _PendingForcedSample:
    """Highest-priority producer force reason awaiting its same-frame sample."""

    frame: int
    reason: str


@dataclass
class _Task7FrameOrdering:
    """Producer-order state for one frame's post-dispatch sampler block."""

    frame: int | None = None
    sampler_started: bool = False
    last_sampler_object_id: int | None = None
    state_object_ids: set[int] = dataclass_field(default_factory=set)
    sample_object_ids: set[int] = dataclass_field(default_factory=set)


_SAMPLE_SNAPSHOT_FIELDS = (
    "position",
    "orientation",
    "layer_id",
    "speed",
    "current_state",
    "current_state_source",
    "ai_state_id",
    "ai_state_name",
    "locomotor_set_id",
    "locomotor_set_name",
    "is_engine_moving",
    "path_goal",
    "path_goal_status",
    "is_mobile",
    "is_structure",
    "is_disabled",
    "current_order_id",
    "current_order_message_type",
    "current_order_message_name",
)


def _sample_snapshot_facts(payload: dict[str, object]) -> dict[str, object]:
    """Mirror exactly the engine fields compared by ReplayMovementSampler::sameSample."""
    return {field: payload[field] for field in _SAMPLE_SNAPSHOT_FIELDS}


class _ReferenceRequirement(Enum):
    """Distinguish current-state subjects from immutable historical provenance."""

    REQUIRES_CREATION = "requires_creation"
    REQUIRES_ALIVE = "requires_alive"


_OBJECT_REFERENCE_RULES: dict[str, dict[str, _ReferenceRequirement]] = {
    "construction_started": {
        "object_id": _ReferenceRequirement.REQUIRES_ALIVE,
        "producer_object_id": _ReferenceRequirement.REQUIRES_CREATION,
        "builder_object_id": _ReferenceRequirement.REQUIRES_CREATION,
    },
    "construction_completed": {
        "object_id": _ReferenceRequirement.REQUIRES_ALIVE,
        "producer_object_id": _ReferenceRequirement.REQUIRES_CREATION,
        "builder_object_id": _ReferenceRequirement.REQUIRES_CREATION,
    },
    "owner_changed": {"object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "sold": {"object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "object_destroyed": {"object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "production_queued": {"producer_object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "production_cancelled": {"producer_object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "production_completed": {"producer_object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "upgrade_queued": {"producer_object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "upgrade_cancelled": {"producer_object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "upgrade_completed": {"producer_object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "science_purchased": {"source_object_id": _ReferenceRequirement.REQUIRES_CREATION},
    "special_power_used": {
        "source_object_id": _ReferenceRequirement.REQUIRES_ALIVE,
        "target_object_id": _ReferenceRequirement.REQUIRES_ALIVE,
    },
    "supply_collected": {
        "collector_object_id": _ReferenceRequirement.REQUIRES_ALIVE,
        "source_object_id": _ReferenceRequirement.REQUIRES_CREATION,
        "dropoff_object_id": _ReferenceRequirement.REQUIRES_ALIVE,
    },
    "damage_applied": {
        "victim_object_id": _ReferenceRequirement.REQUIRES_ALIVE,
        "attacker_object_id": _ReferenceRequirement.REQUIRES_CREATION,
    },
    "healing_applied": {
        "target_object_id": _ReferenceRequirement.REQUIRES_ALIVE,
        "source_object_id": _ReferenceRequirement.REQUIRES_ALIVE,
    },
    "veterancy_changed": {"object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "entity_state_changed": {"object_id": _ReferenceRequirement.REQUIRES_ALIVE},
    "entity_sample": {"object_id": _ReferenceRequirement.REQUIRES_ALIVE},
}


def _dereference_schema(value: object, definitions: dict[str, object]) -> object:
    """Inline local definitions so record-level validators retain schema references."""
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            definition = reference.removeprefix("#/$defs/")
            target: object = definitions
            for component in definition.split("/"):
                target = cast(dict[str, object], target)[component]
            return _dereference_schema(target, definitions)
        return {key: _dereference_schema(item, definitions) for key, item in value.items()}
    if isinstance(value, list):
        return [_dereference_schema(item, definitions) for item in value]
    return value


_ENVELOPE_VALIDATORS = {
    version: Draft202012Validator(_dereference_schema(definitions["envelope"], definitions), format_checker=_FORMAT_CHECKER)
    for version, definitions in _DEFINITIONS.items()
}
_MANIFEST_VALIDATORS = {
    version: Draft202012Validator(
        _dereference_schema(definitions["manifestPayload"], definitions), format_checker=_FORMAT_CHECKER
    )
    for version, definitions in _DEFINITIONS.items()
}
_COMPLETE_VALIDATORS = {
    version: Draft202012Validator(
        _dereference_schema(definitions["completePayload"], definitions), format_checker=_FORMAT_CHECKER
    )
    for version, definitions in _DEFINITIONS.items()
}
_EVENT_VALIDATORS = {
    version: {
        event_type: Draft202012Validator(
            _dereference_schema(payload_schema, definitions), format_checker=_FORMAT_CHECKER
        )
        for event_type, payload_schema in cast(dict[str, dict[str, object]], definitions["eventPayloads"]).items()
    }
    for version, definitions in _DEFINITIONS.items()
}


def _error(path: Path, line_number: int, sequence: object, detail: str) -> TelemetryTraceValidationError:
    """Create diagnostics that later runner stages can attribute to one source record."""
    return TelemetryTraceValidationError(f"trace '{path}' record {line_number} sequence {sequence} {detail}")


def _validation_path(error_path: object, message: str, prefix: str) -> str:
    """Return the concrete field path for a leaf JSON Schema validation error."""
    components = [str(component) for component in cast(tuple[object, ...], error_path)]
    if " is a required property" in message:
        missing = message.split("'", maxsplit=2)[1]
        components.append(missing)
    return ".".join([prefix, *components]) if components else prefix


def _first_validation_error(validator: Draft202012Validator, value: object, prefix: str) -> str | None:
    """Select a deterministic leaf error without losing its record-local field path."""
    errors = sorted(validator.iter_errors(value), key=lambda error: (list(error.absolute_path), error.message))
    if not errors:
        return None
    error = errors[0]
    path = _validation_path(tuple(error.absolute_path), error.message, prefix)
    return f"schema path {path}: {error.message}"


def _schema_error(record: Mapping[str, object], version: int) -> str | None:
    """Validate the envelope and its selected event payload without outer oneOf noise."""
    envelope_error = _first_validation_error(_ENVELOPE_VALIDATORS[version], record, "<root>")
    if envelope_error is not None:
        return envelope_error.replace("<root>.", "")
    event_type = record.get("event_type")
    payload = record.get("payload")
    if event_type == "manifest":
        return _first_validation_error(_MANIFEST_VALIDATORS[version], payload, "payload")
    if event_type == "complete":
        return _first_validation_error(_COMPLETE_VALIDATORS[version], payload, "payload")
    if not isinstance(event_type, str) or event_type not in _EVENT_VALIDATORS[version]:
        return "schema path event_type: unsupported telemetry event type"
    return _first_validation_error(_EVENT_VALIDATORS[version][event_type], payload, "payload")


def _reject_nonstandard_constant(value: str) -> object:
    """Reject JavaScript numeric constants that are not legal JSON numbers."""
    raise ValueError(f"non-standard numeric constant {value}")


def _parse_finite_float(value: str) -> float:
    """Decode one legal JSON float only when its Python representation remains finite."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"nonfinite number {value}")
    return parsed


def _validate_catalog_asset(
    path: Path, reference: GameDataCatalogReference, engine_build: str
) -> _CatalogIdentities:
    """Resolve and validate the exact content-bound v2 catalog before records escape."""
    catalog_path_text = reference.path
    relative_path = Path(catalog_path_text)
    if relative_path.is_absolute() or relative_path.name != catalog_path_text:
        raise TelemetryTraceValidationError(f"trace '{path}': catalog path must be one safe relative basename")
    catalog_path = (path.parent / relative_path).resolve()
    if catalog_path.parent != path.parent.resolve():
        raise TelemetryTraceValidationError(f"trace '{path}': catalog path escapes the trace directory")
    try:
        catalog_bytes = catalog_path.read_bytes()
    except FileNotFoundError as error:
        raise TelemetryTraceValidationError(f"trace '{path}': catalog asset does not exist: {catalog_path}") from error
    except OSError as error:
        raise TelemetryTraceValidationError(f"trace '{path}': cannot read catalog asset: {error}") from error
    expected_sha256 = reference.sha256
    if hashlib.sha256(catalog_bytes).hexdigest() != expected_sha256:
        raise TelemetryTraceValidationError(f"trace '{path}': catalog sha256 does not match exact asset bytes")
    try:
        catalog_text = catalog_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TelemetryTraceValidationError(f"trace '{path}': catalog contains invalid UTF-8: {error}") from error
    try:
        catalog = json.loads(
            catalog_text,
            parse_constant=_reject_nonstandard_constant,
            parse_float=_parse_finite_float,
        )
    except json.JSONDecodeError as error:
        raise TelemetryTraceValidationError(f"trace '{path}': catalog contains invalid JSON: {error.msg}") from error
    except ValueError as error:
        raise TelemetryTraceValidationError(f"trace '{path}': catalog {error}") from error
    schema_error = _first_validation_error(_CATALOG_VALIDATOR, catalog, "catalog")
    if schema_error is not None:
        raise TelemetryTraceValidationError(f"trace '{path}': catalog {schema_error}")
    if not isinstance(catalog, dict) or catalog.get("engine_data_identity") != engine_build:
        raise TelemetryTraceValidationError(
            f"trace '{path}': catalog engine_data_identity differs from manifest engine_build"
        )
    thing_templates = cast(list[dict[str, object]], catalog["thing_templates"])
    upgrades = cast(list[dict[str, object]], catalog["upgrades"])
    sciences = cast(list[dict[str, object]], catalog["sciences"])
    return _CatalogIdentities(
        thing_templates=frozenset(cast(str, entry["name"]) for entry in thing_templates),
        thing_template_kind_of_flags={
            cast(str, entry["name"]): frozenset(
                cast(str, flag["name"])
                for flag in cast(list[dict[str, object]], entry["kind_of_flags"])
            )
            for entry in thing_templates
        },
        upgrades=frozenset(cast(str, entry["name"]) for entry in upgrades),
        sciences=frozenset(cast(str, entry["name"]) for entry in sciences),
    )


def _referenced_object_ids(
    event_type: str, payload: Mapping[str, object]
) -> Iterator[tuple[int, _ReferenceRequirement]]:
    """Yield only fields whose schema semantics are entity identities, never category or production IDs."""
    for field, requirement in _OBJECT_REFERENCE_RULES.get(event_type, {}).items():
        value = payload.get(field)
        if isinstance(value, int):
            yield value, requirement
    if event_type == "object_created":
        context = payload.get("creation_context")
        if isinstance(context, Mapping):
            producer = context.get("producer_object_id")
            if isinstance(producer, int):
                yield producer, _ReferenceRequirement.REQUIRES_CREATION
    elif event_type == "order_issued":
        selected = payload.get("selected_object_ids")
        if isinstance(selected, list):
            yield from (
                (value, _ReferenceRequirement.REQUIRES_ALIVE)
                for value in selected
                if isinstance(value, int)
            )
        target = payload.get("target_object_id")
        if isinstance(target, int):
            yield target, _ReferenceRequirement.REQUIRES_ALIVE


def _validate_object_reference(
    path: Path,
    line_number: int,
    sequence: int,
    event_type: str,
    object_id: int,
    requirement: _ReferenceRequirement,
    entities: dict[int, _EntityLifecycleState],
) -> None:
    state = entities.get(object_id)
    if state is None:
        raise _error(
            path,
            line_number,
            sequence,
            f"event {event_type} references object_id {object_id} before object_created",
        )
    if requirement is _ReferenceRequirement.REQUIRES_ALIVE and state.destroyed:
        raise _error(
            path,
            line_number,
            sequence,
            f"event {event_type} references object_id {object_id} after object_destroyed",
        )


def _validate_v2_entity_lifecycle(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    entities: dict[int, _EntityLifecycleState],
    sold_ids: set[int],
    destroyed_ids: set[int],
    players_initialized_count: int,
    engine_player_indices: frozenset[int],
) -> None:
    """Validate entity identity and irreversible transitions before any buffered record can escape."""
    event_type = record.event_type
    if event_type in {"manifest", "players_initialized", "complete"}:
        return
    payload = cast(dict[str, object], record.payload.model_dump())
    for owner_field in (
        "owner_player_index",
        "previous_owner_player_index",
        "new_owner_player_index",
    ):
        owner_player_index = payload.get(owner_field)
        if isinstance(owner_player_index, int) and owner_player_index not in engine_player_indices:
            raise _error(
                path,
                line_number,
                record.sequence,
                f"event {event_type} lifecycle owner {owner_field}={owner_player_index} "
                "is outside the initialized engine player domain",
            )
    if event_type == "object_destroyed" and payload.get("object_id") in destroyed_ids:
        raise _error(
            path,
            line_number,
            record.sequence,
            f"duplicate object_destroyed for object_id {payload['object_id']}",
        )
    references = tuple(_referenced_object_ids(event_type, payload))
    if (event_type == "object_created" or references) and players_initialized_count != 1:
        raise _error(path, line_number, record.sequence, f"event {event_type} must follow players_initialized")

    for object_id, requirement in references:
        _validate_object_reference(
            path,
            line_number,
            record.sequence,
            event_type,
            object_id,
            requirement,
            entities,
        )

    if event_type == "object_created":
        object_id = cast(int, payload["object_id"])
        if object_id in entities:
            raise _error(path, line_number, record.sequence, f"duplicate object_created for object_id {object_id}")
        initial_status = cast(list[str], payload["initial_status"])
        entities[object_id] = _EntityLifecycleState(
            template_name=cast(str, payload["template_name"]),
            owner_player_index=cast(int | None, payload["owner_player_index"]),
            team_id=cast(int | None, payload["team_id"]),
            construction_state="not_present" if "UNDER_CONSTRUCTION" in initial_status else "complete",
        )
        return

    direct_object_id = payload.get("object_id")
    if not isinstance(direct_object_id, int):
        return
    state = entities[direct_object_id]
    direct_template = payload.get("template_name")
    if isinstance(direct_template, str) and direct_template != state.template_name:
        raise _error(
            path,
            line_number,
            record.sequence,
            f"object_id {direct_object_id} template identity changed from {state.template_name} to {direct_template}",
        )
    if event_type in {
        "construction_started",
        "construction_completed",
        "sold",
        "object_destroyed",
    } and (payload["owner_player_index"] != state.owner_player_index or payload["team_id"] != state.team_id):
        raise _error(
            path,
            line_number,
            record.sequence,
            f"{event_type} owner/team differs from object state",
        )

    if event_type == "construction_started":
        previous = cast(str, payload["previous_state"])
        if previous != state.construction_state:
            raise _error(
                path,
                line_number,
                record.sequence,
                f"construction_started previous_state {previous} differs from {state.construction_state}",
            )
        state.construction_state = "under_construction"
    elif event_type == "construction_completed":
        if state.construction_state != "under_construction":
            raise _error(path, line_number, record.sequence, "construction_completed requires construction_started")
        state.construction_state = "complete"
    elif event_type == "owner_changed":
        if (
            payload["previous_owner_player_index"] != state.owner_player_index
            or payload["previous_team_id"] != state.team_id
        ):
            raise _error(path, line_number, record.sequence, "owner_changed previous owner/team differs from object state")
        state.owner_player_index = cast(int | None, payload["new_owner_player_index"])
        state.team_id = cast(int | None, payload["new_team_id"])
    elif event_type == "sold":
        if direct_object_id in sold_ids:
            raise _error(path, line_number, record.sequence, f"duplicate sold for object_id {direct_object_id}")
        sold_ids.add(direct_object_id)
        state.sold = True
    elif event_type == "object_destroyed":
        if direct_object_id in destroyed_ids:
            raise _error(
                path,
                line_number,
                record.sequence,
                f"duplicate object_destroyed for object_id {direct_object_id}",
            )
        expected_previous = "sold" if state.sold else "alive"
        if payload["previous_state"] != expected_previous:
            raise _error(
                path,
                line_number,
                record.sequence,
                f"object_destroyed previous_state must be {expected_previous}",
            )
        destroyed_ids.add(direct_object_id)
        state.destroyed = True


_TASK5_EVENTS = {
    "production_queued",
    "production_cancelled",
    "production_completed",
    "upgrade_queued",
    "upgrade_cancelled",
    "upgrade_completed",
    "science_purchased",
    "special_power_used",
    "cash_changed",
    "supply_collected",
}


def _production_identity(payload: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        payload[field]
        for field in (
            "engine_production_id",
            "producer_object_id",
            "player_index",
            "template_name",
            "queue_position",
            "queued_frame",
            "cost",
            "quantity",
        )
    )


def _upgrade_identity(payload: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        payload[field]
        for field in (
            "producer_object_id",
            "player_index",
            "upgrade_name",
            "queue_position",
            "queued_frame",
            "cost",
        )
    )


def _validate_queue_transition(
    path: Path,
    line_number: int,
    sequence: int,
    event_type: str,
    payload: Mapping[str, object],
    queue_id_field: str,
    identity: tuple[object, ...],
    queues: dict[int, _QueueState],
    label: str,
) -> None:
    queue_id = cast(int, payload[queue_id_field])
    terminal = event_type.rsplit("_", maxsplit=1)[1]
    if terminal == "queued":
        if queue_id in queues:
            raise _error(path, line_number, sequence, f"duplicate {label}_queued for {queue_id_field} {queue_id}")
        queues[queue_id] = _QueueState(identity=identity)
        return
    state = queues.get(queue_id)
    if state is None:
        raise _error(path, line_number, sequence, f"{event_type} requires {label}_queued for {queue_id_field} {queue_id}")
    if state.identity != identity:
        raise _error(path, line_number, sequence, f"{label} identity changed for {queue_id_field} {queue_id}")
    if cast(int, payload["terminal_frame"]) < cast(int, payload["queued_frame"]):
        raise _error(path, line_number, sequence, f"{label} terminal_frame precedes queued_frame")
    if state.terminal is not None:
        raise _error(
            path,
            line_number,
            sequence,
            f"{label} {queue_id_field} {queue_id} has mutually exclusive terminal {state.terminal} and {terminal}",
        )
    state.terminal = terminal


def _require_current_owner(
    path: Path,
    line_number: int,
    sequence: int,
    event_type: str,
    field: str,
    object_id: int,
    player_index: int,
    entities: Mapping[int, _EntityLifecycleState],
) -> None:
    state = entities[object_id]
    if state.owner_player_index is None:
        raise _error(
            path,
            line_number,
            sequence,
            f"event {event_type} {field} must have a non-null current owner",
        )
    if state.owner_player_index != player_index:
        raise _error(
            path,
            line_number,
            sequence,
            f"event {event_type} player_index differs from {field} current owner",
        )


def _validate_v2_economy_production(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    players_initialized_count: int,
    production_queues: dict[int, _QueueState],
    upgrade_queues: dict[int, _QueueState],
    cash_after_by_player: dict[int, int],
    engine_player_indices: frozenset[int],
    entities: Mapping[int, _EntityLifecycleState],
) -> None:
    """Validate task-specific state transitions without exposing partial records."""
    event_type = record.event_type
    if event_type not in _TASK5_EVENTS:
        return
    if players_initialized_count != 1:
        raise _error(path, line_number, record.sequence, f"event {event_type} must follow players_initialized")
    payload = cast(dict[str, object], record.payload.model_dump())
    player_index = cast(int, payload["player_index"])
    if player_index not in engine_player_indices:
        raise _error(
            path,
            line_number,
            record.sequence,
            f"event {event_type} player_index {player_index} is outside the initialized engine player domain",
        )
    if event_type.startswith("production_"):
        if event_type == "production_queued":
            producer_object_id = cast(int, payload["producer_object_id"])
            _require_current_owner(
                path,
                line_number,
                record.sequence,
                event_type,
                "producer_object_id",
                producer_object_id,
                player_index,
                entities,
            )
        if event_type == "production_queued" and payload["queued_frame"] != record.frame:
            raise _error(path, line_number, record.sequence, "production queued_frame must equal envelope frame")
        if event_type != "production_queued" and payload["terminal_frame"] != record.frame:
            raise _error(path, line_number, record.sequence, "production terminal_frame must equal envelope frame")
        _validate_queue_transition(
            path,
            line_number,
            record.sequence,
            event_type,
            payload,
            "production_id",
            _production_identity(payload),
            production_queues,
            "production",
        )
    elif event_type.startswith("upgrade_"):
        if event_type == "upgrade_queued":
            producer_object_id = cast(int, payload["producer_object_id"])
            _require_current_owner(
                path,
                line_number,
                record.sequence,
                event_type,
                "producer_object_id",
                producer_object_id,
                player_index,
                entities,
            )
        if event_type == "upgrade_queued" and payload["queued_frame"] != record.frame:
            raise _error(path, line_number, record.sequence, "upgrade queued_frame must equal envelope frame")
        if event_type != "upgrade_queued" and payload["terminal_frame"] != record.frame:
            raise _error(path, line_number, record.sequence, "upgrade terminal_frame must equal envelope frame")
        _validate_queue_transition(
            path,
            line_number,
            record.sequence,
            event_type,
            payload,
            "upgrade_queue_id",
            _upgrade_identity(payload),
            upgrade_queues,
            "upgrade",
        )
    elif event_type == "cash_changed":
        player_index = cast(int, payload["player_index"])
        before = cast(int, payload["before"])
        delta = cast(int, payload["delta"])
        after = cast(int, payload["after"])
        if before + delta != after:
            raise _error(path, line_number, record.sequence, "cash_changed violates exact cash delta")
        prior_after = cash_after_by_player.get(player_index)
        if prior_after is not None and before != prior_after:
            raise _error(path, line_number, record.sequence, f"cash continuity failed for player_index {player_index}")
        cash_after_by_player[player_index] = after
    elif event_type == "science_purchased":
        before = cast(int, payload["points_before"])
        cost = cast(int, payload["purchase_cost_points"])
        after = cast(int, payload["points_after"])
        if before - cost != after:
            raise _error(path, line_number, record.sequence, "science point transition does not match purchase cost")
    elif event_type == "special_power_used":
        source_object_id = cast(int, payload["source_object_id"])
        _require_current_owner(
            path,
            line_number,
            record.sequence,
            event_type,
            "source_object_id",
            source_object_id,
            player_index,
            entities,
        )
    elif event_type == "supply_collected":
        collector_object_id = cast(int, payload["collector_object_id"])
        dropoff_object_id = cast(int, payload["dropoff_object_id"])
        _require_current_owner(
            path,
            line_number,
            record.sequence,
            event_type,
            "collector_object_id",
            collector_object_id,
            player_index,
            entities,
        )
        _require_current_owner(
            path,
            line_number,
            record.sequence,
            event_type,
            "dropoff_object_id",
            dropoff_object_id,
            player_index,
            entities,
        )
        supply_source_object_id = cast(int | None, payload["source_object_id"])
        if supply_source_object_id is not None and supply_source_object_id == dropoff_object_id:
            raise _error(
                path,
                line_number,
                record.sequence,
                "supply_collected source_object_id must differ from dropoff_object_id",
            )


def _validate_v2_supply_cash_pair(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    pending: _PendingSupplyCashPair | None,
) -> _PendingSupplyCashPair | None:
    event_type = record.event_type
    payload = cast(dict[str, object], record.payload.model_dump())
    if pending is not None:
        if event_type != "cash_changed":
            raise _error(
                path,
                line_number,
                record.sequence,
                "supply_collected cash pair must be the immediately following record",
            )
        before = cast(int, payload["before"])
        after = cast(int, payload["after"])
        if (
            record.sequence != pending.sequence + 1
            or record.frame != pending.frame
            or payload["player_index"] != pending.player_index
            or payload["reason"] != "supply_income"
            or payload["track_income"] is not True
            or (before + pending.amount) % _UINT32_MODULUS != after
        ):
            raise _error(
                path,
                line_number,
                record.sequence,
                "supply_collected cash pair has inconsistent sequence, frame, player, amount, or reason",
            )
        return None
    if event_type == "cash_changed" and payload["reason"] == "supply_income":
        raise _error(path, line_number, record.sequence, "orphan supply_income cash_changed event")
    if event_type == "supply_collected":
        amount = cast(int, payload["amount"])
        if amount <= 0:
            raise _error(path, line_number, record.sequence, "supply_collected cash pair requires positive amount")
        return _PendingSupplyCashPair(
            sequence=record.sequence,
            frame=record.frame,
            player_index=cast(int, payload["player_index"]),
            amount=amount,
        )
    return None


def _validate_v2_final_cash_balances(
    path: Path,
    complete: CompleteRecord,
    cash_after_by_player: Mapping[int, int],
    engine_player_indices: frozenset[int],
) -> None:
    balances = complete.payload.final_cash_balances
    if not balances:
        raise TelemetryTraceValidationError(f"trace '{path}': v2 requires at least one final cash balance")
    previous_index: int | None = None
    by_player: dict[int, FinalCashBalance] = {}
    for balance in balances:
        player_index = balance.player_index
        if previous_index is not None and player_index == previous_index:
            raise TelemetryTraceValidationError(
                f"trace '{path}': final cash balances contain duplicate player_index {player_index}"
            )
        if previous_index is not None and player_index < previous_index:
            raise TelemetryTraceValidationError(
                f"trace '{path}': final cash balances must be ordered by player_index"
            )
        previous_index = player_index
        by_player[player_index] = balance
    if list(by_player) != sorted(engine_player_indices):
        raise TelemetryTraceValidationError(
            f"trace '{path}': final cash balance player indices must be exactly equal engine_player_indices"
        )
    for player_index, last_after in cash_after_by_player.items():
        final_balance = by_player.get(player_index)
        if final_balance is None or not final_balance.has_money or final_balance.balance != last_after:
            raise TelemetryTraceValidationError(
                f"trace '{path}': final cash balance does not reconcile player_index {player_index}"
            )


_TASK6_COMBAT_EVENTS = {"damage_applied", "healing_applied", "veterancy_changed"}
_TASK6_PLAYER_EVENTS = {"player_defeated", "player_surrendered", "player_disconnected"}


def _require_player_domain(
    path: Path,
    line_number: int,
    sequence: int,
    event_type: str,
    field: str,
    value: object,
    engine_player_indices: frozenset[int],
) -> None:
    if isinstance(value, int) and value not in engine_player_indices:
        raise _error(
            path,
            line_number,
            sequence,
            f"event {event_type} {field}={value} is outside the initialized engine player domain",
        )


def _require_observed_owner(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    field: str,
    object_id: int,
    player_index: object,
    entities: Mapping[int, _EntityLifecycleState],
    *,
    allow_unknown: bool = False,
) -> None:
    if allow_unknown and player_index is None:
        return
    if player_index != entities[object_id].owner_player_index:
        raise _error(
            path,
            line_number,
            record.sequence,
            f"event {record.event_type} {field} differs from the referenced object's observed owner",
        )


def _validate_v2_combat(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    players_initialized_count: int,
    engine_player_indices: frozenset[int],
    entities: dict[int, _EntityLifecycleState],
) -> None:
    """Validate health/veterancy arithmetic and attribution against trace-local observed state."""
    if record.event_type not in _TASK6_COMBAT_EVENTS:
        return
    if players_initialized_count != 1:
        raise _error(path, line_number, record.sequence, f"event {record.event_type} must follow players_initialized")
    payload = cast(dict[str, object], record.payload.model_dump())
    for field in ("victim_player_index", "target_player_index", "source_player_index", "owner_player_index"):
        _require_player_domain(
            path,
            line_number,
            record.sequence,
            record.event_type,
            field,
            payload.get(field),
            engine_player_indices,
        )
    if record.event_type == "damage_applied":
        for source_player_index in cast(list[int], payload["source_player_indices"]):
            _require_player_domain(
                path,
                line_number,
                record.sequence,
                record.event_type,
                "source_player_indices",
                source_player_index,
                engine_player_indices,
            )

    if record.event_type == "veterancy_changed":
        object_id = cast(int, payload["object_id"])
        object_state = entities[object_id]
        _require_observed_owner(
            path,
            line_number,
            record,
            "owner_player_index",
            object_id,
            payload["owner_player_index"],
            entities,
        )
        previous_level_id = cast(int, payload["previous_level_id"])
        if (
            object_state.current_veterancy_level_id is not None
            and previous_level_id != object_state.current_veterancy_level_id
        ):
            raise _error(
                path,
                line_number,
                record.sequence,
                f"event veterancy_changed breaks observed veterancy continuity for object_id {object_id}",
            )
        object_state.current_veterancy_level_id = cast(int, payload["new_level_id"])
        return

    subject_field = "victim_object_id" if record.event_type == "damage_applied" else "target_object_id"
    subject_id = cast(int, payload[subject_field])
    subject_player_field = "victim_player_index" if record.event_type == "damage_applied" else "target_player_index"
    _require_observed_owner(
        path,
        line_number,
        record,
        subject_player_field,
        subject_id,
        payload[subject_player_field],
        entities,
    )
    if record.event_type == "healing_applied" and isinstance(payload["source_object_id"], int):
        provenance_id = payload["source_object_id"]
        _require_observed_owner(
            path,
            line_number,
            record,
            "source_player_index",
            provenance_id,
            payload["source_player_index"],
            entities,
            allow_unknown=True,
        )


def _validate_v2_player_transition(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    players_initialized_count: int,
    engine_player_indices: frozenset[int],
    slot_player_indices: Mapping[int, int],
    terminal_players: dict[int, str],
    disconnected_slots: set[int],
) -> None:
    if record.event_type not in _TASK6_PLAYER_EVENTS:
        return
    if players_initialized_count != 1:
        raise _error(path, line_number, record.sequence, f"event {record.event_type} must follow players_initialized")
    payload = cast(dict[str, object], record.payload.model_dump())
    player_index = cast(int, payload["player_index"])
    _require_player_domain(
        path,
        line_number,
        record.sequence,
        record.event_type,
        "player_index",
        player_index,
        engine_player_indices,
    )
    prior = terminal_players.get(player_index)
    if prior is not None:
        raise _error(
            path,
            line_number,
            record.sequence,
            f"duplicate or conflicting terminal player transition for player_index {player_index}: {prior}",
        )
    replay_slot_index = payload["replay_slot_index"]
    if isinstance(replay_slot_index, int) and slot_player_indices.get(replay_slot_index) != player_index:
        raise _error(
            path,
            line_number,
            record.sequence,
            "terminal player transition replay_slot_index does not map to player_index",
        )
    if record.event_type == "player_disconnected":
        if not isinstance(replay_slot_index, int):
            raise _error(path, line_number, record.sequence, "player_disconnected requires a replay slot")
        disconnected_slots.add(replay_slot_index)
    terminal_players[player_index] = record.event_type


def _validate_v2_outcome(
    path: Path,
    records: tuple[TelemetryRecord, ...],
    outcome_records: tuple[MatchOutcomeRecord, ...],
    complete: CompleteRecord,
    engine_player_indices: frozenset[int],
    observed_disconnected_slots: frozenset[int],
) -> None:
    if len(outcome_records) != 1:
        raise TelemetryTraceValidationError(
            f"trace '{path}': v2 requires exactly one match_outcome event; found {len(outcome_records)}"
        )
    outcome = outcome_records[0]
    if len(records) < 2 or records[-2] is not outcome:
        raise TelemetryTraceValidationError(f"trace '{path}': match_outcome must be immediately before complete")
    payload = outcome.payload
    if outcome.frame != complete.frame:
        raise TelemetryTraceValidationError(f"trace '{path}': match_outcome frame must equal complete final frame")
    if payload.engine_player_indices != sorted(engine_player_indices):
        raise TelemetryTraceValidationError(
            f"trace '{path}': match_outcome engine_player_indices must be exactly equal initialized domain"
        )
    outcome_players = set(payload.winner_player_indices or []) | set(payload.loser_player_indices or [])
    if not outcome_players <= engine_player_indices:
        raise TelemetryTraceValidationError(
            f"trace '{path}': match_outcome winner/loser is outside the initialized engine player domain"
        )
    header_disconnected_slots = frozenset(payload.replay_header_disconnected_slots or [])
    if not observed_disconnected_slots <= header_disconnected_slots:
        raise TelemetryTraceValidationError(
            f"trace '{path}': player_disconnected event lacks matching replay-header disconnect metadata"
        )
    completion_facts = (
        payload.terminal_reason,
        payload.quit_early,
        payload.replay_header_desync,
        payload.replay_header_disconnected_slots,
        payload.crc_mismatch,
        payload.crc_mismatch_frame,
        payload.clean_shutdown,
    )
    exact_facts = (
        complete.payload.terminal_reason,
        complete.payload.quit_early,
        complete.payload.replay_header_desync,
        complete.payload.replay_header_disconnected_slots,
        complete.payload.crc_mismatch,
        complete.payload.crc_mismatch_frame,
        complete.payload.clean_shutdown,
    )
    if completion_facts != exact_facts:
        raise TelemetryTraceValidationError(f"trace '{path}': match_outcome contradicts complete completion facts")


def _validate_v2_catalog_identities(
    path: Path,
    records: tuple[TelemetryRecord, ...],
    identities: _CatalogIdentities,
) -> None:
    for line_number, record in enumerate(records, start=1):
        payload = record.payload.model_dump()
        if record.event_type.startswith("production_") and payload["template_name"] not in identities.thing_templates:
            raise _error(path, line_number, record.sequence, "template_name is absent from game data catalog")
        if record.event_type.startswith("upgrade_") and payload["upgrade_name"] not in identities.upgrades:
            raise _error(path, line_number, record.sequence, "upgrade_name is absent from game data catalog")
        if record.event_type == "science_purchased" and payload["science_name"] not in identities.sciences:
            raise _error(path, line_number, record.sequence, "science_name is absent from game data catalog")


def _validated_order_coverage(path: Path, manifest: ManifestRecord) -> dict[int, tuple[str, str, int | None]]:
    """Validate the closed dispatch capability against the packaged numeric/name command catalog."""
    if manifest.schema_version != 2:
        return {}
    raw_coverage = manifest.payload.exporter_settings.get("order_coverage")
    if raw_coverage != canonical_order_coverage():
        raise TelemetryTraceValidationError(
            f"trace '{path}': order_coverage must equal the canonical closed supported-order coverage"
        )
    if not isinstance(raw_coverage, dict):
        raise TelemetryTraceValidationError(f"trace '{path}': order_coverage must be a closed object")
    commands = raw_coverage.get("supported_commands")
    if not isinstance(commands, list):
        raise TelemetryTraceValidationError(f"trace '{path}': order_coverage supported_commands must be an array")
    result: dict[int, tuple[str, str, int | None]] = {}
    prior_message_type: int | None = None
    for entry in commands:
        if not isinstance(entry, dict):
            raise TelemetryTraceValidationError(f"trace '{path}': order_coverage command entry must be an object")
        message_type = entry.get("message_type")
        message_name = entry.get("message_name")
        target_kind = entry.get("target_kind")
        target_argument_index = entry.get("target_argument_index")
        if type(message_type) is not int or not isinstance(message_name, str) or not isinstance(target_kind, str):
            raise TelemetryTraceValidationError(f"trace '{path}': order_coverage command identity has invalid types")
        catalog_name = message_name_for(message_type)
        if catalog_name is None or message_name != catalog_name:
            raise TelemetryTraceValidationError(
                f"trace '{path}': order_coverage message numeric/name identity differs from packaged command catalog"
            )
        if message_type in result or (prior_message_type is not None and message_type <= prior_message_type):
            raise TelemetryTraceValidationError(
                f"trace '{path}': order_coverage supported commands must be strictly numeric ordered and unique"
            )
        if (target_kind == "none") != (target_argument_index is None):
            raise TelemetryTraceValidationError(
                f"trace '{path}': order_coverage target argument must be null exactly for target-free commands"
            )
        if target_kind != "none" and type(target_argument_index) is not int:
            raise TelemetryTraceValidationError(f"trace '{path}': targeted order requires a numeric argument index")
        result[message_type] = (message_name, target_kind, cast(int | None, target_argument_index))
        prior_message_type = message_type
    return result


def _validate_v2_task7_frame_order(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    ordering: _Task7FrameOrdering,
) -> None:
    """Reject record orders that the post-dispatch/end-update producer cannot emit."""
    if record.event_type not in {"order_issued", "entity_state_changed", "entity_sample"}:
        return
    if ordering.frame != record.frame:
        ordering.frame = record.frame
        ordering.sampler_started = False
        ordering.last_sampler_object_id = None
        ordering.state_object_ids.clear()
        ordering.sample_object_ids.clear()

    if record.event_type == "order_issued":
        if ordering.sampler_started:
            raise _error(
                path,
                line_number,
                record.sequence,
                "order_issued cannot follow the same-frame end-of-update sampler block",
            )
        return

    ordering.sampler_started = True
    payload = cast(dict[str, object], record.payload.model_dump())
    object_id = cast(int, payload["object_id"])
    if ordering.last_sampler_object_id is not None and object_id < ordering.last_sampler_object_id:
        raise _error(
            path,
            line_number,
            record.sequence,
            "same-frame sampler records must use ascending numeric object_id order",
        )
    ordering.last_sampler_object_id = object_id

    if record.event_type == "entity_state_changed":
        if object_id in ordering.state_object_ids:
            raise _error(path, line_number, record.sequence, "duplicate entity_state_changed for object/frame")
        if object_id in ordering.sample_object_ids:
            raise _error(
                path,
                line_number,
                record.sequence,
                "entity_sample cannot precede entity_state_changed for the same object/frame",
            )
        ordering.state_object_ids.add(object_id)
        return

    if object_id in ordering.sample_object_ids:
        raise _error(path, line_number, record.sequence, "duplicate entity_sample for object/frame")
    ordering.sample_object_ids.add(object_id)


def _validate_v2_order_movement(
    path: Path,
    line_number: int,
    record: TelemetryRecord,
    entities: dict[int, _EntityLifecycleState],
    engine_player_indices: frozenset[int],
    order_coverage: dict[int, tuple[str, str, int | None]],
    orders: dict[int, _ObservedOrder],
    current_order_by_object: dict[int, int],
    current_state_by_object: dict[int, _ObservedEngineState],
    pending_forced_samples: dict[int, _PendingForcedSample],
    pending_lifecycle_samples: dict[int, int],
    last_samples: dict[int, _LastEntitySample],
    movement_sample_frames: int,
) -> None:
    """Validate current order/state references and deterministic bounded movement evidence."""
    event_type = record.event_type
    payload = cast(dict[str, object], record.payload.model_dump())
    if event_type == "object_destroyed":
        object_id = cast(int, payload["object_id"])
        pending_force = pending_forced_samples.get(object_id)
        if pending_force is not None and pending_force.frame == record.frame:
            pending_forced_samples.pop(object_id)
        if pending_lifecycle_samples.get(object_id) == record.frame:
            pending_lifecycle_samples.pop(object_id)
        prior = last_samples.get(object_id)
        if (
            prior is not None
            and prior.is_engine_moving
            and prior.interval_eligible
            and record.frame - prior.frame > movement_sample_frames
        ):
            raise _error(
                path,
                line_number,
                record.sequence,
                "moving entity sample tail gap exceeds movement_sample_frames before object_destroyed",
            )
        current_order_by_object.pop(object_id, None)
        current_state_by_object.pop(object_id, None)
        last_samples.pop(object_id, None)
    for object_id, forced_sample in pending_forced_samples.items():
        if record.frame > forced_sample.frame:
            raise _error(
                path,
                line_number,
                record.sequence,
                f"entity object_id {object_id} is missing its same-frame order/state-forced entity_sample",
            )
    for object_id, lifecycle_sample_frame in pending_lifecycle_samples.items():
        if record.frame > lifecycle_sample_frame:
            raise _error(
                path,
                line_number,
                record.sequence,
                f"entity object_id {object_id} is missing its same-frame lifecycle-forced entity_sample",
            )

    if event_type == "object_destroyed":
        return

    if event_type == "object_created":
        pending_lifecycle_samples[cast(int, payload["object_id"])] = record.frame
        return

    if event_type == "order_issued":
        message_type = cast(int, payload["message_type"])
        message_name = cast(str, payload["message_name"])
        coverage = order_coverage.get(message_type)
        if coverage is None:
            raise _error(path, line_number, record.sequence, "order is outside manifest closed supported-order coverage")
        if coverage[:2] != (message_name, payload["target_kind"]):
            raise _error(
                path,
                line_number,
                record.sequence,
                "order message numeric/name/target identity differs from manifest coverage and packaged catalog",
            )
        if message_name_for(message_type) != message_name:
            raise _error(path, line_number, record.sequence, "order message differs from packaged command catalog")
        source_player = cast(int, payload["source_player_index"])
        if source_player not in engine_player_indices:
            raise _error(path, line_number, record.sequence, "order source player is outside engine player domain")
        order_id = cast(int, payload["order_id"])
        if order_id in orders or order_id != len(orders) + 1:
            raise _error(path, line_number, record.sequence, "order_id must be trace-local, contiguous, and unique")
        selected_ids = cast(list[int], payload["selected_object_ids"])
        selected_entities = cast(list[dict[str, object]], payload["selected_entities"])
        for object_id, identity in zip(selected_ids, selected_entities, strict=True):
            state = entities[object_id]
            if identity["template_name"] != state.template_name:
                raise _error(path, line_number, record.sequence, "selected entity template identity changed")
            if state.owner_player_index != source_player:
                raise _error(path, line_number, record.sequence, "selected entity is not owned by source player")
            current_order_by_object[object_id] = order_id
            pending_force = pending_forced_samples.get(object_id)
            if pending_force is None or pending_force.reason != "state_forced":
                pending_forced_samples[object_id] = _PendingForcedSample(record.frame, "order_forced")
        target_id = payload["target_object_id"]
        if isinstance(target_id, int) and payload["target_template_name"] != entities[target_id].template_name:
            raise _error(path, line_number, record.sequence, "target template identity changed")
        orders[order_id] = _ObservedOrder(message_type, message_name, tuple(selected_ids))
        return

    if event_type == "entity_state_changed":
        object_id = cast(int, payload["object_id"])
        if payload["owner_player_index"] != entities[object_id].owner_player_index:
            raise _error(path, line_number, record.sequence, "state owner identity changed without owner_changed")
        current_order_id = cast(int | None, payload["current_order_id"])
        if current_order_id != current_order_by_object.get(object_id):
            raise _error(path, line_number, record.sequence, "state current_order_id contradicts post-dispatch order history")
        previous_state = _ObservedEngineState(
            cast(str, payload["previous_state"]),
            cast(str, payload["previous_state_source"]),
            cast(int | None, payload["previous_ai_state_id"]),
            cast(str | None, payload["previous_ai_state_name"]),
            cast(int | None, payload["previous_locomotor_set_id"]),
            cast(str | None, payload["previous_locomotor_set_name"]),
            cast(bool, payload["previous_is_engine_moving"]),
        )
        expected_state = current_state_by_object.get(object_id)
        if expected_state is not None and previous_state != expected_state:
            raise _error(path, line_number, record.sequence, "state transition previous engine state contradicts history")
        current_state_by_object[object_id] = _ObservedEngineState(
            cast(str, payload["current_state"]),
            cast(str, payload["current_state_source"]),
            cast(int | None, payload["current_ai_state_id"]),
            cast(str | None, payload["current_ai_state_name"]),
            cast(int | None, payload["current_locomotor_set_id"]),
            cast(str | None, payload["current_locomotor_set_name"]),
            cast(bool, payload["current_is_engine_moving"]),
        )
        pending_forced_samples[object_id] = _PendingForcedSample(record.frame, "state_forced")
        return

    if event_type != "entity_sample":
        return

    object_id = cast(int, payload["object_id"])
    state = entities[object_id]
    if payload["template_name"] != state.template_name:
        raise _error(path, line_number, record.sequence, "sample template identity changed")
    if payload["owner_player_index"] != state.owner_player_index:
        raise _error(path, line_number, record.sequence, "sample owner identity changed without owner_changed")
    sample_state = _ObservedEngineState(
        cast(str, payload["current_state"]),
        cast(str, payload["current_state_source"]),
        cast(int | None, payload["ai_state_id"]),
        cast(str | None, payload["ai_state_name"]),
        cast(int | None, payload["locomotor_set_id"]),
        cast(str | None, payload["locomotor_set_name"]),
        cast(bool, payload["is_engine_moving"]),
    )
    expected_state = current_state_by_object.get(object_id)
    if expected_state is not None and sample_state != expected_state:
        raise _error(path, line_number, record.sequence, "sample engine state contradicts latest entity state")
    current_state_by_object[object_id] = sample_state
    sample_order_id = cast(int | None, payload["current_order_id"])
    if sample_order_id != current_order_by_object.get(object_id):
        raise _error(path, line_number, record.sequence, "sample current order contradicts post-dispatch order history")
    if sample_order_id is not None:
        observed_order = orders[sample_order_id]
        if (
            object_id not in observed_order.selected_object_ids
            or payload["current_order_message_type"] != observed_order.message_type
            or payload["current_order_message_name"] != observed_order.message_name
        ):
            raise _error(path, line_number, record.sequence, "sample current order numeric/name reference is incoherent")
    pending_force = pending_forced_samples.get(object_id)
    if pending_force is not None:
        if pending_force.frame != record.frame:
            raise _error(path, line_number, record.sequence, "forced entity sample must share its order/state frame")
        del pending_forced_samples[object_id]
    lifecycle_frame = pending_lifecycle_samples.get(object_id)
    if lifecycle_frame is not None:
        if lifecycle_frame != record.frame:
            raise _error(path, line_number, record.sequence, "lifecycle entity sample must share its creation frame")
        del pending_lifecycle_samples[object_id]
    prior = last_samples.get(object_id)
    facts = _sample_snapshot_facts(payload)
    sample_reason = cast(str, payload["sample_reason"])
    expected_forced_reason = pending_force.reason if pending_force is not None else None
    if expected_forced_reason is None and lifecycle_frame is not None:
        expected_forced_reason = "lifecycle_forced"
    if expected_forced_reason is not None and sample_reason != expected_forced_reason:
        raise _error(
            path,
            line_number,
            record.sequence,
            f"sample_reason must be {expected_forced_reason} for the pending producer force",
        )
    if prior is not None:
        frame_gap = record.frame - prior.frame
        if frame_gap <= 0:
            raise _error(path, line_number, record.sequence, "duplicate entity samples in one frame are forbidden")
        if prior.is_engine_moving and prior.interval_eligible and frame_gap > movement_sample_frames:
            raise _error(
                path,
                line_number,
                record.sequence,
                "moving entity sample gap exceeds movement_sample_frames",
            )
        if expected_forced_reason is None:
            interval_eligible = (
                cast(bool, payload["is_mobile"])
                and not cast(bool, payload["is_structure"])
                and not cast(bool, payload["is_disabled"])
            )
            changed = facts != prior.facts
            expected_interval_reason = "changed" if changed else "periodic_moving_heartbeat"
            if (
                not interval_eligible
                or frame_gap < movement_sample_frames
                or (not changed and not cast(bool, payload["is_engine_moving"]))
                or sample_reason != expected_interval_reason
            ):
                raise _error(
                    path,
                    line_number,
                    record.sequence,
                    f"sample_reason does not match producer interval provenance ({expected_interval_reason})",
                )
            if not changed and frame_gap != movement_sample_frames:
                raise _error(
                    path,
                    line_number,
                    record.sequence,
                    "periodic heartbeat gap must exactly equal movement_sample_frames",
                )
    elif expected_forced_reason is None:
        raise _error(path, line_number, record.sequence, "first entity sample must preserve lifecycle provenance")
    last_samples[object_id] = _LastEntitySample(
        record.frame,
        facts,
        cast(bool, payload["is_engine_moving"]),
        cast(bool, payload["is_mobile"])
        and not cast(bool, payload["is_structure"])
        and not cast(bool, payload["is_disabled"]),
    )


# TheSuperHackers @feature Leex 19/08/2026 Validate immutable observed telemetry before later import stages consume it. (#TBD)
def iter_validated_trace(path: Path) -> Iterator[TelemetryRecord]:
    """Return an iterator only after the complete trace is validated as immutable evidence."""
    return iter(_validated_records(path))


def _validated_records(path: Path) -> tuple[TelemetryRecord, ...]:
    """Read the whole source before exposing any record to callers."""
    try:
        source = path.read_bytes()
    except OSError as error:
        raise TelemetryTraceValidationError(f"trace '{path}': cannot read trace: {error}") from error

    prior_sequence: int | None = None
    prior_order_movement_frame: int | None = None
    expected_schema_version: int | None = None
    expected_run_id: object | None = None
    expected_catalog: GameDataCatalogReference | None = None
    expected_map_reference: MapAssetReference | None = None
    authoritative_map: MapAsset | None = None
    catalog_identities: _CatalogIdentities | None = None
    expected_engine_build: str | None = None
    players_initialized_count = 0
    actual_event_counts: dict[str, int] = {}
    complete_seen = False
    digest = hashlib.sha256()
    records_seen = 0
    validated_records: list[TelemetryRecord] = []
    entity_lifecycles: dict[int, _EntityLifecycleState] = {}
    sold_object_ids: set[int] = set()
    destroyed_object_ids: set[int] = set()
    production_queues: dict[int, _QueueState] = {}
    upgrade_queues: dict[int, _QueueState] = {}
    cash_after_by_player: dict[int, int] = {}
    engine_player_indices: frozenset[int] | None = None
    pending_supply_cash_pair: _PendingSupplyCashPair | None = None
    complete_record: CompleteRecord | None = None
    slot_player_indices: dict[int, int] = {}
    terminal_players: dict[int, str] = {}
    observed_disconnected_slots: set[int] = set()
    outcome_records: list[MatchOutcomeRecord] = []
    movement_sample_frames = 15
    order_coverage: dict[int, tuple[str, str, int | None]] = {}
    observed_orders: dict[int, _ObservedOrder] = {}
    current_order_by_object: dict[int, int] = {}
    current_state_by_object: dict[int, _ObservedEngineState] = {}
    pending_forced_samples: dict[int, _PendingForcedSample] = {}
    pending_lifecycle_samples: dict[int, int] = {}
    last_entity_samples: dict[int, _LastEntitySample] = {}
    task7_frame_ordering = _Task7FrameOrdering()

    for line_number, raw_line in enumerate(source.splitlines(keepends=True), start=1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _error(path, line_number, "unknown", f"invalid UTF-8: {error}") from error
        content = line.rstrip("\r\n")
        if not content:
            raise _error(path, line_number, "unknown", "blank lines are not allowed")
        try:
            decoded = json.loads(
                content,
                parse_constant=_reject_nonstandard_constant,
                parse_float=_parse_finite_float,
            )
        except json.JSONDecodeError as error:
            raise _error(path, line_number, "unknown", f"invalid JSON: {error.msg}") from error
        except ValueError as error:
            raise _error(path, line_number, "unknown", str(error)) from error
        if not isinstance(decoded, dict):
            raise _error(path, line_number, "unknown", "record must be a JSON object")
        record = cast(dict[str, object], decoded)
        sequence = record.get("sequence", "unknown")

        version = record.get("schema_version")
        if type(version) is int and version not in SUPPORTED_SCHEMA_VERSIONS:
            raise _error(path, line_number, sequence, f"schema_version {version} has unsupported major version")
        if expected_schema_version is not None and type(version) is int and version != expected_schema_version:
            raise _error(path, line_number, sequence, "schema_version differs from manifest")
        selected_version = version if type(version) is int and version in SUPPORTED_SCHEMA_VERSIONS else SCHEMA_VERSION
        schema_error = _schema_error(record, selected_version)
        if schema_error is not None:
            raise _error(path, line_number, sequence, schema_error)
        try:
            validated = _RECORD_ADAPTER.validate_python(record, context={"schema_version": selected_version})
        except ValidationError as error:
            first_error = error.errors()[0]
            message = str(first_error["msg"])
            fields = ".".join(str(item) for item in first_error["loc"])
            detail = f"{fields}: {message}"
            if "logic_time_seconds" in message:
                detail = f"logic_time_seconds: {message}"
            raise _error(path, line_number, sequence, detail) from error

        if complete_seen:
            raise _error(path, line_number, validated.sequence, "records after complete are not allowed")
        if line_number == 1 and validated.event_type != "manifest":
            raise _error(path, line_number, validated.sequence, "first record must be manifest")
        if line_number > 1 and validated.event_type == "manifest":
            raise _error(path, line_number, validated.sequence, "manifest must be first")
        if expected_run_id is None:
            expected_run_id = validated.run_id
        elif validated.run_id != expected_run_id:
            raise _error(path, line_number, validated.sequence, "run_id differs from manifest")
        if expected_schema_version is None:
            expected_schema_version = validated.schema_version
        elif validated.schema_version != expected_schema_version:
            raise _error(path, line_number, validated.sequence, "schema_version differs from manifest")
        if prior_sequence is not None and validated.sequence <= prior_sequence:
            raise _error(
                path,
                line_number,
                validated.sequence,
                f"is not greater than previous sequence {prior_sequence}",
            )
        if validated.event_type in {"order_issued", "entity_state_changed", "entity_sample"}:
            if prior_order_movement_frame is not None and validated.frame < prior_order_movement_frame:
                raise _error(
                    path,
                    line_number,
                    validated.sequence,
                    f"order/state/sample frame {validated.frame} is less than previous frame {prior_order_movement_frame}",
                )
            prior_order_movement_frame = validated.frame

        if isinstance(validated, ManifestRecord):
            expected_catalog = validated.payload.game_data_catalog
            expected_engine_build = validated.payload.engine_build
            if expected_schema_version == 2:
                assert expected_catalog is not None
                catalog_identities = _validate_catalog_asset(path, expected_catalog, expected_engine_build)
                expected_map_reference = validated.payload.map_asset
                assert expected_map_reference is not None
                try:
                    authoritative_map = load_map_asset(
                        path.parent / Path(*expected_map_reference.path.split("/")),
                        expected_reference=expected_map_reference,
                        expected_engine_data_identity=validated.payload.engine_build,
                        expected_map_identity=validated.payload.map_identity,
                    )
                except MapAssetValidationError as error:
                    raise _error(path, line_number, validated.sequence, f"map_asset: {error}") from error
            interval = validated.payload.exporter_settings["movement_sample_frames"]
            assert type(interval) is int
            movement_sample_frames = interval
            order_coverage = _validated_order_coverage(path, validated)
        elif isinstance(validated, PlayersInitializedRecord):
            players_initialized_count += 1
            if expected_schema_version == 2 and validated.payload.game_data_catalog != expected_catalog:
                raise _error(path, line_number, validated.sequence, "game_data_catalog differs from manifest")
            if expected_schema_version == 2:
                assert validated.payload.engine_player_indices is not None
                engine_player_indices = frozenset(validated.payload.engine_player_indices)
                assert validated.payload.slots is not None
                slot_player_indices = {
                    slot.slot_index: slot.player_index
                    for slot in validated.payload.slots
                    if slot.player_index is not None
                }
        elif isinstance(validated, MatchOutcomeRecord):
            outcome_records.append(validated)

        if expected_schema_version == 2 and isinstance(validated, EntitySampleRecord):
            assert authoritative_map is not None
            assert catalog_identities is not None
            sample_kind_of_flags = catalog_identities.thing_template_kind_of_flags.get(
                validated.payload.template_name or "", frozenset()
            )
            try:
                authoritative_map.require_entity_position(
                    validated.payload,
                    sample_kind_of_flags,
                )
            except MapAssetValidationError as error:
                raise _error(
                    path,
                    line_number,
                    validated.sequence,
                    f"map bounds: {error}; policy {validated.payload.position_bounds_policy}; "
                    f"template KindOf flags {sorted(sample_kind_of_flags)}",
                ) from error

        if expected_schema_version == 2:
            if engine_player_indices is None:
                engine_player_indices = frozenset()
            _validate_v2_entity_lifecycle(
                path,
                line_number,
                validated,
                entity_lifecycles,
                sold_object_ids,
                destroyed_object_ids,
                players_initialized_count,
                engine_player_indices,
            )
            _validate_v2_economy_production(
                path,
                line_number,
                validated,
                players_initialized_count,
                production_queues,
                upgrade_queues,
                cash_after_by_player,
                engine_player_indices,
                entity_lifecycles,
            )
            pending_supply_cash_pair = _validate_v2_supply_cash_pair(
                path,
                line_number,
                validated,
                pending_supply_cash_pair,
            )
            _validate_v2_combat(
                path,
                line_number,
                validated,
                players_initialized_count,
                engine_player_indices,
                entity_lifecycles,
            )
            _validate_v2_player_transition(
                path,
                line_number,
                validated,
                players_initialized_count,
                engine_player_indices,
                slot_player_indices,
                terminal_players,
                observed_disconnected_slots,
            )
            _validate_v2_task7_frame_order(path, line_number, validated, task7_frame_ordering)
            _validate_v2_order_movement(
                path,
                line_number,
                validated,
                entity_lifecycles,
                engine_player_indices,
                order_coverage,
                observed_orders,
                current_order_by_object,
                current_state_by_object,
                pending_forced_samples,
                pending_lifecycle_samples,
                last_entity_samples,
                movement_sample_frames,
            )

        records_seen += 1
        prior_sequence = validated.sequence
        actual_event_counts[validated.event_type] = actual_event_counts.get(validated.event_type, 0) + 1
        if isinstance(validated, CompleteRecord):
            if expected_schema_version == 2 and validated.payload.map_assets != [expected_map_reference]:
                raise _error(
                    path,
                    line_number,
                    validated.sequence,
                    "complete map_assets must exactly repeat the manifest map_asset reference",
                )
            if validated.payload.trace_sha256 != digest.hexdigest():
                raise _error(path, line_number, validated.sequence, "complete trace_sha256 does not match prior trace bytes")
            if validated.payload.event_counts != actual_event_counts:
                raise _error(
                    path,
                    line_number,
                    validated.sequence,
                    "schema path payload.event_counts: must exactly equal counts recomputed from buffered records",
                )
            complete_seen = True
            complete_record = validated
        else:
            digest.update(raw_line)
        validated_records.append(validated)

    if records_seen == 0:
        raise TelemetryTraceValidationError(f"trace '{path}': trace is empty")
    if not complete_seen:
        assert prior_sequence is not None
        raise _error(path, records_seen, prior_sequence, "must be complete; trace is incomplete")
    if expected_schema_version == 2:
        if players_initialized_count != 1:
            raise TelemetryTraceValidationError(
                f"trace '{path}': v2 requires exactly one players_initialized event; found {players_initialized_count}"
            )
        if expected_catalog is None or expected_engine_build is None or authoritative_map is None:
            raise TelemetryTraceValidationError(f"trace '{path}': v2 manifest asset identity is unavailable")
        missing_construction = sorted(
            object_id
            for object_id, state in entity_lifecycles.items()
            if state.construction_state == "not_present"
        )
        if missing_construction:
            raise TelemetryTraceValidationError(
                f"trace '{path}': object_id {missing_construction[0]} initial UNDER_CONSTRUCTION status is missing "
                "construction_started"
            )
        assert complete_record is not None
        assert engine_player_indices is not None
        _validate_v2_final_cash_balances(path, complete_record, cash_after_by_player, engine_player_indices)
        _validate_v2_outcome(
            path,
            tuple(validated_records),
            tuple(outcome_records),
            complete_record,
            engine_player_indices,
            frozenset(observed_disconnected_slots),
        )
        assert catalog_identities is not None
        _validate_v2_catalog_identities(path, tuple(validated_records), catalog_identities)
        if pending_forced_samples:
            object_id = min(pending_forced_samples)
            raise TelemetryTraceValidationError(
                f"trace '{path}': entity object_id {object_id} is missing its same-frame order/state-forced entity_sample"
            )
        if pending_lifecycle_samples:
            object_id = min(pending_lifecycle_samples)
            raise TelemetryTraceValidationError(
                f"trace '{path}': entity object_id {object_id} is missing its same-frame lifecycle-forced entity_sample"
            )
        for object_id, sample in last_entity_samples.items():
            entity = entity_lifecycles[object_id]
            if (
                not entity.destroyed
                and sample.is_engine_moving
                and sample.interval_eligible
                and complete_record.payload.final_frame - sample.frame > movement_sample_frames
            ):
                raise TelemetryTraceValidationError(
                    f"trace '{path}': moving entity sample tail gap exceeds movement_sample_frames"
                )
    return tuple(validated_records)
