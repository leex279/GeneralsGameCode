"""Database-backed player identity service contract tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import (
    Base,
    create_database_engine,
    create_session_factory,
    make_alembic_config,
    upgrade_database,
)
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    FeatureSet,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    ParserRun,
    Player,
    PlayerAlias,
    PlayerIdentityOperation,
    Replay,
    ReplayPlayer,
    Source,
)
from generals_replay_analyzer.identity import (
    EMBEDDED_REPLAY_NAME_NAMESPACE,
    IdentityBusyError,
    IdentityConflictError,
    IdentityInvariantError,
    PlayerIdentityService,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


class DeterministicIds:
    """Yield stable UUIDs without exposing insertion order in service DTOs."""

    def __init__(self, start: int = 10_000) -> None:
        self.value = start

    def __call__(self) -> str:
        self.value += 1
        return str(UUID(int=self.value))


def _public_id(number: int) -> str:
    return str(UUID(int=number))


def _seed_replay(
    session: Session,
    *,
    public_number: int = 1,
    sha_character: str = "a",
    name: str = "leex279_vs_fox27.rep",
) -> Replay:
    replay = Replay(
        public_id=_public_id(public_number),
        sha256=sha_character * 64,
        replay_name=name,
        version_string="1.04",
        version_number=104,
        frame_count=100,
        start_time=0,
        end_time=100,
        exe_crc=1,
        ini_crc=2,
        map_crc=3,
        map_name="Tournament Desert",
        seed=4,
        header_json={"fixture": True},
        lifecycle_state="parsed",
        updated_at=NOW,
        created_at=NOW,
    )
    session.add(replay)
    session.flush()
    return replay


def _seed_run_with_slots(
    session: Session,
    replay: Replay,
    slots: tuple[tuple[int, str, str | None, int | None], ...],
    *,
    run_number: int = 100,
    status: str = "succeeded",
) -> ParserRun:
    run = ParserRun(
        run_id=_public_id(run_number),
        replay_id=replay.id,
        parser_version=f"fixture-{run_number}",
        schema_version=1,
        input_sha256=f"{run_number % 10}" * 64,
        result_sha256="b" * 64 if status == "succeeded" else None,
        status="running" if status == "succeeded" else status,
        completion_status="complete" if status == "succeeded" else "failed",
        command_stream_offset=10,
        end_offset=20,
        warnings_json=[],
        error_json=None if status == "succeeded" else {"code": "fixture"},
        started_at=NOW,
        completed_at=NOW,
    )
    session.add(run)
    session.flush()
    for slot_index, slot_kind, original_name, public_number in slots:
        session.add(
            ReplayPlayer(
                public_id=_public_id(public_number or (1_000 + slot_index)),
                replay_id=replay.id,
                parser_run_id=run.id,
                slot_index=slot_index,
                slot_kind=slot_kind,
                original_name=original_name,
                normalized_name=None,
                observed_json={"slot_index": slot_index, "original_name": original_name},
            )
        )
    session.flush()
    if status == "succeeded":
        run.status = "succeeded"
        session.flush()
    return run


def _service(factory: sessionmaker[Session], start: int = 10_000) -> PlayerIdentityService:
    return PlayerIdentityService(factory, public_id_factory=DeterministicIds(start), now_factory=lambda: NOW)


def _revision_map(session: Session) -> dict[str, int]:
    return dict(session.execute(select(Player.public_id, Player.identity_revision)).all())


def _assert_no_internal_ids(value: object) -> None:
    if isinstance(value, dict):
        assert "id" not in value
        for nested in value.values():
            _assert_no_internal_ids(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_internal_ids(nested)


def test_migration_head_adds_the_immutable_identity_ledger(identity_database_path: Path) -> None:
    """Catch a missing or mutable packaged Task 5 audit migration."""
    from alembic import command

    config = make_alembic_config(identity_database_path)
    command.upgrade(config, "0001_replay_analyzer_v2")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{identity_database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert "player_identity_operations" in inspector.get_table_names()
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0005_llm_graph_immutability"
            )
            objects = {
                (row.type, row.name)
                for row in connection.execute(
                    text(
                        "SELECT type, name FROM sqlite_master "
                        "WHERE tbl_name = 'player_identity_operations' AND name NOT LIKE 'sqlite_%'"
                    )
                )
            }
        assert ("index", "ix_player_identity_operations_created_kind") in objects
        assert ("index", "ix_player_identity_operations_inverse_of") in objects
        assert ("trigger", "trg_player_identity_operations_no_update") in objects
        assert ("trigger", "trg_player_identity_operations_no_delete") in objects
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()
    command.downgrade(config, "0001_replay_analyzer_v2")
    with sqlite3.connect(identity_database_path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'player_identity_operations'"
        ).fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0001_replay_analyzer_v2",
        )
    command.upgrade(config, "0002_player_identity_audit")
    with sqlite3.connect(identity_database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0002_player_identity_audit",
        )


def test_exact_resolution_preserves_observations_and_ignores_strata_provenance(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch filename/provenance evidence or parser-field rewrites entering automatic identity."""
    with identity_session_factory.begin() as session:
        replay = _seed_replay(session)
        _seed_run_with_slots(
            session,
            replay,
            (
                (3, "open", None, 1_003),
                (1, "human", "FOX27", 1_001),
                (2, "computer", "Brutal AI", 1_002),
                (0, "human", "leex279", 1_000),
            ),
        )
        session.add(
            Source(
                public_id=_public_id(800),
                replay_id=replay.id,
                source_kind="strata",
                original_locator="C:/private/3133811/leex279_vs_fox27.rep",
                original_filename="3133811_leex279_vs_fox27.rep",
                strata_match_id="3133811",
                strata_source_user_token="e80b96708aa4254945941fd5f81489bb",
                discovered_at=NOW,
                provenance_json={},
            )
        )

    batch = _service(identity_session_factory).resolve_parser_run(
        replay_public_id=_public_id(1), parser_run_id=_public_id(100)
    )
    assert [(decision.replay_player_public_id, decision.outcome) for decision in batch.decisions] == [
        (_public_id(1_000), "created"),
        (_public_id(1_001), "created"),
        (_public_id(1_002), "ineligible"),
        (_public_id(1_003), "ineligible"),
    ]
    assert batch.affected_player_revisions == tuple(sorted(batch.affected_player_revisions))
    with identity_session_factory() as session:
        aliases = session.scalars(select(PlayerAlias).order_by(PlayerAlias.normalized_name)).all()
        assert [(alias.namespace, alias.normalized_name, alias.original_name) for alias in aliases] == [
            (EMBEDDED_REPLAY_NAME_NAMESPACE, "fox27", "FOX27"),
            (EMBEDDED_REPLAY_NAME_NAMESPACE, "leex279", "leex279"),
        ]
        forbidden = {"3133811", "e80b96708aa4254945941fd5f81489bb"}
        assert forbidden.isdisjoint({alias.normalized_name for alias in aliases})
        assert forbidden.isdisjoint(set(session.scalars(select(Player.display_name))))
        rows = session.scalars(select(ReplayPlayer).order_by(ReplayPlayer.slot_index)).all()
        assert [(row.original_name, row.normalized_name) for row in rows] == [
            ("leex279", None),
            ("FOX27", None),
            ("Brutal AI", None),
            (None, None),
        ]
        assert session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) == 2
        assert set(_revision_map(session).values()) == {1}

    second = _service(identity_session_factory, 20_000).resolve_parser_run(
        replay_public_id=_public_id(1), parser_run_id=_public_id(100)
    )
    assert [decision.outcome for decision in second.decisions] == [
        "already_linked",
        "already_linked",
        "ineligible",
        "ineligible",
    ]
    with identity_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) == 2
        assert set(_revision_map(session).values()) == {1}


