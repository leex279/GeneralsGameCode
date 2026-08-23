"""Deterministic, escaped human-readable HTML report rendering."""

from __future__ import annotations

import html
import json

from generals_replay_analyzer.report.model import (
    CanonicalValue,
    ReportDocument,
    ReportQualityIssue,
    ReportValue,
    thaw_report_value,
)
from generals_replay_analyzer.report.resources import load_report_resources, validate_document


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True).replace("=", "&#61;")


def _canonical_text(value: CanonicalValue) -> str:
    return json.dumps(
        thaw_report_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _display_value(value: ReportValue) -> str:
    if value.availability == "unavailable":
        return f'<span class="unavailable">unavailable ({_escape(value.unavailable_reason)})</span>'
    raw = value.raw_value
    if type(raw) is float:
        rendered = f"{raw:.3f}".rstrip("0").rstrip(".")
    elif raw is None:
        rendered = "unavailable"
    elif type(raw) in (str, bool, int):
        rendered = str(raw).lower() if type(raw) is bool else str(raw)
    else:
        rendered = _canonical_text(raw)
    suffix = "" if value.unit is None else f" {_escape(value.unit)}"
    partial = "" if value.availability == "available" else f" (partial: {_escape(value.unavailable_reason)})"
    return f"{_escape(rendered)}{suffix}{partial}"


def _values(title: str, values: tuple[ReportValue, ...]) -> str:
    rows: list[str] = []
    for value in values:
        evidence = ", ".join(f"{ref.tier}:{ref.public_id}" for ref in value.evidence) or "none"
        details = _escape(_canonical_text(value.details))
        rows.append(
            "<tr>"
            f"<th>{_escape(value.label)}</th><td>{_display_value(value)}</td>"
            f"<td><code>{_escape(evidence)}</code></td><td><code>{details}</code></td>"
            "</tr>"
        )
    body = "".join(rows) if rows else '<tr><td colspan="4">none</td></tr>'
    return (
        f"<section><h2>{title}</h2><table><thead><tr><th>Claim</th><th>Value</th>"
        f"<th>Evidence</th><th>Details</th></tr></thead><tbody>{body}</tbody></table></section>"
    )


def _issues(values: tuple[ReportQualityIssue, ...]) -> str:
    rows = "".join(
        "<tr>"
        f"<th>{_escape(value.issue_code)}</th><td>{_escape(value.stage)}</td><td>{_escape(value.severity)}</td>"
        f"<td><code>{_escape(_canonical_text(value.details))}</code></td>"
        "</tr>"
        for value in values
    )
    if not rows:
        rows = '<tr><td colspan="4">none</td></tr>'
    return (
        "<section><h2>Quality issues</h2><table><thead><tr><th>Issue</th><th>Stage</th>"
        f"<th>Severity</th><th>Details</th></tr></thead><tbody>{rows}</tbody></table></section>"
    )


def _render_html_validated(document: ReportDocument) -> str:
    loaded = load_report_resources()
    lifecycle = document.lifecycle
    parts = [
        "<main>",
        "<h1>Replay analysis report</h1>",
        (
            "<section><h2>Identity</h2><dl>"
            f"<dt>Report</dt><dd><code>{_escape(document.report_public_id)}</code></dd>"
            f"<dt>Replay</dt><dd><code>{_escape(document.replay_public_id)}</code></dd>"
            f"<dt>Player</dt><dd><code>{_escape(document.replay_player_public_id or 'all players')}</code></dd>"
            "</dl></section>"
        ),
        (
            "<section><h2>Lifecycle</h2><dl>"
            f"<dt>State</dt><dd>{_escape(lifecycle.lifecycle_state)}</dd>"
            f"<dt>Parser</dt><dd>{_escape(lifecycle.parser_completion_status or 'unavailable')}</dd>"
            f"<dt>Telemetry</dt><dd>{_escape(lifecycle.telemetry_status or 'unavailable')}</dd>"
            f"<dt>Runner</dt><dd>{_escape(lifecycle.telemetry_runner_status or 'unavailable')}</dd>"
            "</dl></section>"
        ),
        _values("Evidence availability", document.evidence_availability),
        _issues(document.quality_issues),
        _values("Observed evidence", document.observed),
        _values("Derived evidence", document.derived),
        _values("Inferred evidence", document.inferred),
        (
            "<section><h2>Ollama</h2><dl>"
            f"<dt>Status</dt><dd>{_escape(document.ollama.status)}</dd>"
            f"<dt>Validated prose</dt><dd><code>{_escape('none' if document.ollama.validated_prose is None else _canonical_text(document.ollama.validated_prose))}</code></dd>"
            "</dl></section>"
        ),
        "</main>",
    ]
    return loaded.html_template.replace("{{REPORT_BODY}}", "".join(parts))


def render_html(document: ReportDocument) -> str:
    """Render one report through the exact pinned script-free template."""
    validate_document(document)
    return _render_html_validated(document)
