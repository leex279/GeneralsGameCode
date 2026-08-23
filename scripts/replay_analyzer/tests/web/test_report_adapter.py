from __future__ import annotations

from generals_replay_analyzer.web.adapters.report import AnalyticsReportAdapter
from generals_replay_analyzer.web.viewmodels.report import ReplayReportViewModel


def test_report_adapter_module_is_available() -> None:
    assert AnalyticsReportAdapter.__name__ == "AnalyticsReportAdapter"
    assert "coaching" in ReplayReportViewModel.model_fields
