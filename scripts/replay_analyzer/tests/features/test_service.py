"""Feature context materialization, cache, race, and rollback tests."""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    Entity,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ParserRun,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureScope,
    FeatureValue,
    FeatureWindow,
    validate_feature_value,
)
from generals_replay_analyzer.features.build_order import BuildOrderExtractor
from generals_replay_analyzer.features.context import FeatureContext, cache_key, canonical_json, input_digest
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence, thaw_canonical
from generals_replay_analyzer.features.registry import (
    REGISTRY_SCHEMA,
    FeatureDefinition,
    FeatureRegistry,
)
from generals_replay_analyzer.features.service import (
    ExtractFeaturesRequest,
    FeatureExtractionError,
    FeatureExtractionService,
)


@pytest.fixture
def feature_engine(tmp_path: Path) -> Engine:
    database = tmp_path / "external-data-root" / "library.sqlite3"
    database.parent.mkdir()
    upgrade_database(database)
    engine = create_database_engine(database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def feature_factory(feature_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(feature_engine)


def _seed_replay(
    factory: sessionmaker[Session],
    *,
    replay_public_id: str = "00000000-0000-4000-8000-000000000301",
    replay_sha256: str = "a" * 64,
    replay_player_public_id: str = "00000000-0000-4000-8000-000000000302",
    parser_run_id: str = "00000000-0000-4000-8000-000000000303",
    telemetry_run_id: str = "00000000-0000-4000-8000-000000000304",
) -> tuple[str, str, tuple[str, ...]]:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    player_index = 0
    events = (
        (
            10,
            "object_created",
            {"object_id": 7, "owner_player_index": player_index, "template_name": "FabricatedObjectTemplate"},
        ),
        (
            20,
            "construction_completed",
            {"object_id": 7, "owner_player_index": player_index, "template_name": "FabricatedCompletionTemplate"},
        ),
        (
            120,
            "complete",
            {
                "final_frame": 120,
                "terminal_reason": "clean_completion",
                "final_cash_balances": [{"player_index": player_index, "has_money": True, "balance": 500}],
            },
        ),
    )
    with factory() as session:
        replay = Replay(
            public_id=replay_public_id,
            sha256=replay_sha256,
            replay_name="fixture",
            version_string="1.04",
            version_number=1,
            frame_count=120,
            start_time=0,
            end_time=120,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="fixture.map",
            seed=4,
            header_json={},
            lifecycle_state="engine_verified",
            updated_at=now,
            created_at=now,
        )
        session.add(replay)
        session.flush()
        parser = ParserRun(
            run_id=parser_run_id,
            replay_id=replay.id,
            parser_version="parser-v1",
            schema_version=1,
            input_sha256=replay_sha256,
            status="running",
            warnings_json=[],
            started_at=now,
        )
        session.add(parser)
        session.flush()
        replay_player = ReplayPlayer(
            public_id=replay_player_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            slot_index=0,
            slot_kind="human",
            original_name="Player",
            normalized_name="player",
            player_index=player_index,
            observed_json={"player_index": player_index},
        )
        session.add(replay_player)
        telemetry = TelemetryRun(
            run_id=telemetry_run_id,
            replay_id=replay.id,
            schema_version=2,
            engine_build="fixture",
            settings_json={},
            status="running",
            runner_status="succeeded",
            diagnostics_json={},
            started_at=now,
        )
        session.add(telemetry)
        session.flush()
        session.add(
            Entity(
                public_id=str(uuid5(NAMESPACE_URL, f"{telemetry_run_id}:entity:7")),
                telemetry_run_id=telemetry.id,
                replay_id=replay.id,
                object_id=7,
                template_name="ChinaPowerPlant",
                initial_owner_player_index=player_index,
                kind_of_flags_json=["STRUCTURE"],
                creation_sequence=0,
                creation_frame=10,
                observed_json={"source": "object_created"},
            )
        )
        evidence_ids: list[str] = []
        for sequence, (frame, event_type, payload) in enumerate(events):
            public_id = str(uuid5(NAMESPACE_URL, f"{telemetry_run_id}:{sequence}"))
            evidence = EvidenceItem(
                public_id=public_id,
                replay_id=replay.id,
                telemetry_run_id=telemetry.id,
                tier="observed",
                source_kind="telemetry",
                source_key=f"telemetry:{telemetry_run_id}:sequence:{sequence}",
                schema_version=2,
                created_at=now,
            )
            session.add(evidence)
            session.flush()
            session.add(
                TelemetryEvent(
                    telemetry_run_id=telemetry.id,
                    sequence=sequence,
                    frame=frame,
                    logic_time_seconds=frame / 30.0,
                    schema_version=2,
                    event_type=event_type,
                    payload_json=payload,
                    raw_record_json={"event_type": event_type, "payload": payload},
                    evidence_item_id=evidence.id,
                )
            )
            evidence_ids.append(public_id)
        parser_evidence_public_id = str(uuid5(NAMESPACE_URL, f"{parser_run_id}:command:0"))
        parser_evidence = EvidenceItem(
            public_id=parser_evidence_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            tier="observed",
            source_kind="parser",
            source_key=f"parser:{parser_run_id}:command:0",
            schema_version=1,
            created_at=now,
        )
        session.add(parser_evidence)
        session.flush()
        session.add(
            ReplayCommand(
                parser_run_id=parser.id,
                replay_id=replay.id,
                replay_player_id=replay_player.id,
                command_index=0,
                frame=5,
                player_index=player_index,
                message_type=1074,
                message_name="MSG_DO_STOP",
                start_offset=1,
                end_offset=2,
                arguments_json=[],
                evidence_item_id=parser_evidence.id,
            )
        )
        evidence_ids.append(parser_evidence_public_id)
        orphan_public_id = str(uuid5(NAMESPACE_URL, f"{telemetry_run_id}:orphan"))
        session.add(
            EvidenceItem(
                public_id=orphan_public_id,
                replay_id=replay.id,
                telemetry_run_id=telemetry.id,
                tier="observed",
                source_kind="telemetry",
                source_key=f"telemetry:{telemetry_run_id}:orphan",
                schema_version=2,
                created_at=now,
            )
        )
        evidence_ids.append(orphan_public_id)
        session.commit()
        parser.status = "succeeded"
        parser.completion_status = "complete"
        parser.result_sha256 = "b" * 64
        parser.completed_at = now
        telemetry.status = "succeeded"
        telemetry.final_frame = 120
        telemetry.command_count = 0
        telemetry.trace_sha256 = "c" * 64
        telemetry.completed_at = now
        session.commit()
    return replay_public_id, replay_player_public_id, tuple(evidence_ids)


def _request(replay: str, player: str, *extractors: str, settings: object = ()) -> ExtractFeaturesRequest:
    return ExtractFeaturesRequest(replay, player, tuple(extractors), settings)  # type: ignore[arg-type]


def test_service_reuses_immutable_success_and_persists_direct_same_replay_links(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, evidence_ids = _seed_replay(feature_factory)
    service = FeatureExtractionService(feature_factory)
    first = service.extract(_request(replay, player, "build"))[0]
    second = service.extract(_request(replay, player, "build"))[0]
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.feature_set_public_id == first.feature_set_public_id
    assert second.cache_key == first.cache_key
    assert tuple(value.name for value in second.features) == tuple(sorted(value.name for value in second.features))
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet).where(FeatureSet.status == "succeeded")) == 1
        assert session.scalar(select(func.count()).select_from(Feature)) == 3
        linked = session.execute(
            select(EvidenceItem.public_id, EvidenceItem.replay_id)
            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .join(Feature, Feature.id == FeatureEvidence.feature_id)
        ).all()
        replay_id = session.scalar(select(Replay.id).where(Replay.public_id == replay))
        assert {public_id for public_id, _ in linked} <= set(evidence_ids)
        assert all(link_replay_id == replay_id for _, link_replay_id in linked)


def test_build_template_uses_and_links_authoritative_object_creation_evidence(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    receipt = FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))[0]
    sequence = next(value for value in receipt.features if value.name == "build.completed_sequence")
    assert sequence.raw_value == ((("frame", 20), ("template_name", "ChinaPowerPlant")),)
    assert {ref.source_key for ref in sequence.input_evidence} == {
        "telemetry:00000000-0000-4000-8000-000000000304:sequence:0",
        "telemetry:00000000-0000-4000-8000-000000000304:sequence:1",
    }
    with feature_factory() as session:
        persisted = session.scalar(select(Feature).where(Feature.name == "build.completed_sequence"))
        assert persisted is not None and persisted.json_value == [{"frame": 20, "template_name": "ChinaPowerPlant"}]
        linked = session.scalars(
            select(EvidenceItem.source_key)
            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .where(FeatureEvidence.feature_id == persisted.id)
        ).all()
        assert set(linked) == {
            "telemetry:00000000-0000-4000-8000-000000000304:sequence:0",
            "telemetry:00000000-0000-4000-8000-000000000304:sequence:1",
        }


