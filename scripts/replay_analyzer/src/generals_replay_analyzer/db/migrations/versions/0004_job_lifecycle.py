"""Add secure durable job lifecycle, evidence, and log references."""

from alembic import op

revision = "0004_job_lifecycle"
down_revision = "0003_feature_partial_quality"
branch_labels = None
depends_on = None

_UUID_CHECK = (
    "length(public_id) = 36 AND public_id = lower(public_id) "
    "AND substr(public_id, 9, 1) = '-' AND substr(public_id, 14, 1) = '-' "
    "AND substr(public_id, 19, 1) = '-' AND substr(public_id, 24, 1) = '-' "
    "AND length(replace(public_id, '-', '')) = 32 "
    "AND replace(public_id, '-', '') NOT GLOB '*[^0-9a-f]*'"
)
_EXECUTION_UUID_CHECK = (
    "lease_execution_public_id IS NULL OR (length(lease_execution_public_id) = 36 "
    "AND lease_execution_public_id = lower(lease_execution_public_id) "
    "AND substr(lease_execution_public_id, 9, 1) = '-' "
    "AND substr(lease_execution_public_id, 14, 1) = '-' "
    "AND substr(lease_execution_public_id, 19, 1) = '-' "
    "AND substr(lease_execution_public_id, 24, 1) = '-' "
    "AND length(replace(lease_execution_public_id, '-', '')) = 32 "
    "AND replace(lease_execution_public_id, '-', '') NOT GLOB '*[^0-9a-f]*')"
)

_NEW_JOBS = f"""CREATE TABLE jobs (
	replay_id INTEGER,
	stage VARCHAR(64) NOT NULL,
	component_version VARCHAR(255) NOT NULL,
	idempotency_key TEXT NOT NULL,
	status VARCHAR(32) NOT NULL,
	priority INTEGER NOT NULL,
	attempt_count INTEGER NOT NULL,
	max_attempts INTEGER NOT NULL,
	available_at DATETIME NOT NULL,
	lease_owner VARCHAR(255),
	lease_expires_at DATETIME,
	started_at DATETIME,
	completed_at DATETIME,
	input_json TEXT NOT NULL,
	output_json TEXT,
	error_code VARCHAR(128),
	error_message TEXT,
	error_details_json TEXT,
	retryable BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	revision INTEGER NOT NULL,
	lease_token_sha256 VARCHAR(64),
	lease_execution_public_id VARCHAR(36),
	last_heartbeat_at DATETIME,
	cancel_requested_at DATETIME,
	cancel_requested_by VARCHAR(255),
	cancel_reason_code VARCHAR(128),
	progress_completed INTEGER,
	progress_total INTEGER,
	progress_unit VARCHAR(64),
	progress_updated_at DATETIME,
	id INTEGER NOT NULL,
	public_id VARCHAR(36) NOT NULL CONSTRAINT ck_jobs_public_id_lowercase_uuid CHECK ({_UUID_CHECK}),
	CONSTRAINT pk_jobs PRIMARY KEY (id),
	CONSTRAINT ck_jobs_status_valid CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
	CONSTRAINT ck_jobs_priority_nonnegative CHECK (priority >= 0),
	CONSTRAINT ck_jobs_attempts_valid CHECK (max_attempts >= 1 AND attempt_count >= 0 AND attempt_count <= max_attempts),
	CONSTRAINT ck_jobs_revision_nonnegative CHECK (revision >= 0),
	CONSTRAINT ck_jobs_retryable_boolean CHECK (retryable IN (0, 1)),
	CONSTRAINT ck_jobs_terminal_state_valid CHECK ((status IN ('succeeded','failed','cancelled') AND completed_at IS NOT NULL) OR (status IN ('pending','running') AND completed_at IS NULL)),
	CONSTRAINT ck_jobs_terminal_retry_valid CHECK (status NOT IN ('succeeded','cancelled') OR retryable = 0),
	CONSTRAINT ck_jobs_lease_token_sha256_lowercase_sha256 CHECK (lease_token_sha256 IS NULL OR (length(lease_token_sha256) = 64 AND lease_token_sha256 = lower(lease_token_sha256) AND lease_token_sha256 NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT ck_jobs_lease_execution_uuid CHECK ({_EXECUTION_UUID_CHECK}),
	CONSTRAINT ck_jobs_lease_state_valid CHECK ((status = 'running' AND lease_owner IS NOT NULL AND length(trim(lease_owner)) > 0 AND lease_expires_at IS NOT NULL AND lease_token_sha256 IS NOT NULL AND lease_execution_public_id IS NOT NULL AND last_heartbeat_at IS NOT NULL) OR (status != 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL AND lease_token_sha256 IS NULL AND lease_execution_public_id IS NULL AND last_heartbeat_at IS NULL)),
	CONSTRAINT ck_jobs_cancellation_facts_all_or_none CHECK ((cancel_requested_at IS NULL AND cancel_requested_by IS NULL AND cancel_reason_code IS NULL) OR (cancel_requested_at IS NOT NULL AND cancel_requested_by IS NOT NULL AND length(trim(cancel_requested_by)) > 0 AND cancel_reason_code IS NOT NULL AND length(trim(cancel_reason_code)) > 0 AND status IN ('running','failed','cancelled'))),
	CONSTRAINT ck_jobs_progress_all_or_none CHECK ((progress_completed IS NULL AND progress_total IS NULL AND progress_unit IS NULL AND progress_updated_at IS NULL) OR (progress_completed IS NOT NULL AND progress_total IS NOT NULL AND progress_unit IS NOT NULL AND progress_updated_at IS NOT NULL AND progress_completed >= 0 AND progress_total > 0 AND progress_completed <= progress_total AND length(trim(progress_unit)) BETWEEN 1 AND 64)),
	CONSTRAINT fk_jobs_replay_id_replays FOREIGN KEY(replay_id) REFERENCES replays (id) ON DELETE SET NULL,
	CONSTRAINT uq_jobs_idempotency_key UNIQUE (idempotency_key),
	CONSTRAINT uq_jobs_public_id UNIQUE (public_id)
)"""

