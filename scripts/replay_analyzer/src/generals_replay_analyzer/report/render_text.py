"""Deterministic LF-only plain-text report rendering."""

from __future__ import annotations

import json

from generals_replay_analyzer.report.model import CanonicalValue, ReportDocument, ReportValue, thaw_report_value
from generals_replay_analyzer.report.resources import validate_document


def _safe_text(value: object) -> str:
    rendered = str(value)
    if "\r" in rendered or "\n" in rendered:
        raise ValueError("report text fields may not contain a line break")
    return rendered


def _canonical_text(value: CanonicalValue) -> str:
    return json.dumps(
        thaw_report_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _display_value(value: ReportValue) -> str:
    if value.availability == "unavailable":
        return f"unavailable ({_safe_text(value.unavailable_reason)})"
    raw = value.raw_value
    if type(raw) is float:
        rendered = f"{raw:.3f}".rstrip("0").rstrip(".")
    elif type(raw) is bool:
        rendered = str(raw).lower()
    elif type(raw) in (str, int):
        rendered = _safe_text(raw)
    else:
        rendered = _canonical_text(raw)
    unit = "" if value.unit is None else f" {_safe_text(value.unit)}"
    partial = "" if value.availability == "available" else f" (partial: {_safe_text(value.unavailable_reason)})"
    return f"{rendered}{unit}{partial}"


def _values(title: str, values: tuple[ReportValue, ...]) -> list[str]:
    lines = [title]
    lines.extend(f"- {_safe_text(value.label)}: {_display_value(value)}" for value in values)
    if not values:
        lines.append("- none")
    return lines


def _render_text_validated(document: ReportDocument) -> str:
    lifecycle = document.lifecycle
    lines = [
        "Replay analysis report",
        f"Report: {document.report_public_id}",
        f"Replay: {document.replay_public_id}",
        f"Player: {document.replay_player_public_id or 'all players'}",
        "",
        "Lifecycle",
        f"- State: {_safe_text(lifecycle.lifecycle_state)}",
        f"- Parser: {_safe_text(lifecycle.parser_completion_status or 'unavailable')}",
        f"- Telemetry: {_safe_text(lifecycle.telemetry_status or 'unavailable')}",
        f"- Runner: {_safe_text(lifecycle.telemetry_runner_status or 'unavailable')}",
        "",
        *_values("Evidence availability", document.evidence_availability),
        "",
        "Quality issues",
        *(
            [
                f"- {_safe_text(issue.issue_code)} [{_safe_text(issue.severity)}]: {_canonical_text(issue.details)}"
                for issue in document.quality_issues
            ]
            or ["- none"]
        ),
        "",
        *_values("Observed evidence", document.observed),
        "",
        *_values("Derived evidence", document.derived),
        "",
        *_values("Inferred evidence", document.inferred),
        "",
        "Ollama",
        f"- Status: {_safe_text(document.ollama.status)}",
        f"- Validated prose: {'none' if document.ollama.validated_prose is None else _canonical_text(document.ollama.validated_prose)}",
        "",
        "Warnings",
        *([f"- {_safe_text(warning)}" for warning in document.warnings] or ["- none"]),
    ]
    return "\n".join(lines) + "\n"


def render_text(document: ReportDocument) -> str:
    """Render the stable fixed-order human-readable report without environment metadata."""
    validate_document(document)
    return _render_text_validated(document)
