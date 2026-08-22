from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from generals_replay_analyzer.report.model import (
    FrozenReportMapping,
    OllamaReportStatus,
    ReportDocument,
    ReportEvidenceRef,
    ReportLifecycle,
    ReportQualityIssue,
    ReportValue,
    document_to_mapping,
    thaw_report_value,
)

REPLAY_ID = "00000000-0000-4000-8000-000000000001"
PLAYER_ID = "00000000-0000-4000-8000-000000000002"
EVIDENCE_A = "00000000-0000-4000-8000-000000000003"
EVIDENCE_B = "00000000-0000-4000-8000-000000000004"
REPORT_ID = "00000000-0000-5000-8000-000000000005"
RUN_ID = "00000000-0000-4000-8000-000000000006"
ISSUE_ID = "00000000-0000-4000-8000-000000000007"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def available_value(*, raw_value: object = 0.0) -> ReportValue:
    return ReportValue(
        claim_id="feature:income",
        section="economy",
        label="Income",
        raw_value=raw_value,
        unit="credits",
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "player", "public_id": PLAYER_ID},
        frame_window=(0, 900),
        evidence=(
            ReportEvidenceRef(EVIDENCE_B, "derived"),
            ReportEvidenceRef(EVIDENCE_A, "observed"),
            ReportEvidenceRef(EVIDENCE_A, "observed"),
        ),
        details={"values": [1, {"z": "last", "a": "first"}]},
    )


