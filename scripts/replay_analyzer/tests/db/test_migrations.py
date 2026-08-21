"""Packaged Alembic baseline and metadata parity tests."""

import json
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, Table, inspect, text

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
    assert scripts.get_heads() == ["0001_replay_analyzer_v2"]

    upgrade_database(database_path)
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
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []

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


def test_baseline_does_not_create_tables_owned_by_future_revisions(database_path: Path) -> None:
    """Catch a live-metadata baseline that preempts a later Alembic revision."""
    future_table = Table("future_revision_table", Base.metadata, Column("id", Integer, primary_key=True))
    try:
        upgrade_database(database_path)
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
        upgrade_database(database_path)
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
