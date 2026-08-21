"""Raw SQL immutability tests for successful observation runs."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def observation_graph(migrated_engine: object) -> Iterator[tuple[Connection, dict[str, int]]]:
    connection = migrated_engine.connect()  # type: ignore[attr-defined]
    transaction = connection.begin()
    replay_id = int(
        connection.execute(
            text(
                "INSERT INTO replays (public_id, sha256, replay_name, version_string, version_number, frame_count, "
                "start_time, end_time, exe_crc, ini_crc, map_crc, map_name, seed, header_json, lifecycle_state, created_at, "
                "updated_at) VALUES ('00000000-0000-4000-8000-000000000001', :sha, 'r', '1.04', 104, 1, 0, 1, 1, 1, "
                "1, 'map', 1, '{}', 'parsed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"sha": SHA_A},
        ).lastrowid
    )
    parser_id = int(
        connection.execute(
            text(
                "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
                "warnings_json, started_at) VALUES ('10000000-0000-4000-8000-000000000001', :replay, 'v1', 1, :sha, "
                "'running', '[]', CURRENT_TIMESTAMP)"
            ),
            {"replay": replay_id, "sha": SHA_A},
        ).lastrowid
    )
    telemetry_id = int(
        connection.execute(
            text(
                "INSERT INTO telemetry_runs (run_id, replay_id, schema_version, engine_build, settings_json, status, "
                "runner_status, diagnostics_json, started_at) VALUES "
                "('20000000-0000-4000-8000-000000000001', :replay, 2, 'build', '{}', 'running', 'succeeded', '{}', CURRENT_TIMESTAMP)"
            ),
            {"replay": replay_id},
        ).lastrowid
    )
    parser_evidence = int(
        connection.execute(
            text(
                "INSERT INTO evidence_items (public_id, replay_id, parser_run_id, tier, source_kind, source_key, schema_version, "
                "created_at) VALUES ('00000000-0000-4000-8000-000000000002', :replay, :run, 'observed', 'parser', "
                "'parser:10000000-0000-4000-8000-000000000001:command:0', 1, CURRENT_TIMESTAMP)"
            ),
            {"replay": replay_id, "run": parser_id},
        ).lastrowid
    )
    telemetry_evidence = int(
        connection.execute(
            text(
                "INSERT INTO evidence_items (public_id, replay_id, telemetry_run_id, tier, source_kind, source_key, "
                "schema_version, created_at) VALUES ('00000000-0000-4000-8000-000000000003', :replay, :run, 'observed', "
                "'telemetry', 'telemetry:20000000-0000-4000-8000-000000000001:sequence:0', 2, CURRENT_TIMESTAMP)"
            ),
            {"replay": replay_id, "run": telemetry_id},
        ).lastrowid
    )
    replay_player_id = int(
        connection.execute(
            text(
                "INSERT INTO replay_players (public_id, replay_id, parser_run_id, slot_index, slot_kind, original_name, "
                "observed_json) VALUES ('00000000-0000-4000-8000-000000000004', :replay, :run, 0, 'human', 'Alice', '{}')"
            ),
            {"replay": replay_id, "run": parser_id},
        ).lastrowid
    )
    command_id = int(
        connection.execute(
            text(
                "INSERT INTO commands (parser_run_id, replay_id, replay_player_id, command_index, frame, player_index, "
                "message_type, start_offset, end_offset, arguments_json, evidence_item_id) VALUES "
                "(:run, :replay, :player, 0, 1, 0, 1001, 10, 20, '{}', :evidence)"
            ),
            {"run": parser_id, "replay": replay_id, "player": replay_player_id, "evidence": parser_evidence},
        ).lastrowid
    )
    telemetry_event_id = int(
        connection.execute(
            text(
                "INSERT INTO telemetry_events (telemetry_run_id, sequence, frame, logic_time_seconds, schema_version, "
                "event_type, payload_json, raw_record_json, evidence_item_id) VALUES "
                "(:run, 0, 0, 0.0, 2, 'manifest', '{}', '{}', :evidence)"
            ),
            {"run": telemetry_id, "evidence": telemetry_evidence},
        ).lastrowid
    )
    extra_event_ids: list[int] = []
    for sequence in range(1, 5):
        evidence_id = int(
            connection.execute(
                text(
                    "INSERT INTO evidence_items (public_id, replay_id, telemetry_run_id, tier, source_kind, source_key, "
                    "schema_version, created_at) VALUES (:public_id, :replay, :run, 'observed', 'telemetry', :source_key, "
                    "2, CURRENT_TIMESTAMP)"
                ),
                {
                    "public_id": f"00000000-0000-4000-8000-{sequence + 10:012d}",
                    "replay": replay_id,
                    "run": telemetry_id,
                    "source_key": f"telemetry:20000000-0000-4000-8000-000000000001:sequence:{sequence}",
                },
            ).lastrowid
        )
        extra_event_ids.append(
            int(
                connection.execute(
                    text(
                        "INSERT INTO telemetry_events (telemetry_run_id, sequence, frame, logic_time_seconds, "
                        "schema_version, event_type, payload_json, raw_record_json, evidence_item_id) VALUES "
                        "(:run, :sequence, :sequence, :seconds, 2, 'fixture', '{}', '{}', :evidence)"
                    ),
                    {
                        "run": telemetry_id,
                        "sequence": sequence,
                        "seconds": sequence / 30,
                        "evidence": evidence_id,
                    },
                ).lastrowid
            )
        )
    entity_id = int(
        connection.execute(
            text(
                "INSERT INTO entities (public_id, telemetry_run_id, replay_id, object_id, template_name, kind_of_flags_json, "
                "observed_json) VALUES ('00000000-0000-4000-8000-000000000005', :run, :replay, 1, 'Unit', '[]', '{}')"
            ),
            {"run": telemetry_id, "replay": replay_id},
        ).lastrowid
    )
    entity_sample_id = int(
        connection.execute(
            text(
                "INSERT INTO entity_samples (telemetry_run_id, entity_id, telemetry_event_id, sequence, frame, x, y, z, "
                "orientation, current_state, source, sample_reason, payload_json) VALUES "
                "(:run, :entity, :event, 0, 0, 1, 2, 3, 0, 'idle', 'telemetry', 'event', '{}')"
            ),
            {"run": telemetry_id, "entity": entity_id, "event": telemetry_event_id},
        ).lastrowid
    )
    production_id = int(
        connection.execute(
            text(
                "INSERT INTO production_events (telemetry_run_id, telemetry_event_id, replay_id, frame, event_type, "
                "item_kind, item_name, quantity, state, payload_json) VALUES "
                "(:run, :event, :replay, 1, 'queued', 'unit', 'Unit', 1, 'queued', '{}')"
            ),
            {"run": telemetry_id, "event": extra_event_ids[0], "replay": replay_id},
        ).lastrowid
    )
    economy_id = int(
        connection.execute(
            text(
                "INSERT INTO economy_events (telemetry_run_id, telemetry_event_id, replay_id, frame, event_type, "
                "payload_json) VALUES (:run, :event, :replay, 2, 'income', '{}')"
            ),
            {"run": telemetry_id, "event": extra_event_ids[1], "replay": replay_id},
        ).lastrowid
    )
    combat_id = int(
        connection.execute(
            text(
                "INSERT INTO combat_events (telemetry_run_id, telemetry_event_id, replay_id, frame, event_type, "
                "payload_json) VALUES (:run, :event, :replay, 3, 'damage', '{}')"
            ),
            {"run": telemetry_id, "event": extra_event_ids[2], "replay": replay_id},
        ).lastrowid
    )
    connection.execute(
        text("UPDATE parser_runs SET status='succeeded', completed_at=CURRENT_TIMESTAMP WHERE id=:id"),
        {"id": parser_id},
    )
    connection.execute(
        text("UPDATE telemetry_runs SET status='succeeded', completed_at=CURRENT_TIMESTAMP WHERE id=:id"),
        {"id": telemetry_id},
    )
    try:
        yield (
            connection,
            {
                "replay": replay_id,
                "parser": parser_id,
                "telemetry": telemetry_id,
                "parser_evidence": parser_evidence,
                "telemetry_evidence": telemetry_evidence,
                "replay_player": replay_player_id,
                "command": command_id,
                "telemetry_event": telemetry_event_id,
                "entity": entity_id,
                "entity_sample": entity_sample_id,
                "production": production_id,
                "economy": economy_id,
                "combat": combat_id,
                "unused_event": extra_event_ids[3],
            },
        )
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("statement", "key"),
    [
        ("UPDATE parser_runs SET parser_version='changed' WHERE id=:id", "parser"),
        ("DELETE FROM parser_runs WHERE id=:id", "parser"),
        ("UPDATE telemetry_runs SET engine_build='changed' WHERE id=:id", "telemetry"),
        ("DELETE FROM telemetry_runs WHERE id=:id", "telemetry"),
        ("UPDATE evidence_items SET source_key='changed' WHERE id=:id", "parser_evidence"),
        ("DELETE FROM evidence_items WHERE id=:id", "telemetry_evidence"),
        ("UPDATE replay_players SET original_name='rewritten' WHERE id=:id", "replay_player"),
        ("DELETE FROM replay_players WHERE id=:id", "replay_player"),
        ("UPDATE commands SET frame=frame+1 WHERE id=:id", "command"),
        ("DELETE FROM commands WHERE id=:id", "command"),
        ("UPDATE telemetry_events SET event_type='changed' WHERE id=:id", "telemetry_event"),
        ("DELETE FROM telemetry_events WHERE id=:id", "telemetry_event"),
        ("UPDATE entities SET template_name='changed' WHERE id=:id", "entity"),
        ("DELETE FROM entities WHERE id=:id", "entity"),
        ("UPDATE entity_samples SET frame=frame+1 WHERE id=:id", "entity_sample"),
        ("DELETE FROM entity_samples WHERE id=:id", "entity_sample"),
        ("UPDATE production_events SET frame=frame+1 WHERE id=:id", "production"),
        ("DELETE FROM production_events WHERE id=:id", "production"),
        ("UPDATE economy_events SET frame=frame+1 WHERE id=:id", "economy"),
        ("DELETE FROM economy_events WHERE id=:id", "economy"),
        ("UPDATE combat_events SET frame=frame+1 WHERE id=:id", "combat"),
        ("DELETE FROM combat_events WHERE id=:id", "combat"),
    ],
)
def test_successful_observations_reject_raw_update_and_delete(
    observation_graph: tuple[Connection, dict[str, int]], statement: str, key: str
) -> None:
    """Catch raw SQL attempts to reopen or rewrite a successful observation run."""
    connection, identities = observation_graph
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(text(statement), {"id": identities[key]})


def test_successful_observations_reject_raw_append(observation_graph: tuple[Connection, dict[str, int]]) -> None:
    """Catch late child/evidence insertion after a run has become successful."""
    connection, identities = observation_graph
    derived_ids: list[int] = []
    for suffix in (90, 91):
        derived_ids.append(
            int(
                connection.execute(
                    text(
                        "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
                        "created_at) VALUES (:public_id, :replay, 'derived', 'test', :source, 1, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "public_id": f"00000000-0000-4000-8000-{suffix:012d}",
                        "replay": identities["replay"],
                        "source": f"derived:{suffix}",
                    },
                ).lastrowid
            )
        )
    statements = [
        (
            (
                "INSERT INTO replay_players (public_id, replay_id, parser_run_id, slot_index, slot_kind, observed_json) "
                "VALUES ('00000000-0000-4000-8000-000000000098', :replay, :run, 1, 'human', '{}')"
            ),
            {"run": identities["parser"], "replay": identities["replay"]},
        ),
        (
            (
                "INSERT INTO commands (parser_run_id, replay_id, command_index, frame, player_index, message_type, "
                "start_offset, end_offset, arguments_json, evidence_item_id) VALUES "
                "(:run, :replay, 1, 2, 0, 1002, 20, 30, '{}', :evidence)"
            ),
            {"run": identities["parser"], "replay": identities["replay"], "evidence": derived_ids[0]},
        ),
        (
            (
                "INSERT INTO telemetry_events (telemetry_run_id, sequence, frame, logic_time_seconds, schema_version, "
                "event_type, payload_json, raw_record_json, evidence_item_id) VALUES "
                "(:run, 99, 99, 3.3, 2, 'late', '{}', '{}', :evidence)"
            ),
            {"run": identities["telemetry"], "evidence": derived_ids[1]},
        ),
        (
            (
                "INSERT INTO evidence_items (public_id, replay_id, parser_run_id, tier, source_kind, source_key, "
                "schema_version, created_at) VALUES ('00000000-0000-4000-8000-000000000099', :replay, :run, "
                "'observed', 'parser', 'late', 1, CURRENT_TIMESTAMP)"
            ),
            {"replay": identities["replay"], "run": identities["parser"]},
        ),
        (
            (
                "INSERT INTO entities (public_id, telemetry_run_id, replay_id, object_id, template_name, "
                "kind_of_flags_json, observed_json) VALUES ('00000000-0000-4000-8000-000000000092', :run, :replay, "
                "99, 'Late', '[]', '{}')"
            ),
            {"run": identities["telemetry"], "replay": identities["replay"]},
        ),
        (
            (
                "INSERT INTO entity_samples (telemetry_run_id, entity_id, telemetry_event_id, sequence, frame, x, y, z, "
                "orientation, current_state, source, sample_reason, payload_json) VALUES "
                "(:run, :entity, :event, 99, 99, 1, 2, 3, 0, 'late', 'telemetry', 'late', '{}')"
            ),
            {"run": identities["telemetry"], "entity": identities["entity"], "event": identities["unused_event"]},
        ),
        (
            (
                "INSERT INTO production_events (telemetry_run_id, telemetry_event_id, replay_id, frame, event_type, "
                "item_kind, item_name, quantity, state, payload_json) VALUES "
                "(:run, :event, :replay, 99, 'late', 'unit', 'Late', 1, 'late', '{}')"
            ),
            {"run": identities["telemetry"], "event": identities["unused_event"], "replay": identities["replay"]},
        ),
        (
            (
                "INSERT INTO economy_events (telemetry_run_id, telemetry_event_id, replay_id, frame, event_type, "
                "payload_json) VALUES (:run, :event, :replay, 99, 'late', '{}')"
            ),
            {"run": identities["telemetry"], "event": identities["unused_event"], "replay": identities["replay"]},
        ),
        (
            (
                "INSERT INTO combat_events (telemetry_run_id, telemetry_event_id, replay_id, frame, event_type, "
                "payload_json) VALUES (:run, :event, :replay, 99, 'late', '{}')"
            ),
            {"run": identities["telemetry"], "event": identities["unused_event"], "replay": identities["replay"]},
        ),
    ]
    for statement, values in statements:
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text(statement), values)


def test_identity_seam_and_new_derived_evidence_remain_mutable(
    observation_graph: tuple[Connection, dict[str, int]],
) -> None:
    """Catch triggers that overreach into audited identity linkage or replaceable derived rows."""
    connection, identities = observation_graph
    player_id = int(
        connection.execute(
            text(
                "INSERT INTO players (public_id, display_name, identity_revision, created_at, updated_at) VALUES "
                "('00000000-0000-4000-8000-000000000010', 'Alice', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        ).lastrowid
    )
    connection.execute(
        text("UPDATE replay_players SET player_id=:player WHERE id=:id"),
        {"player": player_id, "id": identities["replay_player"]},
    )
    connection.execute(
        text(
                "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, created_at) "
                "VALUES ('00000000-0000-4000-8000-000000000080', :replay, 'derived', 'feature', 'feature:new', 1, CURRENT_TIMESTAMP)"
        ),
        {"replay": identities["replay"]},
    )


def test_failed_attempt_can_precede_distinct_successful_run(migrated_engine: object) -> None:
    """Catch uniqueness rules that erase failed parser history or prevent a clean retry."""
    with migrated_engine.begin() as connection:  # type: ignore[attr-defined]
        replay_id = int(
            connection.execute(
                text(
                    "INSERT INTO replays (public_id, sha256, replay_name, version_string, version_number, frame_count, "
                    "start_time, end_time, exe_crc, ini_crc, map_crc, map_name, seed, header_json, lifecycle_state, created_at, "
                    "updated_at) VALUES ('00000000-0000-4000-8000-000000000020', :sha, 'r', '1.04', 104, 1, 0, 1, 1, 1, "
                    "1, 'map', 1, '{}', 'parsed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"sha": SHA_B},
            ).lastrowid
        )
        values = {"replay": replay_id, "sha": SHA_B}
        connection.execute(
            text(
                "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
                "warnings_json, started_at) VALUES ('10000000-0000-4000-8000-000000000020', :replay, 'v1', 1, :sha, "
                "'failed', '[]', CURRENT_TIMESTAMP)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO parser_runs (run_id, replay_id, parser_version, schema_version, input_sha256, status, "
                "warnings_json, started_at) VALUES ('10000000-0000-4000-8000-000000000021', :replay, 'v1', 1, :sha, "
                "'succeeded', '[]', CURRENT_TIMESTAMP)"
            ),
            values,
        )
