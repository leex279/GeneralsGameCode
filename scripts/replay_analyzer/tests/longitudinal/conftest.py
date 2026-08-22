"""File-backed generated corpus fixtures for longitudinal persistence tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ParserRun,
    Player,
    Replay,
    ReplayPlayer,
)

PLAYER_PUBLIC_ID = "00000000-0000-4000-8000-000000009001"


@pytest.fixture
def longitudinal_engine(tmp_path: Path) -> Engine:
    database = tmp_path / "external-task1-data-root" / "library.sqlite3"
    database.parent.mkdir()
    upgrade_database(database)
    engine = create_database_engine(database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def longitudinal_factory(longitudinal_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(longitudinal_engine)


@pytest.fixture
def seed_corpus(longitudinal_factory: sessionmaker[Session]) -> Callable[..., str]:
    def seed(
        values: tuple[float | None, ...] = (0.0, 10.0, 20.0),
        *,
        player_public_id: str = PLAYER_PUBLIC_ID,
        identity_revision: int = 0,
        include_build_sequences: bool = False,
    ) -> str:
        now = datetime(2026, 8, 22, tzinfo=UTC)
        with longitudinal_factory() as session:
            player = Player(
                public_id=player_public_id,
                display_name="Canonical Player",
                identity_revision=identity_revision,
                created_at=now,
                updated_at=now,
            )
            session.add(player)
            session.flush()
            for index, value in enumerate(values, start=1):
                replay_public_id = str(uuid5(NAMESPACE_URL, f"longitudinal-replay:{index}"))
                replay_sha256 = f"{index:064x}"
                replay = Replay(
                    public_id=replay_public_id,
                    sha256=replay_sha256,
                    replay_name=f"fixture-{index}",
                    version_string="1.04",
                    version_number=104,
                    frame_count=900,
                    start_time=1_700_000_000 + index * 100,
                    end_time=1_700_000_100 + index * 100,
                    exe_crc=1,
                    ini_crc=2,
                    map_crc=3,
                    map_name="Tournament Desert",
                    seed=index,
                    header_json={"patch_identity": "retail-1.04", "subfaction": "Tank"},
                    lifecycle_state="engine_verified",
                    created_at=now,
                    updated_at=now,
                )
                session.add(replay)
                session.flush()
                parser_run_id = str(uuid5(NAMESPACE_URL, f"longitudinal-parser:{index}"))
                parser = ParserRun(
                    run_id=parser_run_id,
                    replay_id=replay.id,
                    parser_version="parser-v1",
                    schema_version=1,
                    input_sha256=replay.sha256,
                    status="running",
                    warnings_json=[],
                    started_at=now,
                )
                session.add(parser)
                session.flush()
                replay_player_public_id = str(uuid5(NAMESPACE_URL, f"longitudinal-replay-player:{index}"))
                replay_player = ReplayPlayer(
                    public_id=replay_player_public_id,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    player_id=player.id,
                    slot_index=0,
                    slot_kind="human",
                    original_name="Canonical Player",
                    normalized_name="canonical player",
                    player_index=0,
                    team_id=0,
                    faction="China",
                    start_position=index - 1,
                    observed_json={"faction": "China", "subfaction": "Tank"},
                )
                session.add(replay_player)
                session.flush()
                observed = EvidenceItem(
                    public_id=str(uuid5(NAMESPACE_URL, f"longitudinal-observed:{index}")),
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    tier="observed",
                    source_kind="parser",
                    source_key=f"parser:{parser_run_id}:command:0",
                    schema_version=1,
                    created_at=now,
                )
                session.add(observed)
                session.flush()
                feature_set = FeatureSet(
                    public_id=str(uuid5(NAMESPACE_URL, f"longitudinal-feature-set:{index}")),
                    replay_id=replay.id,
                    replay_player_id=replay_player.id,
                    extractor_name="economy",
                    extractor_version="economy-v1",
                    input_digest=f"{1000 + index:064x}",
                    cache_key=f"{2000 + index:064x}",
                    status="succeeded",
                    settings_json={},
                    completed_at=now,
                    created_at=now,
                )
                session.add(feature_set)
                session.flush()
                derived = EvidenceItem(
                    public_id=str(uuid5(NAMESPACE_URL, f"longitudinal-derived:{index}")),
                    replay_id=replay.id,
                    tier="derived",
                    source_kind="feature",
                    source_key=f"feature:{feature_set.public_id}:economy.cash_change_total:player:{replay_player.public_id}:0:900",
                    schema_version=1,
                    created_at=now,
                )
                session.add(derived)
                session.flush()
                feature = Feature(
                    public_id=str(uuid5(NAMESPACE_URL, f"longitudinal-feature:{index}")),
                    feature_set_id=feature_set.id,
                    evidence_item_id=derived.id,
                    name="economy.cash_change_total",
                    value_type="real",
                    real_value=value,
                    unit="credits",
                    scope_type="player",
                    scope_key=replay_player.public_id,
                    replay_player_id=replay_player.id,
                    frame_start=0,
                    frame_end=900,
                    quality="available" if value is not None else "unavailable",
                    quality_reason=None if value is not None else "missing_economy_observation",
                    details_json={},
                )
                session.add(feature)
                session.flush()
                session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=observed.id, role="input"))
                if include_build_sequences:
                    build_set = FeatureSet(
                        public_id=str(uuid5(NAMESPACE_URL, f"longitudinal-build-set:{index}")),
                        replay_id=replay.id,
                        replay_player_id=replay_player.id,
                        extractor_name="build-order",
                        extractor_version="build-order-v1",
                        input_digest=f"{4000 + index:064x}",
                        cache_key=f"{5000 + index:064x}",
                        status="succeeded",
                        settings_json={},
                        completed_at=now,
                        created_at=now,
                    )
                    session.add(build_set)
                    session.flush()
                    build_evidence = EvidenceItem(
                        public_id=str(uuid5(NAMESPACE_URL, f"longitudinal-build-derived:{index}")),
                        replay_id=replay.id,
                        tier="derived",
                        source_kind="feature",
                        source_key=f"feature:{build_set.public_id}:build.completed_sequence:player:{replay_player.public_id}:0:900",
                        schema_version=1,
                        created_at=now,
                    )
                    session.add(build_evidence)
                    session.flush()
                    build_feature = Feature(
                        public_id=str(uuid5(NAMESPACE_URL, f"longitudinal-build-feature:{index}")),
                        feature_set_id=build_set.id,
                        evidence_item_id=build_evidence.id,
                        name="build.completed_sequence",
                        value_type="json",
                        json_value=["ChinaPowerPlant", "ChinaBarracks", "ChinaSupplyCenter"],
                        unit="json",
                        scope_type="player",
                        scope_key=replay_player.public_id,
                        replay_player_id=replay_player.id,
                        frame_start=0,
                        frame_end=900,
                        quality="available",
                        details_json={},
                    )
                    session.add(build_feature)
                    session.flush()
                    session.add(
                        FeatureEvidence(feature_id=build_feature.id, evidence_item_id=observed.id, role="input")
                    )
                parser.status = "succeeded"
                parser.completion_status = "complete"
                parser.result_sha256 = f"{3000 + index:064x}"
                parser.completed_at = now
            session.commit()
        return player_public_id

    return seed
