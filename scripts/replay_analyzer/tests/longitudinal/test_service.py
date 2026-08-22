from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    Feature,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    Player,
    Replay,
    ReplayQualityIssue,
)
from generals_replay_analyzer.identity.audit import identity_cache_digest
from generals_replay_analyzer.longitudinal.segments import (
    LongitudinalExclusionDTO,
    LongitudinalRequest,
    LongitudinalSettings,
    QualityPolicy,
    SegmentKey,
)
from generals_replay_analyzer.longitudinal.service import LongitudinalAnalysisError, LongitudinalAnalysisService

from .conftest import PLAYER_PUBLIC_ID


def _request(player_public_id: str, **settings_changes: object) -> LongitudinalRequest:
    settings = LongitudinalSettings(
        minimum_sample_size=2,
        bootstrap_resamples=100,
        confidence_level=0.95,
        enabled_metrics=("economy.cash_change_total",),
    )
    if settings_changes:
        settings = replace(settings, **settings_changes)
    return LongitudinalRequest(
        player_public_id=player_public_id,
        segment=SegmentKey(subject_faction="China", subject_subfaction="Tank", replay_version="1.04", replay_patch="retail-1.04"),
        metric_names=("economy.cash_change_total",),
        pattern_names=(),
        settings=settings,
    )


def _service(
    factory: sessionmaker[Session], *, minimum: int = 2
) -> LongitudinalAnalysisService:
    database = Path(str(factory.kw["bind"].url.database))
    return LongitudinalAnalysisService(
        factory,
        analyzer_settings=AnalyzerSettings(
            data_root=database.parent / "longitudinal-service-settings",
            minimum_longitudinal_sample_size=minimum,
        ),
    )


def test_service_persists_exact_public_member_graph_and_returns_idempotent_receipt(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    service = _service(longitudinal_factory)

    receipt = service.analyze(_request(player_public_id))
    again = service.analyze(_request(player_public_id))

    assert again == receipt
    assert receipt.status == "succeeded"
    assert receipt.identity_revision == 0
    assert receipt.results[0].sample_count == 3
    assert receipt.results[0].missing_count == 0
    assert receipt.results[0].statistics["median"] == 10.0
    assert len(receipt.results[0].members) == 3
    assert all(member.feature_public_id and member.strategy_assessment_public_id is None for member in receipt.results[0].members)
    assert all("database_id" not in key and key != "id" for key in _all_keys(asdict(receipt)))

    with longitudinal_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongitudinalRun)) == 1
        assert session.scalar(select(func.count()).select_from(LongitudinalResult)) == 1
        assert session.scalar(select(func.count()).select_from(LongitudinalMember)) == 3
        run = session.scalar(select(LongitudinalRun))
        assert run is not None and run.identity_revision == 0 and run.status == "succeeded"


def _all_keys(value: object) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(value) + tuple(key for item in value.values() for key in _all_keys(item))
    if isinstance(value, (list, tuple)):
        return tuple(key for item in value for key in _all_keys(item))
    return ()


def test_identity_revision_creates_new_cache_and_preserves_old_graph_byte_for_byte(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    service = _service(longitudinal_factory)
    old = service.analyze(_request(player_public_id))
    with longitudinal_factory() as session:
        before = {
            table: tuple(session.execute(text(f"SELECT * FROM {table} ORDER BY id")).all())
            for table in ("longitudinal_runs", "longitudinal_results", "longitudinal_members")
        }
        player = session.scalar(select(Player).where(Player.public_id == player_public_id))
        assert player is not None
        player.identity_revision = 1
        session.commit()

    new = service.analyze(_request(player_public_id))
    assert new.identity_revision == 1
    assert new.cache_key != old.cache_key
    with longitudinal_factory() as session:
        after = {
            table: tuple(session.execute(text(f"SELECT * FROM {table} WHERE id <= :limit ORDER BY id"), {"limit": len(rows)}).all())
            for table, rows in before.items()
        }
    assert after == before
    assert identity_cache_digest(PLAYER_PUBLIC_ID, 0) != identity_cache_digest(PLAYER_PUBLIC_ID, 1)


def test_concurrent_same_key_returns_one_successful_winner(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    request = _request(player_public_id)

    def analyze() -> object:
        return _service(longitudinal_factory).analyze(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(lambda _: analyze(), range(2)))
    assert receipts[0] == receipts[1]
    with longitudinal_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongitudinalRun).where(LongitudinalRun.status == "succeeded")) == 1


