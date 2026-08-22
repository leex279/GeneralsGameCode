"""Database-boundary canonicalization, uniqueness, and lifecycle tests."""

from collections.abc import Mapping

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError

from generals_replay_analyzer.db.base import utc_now
from generals_replay_analyzer.db.types import CanonicalJSON

SHA_A = "a" * 64
SHA_B = "b" * 64
PUBLIC_A = "00000000-0000-4000-8000-000000000001"
PUBLIC_B = "00000000-0000-4000-8000-000000000002"
PUBLIC_C = "00000000-0000-4000-8000-000000000003"


def _insert_asset(
    connection: object, *, public_id: str = PUBLIC_A, sha256: str = SHA_A, path: str = "replays/a.rep"
) -> int:
    result = connection.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO managed_assets (public_id, sha256, kind, relative_path, size_bytes, created_at) "
            "VALUES (:public_id, :sha256, 'replay', :path, 1, CURRENT_TIMESTAMP)"
        ),
        {"public_id": public_id, "sha256": sha256, "path": path},
    )
    return int(result.lastrowid)


def _insert_replay(
    connection: object,
    *,
    public_id: str = PUBLIC_B,
    sha256: str = SHA_B,
    managed_asset_id: int | None = None,
    lifecycle_state: str = "discovered",
) -> int:
    result = connection.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO replays "
            "(public_id, sha256, managed_asset_id, replay_name, version_string, version_number, frame_count, "
            "start_time, end_time, exe_crc, ini_crc, map_crc, map_name, seed, header_json, lifecycle_state, "
            "created_at, updated_at) VALUES "
            "(:public_id, :sha256, :asset, 'fixture', '1.04', 104, 1, 0, 1, 1, 1, 1, 'map', 1, '{}', "
            ":state, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"public_id": public_id, "sha256": sha256, "asset": managed_asset_id, "state": lifecycle_state},
    )
    return int(result.lastrowid)


def _assert_integrity_error(connection: object, statement: str, parameters: Mapping[str, object]) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():  # type: ignore[attr-defined]
        connection.execute(text(statement), parameters)  # type: ignore[attr-defined]


