"""Exact-binding comparison calculations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from generals_replay_analyzer.comparison.service import (
    ComparisonDefinition,
    ComparisonFilters,
    ComparisonInput,
    ComparisonSelection,
    ComparisonValue,
    ReplayComparisonService,
)
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    Feature,
    FeatureSet,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    ParserRun,
    Player,
    Replay,
    ReplayPlayer,
    Report,
    StrategyAssessment,
)


def _id(value: int) -> str:
    return str(UUID(int=value))


def _value(raw: float | None, *, unit: str = "frames", sample_count: int = 5) -> ComparisonValue:
    return ComparisonValue(
        raw_value=raw,
        unit=unit,
        sample_count=sample_count,
        missing_count=0,
        availability="available" if raw is not None else "unavailable",
        evidence_public_ids=(),
    )


def _add_cross_faction_match_subject(
    session: Session,
    *,
    ordinal: int,
    faction: str,
    now: datetime,
) -> str:
    base = 800 + ordinal * 50
    player = Player(
        public_id=_id(base),
        display_name=f"Cross faction {ordinal}",
        identity_revision=1,
        updated_at=now,
        created_at=now,
    )
    session.add(player)
    session.flush()
    replay = Replay(
        public_id=_id(base + 1),
        sha256=str(ordinal + 1) * 64,
        replay_name="hidden.rep",
        version_string="1.04",
        version_number=104,
        frame_count=100,
        start_time=1_700_010_000 + ordinal * 100,
        end_time=1_700_010_100 + ordinal * 100,
        exe_crc=1,
        ini_crc=2,
        map_crc=3,
        map_name="TD",
        seed=4,
        header_json={"patch_identity": "1.04"},
        lifecycle_state="engine_verified",
        updated_at=now,
        created_at=now,
    )
    session.add(replay)
    session.flush()
    parser = ParserRun(
        run_id=_id(base + 2),
        replay_id=replay.id,
        parser_version="v1",
        schema_version=1,
        input_sha256=str(ordinal + 3) * 64,
        result_sha256=str(ordinal + 5) * 64,
        status="running",
        completion_status="complete",
        command_stream_offset=1,
        end_offset=2,
        warnings_json=[],
        started_at=now,
        completed_at=now,
    )
    session.add(parser)
    session.flush()
    replay_player = ReplayPlayer(
        public_id=_id(base + 3),
        replay_id=replay.id,
        parser_run_id=parser.id,
        player_id=player.id,
        slot_index=0,
        slot_kind="human",
        original_name=player.display_name,
        faction=faction,
        result="win" if ordinal == 0 else "loss",
        observed_json={},
    )
    session.add(replay_player)
    session.flush()
    feature_set = FeatureSet(
        public_id=_id(base + 4),
        replay_id=replay.id,
        replay_player_id=replay_player.id,
        extractor_name="fixture",
        extractor_version="v1",
        input_digest=str(ordinal + 6) * 64,
        cache_key=str(ordinal + 7) * 64,
        status="running",
        settings_json={},
        completed_at=now,
        created_at=now,
    )
    session.add(feature_set)
    session.flush()
    for offset, (name, value, unit) in enumerate(
        (
            ("economy.cash_change_total", float(-300 - ordinal * 200), "credits"),
            ("activity.supported_order_action_count", float(20 + ordinal), "count"),
        )
    ):
        evidence = EvidenceItem(
            public_id=_id(base + 10 + offset),
            replay_id=replay.id,
            tier="derived",
            source_kind="fixture",
            source_key=f"cross-faction:{ordinal}:{offset}",
            schema_version=1,
            created_at=now,
        )
        session.add(evidence)
        session.flush()
        session.add(
            Feature(
                public_id=_id(base + 20 + offset),
                feature_set_id=feature_set.id,
                evidence_item_id=evidence.id,
                name=name,
                value_type="real",
                real_value=value,
                unit=unit,
                scope_type="player",
                scope_key=replay_player.public_id,
                replay_player_id=replay_player.id,
                frame_start=0,
                frame_end=100,
                quality="available",
                details_json={},
            )
        )
    feature_set.status = "succeeded"
    report = Report(
        public_id=_id(base + 30),
        replay_id=replay.id,
        replay_player_id=replay_player.id,
        report_version="replay-report-v1",
        input_digest=str(ordinal + 8) * 64,
        cache_key=("9" if ordinal == 0 else "a") * 64,
        report_json={"schema_version": "replay-report-v1"},
        created_at=now,
    )
    session.add(report)
    parser.status = "succeeded"
    return report.public_id


def test_exact_aligned_values_produce_a_service_owned_difference() -> None:
    """Catch numerical deltas moving into routes/templates or reversing left/right order."""
    definition = ComparisonDefinition("first_unit", "v1", "frames", "player", "window-v1", "same_faction_only")
    result = ReplayComparisonService.compare_values(
        ComparisonInput("players", definition, _value(100.0), _value(125.0), 3, True)
    )
    assert result.state == "comparable"
    assert result.derived_difference == -25.0


def test_mismatch_and_small_samples_never_emit_an_unaligned_delta() -> None:
    """Catch fake bars/deltas for unit/version or minimum-sample mismatches."""
    definition = ComparisonDefinition("first_unit", "v1", "frames", "player", "window-v1", "same_faction_only")
    small = ReplayComparisonService.compare_values(
        ComparisonInput("players", definition, _value(100.0, sample_count=2), _value(125.0), 3, True)
    )
    assert small.state == "not_comparable"
    assert small.derived_difference is None
    assert small.reason_codes == ("minimum_sample_size_not_met",)
    mismatch = ReplayComparisonService.compare_values(
        ComparisonInput("players", definition, _value(100.0, unit="frames"), _value(2.0, unit="seconds"), 3, True)
    )
    assert mismatch.state == "not_comparable"
    assert mismatch.derived_difference is None
    assert mismatch.reason_codes == ("unit_mismatch",)


def test_unsupported_time_source_is_explicitly_unavailable() -> None:
    """Catch filesystem time or browser clock being substituted for proven UTC replay starts."""
    definition = ComparisonDefinition("cash", "v1", "credits", "player", "window-v1", "same_faction_only")
    result = ReplayComparisonService.compare_values(
        ComparisonInput("time_periods", definition, _value(10.0), _value(8.0), 3, False)
    )
    assert result.state == "unavailable"
    assert result.derived_difference is None
    assert result.reason_codes == ("utc_source_unproven",)


def test_incomplete_selection_keeps_the_requested_mode_explicitly_unavailable() -> None:
    """Catch comparison modes disappearing or selecting fabricated fixture bindings."""
    result = ReplayComparisonService().resolve(
        ComparisonSelection("strategies", None, None, False, ("strategy.fast_tech",), 3)
    )
    assert result.state == "unavailable"
    assert result.fixed_query is None
    assert result.reason_codes == ("comparison_subject_incomplete",)


def test_selection_configuration_and_run_filters_fail_closed() -> None:
    """Cover every accepted segment field without substituting a nearby cohort."""
    service = ReplayComparisonService()
    assert service.resolve(ComparisonSelection("players", _id(1), _id(2), False, (), 3)).reason_codes == (
        "comparison_metrics_required",
    )
    assert service.resolve(ComparisonSelection("players", _id(1), _id(2), True, ("cash",), 3)).reason_codes == (
        "comparison_repository_unavailable",
    )
    assert service.resolve(ComparisonSelection("players", _id(1), _id(2), False, ("cash",), 2)).reason_codes == (
        "minimum_sample_size_not_accepted",
    )
    quality = {"quality_floor": "partial"}
    quality_digest = hashlib.sha256(json.dumps(quality, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    run = LongitudinalRun(
        run_id=_id(3),
        player_id=1,
        identity_revision=1,
        analyzer_name="analyzer",
        analyzer_version="v1",
        segment_key_json={
            "subject_faction": "USA",
            "subject_subfaction": "Laser",
            "opponent_faction": "GLA",
            "opponent_player_public_id": _id(4),
            "map_public_id": _id(5),
            "start_position": 1,
            "replay_patch": "1.04",
            "start_inclusive": 1_700_000_000,
            "end_exclusive": 1_700_000_100,
            "quality_policy": quality,
        },
        settings_json={},
        input_digest="a" * 64,
        cache_key="b" * 64,
        status="succeeded",
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
    )
    filters = ComparisonFilters(
        faction="USA",
        subfaction="Laser",
        opponent_faction="GLA",
        opponent_player_public_id=_id(4),
        map_public_id=_id(5),
        start_position="1",
        patch="1.04",
        date_from_utc=datetime.fromtimestamp(1_700_000_000, UTC),
        date_to_utc=datetime.fromtimestamp(1_700_000_100, UTC),
        quality_policy_digest=quality_digest,
    )
    assert service._run_matches_filters(run, filters) is True
    assert service._run_matches_filters(run, replace(filters, start_position="invalid")) is False
    assert service._run_matches_filters(run, replace(filters, start_position="2")) is False
    non_utc = timezone(timedelta(hours=2))
    assert (
        service._run_matches_filters(
            run,
            replace(filters, date_from_utc=datetime(2023, 11, 14, tzinfo=non_utc)),
        )
        is False
    )
    assert (
        service._run_matches_filters(
            run,
            replace(filters, date_to_utc=datetime(2023, 11, 14, tzinfo=non_utc)),
        )
        is False
    )


def test_player_selection_resolves_complete_immutable_longitudinal_bindings(tmp_path: Path) -> None:
    """Catch selector resolution returning IDs without version/digest/algorithm bindings."""
    path = tmp_path / "comparison.sqlite3"
    upgrade_database(path)
    engine = create_database_engine(path)
    factory = create_session_factory(engine)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    try:
        with factory.begin() as session:
            players = (
                Player(public_id=_id(1), display_name="Alpha", identity_revision=2, updated_at=now, created_at=now),
                Player(public_id=_id(2), display_name="Bravo", identity_revision=4, updated_at=now, created_at=now),
            )
            session.add_all(players)
            session.flush()
            for index, player in enumerate(players, start=10):
                session.add(
                    LongitudinalRun(
                        run_id=_id(index),
                        player_id=player.id,
                        identity_revision=player.identity_revision,
                        analyzer_name="longitudinal-player-analysis",
                        analyzer_version="v1",
                        segment_key_json={
                            "schema_version": "longitudinal-segment-v1",
                            "quality_policy": {"quality_floor": "partial"},
                        },
                        settings_json={
                            "storage_schema": "longitudinal-run-settings-v1",
                            "definitions": [
                                {
                                    "public_name": "cash",
                                    "result_kind": "metric",
                                    "definition_version": "cash-v1",
                                    "unit": "credits",
                                    "scope_types": ["player"],
                                }
                            ],
                            "settings": {"bootstrap_algorithm_version": "median-bootstrap-v1"},
                        },
                        input_digest=str(index)[-1] * 64,
                        cache_key=str(index + 2)[-1] * 64,
                        status="succeeded",
                        created_at=now,
                        completed_at=now,
                    )
                )
        resolved = ReplayComparisonService(factory).resolve(
            ComparisonSelection("players", _id(1), _id(2), False, ("cash",), 3)
        )
        assert resolved.state == "resolved"
        assert resolved.fixed_query is not None
        assert resolved.fixed_query.left.segment_digest == resolved.fixed_query.left.segment_digest.lower()  # type: ignore[union-attr]
        assert resolved.fixed_query.left.statistics_algorithm_versions == ("median-bootstrap-v1",)  # type: ignore[union-attr]
        assert resolved.fixed_query.metric_definitions[0].definition_version == "cash-v1"
    finally:
        engine.dispose()


def test_match_comparison_declares_only_faction_neutral_economy_cross_faction(tmp_path: Path) -> None:
    path = tmp_path / "cross-faction.sqlite3"
    upgrade_database(path)
    engine = create_database_engine(path)
    factory = create_session_factory(engine)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    try:
        with factory.begin() as session:
            left_report = _add_cross_faction_match_subject(
                session,
                ordinal=0,
                faction="USA",
                now=now,
            )
            right_report = _add_cross_faction_match_subject(
                session,
                ordinal=1,
                faction="GLA",
                now=now,
            )
        service = ReplayComparisonService(factory, minimum_sample_size=1)
        economy = service.resolve(
            ComparisonSelection(
                "matches",
                left_report,
                right_report,
                False,
                ("economy.cash_change_total",),
                1,
            )
        )
        assert economy.state == "resolved", economy.reason_codes
        assert economy.fixed_query is not None
        assert economy.fixed_query.metric_definitions[0].faction_comparability == "declared_cross_faction"
        economy_result = service.compare(economy.fixed_query)
        assert economy_result.state == "comparable"
        assert economy_result.metrics[0].value.derived_difference == 200.0

        activity = service.resolve(
            ComparisonSelection(
                "matches",
                left_report,
                right_report,
                False,
                ("activity.supported_order_action_count",),
                1,
            )
        )
        assert activity.state == "resolved"
        assert activity.fixed_query is not None
        assert activity.fixed_query.metric_definitions[0].faction_comparability == "same_faction_only"
        activity_result = service.compare(activity.fixed_query)
        assert activity_result.state == "not_comparable"
        assert activity_result.metrics[0].value.reason_codes == ("faction_mismatch",)
    finally:
        engine.dispose()


def test_all_five_modes_resolve_only_exact_accepted_rows_and_compare_in_service(tmp_path: Path) -> None:
    """Catch omitted modes, latest substitution, manual/LLM strategy facts, or route-owned calculations."""
    path = tmp_path / "all-modes.sqlite3"
    upgrade_database(path)
    engine = create_database_engine(path)
    factory = create_session_factory(engine)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    try:
        ids: dict[str, list[str]] = {"players": [], "reports": [], "openings": [], "strategies": [], "periods": []}
        with factory.begin() as session:
            players = [
                Player(
                    public_id=_id(100 + i), display_name=f"P{i}", identity_revision=1, updated_at=now, created_at=now
                )
                for i in range(2)
            ]
            session.add_all(players)
            session.flush()
            ids["players"] = [item.public_id for item in players]
            for i, player in enumerate(players):
                replay = Replay(
                    public_id=_id(110 + i),
                    sha256=str(i + 1) * 64,
                    replay_name="hidden.rep",
                    version_string="1.04",
                    version_number=104,
                    frame_count=100,
                    start_time=1_700_000_000 + i * 100,
                    end_time=1_700_000_100 + i * 100,
                    exe_crc=1,
                    ini_crc=2,
                    map_crc=3,
                    map_name="TD",
                    seed=4,
                    header_json={"patch_identity": "1.04"},
                    lifecycle_state="engine_verified",
                    updated_at=now,
                    created_at=now,
                )
                session.add(replay)
                session.flush()
                parser = ParserRun(
                    run_id=_id(120 + i),
                    replay_id=replay.id,
                    parser_version="v1",
                    schema_version=1,
                    input_sha256=str(i + 3) * 64,
                    result_sha256=str(i + 5) * 64,
                    status="running",
                    completion_status="complete",
                    command_stream_offset=1,
                    end_offset=2,
                    warnings_json=[],
                    started_at=now,
                    completed_at=now,
                )
                session.add(parser)
                session.flush()
                replay_player = ReplayPlayer(
                    public_id=_id(130 + i),
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    player_id=player.id,
                    slot_index=0,
                    slot_kind="human",
                    original_name=player.display_name,
                    faction="USA",
                    result="win",
                    observed_json={},
                )
                session.add(replay_player)
                session.flush()
                parser.status = "succeeded"
                evidence = EvidenceItem(
                    public_id=_id(140 + i),
                    replay_id=replay.id,
                    tier="derived",
                    source_kind="fixture",
                    source_key=f"feature:{i}",
                    schema_version=1,
                    created_at=now,
                )
                session.add(evidence)
                session.flush()
                feature_set = FeatureSet(
                    public_id=_id(150 + i),
                    replay_id=replay.id,
                    replay_player_id=replay_player.id,
                    extractor_name="fixture",
                    extractor_version="v1",
                    input_digest=str(i + 6) * 64,
                    cache_key=str(i + 7) * 64,
                    status="running",
                    settings_json={},
                    completed_at=now,
                    created_at=now,
                )
                session.add(feature_set)
                session.flush()
                feature = Feature(
                    public_id=_id(160 + i),
                    feature_set_id=feature_set.id,
                    evidence_item_id=evidence.id,
                    name="cash",
                    value_type="real",
                    real_value=float(10 + i),
                    unit="credits",
                    scope_type="player",
                    scope_key=replay_player.public_id,
                    replay_player_id=replay_player.id,
                    frame_start=0,
                    frame_end=100,
                    quality="available",
                    details_json={},
                )
                session.add(feature)
                feature_set.status = "succeeded"
                report = Report(
                    public_id=_id(170 + i),
                    replay_id=replay.id,
                    replay_player_id=replay_player.id,
                    report_version=f"report-v{i + 1}",
                    input_digest=("8" if i == 0 else "9") * 64,
                    cache_key=("a" if i == 0 else "b") * 64,
                    report_json={"schema_version": f"report-schema-v{i + 1}"},
                    created_at=now,
                )
                session.add(report)
                ids["reports"].append(report.public_id)
                run = LongitudinalRun(
                    run_id=_id(180 + i),
                    player_id=player.id,
                    identity_revision=1,
                    analyzer_name="longitudinal-player-analysis",
                    analyzer_version="v1",
                    segment_key_json={
                        "schema_version": "longitudinal-segment-v1",
                        "quality_policy": {"quality_floor": "partial"},
                    },
                    settings_json={
                        "definitions": [
                            {
                                "public_name": "cash",
                                "result_kind": "metric",
                                "definition_version": "cash-v1",
                                "unit": "credits",
                                "scope_types": ["player"],
                            },
                            {
                                "public_name": "recurring_opening",
                                "result_kind": "pattern",
                                "definition_version": "opening-v1",
                                "unit": None,
                                "scope_types": ["player"],
                            },
                            {
                                "public_name": "strategy.fast_tech",
                                "result_kind": "pattern",
                                "definition_version": "strategy-v1",
                                "unit": None,
                                "scope_types": ["player"],
                                "taxonomy_version": "taxonomy-v1",
                            },
                        ],
                        "settings": {"bootstrap_algorithm_version": "median-bootstrap-v1"},
                    },
                    input_digest=str(i + 1) * 64,
                    cache_key=str(i + 2) * 64,
                    status="pending",
                    created_at=now,
                    completed_at=now,
                )
                session.add(run)
                session.flush()
                for offset, (name, kind, stats) in enumerate(
                    (
                        ("cash", "metric", {"value": float(10 + i)}),
                        ("recurring_opening", "pattern", {"share": 0.6 + i * 0.1}),
                        ("strategy.fast_tech", "pattern", {"share": 0.4 + i * 0.1}),
                    )
                ):
                    result_evidence = EvidenceItem(
                        public_id=_id(200 + i * 10 + offset),
                        replay_id=replay.id,
                        tier="derived",
                        source_kind="longitudinal_corpus",
                        source_key=f"long:{i}:{offset}",
                        schema_version=1,
                        created_at=now,
                    )
                    session.add(result_evidence)
                    session.flush()
                    result = LongitudinalResult(
                        public_id=_id(220 + i * 10 + offset),
                        longitudinal_run_id=run.id,
                        evidence_item_id=result_evidence.id,
                        result_name=name,
                        result_kind=kind,
                        sample_count=5,
                        missing_count=0,
                        quality="available",
                        statistics_json={"public_statistics": stats},
                    )
                    session.add(result)
                    session.flush()
                    if name == "recurring_opening":
                        ids["openings"].append(result.public_id)
                    if name == "strategy.fast_tech":
                        assessment_evidence = EvidenceItem(
                            public_id=_id(240 + i),
                            replay_id=replay.id,
                            tier="derived",
                            source_kind="rule_strategy",
                            source_key=f"strategy:{i}",
                            schema_version=1,
                            created_at=now,
                        )
                        session.add(assessment_evidence)
                        session.flush()
                        assessment = StrategyAssessment(
                            public_id=_id(250 + i),
                            evidence_item_id=assessment_evidence.id,
                            replay_id=replay.id,
                            replay_player_id=replay_player.id,
                            method="rule",
                            strategy_label="fast_tech",
                            phase="opening",
                            taxonomy_version="taxonomy-v1",
                            rule_version="rule-v1",
                            frame_start=0,
                            frame_end=100,
                            quality="available",
                            details_json={},
                            created_at=now,
                        )
                        session.add(assessment)
                        session.flush()
                        session.add(
                            LongitudinalMember(
                                longitudinal_result_id=result.id,
                                replay_id=replay.id,
                                replay_player_id=replay_player.id,
                                feature_set_id=feature_set.id,
                                strategy_assessment_id=assessment.id,
                                evidence_item_id=assessment_evidence.id,
                            )
                        )
                        ids["strategies"].append(assessment.public_id)
                    else:
                        session.add(
                            LongitudinalMember(
                                longitudinal_result_id=result.id,
                                replay_id=replay.id,
                                replay_player_id=replay_player.id,
                                feature_set_id=feature_set.id,
                                evidence_item_id=result_evidence.id,
                            )
                        )
                run.status = "succeeded"
            # Two exact disjoint periods for the same player/revision.
            for offset, bounds in enumerate(((1_700_000_000, 1_700_000_100), (1_700_000_200, 1_700_000_300))):
                period = LongitudinalRun(
                    run_id=_id(270 + offset),
                    player_id=players[0].id,
                    identity_revision=1,
                    analyzer_name="longitudinal-player-analysis",
                    analyzer_version="v1",
                    segment_key_json={
                        "schema_version": "longitudinal-segment-v1",
                        "start_inclusive": bounds[0],
                        "end_exclusive": bounds[1],
                        "quality_policy": {"quality_floor": "partial"},
                    },
                    settings_json={
                        "definitions": [
                            {
                                "public_name": "cash",
                                "result_kind": "metric",
                                "definition_version": "cash-v1",
                                "unit": "credits",
                                "scope_types": ["player"],
                            }
                        ],
                        "settings": {"bootstrap_algorithm_version": "median-bootstrap-v1"},
                    },
                    input_digest=str(offset + 3) * 64,
                    cache_key=str(offset + 5) * 64,
                    status="pending",
                    created_at=now,
                    completed_at=now,
                )
                session.add(period)
                session.flush()
                ev = EvidenceItem(
                    public_id=_id(280 + offset),
                    replay_id=session.query(Replay).first().id,
                    tier="derived",
                    source_kind="longitudinal_corpus",
                    source_key=f"period:{offset}",
                    schema_version=1,
                    created_at=now,
                )
                session.add(ev)
                session.flush()
                session.add(
                    LongitudinalResult(
                        public_id=_id(290 + offset),
                        longitudinal_run_id=period.id,
                        evidence_item_id=ev.id,
                        result_name="cash",
                        result_kind="metric",
                        sample_count=5,
                        missing_count=0,
                        quality="available",
                        statistics_json={"public_statistics": {"value": float(20 + offset)}},
                    )
                )
                period.status = "succeeded"
                ids["periods"].append(period.run_id)

        service = ReplayComparisonService(factory)
        selections = (
            ComparisonSelection("players", ids["players"][0], ids["players"][1], False, ("cash",), 3),
            ComparisonSelection("matches", ids["reports"][0], ids["reports"][1], False, ("cash",), 3),
            ComparisonSelection("openings", ids["openings"][0], ids["openings"][1], False, ("recurring_opening",), 3),
            ComparisonSelection(
                "strategies", ids["strategies"][0], ids["strategies"][1], False, ("strategy.fast_tech",), 3
            ),
            ComparisonSelection("time_periods", ids["periods"][0], ids["periods"][1], False, ("cash",), 3),
        )
        fixed_queries = {}
        for selection in selections:
            resolution = service.resolve(selection)
            assert resolution.state == "resolved", (selection.kind, resolution.reason_codes)
            assert resolution.fixed_query is not None
            fixed_queries[selection.kind] = resolution.fixed_query
            comparison = service.compare(resolution.fixed_query)
            assert comparison.state in {"comparable", "partial"}
            assert comparison.metrics[0].value.derived_difference is not None

        poison = service.resolve(
            ComparisonSelection("strategies", ids["openings"][0], ids["openings"][1], False, ("strategy.fast_tech",), 3)
        )
        assert poison.state == "unavailable"
        assert poison.reason_codes == ("rule_strategy_subject_not_found",)

        filtered_match = service.resolve(
            ComparisonSelection(
                "matches",
                ids["reports"][0],
                ids["reports"][1],
                False,
                ("cash",),
                3,
                ComparisonFilters(
                    faction="USA",
                    patch="1.04",
                    date_from_utc=datetime.fromtimestamp(1_699_999_999, UTC),
                    date_to_utc=datetime.fromtimestamp(1_700_000_201, UTC),
                ),
            )
        )
        assert filtered_match.state == "resolved"
        rejected_match = service.resolve(
            ComparisonSelection(
                "matches",
                ids["reports"][0],
                ids["reports"][1],
                False,
                ("cash",),
                3,
                ComparisonFilters(faction="GLA"),
            )
        )
        assert rejected_match.reason_codes == ("comparison_filter_not_materialized",)

        player_query = fixed_queries["players"]
        before_semantic_change = service.compare(player_query)
        with factory.begin() as session:
            left_run = session.scalar(select(LongitudinalRun).where(LongitudinalRun.run_id == player_query.left.run_id))
            assert left_run is not None
            left_value = session.scalar(
                select(LongitudinalResult).where(
                    LongitudinalResult.longitudinal_run_id == left_run.id,
                    LongitudinalResult.result_name == "cash",
                )
            )
            assert left_value is not None
            left_value.sample_count += 1
        after_semantic_change = service.compare(player_query)
        assert before_semantic_change.input_digest != after_semantic_change.input_digest
        assert before_semantic_change.comparison_public_id != after_semantic_change.comparison_public_id
        with pytest.raises(ValueError, match="fixed_longitudinal_binding_mismatch"):
            service.compare(replace(player_query, left=replace(player_query.left, analyzer_version="forged")))
        with pytest.raises(ValueError, match="fixed_definition_binding_mismatch"):
            service.compare(
                replace(
                    player_query,
                    metric_definitions=(replace(player_query.metric_definitions[0], unit="seconds"),),
                )
            )
        with pytest.raises(ValueError, match="fixed_minimum_sample_size_mismatch"):
            service.compare(replace(player_query, minimum_sample_size=1))

        match_query = fixed_queries["matches"]
        with pytest.raises(ValueError, match="fixed_match_binding_mismatch"):
            service.compare(replace(match_query, left=replace(match_query.left, report_version="forged")))
        with pytest.raises(ValueError, match="fixed_feature_set_binding_mismatch"):
            service.compare(
                replace(
                    match_query,
                    left=replace(
                        match_query.left,
                        feature_set_public_ids=match_query.right.feature_set_public_ids,
                    ),
                )
            )

        opening_query = fixed_queries["openings"]
        with pytest.raises(ValueError, match="fixed_opening_binding_mismatch"):
            service.compare(
                replace(
                    opening_query,
                    left=replace(opening_query.left, opening_definition_version="forged"),
                )
            )
        strategy_query = fixed_queries["strategies"]
        with pytest.raises(ValueError, match="fixed_strategy_binding_mismatch"):
            service.compare(replace(strategy_query, left=replace(strategy_query.left, rule_version="forged")))
        period_query = fixed_queries["time_periods"]
        assert period_query.left.start_inclusive_utc is not None
        with pytest.raises(ValueError, match="fixed_time_period_binding_mismatch"):
            service.compare(
                replace(
                    period_query,
                    left=replace(
                        period_query.left,
                        start_inclusive_utc=period_query.left.start_inclusive_utc.replace(year=2025),
                    ),
                )
            )

        with factory.begin() as session:
            player = session.scalar(
                select(Player).where(Player.public_id == fixed_queries["players"].right.player_public_id)
            )
            assert player is not None
            replay = Replay(
                public_id=_id(500),
                sha256="e" * 64,
                replay_name="hidden.rep",
                version_string="1.04",
                version_number=104,
                frame_count=100,
                start_time=1_700_001_000,
                end_time=1_700_001_100,
                exe_crc=1,
                ini_crc=2,
                map_crc=3,
                map_name="TD",
                seed=4,
                header_json={},
                lifecycle_state="engine_verified",
                updated_at=now,
                created_at=now,
            )
            session.add(replay)
            session.flush()
            parser = ParserRun(
                run_id=_id(501),
                replay_id=replay.id,
                parser_version="v1",
                schema_version=1,
                input_sha256="f" * 64,
                result_sha256="0" * 64,
                status="running",
                completion_status="complete",
                command_stream_offset=1,
                end_offset=2,
                warnings_json=[],
                started_at=now,
                completed_at=now,
            )
            session.add(parser)
            session.flush()
            replay_player = ReplayPlayer(
                public_id=_id(502),
                replay_id=replay.id,
                parser_run_id=parser.id,
                player_id=player.id,
                slot_index=0,
                slot_kind="human",
                original_name="P1",
                faction="GLA",
                result="win",
                observed_json={},
            )
            session.add(replay_player)
            session.flush()
            right_run = session.scalar(
                select(LongitudinalRun).where(LongitudinalRun.run_id == player_query.right.run_id)
            )
            assert right_run is not None
            right_value = session.scalar(
                select(LongitudinalResult).where(
                    LongitudinalResult.longitudinal_run_id == right_run.id,
                    LongitudinalResult.result_name == "cash",
                )
            )
            assert right_value is not None
            member = session.scalar(
                select(LongitudinalMember).where(LongitudinalMember.longitudinal_result_id == right_value.id)
            )
            assert member is not None
            member.replay_id = replay.id
            member.replay_player_id = replay_player.id
            parser.status = "succeeded"
        faction_mismatch = service.compare(player_query)
        assert faction_mismatch.metrics[0].value.state == "not_comparable"
        assert faction_mismatch.metrics[0].value.reason_codes == ("faction_mismatch",)
    finally:
        engine.dispose()
