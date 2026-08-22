"""Versioned evidence-backed replay report contracts."""

from generals_replay_analyzer.report.assembly import assemble_report
from generals_replay_analyzer.report.model import (
    OllamaReportStatus,
    ReportAssemblyInput,
    ReportAssetDTO,
    ReportDocument,
    ReportEvidenceRef,
    ReportFormat,
    ReportLifecycle,
    ReportQualityIssue,
    ReportReceipt,
    ReportRequest,
    ReportValue,
    document_to_mapping,
)

__all__ = [
    "OllamaReportStatus",
    "ReportAssemblyInput",
    "ReportAssetDTO",
    "ReportDocument",
    "ReportEvidenceRef",
    "ReportFormat",
    "ReportLifecycle",
    "ReportQualityIssue",
    "ReportReceipt",
    "ReportRequest",
    "ReportValue",
    "assemble_report",
    "document_to_mapping",
]
