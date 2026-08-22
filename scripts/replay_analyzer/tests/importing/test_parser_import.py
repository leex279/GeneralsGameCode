"""Transactional normalization tests for authoritative parser observations."""

from __future__ import annotations

import hashlib
import random
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.commands import GameMessageArgumentDataType, ReplayArgument, ReplayCommand
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    ManagedAsset,
    ParserRun,
    Player,
    PlayerAlias,
    Replay,
    ReplayPlayer,
    ReplayQualityIssue,
)
from generals_replay_analyzer.db.models import ReplayCommand as StoredReplayCommand
from generals_replay_analyzer.importing import parser_import as parser_import_module
from generals_replay_analyzer.importing.parser_import import ParserObservationImporter
from generals_replay_analyzer.importing.stages import canonical_json
from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.storage import ContentAddressedStore

PINNED_REPLAY = Path(__file__).parents[1] / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"
NOW = datetime(2026, 8, 22, 8, 30, tzinfo=UTC)


class DeterministicUUIDs:
    """Return stable UUIDs while keeping every persisted public identity unique."""

    def __init__(self, start: int = 1) -> None:
        self._value = start

    def __call__(self) -> UUID:
        value = UUID(int=self._value)
        self._value += 1
        return value


def _managed_replay(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> tuple[str, Path]:
    source = tmp_path / "match_3133811_user_ABCDEF_replay.rep"
    shutil.copyfile(PINNED_REPLAY, source)
    stored = ContentAddressedStore(settings.managed_replay_directory).store_file(source)
    with session_factory.begin() as session:
        asset = ManagedAsset(
            public_id="00000000-0000-0000-0000-00000000a001",
            sha256=stored.sha256,
            kind="replay",
            relative_path=stored.path.relative_to(settings.data_root).as_posix(),
            size_bytes=stored.size,
            media_type=None,
        )
        session.add(asset)
        session.flush()
        session.add(
            Replay(
                public_id="00000000-0000-0000-0000-00000000a002",
                sha256=stored.sha256,
                managed_asset_id=asset.id,
                map_id=None,
                replay_name=source.name,
                version_string="pending",
                version_number=0,
                frame_count=0,
                start_time=0,
                end_time=0,
                exe_crc=0,
                ini_crc=0,
                map_crc=0,
                map_name="pending",
                seed=0,
                starting_cash=None,
                header_json={"status": "pending"},
                lifecycle_state="discovered",
                updated_at=NOW,
            )
        )
    return stored.sha256, stored.path


def _importer(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    parser: object = parse_replay,
    uuid_start: int = 1,
) -> ParserObservationImporter:
    return ParserObservationImporter(
        session_factory,
        settings.data_root,
        parser=parser,  # type: ignore[arg-type]
        parser_version="fixture-parser-1",
        schema_version=1,
        clock=lambda: NOW,
        uuid_factory=DeterministicUUIDs(uuid_start),
    )


def test_complete_parser_import_preserves_commands_and_is_idempotent(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> None:
    """Catch lossy command serialization, invented identity links, or duplicate successful graphs."""
    replay_sha256, managed_path = _managed_replay(session_factory, settings, tmp_path)
    parsed = parse_replay(managed_path)
    importer = _importer(session_factory, settings)

    result = importer.import_replay(replay_sha256)
    cached = importer.import_replay(replay_sha256)

    assert result.status == "succeeded"
    assert result.command_count == len(parsed.commands) == 3993
    assert cached.run_id == result.run_id
    assert cached.cache_hit is True
    with session_factory() as session:
        runs = list(session.scalars(select(ParserRun)))
        assert len(runs) == 1
        players = list(session.scalars(select(ReplayPlayer).order_by(ReplayPlayer.slot_index)))
        assert [(row.slot_index, row.original_name, row.player_index) for row in players[:2]] == [
            (0, "leex279", None),
            (1, "FOX27", None),
        ]
        assert all(row.player_id is None and row.normalized_name is None for row in players)
        command = session.scalar(
            select(StoredReplayCommand).where(
                StoredReplayCommand.parser_run_id == runs[0].id,
                StoredReplayCommand.command_index == 4,
            )
        )
        assert command is not None
        source = parsed.commands[4]
        assert (command.command_index, command.frame, command.start_offset, command.end_offset) == (
            4,
            source.frame,
            source.start_offset,
            source.end_offset,
        )
        assert command.replay_player_id is None
        assert command.arguments_json == [
            {"argument_index": 0, "raw_bytes_hex": "01", "type_name": "BOOLEAN", "type_value": 2, "value": True},
            {
                "argument_index": 1,
                "raw_bytes_hex": "a7000000",
                "type_name": "OBJECT_ID",
                "type_value": 3,
                "value": 167,
            },
        ]
        evidence = list(session.scalars(select(EvidenceItem).order_by(EvidenceItem.source_key)))
        assert len(evidence) == 3993
        assert {item.source_kind for item in evidence} == {"parser_command"}
        assert f"parser:{result.run_id}:command:0" in {item.source_key for item in evidence}
        assert session.scalar(select(func.count(Player.id))) == 0
        assert session.scalar(select(func.count(PlayerAlias.id))) == 0
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None and replay.lifecycle_state == "parsed"
        assert replay.replay_name == parsed.header.replay_name


def test_truncated_parser_prefix_is_observed_but_invalid_attempt_is_atomic(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> None:
    """Catch failure handling that discards a bounded prefix or retains partial invalid children."""
    replay_sha256, managed_path = _managed_replay(session_factory, settings, tmp_path)
    parsed = parse_replay(managed_path)
    truncated = replace(parsed, commands=parsed.commands[:3], completion_status="truncated")
    success = _importer(session_factory, settings, parser=lambda _path: truncated).import_replay(replay_sha256)
    assert success.status == "succeeded" and success.command_count == 3

    invalid_command = replace(parsed.commands[0], end_offset=parsed.commands[0].start_offset)
    invalid = replace(parsed, commands=(invalid_command,), completion_status="complete")
    failed = _importer(session_factory, settings, parser=lambda _path: invalid, uuid_start=10_000).import_replay(
        replay_sha256, parser_version="fixture-parser-invalid"
    )
    assert failed.status == "failed" and failed.command_count == 0

    with session_factory() as session:
        successful_run = session.scalar(select(ParserRun).where(ParserRun.run_id == success.run_id))
        failed_run = session.scalar(select(ParserRun).where(ParserRun.run_id == failed.run_id))
        assert successful_run is not None and failed_run is not None
        issues = list(session.scalars(select(ReplayQualityIssue).order_by(ReplayQualityIssue.id)))
        assert [(issue.parser_run_id, issue.issue_code) for issue in issues] == [
            (successful_run.id, "parser_truncated"),
            (failed_run.id, "parser_failure"),
        ]
        assert session.scalar(
            select(func.count(ReplayPlayer.id)).where(ReplayPlayer.parser_run_id == failed_run.id)
        ) == 0
        assert session.scalar(
            select(func.count(EvidenceItem.id)).where(EvidenceItem.parser_run_id == failed_run.id)
        ) == 0


def test_failed_parser_attempt_reuses_only_the_same_final_job_key(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> None:
    """Catch a local parser-import failure duplicating history after final-job lease recovery."""
    replay_sha256, managed_path = _managed_replay(session_factory, settings, tmp_path)
    parsed = parse_replay(managed_path)
    invalid_command = replace(parsed.commands[0], end_offset=parsed.commands[0].start_offset)
    invalid = replace(parsed, commands=(invalid_command,), completion_status="complete")
    importer = _importer(session_factory, settings, parser=lambda _path: invalid, uuid_start=11_000)

    first = importer.import_replay(
        replay_sha256,
        parser_version="fixture-parser-crash-window",
        idempotency_key="import_observations:1:stable-key",
    )
    replayed = importer.import_replay(
        replay_sha256,
        parser_version="fixture-parser-crash-window",
        idempotency_key="import_observations:1:stable-key",
    )
    changed_key = importer.import_replay(
        replay_sha256,
        parser_version="fixture-parser-crash-window",
        idempotency_key="import_observations:1:changed-key",
    )

    assert first.status == replayed.status == changed_key.status == "failed"
    assert replayed.run_id == first.run_id and replayed.cache_hit is True
    assert changed_key.run_id != first.run_id and changed_key.cache_hit is False
    with session_factory() as session:
        runs = list(
            session.scalars(
                select(ParserRun)
                .where(ParserRun.parser_version == "fixture-parser-crash-window")
                .order_by(ParserRun.id)
            )
        )
        assert [run.run_id for run in runs] == [first.run_id, changed_key.run_id]
        assert [run.error_json["import_observations_idempotency_key"] for run in runs] == [
            "import_observations:1:stable-key",
            "import_observations:1:changed-key",
        ]
        assert session.scalar(
            select(func.count(EvidenceItem.id)).where(EvidenceItem.parser_run_id.in_([run.id for run in runs]))
        ) == 0


def test_seeded_hundred_command_projection_is_sorted_canonical_and_rolls_back_bad_boundary(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> None:
    """Catch presentation-order persistence or a single invalid boundary escaping transaction rollback."""
    seed = 0x4A11CE
    replay_sha256, managed_path = _managed_replay(session_factory, settings, tmp_path)
    base = parse_replay(managed_path)
    randomizer = random.Random(seed)
    values = list(range(100))
    randomizer.shuffle(values)
    commands = tuple(
        ReplayCommand(
            frame=index,
            player_index=2,
            message_type=10,
            message_name="GameMessageType_10",
            arguments=(ReplayArgument(GameMessageArgumentDataType.INTEGER, values[index], values[index].to_bytes(4, "little")),),
            start_offset=base.command_stream_offset + index * 16,
            end_offset=base.command_stream_offset + index * 16 + 16,
        )
        for index in range(100)
    )
    projected = replace(base, commands=commands, end_offset=commands[-1].end_offset)
    result = _importer(session_factory, settings, parser=lambda _path: projected).import_replay(replay_sha256)
    assert result.command_count == 100
    with session_factory() as session:
        run = session.scalar(select(ParserRun).where(ParserRun.run_id == result.run_id))
        assert run is not None
        rows = list(
            session.scalars(
                select(StoredReplayCommand)
                .where(StoredReplayCommand.parser_run_id == run.id)
                .order_by(StoredReplayCommand.command_index)
            )
        )
        assert [row.command_index for row in rows] == list(range(100))
        assert [row.arguments_json[0]["value"] for row in rows] == values

    bad = replace(projected, commands=(*commands[:50], replace(commands[50], start_offset=commands[49].start_offset)))
    failed = _importer(session_factory, settings, parser=lambda _path: bad, uuid_start=10_000).import_replay(
        replay_sha256, parser_version="fixture-parser-bad-boundary"
    )
    with session_factory() as session:
        failed_run = session.scalar(select(ParserRun).where(ParserRun.run_id == failed.run_id))
        assert failed_run is not None and failed_run.status == "failed"
        assert session.scalar(select(func.count(EvidenceItem.id)).where(EvidenceItem.parser_run_id == failed_run.id)) == 0


def test_parser_setup_projection_is_closed_and_preserves_every_exact_setup_field(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> None:
    """Catch setup facts being hashed but discarded from the accepted Replay projection."""
    replay_sha256, managed_path = _managed_replay(session_factory, settings, tmp_path)
    parsed = parse_replay(managed_path)

    result = _importer(session_factory, settings).import_replay(replay_sha256)

    with session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None
        assert replay.header_json == {
            "header": parsed.header.to_dict(),
            "setup": {
                "difficulty": parsed.setup.difficulty,
                "original_game_mode": parsed.setup.original_game_mode,
                "rank_points": parsed.setup.rank_points,
                "max_fps": parsed.setup.max_fps,
                "start_offset": parsed.setup.start_offset,
                "end_offset": parsed.setup.end_offset,
            },
        }
        assert canonical_json(replay.header_json) == canonical_json(
            {"setup": parsed.setup.to_dict(), "header": parsed.header.to_dict()}
        )
        assert session.scalar(select(ParserRun).where(ParserRun.run_id == result.run_id)) is not None


def test_parser_command_input_permutation_persists_one_canonical_source_order(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> None:
    """Catch parser tuple presentation order entering immutable command indexes or result bytes."""
    replay_sha256, managed_path = _managed_replay(session_factory, settings, tmp_path)
    base = parse_replay(managed_path)
    source = base.commands[:100]
    randomizer = random.Random(0xC011A7E)
    permuted = list(source)
    randomizer.shuffle(permuted)
    projected = replace(base, commands=tuple(permuted), end_offset=source[-1].end_offset)

    result = _importer(session_factory, settings, parser=lambda _path: projected).import_replay(replay_sha256)

    assert result.status == "succeeded" and result.command_count == 100
    with session_factory() as session:
        run = session.scalar(select(ParserRun).where(ParserRun.run_id == result.run_id))
        rows = list(
            session.scalars(
                select(StoredReplayCommand)
                .where(StoredReplayCommand.parser_run_id == run.id)
                .order_by(StoredReplayCommand.command_index)
            )
        )
        assert [row.start_offset for row in rows] == [command.start_offset for command in source]
        expected_projection = parser_import_module._result_projection(
            replace(projected, commands=source)
        )
        assert run.result_sha256 == hashlib.sha256(canonical_json(expected_projection).encode()).hexdigest()


def test_parser_reverifies_managed_replay_inside_final_transaction(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tmp_path: Path
) -> None:
    """Catch a managed replay swap after parsing but before parser child insertion."""
    replay_sha256, managed_path = _managed_replay(session_factory, settings, tmp_path)
    parsed = parse_replay(managed_path)

    def tampering_parser(_path: Path):  # type: ignore[no-untyped-def]
        managed_path.write_bytes(managed_path.read_bytes() + b"tampered")
        return parsed

    result = _importer(session_factory, settings, parser=tampering_parser).import_replay(replay_sha256)

    assert result.status == "failed" and result.command_count == 0
    with session_factory() as session:
        run = session.scalar(select(ParserRun).where(ParserRun.run_id == result.run_id))
        assert run is not None and run.status == "failed"
        assert session.scalar(select(func.count(EvidenceItem.id)).where(EvidenceItem.parser_run_id == run.id)) == 0
