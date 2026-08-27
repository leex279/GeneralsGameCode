"""Adapt strict Zero Hour replay parsing into Strata correlation evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from generals_replay_analyzer.parser import ParsedReplay, parse_replay
from generals_replay_analyzer.provenance import extract_source_provenance

from .contracts import ReplayContext, ReplayParticipant
from .normalization import normalize_query_name


def _participant(slot: Any) -> ReplayParticipant:
    query = normalize_query_name(slot.name) if slot.name is not None else None
    return ReplayParticipant(
        slot_index=slot.index,
        player_index=None,
        kind=slot.kind,
        name_raw=None if query is None else query.raw,
        name_nfc=None if query is None else query.nfc,
        name_casefold=None if query is None else query.casefold,
        player_template=slot.player_template,
        team=slot.team,
        color=slot.color,
        start_position=slot.start_position,
        ai_difficulty=slot.ai_difficulty,
    )


def _signature_slot(slot: Any) -> dict[str, object]:
    result: dict[str, object] = {"slot_index": slot.index, "kind": slot.kind}
    if slot.kind in {"human", "ai"}:
        result.update(
            {
                "name": slot.name,
                "player_template": slot.player_template,
                "team": slot.team,
                "color": slot.color,
                "start_position": slot.start_position,
                "ai_difficulty": slot.ai_difficulty,
            }
        )
    return result


def _match_signature_payload(parsed: ParsedReplay) -> dict[str, object]:
    header = parsed.header
    return {
        "game": "zero_hour",
        "version_string": header.version_string,
        "version_number": header.version_number,
        "start_time": header.start_time,
        "end_time": header.end_time,
        "frame_count": header.frame_count,
        "map_path": header.map,
        "map_crc": header.map_crc,
        "map_size": header.map_size,
        "seed": header.seed,
        "starting_cash": header.starting_cash,
        "slots": [_signature_slot(slot) for slot in sorted(header.slots, key=lambda item: item.index)],
    }


def _match_signature_sha256(parsed: ParsedReplay) -> str:
    payload = json.dumps(
        _match_signature_payload(parsed),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


# TheSuperHackers @feature Leex 27/08/2026 Derive replay-local identity context without presenting external IDs as embedded data.
def build_replay_context(path: Path) -> ReplayContext:
    """Parse one replay and return stable fingerprints plus replay-local player facts."""
    parsed = parse_replay(path)
    data = path.read_bytes()
    provenance = extract_source_provenance(path)
    command_stream_sha256 = None
    if parsed.completion_status == "complete":
        command_stream_sha256 = hashlib.sha256(data[parsed.command_stream_offset : parsed.end_offset]).hexdigest().upper()
    hinted_match_id = int(provenance.strata_match_id) if provenance.strata_match_id is not None else None
    header = parsed.header
    logic_duration = None
    if parsed.setup.max_fps > 0:
        logic_duration = header.frame_count / parsed.setup.max_fps
    return ReplayContext(
        path=path,
        replay_sha256=provenance.sha256,
        command_stream_sha256=command_stream_sha256,
        match_signature_sha256=_match_signature_sha256(parsed),
        hinted_strata_match_id=hinted_match_id,
        version_string=header.version_string,
        version_number=header.version_number,
        start_time=header.start_time,
        end_time=header.end_time,
        frame_count=header.frame_count,
        header_duration_seconds=max(0, header.end_time - header.start_time),
        logic_duration_seconds=logic_duration,
        map_path=header.map,
        map_crc=header.map_crc,
        map_size=header.map_size,
        seed=header.seed,
        starting_cash=header.starting_cash,
        local_player_index=header.local_player_index,
        slots=tuple(_participant(slot) for slot in sorted(header.slots, key=lambda item: item.index)),
        completion_status=parsed.completion_status,
    )
