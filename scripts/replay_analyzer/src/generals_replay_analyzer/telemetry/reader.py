"""Strict NDJSON reader for observed Replay Analyzer telemetry traces."""

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from generals_replay_analyzer.telemetry.model import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    CompleteRecord,
    GameDataCatalogReference,
    ManifestRecord,
    PlayersInitializedRecord,
    TelemetryRecord,
)

_RECORD_ADAPTER: TypeAdapter[TelemetryRecord] = TypeAdapter(TelemetryRecord)


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


_OBJECT_REFERENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "construction_started": ("object_id", "producer_object_id", "builder_object_id"),
    "construction_completed": ("object_id", "producer_object_id", "builder_object_id"),
    "owner_changed": ("object_id",),
    "sold": ("object_id",),
    "object_destroyed": ("object_id",),
    "production_queued": ("producer_object_id",),
    "production_cancelled": ("producer_object_id",),
    "production_completed": ("producer_object_id",),
    "supply_collected": ("collector_object_id", "source_object_id"),
    "damage_applied": ("victim_object_id", "attacker_object_id"),
    "healing_applied": ("target_object_id",),
    "veterancy_changed": ("object_id",),
    "entity_state_changed": ("object_id",),
    "entity_sample": ("object_id",),
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


def _validate_catalog_asset(path: Path, reference: GameDataCatalogReference, engine_build: str) -> None:
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


def _referenced_object_ids(event_type: str, payload: Mapping[str, object]) -> Iterator[int]:
    """Yield only fields whose schema semantics are entity identities, never category or production IDs."""
    for field in _OBJECT_REFERENCE_FIELDS.get(event_type, ()):
        value = payload.get(field)
        if isinstance(value, int):
            yield value
    if event_type == "object_created":
        context = payload.get("creation_context")
        if isinstance(context, Mapping):
            producer = context.get("producer_object_id")
            if isinstance(producer, int):
                yield producer
    elif event_type == "order_issued":
        selected = payload.get("selected_object_ids")
        if isinstance(selected, list):
            yield from (value for value in selected if isinstance(value, int))
        target = payload.get("target_object_id")
        if isinstance(target, int):
            yield target


def _validate_object_reference(
    path: Path,
    line_number: int,
    sequence: int,
    event_type: str,
    object_id: int,
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
    if state.destroyed:
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
) -> None:
    """Validate entity identity and irreversible transitions before any buffered record can escape."""
    event_type = record.event_type
    if event_type in {"manifest", "players_initialized", "complete"}:
        return
    payload = cast(dict[str, object], record.payload.model_dump())
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

    for object_id in references:
        _validate_object_reference(path, line_number, record.sequence, event_type, object_id, entities)

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
    expected_schema_version: int | None = None
    expected_run_id: object | None = None
    expected_catalog: GameDataCatalogReference | None = None
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
            validated = _RECORD_ADAPTER.validate_python(record)
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

        if isinstance(validated, ManifestRecord):
            expected_catalog = validated.payload.game_data_catalog
            expected_engine_build = validated.payload.engine_build
        elif isinstance(validated, PlayersInitializedRecord):
            players_initialized_count += 1
            if expected_schema_version == 2 and validated.payload.game_data_catalog != expected_catalog:
                raise _error(path, line_number, validated.sequence, "game_data_catalog differs from manifest")

        if expected_schema_version == 2:
            _validate_v2_entity_lifecycle(
                path,
                line_number,
                validated,
                entity_lifecycles,
                sold_object_ids,
                destroyed_object_ids,
                players_initialized_count,
            )

        records_seen += 1
        prior_sequence = validated.sequence
        actual_event_counts[validated.event_type] = actual_event_counts.get(validated.event_type, 0) + 1
        if isinstance(validated, CompleteRecord):
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
        if expected_catalog is None or expected_engine_build is None:
            raise TelemetryTraceValidationError(f"trace '{path}': v2 manifest catalog identity is unavailable")
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
        _validate_catalog_asset(path, expected_catalog, expected_engine_build)
    return tuple(validated_records)
