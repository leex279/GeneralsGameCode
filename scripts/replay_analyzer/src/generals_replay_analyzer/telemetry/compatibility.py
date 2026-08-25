"""Narrow compatibility handling for known native telemetry producer extensions."""

from __future__ import annotations

import hashlib
import json
import os
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
    victim_templates: dict[int, str | None] = {}
    temporary = trace_path.with_name(f".{trace_path.name}.compat-{os.getpid()}.tmp")
    original_digest = hashlib.sha256()
    prepared_digest = hashlib.sha256()
    pending: bytes | None = None
    try:
        with trace_path.open("rb") as source, temporary.open("wb") as destination:
            for raw_line in source:
                if pending is None:
                    pending = raw_line
                    continue
                original_digest.update(pending)
                try:
                    decoded = json.loads(pending)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None, {}
                prepared = pending
                payload = decoded.get("payload") if isinstance(decoded, dict) else None
                if (
                    isinstance(decoded, dict)
                    and decoded.get("schema_version") == 2
                    and decoded.get("event_type") == "damage_applied"
                    and isinstance(payload, dict)
                    and "victim_template_name" in payload
                ):
                    sequence = decoded.get("sequence")
                    value = payload.get("victim_template_name")
                    if type(sequence) is not int or sequence in victim_templates or (
                        value is not None and (not isinstance(value, str) or not value)
                    ):
                        return None, {}
                    victim_templates[sequence] = value
                    payload.pop("victim_template_name")
                    prepared = (
                        json.dumps(decoded, separators=(",", ":"), allow_nan=False).encode("utf-8")
                        + _trace_line_ending(pending)
                    )
                destination.write(prepared)
                prepared_digest.update(prepared)
                pending = raw_line
            if not victim_templates:
                return None, {}
            try:
                complete = json.loads(pending) if pending is not None else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, {}
            complete_payload = complete.get("payload") if isinstance(complete, dict) else None
            if (
                not isinstance(complete, dict)
                or complete.get("schema_version") != 2
                or complete.get("event_type") != "complete"
                or not isinstance(complete_payload, dict)
            ):
                return None, {}
            original_trace_sha256 = complete_payload.get("trace_sha256")
            if not isinstance(original_trace_sha256, str) or original_digest.hexdigest() != original_trace_sha256:
                return None, {}
            complete_payload["trace_sha256"] = prepared_digest.hexdigest()
            assert pending is not None
            destination.write(
                json.dumps(complete, separators=(",", ":"), allow_nan=False).encode("utf-8")
                + _trace_line_ending(pending)
            )
        # TheSuperHackers @performance Leex 26/08/2026 Stream the production compatibility bridge one line at a time. (#TBD)
        os.replace(temporary, trace_path)
        return original_trace_sha256, victim_templates
    finally:
        temporary.unlink(missing_ok=True)