@pytest.mark.parametrize("source_kind", ["parser", "telemetry"])
@pytest.mark.parametrize("defect", ["tier", "replay", "run"])
def test_context_boundary_rejects_malformed_persisted_evidence_links(
    feature_factory: sessionmaker[Session],
    feature_engine: Engine,
    source_kind: str,
    defect: str,
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    other_replay, _, _ = _seed_replay(
        feature_factory,
        replay_public_id="00000000-0000-4000-8000-000000000311",
        replay_sha256="d" * 64,
        replay_player_public_id="00000000-0000-4000-8000-000000000312",
        parser_run_id="00000000-0000-4000-8000-000000000313",
        telemetry_run_id="00000000-0000-4000-8000-000000000314",
    )
    with feature_engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_evidence_items_observed_no_update"))
        evidence_id = connection.execute(
            text(
                "SELECT id FROM evidence_items WHERE source_kind = :source_kind AND replay_id = "
                "(SELECT id FROM replays WHERE public_id = :replay) ORDER BY id LIMIT 1"
            ),
            {"source_kind": source_kind, "replay": replay},
        ).scalar_one()
        if defect == "tier":
            connection.execute(
                text("UPDATE evidence_items SET tier = 'derived' WHERE id = :id"), {"id": evidence_id}
            )
        elif defect == "replay":
            connection.execute(
                text(
                    "UPDATE evidence_items SET replay_id = (SELECT id FROM replays WHERE public_id = :other) "
                    "WHERE id = :id"
                ),
                {"other": other_replay, "id": evidence_id},
            )
        else:
            run_column = "parser_run_id" if source_kind == "parser" else "telemetry_run_id"
            run_table = "parser_runs" if source_kind == "parser" else "telemetry_runs"
            connection.execute(
                text(
                    f"UPDATE evidence_items SET {run_column} = "
                    f"(SELECT id FROM {run_table} WHERE replay_id = "
                    "(SELECT id FROM replays WHERE public_id = :other)) WHERE id = :id"
                ),
                {"other": other_replay, "id": evidence_id},
            )
    with pytest.raises(FeatureExtractionError, match="malformed persisted evidence"):
        FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))


