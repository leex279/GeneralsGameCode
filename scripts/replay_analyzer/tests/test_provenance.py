"""Checksum-pinned source provenance tests for the Zero Hour replay fixture."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from shutil import copyfile

import pytest

from generals_replay_analyzer.provenance import SourceProvenance, extract_source_provenance, sha256_file

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"
FIXTURE_SHA256 = "EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8"
SOURCE_FILENAME = "match_3133811_user_e80b96708aa4254945941fd5f81489bb_replay.rep"
STRATA_MATCH_ID = "3133811"
STRATA_SOURCE_USER_TOKEN = "e80b96708aa4254945941fd5f81489bb"


def test_sha256_file_returns_the_pinned_checksum_for_real_replay_bytes() -> None:
    """Reject a changed fixture or a digest computed from text instead of replay bytes."""
    assert sha256_file(FIXTURE_PATH) == FIXTURE_SHA256


def test_extract_source_provenance_reports_external_filename_metadata_for_real_replay_bytes(tmp_path: Path) -> None:
    """Reject missing provenance fields or treating source identifiers as player identity."""
    source_named_replay = tmp_path / SOURCE_FILENAME
    copyfile(FIXTURE_PATH, source_named_replay)

    provenance = extract_source_provenance(source_named_replay)

    assert provenance == SourceProvenance(
        original_filename=SOURCE_FILENAME,
        strata_match_id=STRATA_MATCH_ID,
        strata_source_user_token=STRATA_SOURCE_USER_TOKEN,
        sha256=FIXTURE_SHA256,
    )
    assert provenance.strata_match_id not in {"leex279", "fox27"}
    assert provenance.strata_source_user_token not in {"leex279", "fox27"}


def test_extract_source_provenance_keeps_non_source_filename_metadata_empty() -> None:
    """Reject inferring external provenance from the fixture's player-label filename."""
    provenance = extract_source_provenance(FIXTURE_PATH)

    assert provenance.original_filename == "leex279_vs_fox27.rep"
    assert provenance.strata_match_id is None
    assert provenance.strata_source_user_token is None
    assert provenance.sha256 == FIXTURE_SHA256


def test_source_provenance_is_immutable() -> None:
    """Reject mutable provenance records that could disconnect metadata from a pinned file."""
    provenance = extract_source_provenance(FIXTURE_PATH)

    with pytest.raises(FrozenInstanceError):
        provenance.sha256 = "not-the-fixture-hash"  # type: ignore[misc]


def test_pinned_replay_bytes_do_not_embed_external_source_identifiers() -> None:
    """Reject a test that mistakes filename provenance for replay-internal player data."""
    fixture_bytes = FIXTURE_PATH.read_bytes()

    assert b"3133811" not in fixture_bytes
    assert bytes.fromhex(STRATA_SOURCE_USER_TOKEN) not in fixture_bytes
