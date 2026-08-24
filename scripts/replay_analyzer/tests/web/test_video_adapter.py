from generals_replay_analyzer.web.adapters.video import _evidence_horizon


def test_evidence_horizon_requires_authoritative_lifecycle_not_issue_text() -> None:
    assert _evidence_horizon({"quality_issues": [{"issue_code": "crc_mismatch"}]}) == "partial"
    assert _evidence_horizon({"quality_issues": [], "lifecycle": {"telemetry_status": "complete", "parser_completion_status": "complete"}}) == "complete"
    assert _evidence_horizon({"quality_issues": [{"issue_code": "crc_mismatch"}], "lifecycle": {"telemetry_status": "complete", "parser_completion_status": "complete"}}) == "complete"
    assert _evidence_horizon({"lifecycle": {"telemetry_status": "partial", "parser_completion_status": "complete"}}) == "partial"
