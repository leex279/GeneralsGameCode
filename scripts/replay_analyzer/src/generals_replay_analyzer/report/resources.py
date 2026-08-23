"""Pinned package-resource loading and report-schema validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from typing import TypeAlias, cast

from jsonschema import Draft202012Validator, validators  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from generals_replay_analyzer.report.model import ReportDocument, document_to_mapping

_TEMPLATE_NAME = "replay-report-v1.html"
_SCHEMA_NAME = "replay-report-v1.schema.json"
_TEMPLATE_SHA256 = "2f9e5f40054a1dd602d769fc4f6777ac18b53f1b8a98a077581ec94008c9a190"
_SCHEMA_SHA256 = "864e1450fb19febacbaf7e99b86f0a0dbea079310b0978e939777b760471ea6e"
_JSONFingerprint: TypeAlias = tuple[object, ...]


class ReportResourceError(RuntimeError):
    """A pinned report resource is missing, corrupt, or invalid."""


@dataclass(frozen=True)
class ReportResources:
    html_template: str
    html_template_sha256: str
    document_schema: dict[str, object]
    document_schema_sha256: str


# TheSuperHackers @fix Leex 23/08/2026 Keep pinned report bytes deterministic when Git checks text resources out with Windows newlines. (#TBD)
def _canonicalize_text_resource(payload: bytes) -> bytes:
    return payload.replace(b"\r\n", b"\n")


def _checked_resource(name: str, expected_sha256: str) -> bytes:
    try:
        payload = resources.files("generals_replay_analyzer").joinpath("data").joinpath(name).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ReportResourceError(f"required report resource is unavailable: {name}") from exc
    payload = _canonicalize_text_resource(payload)
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ReportResourceError(f"report resource digest mismatch: {name}")
    return payload


# TheSuperHackers @feature Leex 22/08/2026 Fail closed when versioned report presentation resources drift. (#TBD)
def load_report_resources() -> ReportResources:
    """Load the exact v1 template/schema pair and verify their pinned identities."""
    template_bytes = _checked_resource(_TEMPLATE_NAME, _TEMPLATE_SHA256)
    schema_bytes = _checked_resource(_SCHEMA_NAME, _SCHEMA_SHA256)
    try:
        template = template_bytes.decode("utf-8")
        schema = cast(dict[str, object], json.loads(schema_bytes))
        Draft202012Validator.check_schema(schema)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        raise ReportResourceError("report resource content is invalid") from exc
    if template.count("{{REPORT_BODY}}") != 1:
        raise ReportResourceError("report template must contain exactly one report body slot")
    return ReportResources(template, _TEMPLATE_SHA256, schema, _SCHEMA_SHA256)


# TheSuperHackers @performance Leex 23/08/2026 Validate large canonical evidence arrays without quadratic equality scans. (#TBD)
def _json_fingerprint(value: object) -> _JSONFingerprint:
    """Return one hashable identity with exact JSON Schema equality semantics."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) in (int, float):
        return ("number", value)
    if type(value) is str:
        return ("string", value)
    if type(value) is list:
        return ("array", tuple(_json_fingerprint(item) for item in cast(list[object], value)))
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        return (
            "object",
            tuple(sorted((key, _json_fingerprint(item)) for key, item in mapping.items())),
        )
    raise TypeError(f"unsupported JSON Schema instance type: {type(value).__name__}")


def _linear_unique_items(
    _validator: object,
    enabled: object,
    instance: object,
    _schema: object,
) -> Iterator[ValidationError]:
    """Validate uniqueItems in linear time for the closed canonical JSON tree."""
    if enabled is not True or type(instance) is not list:
        return
    seen: set[_JSONFingerprint] = set()
    for item in cast(list[object], instance):
        identity = _json_fingerprint(item)
        if identity in seen:
            yield ValidationError("array contains non-unique elements")
            return
        seen.add(identity)


_LinearDraft202012Validator = validators.extend(
    Draft202012Validator,
    {"uniqueItems": _linear_unique_items},
)


def validate_document(document: ReportDocument) -> None:
    """Validate one accepted DTO projection against the pinned public schema."""
    if type(document) is not ReportDocument:
        raise TypeError("document must be a ReportDocument")
    loaded = load_report_resources()
    try:
        _LinearDraft202012Validator(loaded.document_schema).validate(document_to_mapping(document))
    except ValidationError as exc:
        raise ValueError(f"report document does not satisfy replay-report-v1: {exc.message}") from exc
