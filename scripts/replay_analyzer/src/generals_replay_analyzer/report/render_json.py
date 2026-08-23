"""Canonical structured replay-report rendering."""

from __future__ import annotations

import json

from generals_replay_analyzer.report.model import ReportDocument, document_to_mapping
from generals_replay_analyzer.report.resources import validate_document


def _render_json_validated(document: ReportDocument) -> bytes:
    return json.dumps(
        document_to_mapping(document),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def render_json(document: ReportDocument) -> bytes:
    """Return the unrounded canonical UTF-8 public document."""
    validate_document(document)
    return _render_json_validated(document)
