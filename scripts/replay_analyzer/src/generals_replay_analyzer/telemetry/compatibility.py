"""Narrow compatibility handling for known native telemetry producer extensions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _trace_line_ending(raw_line: bytes) -> bytes:
    if raw_line.endswith(b"\r\n"):
        return b"\r\n"
    if raw_line.endswith(b"\n"):
        return b"\n"
    return b""


# TheSuperHackers @fix Leex 25/08/2026 Bridge only the known Zero Hour victim-template producer skew while retaining its observed evidence. (#TBD)
def bridge_v2_damage_victim_template_name(trace_path: Path) -> tuple[str | None, dict[int, str | None]]:
    """Prepare the one known v2 producer extension for the protected strict telemetry reader.

    The original trace hash is verified before rewriting the disposable validation copy, and the
    extension is returned by sequence so persistence can retain it after strict validation.
    """
    source = trace_path.read_bytes()
    lines = source.splitlines(keepends=True)
    decoded_lines: list[dict[str, object] | None] = []
    victim_templates: dict[int, str | None] = {}
    changed_indices: set[int] = set()
    for index, raw_line in enumerate(lines):
        try:
            decoded = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, {}
        if not isinstance(decoded, dict):
            decoded_lines.append(None)
            continue
        decoded_lines.append(decoded)
        payload = decoded.get("payload")
        if (
            decoded.get("schema_version") != 2
            or decoded.get("event_type") != "damage_applied"
            or not isinstance(payload, dict)
            or "victim_template_name" not in payload
        ):
            continue
        sequence = decoded.get("sequence")
        value = payload.get("victim_template_name")
        if type(sequence) is not int or sequence in victim_templates or (
            value is not None and (not isinstance(value, str) or not value)
        ):
            return None, {}
        victim_templates[sequence] = value
        payload.pop("victim_template_name")
        changed_indices.add(index)

    if not changed_indices:
        return None, {}
    complete = decoded_lines[-1] if decoded_lines else None
    complete_payload = complete.get("payload") if isinstance(complete, dict) else None
    if (
        not isinstance(complete, dict)
        or complete.get("schema_version") != 2
        or complete.get("event_type") != "complete"
        or not isinstance(complete_payload, dict)
    ):
        return None, {}
    original_trace_sha256 = complete_payload.get("trace_sha256")
    if not isinstance(original_trace_sha256, str):
        return None, {}
    if hashlib.sha256(b"".join(lines[:-1])).hexdigest() != original_trace_sha256:
        return None, {}

    prepared_lines = list(lines)
    for index in changed_indices:
        decoded = decoded_lines[index]
        assert decoded is not None
        prepared_lines[index] = (
            json.dumps(decoded, separators=(",", ":"), allow_nan=False).encode("utf-8")
            + _trace_line_ending(lines[index])
        )
    complete_payload["trace_sha256"] = hashlib.sha256(b"".join(prepared_lines[:-1])).hexdigest()
    prepared_lines[-1] = (
        json.dumps(complete, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + _trace_line_ending(lines[-1])
    )
    trace_path.write_bytes(b"".join(prepared_lines))
    return original_trace_sha256, victim_templates