def test_sha256_public_id_and_core_uniqueness_constraints(migrated_engine: object) -> None:
    """Catch uppercase/malformed digests and duplicate stable identities at the DB boundary."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        asset_id = _insert_asset(connection)
        _insert_replay(connection, managed_asset_id=asset_id)
        for bad_digest in ("A" * 64, "a" * 63, "g" * 64):
            _assert_integrity_error(
                connection,
                "INSERT INTO managed_assets (public_id, sha256, kind, relative_path, size_bytes, created_at) "
                "VALUES (:public_id, :sha256, 'trace', :path, 1, CURRENT_TIMESTAMP)",
                {"public_id": PUBLIC_C, "sha256": bad_digest, "path": f"traces/{bad_digest[:8]}"},
            )
        _assert_integrity_error(
            connection,
            "INSERT INTO managed_assets (public_id, sha256, kind, relative_path, size_bytes, created_at) "
            "VALUES (:public_id, :sha256, 'trace', 'traces/b', 1, CURRENT_TIMESTAMP)",
            {"public_id": PUBLIC_C, "sha256": SHA_A},
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO replays (public_id, sha256, replay_name, version_string, version_number, frame_count, "
            "start_time, end_time, exe_crc, ini_crc, map_crc, map_name, seed, header_json, lifecycle_state, "
            "created_at, updated_at) VALUES (:public_id, :sha256, 'x', '1.04', 104, 1, 0, 1, 1, 1, 1, "
            "'map', 1, '{}', 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            {"public_id": PUBLIC_C, "sha256": SHA_B},
        )
        connection.execute(
            text(
                "INSERT INTO players (public_id, display_name, identity_revision, created_at, updated_at) "
                "VALUES (:public_id, 'x', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"public_id": PUBLIC_B},
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO players (public_id, display_name, identity_revision, created_at, updated_at) "
            "VALUES (:public_id, 'x', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            {"public_id": PUBLIC_B},
        )


@pytest.mark.parametrize(
    "malformed_uuid",
    [
        "0000000--0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-00000-000001",
        "00000000-0000-4000-8000-00000000000-",
    ],
)
def test_public_and_run_ids_reject_extra_hyphens(migrated_engine: object, malformed_uuid: str) -> None:
    """Catch UUID-shaped values whose fifth hyphen replaces a required hexadecimal digit."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        replay_id = _insert_replay(connection)
        _assert_integrity_error(
            connection,
            "INSERT INTO players (public_id, display_name, identity_revision, created_at, updated_at) "
            "VALUES (:public_id, 'x', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            {"public_id": malformed_uuid},
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
            "warnings_json, started_at) VALUES (:run_id, :replay, 'v1', 1, :sha, 'running', '[]', CURRENT_TIMESTAMP)",
            {"run_id": malformed_uuid, "replay": replay_id, "sha": SHA_B},
        )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        ".",
        "..",
        "./replay.rep",
        "../replay.rep",
        "replays/./replay.rep",
        "replays/../replay.rep",
        "replays/.",
        "replays/..",
        "replays\\replay.rep",
        "/replays/replay.rep",
        "C:/replays/replay.rep",
        "file:replays/replay.rep",
        "replays//replay.rep",
        "replays/",
    ],
)
def test_managed_asset_rejects_non_product_relative_posix_paths(
    migrated_engine: object, unsafe_path: str
) -> None:
    """Catch traversal, platform-specific, absolute, URI, and empty-segment asset paths."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        _assert_integrity_error(
            connection,
            "INSERT INTO managed_assets (public_id, sha256, kind, relative_path, size_bytes, created_at) "
            "VALUES ('00000000-0000-4000-8000-000000000001', :sha, 'replay', :path, 1, CURRENT_TIMESTAMP)",
            {"sha": SHA_A, "path": unsafe_path},
        )


def test_managed_asset_accepts_nested_product_relative_posix_paths(migrated_engine: object) -> None:
    """Keep legitimate nested package paths available at the database boundary."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        _insert_asset(connection, path="replays/sha256/ab/cd.rep")
        _insert_asset(
            connection,
            public_id=PUBLIC_B,
            sha256=SHA_B,
            path="maps/v1/manifests/map.json",
        )


def test_canonical_json_uses_stable_bytes_and_rejects_nonfinite_numbers() -> None:
    """Catch insertion-order-dependent cache inputs and nonstandard JSON numbers."""
    json_type = CanonicalJSON()
    first = json_type.process_bind_param({"z": [2, 1], "a": "ä"}, None)
    second = json_type.process_bind_param({"a": "ä", "z": [2, 1]}, None)
    assert first == second == '{"a":"ä","z":[2,1]}'
    assert json_type.process_result_value(first, None) == {"a": "ä", "z": [2, 1]}
    assert json_type.process_bind_param(None, None) is None
    assert json_type.process_result_value(None, None) is None
    assert utc_now().utcoffset() is not None
    for nonfinite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises((ValueError, StatementError)):
            json_type.process_bind_param({"value": nonfinite}, None)
    with pytest.raises(TypeError):
        json_type.process_bind_param({"value": object()}, None)


