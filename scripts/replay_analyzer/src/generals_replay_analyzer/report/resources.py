"""Pinned package-resource loading and report-schema validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from typing import cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from generals_replay_analyzer.report.model import ReportDocument, document_to_mapping

_TEMPLATE_NAME = "replay-report-v1.html"
_SCHEMA_NAME = "replay-report-v1.schema.json"
_TEMPLATE_SHA256 = "2f9e5f40054a1dd602d769fc4f6777ac18b53f1b8a98a077581ec94008c9a190"
_SCHEMA_SHA256 = "864e1450fb19febacbaf7e99b86f0a0dbea079310b0978e939777b760471ea6e"


class ReportResourceError(RuntimeError):
    """A pinned report resource is missing, corrupt, or invalid."""


@dataclass(frozen=True)
class ReportResources:
    html_template: str
    html_template_sha256: str
    document_schema: dict[str, object]
    document_schema_sha256: str


def _checked_resource(name: str, expected_sha256: str) -> bytes:
    try:
        payload = resources.files("generals_replay_analyzer").joinpath("data").joinpath(name).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ReportResourceError(f"required report resource is unavailable: {name}") from exc
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


def validate_document(document: ReportDocument) -> None:
    """Validate one accepted DTO projection against the pinned public schema."""
    if type(document) is not ReportDocument:
        raise TypeError("document must be a ReportDocument")
    loaded = load_report_resources()
    try:
        Draft202012Validator(loaded.document_schema).validate(document_to_mapping(document))
    except ValidationError as exc:
        raise ValueError(f"report document does not satisfy replay-report-v1: {exc.message}") from exc
