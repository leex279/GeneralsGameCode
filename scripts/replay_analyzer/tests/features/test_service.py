"""Feature context materialization, cache, race, and rollback tests."""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ParserRun,
    Replay,
    ReplayPlayer,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.base import FeatureBundle, FeatureScope, FeatureValue, FeatureWindow
from generals_replay_analyzer.features.build_order import BuildOrderExtractor
from generals_replay_analyzer.features.evidence import EvidenceRef
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
            20,
            "construction_completed",
            {"object_id": 7, "owner_player_index": player_index, "template_name": "ChinaPowerPlant"},
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


def test_one_hundred_request_permutations_have_stable_receipt_order_and_raw_values(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    service = FeatureExtractionService(feature_factory)
    expected: tuple[tuple[str, tuple[tuple[str, object], ...]], ...] | None = None
    randomizer = random.Random(0x6A11CE)
    for _ in range(100):
        names = ["build", "activity"]
        randomizer.shuffle(names)
        receipts = service.extract(_request(replay, player, *names, settings={"b": 2, "a": 1}))
        actual = tuple(
            (receipt.extractor_name, tuple((value.name, value.raw_value) for value in receipt.features))
            for receipt in receipts
        )
        expected = actual if expected is None else expected
        assert actual == expected


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
