from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from shutil import copyfile

from generals_replay_analyzer.parser import ParsedReplay, parse_replay
from generals_replay_analyzer.strata.replay_context import (
    _match_signature_sha256,
    build_replay_context,
)

FIXTURE = Path("tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep")
SOURCE_NAME = "match_3133811_user_e80b96708aa4254945941fd5f81489bb_replay.rep"


def test_replay_context_preserves_local_identifiers_and_names() -> None:
    context = build_replay_context(FIXTURE)

    assert context.replay_sha256 == "EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8"
    assert context.hinted_strata_match_id is None
    assert [(item.slot_index, item.player_index, item.name_raw) for item in context.human_players] == [
        (0, None, "leex279"),
        (1, None, "FOX27"),
    ]
    assert context.map_path == "userdata/maps/[rank] sand scorpion"
    assert context.header_duration_seconds == 940


def test_strict_source_filename_provides_an_untrusted_match_hint(tmp_path: Path) -> None:
    source_named = tmp_path / SOURCE_NAME
    copyfile(FIXTURE, source_named)

    assert build_replay_context(source_named).hinted_strata_match_id == 3133811


def test_command_stream_and_match_signature_fingerprints_are_stable() -> None:
    context = build_replay_context(FIXTURE)

    assert context.command_stream_sha256 is not None
    assert re.fullmatch(r"[0-9A-F]{64}", context.command_stream_sha256)
    assert re.fullmatch(r"[0-9A-F]{64}", context.match_signature_sha256)
    assert context.to_dict()["fingerprints"]["raw_replay_sha256"] == context.replay_sha256  # type: ignore[index]


def test_incomplete_command_stream_has_no_command_fingerprint(tmp_path: Path) -> None:
    truncated = tmp_path / "truncated.rep"
    truncated.write_bytes(FIXTURE.read_bytes()[:-2])

    context = build_replay_context(truncated)

    assert context.completion_status == "truncated"
    assert context.command_stream_sha256 is None


def test_match_signature_excludes_recorder_and_transport_only_header_fields() -> None:
    parsed = parse_replay(FIXTURE)
    changed_slots = tuple(
        replace(slot, ip=123, port=456, accepted=not slot.accepted, has_map=not slot.has_map)
        if slot.kind == "human"
        else slot
        for slot in parsed.header.slots
    )
    changed = ParsedReplay(
        header=replace(
            parsed.header,
            replay_name="Different local replay name",
            system_time=(2030, 1, 2, 3, 4, 5, 6, 7),
            local_player_index=1,
            slots=changed_slots,
        ),
        setup=parsed.setup,
        command_stream_offset=parsed.command_stream_offset,
        commands=parsed.commands,
        warnings=parsed.warnings,
        end_offset=parsed.end_offset,
        completion_status=parsed.completion_status,
    )

    assert _match_signature_sha256(changed) == _match_signature_sha256(parsed)


def test_replay_json_keeps_local_player_index_separate_from_external_identity() -> None:
    document = build_replay_context(FIXTURE).to_dict()
    first = document["slots"][0]  # type: ignore[index]

    assert document["local_player_index"] == 0
    assert first["slot_index"] == 0  # type: ignore[index]
    assert first["player_index"] is None  # type: ignore[index]
    assert "strata_player_id" not in first
