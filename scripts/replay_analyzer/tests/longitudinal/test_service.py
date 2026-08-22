from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
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


def test_service_persists_exact_public_member_graph_and_returns_idempotent_receipt(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    service = LongitudinalAnalysisService(longitudinal_factory)

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
    service = LongitudinalAnalysisService(longitudinal_factory)
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
        return LongitudinalAnalysisService(longitudinal_factory).analyze(request)

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
        LongitudinalAnalysisService(longitudinal_factory).analyze(_request(player_public_id))
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

    receipt = LongitudinalAnalysisService(longitudinal_factory).analyze(_request(player_public_id))
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
    receipt = LongitudinalAnalysisService(longitudinal_factory).analyze(request)
    assert receipt.results[0].quality == "partial"
    assert receipt.results[0].members[0].quality == "partial"
    assert receipt.results[0].members[0].active_issue_codes == ("crc_mismatch",)


def test_unsupported_opponent_and_strategy_provenance_fail_closed_with_typed_reasons(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    service = LongitudinalAnalysisService(longitudinal_factory)
    opponent_request = replace(
        _request(player_public_id),
        segment=SegmentKey(opponent_player_public_id="00000000-0000-4000-8000-000000009999"),
    )
    with pytest.raises(LongitudinalAnalysisError, match="unsupported_team_opponent_relation"):
        service.analyze(opponent_request)

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
    with pytest.raises(LongitudinalAnalysisError, match="ambiguous_feature_set_provenance"):
        service.analyze(strategy_request)


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
def test_exact_segment_filters_never_create_an_empty_successful_graph(
    longitudinal_factory: sessionmaker[Session], seed_corpus: object, segment: SegmentKey
) -> None:
    player_public_id = seed_corpus()  # type: ignore[operator]
    request = replace(_request(player_public_id), segment=segment)
    with pytest.raises(LongitudinalAnalysisError, match="no_eligible_measurements"):
        LongitudinalAnalysisService(longitudinal_factory).analyze(request)
    with longitudinal_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongitudinalRun)) == 0


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
    receipt = LongitudinalAnalysisService(longitudinal_factory).analyze(request)
    result = receipt.results[0]
    assert result.result_kind == "pattern"
    assert result.statistics["recurring_prefix"] == [
        "ChinaPowerPlant",
        "ChinaBarracks",
        "ChinaSupplyCenter",
    ]
    assert result.statistics["algorithm_version"] == "recurring-opening-prefix-wilson-v1"
    assert result.statistics["confidence_level"] == 0.95