_OLD_JOBS = f"""CREATE TABLE jobs (
	replay_id INTEGER,
	stage VARCHAR(64) NOT NULL,
	component_version VARCHAR(255) NOT NULL,
	idempotency_key TEXT NOT NULL,
	status VARCHAR(32) NOT NULL,
	priority INTEGER NOT NULL,
	attempt_count INTEGER NOT NULL,
	max_attempts INTEGER NOT NULL,
	available_at DATETIME NOT NULL,
	lease_owner VARCHAR(255),
	lease_expires_at DATETIME,
	started_at DATETIME,
	completed_at DATETIME,
	input_json TEXT NOT NULL,
	output_json TEXT,
	error_code VARCHAR(128),
	error_message TEXT,
	error_details_json TEXT,
	retryable BOOLEAN NOT NULL,
	id INTEGER NOT NULL,
	public_id VARCHAR(36) NOT NULL CONSTRAINT ck_jobs_public_id_lowercase_uuid CHECK ({_UUID_CHECK}),
	CONSTRAINT pk_jobs PRIMARY KEY (id),
	CONSTRAINT ck_jobs_status_valid CHECK (status IN ('pending','running','succeeded','failed')),
	CONSTRAINT ck_jobs_priority_nonnegative CHECK (priority >= 0),
	CONSTRAINT ck_jobs_attempts_nonnegative CHECK (attempt_count >= 0 AND max_attempts >= 0),
	CONSTRAINT ck_jobs_attempt_count_within_maximum CHECK (attempt_count <= max_attempts),
	CONSTRAINT ck_jobs_retryable_boolean CHECK (retryable IN (0, 1)),
	CONSTRAINT ck_jobs_lease_state_valid CHECK ((status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status != 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
	CONSTRAINT fk_jobs_replay_id_replays FOREIGN KEY(replay_id) REFERENCES replays (id) ON DELETE SET NULL,
	CONSTRAINT uq_jobs_idempotency_key UNIQUE (idempotency_key),
	CONSTRAINT uq_jobs_public_id UNIQUE (public_id)
)"""

_DEPENDENCIES = """CREATE TABLE job_dependencies (
	job_id INTEGER NOT NULL,
	depends_on_job_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_job_dependencies PRIMARY KEY (job_id, depends_on_job_id),
	CONSTRAINT ck_job_dependencies_not_self_edge CHECK (job_id != depends_on_job_id),
	CONSTRAINT fk_job_dependencies_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE,
	CONSTRAINT fk_job_dependencies_depends_on_job_id_jobs FOREIGN KEY(depends_on_job_id) REFERENCES jobs (id) ON DELETE CASCADE
)"""

_OLD_COLUMNS = (
    "replay_id, stage, component_version, idempotency_key, status, priority, attempt_count, max_attempts, "
    "available_at, lease_owner, lease_expires_at, started_at, completed_at, input_json, output_json, error_code, "
    "error_message, error_details_json, retryable, id, public_id"
)


