"""Packaged Alembic baseline and metadata parity tests."""

import json
from pathlib import Path
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, Table, inspect, text
from sqlalchemy.exc import IntegrityError

from generals_replay_analyzer.db import (
    Base,
    create_database_engine,
    downgrade_database,
    make_alembic_config,
    models,
    upgrade_database,
)

APPLICATION_TABLES = {
    "analysis_runs",
    "assessment_evidence",
    "combat_events",
    "commands",
    "economy_events",
    "entities",
    "entity_samples",
    "evidence_items",
    "feature_evidence",
    "feature_sets",
    "features",
    "job_dependencies",
    "jobs",
    "longitudinal_members",
    "longitudinal_results",
    "longitudinal_runs",
    "managed_assets",
    "map_regions",
    "map_resources",
    "maps",
    "parser_runs",
    "player_aliases",
    "players",
    "production_events",
    "replay_players",
    "replay_quality_issues",
    "replays",
    "reports",
    "sources",
    "strategy_assessments",
    "telemetry_events",
    "telemetry_runs",
}
EXPECTED_SCHEMA_PATH = Path(__file__).with_name("schema_0001_expected.json")

IMMUTABILITY_TRIGGERS = {
    "trg_parser_runs_succeeded_no_delete",
    "trg_parser_runs_succeeded_no_update",
    "trg_telemetry_runs_succeeded_no_delete",
    "trg_telemetry_runs_succeeded_no_update",
    "trg_commands_succeeded_no_insert",
    "trg_commands_succeeded_no_update",
    "trg_commands_succeeded_no_delete",
    "trg_evidence_items_observed_no_insert",
    "trg_evidence_items_observed_no_update",
    "trg_evidence_items_observed_no_delete",
    "trg_replay_players_succeeded_no_delete",
    "trg_replay_players_succeeded_no_insert",
    "trg_replay_players_succeeded_no_observation_update",
}
for table_name in (
    "telemetry_events",
    "entities",
    "entity_samples",
    "production_events",
    "economy_events",
    "combat_events",
):
    IMMUTABILITY_TRIGGERS.update(
        {
            f"trg_{table_name}_succeeded_no_insert",
            f"trg_{table_name}_succeeded_no_update",
            f"trg_{table_name}_succeeded_no_delete",
        }
    )


def _schema_fingerprint(database_path: Path) -> tuple[tuple[str, str, str], ...]:
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            )
            return tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows)
    finally:
        engine.dispose()


def _schema_snapshot(engine: object) -> dict[str, Any]:
    inspector = inspect(engine)
    table_names = sorted(name for name in inspector.get_table_names() if name != "alembic_version")
    indexes: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    foreign_keys: list[dict[str, Any]] = []
    for table_name in table_names:
        for index in inspector.get_indexes(table_name):
            where = index.get("dialect_options", {}).get("sqlite_where")
            indexes.append(
                {
                    "columns": list(index["column_names"]),
                    "name": index["name"],
                    "partial_where": None if where is None else str(where),
                    "table": table_name,
                    "unique": bool(index["unique"]),
                }
            )
        for check in inspector.get_check_constraints(table_name):
            checks.append({"name": check["name"], "sqltext": check["sqltext"], "table": table_name})
        for foreign_key in inspector.get_foreign_keys(table_name):
            foreign_keys.append(
                {
                    "columns": list(foreign_key["constrained_columns"]),
                    "name": foreign_key["name"],
                    "ondelete": foreign_key.get("options", {}).get("ondelete"),
                    "referred_columns": list(foreign_key["referred_columns"]),
                    "referred_table": foreign_key["referred_table"],
                    "table": table_name,
                }
            )
    with engine.connect() as connection:  # type: ignore[attr-defined]
        master = connection.execute(
            text(
                "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ).all()
        triggers = connection.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
        ).all()
    return {
        "checks": sorted(checks, key=lambda item: (item["table"], item["name"] or "")),
        "foreign_keys": sorted(foreign_keys, key=lambda item: (item["table"], item["name"] or "")),
        "indexes": sorted(indexes, key=lambda item: (item["table"], item["name"] or "")),
        "master": [[str(row[0]), str(row[1]), str(row[2])] for row in master],
        "tables": table_names,
        "triggers": [{"name": str(row[0]), "sql": str(row[1])} for row in triggers],
    }


def _expected_schema() -> dict[str, Any]:
    return json.loads(EXPECTED_SCHEMA_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_packaged_baseline_has_one_head_and_exact_independent_schema(database_path: Path) -> None:
    """Compare every table, named index/check/FK, predicate, action, and trigger to a frozen oracle."""
    config = make_alembic_config(database_path)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0004_job_lifecycle"]

    upgrade_database(database_path, "0001_replay_analyzer_v2")
    engine = create_database_engine(database_path)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == APPLICATION_TABLES | {"alembic_version"}
        assert _schema_snapshot(engine) == _expected_schema()

        with engine.connect() as connection:
            triggers = {
                str(row[0]) for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
            }
            assert triggers == IMMUTABILITY_TRIGGERS
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"

        for table in APPLICATION_TABLES:
            indexed_leading_columns = {
                tuple(index["column_names"]) for index in inspector.get_indexes(table) if index["column_names"]
            }
            indexed_leading_columns.update(
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table)
                if constraint["column_names"]
            )
            primary_key = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
            if primary_key:
                indexed_leading_columns.add(primary_key)
            for foreign_key in inspector.get_foreign_keys(table):
                for column in foreign_key["constrained_columns"]:
                    assert any(columns[0] == column for columns in indexed_leading_columns), (table, column)
    finally:
        engine.dispose()


def test_downgrade_and_reupgrade_restore_identical_schema(database_path: Path) -> None:
    """Catch checkout-only migrations and incomplete trigger/index teardown."""
    upgrade_database(database_path)
    first = _schema_fingerprint(database_path)
    downgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version'")
                ).all()
                == []
            )
    finally:
        engine.dispose()

    upgrade_database(database_path)
    assert _schema_fingerprint(database_path) == first