def test_report_values_are_deeply_immutable_canonical_and_preserve_measured_zero() -> None:
    value = available_value(raw_value=0.0)

    assert value.raw_value == 0.0
    assert value.evidence == (
        ReportEvidenceRef(EVIDENCE_A, "observed"),
        ReportEvidenceRef(EVIDENCE_B, "derived"),
    )
    assert thaw_report_value(value.scope) == {"public_id": PLAYER_ID, "scope_type": "player"}
    assert thaw_report_value(value.details) == {"values": [1, {"a": "first", "z": "last"}]}
    with pytest.raises(FrozenInstanceError):
        value.label = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "invalid",
    [
        float("nan"),
        float("inf"),
        -0.0,
        Path("source.rep"),
        b"replay",
        datetime(2026, 8, 22, tzinfo=UTC),
        "C:/private/replay.rep",
        "/home/player/replay.rep",
        "No source at C:/private/replay.rep",
        {"row_id": 7},
        {"managed_path": "secret"},
    ],
)
def test_report_values_reject_noncanonical_process_and_persistence_values(invalid: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        available_value(raw_value=invalid)


def test_unavailable_requires_null_and_reason_while_partial_requires_value_and_reason() -> None:
    unavailable = ReportValue(
        "feature:missing",
        "economy",
        "Missing fact",
        None,
        None,
        "unavailable",
        "telemetry_unavailable",
        {"scope_type": "replay"},
        None,
        (),
        {},
    )
    partial = ReportValue(
        "feature:partial",
        "economy",
        "Partial fact",
        1.25,
        "ratio",
        "partial",
        "sample_window_incomplete",
        {"scope_type": "replay"},
        (10, 20),
        (ReportEvidenceRef(EVIDENCE_A, "observed"),),
        {},
    )

    assert unavailable.raw_value is None
    assert unavailable.evidence == ()
    assert partial.raw_value == 1.25
    with pytest.raises(ValueError, match="unavailable"):
        available_value(raw_value=None)
    with pytest.raises(ValueError, match="partial"):
        ReportValue(
            "feature:partial",
            "economy",
            "Partial fact",
            1,
            None,
            "partial",
            None,
            {},
            None,
            (ReportEvidenceRef(EVIDENCE_A, "observed"),),
            {},
        )


def test_non_null_report_claim_requires_public_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        ReportValue(
            "feature:unsupported",
            "economy",
            "Unsupported",
            1,
            None,
            "available",
            None,
            {},
            None,
            (),
            {},
        )


def test_report_document_enforces_tier_separation_sorting_and_ollama_state() -> None:
    observed = ReportValue(
        "observed:z",
        "timeline",
        "Z",
        1,
        None,
        "available",
        None,
        {},
        (1, 1),
        (ReportEvidenceRef(EVIDENCE_A, "observed"),),
        {},
    )
    derived = available_value(raw_value=3.141592653589793)
    issue = ReportQualityIssue(ISSUE_ID, "telemetry", "missing_sample", "warning", {"count": 1}, False)
    ollama = OllamaReportStatus(
        requested=True,
        status="succeeded",
        analysis_run_id=RUN_ID,
        provider="ollama",
        model_name="qwen3.6:27b",
        model_digest=SHA_A,
        prompt_version="strategy-report-v1",
        response_schema_version="strategy-report-response-v1",
        diagnostic_codes=("ok",),
        validated_prose={"summary": "Evidence-backed summary."},
    )
    document = ReportDocument(
        "replay-report-v1",
        REPORT_ID,
        "replay-report-v1",
        SHA_B,
        SHA_C,
        REPLAY_ID,
        SHA_A,
        PLAYER_ID,
        ReportLifecycle("partial", "complete", "succeeded", "success"),
        (),
        (issue,),
        (observed,),
        (derived,),
        (),
        ollama,
        ("z_warning", "a_warning", "a_warning"),
    )

    mapping = document_to_mapping(document)
    assert mapping["derived"][0]["raw_value"] == 3.141592653589793
    assert mapping["warnings"] == ["a_warning", "z_warning"]
    assert mapping["ollama"]["validated_prose"] == {"summary": "Evidence-backed summary."}

    with pytest.raises(ValueError, match="observed"):
        ReportDocument(
            "replay-report-v1",
            REPORT_ID,
            "replay-report-v1",
            SHA_B,
            SHA_C,
            REPLAY_ID,
            SHA_A,
            None,
            ReportLifecycle("parsed", "complete", None, None),
            (),
            (),
            (derived,),
            (),
            (),
            OllamaReportStatus.not_requested(),
            (),
        )


def test_ollama_not_requested_is_closed_and_path_free() -> None:
    status = OllamaReportStatus.not_requested()
    assert status.requested is False
    assert status.status == "not_requested"
    assert status.validated_prose is None

    with pytest.raises(ValueError, match="not_requested"):
        OllamaReportStatus(False, "not_requested", RUN_ID, None, None, None, None, None, (), None)


@pytest.mark.parametrize(
    "value",
    ["NOT-A-UUID", "ABCDEFAB-0000-4000-8000-000000000003", "00000000-0000-4000-8000-00000000000A"],
)
def test_public_evidence_ids_require_canonical_lowercase_hyphenated_uuid(value: str) -> None:
    with pytest.raises(ValueError, match="public_id"):
        ReportEvidenceRef(value, "observed")


@pytest.mark.parametrize(
    "forged",
    [
        FrozenReportMapping((("source", Path("secret.rep")),)),
        FrozenReportMapping((("created", datetime(2026, 8, 22, tzinfo=UTC)),)),
        FrozenReportMapping((("payload", b"secret"),)),
        FrozenReportMapping((("duplicate", 1), ("duplicate", 2))),
        FrozenReportMapping((("nested", FrozenReportMapping((("managed_path", "secret"),))),)),
    ],
)
def test_caller_constructed_frozen_marker_is_recursively_revalidated(forged: FrozenReportMapping) -> None:
    with pytest.raises((TypeError, ValueError)):
        available_value(raw_value=forged)


@pytest.mark.parametrize(
    "value",
    [
        "key=C:/private/replay.rep",
        "file://server/replay.rep",
        "prefix /tmp/replay.rep",
        "/tmp",
        "source:/home/player/replay.rep",
        "source: /var/private/replay.rep",
        "https://host/private/replay.rep",
        "s3://private/replay.rep",
        "source:\\\\server\\share\\replay.rep",
    ],
)
def test_locator_classifier_rejects_embedded_absolute_locator_forms(value: str) -> None:
    with pytest.raises(ValueError, match="paths"):
        available_value(raw_value=value)


@pytest.mark.parametrize("value", ["ratio 1/2", "and/or is benign prose", "the source:player label"])
def test_locator_classifier_preserves_benign_prose(value: str) -> None:
    assert available_value(raw_value=value).raw_value == value


def test_one_public_evidence_id_cannot_claim_multiple_tiers_in_one_document() -> None:
    observed = ReportValue(
        "observed:a",
        "timeline",
        "A",
        1,
        None,
        "available",
        None,
        {},
        None,
        (ReportEvidenceRef(EVIDENCE_A, "observed"),),
        {},
    )
    derived = ReportValue(
        "derived:a",
        "economy",
        "A",
        2,
        None,
        "available",
        None,
        {},
        None,
        (ReportEvidenceRef(EVIDENCE_A, "derived"),),
        {},
    )
    with pytest.raises(ValueError, match="multiple tiers"):
        ReportDocument(
            "replay-report-v1",
            REPORT_ID,
            "replay-report-v1",
            SHA_B,
            SHA_C,
            REPLAY_ID,
            SHA_A,
            None,
            ReportLifecycle("parsed", "complete", None, None),
            (),
            (),
            (observed,),
            (derived,),
            (),
            OllamaReportStatus.not_requested(),
            (),
        )
