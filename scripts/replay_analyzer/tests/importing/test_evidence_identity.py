"""Canonical public identities for frozen observed-evidence locators."""

from __future__ import annotations

import json
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from generals_replay_analyzer.importing.evidence_identity import (
    ObservedEvidenceIdentity,
    parser_command_evidence_identities,
    parser_command_evidence_identity,
    telemetry_event_evidence_identities,
    telemetry_event_evidence_identity,
    validate_observed_evidence_identity,
)

REPLAY_PUBLIC_ID = "00000000-0000-0000-0000-00000000a002"
TELEMETRY_RUN_PUBLIC_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_fixed_parser_command_and_telemetry_event_uuid5_vectors() -> None:
    """Catch namespace, field, encoding, or random-factory drift in public citations."""
    parser = parser_command_evidence_identity(REPLAY_PUBLIC_ID, "fixture-parser-1", 4096)
    telemetry = telemetry_event_evidence_identity(TELEMETRY_RUN_PUBLIC_ID, 7)

    assert parser == ObservedEvidenceIdentity(
        public_id="74a46ecd-de5b-555d-850b-ed6eb41250b5",
        source_kind="parser_command",
        source_key=(
            'observed-evidence:parser_command:v1:{"parser_version":"fixture-parser-1",'
            '"replay_public_id":"00000000-0000-0000-0000-00000000a002","start_offset":4096}'
        ),
    )
    assert telemetry == ObservedEvidenceIdentity(
        public_id="f3024034-0534-55b8-8b7a-c7dcf710a1ff",
        source_kind="telemetry_event",
        source_key=(
            'observed-evidence:telemetry_event:v1:{"sequence":7,'
            '"telemetry_run_public_id":"123e4567-e89b-12d3-a456-426614174000"}'
        ),
    )


def test_true_locator_changes_produce_distinct_public_ids() -> None:
    """Catch omission of any frozen locator component from the UUIDv5 name."""
    parser_ids = {
        parser_command_evidence_identity(REPLAY_PUBLIC_ID, "fixture-parser-1", 4096).public_id,
        parser_command_evidence_identity("00000000-0000-0000-0000-00000000a003", "fixture-parser-1", 4096).public_id,
        parser_command_evidence_identity(REPLAY_PUBLIC_ID, "fixture-parser-2", 4096).public_id,
        parser_command_evidence_identity(REPLAY_PUBLIC_ID, "fixture-parser-1", 4112).public_id,
    }
    telemetry_ids = {
        telemetry_event_evidence_identity(TELEMETRY_RUN_PUBLIC_ID, 7).public_id,
        telemetry_event_evidence_identity("123e4567-e89b-12d3-a456-426614174001", 7).public_id,
        telemetry_event_evidence_identity(TELEMETRY_RUN_PUBLIC_ID, 8).public_id,
    }
    assert len(parser_ids) == 4
    assert len(telemetry_ids) == 3
    assert parser_ids.isdisjoint(telemetry_ids)


