"""Strategy database adapter selection, cache, and atomic graph tests."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ManagedAsset,
    ParserRun,
    Replay,
    ReplayPlayer,
    StrategyAssessment,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence
from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureRegistry
from generals_replay_analyzer.strategy.rules import CatalogProof, StrategyContext
from generals_replay_analyzer.strategy.service import StrategyAssessmentService

from .conftest import MemoryResource

REPLAY = "00000000-0000-4000-8000-000000000301"
PLAYER = "00000000-0000-4000-8000-000000000302"
FEATURE_SET = "00000000-0000-4000-8000-000000000303"


class _InjectedApplicabilityService(StrategyAssessmentService):
    def __init__(
        self,
        factory: sessionmaker[Session],
        taxonomy_resource: MemoryResource,
        catalog: CatalogProof,
        player_faction_evidence: EvidenceRef,
        opponent_faction_evidence: EvidenceRef,
        map_identity_evidence: EvidenceRef,
    ) -> None:
        super().__init__(factory, taxonomy_resource=taxonomy_resource)
        self._catalog = catalog
        self._player_faction_evidence = player_faction_evidence
        self._opponent_faction_evidence = opponent_faction_evidence
        self._map_identity_evidence = map_identity_evidence

    def _build_context(
        self,
        replay_public_id: str,
        replay_player_public_id: str | None,
        feature_set_public_ids: tuple[str, ...],
        registry: FeatureRegistry,
    ) -> StrategyContext:
        context = super()._build_context(
            replay_public_id,
            replay_player_public_id,
            feature_set_public_ids,
            registry,
        )
        return replace(
            context,
            player_faction_template_name="FactionAmerica",
            player_faction_evidence=self._player_faction_evidence,
            opponent_faction_template_name="FactionChina",
            opponent_faction_evidence=self._opponent_faction_evidence,
            map_identity="maps/test/map.ini",
            map_identity_evidence=self._map_identity_evidence,
            catalog=self._catalog,
        )


class _PermutingApplicabilityService(_InjectedApplicabilityService):
    def __init__(
        self,
        factory: sessionmaker[Session],
        taxonomy_resource: MemoryResource,
        catalog: CatalogProof,
        player_faction_evidence: EvidenceRef,
        opponent_faction_evidence: EvidenceRef,
        map_identity_evidence: EvidenceRef,
    ) -> None:
        super().__init__(
            factory,
            taxonomy_resource,
            catalog,
            player_faction_evidence,
            opponent_faction_evidence,
            map_identity_evidence,
        )
        self._permutation_case = 0

    def _build_context(
        self,
        replay_public_id: str,
        replay_player_public_id: str | None,
        feature_set_public_ids: tuple[str, ...],
        registry: FeatureRegistry,
    ) -> StrategyContext:
        context = super()._build_context(
            replay_public_id,
            replay_player_public_id,
            feature_set_public_ids,
            registry,
        )
        orders = tuple(itertools.permutations(context.features))
        order = orders[self._permutation_case % len(orders)]
        case = self._permutation_case
        self._permutation_case += 1
        permuted = tuple(
            replace(
                feature,
                value=replace(
                    feature.value,
                    input_evidence=(
                        tuple(reversed(feature.value.input_evidence))
                        if (case + index) % 2 == 0
                        else feature.value.input_evidence
                    ),
                ),
            )
            for index, feature in enumerate(order)
        )
        return replace(context, features=permuted)


@pytest.fixture
def strategy_data_root(tmp_path: Path) -> Path:
    root = tmp_path / "external-data-root"
    root.mkdir()
    return root


@pytest.fixture
def strategy_engine(strategy_data_root: Path) -> Engine:
    database = strategy_data_root / "library.sqlite3"
    upgrade_database(database)
    engine = create_database_engine(database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def strategy_factory(strategy_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(strategy_engine)


def _seed_feature_set(
    factory: sessionmaker[Session],
    *,
    replay_public_id: str = REPLAY,
    replay_sha256: str = "a" * 64,
    player_public_id: str = PLAYER,
    feature_set_public_id: str = FEATURE_SET,
    raw_value: float = 150.0,
    status: str = "succeeded",
    feature_evidence_tier: Literal["observed", "derived", "inferred"] = "derived",
    input_evidence_tier: Literal["observed", "derived", "inferred"] = "observed",
    telemetry_parser_run_id: str | None = None,
    include_opponent: bool = False,
    complete_runs: bool = True,
) -> tuple[str, str, str]:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    identity = feature_set_public_id[-12:]
    with factory() as session:
        replay = Replay(
            public_id=replay_public_id,
            sha256=replay_sha256,
            replay_name=f"fixture-{identity}",
            version_string="1.04",
            version_number=1,
            frame_count=300,
            start_time=0,
            end_time=300,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="maps/test/map.ini",
            seed=4,
            header_json={},
            lifecycle_state="engine_verified",
            updated_at=now,
            created_at=now,
        )
        session.add(replay)
        session.flush()
        parser = ParserRun(
            run_id=str(uuid5(NAMESPACE_URL, f"parser:{identity}")),
            replay_id=replay.id,
            parser_version="parser-v2",
            schema_version=1,
            input_sha256=replay_sha256,
            status="running",
            completion_status=None,
            result_sha256=None,
            warnings_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(parser)
        session.flush()
        player = ReplayPlayer(
            public_id=player_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            slot_index=0,
            slot_kind="human",
            original_name="Player",
            normalized_name="player",
            player_index=0,
            faction="FactionAmerica",
            observed_json={"faction": "FactionAmerica"},
        )
        session.add(player)
        if include_opponent:
            session.add(
                ReplayPlayer(
                    public_id="00000000-0000-4000-8000-000000000307",
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    slot_index=1,
                    slot_kind="human",
                    original_name="Opponent",
                    normalized_name="opponent",
                    player_index=1,
                    faction="FactionChina",
                    observed_json={"faction": "FactionChina"},
                )
            )
        session.flush()
        telemetry = TelemetryRun(
            run_id=str(uuid5(NAMESPACE_URL, f"telemetry:{identity}")),
            replay_id=replay.id,
            schema_version=2,
            engine_build="fixture",
            settings_json={"parser_run_id": telemetry_parser_run_id or parser.run_id},
            status="running",
            runner_status="succeeded",
            diagnostics_json={},
            started_at=now,
        )
        session.add(telemetry)
        session.flush()
        observed = EvidenceItem(
            public_id=str(uuid5(NAMESPACE_URL, f"observed:{identity}")),
            replay_id=replay.id,
            parser_run_id=parser.id,
            telemetry_run_id=telemetry.id,
            tier=input_evidence_tier,
            source_kind="telemetry",
            source_key=f"telemetry:fixture:{identity}",
            schema_version=2,
            created_at=now,
        )
        derived = EvidenceItem(
            public_id=str(uuid5(NAMESPACE_URL, f"feature-evidence:{identity}")),
            replay_id=replay.id,
            parser_run_id=parser.id,
            telemetry_run_id=telemetry.id,
            tier=feature_evidence_tier,
            source_kind="feature",
            source_key=f"feature:fixture:{identity}",
            schema_version=1,
            created_at=now,
        )
        session.add_all((observed, derived))
        session.flush()
        feature_set = FeatureSet(
            public_id=feature_set_public_id,
            replay_id=replay.id,
            replay_player_id=player.id,
            extractor_name="economy",
            extractor_version="economy-v1",
            input_digest="c" * 64,
            cache_key=("d" * 52) + identity,
            status=status,
            settings_json={"fixture": identity},
            completed_at=now if status == "succeeded" else None,
            error_json=None,
            created_at=now,
        )
        session.add(feature_set)
        session.flush()
        feature = Feature(
            public_id=str(uuid5(NAMESPACE_URL, f"feature:{identity}")),
            feature_set_id=feature_set.id,
            evidence_item_id=derived.id,
            name="economy.supply_collected_total",
            value_type="real",
            real_value=raw_value,
            unit="credits",
            scope_type="player",
            scope_key=player_public_id,
            replay_player_id=player.id,
            frame_start=0,
            frame_end=300,
            quality="available",
            quality_reason=None,
            details_json={},
        )
        session.add(feature)
        session.flush()
        session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=observed.id, role="input"))
        session.commit()
        if complete_runs:
            parser.status = "succeeded"
            parser.completion_status = "complete"
            parser.result_sha256 = "b" * 64
            parser.completed_at = now
            telemetry.status = "succeeded"
            telemetry.final_frame = 300
            telemetry.command_count = 0
            telemetry.trace_sha256 = "e" * 64
            telemetry.completed_at = now
            session.commit()
    return replay_public_id, player_public_id, feature_set_public_id


def _add_named_feature(
    factory: sessionmaker[Session],
    *,
    sequence: int,
    name: str,
    value_type: Literal["integer", "real"],
    raw_value: float,
    unit: str,
) -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    public_id = f"00000000-0000-4000-8000-{sequence:012d}"
    with factory() as session:
        feature_set = session.scalar(select(FeatureSet).where(FeatureSet.public_id == FEATURE_SET))
        player = session.scalar(select(ReplayPlayer).where(ReplayPlayer.public_id == PLAYER))
        assert feature_set is not None and player is not None
        observed = EvidenceItem(
            public_id=public_id,
            replay_id=feature_set.replay_id,
            tier="observed",
            source_kind="telemetry",
            source_key=f"telemetry:named:{sequence}",
            schema_version=2,
            created_at=now,
        )
        derived = EvidenceItem(
            public_id=f"00000000-0000-4000-8000-{sequence + 100:012d}",
            replay_id=feature_set.replay_id,
            tier="derived",
            source_kind="feature",
            source_key=f"feature:named:{sequence}",
            schema_version=1,
            created_at=now,
        )
        session.add_all((observed, derived))
        session.flush()
        feature = Feature(
            public_id=f"00000000-0000-4000-8000-{sequence + 200:012d}",
            feature_set_id=feature_set.id,
            evidence_item_id=derived.id,
            name=name,
            value_type=value_type,
            integer_value=raw_value if value_type == "integer" else None,
            real_value=raw_value if value_type == "real" else None,
            unit=unit,
            scope_type="player",
            scope_key=player.public_id,
            replay_player_id=player.id,
            frame_start=0,
            frame_end=300,
            quality="available",
            details_json={},
        )
        session.add(feature)
        session.flush()
        session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=observed.id, role="input"))
        session.commit()


def _seed_applicability(
    factory: sessionmaker[Session],
) -> tuple[CatalogProof, EvidenceRef, EvidenceRef, EvidenceRef]:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    specifications = (
        ("00000000-0000-4000-8000-000000000901", "catalog", "catalog:fixture"),
        ("00000000-0000-4000-8000-000000000902", "parser_fact", "parser:player-faction"),
        ("00000000-0000-4000-8000-000000000903", "parser_fact", "parser:opponent-faction"),
        ("00000000-0000-4000-8000-000000000904", "map_manifest", "map:fixture"),
    )
    refs: list[EvidenceRef] = []
    with factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY))
        assert replay is not None
        for public_id, source_kind, source_key in specifications:
            session.add(
                EvidenceItem(
                    public_id=public_id,
                    replay_id=replay.id,
                    tier="observed",
                    source_kind=source_kind,
                    source_key=source_key,
                    schema_version=1,
                    created_at=now,
                )
            )
            refs.append(EvidenceRef(public_id, "observed", source_kind, source_key, f"{source_kind}-v1"))
        session.commit()
    catalog = CatalogProof(
        catalog_identity="catalog-v1:fixture",
        evidence=ObservedEvidence(
            refs[0],
            None,
            "catalog_manifest",
            {"catalog_identity": "catalog-v1:fixture"},
        ),
        factions_by_template=(
            ("AmericaTankDozer", "FactionAmerica"),
            ("ChinaDozer", "FactionChina"),
        ),
        category_tags_by_template=(
            ("AmericaTankDozer", ("builder",)),
            ("ChinaDozer", ("builder",)),
        ),
    )
    return catalog, refs[1], refs[2], refs[3]


def _catalog_template(
    ordinal: int,
    name: str,
    faction: str,
    *category_tags: str,
    configured_build_time_seconds: object = 0.0,
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "name": name,
        "faction": faction,
        "kind_of_flags": [],
        "behavior_modules": [],
        "build_cost": 0,
        "configured_build_time_seconds": configured_build_time_seconds,
        "prerequisites": [],
        "locomotor_sets": [],
        "production_capable": False,
        "weapon_sets": [],
        "derived_weapon_names": [],
        "category_tags": list(category_tags),
    }


def _seed_persisted_applicability(
    factory: sessionmaker[Session],
    data_root: Path,
    *,
    catalog_build_time: object = 0.0,
) -> tuple[str, str]:
    catalog = {
        "schema_version": 1,
        "type": "game_data_catalog",
        "engine_data_identity": "fixture",
        "weapon_scope": "referenced_by_thing_templates",
        "locomotor_scope": "referenced_by_thing_templates",
        "thing_templates": [
            _catalog_template(
                0,
                "AmericaTankDozer",
                "FactionAmerica",
                "builder",
                configured_build_time_seconds=catalog_build_time,
            ),
            _catalog_template(1, "ChinaDozer", "FactionChina", "builder"),
        ],
        "upgrades": [],
        "sciences": [],
        "weapons": [],
        "locomotors": [],
    }
    catalog_bytes = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    relative_path = f"assets/game-data-catalog-v1-{catalog_sha256}.json"
    catalog_path = data_root / Path(*relative_path.split("/"))
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_bytes(catalog_bytes)
    now = datetime(2026, 8, 22, tzinfo=UTC)
    manifest_public_id = "00000000-0000-4000-8000-000000000905"
    players_public_id = "00000000-0000-4000-8000-000000000906"
    with factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY))
        player = session.scalar(select(ReplayPlayer).where(ReplayPlayer.public_id == PLAYER))
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.replay_id == replay.id))  # type: ignore[union-attr]
        assert replay is not None and player is not None and telemetry is not None
        parser = session.get(ParserRun, player.parser_run_id)
        assert parser is not None
        asset = ManagedAsset(
            public_id="00000000-0000-4000-8000-000000000308",
            sha256=catalog_sha256,
            kind="telemetry_catalog",
            relative_path=relative_path,
            size_bytes=len(catalog_bytes),
            media_type="application/json",
            created_at=now,
        )
        session.add(asset)
        session.flush()
        telemetry.catalog_asset_id = asset.id
        catalog_reference = {
            "type": "game_data_catalog",
            "path": catalog_path.name,
            "sha256": catalog_sha256,
            "engine_data_identity": "fixture",
        }
        manifest_payload = {
            "engine_build": "fixture",
            "replay_version": "1.04",
            "map_identity": "maps/test/map.ini",
            "initial_seed": 4,
            "exporter_settings": {"movement_sample_frames": 15, "audio_enabled": False, "order_coverage": []},
            "game_data_catalog": catalog_reference,
            "map_asset": None,
        }
        slots = [
            {
                "slot_index": index,
                "slot_state": "human" if index < 2 else "open",
                "occupied": index < 2,
                "resolution_status": "resolved" if index < 2 else "not_applicable",
                "replay_name": ("Player", "Opponent")[index] if index < 2 else None,
                "player_index": index if index < 2 else None,
                "team_id": index if index < 2 else None,
                "faction_template_name": ("FactionAmerica", "FactionChina")[index] if index < 2 else None,
                "color": index if index < 2 else None,
                "start_position_status": "unknown" if index < 2 else "not_applicable",
                "start_position": None,
                "controller": "human" if index < 2 else None,
                "is_human": index < 2,
                "is_header_local_slot": index == 0,
                "is_resolved_local_player": True if index == 0 else False if index == 1 else None,
            }
            for index in range(8)
        ]
        players_payload = {
            "header_local_slot_index": 0,
            "slots": slots,
            "engine_player_indices": [0, 1],
            "game_data_catalog": catalog_reference,
        }
        for sequence, event_type, payload, public_id in (
            (0, "manifest", manifest_payload, manifest_public_id),
            (1, "players_initialized", players_payload, players_public_id),
        ):
            evidence = EvidenceItem(
                public_id=public_id,
                replay_id=replay.id,
                parser_run_id=parser.id,
                telemetry_run_id=telemetry.id,
                tier="observed",
                source_kind="telemetry_event",
                source_key=f"telemetry:{telemetry.run_id}:sequence:{sequence}",
                schema_version=2,
                created_at=now,
            )
            session.add(evidence)
            session.flush()
            session.add(
                TelemetryEvent(
                    telemetry_run_id=telemetry.id,
                    sequence=sequence,
                    frame=0,
                    logic_time_seconds=0.0,
                    schema_version=2,
                    event_type=event_type,
                    payload_json=payload,
                    raw_record_json={
                        "schema_version": 2,
                        "run_id": telemetry.run_id,
                        "sequence": sequence,
                        "frame": 0,
                        "logic_time_seconds": 0.0,
                        "event_type": event_type,
                        "payload": payload,
                    },
                    evidence_item_id=evidence.id,
                )
            )
        parser.status = "succeeded"
        parser.completion_status = "complete"
        parser.result_sha256 = "b" * 64
        parser.completed_at = now
        telemetry.status = "succeeded"
        telemetry.final_frame = 300
        telemetry.command_count = 0
        telemetry.trace_sha256 = "e" * 64
        telemetry.completed_at = now
        session.commit()
    return manifest_public_id, players_public_id


def _add_secondary_feature_inputs(factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    with factory() as session:
        feature_set = session.scalar(select(FeatureSet).where(FeatureSet.public_id == FEATURE_SET))
        assert feature_set is not None
        features = tuple(
            session.scalars(
                select(Feature).where(Feature.feature_set_id == feature_set.id).order_by(Feature.public_id)
            ).all()
        )
        for index, feature in enumerate(features):
            evidence = EvidenceItem(
                public_id=f"00000000-0000-4000-8000-{950 + index:012d}",
                replay_id=feature_set.replay_id,
                tier="observed",
                source_kind="telemetry",
                source_key=f"telemetry:secondary-input:{index}",
                schema_version=2,
                created_at=now,
            )
            session.add(evidence)
            session.flush()
            session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=evidence.id, role="input"))
        session.commit()


def _named_service(
    factory: sessionmaker[Session],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
    service_type: type[_InjectedApplicabilityService] = _InjectedApplicabilityService,
) -> _InjectedApplicabilityService:
    _seed_feature_set(factory)
    _add_named_feature(
        factory,
        sequence=11,
        name="production.completed_count",
        value_type="integer",
        raw_value=3,
        unit="count",
    )
    _add_named_feature(
        factory,
        sequence=12,
        name="combat.applied_damage_taken",
        value_type="real",
        raw_value=75.0,
        unit="damage",
    )
    catalog, player_faction, opponent_faction, map_identity = _seed_applicability(factory)
    return service_type(
        factory,
        taxonomy_resource(),
        catalog,
        player_faction,
        opponent_faction,
        map_identity,
    )


def _counts(factory: sessionmaker[Session]) -> tuple[int, int, int, int]:
    with factory() as session:
        return (
            session.scalar(
                select(func.count()).select_from(EvidenceItem).where(EvidenceItem.source_kind == "strategy_rule")
            )
            or 0,
            session.scalar(select(func.count()).select_from(StrategyAssessment)) or 0,
            session.scalar(select(func.count()).select_from(AssessmentEvidence)) or 0,
            session.scalar(select(func.count()).select_from(AnalysisRun)) or 0,
        )


def test_service_persists_one_immutable_rule_graph_and_returns_public_dtos(
    strategy_factory: sessionmaker[Session],
) -> None:
    _seed_feature_set(strategy_factory)
    service = StrategyAssessmentService(strategy_factory)

    receipt = service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)

    assert receipt.taxonomy_version == "strategy-taxonomy-v1.0.0"
    assert len(receipt.taxonomy_sha256) == len(receipt.cache_key) == 64
    assert tuple(item.strategy_id for item in receipt.assessments) == ("unknown_or_mixed",)
    assert receipt.assessments[0].quality == "unavailable"
    assert receipt.assessments[0].rule_score is None
    assert _counts(strategy_factory) == (1, 1, 0, 0)
    with strategy_factory() as session:
        row = session.scalar(select(StrategyAssessment))
        assert row is not None
        evidence = session.get(EvidenceItem, row.evidence_item_id)
        assert evidence is not None
        assert row.method == "rule"
        assert row.analysis_run_id is None
        assert row.model_version is None
        assert row.confidence is None
        assert evidence.parser_run_id is not None
        assert evidence.telemetry_run_id is not None
        assert f":{receipt.cache_key}:unknown_or_mixed:cross_phase:0:300" in evidence.source_key
        assert row.details_json["score_kind"] == "transparent_rule_score_not_probability"
        assert row.details_json["cache_identity"]["feature_set_public_ids"] == [FEATURE_SET]
        assert len(row.details_json["cache_identity"]["registry_content_sha256"]) == 64
        assert len(row.details_json["cache_identity"]["taxonomy_rules_sha256"]) == 64


def test_strategy_rejects_cross_attempt_parser_and_telemetry_owners_before_writes(
    strategy_factory: sessionmaker[Session],
) -> None:
    _seed_feature_set(
        strategy_factory,
        telemetry_parser_run_id="00000000-0000-4000-8000-000000000399",
    )

    with pytest.raises(ValueError, match="parser.*telemetry|branch"):
        StrategyAssessmentService(strategy_factory).assess_rule_candidates(
            REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY
        )

    assert _counts(strategy_factory) == (0, 0, 0, 0)


def test_named_service_persists_exact_applicability_feature_and_contradiction_roles(
    strategy_factory: sessionmaker[Session],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    service = _named_service(strategy_factory, taxonomy_resource)

    receipt = service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)

    assert tuple(item.strategy_id for item in receipt.assessments) == ("catalog_proven_pressure",)
    assessment = receipt.assessments[0]
    assert assessment.quality == "available"
    assert assessment.rule_score == pytest.approx(5.0 / 6.0)
    supporting_ids = {ref.public_id for ref in assessment.supporting_evidence}
    contradicting_ids = {ref.public_id for ref in assessment.contradicting_evidence}
    assert {
        "00000000-0000-4000-8000-000000000901",
        "00000000-0000-4000-8000-000000000902",
        "00000000-0000-4000-8000-000000000903",
        "00000000-0000-4000-8000-000000000904",
    } <= supporting_ids
    assert len(supporting_ids) == 8
    assert len(contradicting_ids) == 2
    assert supporting_ids.isdisjoint(contradicting_ids)
    with strategy_factory() as session:
        row = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "rule"))
        assert row is not None
        roles = tuple(
            session.execute(
                select(AssessmentEvidence.role, EvidenceItem.public_id)
                .join(EvidenceItem, EvidenceItem.id == AssessmentEvidence.evidence_item_id)
                .where(AssessmentEvidence.assessment_id == row.id)
                .order_by(AssessmentEvidence.role, EvidenceItem.source_kind, EvidenceItem.source_key)
            ).all()
        )
    assert sum(role == "supporting" for role, _ in roles) == 8
    assert sum(role == "contradicting" for role, _ in roles) == 2


def test_service_builds_named_strategy_applicability_from_persisted_telemetry(
    strategy_factory: sessionmaker[Session],
    strategy_data_root: Path,
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    """Catch production strategy assessment discarding imported faction, map, and catalog proof."""
    _seed_feature_set(strategy_factory, include_opponent=True, complete_runs=False)
    _add_named_feature(
        strategy_factory,
        sequence=11,
        name="production.completed_count",
        value_type="integer",
        raw_value=3,
        unit="count",
    )
    _add_named_feature(
        strategy_factory,
        sequence=12,
        name="combat.applied_damage_taken",
        value_type="real",
        raw_value=75.0,
        unit="damage",
    )
    manifest_public_id, players_public_id = _seed_persisted_applicability(
        strategy_factory,
        strategy_data_root,
    )

    receipt = StrategyAssessmentService(
        strategy_factory,
        data_root=strategy_data_root,
        taxonomy_resource=taxonomy_resource(),
    ).assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)

    assert tuple(item.strategy_id for item in receipt.assessments) == ("catalog_proven_pressure",)
    assessment = receipt.assessments[0]
    assert assessment.quality == "available"
    assert {manifest_public_id, players_public_id} <= {
        reference.public_id for reference in assessment.supporting_evidence
    }


def test_nonfinite_managed_catalog_cannot_enable_a_named_strategy(
    strategy_factory: sessionmaker[Session],
    strategy_data_root: Path,
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    """Catch a hash-valid JavaScript NaN token passing catalog validation."""
    _seed_feature_set(strategy_factory, include_opponent=True, complete_runs=False)
    _add_named_feature(
        strategy_factory,
        sequence=11,
        name="production.completed_count",
        value_type="integer",
        raw_value=3,
        unit="count",
    )
    _add_named_feature(
        strategy_factory,
        sequence=12,
        name="combat.applied_damage_taken",
        value_type="real",
        raw_value=75.0,
        unit="damage",
    )
    _seed_persisted_applicability(
        strategy_factory,
        strategy_data_root,
        catalog_build_time=float("nan"),
    )

    receipt = StrategyAssessmentService(
        strategy_factory,
        data_root=strategy_data_root,
        taxonomy_resource=taxonomy_resource(),
    ).assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)

    assert tuple(item.strategy_id for item in receipt.assessments) == ("unknown_or_mixed",)
    assert receipt.assessments[0].quality == "unavailable"


def test_exact_cache_hit_is_idempotent_and_input_change_retains_history(
    strategy_factory: sessionmaker[Session],
) -> None:
    _seed_feature_set(strategy_factory)
    service = StrategyAssessmentService(strategy_factory)
    first = service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    second = service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)

    assert second == first
    assert _counts(strategy_factory) == (1, 1, 0, 0)

    other_factory = strategy_factory
    with other_factory() as session:
        existing = session.scalar(select(FeatureSet).where(FeatureSet.public_id == FEATURE_SET))
        assert existing is not None
        replay = session.get(Replay, existing.replay_id)
        player = session.get(ReplayPlayer, existing.replay_player_id)
        assert replay is not None and player is not None
        observed = session.scalar(
            select(EvidenceItem).where(
                EvidenceItem.replay_id == replay.id,
                EvidenceItem.source_kind == "telemetry",
            )
        )
        derived = session.scalar(
            select(EvidenceItem).where(
                EvidenceItem.replay_id == replay.id,
                EvidenceItem.source_kind == "feature",
            )
        )
        assert observed is not None and derived is not None
        changed_derived = EvidenceItem(
            public_id="00000000-0000-4000-8000-000000000306",
            replay_id=replay.id,
            parser_run_id=derived.parser_run_id,
            telemetry_run_id=derived.telemetry_run_id,
            tier="derived",
            source_kind="feature",
            source_key="feature:fixture:changed",
            schema_version=1,
            created_at=datetime(2026, 8, 22, tzinfo=UTC),
        )
        session.add(changed_derived)
        session.flush()
        clone_id = "00000000-0000-4000-8000-000000000304"
        clone = FeatureSet(
            public_id=clone_id,
            replay_id=replay.id,
            replay_player_id=player.id,
            extractor_name="economy-v2",
            extractor_version="economy-v2",
            input_digest="e" * 64,
            cache_key="f" * 64,
            status="succeeded",
            settings_json={"fixture": "changed"},
            completed_at=datetime(2026, 8, 22, tzinfo=UTC),
            created_at=datetime(2026, 8, 22, tzinfo=UTC),
        )
        session.add(clone)
        session.flush()
        feature = Feature(
            public_id="00000000-0000-4000-8000-000000000307",
            feature_set_id=clone.id,
            evidence_item_id=changed_derived.id,
            name="economy.supply_collected_total",
            value_type="real",
            real_value=151.0,
            unit="credits",
            scope_type="player",
            scope_key=player.public_id,
            replay_player_id=player.id,
            frame_start=0,
            frame_end=300,
            quality="available",
            details_json={},
        )
        session.add(feature)
        session.flush()
        session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=observed.id, role="input"))
        session.commit()

    changed = service.assess_rule_candidates(REPLAY, PLAYER, ("00000000-0000-4000-8000-000000000304",), BASE_REGISTRY)
    assert changed.cache_key != first.cache_key
    assert _counts(strategy_factory) == (2, 2, 0, 0)


@pytest.mark.parametrize(
    ("feature_sets", "message"),
    [
        ((), "nonempty"),
        ((FEATURE_SET, FEATURE_SET), "unique"),
        (("00000000-0000-4000-8000-000000000399", FEATURE_SET), "sorted"),
        (("00000000-0000-4000-8000-000000000399",), "unknown"),
    ],
)
def test_selected_feature_set_ids_are_exact_nonempty_sorted_and_unique(
    strategy_factory: sessionmaker[Session], feature_sets: tuple[str, ...], message: str
) -> None:
    _seed_feature_set(strategy_factory)

    with pytest.raises((ValueError, LookupError), match=message):
        StrategyAssessmentService(strategy_factory).assess_rule_candidates(REPLAY, PLAYER, feature_sets, BASE_REGISTRY)


@pytest.mark.parametrize(
    ("seed_change", "message"),
    [
        ({"status": "failed"}, "succeeded"),
        ({"feature_evidence_tier": "observed"}, "derived"),
        ({"input_evidence_tier": "inferred"}, "observed"),
    ],
)
def test_service_rejects_incomplete_or_wrong_tier_feature_graphs(
    strategy_factory: sessionmaker[Session], seed_change: dict[str, object], message: str
) -> None:
    _seed_feature_set(strategy_factory, **seed_change)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=message):
        StrategyAssessmentService(strategy_factory).assess_rule_candidates(
            REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY
        )
    assert _counts(strategy_factory) == (0, 0, 0, 0)


def test_requested_player_must_belong_to_the_replay(strategy_factory: sessionmaker[Session]) -> None:
    _seed_feature_set(strategy_factory)

    with pytest.raises(ValueError, match="player"):
        StrategyAssessmentService(strategy_factory).assess_rule_candidates(
            REPLAY, "00000000-0000-4000-8000-000000000399", (FEATURE_SET,), BASE_REGISTRY
        )


def test_unknown_replay_is_rejected_and_empty_successful_feature_set_gets_unavailable_fallback(
    strategy_factory: sessionmaker[Session],
) -> None:
    service = StrategyAssessmentService(strategy_factory)
    with pytest.raises(LookupError, match="replay"):
        service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)

    _seed_feature_set(strategy_factory)
    with strategy_factory() as session:
        feature = session.scalar(select(Feature))
        assert feature is not None
        session.delete(feature)
        session.commit()
    with pytest.raises(ValueError, match="no authoritative run owner"):
        service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)


def test_unavailable_persisted_feature_without_input_evidence_remains_unavailable_data(
    strategy_factory: sessionmaker[Session],
) -> None:
    _seed_feature_set(strategy_factory)
    with strategy_factory() as session:
        feature = session.scalar(select(Feature))
        assert feature is not None
        feature.real_value = None
        feature.quality = "unavailable"
        feature.quality_reason = "no_supported_observations"
        link = session.scalar(select(FeatureEvidence).where(FeatureEvidence.feature_id == feature.id))
        assert link is not None
        session.delete(link)
        session.commit()

    receipt = StrategyAssessmentService(strategy_factory).assess_rule_candidates(
        REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY
    )

    assert receipt.assessments[0].strategy_id == "unknown_or_mixed"
    assert receipt.assessments[0].quality == "unavailable"


def test_tampered_cache_details_are_rejected_instead_of_reused(
    strategy_factory: sessionmaker[Session],
) -> None:
    _seed_feature_set(strategy_factory)
    service = StrategyAssessmentService(strategy_factory)
    service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    with strategy_factory() as session:
        row = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "rule"))
        assert row is not None
        details = dict(row.details_json)
        details["cache_identity"] = {"tampered": True}
        row.details_json = details
        session.commit()

    with pytest.raises(ValueError, match="cache identity"):
        service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)


@pytest.mark.parametrize(
    "poison_target",
    ("result_replay", "assessment_replay", "assessment_player"),
)
def test_exact_key_cache_rejects_result_and_assessment_ownership_poisoning(
    strategy_factory: sessionmaker[Session], poison_target: str
) -> None:
    _seed_feature_set(strategy_factory)
    service = StrategyAssessmentService(strategy_factory)
    service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    other_replay = "00000000-0000-4000-8000-000000000401"
    other_player = "00000000-0000-4000-8000-000000000402"
    _seed_feature_set(
        strategy_factory,
        replay_public_id=other_replay,
        replay_sha256="9" * 64,
        player_public_id=other_player,
        feature_set_public_id="00000000-0000-4000-8000-000000000403",
    )
    with strategy_factory() as session:
        row = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "rule"))
        other_replay_row = session.scalar(select(Replay).where(Replay.public_id == other_replay))
        other_player_row = session.scalar(select(ReplayPlayer).where(ReplayPlayer.public_id == other_player))
        assert row is not None and other_replay_row is not None and other_player_row is not None
        result_evidence = session.get(EvidenceItem, row.evidence_item_id)
        assert result_evidence is not None
        if poison_target == "result_replay":
            result_evidence.replay_id = other_replay_row.id
        elif poison_target == "assessment_replay":
            row.replay_id = other_replay_row.id
        else:
            row.replay_player_id = other_player_row.id
        session.commit()

    with pytest.raises(ValueError, match="cache|ownership|replay|tier"):
        service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)


@pytest.mark.parametrize("poison_target", ("link_replay", "link_role"))
def test_named_cache_rejects_link_ownership_or_role_poisoning(
    strategy_factory: sessionmaker[Session],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
    poison_target: str,
) -> None:
    service = _named_service(strategy_factory, taxonomy_resource)
    service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    other_replay = "00000000-0000-4000-8000-000000000401"
    other_player = "00000000-0000-4000-8000-000000000402"
    _seed_feature_set(
        strategy_factory,
        replay_public_id=other_replay,
        replay_sha256="9" * 64,
        player_public_id=other_player,
        feature_set_public_id="00000000-0000-4000-8000-000000000403",
    )
    with strategy_factory() as session:
        row = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "rule"))
        other_replay_row = session.scalar(select(Replay).where(Replay.public_id == other_replay))
        assert row is not None and other_replay_row is not None
        link = session.scalar(
            select(AssessmentEvidence)
            .join(EvidenceItem, EvidenceItem.id == AssessmentEvidence.evidence_item_id)
            .where(AssessmentEvidence.assessment_id == row.id)
            .where(EvidenceItem.source_kind == "catalog" if poison_target == "link_replay" else True)
            .order_by(AssessmentEvidence.evidence_item_id)
        )
        assert link is not None
        if poison_target == "link_replay":
            evidence = session.get(EvidenceItem, link.evidence_item_id)
            assert evidence is not None
            evidence.replay_id = other_replay_row.id
        else:
            link.role = "contradicting" if link.role == "supporting" else "supporting"
        session.commit()

    with pytest.raises(ValueError, match="cache|ownership|replay|evidence graph"):
        service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)


def test_concurrent_requests_recover_the_single_persisted_winner(strategy_factory: sessionmaker[Session]) -> None:
    _seed_feature_set(strategy_factory)

    def assess() -> object:
        return StrategyAssessmentService(strategy_factory).assess_rule_candidates(
            REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(lambda _: assess(), range(2)))

    assert receipts[0] == receipts[1]
    assert _counts(strategy_factory) == (1, 1, 0, 0)


def test_named_concurrent_requests_recover_one_complete_linked_graph(
    strategy_factory: sessionmaker[Session],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    service = _named_service(strategy_factory, taxonomy_resource)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(
            pool.map(
                lambda _: service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY),
                range(2),
            )
        )

    assert receipts[0] == receipts[1]
    assert _counts(strategy_factory) == (1, 1, 10, 0)


def test_one_hundred_feature_and_evidence_permutations_reuse_the_exact_persisted_named_graph(
    strategy_factory: sessionmaker[Session],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    service = _named_service(
        strategy_factory,
        taxonomy_resource,
        service_type=_PermutingApplicabilityService,
    )
    _add_secondary_feature_inputs(strategy_factory)

    receipts = tuple(service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY) for _ in range(100))

    assert all(receipt == receipts[0] for receipt in receipts)
    assert _counts(strategy_factory) == (1, 1, 13, 0)
    with strategy_factory() as session:
        row = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "rule"))
        assert row is not None
        graph = tuple(
            session.execute(
                select(AssessmentEvidence.role, EvidenceItem.source_kind, EvidenceItem.source_key)
                .join(EvidenceItem, EvidenceItem.id == AssessmentEvidence.evidence_item_id)
                .where(AssessmentEvidence.assessment_id == row.id)
                .order_by(
                    AssessmentEvidence.role,
                    EvidenceItem.source_kind,
                    EvidenceItem.source_key,
                    EvidenceItem.public_id,
                )
            ).all()
        )
    assert len(graph) == 13
    assert graph == tuple(sorted(graph))


def test_failed_graph_insert_rolls_back_all_new_rows(
    strategy_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_feature_set(strategy_factory)
    service = StrategyAssessmentService(strategy_factory)

    def fail_after_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("fixture insert failure")

    monkeypatch.setattr(service, "_after_graph_insert", fail_after_insert)

    with pytest.raises(RuntimeError, match="fixture insert failure"):
        service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    assert _counts(strategy_factory) == (0, 0, 0, 0)


def test_failed_named_graph_insert_rolls_back_result_assessment_and_all_links(
    strategy_factory: sessionmaker[Session],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _named_service(strategy_factory, taxonomy_resource)

    def fail_after_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("named graph insert failure")

    monkeypatch.setattr(service, "_after_graph_insert", fail_after_insert)

    with pytest.raises(RuntimeError, match="named graph insert failure"):
        service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    assert _counts(strategy_factory) == (0, 0, 0, 0)


def test_manual_rows_do_not_enter_rule_cache_identity(strategy_factory: sessionmaker[Session]) -> None:
    _seed_feature_set(strategy_factory)
    service = StrategyAssessmentService(strategy_factory)
    first = service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    now = datetime(2026, 8, 22, tzinfo=UTC)
    with strategy_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY))
        player = session.scalar(select(ReplayPlayer).where(ReplayPlayer.public_id == PLAYER))
        assert replay is not None and player is not None
        evidence = EvidenceItem(
            public_id="00000000-0000-4000-8000-000000000390",
            replay_id=replay.id,
            tier="derived",
            source_kind="manual",
            source_key="manual:fixture",
            schema_version=1,
            created_at=now,
        )
        session.add(evidence)
        session.flush()
        session.add(
            StrategyAssessment(
                public_id="00000000-0000-4000-8000-000000000391",
                evidence_item_id=evidence.id,
                replay_id=replay.id,
                replay_player_id=player.id,
                method="manual",
                strategy_label="human_label",
                phase="cross_phase",
                taxonomy_version=None,
                rule_version=None,
                model_version=None,
                frame_start=0,
                frame_end=300,
                quality="available",
                confidence=None,
                details_json={"reason": "human correction"},
                created_at=now,
            )
        )
        session.commit()

    second = service.assess_rule_candidates(REPLAY, PLAYER, (FEATURE_SET,), BASE_REGISTRY)
    assert second == first
    with strategy_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(StrategyAssessment).where(StrategyAssessment.method == "rule")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(StrategyAssessment).where(StrategyAssessment.method == "manual")
            )
            == 1
        )
