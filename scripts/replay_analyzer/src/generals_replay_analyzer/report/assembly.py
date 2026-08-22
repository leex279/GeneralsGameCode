"""Pure deterministic report identity and document assembly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from generals_replay_analyzer.report.model import (
    ReportAssemblyInput,
    ReportDocument,
    lifecycle_to_mapping,
    ollama_to_mapping,
    quality_issue_to_mapping,
    report_value_to_mapping,
    thaw_report_value,
)

REPORT_VERSION = "replay-report-v1"
DISPLAY_POLICY_VERSION = "replay-report-display-v1"
_INPUT_SCHEMA = "report-input-v1"
_CACHE_SCHEMA = "replay-report-cache-v1"
_PUBLIC_ID_NAMESPACE = uuid5(NAMESPACE_URL, "replay-report-public-id-v1")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sorted_source(source: ReportAssemblyInput) -> ReportAssemblyInput:
    return replace(
        source,
        evidence_availability=tuple(
            sorted(source.evidence_availability, key=lambda value: (value.section, value.claim_id))
        ),
        quality_issues=tuple(
            sorted(source.quality_issues, key=lambda value: (value.stage, value.issue_code, value.public_id))
        ),
        observed=tuple(sorted(source.observed, key=lambda value: (value.section, value.claim_id))),
        derived=tuple(sorted(source.derived, key=lambda value: (value.section, value.claim_id))),
        inferred=tuple(sorted(source.inferred, key=lambda value: (value.section, value.claim_id))),
        warnings=tuple(sorted(set(source.warnings))),
    )


def _input_identity(source: ReportAssemblyInput) -> dict[str, object]:
    return {
        "input_schema": _INPUT_SCHEMA,
        "replay": {
            "public_id": source.replay_public_id,
            "sha256": source.replay_sha256,
            "player_public_id": source.replay_player_public_id,
            "header_identity": thaw_report_value(source.header_identity),
        },
        "component_identity": thaw_report_value(source.component_identity),
        "lifecycle": lifecycle_to_mapping(source.lifecycle),
        "evidence_availability": [report_value_to_mapping(item) for item in source.evidence_availability],
        "quality_issues": [quality_issue_to_mapping(item) for item in source.quality_issues],
        "observed": [report_value_to_mapping(item) for item in source.observed],
        "derived": [report_value_to_mapping(item) for item in source.derived],
        "inferred": [report_value_to_mapping(item) for item in source.inferred],
        "ollama": ollama_to_mapping(source.ollama),
        "warnings": list(source.warnings),
    }


# TheSuperHackers @feature Leex 22/08/2026 Derive report identities only from canonical persisted evidence and versions. (#TBD)
def assemble_report(
    source: ReportAssemblyInput,
    *,
    html_template_sha256: str,
    document_schema_sha256: str,
    report_version: str = REPORT_VERSION,
    display_policy_version: str = DISPLAY_POLICY_VERSION,
) -> ReportDocument:
    """Sort one accepted evidence snapshot and derive stable analytical/presentation identities."""
    if type(source) is not ReportAssemblyInput:
        raise TypeError("source must be a ReportAssemblyInput")
    for value, label in (
        (html_template_sha256, "html_template_sha256"),
        (document_schema_sha256, "document_schema_sha256"),
    ):
        if (
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256")
    if report_version != REPORT_VERSION or display_policy_version != DISPLAY_POLICY_VERSION:
        raise ValueError("unsupported report or display policy version")
    ordered = _sorted_source(source)
    input_digest = _sha256(_input_identity(ordered))
    cache_key = _sha256(
        {
            "cache_schema": _CACHE_SCHEMA,
            "report_version": report_version,
            "input_digest": input_digest,
            "include_validated_ollama": ordered.ollama.requested,
            "display_policy_version": display_policy_version,
            "html_template_sha256": html_template_sha256,
            "document_schema_sha256": document_schema_sha256,
        }
    )
    public_name = _canonical_bytes(
        {
            "replay_public_id": ordered.replay_public_id,
            "replay_player_public_id": ordered.replay_player_public_id,
            "report_version": report_version,
            "input_digest": input_digest,
        }
    ).decode("utf-8")
    report_public_id = str(uuid5(_PUBLIC_ID_NAMESPACE, public_name))
    return ReportDocument(
        schema_version="replay-report-v1",
        report_public_id=report_public_id,
        report_version=report_version,
        input_digest=input_digest,
        cache_key=cache_key,
        replay_public_id=ordered.replay_public_id,
        replay_sha256=ordered.replay_sha256,
        replay_player_public_id=ordered.replay_player_public_id,
        lifecycle=ordered.lifecycle,
        evidence_availability=ordered.evidence_availability,
        quality_issues=ordered.quality_issues,
        observed=ordered.observed,
        derived=ordered.derived,
        inferred=ordered.inferred,
        ollama=ordered.ollama,
        warnings=ordered.warnings,
    )