def test_cache_changes_for_settings_and_extractor_version_without_mutating_old_sets(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    original = FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))[0]
    changed_settings = FeatureExtractionService(feature_factory).extract(
        _request(replay, player, "build", settings={"policy": "strict"})
    )[0]

    class BuildV2(BuildOrderExtractor):
        version = "build-v2"

    changed_version = FeatureExtractionService(feature_factory, extractors=(BuildV2(),)).extract(
        _request(replay, player, "build")
    )[0]
    assert len({original.cache_key, changed_settings.cache_key, changed_version.cache_key}) == 3
    with feature_factory() as session:
        sets = session.scalars(select(FeatureSet).where(FeatureSet.status == "succeeded")).all()
        assert len(sets) == 3
        first = session.scalar(select(FeatureSet).where(FeatureSet.public_id == original.feature_set_public_id))
        assert first is not None and first.cache_key == original.cache_key and first.status == "succeeded"


def test_competing_misses_return_one_winner(feature_factory: sessionmaker[Session]) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    request = _request(replay, player, "build")
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda _: FeatureExtractionService(feature_factory).extract(request)[0], range(2)))
    assert len({receipt.feature_set_public_id for receipt in receipts}) == 1
    assert sorted(receipt.cache_hit for receipt in receipts) == [False, True]
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet).where(FeatureSet.status == "succeeded")) == 1


def test_persistence_failure_rolls_back_all_children_and_retains_typed_failure(
    feature_factory: sessionmaker[Session], feature_engine: Engine
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    with feature_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER task6_fail_feature BEFORE INSERT ON features "
                "WHEN NEW.name = 'build.completed_sequence' BEGIN SELECT RAISE(ABORT, 'forced task6 failure'); END"
            )
        )
    with pytest.raises(FeatureExtractionError, match="persist"):
        FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0
        assert session.scalar(select(func.count()).select_from(EvidenceItem).where(EvidenceItem.tier == "derived")) == 0
        failed = session.scalars(select(FeatureSet).where(FeatureSet.status == "failed")).all()
        assert len(failed) == 1 and failed[0].error_json["code"] == "feature_persistence_failed"