def test_status_feature_lease_and_job_edge_checks(migrated_engine: object) -> None:
    """Catch invalid enums, windows, typed feature values, confidence, leases, and dependency loops."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        replay_id = _insert_replay(connection)
        _assert_integrity_error(
            connection, "UPDATE replays SET lifecycle_state='mystery' WHERE id=:id", {"id": replay_id}
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, created_at) "
            "VALUES (:public_id, :replay, 'guessed', 'test', 'test:bad', 1, CURRENT_TIMESTAMP)",
            {"public_id": PUBLIC_C, "replay": replay_id},
        )
        parser = connection.execute(
            text(
                "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
                "warnings_json, started_at) VALUES (:run, :replay, 'v1', 1, :sha, 'pending', '[]', CURRENT_TIMESTAMP)"
            ),
            {"run": "10000000-0000-4000-8000-000000000001", "replay": replay_id, "sha": SHA_B},
        )
        parser_id = int(parser.lastrowid)
        _assert_integrity_error(connection, "UPDATE parser_runs SET status='done' WHERE id=:id", {"id": parser_id})

        feature_set = connection.execute(
            text(
                "INSERT INTO feature_sets (public_id, replay_id, extractor_name, extractor_version, input_digest, "
                "cache_key, status, settings_json, created_at) VALUES (:public_id, :replay, 'x', '1', :input, :cache, "
                "'running', '{}', CURRENT_TIMESTAMP)"
            ),
            {"public_id": PUBLIC_C, "replay": replay_id, "input": SHA_A, "cache": SHA_B},
        )
        feature_set_id = int(feature_set.lastrowid)
        feature_cases = (
            ("integer", "1", "2.0", 0, 10, 0.5),
            ("integer", "1", "NULL", 10, 5, 0.5),
            ("integer", "1", "NULL", 0, 10, 1.1),
        )
        for offset, (value_type, integer_value, real_value, frame_start, frame_end, confidence) in enumerate(
            feature_cases, start=4
        ):
            evidence_id = int(
                connection.execute(
                    text(
                        "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
                        "created_at) VALUES (:public_id, :replay, 'derived', 'feature', :source, 1, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "public_id": f"00000000-0000-4000-8000-{offset:012d}",
                        "replay": replay_id,
                        "source": f"feature:{offset}",
                    },
                ).lastrowid
            )
            _assert_integrity_error(
                connection,
                "INSERT INTO features (public_id, feature_set_id, evidence_item_id, name, value_type, integer_value, "
                "real_value, scope_type, scope_key, frame_start, frame_end, quality, confidence, details_json) VALUES "
                f"(:public_id, :set_id, :evidence, 'apm-{offset}', '{value_type}', {integer_value}, {real_value}, "
                "'replay', 'all', :frame_start, :frame_end, 'available', :confidence, '{}')",
                {
                    "public_id": f"00000000-0000-4000-8000-{offset + 10:012d}",
                    "set_id": feature_set_id,
                    "evidence": evidence_id,
                    "frame_start": frame_start,
                    "frame_end": frame_end,
                    "confidence": confidence,
                },
            )

        job_one = connection.execute(
            text(
                "INSERT INTO jobs (public_id, replay_id, stage, component_version, idempotency_key, status, priority, "
                "attempt_count, max_attempts, available_at, created_at, revision, input_json, retryable) VALUES "
                "('00000000-0000-4000-8000-000000000006', :replay, 'parse', '1', 'job:1', 'pending', 0, 0, 3, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, '{}', 1)"
            ),
            {"replay": replay_id},
        )
        job_id = int(job_one.lastrowid)
        _assert_integrity_error(connection, "UPDATE jobs SET status='running' WHERE id=:id", {"id": job_id})
        _assert_integrity_error(
            connection,
            "UPDATE jobs SET lease_owner='worker', lease_expires_at=CURRENT_TIMESTAMP WHERE id=:id",
            {"id": job_id},
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO job_dependencies (job_id, depends_on_job_id, created_at) VALUES (:id, :id, CURRENT_TIMESTAMP)",
            {"id": job_id},
        )


def test_uniqueness_and_lifecycle_quality_separation(migrated_engine: object) -> None:
    """Catch collapsed lifecycle/issues and retry rows that overwrite or duplicate successful identities."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        replay_id = _insert_replay(connection)
        player_id = int(
            connection.execute(
                text(
                    "INSERT INTO players (public_id, display_name, identity_revision, created_at, updated_at) "
                    "VALUES (:public_id, 'Alice', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"public_id": PUBLIC_C},
            ).lastrowid
        )
        connection.execute(
            text(
                "INSERT INTO player_aliases (public_id, player_id, namespace, normalized_name, original_name, created_at) "
                "VALUES ('00000000-0000-4000-8000-000000000004', :player, 'embedded', 'alice', 'Alice', CURRENT_TIMESTAMP)"
            ),
            {"player": player_id},
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO player_aliases (public_id, player_id, namespace, normalized_name, original_name, created_at) "
            "VALUES ('00000000-0000-4000-8000-000000000005', :player, 'embedded', 'alice', 'ALICE', CURRENT_TIMESTAMP)",
            {"player": player_id},
        )

        evidence = connection.execute(
            text(
                "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, created_at) "
                "VALUES ('00000000-0000-4000-8000-000000000006', :replay, 'observed', 'parser', 'parser:r:command:0', 1, CURRENT_TIMESTAMP)"
            ),
            {"replay": replay_id},
        )
        evidence_id = int(evidence.lastrowid)
        _assert_integrity_error(
            connection,
            "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, created_at) "
            "VALUES ('00000000-0000-4000-8000-000000000007', :replay, 'observed', 'parser', 'parser:r:command:0', 1, CURRENT_TIMESTAMP)",
            {"replay": replay_id},
        )

        connection.execute(
            text(
                "INSERT INTO replay_quality_issues (public_id, replay_id, evidence_item_id, stage, issue_code, severity, "
                "details_json, detected_at) VALUES ('00000000-0000-4000-8000-000000000008', :replay, :evidence, "
                "'parse', 'truncated', 'warning', '{}', CURRENT_TIMESTAMP)"
            ),
            {"replay": replay_id, "evidence": evidence_id},
        )
        connection.execute(text("UPDATE replays SET lifecycle_state='partial' WHERE id=:id"), {"id": replay_id})
        assert (
            connection.execute(text("SELECT lifecycle_state FROM replays WHERE id=:id"), {"id": replay_id}).scalar_one()
            == "partial"
        )
        assert (
            connection.execute(
                text("SELECT issue_code FROM replay_quality_issues WHERE replay_id=:id"), {"id": replay_id}
            ).scalar_one()
            == "truncated"
        )

        analysis_values = {
            "run": "20000000-0000-4000-8000-000000000001",
            "replay": replay_id,
            "digest": SHA_A,
            "cache": SHA_B,
        }
        statement = text(
            "INSERT INTO analysis_runs (run_id, replay_id, provider, model_name, model_digest, prompt_version, "
            "prompt_digest, response_schema_version, response_schema_digest, settings_digest, input_digest, cache_key, "
            "status, diagnostics_json, created_at) VALUES (:run, :replay, 'ollama', 'm', :digest, '1', :digest, '1', "
            ":digest, :digest, :digest, :cache, 'succeeded', '{}', CURRENT_TIMESTAMP)"
        )
        connection.execute(statement, analysis_values)
        _assert_integrity_error(
            connection, str(statement), {**analysis_values, "run": "20000000-0000-4000-8000-000000000002"}
        )

        connection.execute(
            text(
                "INSERT INTO jobs (public_id, stage, component_version, idempotency_key, status, priority, attempt_count, "
                "max_attempts, available_at, created_at, revision, input_json, retryable) VALUES "
                "('00000000-0000-4000-8000-000000000009', 'report', '1', 'same', 'pending', 0, 0, 3, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, '{}', 1)"
            )
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO jobs (public_id, stage, component_version, idempotency_key, status, priority, attempt_count, "
            "max_attempts, available_at, created_at, revision, input_json, retryable) VALUES "
            "('00000000-0000-4000-8000-000000000010', 'report', '1', 'same', 'pending', 0, 0, 3, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, '{}', 1)",
            {},
        )


def test_parser_command_and_telemetry_sequence_uniqueness(migrated_engine: object) -> None:
    """Catch duplicate observation positions inside one parser or telemetry run."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        replay_id = _insert_replay(connection)
        parser_id = int(
            connection.execute(
                text(
                    "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
                    "warnings_json, started_at) VALUES ('30000000-0000-4000-8000-000000000001', :replay, 'v1', 1, :sha, "
                    "'running', '[]', CURRENT_TIMESTAMP)"
                ),
                {"replay": replay_id, "sha": SHA_B},
            ).lastrowid
        )
        player_id = int(
            connection.execute(
                text(
                    "INSERT INTO replay_players (public_id, replay_id, parser_run_id, slot_index, slot_kind, observed_json) "
                    "VALUES ('00000000-0000-4000-8000-000000000020', :replay, :parser, 0, 'human', '{}')"
                ),
                {"replay": replay_id, "parser": parser_id},
            ).lastrowid
        )
        evidence_ids: list[int] = []
        for suffix in (21, 22, 23, 24):
            evidence_ids.append(
                int(
                    connection.execute(
                        text(
                            "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, "
                            "schema_version, created_at) VALUES (:public_id, :replay, 'observed', 'test', :source, 1, "
                            "CURRENT_TIMESTAMP)"
                        ),
                        {
                            "public_id": f"00000000-0000-4000-8000-{suffix:012d}",
                            "replay": replay_id,
                            "source": f"test:{suffix}",
                        },
                    ).lastrowid
                )
            )
        connection.execute(
            text(
                "INSERT INTO commands (parser_run_id, replay_id, replay_player_id, command_index, frame, player_index, "
                "message_type, start_offset, end_offset, arguments_json, evidence_item_id) VALUES "
                "(:parser, :replay, :player, 0, 1, 0, 1001, 10, 20, '{}', :evidence)"
            ),
            {"parser": parser_id, "replay": replay_id, "player": player_id, "evidence": evidence_ids[0]},
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO commands (parser_run_id, replay_id, replay_player_id, command_index, frame, player_index, "
            "message_type, start_offset, end_offset, arguments_json, evidence_item_id) VALUES "
            "(:parser, :replay, :player, 0, 2, 0, 1002, 20, 30, '{}', :evidence)",
            {"parser": parser_id, "replay": replay_id, "player": player_id, "evidence": evidence_ids[1]},
        )

        telemetry_id = int(
            connection.execute(
                text(
                    "INSERT INTO telemetry_runs (run_id, replay_id, schema_version, engine_build, settings_json, status, "
                    "runner_status, diagnostics_json, started_at) VALUES ('40000000-0000-4000-8000-000000000001', "
                    ":replay, 2, 'build', '{}', 'running', 'succeeded', '{}', CURRENT_TIMESTAMP)"
                ),
                {"replay": replay_id},
            ).lastrowid
        )
        connection.execute(
            text("UPDATE evidence_items SET telemetry_run_id=:run WHERE id IN (:first, :second)"),
            {"run": telemetry_id, "first": evidence_ids[2], "second": evidence_ids[3]},
        )
        connection.execute(
            text(
                "INSERT INTO telemetry_events (telemetry_run_id, sequence, frame, logic_time_seconds, schema_version, "
                "event_type, payload_json, raw_record_json, evidence_item_id) VALUES "
                "(:run, 0, 0, 0.0, 2, 'manifest', '{}', '{}', :evidence)"
            ),
            {"run": telemetry_id, "evidence": evidence_ids[2]},
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO telemetry_events (telemetry_run_id, sequence, frame, logic_time_seconds, schema_version, "
            "event_type, payload_json, raw_record_json, evidence_item_id) VALUES "
            "(:run, 0, 1, 0.03, 2, 'duplicate', '{}', '{}', :evidence)",
            {"run": telemetry_id, "evidence": evidence_ids[3]},
        )


def test_foreign_key_cascade_and_restrict_behavior(migrated_engine: object) -> None:
    """Catch ownership children that survive deletion and immutable assets that can be orphaned."""
    with migrated_engine.connect() as connection, connection.begin():  # type: ignore[attr-defined]
        asset_id = _insert_asset(connection)
        replay_id = _insert_replay(connection, managed_asset_id=asset_id)
        parser_id = int(
            connection.execute(
                text(
                    "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
                    "warnings_json, started_at) VALUES ('50000000-0000-4000-8000-000000000001', :replay, 'v1', 1, :sha, "
                    "'running', '[]', CURRENT_TIMESTAMP)"
                ),
                {"replay": replay_id, "sha": SHA_B},
            ).lastrowid
        )
        _assert_integrity_error(connection, "DELETE FROM managed_assets WHERE id=:id", {"id": asset_id})
        connection.execute(text("DELETE FROM replays WHERE id=:id"), {"id": replay_id})
        assert connection.execute(text("SELECT id FROM parser_runs WHERE id=:id"), {"id": parser_id}).first() is None
        connection.execute(text("DELETE FROM managed_assets WHERE id=:id"), {"id": asset_id})