def _drop_dependencies(connection: object) -> list[tuple[object, ...]]:
    edges = [
        tuple(row)
        for row in connection.exec_driver_sql(  # type: ignore[attr-defined]
            "SELECT job_id, depends_on_job_id, created_at FROM job_dependencies ORDER BY job_id, depends_on_job_id"
        )
    ]
    connection.exec_driver_sql("DROP TABLE job_dependencies")  # type: ignore[attr-defined]
    return edges


def _restore_dependencies(connection: object, edges: list[tuple[object, ...]]) -> None:
    connection.exec_driver_sql(_DEPENDENCIES)  # type: ignore[attr-defined]
    connection.exec_driver_sql("CREATE INDEX ix_job_dependencies_job_id ON job_dependencies (job_id)")  # type: ignore[attr-defined]
    connection.exec_driver_sql(  # type: ignore[attr-defined]
        "CREATE INDEX ix_job_dependencies_depends_on_job_id ON job_dependencies (depends_on_job_id)"
    )
    if edges:
        connection.exec_driver_sql(  # type: ignore[attr-defined]
            "INSERT INTO job_dependencies (job_id, depends_on_job_id, created_at) VALUES (?, ?, ?)", edges
        )


def _create_new_indexes(connection: object) -> None:
    for statement in (
        "CREATE INDEX ix_jobs_replay_id ON jobs (replay_id)",
        "CREATE INDEX ix_jobs_claim ON jobs (status, stage, available_at, priority)",
        "CREATE INDEX ix_jobs_recovery ON jobs (status, lease_expires_at)",
        "CREATE INDEX ix_jobs_cancellation ON jobs (status, cancel_requested_at)",
        "CREATE INDEX ix_jobs_ui_listing ON jobs (replay_id, status, created_at)",
    ):
        connection.exec_driver_sql(statement)  # type: ignore[attr-defined]


def _create_old_indexes(connection: object) -> None:
    for statement in (
        "CREATE INDEX ix_jobs_replay_id ON jobs (replay_id)",
        "CREATE INDEX ix_jobs_status_available_priority ON jobs (status, available_at, priority)",
        "CREATE INDEX ix_jobs_status_lease_expires ON jobs (status, lease_expires_at)",
    ):
        connection.exec_driver_sql(statement)  # type: ignore[attr-defined]