def test_cross_replay_or_inferred_input_is_rejected_without_persisted_children(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    other_replay, _, other_evidence = _seed_replay(
        feature_factory,
        replay_public_id="00000000-0000-4000-8000-000000000311",
        replay_sha256="d" * 64,
        replay_player_public_id="00000000-0000-4000-8000-000000000312",
        parser_run_id="00000000-0000-4000-8000-000000000313",
        telemetry_run_id="00000000-0000-4000-8000-000000000314",
    )
    assert other_replay != replay

    class BadExtractor:
        name = "bad"
        version = "bad-v1"
        feature_names = ("build.completed_count",)

        def __init__(self, tier: str) -> None:
            self._tier = tier

        def extract(self, context: object) -> FeatureBundle:
            ref = EvidenceRef(other_evidence[0], cast(object, self._tier), "telemetry", "foreign", "telemetry-v2")  # type: ignore[arg-type]
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (ref,),
                    ),
                ),
            )

    for tier in ("observed", "inferred"):
        with pytest.raises(FeatureExtractionError):
            FeatureExtractionService(feature_factory, extractors=(BadExtractor(tier),)).extract(
                _request(replay, player, "bad")
            )
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0


@pytest.mark.parametrize("source", ["absent_context", "other_run"])
def test_persistence_rejects_same_replay_evidence_not_authorized_by_exact_context(
    feature_factory: sessionmaker[Session],
    source: str,
) -> None:
    replay, player, evidence_ids = _seed_replay(feature_factory)
    with feature_factory() as session:
        replay_row = session.scalar(select(Replay).where(Replay.public_id == replay))
        assert replay_row is not None
        if source == "absent_context":
            evidence = session.scalar(select(EvidenceItem).where(EvidenceItem.public_id == evidence_ids[-1]))
        else:
            run = TelemetryRun(
                run_id="00000000-0000-4000-8000-000000000318",
                replay_id=replay_row.id,
                schema_version=2,
                engine_build="fixture-other",
                settings_json={},
                status="failed",
                runner_status="failed",
                diagnostics_json={},
                started_at=datetime(2026, 8, 22, tzinfo=UTC),
                completed_at=datetime(2026, 8, 22, tzinfo=UTC),
            )
            session.add(run)
            session.flush()
            evidence = EvidenceItem(
                public_id="00000000-0000-4000-8000-000000000319",
                replay_id=replay_row.id,
                telemetry_run_id=run.id,
                tier="observed",
                source_kind="telemetry",
                source_key="telemetry:other-run:orphan",
                schema_version=2,
                created_at=datetime(2026, 8, 22, tzinfo=UTC),
            )
            session.add(evidence)
            session.commit()
        assert evidence is not None
        ref = EvidenceRef(
            evidence.public_id,
            "observed",
            evidence.source_kind,
            evidence.source_key,
            f"{evidence.source_kind}-v{evidence.schema_version}",
        )

    class DefectiveExtractor:
        name = "defective"
        version = "defective-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: object) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (ref,),
                    ),
                ),
            )

    with pytest.raises(FeatureExtractionError, match="feature persistence failed") as caught:
        FeatureExtractionService(feature_factory, extractors=(DefectiveExtractor(),)).extract(
            _request(replay, player, "defective")
        )
    assert caught.value.__cause__ is not None
    assert str(caught.value.__cause__) == "feature evidence is not authorized by exact feature context"
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0


def test_persistence_rejects_forged_input_hidden_by_exact_later_cross_role_duplicates(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    base_service = FeatureExtractionService(feature_factory)
    context = base_service._build_context(_request(replay, player, "build"))
    exact = context.observed[0].ref
    forged = replace(exact, source_key=f"{exact.source_key}:forged")

    class CrossRoleCollisionExtractor:
        name = "cross_role_collision"
        version = "cross-role-collision-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (forged,),
                        (exact,),
                        (exact,),
                    ),
                ),
            )

    with pytest.raises(FeatureExtractionError, match="feature persistence failed") as caught:
        FeatureExtractionService(
            feature_factory,
            extractors=(CrossRoleCollisionExtractor(),),
        ).extract(_request(replay, player, "cross_role_collision"))
    assert caught.value.__cause__ is not None
    assert str(caught.value.__cause__) == "feature evidence is not authorized by exact feature context"
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0