def test_feature_partial_quality_migration_preserves_rows_and_enforces_states(database_path: Path) -> None:
    """Catch a feature-table rebuild that loses evidence links or cannot persist truthful partial reasons."""
    upgrade_database(database_path, "0002_player_identity_audit")
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO replays (public_id, sha256, replay_name, version_string, version_number, frame_count, "
                    "start_time, end_time, exe_crc, ini_crc, map_crc, map_name, seed, header_json, lifecycle_state, "
                    "created_at, updated_at) VALUES (:public_id, :sha256, 'fixture', '1.04', 1, 30, 0, 30, 1, 2, 3, "
                    "'map', 4, '{}', 'parsed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"public_id": "00000000-0000-4000-8000-000000000101", "sha256": "1" * 64},
            )
            replay_id = connection.execute(text("SELECT id FROM replays")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
                    "created_at) VALUES (:public_id, :replay_id, :tier, :source_kind, :source_key, 1, CURRENT_TIMESTAMP)"
                ),
                [
                    {
                        "public_id": "00000000-0000-4000-8000-000000000102",
                        "replay_id": replay_id,
                        "tier": "observed",
                        "source_kind": "telemetry",
                        "source_key": "telemetry:fixture:1",
                    },
                    {
                        "public_id": "00000000-0000-4000-8000-000000000103",
                        "replay_id": replay_id,
                        "tier": "derived",
                        "source_kind": "feature",
                        "source_key": "feature:fixture:complete",
                    },
                ],
            )
            observed_id = connection.execute(
                text("SELECT id FROM evidence_items WHERE tier = 'observed'")
            ).scalar_one()
            derived_id = connection.execute(
                text("SELECT id FROM evidence_items WHERE tier = 'derived'")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO feature_sets (public_id, replay_id, extractor_name, extractor_version, input_digest, "
                    "cache_key, status, settings_json, created_at, completed_at) VALUES (:public_id, :replay_id, "
                    "'fixture', 'v1', :input_digest, :cache_key, 'succeeded', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "public_id": "00000000-0000-4000-8000-000000000104",
                    "replay_id": replay_id,
                    "input_digest": "2" * 64,
                    "cache_key": "3" * 64,
                },
            )
            feature_set_id = connection.execute(text("SELECT id FROM feature_sets")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO features (public_id, feature_set_id, evidence_item_id, name, value_type, integer_value, "
                    "unit, scope_type, scope_key, frame_start, frame_end, quality, details_json) VALUES (:public_id, "
                    ":feature_set_id, :evidence_item_id, 'fixture.complete', 'integer', 7, 'count', 'replay', 'fixture', "
                    "0, 30, 'available', '{}')"
                ),
                {
                    "public_id": "00000000-0000-4000-8000-000000000105",
                    "feature_set_id": feature_set_id,
                    "evidence_item_id": derived_id,
                },
            )
            feature_id = connection.execute(text("SELECT id FROM features")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO feature_evidence (feature_id, evidence_item_id, role) "
                    "VALUES (:feature_id, :evidence_item_id, 'input')"
                ),
                {"feature_id": feature_id, "evidence_item_id": observed_id},
            )
    finally:
        engine.dispose()

    upgrade_database(database_path, "0003_feature_partial_quality")
    migrated = create_database_engine(database_path)
    try:
        with migrated.begin() as connection:
            assert connection.execute(text("SELECT integer_value FROM features")).scalar_one() == 7
            assert connection.execute(text("SELECT role FROM feature_evidence")).scalar_one() == "input"
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            replay_id = connection.execute(text("SELECT id FROM replays")).scalar_one()
            feature_set_id = connection.execute(text("SELECT id FROM feature_sets")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
                    "created_at) VALUES ('00000000-0000-4000-8000-000000000106', :replay_id, 'derived', 'feature', "
                    "'feature:fixture:partial', 1, CURRENT_TIMESTAMP)"
                ),
                {"replay_id": replay_id},
            )
            partial_evidence_id = connection.execute(
                text("SELECT id FROM evidence_items WHERE source_key = 'feature:fixture:partial'")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO features (public_id, feature_set_id, evidence_item_id, name, value_type, real_value, "
                    "unit, scope_type, scope_key, frame_start, frame_end, quality, quality_reason, details_json) VALUES "
                    "('00000000-0000-4000-8000-000000000107', :feature_set_id, :evidence_item_id, 'fixture.partial', "
                    "'real', 1.234567890123, 'ratio', 'replay', 'fixture', 0, 30, 'partial', 'omitted_unknown_source', '{}')"
                ),
                {"feature_set_id": feature_set_id, "evidence_item_id": partial_evidence_id},
            )
            assert connection.execute(
                text("SELECT real_value, quality_reason FROM features WHERE quality = 'partial'")
            ).one() == (1.234567890123, "omitted_unknown_source")

        invalid_statements = (
            "UPDATE features SET quality = 'partial', quality_reason = NULL WHERE name = 'fixture.complete'",
            "UPDATE features SET quality = 'available', quality_reason = 'not_allowed' WHERE name = 'fixture.complete'",
            (
                "UPDATE features SET quality = 'unavailable', integer_value = NULL, quality_reason = '' "
                "WHERE name = 'fixture.complete'"
            ),
        )
        for statement in invalid_statements:
            with migrated.begin() as connection, pytest.raises(IntegrityError):
                connection.execute(text(statement))
        with migrated.begin() as connection:
            replay_id = connection.execute(text("SELECT id FROM replays")).scalar_one()
            feature_set_id = connection.execute(text("SELECT id FROM feature_sets")).scalar_one()
            feature_id = connection.execute(
                text("SELECT id FROM features WHERE name = 'fixture.partial'")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO players (public_id, display_name, identity_revision, updated_at, created_at) VALUES "
                    "('00000000-0000-4000-8000-000000000108', 'Fixture', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            player_id = connection.execute(text("SELECT id FROM players")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
                    "completion_status, warnings_json, started_at, completed_at) VALUES "
                    "('00000000-0000-4000-8000-000000000109', :replay_id, 'fixture-v1', 1, :input_sha256, 'failed', "
                    "'failed', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"replay_id": replay_id, "input_sha256": "1" * 64},
            )
            parser_run_id = connection.execute(text("SELECT id FROM parser_runs")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO replay_players (public_id, replay_id, parser_run_id, player_id, slot_index, "
                    "slot_kind, original_name, normalized_name, player_index, observed_json) VALUES "
                    "('00000000-0000-4000-8000-00000000010a', :replay_id, :parser_run_id, :player_id, 0, 'human', "
                    "'Fixture', 'fixture', 0, '{}')"
                ),
                {"replay_id": replay_id, "parser_run_id": parser_run_id, "player_id": player_id},
            )
            replay_player_id = connection.execute(text("SELECT id FROM replay_players")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
                    "created_at) VALUES ('00000000-0000-4000-8000-00000000010b', :replay_id, 'derived', "
                    "'longitudinal', 'longitudinal:fixture:result', 1, CURRENT_TIMESTAMP)"
                ),
                {"replay_id": replay_id},
            )
            longitudinal_evidence_id = connection.execute(
                text("SELECT id FROM evidence_items WHERE source_kind = 'longitudinal'")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO longitudinal_runs (run_id, player_id, identity_revision, analyzer_name, "
                    "analyzer_version, segment_key_json, settings_json, input_digest, cache_key, status, created_at, "
                    "completed_at) VALUES ('00000000-0000-4000-8000-00000000010c', :player_id, 0, 'fixture', 'v1', "
                    "'{}', '{}', :input_digest, :cache_key, 'succeeded', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"player_id": player_id, "input_digest": "4" * 64, "cache_key": "5" * 64},
            )
            longitudinal_run_id = connection.execute(text("SELECT id FROM longitudinal_runs")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO longitudinal_results (public_id, longitudinal_run_id, evidence_item_id, result_name, "
                    "result_kind, sample_count, missing_count, quality, statistics_json) VALUES "
                    "('00000000-0000-4000-8000-00000000010d', :run_id, :evidence_id, 'fixture.result', 'real', "
                    "1, 0, 'available', '{}')"
                ),
                {"run_id": longitudinal_run_id, "evidence_id": longitudinal_evidence_id},
            )
            longitudinal_result_id = connection.execute(text("SELECT id FROM longitudinal_results")).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO longitudinal_members (longitudinal_result_id, replay_id, replay_player_id, "
                    "feature_set_id, feature_id, evidence_item_id) VALUES (:result_id, :replay_id, :replay_player_id, "
                    ":feature_set_id, :feature_id, :evidence_id)"
                ),
                {
                    "result_id": longitudinal_result_id,
                    "replay_id": replay_id,
                    "replay_player_id": replay_player_id,
                    "feature_set_id": feature_set_id,
                    "feature_id": feature_id,
                    "evidence_id": longitudinal_evidence_id,
                },
            )
            member_id = connection.execute(text("SELECT id FROM longitudinal_members")).scalar_one()
            feature_indexes = {
                row[1] for row in connection.execute(text("PRAGMA index_list('features')"))
            }
    finally:
        migrated.dispose()

    downgrade_database(database_path, "0002_player_identity_audit")
    downgraded = create_database_engine(database_path)
    try:
        with downgraded.connect() as connection:
            partial = connection.execute(
                text(
                    "SELECT id, public_id, real_value, quality, quality_reason, details_json FROM features "
                    "WHERE name = 'fixture.partial'"
                )
            ).one()
            assert partial[:5] == (
                feature_id,
                "00000000-0000-4000-8000-000000000107",
                1.234567890123,
                "partial",
                None,
            )
            compatibility = json.loads(partial.details_json)
            assert compatibility == {
                "__task6_partial_quality_compatibility__": {
                    "details_json": "{}",
                    "feature_public_id": "00000000-0000-4000-8000-000000000107",
                    "quality_reason": "omitted_unknown_source",
                    "schema": "task6-partial-quality-downgrade-v1",
                }
            }
            assert connection.execute(
                text("SELECT feature_id FROM longitudinal_members WHERE id = :id"), {"id": member_id}
            ).scalar_one() == feature_id
            assert connection.execute(text("SELECT role FROM feature_evidence")).scalar_one() == "input"
            assert {row[1] for row in connection.execute(text("PRAGMA index_list('features')"))} == feature_indexes
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        downgraded.dispose()

    upgrade_database(database_path, "0003_feature_partial_quality")
    reupgraded = create_database_engine(database_path)
    try:
        with reupgraded.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT id, public_id, real_value, quality, quality_reason, details_json FROM features "
                    "WHERE name = 'fixture.partial'"
                )
            ).one() == (
                feature_id,
                "00000000-0000-4000-8000-000000000107",
                1.234567890123,
                "partial",
                "omitted_unknown_source",
                "{}",
            )
            assert connection.execute(
                text("SELECT feature_id FROM longitudinal_members WHERE id = :id"), {"id": member_id}
            ).scalar_one() == feature_id
            assert connection.execute(text("SELECT role FROM feature_evidence")).scalar_one() == "input"
            assert {row[1] for row in connection.execute(text("PRAGMA index_list('features')"))} == feature_indexes
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        reupgraded.dispose()


