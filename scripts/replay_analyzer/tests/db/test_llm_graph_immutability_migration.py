"""Database enforcement for succeeded local-LLM ownership graphs."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from generals_replay_analyzer.db import create_database_engine, upgrade_database


@pytest.fixture
def succeeded_llm_graph(database_path: Path) -> Iterator[tuple[Connection, dict[str, int]]]:
    upgrade_database(database_path)
    engine = create_database_engine(database_path)
    connection = engine.connect()
    transaction = connection.begin()
    replay_id = int(
        connection.execute(
            text(
                "INSERT INTO replays (public_id, sha256, replay_name, version_string, version_number, frame_count, "
                "start_time, end_time, exe_crc, ini_crc, map_crc, map_name, seed, header_json, lifecycle_state, "
                "created_at, updated_at) VALUES ('00000000-0000-4000-8000-000000000501', :sha, 'fixture.rep', "
                "'1.04', 104, 900, 1, 2, 1, 2, 3, 'map', 4, '{}', 'engine_verified', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ),
            {"sha": "a" * 64},
        ).lastrowid
    )
    asset_id = int(
        connection.execute(
            text(
                "INSERT INTO managed_assets (public_id, sha256, kind, relative_path, size_bytes, media_type, "
                "created_at) VALUES ('00000000-0000-4000-8000-000000000502', :sha, 'analysis_raw_response', "
                "'cache/llm/response', 2, 'application/json', CURRENT_TIMESTAMP)"
            ),
            {"sha": "b" * 64},
        ).lastrowid
    )
    unrelated_asset_id = int(
        connection.execute(
            text(
                "INSERT INTO managed_assets (public_id, sha256, kind, relative_path, size_bytes, media_type, "
                "created_at) VALUES ('00000000-0000-4000-8000-000000000514', :sha, 'report', "
                "'reports/unrelated', 1, 'text/plain', CURRENT_TIMESTAMP)"
            ),
            {"sha": "9" * 64},
        ).lastrowid
    )
    run_id = int(
        connection.execute(
            text(
                "INSERT INTO analysis_runs (run_id, replay_id, provider, model_name, model_digest, prompt_version, "
                "prompt_digest, response_schema_version, response_schema_digest, settings_digest, input_digest, "
                "cache_key, status, raw_response_asset_id, validated_response_json, diagnostics_json, created_at) "
                "VALUES ('00000000-0000-4000-8000-000000000503', :replay, 'ollama', 'model', :model, 'prompt-v1', "
                ":prompt, 'schema-v1', :schema, :settings, :input, :cache, 'running', :asset, '{}', "
                "'{\"code\":\"running\"}', CURRENT_TIMESTAMP)"
            ),
            {
                "replay": replay_id,
                "asset": asset_id,
                "model": "c" * 64,
                "prompt": "d" * 64,
                "schema": "e" * 64,
                "settings": "f" * 64,
                "input": "1" * 64,
                "cache": "2" * 64,
            },
        ).lastrowid
    )
    evidence_ids: dict[str, int] = {}
    for suffix, tier, source_kind, source_key in (
        (504, "observed", "telemetry", "event:opening"),
        (505, "observed", "telemetry", "event:reserve"),
        (506, "inferred", "llm", "analysis-run:00000000-0000-4000-8000-000000000503:claim-1"),
        (507, "inferred", "llm", "analysis-run:00000000-0000-4000-8000-000000000503:claim-2"),
        (508, "derived", "rule", "rule:unrelated"),
        (512, "derived", "rule", "rule:pre-success-manual"),
    ):
        evidence_ids[str(suffix)] = int(
            connection.execute(
                text(
                    "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
                    "created_at) VALUES (:public_id, :replay, :tier, :source_kind, :source_key, 1, CURRENT_TIMESTAMP)"
                ),
                {
                    "public_id": f"00000000-0000-4000-8000-{suffix:012d}",
                    "replay": replay_id,
                    "tier": tier,
                    "source_kind": source_kind,
                    "source_key": source_key,
                },
            ).lastrowid
        )
    assessment_id = int(
        connection.execute(
            text(
                "INSERT INTO strategy_assessments (public_id, evidence_item_id, replay_id, analysis_run_id, method, "
                "strategy_label, phase, model_version, frame_start, frame_end, quality, confidence, details_json, "
                "created_at) VALUES ('00000000-0000-4000-8000-000000000509', :evidence, :replay, :run, 'llm', "
                "'oil_grab', 'opening', :model, 0, 900, 'available', 0.8, '{}', CURRENT_TIMESTAMP)"
            ),
            {"evidence": evidence_ids["506"], "replay": replay_id, "run": run_id, "model": "c" * 64},
        ).lastrowid
    )
    connection.execute(
        text(
            "INSERT INTO assessment_evidence (assessment_id, evidence_item_id, role) "
            "VALUES (:assessment, :evidence, 'supporting')"
        ),
        {"assessment": assessment_id, "evidence": evidence_ids["504"]},
    )
    bound_manual_id = int(
        connection.execute(
            text(
                "INSERT INTO strategy_assessments (public_id, evidence_item_id, replay_id, analysis_run_id, method, "
                "strategy_label, phase, frame_start, frame_end, quality, details_json, created_at) VALUES "
                "('00000000-0000-4000-8000-000000000513', :evidence, :replay, :run, 'manual', 'manual-bound', "
                "'opening', 0, 1, 'available', '{}', CURRENT_TIMESTAMP)"
            ),
            {
                "evidence": evidence_ids["512"],
                "replay": replay_id,
                "run": run_id,
            },
        ).lastrowid
    )
    connection.execute(
        text(
            "INSERT INTO assessment_evidence (assessment_id, evidence_item_id, role) "
            "VALUES (:assessment, :evidence, 'supporting')"
        ),
        {"assessment": bound_manual_id, "evidence": evidence_ids["505"]},
    )
    connection.execute(
        text(
            "UPDATE analysis_runs SET status='succeeded', diagnostics_json='{\"code\":\"ok\"}', "
            "completed_at=CURRENT_TIMESTAMP WHERE id=:run"
        ),
        {"run": run_id},
    )
    try:
        yield connection, {
            "replay": replay_id,
            "asset": asset_id,
            "unrelated_asset": unrelated_asset_id,
            "run": run_id,
            "assessment": assessment_id,
            "bound_manual": bound_manual_id,
            "citation": evidence_ids["504"],
            "reserve_citation": evidence_ids["505"],
            "inferred": evidence_ids["506"],
            "reserve_inferred": evidence_ids["507"],
            "unrelated": evidence_ids["508"],
        }
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE analysis_runs SET raw_response_asset_id=NULL WHERE id=:run", {}),
        ("DELETE FROM analysis_runs WHERE id=:run", {}),
        ("DELETE FROM managed_assets WHERE id=:asset", {}),
        ("UPDATE evidence_items SET source_key='poison' WHERE id=:inferred", {}),
        ("DELETE FROM evidence_items WHERE id=:inferred", {}),
        ("UPDATE strategy_assessments SET strategy_label='poison' WHERE id=:assessment", {}),
        ("DELETE FROM strategy_assessments WHERE id=:assessment", {}),
        (
            (
                "INSERT INTO assessment_evidence (assessment_id, evidence_item_id, role) "
                "VALUES (:assessment, :reserve_citation, 'supporting')"
            ),
            {},
        ),
        (
            (
                "UPDATE assessment_evidence SET role='contradicting' "
                "WHERE assessment_id=:assessment AND evidence_item_id=:citation"
            ),
            {},
        ),
        (
            "DELETE FROM assessment_evidence WHERE assessment_id=:assessment AND evidence_item_id=:citation",
            {},
        ),
        ("UPDATE evidence_items SET source_kind='poison' WHERE id=:citation", {}),
        ("DELETE FROM evidence_items WHERE id=:citation", {}),
        (
            (
                "INSERT INTO strategy_assessments (public_id, evidence_item_id, replay_id, analysis_run_id, method, "
                "strategy_label, phase, model_version, frame_start, frame_end, quality, details_json, created_at) VALUES "
                "('00000000-0000-4000-8000-000000000510', :reserve_inferred, :replay, :run, 'llm', 'late', "
                "'opening', :model, 0, 1, 'available', '{}', CURRENT_TIMESTAMP)"
            ),
            {"model": "c" * 64},
        ),
    ],
)
def test_succeeded_llm_graph_rejects_raw_mutation_append_and_delete(
    succeeded_llm_graph: tuple[Connection, dict[str, int]],
    statement: str,
    parameters: dict[str, object],
) -> None:
    connection, identities = succeeded_llm_graph
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(text(statement), identities | parameters)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE strategy_assessments SET strategy_label='poison' WHERE id=:bound_manual",
        "DELETE FROM strategy_assessments WHERE id=:bound_manual",
        (
            "INSERT INTO strategy_assessments (public_id, evidence_item_id, replay_id, analysis_run_id, method, "
            "strategy_label, phase, frame_start, frame_end, quality, details_json, created_at) VALUES "
            "('00000000-0000-4000-8000-000000000515', :reserve_inferred, :replay, :run, 'manual', 'late-manual', "
            "'opening', 0, 1, 'available', '{}', CURRENT_TIMESTAMP)"
        ),
    ],
)
def test_succeeded_run_rejects_assessment_old_and_new_sides_regardless_of_method(
    succeeded_llm_graph: tuple[Connection, dict[str, int]],
    statement: str,
) -> None:
    connection, identities = succeeded_llm_graph
    with pytest.raises(IntegrityError, match="succeeded llm analysis graph is immutable"), connection.begin_nested():
        connection.execute(text(statement), identities)


@pytest.mark.parametrize(
    "statement",
    [
        (
            "INSERT INTO assessment_evidence (assessment_id, evidence_item_id, role) "
            "VALUES (:bound_manual, :citation, 'supporting')"
        ),
        (
            "UPDATE assessment_evidence SET role='contradicting' "
            "WHERE assessment_id=:bound_manual AND evidence_item_id=:reserve_citation"
        ),
        (
            "DELETE FROM assessment_evidence "
            "WHERE assessment_id=:bound_manual AND evidence_item_id=:reserve_citation"
        ),
        "UPDATE evidence_items SET source_key='poison' WHERE id=:reserve_citation",
        "DELETE FROM evidence_items WHERE id=:reserve_citation",
    ],
)
def test_succeeded_run_rejects_method_label_bypass_of_links_and_cited_evidence(
    succeeded_llm_graph: tuple[Connection, dict[str, int]],
    statement: str,
) -> None:
    connection, identities = succeeded_llm_graph
    with pytest.raises(IntegrityError, match="succeeded llm analysis graph is immutable"), connection.begin_nested():
        connection.execute(text(statement), identities)


@pytest.mark.parametrize(
    "statement",
    [
        (
            "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
            "created_at) VALUES ('00000000-0000-4000-8000-000000000516', :replay, 'inferred', 'llm', "
            "'analysis-run:00000000-0000-4000-8000-000000000503:late-claim', 1, CURRENT_TIMESTAMP)"
        ),
        (
            "UPDATE evidence_items SET tier='inferred', source_kind='llm', "
            "source_key='analysis-run:00000000-0000-4000-8000-000000000503:retagged' WHERE id=:unrelated"
        ),
    ],
)
def test_succeeded_run_rejects_new_evidence_in_its_inferred_namespace(
    succeeded_llm_graph: tuple[Connection, dict[str, int]],
    statement: str,
) -> None:
    connection, identities = succeeded_llm_graph
    with pytest.raises(IntegrityError, match="succeeded llm analysis graph is immutable"), connection.begin_nested():
        connection.execute(text(statement), identities)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE managed_assets SET relative_path='poison' WHERE id=:asset",
        "DELETE FROM managed_assets WHERE id=:asset",
    ],
)
def test_succeeded_run_rejects_raw_asset_update_and_delete_at_ownership_boundary(
    succeeded_llm_graph: tuple[Connection, dict[str, int]],
    statement: str,
) -> None:
    connection, identities = succeeded_llm_graph
    with pytest.raises(IntegrityError, match="succeeded llm analysis graph is immutable"), connection.begin_nested():
        connection.execute(text(statement), identities)


def test_pre_success_construction_and_unrelated_manual_rows_remain_mutable(
    succeeded_llm_graph: tuple[Connection, dict[str, int]],
) -> None:
    connection, identities = succeeded_llm_graph
    manual_id = int(
        connection.execute(
            text(
                "INSERT INTO strategy_assessments (public_id, evidence_item_id, replay_id, method, strategy_label, "
                "phase, frame_start, frame_end, quality, details_json, created_at) VALUES "
                "('00000000-0000-4000-8000-000000000511', :evidence, :replay, 'manual', 'manual', 'opening', "
                "0, 1, 'available', '{}', CURRENT_TIMESTAMP)"
            ),
            {"evidence": identities["unrelated"], "replay": identities["replay"]},
        ).lastrowid
    )
    connection.execute(
        text(
            "INSERT INTO assessment_evidence (assessment_id, evidence_item_id, role) "
            "VALUES (:assessment, :evidence, 'supporting')"
        ),
        {"assessment": manual_id, "evidence": identities["unrelated"]},
    )
    connection.execute(
        text("UPDATE evidence_items SET source_key='rule:changed' WHERE id=:evidence"),
        {"evidence": identities["unrelated"]},
    )
    connection.execute(
        text("DELETE FROM assessment_evidence WHERE assessment_id=:assessment"),
        {"assessment": manual_id},
    )
    connection.execute(text("DELETE FROM strategy_assessments WHERE id=:assessment"), {"assessment": manual_id})
    assert connection.scalar(
        text("SELECT strategy_label FROM strategy_assessments WHERE id=:assessment"),
        {"assessment": identities["bound_manual"]},
    ) == "manual-bound"
    connection.execute(
        text("UPDATE managed_assets SET relative_path='reports/changed' WHERE id=:asset"),
        {"asset": identities["unrelated_asset"]},
    )
    connection.execute(
        text("DELETE FROM managed_assets WHERE id=:asset"),
        {"asset": identities["unrelated_asset"]},
    )
    unrelated_namespace_id = int(
        connection.execute(
            text(
                "INSERT INTO evidence_items (public_id, replay_id, tier, source_kind, source_key, schema_version, "
                "created_at) VALUES ('00000000-0000-4000-8000-000000000517', :replay, 'inferred', 'llm', "
                "'analysis-run:00000000-0000-4000-8000-000000000999:unrelated', 1, CURRENT_TIMESTAMP)"
            ),
            {"replay": identities["replay"]},
        ).lastrowid
    )
    connection.execute(
        text("UPDATE evidence_items SET source_key=source_key || '-changed' WHERE id=:evidence"),
        {"evidence": unrelated_namespace_id},
    )
    connection.execute(text("DELETE FROM evidence_items WHERE id=:evidence"), {"evidence": unrelated_namespace_id})