def test_persistence_rejects_same_reference_as_supporting_and_contradicting_with_full_rollback(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    context = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))
    exact = context.observed[0].ref

    class ExactRolesExtractor:
        name = "exact_roles"
        version = "exact-roles-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (exact,),
                        (exact,),
                        (exact,),
                    ),
                ),
            )

    with pytest.raises(
        FeatureExtractionError,
        match="feature evidence cannot be both supporting and contradicting",
    ):
        FeatureExtractionService(
            feature_factory,
            extractors=(ExactRolesExtractor(),),
        ).extract(_request(replay, player, "exact_roles"))
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet)) == 0
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(EvidenceItem).where(EvidenceItem.tier == "derived")
            )
            == 0
        )


def test_persistence_allows_input_and_supporting_reuse_without_contradiction(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    context = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))
    exact = context.observed[0].ref

    class ExactInputAndSupportingExtractor:
        name = "exact_input_supporting"
        version = "exact-input-supporting-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (exact,),
                        (exact,),
                        (),
                    ),
                ),
            )

    receipt = FeatureExtractionService(
        feature_factory,
        extractors=(ExactInputAndSupportingExtractor(),),
    ).extract(_request(replay, player, "exact_input_supporting"))[0]
    assert receipt.features[0].input_evidence == (exact,)
    assert receipt.features[0].supporting_evidence == (exact,)
    assert receipt.features[0].contradicting_evidence == ()
    with feature_factory() as session:
        assert tuple(
            session.scalars(select(FeatureEvidence.role).order_by(FeatureEvidence.role)).all()
        ) == ("input", "supporting")


def _permuted_json(value: object, randomizer: random.Random) -> object:
    if isinstance(value, dict):
        items = list(value.items())
        randomizer.shuffle(items)
        return {key: _permuted_json(item, randomizer) for key, item in items}
    if isinstance(value, list):
        return [_permuted_json(item, randomizer) for item in value]
    return value


def test_one_hundred_semantic_permutations_have_identical_context_cache_receipt_and_persistence(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    base_service = FeatureExtractionService(feature_factory)
    base = base_service._build_context(_request(replay, player, "build"))
    randomizer = random.Random(0x6A11CE)
    contexts: list[FeatureContext] = []
    evidence_link_orders: list[tuple[EvidenceRef, ...]] = []
    for _ in range(100):
        observed_items = [
            ObservedEvidence(
                item.ref,
                item.frame,
                item.event_type,
                _permuted_json(thaw_canonical(item.facts), randomizer),
            )
            for item in base.observed
        ]
        randomizer.shuffle(observed_items)
        link_order = [item.ref for item in observed_items]
        randomizer.shuffle(link_order)
        evidence_link_orders.append(tuple(link_order))
        settings_items = [("alpha", {"x": 1, "y": 2}), ("beta", [3, 2, 1]), ("gamma", True)]
        randomizer.shuffle(settings_items)
        contexts.append(
            replace(
                base,
                observed=tuple(observed_items),
                settings=_permuted_json(dict(settings_items), randomizer),
            )
        )
    expected_json = canonical_json(contexts[0])
    expected_digest = input_digest(contexts[0])
    expected_key = cache_key(contexts[0], "permutation", "permutation-v1")
    assert all(canonical_json(context) == expected_json for context in contexts)
    assert all(input_digest(context) == expected_digest for context in contexts)
    assert all(cache_key(context, "permutation", "permutation-v1") == expected_key for context in contexts)

    class PermutationExtractor:
        name = "permutation"
        version = "permutation-v1"
        feature_names = ("build.completed_count",)

        def __init__(self) -> None:
            self.calls = 0

        def extract(self, context: FeatureContext) -> FeatureBundle:
            refs = [item.ref for item in context.observed]
            random.Random(self.calls).shuffle(refs)
            self.calls += 1
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        len(context.observed),
                        "count",
                        context.scope,
                        FeatureWindow(0, context.final_frame or 0),
                        "complete",
                        None,
                        tuple(refs),
                    ),
                ),
            )

    class PermutedContextService(FeatureExtractionService):
        def __init__(self, contexts: list[FeatureContext], extractor: PermutationExtractor) -> None:
            super().__init__(feature_factory, extractors=(extractor,))
            self._contexts = iter(contexts)

        def _build_context(self, request: ExtractFeaturesRequest) -> FeatureContext:
            return next(self._contexts)

    extractor = PermutationExtractor()
    service = PermutedContextService(contexts, extractor)
    receipts = [service.extract(_request(replay, player, "permutation"))[0] for _ in range(100)]
    expected_receipt = (
        receipts[0].feature_set_public_id,
        receipts[0].input_digest,
        receipts[0].cache_key,
        receipts[0].features,
    )
    assert all(
        (receipt.feature_set_public_id, receipt.input_digest, receipt.cache_key, receipt.features) == expected_receipt
        for receipt in receipts
    )
    assert extractor.calls == 1
    for link_order in evidence_link_orders:
        validated = validate_feature_value(
            replace(receipts[0].features[0], input_evidence=link_order),
            service._registry,
        )
        assert validated.input_evidence == receipts[0].features[0].input_evidence
    with feature_factory() as session:
        persisted = session.scalar(select(Feature).where(Feature.name == "build.completed_count"))
        assert persisted is not None and persisted.integer_value == len(base.observed)
        links = session.scalars(
            select(EvidenceItem.public_id)
            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .where(FeatureEvidence.feature_id == persisted.id)
            .order_by(EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id)
        ).all()
        assert tuple(links) == tuple(ref.public_id for ref in receipts[0].features[0].input_evidence)