def test_case_and_nfc_variation_reuses_only_the_exact_embedded_alias(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch duplicate canonical players or fuzzy/cross-namespace fallback."""
    with identity_session_factory.begin() as session:
        replay = _seed_replay(session)
        run = _seed_run_with_slots(session, replay, ((0, "human", "Élite", 1_100),))
    service = _service(identity_session_factory)
    first = service.resolve_parser_run(replay_public_id=replay.public_id, parser_run_id=run.run_id)
    target = first.decisions[0].player_public_id
    assert target is not None
    with identity_session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == _public_id(1)))
        assert replay is not None
        second_run = _seed_run_with_slots(session, replay, ((0, "human", " E\u0301LITE ", 1_101),), run_number=101)
        unrelated = Player(public_id=_public_id(300), display_name="provenance", identity_revision=0, updated_at=NOW)
        session.add(unrelated)
        session.flush()
        session.add(
            PlayerAlias(
                public_id=_public_id(301),
                player_id=unrelated.id,
                namespace="strata_filename",
                normalized_name="élite",
                original_name="Élite",
                external_subject=None,
                created_at=NOW,
            )
        )
    second = service.resolve_parser_run(replay_public_id=_public_id(1), parser_run_id=second_run.run_id)
    assert second.decisions[0].outcome == "linked"
    assert second.decisions[0].player_public_id == target
    with identity_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlayerAlias)) == 2
        target_row = session.scalar(select(Player).where(Player.public_id == target))
        assert target_row is not None and target_row.identity_revision == 2


def test_conflicting_link_failed_run_and_run_mismatch_write_nothing(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch unsafe relinking or resolution of an ineligible parser observation."""
    with identity_session_factory.begin() as session:
        replay = _seed_replay(session)
        linked = Player(public_id=_public_id(400), display_name="other", identity_revision=7, updated_at=NOW)
        session.add(linked)
        session.flush()
        run = _seed_run_with_slots(session, replay, ((0, "human", "leex279", 1_200),))
        slot = session.scalar(select(ReplayPlayer).where(ReplayPlayer.parser_run_id == run.id))
        assert slot is not None
        slot.player_id = linked.id
        failed = _seed_run_with_slots(
            session, replay, ((0, "human", "FOX27", 1_201),), run_number=101, status="failed"
        )
        retired = Player(
            public_id=_public_id(401),
            display_name="FOX27",
            identity_revision=9,
            updated_at=NOW,
            retired_at=NOW,
            created_at=NOW,
        )
        session.add(retired)
        session.flush()
        session.add(
            PlayerAlias(
                public_id=_public_id(402),
                player_id=retired.id,
                namespace=EMBEDDED_REPLAY_NAME_NAMESPACE,
                normalized_name="fox27",
                original_name="FOX27",
                external_subject=None,
                created_at=NOW,
            )
        )
        retired_run = _seed_run_with_slots(
            session, replay, ((0, "human", "FOX27", 1_202),), run_number=102
        )
        other_replay = _seed_replay(session, public_number=2, sha_character="c", name="other.rep")
    service = _service(identity_session_factory)
    conflict = service.resolve_parser_run(replay_public_id=replay.public_id, parser_run_id=run.run_id)
    assert conflict.decisions[0].outcome == "manual_review"
    failed_batch = service.resolve_parser_run(replay_public_id=replay.public_id, parser_run_id=failed.run_id)
    assert failed_batch.decisions[0].outcome == "ineligible"
    retired_batch = service.resolve_parser_run(replay_public_id=replay.public_id, parser_run_id=retired_run.run_id)
    assert retired_batch.decisions[0].outcome == "manual_review"
    mismatch = service.resolve_parser_run(replay_public_id=other_replay.public_id, parser_run_id=run.run_id)
    assert mismatch.decisions[0].outcome == "ineligible"
    with identity_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) == 0
        assert _revision_map(session) == {_public_id(400): 7, _public_id(401): 9}


