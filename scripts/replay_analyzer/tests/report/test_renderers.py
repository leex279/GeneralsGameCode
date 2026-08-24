from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib import resources

import pytest

from generals_replay_analyzer.report.assembly import assemble_report
from generals_replay_analyzer.report.model import (
    OllamaReportStatus,
    ReportAssemblyInput,
    ReportEvidenceRef,
    ReportLifecycle,
    ReportQualityIssue,
    ReportValue,
)
from generals_replay_analyzer.report.render_html import render_html
from generals_replay_analyzer.report.render_json import render_json
from generals_replay_analyzer.report.render_text import render_text
from generals_replay_analyzer.report.resources import load_report_resources, validate_document

REPLAY_ID = "20000000-0000-4000-8000-000000000001"
PLAYER_ID = "20000000-0000-4000-8000-000000000002"
OBSERVED_ID = "20000000-0000-4000-8000-000000000003"
DERIVED_ID = "20000000-0000-4000-8000-000000000004"
INFERRED_ID = "20000000-0000-4000-8000-000000000005"
ISSUE_ID = "20000000-0000-4000-8000-000000000006"
RUN_ID = "20000000-0000-4000-8000-000000000007"


def _claim(
    claim_id: str,
    tier: str,
    raw_value: object,
    *,
    availability: str = "available",
    reason: str | None = None,
    label: str | None = None,
) -> ReportValue:
    public_id = {"observed": OBSERVED_ID, "derived": DERIVED_ID, "inferred": INFERRED_ID}[tier]
    return ReportValue(
        claim_id,
        {"observed": "timeline", "derived": "economy", "inferred": "interpretation"}[tier],
        label or claim_id,
        raw_value,
        "credits" if tier == "derived" else None,
        availability,  # type: ignore[arg-type]
        reason,
        {"scope_type": "player", "public_id": PLAYER_ID},
        (0, 120),
        () if raw_value is None else (ReportEvidenceRef(public_id, tier),),  # type: ignore[arg-type]
        {"raw_detail": "<script>alert('x')</script>"},
    )


def _source(*, ollama: bool = False) -> ReportAssemblyInput:
    unavailable = _claim(
        "derived:missing",
        "derived",
        None,
        availability="unavailable",
        reason="telemetry_missing",
        label="Missing telemetry",
    )
    inferred = ()
    ollama_status = OllamaReportStatus.not_requested()
    if ollama:
        inferred = (_claim("inferred:strategy", "inferred", "pressure", label="Pressure <angle>"),)
        ollama_status = OllamaReportStatus(
            True,
            "succeeded",
            RUN_ID,
            "ollama",
            "qwen3.6:27b",
            "4" * 64,
            "strategy-report-v1",
            "strategy-report-response-v1",
            ("ok",),
            {
                "schema_version": "strategy-report-response-v1",
                "summary": "Use <safe> pressure.",
                "strategy_assessments": [
                    {
                        "claim_id": "strategy-1",
                        "assessment": "Pressure timing.",
                        "evidence_ids": [DERIVED_ID],
                    }
                ],
            },
        )
    return ReportAssemblyInput(
        replay_public_id=REPLAY_ID,
        replay_sha256="1" * 64,
        replay_player_public_id=PLAYER_ID,
        header_identity={"frame_count": 120, "map_name": "Map <unsafe>"},
        component_identity={"parser": {"schema_version": 1}},
        lifecycle=ReportLifecycle("partial", "complete", None, None),
        evidence_availability=(unavailable,),
        quality_issues=(
            ReportQualityIssue(
                ISSUE_ID,
                "telemetry",
                "missing_telemetry",
                "warning",
                {"message": "Required telemetry source is unavailable"},
                False,
            ),
        ),
        observed=(
            _claim("observed:zero", "observed", 0, label="Measured zero"),
            _claim("observed:text", "observed", "<img src=x onerror=alert(1)>", label="Unsafe <label>"),
        ),
        derived=(
            unavailable,
            _claim("derived:precision", "derived", 3.141592653589793, label="Precise ratio"),
        ),
        inferred=inferred,
        ollama=ollama_status,
        warnings=("quality_warning",),
    )


def _document(*, ollama: bool = False):
    report_resources = load_report_resources()
    return assemble_report(
        _source(ollama=ollama),
        html_template_sha256=report_resources.html_template_sha256,
        document_schema_sha256=report_resources.document_schema_sha256,
    )


def test_package_resources_are_exact_digest_checked_and_validate_the_document() -> None:
    report_resources = load_report_resources()
    package = resources.files("generals_replay_analyzer").joinpath("data")

    template = package.joinpath("replay-report-v1.html").read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    schema = package.joinpath("replay-report-v1.schema.json").read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    assert report_resources.html_template == template.decode("utf-8")
    assert report_resources.html_template_sha256 == hashlib.sha256(template).hexdigest()
    assert report_resources.document_schema_sha256 == hashlib.sha256(schema).hexdigest()
    validate_document(_document())


