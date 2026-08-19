"""Strict NDJSON reader for observed Replay Analyzer telemetry traces."""

import hashlib
import json
from collections.abc import Iterator, Mapping
from importlib import resources
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from generals_replay_analyzer.telemetry.model import SCHEMA_VERSION, CompleteRecord, TelemetryRecord

_RECORD_ADAPTER: TypeAdapter[TelemetryRecord] = TypeAdapter(TelemetryRecord)


class TelemetryTraceValidationError(ValueError):
    """Raised with the trace path and record identity for invalid observed evidence."""


def _schema() -> dict[str, object]:
    """Load the same schema file from installed package data or an editable checkout."""
    packaged = resources.files("generals_replay_analyzer").joinpath("data", "telemetry-v1.schema.json")
    if packaged.is_file():
        return cast(dict[str, object], json.loads(packaged.read_text(encoding="utf-8")))
    source = Path(__file__).resolve().parents[3] / "contracts" / "telemetry-v1.schema.json"
    return cast(dict[str, object], json.loads(source.read_text(encoding="utf-8")))


_SCHEMA = _schema()
_DEFINITIONS = cast(dict[str, object], _SCHEMA["$defs"])
_FORMAT_CHECKER = FormatChecker()


def _dereference_schema(value: object) -> object:
    """Inline local definitions so record-level validators retain schema references."""
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            definition = reference.removeprefix("#/$defs/")
            return _dereference_schema(_DEFINITIONS[definition])
        return {key: _dereference_schema(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_dereference_schema(item) for item in value]
    return value


_ENVELOPE_VALIDATOR = Draft202012Validator(_dereference_schema(_DEFINITIONS["envelope"]), format_checker=_FORMAT_CHECKER)
_MANIFEST_VALIDATOR = Draft202012Validator(
    _dereference_schema(_DEFINITIONS["manifestPayload"]), format_checker=_FORMAT_CHECKER
)
_COMPLETE_VALIDATOR = Draft202012Validator(
    _dereference_schema(_DEFINITIONS["completePayload"]), format_checker=_FORMAT_CHECKER
)
_EVENT_VALIDATORS = {
    event_type: Draft202012Validator(_dereference_schema(payload_schema), format_checker=_FORMAT_CHECKER)
    for event_type, payload_schema in cast(dict[str, dict[str, object]], _DEFINITIONS["eventPayloads"]).items()
}


def _error(path: Path, line_number: int, sequence: object, detail: str) -> TelemetryTraceValidationError:
    """Create diagnostics that later runner stages can attribute to one source record."""
    return TelemetryTraceValidationError(f"trace '{path}' record {line_number} sequence {sequence} {detail}")


def _validation_path(error_path: object, message: str, prefix: str) -> str:
    """Return the concrete field path for a leaf JSON Schema validation error."""
    components = [str(component) for component in cast(tuple[object, ...], error_path)]
    if not components and " is a required property" in message:
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


def _schema_error(record: Mapping[str, object]) -> str | None:
    """Validate the envelope and its selected event payload without outer oneOf noise."""
    envelope_error = _first_validation_error(_ENVELOPE_VALIDATOR, record, "<root>")
    if envelope_error is not None:
        return envelope_error.replace("<root>.", "")
    event_type = record.get("event_type")
    payload = record.get("payload")
    if event_type == "manifest":
        return _first_validation_error(_MANIFEST_VALIDATOR, payload, "payload")
    if event_type == "complete":
        return _first_validation_error(_COMPLETE_VALIDATOR, payload, "payload")
    if not isinstance(event_type, str) or event_type not in _EVENT_VALIDATORS:
        return "schema path event_type: unsupported telemetry event type"
    return _first_validation_error(_EVENT_VALIDATORS[event_type], payload, "payload")


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
    expected_run_id: object | None = None
    complete_seen = False
    digest = hashlib.sha256()
    records_seen = 0
    validated_records: list[TelemetryRecord] = []

    for line_number, raw_line in enumerate(source.splitlines(keepends=True), start=1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _error(path, line_number, "unknown", f"invalid UTF-8: {error}") from error
        content = line.rstrip("\r\n")
        if not content:
            raise _error(path, line_number, "unknown", "blank lines are not allowed")
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as error:
            raise _error(path, line_number, "unknown", f"invalid JSON: {error.msg}") from error
        if not isinstance(decoded, dict):
            raise _error(path, line_number, "unknown", "record must be a JSON object")
        record = cast(dict[str, object], decoded)
        sequence = record.get("sequence", "unknown")

        version = record.get("schema_version")
        if type(version) is int and version != SCHEMA_VERSION:
            raise _error(path, line_number, sequence, f"schema_version {version} has unsupported major version")
        schema_error = _schema_error(record)
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
        if prior_sequence is not None and validated.sequence <= prior_sequence:
            raise _error(
                path,
                line_number,
                validated.sequence,
                f"is not greater than previous sequence {prior_sequence}",
            )

        records_seen += 1
        prior_sequence = validated.sequence
        if isinstance(validated, CompleteRecord):
            if validated.payload.trace_sha256 != digest.hexdigest():
                raise _error(path, line_number, validated.sequence, "complete trace_sha256 does not match prior trace bytes")
            complete_seen = True
        else:
            digest.update(raw_line)
        validated_records.append(validated)

    if records_seen == 0:
        raise TelemetryTraceValidationError(f"trace '{path}': trace is empty")
    if not complete_seen:
        assert prior_sequence is not None
        raise _error(path, records_seen, prior_sequence, "must be complete; trace is incomplete")
    return tuple(validated_records)
