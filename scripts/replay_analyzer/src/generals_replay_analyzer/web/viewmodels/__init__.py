"""View-safe player coaching and replay-library mappings."""

from .coaching import CoachingViewModel, coaching_view
from .report import ReplayReportViewModel, replay_report_view

__all__ = ("CoachingViewModel", "ReplayReportViewModel", "coaching_view", "replay_report_view")