def test_baseline_does_not_create_tables_owned_by_future_revisions(database_path: Path) -> None:
    """Catch a live-metadata baseline that preempts a later Alembic revision."""
    future_table = Table("future_revision_table", Base.metadata, Column("id", Integer, primary_key=True))
    try:
        upgrade_database(database_path, "0001_replay_analyzer_v2")
        engine = create_database_engine(database_path)
        try:
            assert "future_revision_table" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()
    finally:
        Base.metadata.remove(future_table)


def test_baseline_is_independent_of_existing_live_table_and_trigger_changes(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch revision 0001 consulting mutable ORM table shapes or trigger helpers at runtime."""
    managed_assets = Base.metadata.tables["managed_assets"]
    future_column = Column("future_revision_column", Integer)
    managed_assets.append_column(future_column)
    monkeypatch.setattr(
        models,
        "immutability_triggers",
        lambda: [
            (
                "trg_future_revision_only",
                "CREATE TRIGGER trg_future_revision_only BEFORE INSERT ON managed_assets BEGIN SELECT 1; END",
            )
        ],
    )
    try:
        upgrade_database(database_path, "0001_replay_analyzer_v2")
        engine = create_database_engine(database_path)
        try:
            inspector = inspect(engine)
            assert "future_revision_column" not in {
                str(column["name"]) for column in inspector.get_columns("managed_assets")
            }
            with engine.connect() as connection:
                trigger_names = {
                    str(row[0])
                    for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
                }
                assert trigger_names == IMMUTABILITY_TRIGGERS
            assert _schema_snapshot(engine) == _expected_schema()
        finally:
            engine.dispose()
    finally:
        managed_assets._columns.remove(future_column)