def test_json_is_canonical_utf8_and_retains_raw_precision_and_measured_zero() -> None:
    rendered = render_json(_document())
    parsed = json.loads(rendered)

    assert rendered == json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert (
        next(item for item in parsed["derived"] if item["claim_id"] == "derived:precision")["raw_value"]
        == 3.141592653589793
    )
    assert next(item for item in parsed["observed"] if item["claim_id"] == "observed:zero")["raw_value"] == 0
    assert next(item for item in parsed["derived"] if item["claim_id"] == "derived:missing")["raw_value"] is None


def test_html_and_text_use_fixed_sections_display_rounding_and_never_turn_missing_into_zero() -> None:
    document = _document()
    html = render_html(document)
    text = render_text(document)

    assert html.index("Lifecycle") < html.index("Evidence availability") < html.index("Quality issues")
    assert html.index("Quality issues") < html.index("Observed evidence") < html.index("Derived evidence")
    assert html.index("Derived evidence") < html.index("Inferred evidence") < html.index("Ollama")
    assert "3.142 credits" in html
    assert "unavailable (telemetry_missing)" in html
    assert "Measured zero" in html and ">0<" in html
    assert "3.142 credits" in text
    assert "unavailable (telemetry_missing)" in text
    assert "\r" not in text and text.endswith("\n")
    assert "2026-" not in html and "2026-" not in text


def test_html_escapes_every_adversarial_field_and_contains_no_script() -> None:
    html = render_html(_document(ollama=True))

    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "onerror=" not in html.lower()
    assert "&lt;script&gt;" in html
    assert "Unsafe &lt;label&gt;" in html
    assert "Use &lt;safe&gt; pressure." in html
    assert "Pressure &lt;angle&gt;" in html


def test_validated_prose_is_bounded_to_ollama_and_never_promoted_to_an_evidence_tier() -> None:
    document = _document(ollama=True)
    parsed = json.loads(render_json(document))

    assert parsed["ollama"]["validated_prose"]["summary"] == "Use <safe> pressure."
    assert all(
        item["raw_value"] != "Use <safe> pressure."
        for tier in ("observed", "derived", "inferred")
        for item in parsed[tier]
    )
    assert parsed["ollama"]["validated_prose"]["strategy_assessments"][0]["evidence_ids"] == [DERIVED_ID]


def test_all_renderers_are_identical_across_one_hundred_semantic_permutations() -> None:
    report_resources = load_report_resources()
    source = _source()
    baseline = _document()
    expected = (render_json(baseline), render_html(baseline), render_text(baseline))

    for index in range(100):
        permuted = replace(
            source,
            observed=source.observed[index % 2 :] + source.observed[: index % 2],
            derived=tuple(reversed(source.derived)) if index % 3 else source.derived,
            quality_issues=tuple(reversed(source.quality_issues)),
            warnings=tuple(reversed(source.warnings)),
        )
        document = assemble_report(
            permuted,
            html_template_sha256=report_resources.html_template_sha256,
            document_schema_sha256=report_resources.document_schema_sha256,
        )
        assert (render_json(document), render_html(document), render_text(document)) == expected


@pytest.mark.parametrize(
    ("field", "injected"),
    [
        ("warning", "safe\nforged-section"),
        ("lifecycle_state", "partial\nforged-section"),
        ("parser_completion_status", "complete\rforged-section"),
        ("telemetry_status", "succeeded\nforged-section"),
        ("telemetry_runner_status", "success\rforged-section"),
        ("quality_issue_code", "missing_telemetry\nforged-section"),
        ("quality_severity", "warning\rforged-section"),
    ],
)
def test_text_renderer_rejects_cr_lf_in_every_human_text_field(field: str, injected: str) -> None:
    source = _source()
    if field == "warning":
        source = replace(source, warnings=(injected,))
    elif field.startswith("quality_"):
        issue = source.quality_issues[0]
        issue = replace(
            issue,
            issue_code=injected if field == "quality_issue_code" else issue.issue_code,
            severity=injected if field == "quality_severity" else issue.severity,
        )
        source = replace(source, quality_issues=(issue,))
    else:
        source = replace(source, lifecycle=replace(source.lifecycle, **{field: injected}))
    report_resources = load_report_resources()
    document = assemble_report(
        source,
        html_template_sha256=report_resources.html_template_sha256,
        document_schema_sha256=report_resources.document_schema_sha256,
    )
    with pytest.raises(ValueError, match="line break"):
        render_text(document)