def test_legacy_duplicate_exact_aliases_return_manual_review_without_mutation(
    identity_database_path: Path,
    identity_engine: Engine,
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch legacy duplicate aliases escaping the deterministic manual-review corruption boundary."""
    with identity_session_factory.begin() as session:
        replay = _seed_replay(session)
        run = _seed_run_with_slots(session, replay, ((0, "human", "leex279", 1_250),))
        first = Player(public_id=_public_id(410), display_name="First", identity_revision=3, updated_at=NOW)
        second = Player(public_id=_public_id(411), display_name="Second", identity_revision=5, updated_at=NOW)
        session.add_all([first, second])
        session.flush()
        session.add(
            PlayerAlias(
                public_id=_public_id(412),
                player_id=first.id,
                namespace=EMBEDDED_REPLAY_NAME_NAMESPACE,
                normalized_name="leex279",
                original_name="leex279",
                external_subject=None,
                created_at=NOW,
            )
        )
    identity_engine.dispose()
    with sqlite3.connect(identity_database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE player_aliases_legacy AS SELECT * FROM player_aliases")
        connection.execute("DROP TABLE player_aliases")
        connection.execute("ALTER TABLE player_aliases_legacy RENAME TO player_aliases")
        connection.execute(
            "CREATE INDEX ix_player_aliases_player_namespace ON player_aliases (player_id, namespace)"
        )
        second_id = connection.execute(
            "SELECT id FROM players WHERE public_id = ?", (_public_id(411),)
        ).fetchone()
        assert second_id is not None
        connection.execute(
            "INSERT INTO player_aliases "
            "(player_id, namespace, normalized_name, original_name, external_subject, id, public_id, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                second_id[0],
                EMBEDDED_REPLAY_NAME_NAMESPACE,
                "leex279",
                "LEEX279",
                2,
                _public_id(413),
                NOW.isoformat(),
            ),
        )
        connection.commit()

    with identity_session_factory() as session:
        before = (
            _revision_map(session),
            session.scalar(select(func.count()).select_from(PlayerAlias)),
            session.scalar(select(ReplayPlayer.player_id).where(ReplayPlayer.public_id == _public_id(1_250))),
            session.scalar(select(func.count()).select_from(PlayerIdentityOperation)),
        )
    batch = _service(identity_session_factory).resolve_parser_run(
        replay_public_id=replay.public_id, parser_run_id=run.run_id
    )
    assert len(batch.decisions) == 1
    assert batch.decisions[0].outcome == "manual_review"
    assert batch.decisions[0].reason_code == "ambiguous_exact_embedded_alias"
    assert batch.decisions[0].player_public_id is None
    assert batch.decisions[0].alias_public_id is None
    assert batch.affected_player_revisions == ()
    with identity_session_factory() as session:
        after = (
            _revision_map(session),
            session.scalar(select(func.count()).select_from(PlayerAlias)),
            session.scalar(select(ReplayPlayer.player_id).where(ReplayPlayer.public_id == _public_id(1_250))),
            session.scalar(select(func.count()).select_from(PlayerIdentityOperation)),
        )
    assert after == before


@settings(
    max_examples=100,
    deadline=None,
    database=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(order_keys=st.lists(st.integers(), min_size=4, max_size=4, unique=True))
def test_resolution_is_deterministic_for_randomized_slot_insertion_orders(
    tmp_path: Path, order_keys: list[int]
) -> None:
    """Catch insertion-order-dependent decisions, audits, aliases, revisions, or tokens."""
    case_key = hashlib.sha256(repr(order_keys).encode()).hexdigest()
    database_path = tmp_path / f"identity-{case_key}.sqlite3"
    database_path.unlink(missing_ok=True)
    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    factory = create_session_factory(engine)
    logical_slots = (
        (0, "human", "leex279", 1_500),
        (1, "human", "FOX27", 1_501),
        (2, "computer", "Brutal AI", 1_502),
        (3, "open", None, 1_503),
    )
    insertion_order = tuple(slot for _, slot in sorted(zip(order_keys, logical_slots, strict=True)))
    try:
        with factory.begin() as session:
            replay = _seed_replay(session)
            run = _seed_run_with_slots(session, replay, insertion_order)
        service = _service(factory)
        batch = service.resolve_parser_run(replay_public_id=replay.public_id, parser_run_id=run.run_id)
        assert [decision.replay_player_public_id for decision in batch.decisions] == [
            _public_id(1_500),
            _public_id(1_501),
            _public_id(1_502),
            _public_id(1_503),
        ]
        assert [decision.outcome for decision in batch.decisions] == ["created", "created", "ineligible", "ineligible"]
        assert batch.affected_player_revisions == ((_public_id(10_001), 1), (_public_id(10_004), 1))
        assert all(token == token.lower() and len(token) == 64 for public_id in dict(batch.affected_player_revisions) for token in [service.identity_cache_token(player_public_id=public_id)])
        with factory() as session:
            assert [
                (alias.public_id, alias.normalized_name, alias.player_id)
                for alias in session.scalars(select(PlayerAlias).order_by(PlayerAlias.public_id))
            ] == [
                (_public_id(10_002), "leex279", session.scalar(select(Player.id).where(Player.public_id == _public_id(10_001)))),
                (_public_id(10_005), "fox27", session.scalar(select(Player.id).where(Player.public_id == _public_id(10_004)))),
            ]
            audit_rows = session.execute(
                text(
                    "SELECT before_json, after_json, inverse_payload_json, affected_revisions_json "
                    "FROM player_identity_operations ORDER BY public_id"
                )
            ).all()
        assert len(audit_rows) == 2
        for row in audit_rows:
            for payload in row:
                decoded = json.loads(payload)
                assert payload == json.dumps(decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                for collection in ("players", "aliases", "replay_players"):
                    if collection in decoded:
                        public_key = {
                            "players": "player_public_id",
                            "aliases": "alias_public_id",
                            "replay_players": "replay_player_public_id",
                        }[collection]
                        assert decoded[collection] == sorted(decoded[collection], key=lambda item: item[public_key])
    finally:
        engine.dispose()


def _seed_manual_graph(factory: sessionmaker[Session]) -> tuple[str, str, str, str, str]:
    with factory.begin() as session:
        replay = _seed_replay(session)
        run = _seed_run_with_slots(
            session,
            replay,
            ((0, "human", "Target", 1_300), (1, "human", "Source", 1_301), (2, "human", "Source", 1_302)),
        )
        target = Player(public_id=_public_id(500), display_name="Target", identity_revision=2, updated_at=NOW)
        source = Player(public_id=_public_id(501), display_name="Source", identity_revision=4, updated_at=NOW)
        session.add_all([target, source])
        session.flush()
        target_alias = PlayerAlias(
            public_id=_public_id(510),
            player_id=target.id,
            namespace=EMBEDDED_REPLAY_NAME_NAMESPACE,
            normalized_name="target",
            original_name="Target",
            external_subject=None,
            created_at=NOW,
        )
        source_alias = PlayerAlias(
            public_id=_public_id(511),
            player_id=source.id,
            namespace=EMBEDDED_REPLAY_NAME_NAMESPACE,
            normalized_name="source",
            original_name="Source",
            external_subject=None,
            created_at=NOW,
        )
        session.add_all([target_alias, source_alias])
        session.flush()
        slots = session.scalars(select(ReplayPlayer).where(ReplayPlayer.parser_run_id == run.id)).all()
        slots[0].player_id = target.id
        slots[1].player_id = source.id
        slots[2].player_id = source.id
    return target.public_id, source.public_id, source_alias.public_id, slots[1].public_id, slots[2].public_id


def test_merge_and_inverse_are_revision_guarded_append_only_operations(
    identity_session_factory: sessionmaker[Session], identity_engine: Engine
) -> None:
    """Catch partial merge, audit mutation, or recomputed rather than captured inverse behavior."""
    target, source, _, source_slot, other_source_slot = _seed_manual_graph(identity_session_factory)
    service = _service(identity_session_factory)
    old_target_token = service.identity_cache_token(player_public_id=target)
    receipt = service.merge_players(
        target_player_public_id=target,
        source_player_public_ids=(source,),
        expected_revisions={target: 2, source: 4},
        actor="operator:leex",
        reason="same person confirmed",
    )
    assert receipt.affected_player_revisions == ((target, 3), (source, 5))
    assert service.identity_cache_token(player_public_id=target) != old_target_token
    with identity_session_factory() as session:
        source_row = session.scalar(select(Player).where(Player.public_id == source))
        assert source_row is not None and source_row.retired_at == NOW.replace(tzinfo=None)
        links = dict(session.execute(select(ReplayPlayer.public_id, ReplayPlayer.player_id)).all())
        target_id = session.scalar(select(Player.id).where(Player.public_id == target))
        assert links[source_slot] == target_id and links[other_source_slot] == target_id
        operation = session.scalar(select(PlayerIdentityOperation))
        assert operation is not None
        assert operation.before_json["operation_kind"] == "merge_players"
        _assert_no_internal_ids(operation.before_json)
        original_raw = session.execute(
            text(
                "SELECT before_json, after_json, inverse_payload_json, affected_revisions_json "
                "FROM player_identity_operations WHERE public_id = :public_id"
            ),
            {"public_id": receipt.operation_public_id},
        ).one()

    inverse = service.inverse_operation(
        operation_public_id=receipt.operation_public_id,
        expected_revisions={target: 3, source: 5},
        actor="operator:leex",
        reason="merge correction",
    )
    assert inverse.inverse_of_operation_public_id == receipt.operation_public_id
    assert inverse.affected_player_revisions == ((target, 4), (source, 6))
    with identity_session_factory() as session:
        source_row = session.scalar(select(Player).where(Player.public_id == source))
        assert source_row is not None and source_row.retired_at is None
        source_id = source_row.id
        links = dict(session.execute(select(ReplayPlayer.public_id, ReplayPlayer.player_id)).all())
        assert links[source_slot] == source_id and links[other_source_slot] == source_id
        current_raw = session.execute(
            text(
                "SELECT before_json, after_json, inverse_payload_json, affected_revisions_json "
                "FROM player_identity_operations WHERE public_id = :public_id"
            ),
            {"public_id": receipt.operation_public_id},
        ).one()
        assert current_raw == original_raw
        assert session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) == 2
    with pytest.raises(IdentityConflictError):
        service.inverse_operation(
            operation_public_id=receipt.operation_public_id,
            expected_revisions={target: 4, source: 6},
            actor="operator:leex",
            reason="cannot repeat",
        )
    with identity_engine.connect() as connection:
        with pytest.raises(DatabaseError):
            connection.exec_driver_sql(
                "UPDATE player_identity_operations SET reason = 'tamper' WHERE public_id = ?",
                (receipt.operation_public_id,),
            )
        with pytest.raises(DatabaseError):
            connection.exec_driver_sql(
                "DELETE FROM player_identity_operations WHERE public_id = ?", (receipt.operation_public_id,)
            )


def test_split_moves_only_explicit_members_and_external_alias_never_auto_links(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch heuristic split membership or provider aliases entering automatic resolution."""
    target, source, source_alias, selected_slot, unselected_slot = _seed_manual_graph(identity_session_factory)
    service = _service(identity_session_factory)
    source_token_before_split = service.identity_cache_token(player_public_id=source)
    split = service.split_alias(
        alias_public_id=source_alias,
        replay_player_public_ids=(selected_slot,),
        new_display_name="Corrected Source",
        expected_revisions={source: 4},
        actor="operator:leex",
        reason="explicit replay membership",
    )
    assert split.operation_kind == "split_alias"
    new_player = next(public_id for public_id, _ in split.affected_player_revisions if public_id != source)
    assert split.affected_player_revisions == ((source, 5), (new_player, 1))
    split_tokens = dict(split.cache_tokens)
    assert split_tokens[source] == service.identity_cache_token(player_public_id=source)
    assert split_tokens[new_player] == service.identity_cache_token(player_public_id=new_player)
    assert split_tokens[source] != source_token_before_split
    new_token_after_split = split_tokens[new_player]
    with identity_session_factory() as session:
        links = dict(
            session.execute(
                select(ReplayPlayer.public_id, Player.public_id).join(Player, ReplayPlayer.player_id == Player.id)
            ).all()
        )
        assert links[selected_slot] == new_player
        assert links[unselected_slot] == source
        alias_target = session.scalar(
            select(Player.public_id).join(PlayerAlias, PlayerAlias.player_id == Player.id).where(PlayerAlias.public_id == source_alias)
        )
        assert alias_target == new_player
        split_operation = session.scalar(
            select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == split.operation_public_id)
        )
        assert split_operation is not None
        split_before_players = {
            entry["player_public_id"]: entry for entry in split_operation.before_json["players"]
        }
        assert split_before_players[new_player] == {
            "player_public_id": new_player,
            "presence": "absent",
        }
        assert split_before_players[source]["presence"] == "active"
        assert split_before_players[source]["identity_revision"] == 4
        split_before_alias = split_operation.before_json["aliases"]
        assert split_before_alias == [
            {
                "alias_public_id": source_alias,
                "player_public_id": source,
                "namespace": EMBEDDED_REPLAY_NAME_NAMESPACE,
                "normalized_name": "source",
                "external_subject": None,
                "presence": "active",
            }
        ]
        split_after_players = {
            entry["player_public_id"]: entry for entry in split_operation.after_json["players"]
        }
        assert split_after_players[new_player]["presence"] == "active"
        assert split_after_players[new_player]["identity_revision"] == 1
        assert split_operation.inverse_payload_json == {"restore": split_operation.before_json}
        split_original_raw = session.execute(
            text(
                "SELECT before_json, after_json, inverse_payload_json, affected_revisions_json "
                "FROM player_identity_operations WHERE public_id = :public_id"
            ),
            {"public_id": split.operation_public_id},
        ).one()
        source_row = session.scalar(select(Source))
        if source_row is None:
            replay_id = session.scalar(select(Replay.id).where(Replay.public_id == _public_id(1)))
            source_row = Source(
                public_id=_public_id(801),
                replay_id=replay_id,
                source_kind="strata",
                original_locator="private",
                original_filename="source.rep",
                strata_match_id="3133811",
                strata_source_user_token="e80b96708aa4254945941fd5f81489bb",
                discovered_at=NOW,
                provenance_json={},
            )
            session.add(source_row)
            session.commit()
    inverse = service.inverse_operation(
        operation_public_id=split.operation_public_id,
        expected_revisions=dict(split.affected_player_revisions),
        actor="operator:leex",
        reason="split selection corrected",
    )
    assert inverse.inverse_of_operation_public_id == split.operation_public_id
    assert inverse.affected_player_revisions == ((source, 6), (new_player, 2))
    inverse_tokens = dict(inverse.cache_tokens)
    assert inverse_tokens[source] == service.identity_cache_token(player_public_id=source)
    assert inverse_tokens[new_player] == service.identity_cache_token(player_public_id=new_player)
    assert inverse_tokens[source] != split_tokens[source]
    assert inverse_tokens[new_player] != new_token_after_split
    with identity_session_factory() as session:
        links = dict(
            session.execute(
                select(ReplayPlayer.public_id, Player.public_id).join(Player, ReplayPlayer.player_id == Player.id)
            ).all()
        )
        assert links[selected_slot] == source
        assert links[unselected_slot] == source
        alias_target = session.scalar(
            select(Player.public_id).join(PlayerAlias, PlayerAlias.player_id == Player.id).where(PlayerAlias.public_id == source_alias)
        )
        assert alias_target == source
        new_player_row = session.scalar(select(Player).where(Player.public_id == new_player))
        assert new_player_row is not None and new_player_row.retired_at is not None
        assert session.scalar(select(func.count()).select_from(PlayerAlias).where(PlayerAlias.player_id == new_player_row.id)) == 0
        assert session.scalar(
            select(func.count()).select_from(ReplayPlayer).where(ReplayPlayer.player_id == new_player_row.id)
        ) == 0
        inverse_operation = session.scalar(
            select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == inverse.operation_public_id)
        )
        assert inverse_operation is not None
        assert inverse_operation.before_json == split_operation.after_json
        assert inverse_operation.inverse_payload_json == {"restore": inverse_operation.before_json}
        inverse_after_players = {
            entry["player_public_id"]: entry for entry in inverse_operation.after_json["players"]
        }
        assert inverse_after_players[new_player]["presence"] == "retired_tombstone"
        assert inverse_after_players[new_player]["identity_revision"] == 2
        assert session.execute(
            text(
                "SELECT before_json, after_json, inverse_payload_json, affected_revisions_json "
                "FROM player_identity_operations WHERE public_id = :public_id"
            ),
            {"public_id": split.operation_public_id},
        ).one() == split_original_raw
    target_token_before_attach = service.identity_cache_token(player_public_id=target)
    attachment = service.attach_external_alias(
        player_public_id=target,
        provider="strata",
        external_subject="e80b96708aa4254945941fd5f81489bb",
        source_public_id=_public_id(801),
        actor="operator:leex",
        reason="verified provider account",
        expected_revisions={target: 2},
    )
    assert attachment.operation_kind == "attach_external_alias"
    assert attachment.affected_player_revisions == ((target, 3),)
    attach_token = dict(attachment.cache_tokens)[target]
    assert attach_token == service.identity_cache_token(player_public_id=target)
    assert attach_token != target_token_before_attach
    with identity_session_factory() as session:
        external = session.scalar(select(PlayerAlias).where(PlayerAlias.namespace == "external:strata"))
        assert external is not None and external.external_subject == "e80b96708aa4254945941fd5f81489bb"
        assert session.scalar(select(func.count()).select_from(Player)) == 3
        attach_operation = session.scalar(
            select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == attachment.operation_public_id)
        )
        assert attach_operation is not None
        assert attach_operation.before_json["aliases"] == [
            {
                "alias_public_id": external.public_id,
                "presence": "absent",
            }
        ]
        assert attach_operation.after_json["aliases"] == [
            {
                "alias_public_id": external.public_id,
                "player_public_id": target,
                "namespace": "external:strata",
                "normalized_name": "e80b96708aa4254945941fd5f81489bb",
                "external_subject": "e80b96708aa4254945941fd5f81489bb",
                "presence": "active",
            }
        ]
        assert attach_operation.inverse_payload_json == {"restore": attach_operation.before_json}
        attach_original_raw = session.execute(
            text(
                "SELECT before_json, after_json, inverse_payload_json, affected_revisions_json "
                "FROM player_identity_operations WHERE public_id = :public_id"
            ),
            {"public_id": attachment.operation_public_id},
        ).one()
    detached = service.inverse_operation(
        operation_public_id=attachment.operation_public_id,
        expected_revisions=dict(attachment.affected_player_revisions),
        actor="operator:leex",
        reason="provider attachment corrected",
    )
    assert detached.inverse_of_operation_public_id == attachment.operation_public_id
    assert detached.affected_player_revisions == ((target, 4),)
    detached_token = dict(detached.cache_tokens)[target]
    assert detached_token == service.identity_cache_token(player_public_id=target)
    assert detached_token != attach_token
    with identity_session_factory() as session:
        external_target = session.scalar(
            select(Player.public_id).join(PlayerAlias, PlayerAlias.player_id == Player.id).where(
                PlayerAlias.namespace == "external:strata"
            )
        )
        assert external_target is None
        detached_alias = session.scalar(
            select(PlayerAlias).where(PlayerAlias.public_id == external.public_id)
        )
        assert detached_alias is not None
        assert detached_alias.namespace == f"external:detached:{external.public_id}"
        assert session.scalar(
            select(func.count())
            .select_from(PlayerAlias)
            .where(
                PlayerAlias.namespace == EMBEDDED_REPLAY_NAME_NAMESPACE,
                PlayerAlias.normalized_name == detached_alias.normalized_name,
            )
        ) == 0
        detached_operation = session.scalar(
            select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == detached.operation_public_id)
        )
        assert detached_operation is not None
        assert detached_operation.before_json == attach_operation.after_json
        assert detached_operation.inverse_payload_json == {"restore": detached_operation.before_json}
        assert detached_operation.after_json["aliases"] == [
            {
                "alias_public_id": external.public_id,
                "player_public_id": target,
                "namespace": f"external:detached:{external.public_id}",
                "normalized_name": "e80b96708aa4254945941fd5f81489bb",
                "external_subject": "e80b96708aa4254945941fd5f81489bb",
                "presence": "detached_tombstone",
            }
        ]
        assert session.execute(
            text(
                "SELECT before_json, after_json, inverse_payload_json, affected_revisions_json "
                "FROM player_identity_operations WHERE public_id = :public_id"
            ),
            {"public_id": attachment.operation_public_id},
        ).one() == attach_original_raw


