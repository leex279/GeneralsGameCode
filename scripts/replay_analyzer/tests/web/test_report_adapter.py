from __future__ import annotations

from generals_replay_analyzer.web.adapters.report import AnalyticsReportAdapter


def test_report_adapter_module_is_available() -> None:
    assert AnalyticsReportAdapter.__name__ == "AnalyticsReportAdapter"
