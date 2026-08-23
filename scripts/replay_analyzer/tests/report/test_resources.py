"""Performance and semantic controls for pinned report-schema validation."""

from __future__ import annotations

import time
from typing import cast

import pytest

import generals_replay_analyzer.report.resources as resource_module
from generals_replay_analyzer.report.model import (
    OllamaReportStatus,
    ReportDocument,
    ReportEvidenceRef,
    ReportLifecycle,
    ReportValue,
    document_to_mapping,
)
from generals_replay_analyzer.report.resources import ReportResources, validate_document

_REPORT_ID = "00000000-0000-4000-8000-000000000001"
_REPLAY_ID = "00000000-0000-4000-8000-000000000002"
_SHA256 = "a" * 64


def test_report_resource_newlines_are_canonical_across_windows_checkouts() -> None:
    windows_payload = b"first\r\nsecond\r\n"

    assert resource_module._canonicalize_text_resource(windows_payload) == b"first\nsecond\n"


def _document(evidence_count: int = 1) -> ReportDocument:
    evidence = tuple(
        ReportEvidenceRef(
            f"00000000-0000-4000-8000-{index:012x}",
            "observed",
        )
        for index in range(1, evidence_count + 1)
    )
    availability = ReportValue(
        "availability:parser",
        "availability",
        "Parser evidence",
        evidence_count,
        "count",
        "available",
        None,
        {"scope_type": "replay", "replay_public_id": _REPLAY_ID},
        (0, 0),
        evidence,
        {"evidence_count": evidence_count},
    )
    return ReportDocument(
        "replay-report-v1",
        _REPORT_ID,
        "replay-report-v1",
        _SHA256,
        "b" * 64,
        _REPLAY_ID,
        "c" * 64,
        None,
        ReportLifecycle("analyzed", "complete", "succeeded", "success"),
        (availability,),
        (),
        (),
        (),
        (),
        OllamaReportStatus.not_requested(),
        (),
    )


def test_repeated_large_unique_evidence_validation_stays_within_the_query_budget() -> None:
    document = _document(3_993)

    started = time.perf_counter()
    validate_document(document)
    validate_document(document)
    elapsed = time.perf_counter() - started

    assert elapsed < 10.0


@pytest.mark.parametrize(
    ("instance", "is_valid"),
    (
        ([[{"key": [1, 2]}], [{"key": [1, 2]}]], False),
        ([{"first": 1, "second": 2}, {"second": 2, "first": 1}], False),
        ([1, 1.0], False),
        ([True, 1], True),
        ([{"key": [1, 2]}, {"key": [2, 1]}], True),
    ),
)
def test_linear_unique_items_preserves_json_schema_equality(
    monkeypatch: pytest.MonkeyPatch,
    instance: object,
    is_valid: bool,
) -> None:
    document = _document()
    schema: dict[str, object] = {"type": "array", "uniqueItems": True}
    monkeypatch.setattr(
        resource_module,
        "load_report_resources",
        lambda: ReportResources("{{REPORT_BODY}}", _SHA256, schema, _SHA256),
    )
    monkeypatch.setattr(resource_module, "document_to_mapping", lambda _document: instance)

    if is_valid:
        validate_document(document)
    else:
        with pytest.raises(ValueError, match="does not satisfy replay-report-v1"):
            validate_document(document)


def test_validator_still_rejects_malformed_report_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    malformed = cast(dict[str, object], document_to_mapping(document))
    malformed["unexpected"] = "poison"
    monkeypatch.setattr(resource_module, "document_to_mapping", lambda _document: malformed)

    with pytest.raises(ValueError, match="does not satisfy replay-report-v1"):
        validate_document(document)
