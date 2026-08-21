"""Packaged Alembic baseline and metadata parity tests."""

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, Table, inspect, text

from generals_replay_analyzer.db import (
    Base,
    create_database_engine,
    downgrade_database,
    make_alembic_config,
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


def test_packaged_baseline_has_one_head_and_exact_schema(database_path: Path) -> None:
    """Catch missing tables, multiple heads, unnamed critical objects, or ORM drift."""
    config = make_alembic_config(database_path)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0001_replay_analyzer_v2"]

    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == APPLICATION_TABLES | {"alembic_version"}

        with engine.connect() as connection:
            triggers = {
                str(row[0]) for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
            }
            assert triggers == IMMUTABILITY_TRIGGERS
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []

        required_indexes = {
            "ix_managed_assets_kind_created_at",
            "ix_replays_lifecycle_map_version_start",
            "ix_parser_runs_replay_status_version",
            "uq_parser_runs_successful_identity",
            "ix_telemetry_events_run_frame_type",
            "uq_analysis_runs_successful_cache_key",
            "ix_jobs_status_available_priority",
            "ix_job_dependencies_depends_on_job_id",
        }
        actual_indexes = {
            str(row["name"])
            for table in APPLICATION_TABLES
            for row in inspector.get_indexes(table)
            if row["name"] is not None
        }
        assert required_indexes <= actual_indexes

        required_foreign_keys = {
            "fk_sources_replay_id_replays",
            "fk_commands_parser_run_id_parser_runs",
            "fk_telemetry_events_telemetry_run_id_telemetry_runs",
            "fk_job_dependencies_job_id_jobs",
        }
        actual_foreign_keys = {
            str(foreign_key["name"])
            for table in APPLICATION_TABLES
            for foreign_key in inspector.get_foreign_keys(table)
        }
        assert required_foreign_keys <= actual_foreign_keys

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
