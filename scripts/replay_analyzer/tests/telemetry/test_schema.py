"""Behavior tests for the versioned, observed telemetry trace contract."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

RUN_ID = "123e4567-e89b-12d3-a456-426614174000"


def _record(sequence: int, frame: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    """Build a hand-specified telemetry envelope without using production helpers."""
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "sequence": sequence,
        "frame": frame,
        "logic_time_seconds": frame / 30.0,
        "event_type": event_type,
        "payload": payload,
    }


def _manifest_payload() -> dict[str, object]:
    return {
        "engine_build": "zero-hour-1.04",
        "replay_version": "1.04",
        "map_identity": "maps/test.map",
        "initial_seed": 90210,
        "exporter_settings": {"movement_sample_frames": 15},
    }


def _complete_payload(trace_sha256: str) -> dict[str, object]:
    return {
        "final_frame": 30,
        "command_count": 1,
        "event_counts": {"manifest": 1, "object_created": 1, "complete": 1},
        "crc_mismatch": False,
        "replay_truncated": False,
        "clean_shutdown": True,
        "writer_error": None,
        "trace_sha256": trace_sha256,
        "map_assets": [{"path": "maps/test.json", "sha256": "a" * 64}],
    }


def _write_trace(tmp_path: Path, records: list[dict[str, object]], name: str = "trace.ndjson") -> Path:
    """Write an NDJSON fixture exactly as the reader must consume it."""
    trace_path = tmp_path / name
    trace_path.write_bytes(b"".join(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in records))
    return trace_path


def _complete_trace(tmp_path: Path) -> Path:
    """Return a valid three-record trace with a hand-derived pre-completion digest."""
    records = [
        _record(0, 0, "manifest", _manifest_payload()),
        _record(1, 30, "object_created", {"object_id": 248, "template_name": "AmericaVehicleHumvee"}),
    ]
    pre_completion = b"".join(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in records)
    records.append(_record(2, 30, "complete", _complete_payload(hashlib.sha256(pre_completion).hexdigest())))
    return _write_trace(tmp_path, records)


def test_reader_returns_validated_observed_records_with_immutable_evidence_identity(tmp_path: Path) -> None:
    """Catch a reader change that drops the envelope needed to identify an observed event."""
    records = list(iter_validated_trace(_complete_trace(tmp_path)))

    assert [record.sequence for record in records] == [0, 1, 2]
    assert records[1].run_id == UUID(RUN_ID)
    assert records[1].event_type == "object_created"
    assert records[1].payload["object_id"] == 248


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda records: records[1].__setitem__("sequence", 0), "record 2 sequence 0 is not greater than previous sequence 0"),
        (lambda records: records[1].__setitem__("logic_time_seconds", 1.1), "record 2 sequence 1 logic_time_seconds"),
        (lambda records: records[1].__setitem__("schema_version", 2), "record 2 sequence 1 schema_version"),
        (lambda records: records[1].__setitem__("payload", []), "record 2 sequence 1 schema path payload"),
        (lambda records: records[2].__setitem__("event_type", "object_created"), "record 3 sequence 2 must be complete"),
    ],
)
def test_reader_rejects_trace_contract_breaks_with_record_sequence_and_path(
    tmp_path: Path,
    mutate: object,
    error: str,
) -> None:
    """Catch trace corruption that would otherwise make later runner diagnostics ambiguous."""
    trace_path = _complete_trace(tmp_path)
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert callable(mutate)
    mutate(records)
    invalid_path = _write_trace(tmp_path, records, "invalid.ndjson")

    with pytest.raises(TelemetryTraceValidationError, match=error) as raised:
        list(iter_validated_trace(invalid_path))

    assert str(invalid_path) in str(raised.value)


def test_reader_rejects_blank_lines_invalid_utf8_post_completion_and_incomplete_traces(tmp_path: Path) -> None:
    """Catch acceptance of ambiguous, non-terminal, or non-text evidence streams."""
    valid_path = _complete_trace(tmp_path)
    valid_bytes = valid_path.read_bytes()
    cases = {
        "blank.ndjson": valid_bytes.replace(b"\n", b"\n\n", 1),
        "invalid-utf8.ndjson": b"\xff\n",
        "after-complete.ndjson": valid_bytes + valid_bytes.splitlines()[1] + b"\n",
        "incomplete.ndjson": b"\n".join(valid_bytes.splitlines()[:-1]) + b"\n",
    }

    for filename, contents in cases.items():
        trace_path = tmp_path / filename
        trace_path.write_bytes(contents)
        with pytest.raises(TelemetryTraceValidationError, match="trace") as raised:
            list(iter_validated_trace(trace_path))
        assert str(trace_path) in str(raised.value)
