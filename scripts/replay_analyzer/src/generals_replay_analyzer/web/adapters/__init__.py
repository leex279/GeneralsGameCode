"""Production adapters for immutable web application ports."""

from .analytics import AnalyticsJobsAdapter
from .report import AnalyticsReportAdapter

__all__ = ["AnalyticsJobsAdapter", "AnalyticsReportAdapter"]