def _create_children(connection: object) -> None:
    connection.exec_driver_sql(  # type: ignore[attr-defined]
        f"""CREATE TABLE job_stage_results (
	job_id INTEGER NOT NULL,
	stage VARCHAR(64) NOT NULL,
	component_version VARCHAR(255) NOT NULL,
	idempotency_key TEXT NOT NULL,
	output_json TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	id INTEGER NOT NULL,
	public_id VARCHAR(36) NOT NULL CONSTRAINT ck_job_stage_results_public_id_lowercase_uuid CHECK ({_UUID_CHECK}),
	CONSTRAINT pk_job_stage_results PRIMARY KEY (id),
	CONSTRAINT uq_job_stage_results_job_id UNIQUE (job_id),
	CONSTRAINT uq_job_stage_results_job_identity UNIQUE (job_id, idempotency_key),
	CONSTRAINT fk_job_stage_results_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
	CONSTRAINT uq_job_stage_results_public_id UNIQUE (public_id)
)"""
    )
    connection.exec_driver_sql(  # type: ignore[attr-defined]
        f"""CREATE TABLE job_events (
	job_id INTEGER NOT NULL,
	revision INTEGER NOT NULL,
	event_kind VARCHAR(64) NOT NULL,
	state VARCHAR(32) NOT NULL,
	attempt_count INTEGER NOT NULL,
	reason_code VARCHAR(128),
	occurred_at DATETIME NOT NULL,
	id INTEGER NOT NULL,
	public_id VARCHAR(36) NOT NULL CONSTRAINT ck_job_events_public_id_lowercase_uuid CHECK ({_UUID_CHECK}),
	CONSTRAINT pk_job_events PRIMARY KEY (id),
	CONSTRAINT ck_job_events_revision_nonnegative CHECK (revision >= 0),
	CONSTRAINT ck_job_events_attempt_nonnegative CHECK (attempt_count >= 0),
	CONSTRAINT ck_job_events_kind_valid CHECK (event_kind IN ('claimed','progress','cancel_requested','cancelled','succeeded','failed','retry_requested','lease_expired','worker_shutdown','dependency_failed','dependency_cancelled','result_reused')),
	CONSTRAINT ck_job_events_state_valid CHECK (state IN ('pending','running','succeeded','failed','cancelled')),
	CONSTRAINT uq_job_events_job_revision UNIQUE (job_id, revision),
	CONSTRAINT fk_job_events_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
	CONSTRAINT uq_job_events_public_id UNIQUE (public_id)
)"""
    )
    connection.exec_driver_sql(  # type: ignore[attr-defined]
        f"""CREATE TABLE job_log_snapshots (
	job_id INTEGER NOT NULL,
	attempt_count INTEGER NOT NULL,
	label VARCHAR(64) NOT NULL,
	sequence INTEGER NOT NULL,
	managed_asset_id INTEGER NOT NULL,
	media_type VARCHAR(32) NOT NULL,
	byte_count INTEGER NOT NULL,
	redaction_version VARCHAR(64) NOT NULL,
	integrity_version VARCHAR(32) NOT NULL,
	integrity_root_sha256 VARCHAR(64) NOT NULL,
	integrity_chunk_size INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	id INTEGER NOT NULL,
	public_id VARCHAR(36) NOT NULL CONSTRAINT ck_job_log_snapshots_public_id_lowercase_uuid CHECK ({_UUID_CHECK}),
	CONSTRAINT pk_job_log_snapshots PRIMARY KEY (id),
	CONSTRAINT ck_job_log_snapshots_attempt_nonnegative CHECK (attempt_count >= 0),
	CONSTRAINT ck_job_log_snapshots_label_valid CHECK (label IN ('stdout','stderr','supervisor')),
	CONSTRAINT ck_job_log_snapshots_sequence_nonnegative CHECK (sequence >= 0),
	CONSTRAINT ck_job_log_snapshots_media_type_text CHECK (media_type = 'text/plain'),
	CONSTRAINT ck_job_log_snapshots_byte_count_nonnegative CHECK (byte_count >= 0),
	CONSTRAINT ck_job_log_snapshots_redaction_nonempty CHECK (length(trim(redaction_version)) > 0),
	CONSTRAINT ck_job_log_snapshots_integrity_version_closed CHECK (integrity_version = 'sha256-merkle-v1'),
	CONSTRAINT ck_job_log_snapshots_integrity_root_sha256_lowercase_sha256 CHECK (length(integrity_root_sha256) = 64 AND integrity_root_sha256 = lower(integrity_root_sha256) AND integrity_root_sha256 NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_job_log_snapshots_integrity_chunk_size_fixed CHECK (integrity_chunk_size = 4096),
	CONSTRAINT uq_job_log_snapshots_stream_sequence UNIQUE (job_id, attempt_count, label, sequence),
	CONSTRAINT fk_job_log_snapshots_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_log_snapshots_managed_asset_id_managed_assets FOREIGN KEY(managed_asset_id) REFERENCES managed_assets (id) ON DELETE RESTRICT,
	CONSTRAINT uq_job_log_snapshots_public_id UNIQUE (public_id)
)"""
    )
    for statement in (
        "CREATE INDEX ix_job_stage_results_job_id ON job_stage_results (job_id)",
        "CREATE UNIQUE INDEX ix_job_stage_results_job_identity ON job_stage_results (job_id, idempotency_key)",
        "CREATE INDEX ix_job_events_job_id ON job_events (job_id)",
        "CREATE INDEX ix_job_events_job_revision ON job_events (job_id, revision)",
        "CREATE INDEX ix_job_log_snapshots_job_id ON job_log_snapshots (job_id)",
        "CREATE INDEX ix_job_log_snapshots_managed_asset_id ON job_log_snapshots (managed_asset_id)",
        "CREATE INDEX ix_job_log_snapshots_stream ON job_log_snapshots (job_id, attempt_count, label, sequence)",
    ):
        connection.exec_driver_sql(statement)  # type: ignore[attr-defined]
    for table in ("job_stage_results", "job_events", "job_log_snapshots"):
        connection.exec_driver_sql(  # type: ignore[attr-defined]
            f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END"
        )
        connection.exec_driver_sql(  # type: ignore[attr-defined]
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END"
        )


