"""Canonical public identities for immutable observed-evidence locators."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

_OBSERVED_EVIDENCE_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "generals-replay-analyzer:observed-evidence:v1",
)


@dataclass(frozen=True)
class ObservedEvidenceIdentity:
    """Public UUID and source identity derived from one frozen locator."""

    public_id: str
    source_kind: str
    source_key: str


def _canonical_uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical UUID string") from error
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID string")
    return value


def _parser_version(value: object) -> str:
    if type(value) is not str:
        raise TypeError("parser version must be a nonempty canonical string")
    if not value or value != value.strip() or len(value) > 255 or not value.isprintable():
        raise ValueError("parser version must be a nonempty canonical string")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be a nonnegative integer")
    if value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _identity(source_kind: str, locator: dict[str, object]) -> ObservedEvidenceIdentity:
    source_key = f"observed-evidence:{source_kind}:v1:" + json.dumps(
        locator, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return ObservedEvidenceIdentity(
        public_id=str(uuid5(_OBSERVED_EVIDENCE_NAMESPACE, source_key)),
        source_kind=source_kind,
        source_key=source_key,
    )


# TheSuperHackers @feature Leex 23/08/2026 Derive parser citations from replay, parser version, and byte offset. (#TBD)
def parser_command_evidence_identity(
    replay_public_id: object,
    parser_version: object,
    start_offset: object,
) -> ObservedEvidenceIdentity:
    """Return the UUIDv5 identity for one parser command byte locator."""

    return _identity(
        "parser_command",
        {
            "parser_version": _parser_version(parser_version),
            "replay_public_id": _canonical_uuid(replay_public_id, "replay public ID"),
            "start_offset": _nonnegative_int(start_offset, "start offset"),
        },
    )


# TheSuperHackers @feature Leex 23/08/2026 Derive telemetry citations from the public run UUID and sequence. (#TBD)
def telemetry_event_evidence_identity(
    telemetry_run_public_id: object,
    sequence: object,
) -> ObservedEvidenceIdentity:
    """Return the UUIDv5 identity for one telemetry event locator."""

    return _identity(
        "telemetry_event",
        {
            "sequence": _nonnegative_int(sequence, "sequence"),
            "telemetry_run_public_id": _canonical_uuid(
                telemetry_run_public_id,
                "telemetry run public ID",
            ),
        },
    )


def parser_command_evidence_identities(
    replay_public_id: object,
    parser_version: object,
    start_offsets: Iterable[object],
) -> tuple[ObservedEvidenceIdentity, ...]:
    """Canonicalize a parser locator set and reject duplicate byte identities."""

    canonical_replay_public_id = _canonical_uuid(replay_public_id, "replay public ID")
    canonical_parser_version = _parser_version(parser_version)
    offsets = tuple(_nonnegative_int(value, "start offset") for value in start_offsets)
    if len(offsets) != len(set(offsets)):
        raise ValueError("duplicate parser command locator")
    return tuple(
        parser_command_evidence_identity(canonical_replay_public_id, canonical_parser_version, offset)
        for offset in sorted(offsets)
    )


def telemetry_event_evidence_identities(
    telemetry_run_public_id: object,
    sequences: Iterable[object],
) -> tuple[ObservedEvidenceIdentity, ...]:
    """Canonicalize a telemetry locator set and reject duplicate sequences."""

    canonical_run_public_id = _canonical_uuid(telemetry_run_public_id, "telemetry run public ID")
    normalized = tuple(_nonnegative_int(value, "sequence") for value in sequences)
    if len(normalized) != len(set(normalized)):
        raise ValueError("duplicate telemetry event locator")
    return tuple(
        telemetry_event_evidence_identity(canonical_run_public_id, sequence) for sequence in sorted(normalized)
    )


def validate_observed_evidence_identity(
    expected: ObservedEvidenceIdentity,
    *,
    public_id: object,
    source_kind: object,
    source_key: object,
) -> None:
    """Fail closed unless a stored citation exactly matches its recomputed locator."""

    if (
        type(public_id) is not str
        or type(source_kind) is not str
        or type(source_key) is not str
        or public_id != expected.public_id
        or source_kind != expected.source_kind
        or source_key != expected.source_key
    ):
        raise ValueError("observed evidence identity drift")
