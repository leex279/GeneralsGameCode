"""Additive durable job-lifecycle migration behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from generals_replay_analyzer.db import (
    Base,
    create_database_engine,
    downgrade_database,
    make_alembic_config,
    upgrade_database,
)


def _insert_legacy_jobs(database_path: Path) -> None:
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            values = (
                ("00000000-0000-4000-8000-000000000101", "pending", 0, 3, None, None, None, 1),
                (
                    "00000000-0000-4000-8000-000000000102",
                    "running",
                    1,
                    3,
                    "legacy-worker",
                    "2026-08-22 12:05:00",
                    None,
                    1,
                ),
                (
                    "00000000-0000-4000-8000-000000000103",
                    "running",
                    1,
                    1,
                    "legacy-worker",
                    "2026-08-22 12:05:00",
                    None,
                    1,
                ),
                (
                    "00000000-0000-4000-8000-000000000104",
                    "succeeded",
                    1,
                    3,
                    None,
                    None,
                    "2026-08-22 12:03:00",
                    0,
                ),
                (
                    "00000000-0000-4000-8000-000000000105",
                    "failed",
                    2,
                    3,
                    None,
                    None,
                    "2026-08-22 12:04:00",
                    0,
                ),
            )
            for offset, row in enumerate(values):
                connection.execute(
                    text(
                        "INSERT INTO jobs (public_id, stage, component_version, idempotency_key, status, priority, "
                        "attempt_count, max_attempts, available_at, lease_owner, lease_expires_at, started_at, "
                        "completed_at, input_json, output_json, error_code, error_message, error_details_json, retryable) "
                        "VALUES (:public_id, :stage, 'component-v1', :key, :status, 100, :attempt_count, :max_attempts, "
                        "'2026-08-22 12:00:00', :owner, :expiry, '2026-08-22 12:01:00', :completed, :input, :output, "
                        ":error_code, :error_message, :details, :retryable)"
                    ),
                    {
                        "public_id": row[0],
                        "stage": f"stage-{offset}",
                        "key": f"job-key-{offset}",
                        "status": row[1],
                        "attempt_count": row[2],
                        "max_attempts": row[3],
                        "owner": row[4],
                        "expiry": row[5],
                        "completed": row[6],
                        "input": f'{{"offset":{offset}}}',
                        "output": '{"ok":true}' if row[1] == "succeeded" else None,
                        "error_code": "legacy_failed" if row[1] == "failed" else None,
                        "error_message": "safe legacy error" if row[1] == "failed" else None,
                        "details": "{}" if row[1] == "failed" else None,
                        "retryable": row[7],
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO job_dependencies (job_id, depends_on_job_id, created_at) "
                    "SELECT child.id, parent.id, '2026-08-22 12:00:30' FROM jobs child, jobs parent "
                    "WHERE child.public_id = '00000000-0000-4000-8000-000000000105' "
                    "AND parent.public_id = '00000000-0000-4000-8000-000000000104'"
                )
            )
    finally:
        engine.dispose()


def test_0004_is_the_only_head_and_matches_application_metadata(database_path: Path) -> None:
    """Catch a missing/branched migration or model shape that diverges from the installed schema."""
    scripts = ScriptDirectory.from_config(make_alembic_config(database_path))
    assert scripts.get_heads() == ["0004_job_lifecycle"]
    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    finally:
        engine.dispose()


def test_upgrade_preserves_legacy_jobs_edges_and_recovers_insecure_running_rows(database_path: Path) -> None:
    """Catch loss of accepted Task 3 data or fabrication of an unverifiable legacy lease capability."""
    upgrade_database(database_path, "0003_feature_partial_quality")
    _insert_legacy_jobs(database_path)
    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        inspector = inspect(engine)
        assert {"job_stage_results", "job_events", "job_log_snapshots"} <= set(inspector.get_table_names())
        assert {
            "ix_jobs_claim",
            "ix_jobs_recovery",
            "ix_jobs_cancellation",
            "ix_jobs_ui_listing",
        } <= {index["name"] for index in inspector.get_indexes("jobs")}
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT public_id, status, attempt_count, max_attempts, lease_owner, lease_token_sha256, "
                    "lease_execution_public_id, last_heartbeat_at, lease_expires_at, error_code, retryable, revision, "
                    "created_at, input_json, output_json FROM jobs ORDER BY id"
                )
            ).all()
            assert [row[1] for row in rows] == ["pending", "pending", "failed", "succeeded", "failed"]
            assert rows[1][9:12] == ("migration_recovered_running", 1, 0)
            assert rows[2][9:12] == ("migration_recovered_running", 0, 0)
            assert all(row[4:9] == (None, None, None, None, None) for row in rows)
            assert all(row[12] == "2026-08-22 12:00:00" for row in rows)
            assert rows[0][13] == '{"offset":0}'
            assert rows[3][14] == '{"ok":true}'
            edge = connection.execute(
                text(
                    "SELECT child.public_id, parent.public_id FROM job_dependencies edge "
                    "JOIN jobs child ON child.id=edge.job_id JOIN jobs parent ON parent.id=edge.depends_on_job_id"
                )
            ).one()
            assert edge == (
                "00000000-0000-4000-8000-000000000105",
                "00000000-0000-4000-8000-000000000104",
            )
    finally:
        engine.dispose()


def test_new_checks_and_immutable_children_reject_invalid_or_mutated_rows(database_path: Path) -> None:
    """Catch broken lease/progress/cancellation invariants and mutable lifecycle evidence."""
    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO jobs (public_id, stage, component_version, idempotency_key, status, priority, "
                    "attempt_count, max_attempts, available_at, created_at, revision, input_json, retryable, "
                    "progress_completed, progress_total, progress_unit, progress_updated_at) VALUES "
                    "('00000000-0000-4000-8000-000000000201', 'parse', '1', 'invalid-progress', 'pending', "
                    "0, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, '{}', 1, 2, 1, 'items', CURRENT_TIMESTAMP)"
                )
            )
        with engine.begin() as connection:
            job_id = int(
                connection.execute(
                    text(
                        "INSERT INTO jobs (public_id, stage, component_version, idempotency_key, status, priority, "
                        "attempt_count, max_attempts, available_at, created_at, revision, input_json, retryable) VALUES "
                        "('00000000-0000-4000-8000-000000000202', 'parse', '1', 'immutable-child', 'pending', "
                        "0, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, '{}', 1)"
                    )
                ).lastrowid
            )
            result_id = int(
                connection.execute(
                    text(
                        "INSERT INTO job_stage_results (public_id, job_id, stage, component_version, "
                        "idempotency_key, output_json, created_at) VALUES "
                        "('00000000-0000-4000-8000-000000000203', :job, 'parse', '1', 'immutable-child', '{}', "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"job": job_id},
                ).lastrowid
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text("UPDATE job_stage_results SET output_json='{}' WHERE id=:id"), {"id": result_id}
                )
            with pytest.raises(IntegrityError):
                connection.execute(text("DELETE FROM job_stage_results WHERE id=:id"), {"id": result_id})
    finally:
        engine.dispose()


def test_cancelled_downgrade_is_lossy_but_preserves_representable_fields_and_edges(database_path: Path) -> None:
    """Catch downgrade that drops jobs/edges or misrepresents cancelled work as reusable pending work."""
    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            parent_id = int(
                connection.execute(
                    text(
                        "INSERT INTO jobs (public_id, stage, component_version, idempotency_key, status, priority, "
                        "attempt_count, max_attempts, available_at, created_at, revision, completed_at, input_json, "
                        "error_code, error_message, error_details_json, retryable, cancel_requested_at, "
                        "cancel_requested_by, cancel_reason_code) VALUES "
                        "('00000000-0000-4000-8000-000000000301', 'parse', '1', 'cancelled', 'cancelled', 7, 0, 3, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 2, CURRENT_TIMESTAMP, :input_json, "
                        "'user_cancelled', 'cancelled safely', '{}', 0, CURRENT_TIMESTAMP, 'operator', 'user_request')"
                    ),
                    {"input_json": '{"kept":true}'},
                ).lastrowid
            )
            child_id = int(
                connection.execute(
                    text(
                        "INSERT INTO jobs (public_id, stage, component_version, idempotency_key, status, priority, "
                        "attempt_count, max_attempts, available_at, created_at, revision, input_json, retryable) VALUES "
                        "('00000000-0000-4000-8000-000000000302', 'report', '1', 'child', 'pending', 6, 0, 3, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, '{}', 1)"
                    )
                ).lastrowid
            )
            connection.execute(
                text(
                    "INSERT INTO job_dependencies (job_id, depends_on_job_id, created_at) VALUES "
                    "(:child, :parent, CURRENT_TIMESTAMP)"
                ),
                {"child": child_id, "parent": parent_id},
            )
    finally:
        engine.dispose()

    downgrade_database(database_path, "0003_feature_partial_quality")
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            cancelled = connection.execute(
                text(
                    "SELECT status, retryable, error_code, input_json, priority FROM jobs "
                    "WHERE public_id='00000000-0000-4000-8000-000000000301'"
                )
            ).one()
            assert cancelled == ("failed", 0, "cancelled_downgrade", '{"kept":true}', 7)
            assert connection.execute(text("SELECT count(*) FROM job_dependencies")).scalar_one() == 1
            assert not {
                "job_stage_results",
                "job_events",
                "job_log_snapshots",
            }.intersection(inspect(engine).get_table_names())
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()

    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
            assert connection.execute(text("SELECT count(*) FROM job_dependencies")).scalar_one() == 1
    finally:
        engine.dispose()
