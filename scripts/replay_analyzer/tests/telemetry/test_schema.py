"""Behavior tests for the versioned, observed telemetry trace contract."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from generals_replay_analyzer.telemetry.model import EVENT_TYPES
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
        _record(
            1,
            30,
            "object_created",
            {
                "object_id": 248,
                "template_name": "AmericaVehicleHumvee",
                "owner_player_index": 0,
                "team_id": 0,
                "position": {"x": 10.0, "y": 20.0, "z": 0.0},
                "orientation": 0.0,
                "kind_of_flags": ["VEHICLE"],
                "creation_source": "player_created",
            },
        ),
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
    assert records[1].payload.object_id == 248


def test_reader_rejects_object_creation_without_authoritative_lifecycle_identity(tmp_path: Path) -> None:
    """Catch a schema regression that permits object observations without their replay-local identity."""
    trace_path = _complete_trace(tmp_path)
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    records[1]["payload"].pop("object_id")
    invalid_path = _write_trace(tmp_path, records, "missing-object-identity.ndjson")

    with pytest.raises(
        TelemetryTraceValidationError,
        match=r"record 2 sequence 1 schema path payload\.object_id",
    ):
        list(iter_validated_trace(invalid_path))


@pytest.mark.parametrize(
    ("event_type", "payload", "required_path"),
    [
        ("players_initialized", {"game_data_catalog": {"path": "catalog.json", "sha256": "a" * 64}}, "players"),
        ("cash_changed", {"before": 100, "delta": -10, "after": 90, "track_income": False, "reason": "unknown"}, "player_index"),
        ("damage_applied", {"attacker_object_id": None, "weapon_name": None, "attempted_amount": 1.0, "applied_amount": 1.0, "prior_health": 2.0, "new_health": 1.0, "damage_type": "normal", "death_type": "none", "location": {"x": 0.0, "y": 0.0, "z": 0.0}, "killing_blow": False}, "victim_object_id"),
        ("order_issued", {"message_name": None, "source_player_index": 0, "selected_object_ids": [], "target_object_id": None, "target_location": None, "command_source": "replay"}, "message_type"),
        ("entity_sample", {"object_id": 1, "orientation": 0.0, "layer": 0, "speed": 0.0, "current_state": "idle"}, "position"),
    ],
)
def test_reader_rejects_empty_payloads_for_later_engine_event_families(
    tmp_path: Path, event_type: str, payload: dict[str, object], required_path: str
) -> None:
    """Catch loss of the observed fields that later game-data, economy, combat, order, and spatial stages consume."""
    records = [
        _record(0, 0, "manifest", _manifest_payload()),
        _record(1, 0, event_type, payload),
    ]
    pre_completion = b"".join(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in records)
    completion = _complete_payload(hashlib.sha256(pre_completion).hexdigest())
    completion["final_frame"] = 0
    records.append(_record(2, 0, "complete", completion))
    trace_path = _write_trace(tmp_path, records, f"empty-{event_type}.ndjson")

    with pytest.raises(
        TelemetryTraceValidationError,
        match=rf"record 2 sequence 1 schema path payload\.{required_path}",
    ):
        list(iter_validated_trace(trace_path))


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda records: records[1].__setitem__("run_id", "not-a-uuid"), r"schema path run_id"),
        (lambda records: records[1].__setitem__("sequence", -1), r"schema path sequence"),
        (lambda records: records[1].__setitem__("frame", -1), r"schema path frame"),
    ],
)
def test_reader_reports_precise_envelope_paths_for_invalid_observed_identity(
    tmp_path: Path, mutate: Callable[[list[dict[str, object]]], None], error: str
) -> None:
    """Catch envelope validation that hides the malformed identity field behind a top-level oneOf error."""
    trace_path = _complete_trace(tmp_path)
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    mutate(records)
    invalid_path = _write_trace(tmp_path, records, "invalid-envelope.ndjson")

    with pytest.raises(TelemetryTraceValidationError, match=rf"record 2 sequence .* {error}"):
        list(iter_validated_trace(invalid_path))


def test_reader_rejects_decreasing_sequence_after_a_valid_observation(tmp_path: Path) -> None:
    """Catch a sequence comparison that permits a later observed fact to reuse an earlier evidence position."""
    trace_path = _complete_trace(tmp_path)
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    records[2]["sequence"] = 0
    invalid_path = _write_trace(tmp_path, records, "decreasing-sequence.ndjson")

    with pytest.raises(
        TelemetryTraceValidationError,
        match=r"record 3 sequence 0 is not greater than previous sequence 1",
    ):
        list(iter_validated_trace(invalid_path))


def test_reader_reports_missing_completion_field_and_bad_completion_hash(tmp_path: Path) -> None:
    """Catch terminal records that claim completion without complete diagnostics or byte-accurate identity."""
    trace_path = _complete_trace(tmp_path)
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    records[2]["payload"].pop("command_count")
    missing_field_path = _write_trace(tmp_path, records, "missing-completion-field.ndjson")

    with pytest.raises(
        TelemetryTraceValidationError,
        match=r"record 3 sequence 2 schema path payload\.command_count",
    ):
        list(iter_validated_trace(missing_field_path))

    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    records[2]["payload"]["trace_sha256"] = "b" * 64
    bad_hash_path = _write_trace(tmp_path, records, "bad-completion-hash.ndjson")

    with pytest.raises(
        TelemetryTraceValidationError,
        match=r"record 3 sequence 2 complete trace_sha256 does not match prior trace bytes",
    ):
        list(iter_validated_trace(bad_hash_path))


def test_reader_exposes_no_early_records_when_terminal_validation_fails(tmp_path: Path) -> None:
    """Catch streaming validation that leaks a manifest before an incomplete trace is known to be unusable evidence."""
    trace_path = _complete_trace(tmp_path)
    incomplete_path = tmp_path / "incomplete-first-item.ndjson"
    incomplete_path.write_bytes(b"\n".join(trace_path.read_bytes().splitlines()[:-1]) + b"\n")

    with pytest.raises(TelemetryTraceValidationError, match=r"record 2 sequence 1 must be complete"):
        next(iter_validated_trace(incomplete_path))


def test_python_event_types_match_the_packaged_schema_event_contract() -> None:
    """Catch one surface accepting an event family that the installed v1 schema cannot validate."""
    schema_path = Path(__file__).parents[2] / "contracts" / "telemetry-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_events = set(schema["$defs"]["eventPayloads"])

    assert schema_events | {"manifest", "complete"} == set(EVENT_TYPES)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda records: records[1].__setitem__("sequence", 0), "record 2 sequence 0 is not greater than previous sequence 0"),
        (lambda records: records[1].__setitem__("logic_time_seconds", 1.1), "record 2 sequence 1 logic_time_seconds"),
        (lambda records: records[1].__setitem__("schema_version", 2), "record 2 sequence 1 schema_version"),
        (lambda records: records[1].__setitem__("payload", []), "record 2 sequence 1 schema path payload"),
        (
            lambda records: records[2].update(
                {"event_type": "match_outcome", "payload": {"outcome": "unknown", "winner_player_index": None}}
            ),
            "record 3 sequence 2 must be complete",
        ),
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
