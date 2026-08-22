from __future__ import annotations

from dataclasses import replace

from generals_replay_analyzer.report.assembly import assemble_report
from generals_replay_analyzer.report.model import (
    OllamaReportStatus,
    ReportAssemblyInput,
    ReportEvidenceRef,
    ReportLifecycle,
    ReportQualityIssue,
    ReportValue,
    document_to_mapping,
)

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
PLAYER_ID = "10000000-0000-4000-8000-000000000002"
OBSERVED_ID = "10000000-0000-4000-8000-000000000003"
DERIVED_ID = "10000000-0000-4000-8000-000000000004"
ISSUE_ID = "10000000-0000-4000-8000-000000000005"
REPLAY_SHA = "1" * 64
TEMPLATE_SHA = "2" * 64
SCHEMA_SHA = "3" * 64


def _value(claim_id: str, section: str, tier: str, raw: object) -> ReportValue:
    public_id = OBSERVED_ID if tier == "observed" else DERIVED_ID
    return ReportValue(
        claim_id,
        section,
        claim_id,
        raw,
        None,
        "available",
        None,
        {"scope_type": "player", "public_id": PLAYER_ID},
        (0, 60),
        (ReportEvidenceRef(public_id, tier),),  # type: ignore[arg-type]
        {"precision": "raw"},
    )


def _source() -> ReportAssemblyInput:
    return ReportAssemblyInput(
        replay_public_id=REPLAY_ID,
        replay_sha256=REPLAY_SHA,
        replay_player_public_id=PLAYER_ID,
        header_identity={
            "version_number": 104,
            "frame_count": 900,
            "start_time": 1_700_000_000,
            "exe_crc": 1,
            "ini_crc": 2,
            "map_crc": 3,
            "map_name": "Tournament Desert",
            "seed": 4,
        },
        component_identity={
            "parser": {"run_id": "10000000-0000-4000-8000-000000000006", "schema_version": 1},
            "telemetry": {"run_id": "10000000-0000-4000-8000-000000000007", "schema_version": 2},
        },
        lifecycle=ReportLifecycle("engine_verified", "complete", "succeeded", "success"),
        evidence_availability=(),
        quality_issues=(ReportQualityIssue(ISSUE_ID, "telemetry", "resolved_gap", "warning", {"count": 1}, True),),
        observed=(
            _value("observed:z", "timeline", "observed", 0),
            _value("observed:a", "timeline", "observed", 1),
        ),
        derived=(
            _value("derived:z", "economy", "derived", 3.141592653589793),
            _value("derived:a", "economy", "derived", 2),
        ),
        inferred=(),
        ollama=OllamaReportStatus.not_requested(),
        warnings=("z_warning", "a_warning"),
    )


def test_assembly_sorts_semantic_arrays_and_is_byte_stable_across_permutations() -> None:
    source = _source()
    baseline = assemble_report(source, html_template_sha256=TEMPLATE_SHA, document_schema_sha256=SCHEMA_SHA)
    baseline_mapping = document_to_mapping(baseline)

    for index in range(100):
        permuted = replace(
            source,
            observed=source.observed[index % 2 :] + source.observed[: index % 2],
            derived=tuple(reversed(source.derived)) if index % 3 else source.derived,
            quality_issues=tuple(reversed(source.quality_issues)),
            warnings=tuple(reversed(source.warnings)),
        )
        assert (
            document_to_mapping(
                assemble_report(permuted, html_template_sha256=TEMPLATE_SHA, document_schema_sha256=SCHEMA_SHA)
            )
            == baseline_mapping
        )

    assert [item["claim_id"] for item in baseline_mapping["observed"]] == ["observed:a", "observed:z"]
    assert [item["claim_id"] for item in baseline_mapping["derived"]] == ["derived:a", "derived:z"]
    assert baseline_mapping["derived"][1]["raw_value"] == 3.141592653589793


def test_assembly_uses_deterministic_uuidv5_and_separates_input_from_presentation_identity() -> None:
    baseline = assemble_report(_source(), html_template_sha256=TEMPLATE_SHA, document_schema_sha256=SCHEMA_SHA)
    repeated = assemble_report(_source(), html_template_sha256=TEMPLATE_SHA, document_schema_sha256=SCHEMA_SHA)
    template_changed = assemble_report(_source(), html_template_sha256="4" * 64, document_schema_sha256=SCHEMA_SHA)
    semantic_changed = assemble_report(
        replace(_source(), header_identity={"version_number": 104, "frame_count": 901}),
        html_template_sha256=TEMPLATE_SHA,
        document_schema_sha256=SCHEMA_SHA,
    )

    assert baseline == repeated
    assert baseline.report_public_id == "2c8e9ed5-d22e-5ac0-a0f6-2e6296b308f7"
    assert baseline.input_digest == "55a66d037eca3681abc1568c6c620d4f63326ee51beb044e46c90dfd8b4212d5"
    assert baseline.cache_key == "3fb3950ec5f3093c8bc4c21167394e1f0f17563d9eb549c5a98797f740686fdf"
    assert template_changed.input_digest == baseline.input_digest
    assert template_changed.report_public_id == baseline.report_public_id
    assert template_changed.cache_key != baseline.cache_key
    assert semantic_changed.input_digest != baseline.input_digest
    assert semantic_changed.report_public_id != baseline.report_public_id


def test_assembly_identity_changes_for_ollama_only_when_requested() -> None:
    source = _source()
    not_requested = assemble_report(source, html_template_sha256=TEMPLATE_SHA, document_schema_sha256=SCHEMA_SHA)
    requested = assemble_report(
        replace(
            source,
            ollama=OllamaReportStatus(
                True,
                "unavailable",
                "10000000-0000-4000-8000-000000000008",
                "ollama",
                "qwen3.6:27b",
                "5" * 64,
                "strategy-report-v1",
                "strategy-report-response-v1",
                ("model_unavailable",),
                None,
            ),
        ),
        html_template_sha256=TEMPLATE_SHA,
        document_schema_sha256=SCHEMA_SHA,
    )

    assert requested.input_digest != not_requested.input_digest
    assert requested.ollama.status == "unavailable"