def test_service_cache_invalidates_for_observed_fact_scope_catalog_and_schema_identity(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    base = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))
    first_observation = base.observed[0]
    changed_facts = replace(
        first_observation,
        facts={
            **cast(dict[str, object], thaw_canonical(first_observation.facts)),
            "semantic_change": "observed",
        },
    )
    variants = [
        base,
        replace(base, observed=(changed_facts,) + base.observed[1:]),
        replace(
            base,
            replay_player_public_id=None,
            scope=FeatureScope("replay", base.replay_public_id),
        ),
        replace(base, catalog_identity="f" * 64),
        replace(base, observation_schema_versions=(("parser", "1:changed"), ("telemetry", "2:changed"))),
    ]
    registry = FeatureRegistry(
        REGISTRY_SCHEMA,
        (
            FeatureDefinition(
                "build.completed_count",
                "integer",
                "count",
                ("player", "replay"),
                "inclusive",
                "observed",
                "build",
            ),
        ),
    )

    class InvalidationExtractor:
        name = "invalidation"
        version = "invalidation-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        len(context.observed),
                        "count",
                        context.scope,
                        FeatureWindow(0, context.final_frame or 0),
                        "complete",
                        None,
                        (context.observed[0].ref,),
                    ),
                ),
            )

    class VariantService(FeatureExtractionService):
        def __init__(self) -> None:
            super().__init__(feature_factory, extractors=(InvalidationExtractor(),), registry=registry)
            self._variants = iter(variants)

        def _build_context(self, request: ExtractFeaturesRequest) -> FeatureContext:
            return next(self._variants)

    service = VariantService()
    receipts = [service.extract(_request(replay, player, "invalidation"))[0] for _ in variants]
    assert len({receipt.input_digest for receipt in receipts}) == len(variants)
    assert len({receipt.cache_key for receipt in receipts}) == len(variants)
    assert all(receipt.cache_hit is False for receipt in receipts)
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet).where(FeatureSet.status == "succeeded")) == 5


def test_service_rejects_unknown_identity_extractor_and_duplicate_configuration(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    service = FeatureExtractionService(feature_factory)
    with pytest.raises(FeatureExtractionError, match="unknown extractor"):
        service.extract(_request(replay, player, "missing"))
    with pytest.raises(FeatureExtractionError, match="unknown replay"):
        service.extract(_request("00000000-0000-4000-8000-000000000399", player, "build"))
    with pytest.raises(FeatureExtractionError, match="does not belong"):
        service.extract(_request(replay, "00000000-0000-4000-8000-000000000399", "build"))
    with pytest.raises(ValueError, match="unique"):
        FeatureExtractionService(feature_factory, extractors=(BuildOrderExtractor(), BuildOrderExtractor()))
    with pytest.raises(ValueError, match="unique"):
        ExtractFeaturesRequest(replay, player, ("build", "build"))