def test_identity_changes_preserve_succeeded_longitudinal_history_byte_for_byte(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch destructive invalidation or historical canonical membership relabeling."""
    target, source, _, _, _ = _seed_manual_graph(identity_session_factory)
    with identity_session_factory.begin() as session:
        target_row = session.scalar(select(Player).where(Player.public_id == target))
        replay = session.scalar(select(Replay))
        replay_player = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.player_id == target_row.id if target_row is not None else -1)
        )
        assert target_row is not None and replay is not None and replay_player is not None
        feature_evidence = EvidenceItem(
            public_id=_public_id(900),
            replay_id=replay.id,
            parser_run_id=None,
            telemetry_run_id=None,
            tier="derived",
            source_kind="feature_fixture",
            source_key="feature:fixture",
            schema_version=1,
            created_at=NOW,
        )
        result_evidence = EvidenceItem(
            public_id=_public_id(901),
            replay_id=replay.id,
            parser_run_id=None,
            telemetry_run_id=None,
            tier="derived",
            source_kind="longitudinal_fixture",
            source_key="longitudinal:fixture",
            schema_version=1,
            created_at=NOW,
        )
        session.add_all([feature_evidence, result_evidence])
        session.flush()
        feature_set = FeatureSet(
            public_id=_public_id(902),
            replay_id=replay.id,
            replay_player_id=replay_player.id,
            extractor_name="fixture",
            extractor_version="1",
            input_digest="d" * 64,
            cache_key="e" * 64,
            status="succeeded",
            settings_json={},
            completed_at=NOW,
            error_json=None,
            created_at=NOW,
        )
        run = LongitudinalRun(
            run_id=_public_id(903),
            player_id=target_row.id,
            identity_revision=target_row.identity_revision,
            analyzer_name="fixture",
            analyzer_version="1",
            segment_key_json={"segment": "all"},
            settings_json={},
            input_digest="f" * 64,
            cache_key="0" * 64,
            status="succeeded",
            created_at=NOW,
            completed_at=NOW,
            error_json=None,
        )
        session.add_all([feature_set, run])
        session.flush()
        result = LongitudinalResult(
            public_id=_public_id(904),
            longitudinal_run_id=run.id,
            evidence_item_id=result_evidence.id,
            result_name="fixture_result",
            result_kind="summary",
            sample_count=1,
            missing_count=0,
            quality="available",
            quality_reason=None,
            statistics_json={"value": 1},
        )
        session.add(result)
        session.flush()
        session.add(
            LongitudinalMember(
                longitudinal_result_id=result.id,
                replay_id=replay.id,
                replay_player_id=replay_player.id,
                feature_set_id=feature_set.id,
                feature_id=None,
                strategy_assessment_id=None,
                evidence_item_id=feature_evidence.id,
            )
        )
    with identity_session_factory() as session:
        before = tuple(
            tuple(session.execute(text(f"SELECT * FROM {table_name} ORDER BY id")).all())
            for table_name in ("longitudinal_runs", "longitudinal_results", "longitudinal_members")
        )
    _service(identity_session_factory).merge_players(
        target_player_public_id=target,
        source_player_public_ids=(source,),
        expected_revisions={target: 2, source: 4},
        actor="operator:leex",
        reason="history preservation",
    )
    with identity_session_factory() as session:
        after = tuple(
            tuple(session.execute(text(f"SELECT * FROM {table_name} ORDER BY id")).all())
            for table_name in ("longitudinal_runs", "longitudinal_results", "longitudinal_members")
        )
    assert after == before


def test_audit_constraint_failure_rolls_back_tentative_merge_reassignments(
    identity_session_factory: sessionmaker[Session], identity_engine: Engine
) -> None:
    """Catch tentative alias/link/revision changes escaping a failed final audit insert."""
    target, source, _, _, _ = _seed_manual_graph(identity_session_factory)
    with identity_session_factory() as session:
        before = (
            _revision_map(session),
            tuple(session.execute(select(PlayerAlias.public_id, PlayerAlias.player_id).order_by(PlayerAlias.public_id))),
            tuple(session.execute(select(ReplayPlayer.public_id, ReplayPlayer.player_id).order_by(ReplayPlayer.public_id))),
        )
    with identity_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER force_identity_audit_failure BEFORE INSERT ON player_identity_operations "
            "BEGIN SELECT RAISE(ABORT, 'forced identity audit failure'); END"
        )
    with pytest.raises(IdentityInvariantError):
        _service(identity_session_factory).merge_players(
            target_player_public_id=target,
            source_player_public_ids=(source,),
            expected_revisions={target: 2, source: 4},
            actor="operator:leex",
            reason="forced rollback",
        )
    with identity_session_factory() as session:
        after = (
            _revision_map(session),
            tuple(session.execute(select(PlayerAlias.public_id, PlayerAlias.player_id).order_by(PlayerAlias.public_id))),
            tuple(session.execute(select(ReplayPlayer.public_id, ReplayPlayer.player_id).order_by(ReplayPlayer.public_id))),
        )
        assert session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) == 0
    assert after == before


@pytest.mark.parametrize(
    ("sources", "expected", "actor", "reason"),
    [
        ((), {_public_id(500): 2}, "operator", "reason"),
        ((_public_id(500),), {_public_id(500): 2}, "operator", "reason"),
        ((_public_id(501), _public_id(501)), {_public_id(500): 2, _public_id(501): 4}, "operator", "reason"),
        ((_public_id(501),), {_public_id(500): 2}, "operator", "reason"),
        ((_public_id(501),), {_public_id(500): 2, _public_id(501): 3}, "operator", "reason"),
        ((_public_id(501),), {_public_id(500): 2, _public_id(501): 4}, "", "reason"),
        ((_public_id(501),), {_public_id(500): 2, _public_id(501): 4}, "operator", " "),
    ],
)
def test_invalid_merge_requests_roll_back_without_any_write(
    identity_session_factory: sessionmaker[Session],
    sources: tuple[str, ...],
    expected: dict[str, int],
    actor: str,
    reason: str,
) -> None:
    """Catch incomplete revision guards and malformed manual merge membership."""
    target, _, _, _, _ = _seed_manual_graph(identity_session_factory)
    before: tuple[dict[str, int], int]
    with identity_session_factory() as session:
        before = (_revision_map(session), session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) or 0)
    with pytest.raises((IdentityConflictError, IdentityInvariantError)):
        _service(identity_session_factory).merge_players(
            target_player_public_id=target,
            source_player_public_ids=sources,
            expected_revisions=expected,
            actor=actor,
            reason=reason,
        )
    with identity_session_factory() as session:
        after = (_revision_map(session), session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) or 0)
    assert after == before


def test_competing_writer_is_typed_busy_and_exact_resolution_race_is_idempotent(
    identity_database_path: Path,
    identity_engine: Engine,
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch leaked SQLite lock errors or duplicate automatic canonical identities."""
    with identity_session_factory.begin() as session:
        replay = _seed_replay(session)
        run = _seed_run_with_slots(session, replay, ((0, "human", "leex279", 1_400),))
    raw = sqlite3.connect(identity_database_path, timeout=0.01)
    raw.execute("PRAGMA busy_timeout=1")
    raw.execute("BEGIN IMMEDIATE")
    raw.execute("UPDATE replays SET updated_at = updated_at WHERE public_id = ?", (replay.public_id,))
    try:
        with identity_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=1")
        with pytest.raises(IdentityBusyError):
            _service(identity_session_factory).resolve_parser_run(
                replay_public_id=replay.public_id, parser_run_id=run.run_id
            )
    finally:
        raw.rollback()
        raw.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _service(identity_session_factory, 30_000 + index).resolve_parser_run,
                replay_public_id=replay.public_id,
                parser_run_id=run.run_id,
            )
            for index in range(2)
        ]
        results = [future.result() for future in futures]
    assert sorted(result.decisions[0].outcome for result in results) == ["already_linked", "created"]
    with identity_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Player)) == 1
        assert session.scalar(select(func.count()).select_from(PlayerAlias)) == 1
        assert session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) == 1