def test_failed_graph_insert_rolls_back_children_and_retains_typed_failed_diagnostic(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    with longitudinal_factory().bind.begin() as connection:  # type: ignore[union-attr]
        connection.execute(text("CREATE TRIGGER task9_abort_member BEFORE INSERT ON longitudinal_members BEGIN SELECT RAISE(ABORT, 'forced task9 failure'); END"))
    with pytest.raises(LongitudinalAnalysisError, match="persistence_failed"):
        _service(longitudinal_factory).analyze(_request(player_public_id))
    with longitudinal_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongitudinalResult)) == 0
        assert session.scalar(select(func.count()).select_from(LongitudinalMember)) == 0
        failed = session.scalars(select(LongitudinalRun).where(LongitudinalRun.status == "failed")).all()
        assert len(failed) == 1
        assert failed[0].error_json == {"code": "persistence_failed"}


def test_wrong_feature_contract_is_missing_not_a_fabricated_measurement(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    with longitudinal_factory() as session:
        feature = session.scalars(select(Feature).order_by(Feature.id)).first()
        assert feature is not None
        feature.unit = "seconds"
        session.commit()

    receipt = _service(longitudinal_factory).analyze(_request(player_public_id))
    result = receipt.results[0]
    assert result.sample_count == 2
    assert result.missing_count == 1
    assert result.quality == "partial"
    assert result.members[0].raw_value is None
    assert result.members[0].reason == "unsupported_metric_definition"


def test_explicit_quality_issue_opt_in_is_retained_and_downgrades_member_quality(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    with longitudinal_factory() as session:
        replay = session.scalars(select(Replay).order_by(Replay.id)).first()
        assert replay is not None
        session.add(
            ReplayQualityIssue(
                public_id="00000000-0000-4000-8000-000000009500",
                replay_id=replay.id,
                stage="telemetry",
                issue_code="crc_mismatch",
                severity="error",
                details_json={},
            )
        )
        session.commit()
    request = replace(
        _request(player_public_id),
        segment=SegmentKey(
            subject_faction="China",
            subject_subfaction="Tank",
            replay_version="1.04",
            replay_patch="retail-1.04",
            quality_policy=QualityPolicy(
                quality_floor="partial",
                allowed_lifecycle_states=("engine_verified",),
                include_issue_codes=("crc_mismatch",),
            ),
        ),
    )
    receipt = _service(longitudinal_factory).analyze(request)
    assert receipt.results[0].quality == "partial"
    assert receipt.results[0].members[0].quality == "partial"
    assert receipt.results[0].members[0].active_issue_codes == ("crc_mismatch",)


def test_unsupported_opponent_and_strategy_provenance_persist_typed_unavailable_results(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    service = _service(longitudinal_factory)
    opponent_request = replace(
        _request(player_public_id),
        segment=SegmentKey(opponent_player_public_id="00000000-0000-4000-8000-000000009999"),
    )
    opponent = service.analyze(opponent_request)
    assert opponent.results[0].quality == "unavailable"
    assert opponent.results[0].reason == "unsupported_team_opponent_relation"

    strategy_request = replace(
        _request(player_public_id),
        metric_names=(),
        pattern_names=("transition_preferences",),
        settings=replace(
            _request(player_public_id).settings,
            enabled_metrics=(),
            enabled_patterns=("transition_preferences",),
        ),
    )
    strategy = service.analyze(strategy_request)
    assert strategy.results[0].quality == "unavailable"
    assert strategy.results[0].reason == "minimum_sample_not_met"


@pytest.mark.parametrize(
    "segment",
    [
        SegmentKey(subject_faction="USA"),
        SegmentKey(subject_subfaction="Nuke"),
        SegmentKey(map_public_id="00000000-0000-4000-8000-000000009700"),
        SegmentKey(start_position=9),
        SegmentKey(replay_version="1.08"),
        SegmentKey(replay_patch="community-patch"),
        SegmentKey(start_inclusive=1_800_000_000, end_exclusive=1_800_000_100),
    ],
)
def test_exact_segment_filters_persist_explicit_unavailable_graph(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, segment: SegmentKey
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    request = replace(_request(player_public_id), segment=segment)
    receipt = _service(longitudinal_factory).analyze(request)
    assert receipt.results[0].quality == "unavailable"
    assert receipt.results[0].reason == "minimum_sample_not_met"
    with longitudinal_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongitudinalRun)) == 1


def test_service_persists_recurring_opening_with_versioned_interval_metadata(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus(include_build_sequences=True)  # type: ignore[operator]
    base = _request(player_public_id)
    request = replace(
        base,
        metric_names=(),
        pattern_names=("recurring_opening",),
        settings=replace(
            base.settings,
            enabled_metrics=(),
            enabled_patterns=("recurring_opening",),
        ),
    )
    receipt = _service(longitudinal_factory).analyze(request)
    result = receipt.results[0]
    assert result.result_kind == "pattern"
    assert result.statistics["recurring_prefix"] == (
        "ChinaPowerPlant",
        "ChinaBarracks",
        "ChinaSupplyCenter",
    )
    assert result.statistics["algorithm_version"] == "recurring-opening-prefix-wilson-v1"
    assert result.statistics["confidence_level"] == 0.95


def _analyzer_settings(tmp_path: Path, minimum: int = 2) -> AnalyzerSettings:
    root = tmp_path / "external-analyzer-data"
    root.mkdir()
    return AnalyzerSettings(data_root=root, minimum_longitudinal_sample_size=minimum)


def test_service_binds_minimum_to_analyzer_settings(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, tmp_path: Path
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    service = LongitudinalAnalysisService(
        longitudinal_factory,
        analyzer_settings=_analyzer_settings(tmp_path, minimum=5),
    )
    with pytest.raises(LongitudinalAnalysisError, match="minimum_sample_size_mismatch"):
        service.analyze(_request(player_public_id))


def test_every_explicit_pattern_gets_one_persisted_result_instead_of_aborting(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, tmp_path: Path
) -> None:
    player_public_id = seed_corpus((1.0, 2.0, 3.0, 10.0, 11.0, 12.0), include_build_sequences=True)  # type: ignore[operator]
    patterns = tuple(
        sorted(
            (
                "change_point.economy_cash_change_total",
                "consistency.economy_cash_change_total",
                "map_position_habits",
                "opponent_associated.economy_cash_change_total",
                "personal_baseline.economy_cash_change_total",
                "recurring_opening",
                "timing_band.build_first_completed",
                "transition_preferences",
                "trend.economy_cash_change_total",
            )
        )
    )
    request = LongitudinalRequest(
        player_public_id=player_public_id,
        segment=SegmentKey(subject_faction="China"),
        metric_names=(),
        pattern_names=patterns,
        settings=LongitudinalSettings(
            minimum_sample_size=2,
            bootstrap_resamples=30,
            confidence_level=0.9,
            enabled_patterns=patterns,
        ),
    )
    receipt = LongitudinalAnalysisService(
        longitudinal_factory,
        analyzer_settings=_analyzer_settings(tmp_path),
    ).analyze(request)
    assert tuple(result.result_name for result in receipt.results) == patterns
    with longitudinal_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongitudinalResult)) == len(patterns)


def test_unknown_definition_persists_typed_unavailable_result_using_corpus_anchor(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, tmp_path: Path
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    request = LongitudinalRequest(
        player_public_id=player_public_id,
        segment=SegmentKey(subject_faction="China"),
        metric_names=("unknown.metric",),
        pattern_names=(),
        settings=LongitudinalSettings(
            minimum_sample_size=2,
            bootstrap_resamples=20,
            confidence_level=0.9,
            enabled_metrics=("unknown.metric",),
        ),
    )
    receipt = LongitudinalAnalysisService(
        longitudinal_factory,
        analyzer_settings=_analyzer_settings(tmp_path),
    ).analyze(request)
    assert len(receipt.results) == 1
    assert receipt.results[0].quality == "unavailable"
    assert receipt.results[0].reason == "unsupported_metric_definition"
    assert receipt.results[0].members == ()
    assert receipt.results[0].statistics["evidence_anchor_role"] == "schema_required_corpus_anchor"


def test_member_semantics_and_historical_issue_snapshot_are_persisted_immutably(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, tmp_path: Path
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    with longitudinal_factory() as session:
        replay = session.scalars(select(Replay).order_by(Replay.id)).first()
        assert replay is not None
        issue = ReplayQualityIssue(
            public_id="00000000-0000-4000-8000-000000009800",
            replay_id=replay.id,
            stage="telemetry",
            issue_code="crc_mismatch",
            severity="error",
            details_json={"source": "crc"},
        )
        session.add(issue)
        session.commit()
    request = replace(
        _request(player_public_id),
        segment=SegmentKey(
            quality_policy=QualityPolicy(
                quality_floor="partial",
                allowed_lifecycle_states=("engine_verified",),
                include_issue_codes=("crc_mismatch",),
            )
        ),
    )
    service = LongitudinalAnalysisService(
        longitudinal_factory,
        analyzer_settings=_analyzer_settings(tmp_path),
    )
    receipt = service.analyze(request)
    member = receipt.results[0].members[0]
    assert member.feature_set_extractor_name == "economy"
    assert member.feature_set_extractor_version == "economy-v1"
    assert member.feature_set_input_digest
    assert member.feature_value_type == "real"
    assert member.feature_scope_type == "player"
    assert member.derived_evidence_source_kind == "feature"
    assert member.derived_evidence_source_key.startswith("feature:")
    assert len(member.direct_evidence) == 1
    assert member.quality_issues[0].issue_code == "crc_mismatch"
    assert member.quality_issues[0].resolved is False
    with longitudinal_factory() as session:
        stored = session.scalar(select(LongitudinalResult))
        assert stored is not None
        assert stored.statistics_json["storage_schema"] == "longitudinal-result-storage-v1"
        issue = session.scalar(select(ReplayQualityIssue))
        assert issue is not None
        issue.resolved_at = datetime(2026, 8, 22, tzinfo=UTC)
        feature = session.scalar(select(Feature).order_by(Feature.id))
        assert feature is not None
        feature.details_json = {"mutated": True}
        session.commit()
    historical = service._load_receipt(receipt.cache_key)
    assert historical == receipt


def test_aggregate_evidence_uses_explicit_corpus_anchor_metadata(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, tmp_path: Path
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    receipt = LongitudinalAnalysisService(
        longitudinal_factory,
        analyzer_settings=_analyzer_settings(tmp_path),
    ).analyze(_request(player_public_id))
    with longitudinal_factory() as session:
        evidence = session.scalar(select(EvidenceItem).where(EvidenceItem.public_id == receipt.results[0].evidence_public_id))
        assert evidence is not None
        assert evidence.source_kind == "longitudinal_corpus"
        stored = session.scalar(select(LongitudinalResult))
        assert stored is not None
        assert stored.statistics_json["evidence_anchor"]["role"] == "schema_required_corpus_anchor"


def test_one_hundred_selection_permutations_share_exact_cache_member_and_persistence_graph(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    player_public_id = seed_corpus((1.0, 2.0, 3.0, 4.0, 5.0))  # type: ignore[operator]
    request = _request(player_public_id)
    service = _service(longitudinal_factory)
    original_select = service._select
    baseline = None
    for seed in range(100):
        def permuted_select(
            current_request: LongitudinalRequest,
            definitions: object,
            *,
            permutation_seed: int = seed,
        ) -> object:
            player_id, revision, members, anchors, exclusions = original_select(  # type: ignore[arg-type]
                current_request, definitions
            )
            shuffled = list(members)
            random.Random(permutation_seed).shuffle(shuffled)
            return player_id, revision, tuple(shuffled), anchors, exclusions

        monkeypatch.setattr(service, "_select", permuted_select)
        receipt = service.analyze(request)
        baseline = receipt if baseline is None else baseline
        assert receipt == baseline
    with longitudinal_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongitudinalRun).where(LongitudinalRun.status == "succeeded")) == 1
        assert session.scalar(select(func.count()).select_from(LongitudinalMember)) == 5


def test_missing_chronology_is_persisted_in_exclusion_ledger_and_cache_context(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    player_public_id = seed_corpus((1.0, 2.0, 3.0))  # type: ignore[operator]
    pattern_names = ("trend.economy_cash_change_total",)
    request = LongitudinalRequest(
        player_public_id=player_public_id,
        segment=SegmentKey(subject_faction="China"),
        metric_names=(),
        pattern_names=pattern_names,
        settings=LongitudinalSettings(
            minimum_sample_size=2,
            bootstrap_resamples=30,
            confidence_level=0.9,
            enabled_patterns=pattern_names,
        ),
    )
    service = _service(longitudinal_factory)
    original_select = service._select

    def select_with_missing_time(current_request: LongitudinalRequest, definitions: object) -> object:
        player_id, revision, members, anchors, exclusions = original_select(  # type: ignore[arg-type]
            current_request, definitions
        )
        missing = replace(members[0], dto=replace(members[0].dto, replay_start_time=None))
        exclusion = LongitudinalExclusionDTO(
            missing.dto.replay_public_id,
            missing.dto.replay_player_public_id,
            "missing_replay_start_time",
            (None, missing.dto.replay_sha256, missing.dto.replay_player_public_id),
        )
        return player_id, revision, (missing, *members[1:]), anchors, (*exclusions, exclusion)

    monkeypatch.setattr(service, "_select", select_with_missing_time)
    receipt = service.analyze(request)
    with longitudinal_factory() as session:
        run = session.scalar(select(LongitudinalRun).where(LongitudinalRun.cache_key == receipt.cache_key))
        assert run is not None
        exclusions = run.settings_json["exclusions"]
        assert len(exclusions) == 1
        assert exclusions[0]["reason"] == "missing_replay_start_time"


def test_duplicate_direct_evidence_and_duplicate_result_members_are_rejected(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    result = _service(longitudinal_factory).analyze(_request(player_public_id)).results[0]
    member = result.members[0]
    with pytest.raises(ValueError, match="duplicate source evidence"):
        replace(member, direct_evidence=(member.direct_evidence[0], member.direct_evidence[0]))
    with pytest.raises(ValueError, match="duplicate pattern members"):
        replace(result, members=(member, member))
