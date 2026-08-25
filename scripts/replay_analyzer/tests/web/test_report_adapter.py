from __future__ import annotations

from generals_replay_analyzer.report.model import ReportEvidenceRef, ReportValue
from generals_replay_analyzer.web.adapters.report import AnalyticsReportAdapter, _claim
from generals_replay_analyzer.web.viewmodels.report import ReplayReportViewModel


def test_report_adapter_module_is_available() -> None:
    assert AnalyticsReportAdapter.__name__ == "AnalyticsReportAdapter"
    assert "coaching" in ReplayReportViewModel.model_fields


def test_report_adapter_bounds_evidence_projection_and_preserves_total_count() -> None:
    """Catch high-cardinality feature provenance rebuilding tens of thousands of Web DTOs."""
    evidence = tuple(
        ReportEvidenceRef(
            public_id=f"123e4567-e89b-42d3-a456-42661417{index:04d}",
            tier="observed",
        )
        for index in range(7)
    )
    value = ReportValue(
        claim_id="feature:combat.damage:fixture",
        section="combat",
        label="combat.applied_damage_dealt",
        raw_value=123,
        unit="damage",
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "player", "public_id": "123e4567-e89b-42d3-a456-426614174100"},
        frame_window=(0, 300),
        evidence=evidence,
        details={"definition_version": "fixture-v1"},
    )

    claim = _claim(value, "derived")

    assert len(claim.evidence) == 5
    assert claim.evidence_total_count == 7