def test_hundred_locators_are_order_retry_process_and_concurrency_stable() -> None:
    """Catch collection order, module state, process state, or races entering UUIDv5 names."""
    offsets = tuple(4096 + index * 16 for index in range(100))
    sequences = tuple(range(100))
    parser_baseline = parser_command_evidence_identities(REPLAY_PUBLIC_ID, "fixture-parser-1", offsets)
    telemetry_baseline = telemetry_event_evidence_identities(TELEMETRY_RUN_PUBLIC_ID, sequences)

    shuffled_offsets = list(offsets)
    shuffled_sequences = list(sequences)
    random.Random(0x4A11CE).shuffle(shuffled_offsets)
    random.Random(0xC011A7E).shuffle(shuffled_sequences)
    assert parser_command_evidence_identities(REPLAY_PUBLIC_ID, "fixture-parser-1", shuffled_offsets) == parser_baseline
    assert telemetry_event_evidence_identities(TELEMETRY_RUN_PUBLIC_ID, shuffled_sequences) == telemetry_baseline
    assert parser_command_evidence_identities(REPLAY_PUBLIC_ID, "fixture-parser-1", offsets) == parser_baseline
    assert telemetry_event_evidence_identities(TELEMETRY_RUN_PUBLIC_ID, sequences) == telemetry_baseline

    with ThreadPoolExecutor(max_workers=8) as executor:
        concurrent = tuple(
            executor.map(
                lambda _index: parser_command_evidence_identities(
                    REPLAY_PUBLIC_ID, "fixture-parser-1", shuffled_offsets
                ),
                range(32),
            )
        )
    assert set(concurrent) == {parser_baseline}

    script = (
        "import json; "
        "from generals_replay_analyzer.importing.evidence_identity import parser_command_evidence_identities; "
        f"items=parser_command_evidence_identities({REPLAY_PUBLIC_ID!r},'fixture-parser-1',range(4096,5696,16)); "
        "print(json.dumps([[item.public_id,item.source_kind,item.source_key] for item in items],separators=(',',':')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == [
        [item.public_id, item.source_kind, item.source_key] for item in parser_baseline
    ]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: parser_command_evidence_identity(True, "parser-v1", 1), "replay public ID"),
        (lambda: parser_command_evidence_identity("not-a-uuid", "parser-v1", 1), "replay public ID"),
        (lambda: parser_command_evidence_identity(REPLAY_PUBLIC_ID, True, 1), "parser version"),
        (lambda: parser_command_evidence_identity(REPLAY_PUBLIC_ID, "", 1), "parser version"),
        (lambda: parser_command_evidence_identity(REPLAY_PUBLIC_ID, "parser-v1\x7f", 1), "parser version"),
        (lambda: parser_command_evidence_identity(REPLAY_PUBLIC_ID, "parser-v1", True), "start offset"),
        (lambda: parser_command_evidence_identity(REPLAY_PUBLIC_ID, "parser-v1", -1), "start offset"),
        (lambda: telemetry_event_evidence_identity(True, 1), "telemetry run public ID"),
        (lambda: telemetry_event_evidence_identity("not-a-uuid", 1), "telemetry run public ID"),
        (lambda: telemetry_event_evidence_identity(TELEMETRY_RUN_PUBLIC_ID, True), "sequence"),
        (lambda: telemetry_event_evidence_identity(TELEMETRY_RUN_PUBLIC_ID, -1), "sequence"),
        (
            lambda: parser_command_evidence_identities(REPLAY_PUBLIC_ID, "parser-v1", (16, 16)),
            "duplicate parser command locator",
        ),
        (lambda: parser_command_evidence_identities(True, "parser-v1", ()), "replay public ID"),
        (
            lambda: telemetry_event_evidence_identities(TELEMETRY_RUN_PUBLIC_ID, (7, 7)),
            "duplicate telemetry event locator",
        ),
        (lambda: telemetry_event_evidence_identities(True, ()), "telemetry run public ID"),
    ],
)
def test_malformed_boolean_negative_and_duplicate_locators_fail_closed(call: object, message: str) -> None:
    """Catch Python bool/int aliasing or ambiguous locator names reaching public evidence."""
    with pytest.raises((TypeError, ValueError), match=message):
        call()  # type: ignore[operator]


@pytest.mark.parametrize("field", ["public_id", "source_kind", "source_key"])
def test_report_selector_can_recompute_and_reject_observed_identity_drift(field: str) -> None:
    """Catch a report selector accepting a citation that disagrees with its frozen source locator."""
    expected = parser_command_evidence_identity(REPLAY_PUBLIC_ID, "fixture-parser-1", 4096)
    validate_observed_evidence_identity(
        expected,
        public_id=expected.public_id,
        source_kind=expected.source_kind,
        source_key=expected.source_key,
    )
    actual = {
        "public_id": expected.public_id,
        "source_kind": expected.source_kind,
        "source_key": expected.source_key,
    }
    actual[field] += ":drift"
    with pytest.raises(ValueError, match="observed evidence identity drift"):
        validate_observed_evidence_identity(expected, **actual)
