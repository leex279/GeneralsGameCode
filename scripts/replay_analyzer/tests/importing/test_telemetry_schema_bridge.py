"""Regression coverage for importer-local telemetry producer compatibility."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from telemetry.test_combat_outcome_contract import RUN_ID, _valid_trace

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.importing.telemetry_import import (
    ManagedTelemetryArtifact,
    TelemetryAttempt,
    TelemetryObservationImporter,
    _VerifiedArtifact,
)
from generals_replay_analyzer.telemetry import load_validated_telemetry_bundle
from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError


def _add_damage_field(trace: Path, field_name: str, value: object) -> str:
    """Add one producer field and re-attest the fixture as an engine would."""
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    damage = next(record for record in records if record["event_type"] == "damage_applied")
    damage["payload"][field_name] = value
    raw_records = b"".join(
        json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in records[:-1]
    )
    source_trace_sha256 = hashlib.sha256(raw_records).hexdigest()
    records[-1]["payload"]["trace_sha256"] = source_trace_sha256
    trace.write_bytes(
        b"".join(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in records)
    )
    return source_trace_sha256


def _normalized_importer(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, trace: Path
) -> tuple[TelemetryObservationImporter, tuple[_VerifiedArtifact, ...], TelemetryAttempt]:
    """Build the real importer normalization inputs without database asset registration."""
    descriptors: list[ManagedTelemetryArtifact] = []
    verified: list[_VerifiedArtifact] = []
    for index, path in enumerate(sorted(item for item in trace.parent.rglob("*") if item.is_file()), start=1):
        raw = path.read_bytes()
        kind = (
            "telemetry_trace"
            if path == trace
            else "telemetry_catalog"
            if path.name.startswith("game-data-catalog")
            else "telemetry_map_asset"
        )
        descriptor = ManagedTelemetryArtifact(
            f"00000000-0000-0000-0000-{index:012d}",
            kind,
            path.relative_to(trace.parent).as_posix(),
            hashlib.sha256(raw).hexdigest(),
            len(raw),
        )
        descriptors.append(descriptor)
        verified.append(
            _VerifiedArtifact(descriptor, index, path, kind, descriptor.sha256, descriptor.size_bytes)
        )
    manifest = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
    attempt = TelemetryAttempt(
        run_id=RUN_ID,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        process_exit_code=0,
        engine_build=manifest["payload"]["engine_build"],
        engine_executable_sha256="b" * 64,
        diagnostics=(),
        artifacts=tuple(descriptors),
    )
    return (
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=lambda: datetime.now(UTC),
        ),
        tuple(verified),
        attempt,
    )


def test_importer_bridges_known_damage_victim_template_name_without_losing_observed_evidence(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch dropping a known producer-side v2 combat identity during importer compatibility handling."""
    trace_root = settings.data_root / "bridge-known"
    trace_root.mkdir()
    trace = _valid_trace(trace_root)
    source_trace_sha256 = _add_damage_field(trace, "victim_template_name", "ChinaTankBattleMaster")

    with pytest.raises(TelemetryTraceValidationError, match="victim_template_name.*unexpected"):
        load_validated_telemetry_bundle(trace)

    importer, verified, attempt = _normalized_importer(session_factory, settings, trace)
    normalized = importer._load_normalized_bundle(verified, attempt)

    damage_payload = next(
        payload for record, payload in zip(normalized.bundle.records, normalized.payloads, strict=True)
        if record.event_type == "damage_applied"
    )
    damage_raw_record = next(
        raw_record for record, raw_record in zip(normalized.bundle.records, normalized.records, strict=True)
        if record.event_type == "damage_applied"
    )
    assert damage_payload["victim_template_name"] == "ChinaTankBattleMaster"
    assert damage_raw_record["payload"]["victim_template_name"] == "ChinaTankBattleMaster"
    assert normalized.source_trace_sha256 == source_trace_sha256


def test_importer_does_not_bridge_other_damage_payload_extensions(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch widening compatibility handling into silent unknown-field removal."""
    trace_root = settings.data_root / "bridge-reject-unknown"
    trace_root.mkdir()
    trace = _valid_trace(trace_root)
    _add_damage_field(trace, "unrecognized_combat_payload_extension", "not-supported")

    importer, verified, attempt = _normalized_importer(session_factory, settings, trace)

    with pytest.raises(TelemetryTraceValidationError, match="unrecognized_combat_payload_extension.*unexpected"):
        importer._load_normalized_bundle(verified, attempt)
