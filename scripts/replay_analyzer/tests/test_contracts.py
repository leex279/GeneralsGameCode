"""Tests for the provisional, generated Zero Hour message-name catalog."""

import json
from pathlib import Path

import pytest

from generals_replay_analyzer.contracts import (
    MessageCatalogValidationError,
    load_message_catalog,
    message_name_for,
)


def _catalog_document(entries: list[dict[str, object]]) -> dict[str, object]:
    """Return the smallest complete generated-catalog document for validation tests."""
    return {
        "schema_version": 1,
        "game": "Command & Conquer: Generals Zero Hour",
        "patch": "1.04",
        "engine_build": "retail-1.04",
        "source_header_path": "Core/GameEngine/Include/Common/MessageStream.h",
        "generated_at_utc": "2026-08-19T00:00:00Z",
        "generated": True,
        "generation_note": "Provisional source-transcribed/generated artifact; Task 9 replaces it from C++ output.",
        "message_types": entries,
    }


def _write_catalog(tmp_path: Path, document: dict[str, object]) -> Path:
    """Write a deliberately ordered JSON document so validation sees duplicate entries."""
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(document), encoding="utf-8")
    return catalog_path


def test_catalog_resolves_known_network_message_and_preserves_unknown_numeric_type() -> None:
    """Reject a catalog integration that drops newer numeric message IDs during decoding."""
    assert message_name_for(1001) == "MSG_CREATE_SELECTED_GROUP"
    assert message_name_for(987654) is None


@pytest.mark.parametrize(
    "entries",
    [
        [{"id": 1001, "name": "MSG_ONE"}, {"id": 1001, "name": "MSG_TWO"}],
        [{"id": 1001, "name": "MSG_ONE"}, {"id": 1002, "name": "MSG_ONE"}],
    ],
)
def test_catalog_rejects_duplicate_message_numbers_and_names(tmp_path: Path, entries: list[dict[str, object]]) -> None:
    """Reject silent duplicate loss that a JSON object-shaped numeric map would conceal."""
    with pytest.raises(MessageCatalogValidationError, match="duplicate"):
        load_message_catalog(_write_catalog(tmp_path, _catalog_document(entries)))


def test_catalog_rejects_malformed_generated_metadata(tmp_path: Path) -> None:
    """Reject catalog metadata that cannot prove its source and generated-artifact status."""
    document = _catalog_document([{"id": 1001, "name": "MSG_CREATE_SELECTED_GROUP"}])
    document["generated"] = "true"

    with pytest.raises(MessageCatalogValidationError, match="generated"):
        load_message_catalog(_write_catalog(tmp_path, document))