# TheSuperHackers @feature Leex 22/08/2026 Add capability-owned leases and immutable lifecycle evidence. (#TBD)
def upgrade() -> None:
    connection = op.get_bind()
    edges = _drop_dependencies(connection)
    connection.exec_driver_sql("ALTER TABLE jobs RENAME TO task4_jobs_legacy")
    connection.exec_driver_sql(_NEW_JOBS)
    connection.exec_driver_sql(
        f"""INSERT INTO jobs ({_OLD_COLUMNS}, created_at, revision, lease_token_sha256,
	lease_execution_public_id, last_heartbeat_at, cancel_requested_at, cancel_requested_by, cancel_reason_code,
	progress_completed, progress_total, progress_unit, progress_updated_at)
SELECT replay_id, stage, component_version, idempotency_key,
	CASE
		WHEN status = 'running' AND retryable = 1
		     AND attempt_count < CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END THEN 'pending'
		WHEN status = 'running' THEN 'failed'
		WHEN status = 'pending'
		     AND attempt_count >= CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END THEN 'failed'
		ELSE status
	END,
	priority, attempt_count, CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END,
	available_at, NULL, NULL, started_at,
	CASE
		WHEN status IN ('succeeded', 'failed') THEN COALESCE(completed_at, started_at, available_at)
		WHEN status = 'running' AND NOT (
			retryable = 1 AND attempt_count < CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END
		) THEN COALESCE(completed_at, lease_expires_at, started_at, available_at)
		WHEN status = 'pending'
		     AND attempt_count >= CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END
		THEN COALESCE(completed_at, started_at, available_at)
		ELSE NULL
	END,
	input_json, output_json,
	CASE
		WHEN status = 'running' THEN 'migration_recovered_running'
		WHEN status = 'pending'
		     AND attempt_count >= CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END
		THEN 'migration_exhausted_pending'
		ELSE error_code
	END,
	CASE
		WHEN status = 'running' THEN 'legacy running lease recovered without fabricating a capability'
		WHEN status = 'pending'
		     AND attempt_count >= CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END
		THEN 'legacy pending job had no remaining attempt budget'
		ELSE error_message
	END,
	CASE
		WHEN status = 'running' OR (
			status = 'pending' AND attempt_count >= CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END
		) THEN '{{}}'
		ELSE error_details_json
	END,
	CASE
		WHEN status = 'succeeded' THEN 0
		WHEN status = 'running' AND retryable = 1
		     AND attempt_count < CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END THEN 1
		WHEN status = 'running' OR (
			status = 'pending' AND attempt_count >= CASE WHEN max_attempts < 1 THEN 1 ELSE max_attempts END
		) THEN 0
		ELSE retryable
	END,
	id, public_id,
	CASE
		WHEN available_at <= COALESCE(started_at, available_at) AND available_at <= COALESCE(completed_at, available_at)
		THEN available_at
		WHEN started_at IS NOT NULL AND started_at <= COALESCE(completed_at, started_at) THEN started_at
		ELSE completed_at
	END,
	0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
FROM task4_jobs_legacy ORDER BY id"""
    )
    connection.exec_driver_sql("DROP TABLE task4_jobs_legacy")
    _create_new_indexes(connection)
    _restore_dependencies(connection, edges)
    _create_children(connection)


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("job_log_snapshots", "job_events", "job_stage_results"):
        connection.exec_driver_sql(f"DROP TABLE {table}")
    edges = _drop_dependencies(connection)
    connection.exec_driver_sql("ALTER TABLE jobs RENAME TO task4_jobs_lifecycle")
    connection.exec_driver_sql(_OLD_JOBS)
    connection.exec_driver_sql(
        f"""INSERT INTO jobs ({_OLD_COLUMNS})
SELECT replay_id, stage, component_version, idempotency_key,
	CASE WHEN status = 'cancelled' THEN 'failed' ELSE status END,
	priority, attempt_count, max_attempts, available_at,
	CASE WHEN status = 'running' THEN lease_owner ELSE NULL END,
	CASE WHEN status = 'running' THEN lease_expires_at ELSE NULL END,
	started_at, completed_at, input_json, output_json,
	CASE WHEN status = 'cancelled' THEN 'cancelled_downgrade' ELSE error_code END,
	CASE WHEN status = 'cancelled' THEN 'cancelled job represented as failed by schema 0003' ELSE error_message END,
	CASE WHEN status = 'cancelled' THEN '{{}}' ELSE error_details_json END,
	CASE WHEN status = 'cancelled' THEN 0 ELSE retryable END,
	id, public_id
FROM task4_jobs_lifecycle ORDER BY id"""
    )
    connection.exec_driver_sql("DROP TABLE task4_jobs_lifecycle")
    _create_old_indexes(connection)
    _restore_dependencies(connection, edges)
