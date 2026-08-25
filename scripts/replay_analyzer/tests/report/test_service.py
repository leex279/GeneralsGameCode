from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import event, func, select, text

from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    EconomyEvent,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    LongitudinalMember,
    LongitudinalResult,
    ManagedAsset,
    ParserRun,
    Player,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    Report,
    StrategyAssessment,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.report.model import ReportRequest, document_to_mapping
from generals_replay_analyzer.report.service import (
    ReportContractError,
    ReportNotFoundError,
    ReportService,
    _bucket_camera_combat_anchors,
    _CameraCombatAnchor,
    _evenly_sample,
)
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import SeededReportDatabase, stable_uuid


def _service(database: SeededReportDatabase) -> ReportService:
    return ReportService(
        database.session_factory,  # type: ignore[arg-type]
        settings=database.settings,
        store=ContentAddressedStore(database.settings.cache_directory / "reports"),
    )


def test_full_match_observation_sampling_is_bounded_and_spans_the_timeline() -> None:
    rows = tuple(range(10_000))

    selected = _evenly_sample(rows, 256)

    assert len(selected) == 256
    assert selected[0] == rows[0]
    assert selected[-1] == rows[-1]
    assert selected == tuple(sorted(set(selected)))


def test_camera_combat_anchor_buckets_are_deterministic_bounded_and_do_not_reuse_samples() -> None:
    anchors = tuple(
        _CameraCombatAnchor(
            stable_uuid(f"combat-anchor:{index}"),
            index * 100,
            stable_uuid(f"combat-sample:{index}"),
            max(0, index * 100 - 15),
        )
        for index in range(10_000)
    )

    selected = _bucket_camera_combat_anchors(
        anchors,
        final_frame=1_000_000,
        logic_frames_per_second=30,
    )
    reversed_selected = _bucket_camera_combat_anchors(
        tuple(reversed(anchors)),
        final_frame=1_000_000,
        logic_frames_per_second=30,
    )

    assert selected == reversed_selected
    assert len(selected) == 256
    assert len({item.attacker_sample_evidence_public_id for item in selected}) == 256
    bucket_width = max(30 * 15, (1_000_000 + 1 + 255) // 256)
    assert len({item.combat_frame // bucket_width for item in selected}) == 256


def test_report_omits_bulk_partition_diagnostics_from_public_observed_values(
    report_database: SeededReportDatabase,
) -> None:
    now = datetime(2026, 8, 24, 20, 0, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        session.execute(text("DROP TRIGGER trg_telemetry_events_succeeded_no_insert"))
        session.execute(text("DROP TRIGGER trg_evidence_items_observed_no_insert"))
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.status == "succeeded"))
        assert replay is not None and telemetry is not None
        evidence = EvidenceItem(
            public_id=stable_uuid("bulk-partition-grid-evidence"),
            replay_id=replay.id,
            telemetry_run_id=telemetry.id,
            tier="observed",
            source_kind="telemetry_event",
            source_key="telemetry:partition-grid:bulk",
            schema_version=2,
            created_at=now,
        )
        session.add(evidence)
        session.flush()
        session.add(
            TelemetryEvent(
                telemetry_run_id=telemetry.id,
                sequence=99_999,
                frame=300,
                logic_time_seconds=10.0,
                schema_version=2,
                event_type="partition_engine_grid_sample",
                payload_json={"objects": [{"object_id": index, "x": float(index)} for index in range(1_000)]},
                raw_record_json={"event_type": "partition_engine_grid_sample"},
                evidence_item_id=evidence.id,
            )
        )
        session.commit()

    receipt = _service(report_database).create(ReportRequest(report_database.replay_public_id, publish=False))

    assert receipt.document.observed
    assert all(value.label != "partition_engine_grid_sample" for value in receipt.document.observed)


def test_report_feature_assembly_query_count_is_bounded_by_batches(
    report_database: SeededReportDatabase,
) -> None:
    factory = report_database.session_factory
    engine = factory.kw["bind"]  # type: ignore[attr-defined]
    selected: list[str] = []

    def count_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selected.append(statement)

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        request = ReportRequest(
            report_database.replay_public_id,
            report_database.replay_player_public_id,
            include_validated_ollama=False,
            publish=False,
        )
        _service(report_database).create(request)
        baseline = len(selected)
        selected.clear()
        with factory() as session:  # type: ignore[operator]
            replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
            feature_set = session.scalar(select(Feature.feature_set_id).limit(1))
            telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.replay_id == replay.id))
            replay_player_id = session.scalar(
                select(ReplayPlayer.id).where(
                    ReplayPlayer.public_id == report_database.replay_player_public_id
                )
            )
            observed = session.scalar(
                select(EvidenceItem).where(EvidenceItem.public_id == report_database.observed_evidence_id)
            )
            assert (
                replay is not None
                and feature_set is not None
                and telemetry is not None
                and replay_player_id is not None
                and observed is not None
            )
            for index in range(12):
                evidence = EvidenceItem(
                    public_id=stable_uuid(f"bounded-feature-evidence-{index}"),
                    replay_id=replay.id,
                    telemetry_run_id=telemetry.id,
                    tier="derived",
                    source_kind="feature",
                    source_key=f"feature:bounded:{index}",
                    schema_version=1,
                    created_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
                )
                session.add(evidence)
                session.flush()
                feature = Feature(
                    public_id=stable_uuid(f"bounded-feature-{index}"),
                    feature_set_id=feature_set,
                    evidence_item_id=evidence.id,
                    name=f"bounded_feature_{index}",
                    value_type="integer",
                    integer_value=index,
                    scope_type="player",
                    scope_key=report_database.replay_player_public_id,
                    replay_player_id=replay_player_id,
                    frame_start=0,
                    frame_end=1200,
                    quality="available",
                    details_json={"index": index},
                )
                session.add(feature)
                session.flush()
                session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=observed.id, role="input"))
            session.commit()

        selected.clear()
        started = perf_counter()
        receipt = _service(report_database).create(request)
        elapsed = perf_counter() - started
        expanded = len(selected)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert len(tuple(value for value in receipt.document.derived if value.section == "features")) == 13
    assert expanded <= baseline + 2
    assert elapsed < 5.0


def _full_llm_response(
    *, strategy_evidence: str | None = None, bad_strength_evidence: str | None = None
) -> dict[str, object]:
    strategies: list[dict[str, object]] = []
    if strategy_evidence is not None:
        strategies.append(
            {
                "claim_id": "pressure",
                "strategy_label": "pressure",
                "phase": "mid",
                "window": {"frame_start": 300, "frame_end": 900},
                "assessment": "Sustained pressure.",
                "confidence": 0.8,
                "evidence_ids": [strategy_evidence],
            }
        )
    strengths: list[dict[str, object]] = []
    if bad_strength_evidence is not None:
        strengths.append(
            {
                "claim_id": "bad-strength",
                "text": "Unsupported strength.",
                "confidence": 0.5,
                "evidence_ids": [bad_strength_evidence],
            }
        )
    return {
        "schema_version": "strategy-report-response-v1",
        "summary": "Evidence-bound summary.",
        "phase_assessments": [],
        "strategy_assessments": strategies,
        "comparative_observations": [],
        "strengths": strengths,
        "vulnerabilities": [],
        "uncertainty_notes": [],
    }


def test_service_materializes_separate_persisted_tiers_lifecycle_quality_and_validated_ollama(
    report_database: SeededReportDatabase,
) -> None:
    receipt = _service(report_database).create(
        ReportRequest(
            report_database.replay_public_id,
            report_database.replay_player_public_id,
            include_validated_ollama=True,
            publish=False,
        )
    )

    assert receipt.document.lifecycle.lifecycle_state == "partial"
    assert receipt.document.lifecycle.parser_completion_status == "complete"
    assert receipt.document.lifecycle.telemetry_status == "succeeded"
    assert receipt.document.quality_issues[0].issue_code == "sample_gap"
    assert {ref.public_id for value in receipt.document.observed for ref in value.evidence} == {
        report_database.parser_observed_evidence_id,
        report_database.observed_evidence_id,
    }
    assert receipt.document.derived[0].raw_value == 1234.56789
    assert receipt.document.derived[0].unavailable_reason == "sample_window_gap"
    assert {value.section for value in receipt.document.derived} == {"features", "longitudinal", "strategy"}
    rule = next(value for value in receipt.document.derived if value.section == "strategy")
    assert rule.unavailable_reason == "mixed_supported_candidates"
    assert report_database.inferred_evidence_id in {ref.public_id for ref in receipt.document.inferred[0].evidence}
    assert receipt.document.inferred[0].unavailable_reason == "sample_window_gap"
    assert receipt.document.ollama.status == "succeeded"
    assert receipt.document.ollama.validated_prose is not None
    assert all(value.raw_value != receipt.document.ollama.validated_prose for value in receipt.document.observed)
    assert receipt.structured_asset is None and receipt.presentation_bundle_asset is None


def test_requested_player_uses_exact_successful_parser_telemetry_graph_not_arbitrary_attempt(
    report_database: SeededReportDatabase,
) -> None:
    now = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        assert replay is not None
        other_parser = ParserRun(
            run_id="00000000-0000-4000-8000-000000000001",
            replay_id=replay.id,
            parser_version="parser-v99",
            schema_version=99,
            input_sha256=replay.sha256,
            result_sha256="d" * 64,
            status="running",
            completion_status=None,
            warnings_json=[],
            started_at=now,
        )
        session.add(other_parser)
        session.flush()
        other_player = ReplayPlayer(
            public_id=stable_uuid("other-parser-player"),
            replay_id=replay.id,
            parser_run_id=other_parser.id,
            slot_index=0,
            slot_kind="human",
            original_name="Other",
            normalized_name="other",
            player_index=0,
            observed_json={},
        )
        session.add(other_player)
        session.flush()
        other_evidence = EvidenceItem(
            public_id=stable_uuid("other-parser-evidence"),
            replay_id=replay.id,
            parser_run_id=other_parser.id,
            tier="observed",
            source_kind="parser_command",
            source_key="command:other:0",
            schema_version=99,
            created_at=now,
        )
        session.add(other_evidence)
        session.flush()
        session.add(
            ReplayCommand(
                parser_run_id=other_parser.id,
                replay_id=replay.id,
                replay_player_id=other_player.id,
                command_index=0,
                frame=1,
                player_index=0,
                message_type=1,
                message_name="OTHER",
                start_offset=1,
                end_offset=2,
                arguments_json={},
                evidence_item_id=other_evidence.id,
            )
        )
        session.flush()
        other_parser.status = "succeeded"
        other_parser.completion_status = "complete"
        other_parser.completed_at = now
        session.add(
            TelemetryRun(
                run_id="00000000-0000-4000-8000-000000000002",
                replay_id=replay.id,
                schema_version=99,
                engine_build="other",
                engine_executable_sha256="e" * 64,
                settings_json={"parser_run_id": other_parser.run_id},
                status="failed",
                runner_status="runner_failed",
                diagnostics_json=[],
                started_at=now,
                completed_at=now,
            )
        )
        session.commit()

    document = (
        _service(report_database)
        .create(ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, publish=False))
        .document
    )
    assert {value.claim_id for value in document.observed} == {
        f"observed:parser_command:{report_database.parser_observed_evidence_id}",
        f"observed:telemetry_event:{report_database.observed_evidence_id}",
    }
    assert {reference.public_id for value in document.observed for reference in value.evidence} == {
        report_database.parser_observed_evidence_id,
        report_database.observed_evidence_id,
    }


def test_report_rejects_a_succeeded_feature_telemetry_branch_from_another_parser_before_writes(
    report_database: SeededReportDatabase,
) -> None:
    now = datetime(2026, 8, 22, 14, 15, tzinfo=UTC)
    with report_database.session_factory.begin() as session:  # type: ignore[union-attr]
        session.execute(text("DROP TRIGGER trg_telemetry_runs_succeeded_no_update"))
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.replay_id == replay.id))
        assert replay is not None and telemetry is not None
        other_parser = ParserRun(
            run_id=stable_uuid("report-cross-branch-parser"),
            replay_id=replay.id,
            parser_version="parser-cross-branch-v1",
            schema_version=1,
            input_sha256=replay.sha256,
            result_sha256="9" * 64,
            status="succeeded",
            completion_status="complete",
            warnings_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(other_parser)
        telemetry.settings_json = {"parser_run_id": other_parser.run_id}

    with pytest.raises(ReportContractError, match="parser.*telemetry|telemetry.*parser"):
        _service(report_database).create(
            ReportRequest(
                report_database.replay_public_id,
                report_database.replay_player_public_id,
                publish=True,
            )
        )

    with report_database.session_factory() as session:  # type: ignore[operator]
        assert session.scalar(select(func.count()).select_from(Report)) == 0


def test_player_telemetry_ownership_uses_the_exact_resolved_initialization_mapping(
    report_database: SeededReportDatabase,
) -> None:
    now = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        session.execute(text("DROP TRIGGER trg_replay_players_succeeded_no_observation_update"))
        session.execute(text("DROP TRIGGER trg_telemetry_events_succeeded_no_insert"))
        session.execute(text("DROP TRIGGER trg_telemetry_events_succeeded_no_update"))
        session.execute(text("DROP TRIGGER trg_evidence_items_observed_no_insert"))
        player = session.scalar(
            select(ReplayPlayer).where(
                ReplayPlayer.public_id == report_database.replay_player_public_id
            )
        )
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.status == "succeeded"))
        event_row = session.scalar(
            select(TelemetryEvent).where(
                TelemetryEvent.evidence_item_id
                == select(EvidenceItem.id)
                .where(EvidenceItem.public_id == report_database.observed_evidence_id)
                .scalar_subquery()
            )
        )
        assert player is not None and telemetry is not None and event_row is not None
        # Legacy parser indices may conflict with the accepted telemetry mapping;
        # the resolved players_initialized slot mapping remains authoritative.
        player.player_index = 99
        event_row.payload_json = {"player_index": 7, "cash": 9000}
        initialization_evidence = EvidenceItem(
            public_id=stable_uuid("players-initialized-evidence"),
            replay_id=player.replay_id,
            telemetry_run_id=telemetry.id,
            tier="observed",
            source_kind="telemetry_event",
            source_key="telemetry:players-initialized:exact-mapping",
            schema_version=2,
            created_at=now,
        )
        session.add(initialization_evidence)
        session.flush()
        session.add(
            TelemetryEvent(
                telemetry_run_id=telemetry.id,
                sequence=1,
                frame=0,
                logic_time_seconds=0.0,
                schema_version=2,
                event_type="players_initialized",
                payload_json={
                    "slots": [
                        {
                            "slot_index": player.slot_index,
                            "resolution_status": "resolved",
                            "player_index": 7,
                        }
                    ]
                },
                raw_record_json={"event_type": "players_initialized"},
                evidence_item_id=initialization_evidence.id,
            )
        )
        session.commit()

    ownership_statements: list[str] = []
    engine = report_database.session_factory.kw["bind"]  # type: ignore[attr-defined]

    def capture_ownership_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if "FROM economy_events" in statement:
            ownership_statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_ownership_query)
    try:
        receipt = _service(report_database).create(
            ReportRequest(
                report_database.replay_public_id,
                report_database.replay_player_public_id,
                publish=False,
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_ownership_query)

    assert report_database.observed_evidence_id in {
        reference.public_id
        for value in receipt.document.derived
        for reference in value.evidence
    }
    assert ownership_statements
    assert all("JOIN telemetry_events" in statement for statement in ownership_statements)


@pytest.mark.parametrize(
    "initialization_payload",
    (
        {"slots": "malformed"},
        {
            "slots": [
                {
                    "slot_index": 0,
                    "resolution_status": "unresolved",
                    "player_index": 0,
                }
            ]
        },
    ),
)
def test_typed_economy_ownership_never_falls_back_when_initialization_mapping_is_invalid(
    report_database: SeededReportDatabase,
    initialization_payload: object,
) -> None:
    now = datetime(2026, 8, 22, 14, 45, tzinfo=UTC)
    with report_database.session_factory.begin() as session:  # type: ignore[union-attr]
        session.execute(text("DROP TRIGGER trg_telemetry_events_succeeded_no_insert"))
        session.execute(text("DROP TRIGGER trg_economy_events_succeeded_no_insert"))
        session.execute(text("DROP TRIGGER trg_evidence_items_observed_no_insert"))
        player = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == report_database.replay_player_public_id)
        )
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.status == "succeeded"))
        event_row = session.scalar(
            select(TelemetryEvent).where(
                TelemetryEvent.evidence_item_id
                == select(EvidenceItem.id)
                .where(EvidenceItem.public_id == report_database.observed_evidence_id)
                .scalar_subquery()
            )
        )
        assert player is not None and telemetry is not None and event_row is not None
        session.add(
            EconomyEvent(
                telemetry_run_id=telemetry.id,
                telemetry_event_id=event_row.id,
                replay_id=player.replay_id,
                replay_player_id=player.id,
                frame=event_row.frame,
                event_type="economy_sample",
                balance_after=9000,
                payload_json={"player_index": 0, "cash": 9000},
            )
        )
        initialization_evidence = EvidenceItem(
            public_id=stable_uuid(f"invalid-initialization-{initialization_payload!r}"),
            replay_id=player.replay_id,
            telemetry_run_id=telemetry.id,
            tier="observed",
            source_kind="telemetry_event",
            source_key=f"telemetry:invalid-initialization:{initialization_payload!r}",
            schema_version=2,
            created_at=now,
        )
        session.add(initialization_evidence)
        session.flush()
        session.add(
            TelemetryEvent(
                telemetry_run_id=telemetry.id,
                sequence=1,
                frame=0,
                logic_time_seconds=0.0,
                schema_version=2,
                event_type="players_initialized",
                payload_json=initialization_payload,
                raw_record_json={"event_type": "players_initialized"},
                evidence_item_id=initialization_evidence.id,
            )
        )

    with pytest.raises(ReportContractError, match="owned"):
        _service(report_database).create(
            ReportRequest(
                report_database.replay_public_id,
                report_database.replay_player_public_id,
                publish=False,
            )
        )


@pytest.mark.parametrize("target", ["feature", "assessment"])
def test_cross_replay_predecessor_evidence_link_fails_closed(
    report_database: SeededReportDatabase,
    target: str,
) -> None:
    now = datetime(2026, 8, 22, 15, 0, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        foreign = Replay(
            public_id=stable_uuid(f"foreign-{target}-replay"),
            sha256=("7" if target == "feature" else "8") * 64,
            replay_name="foreign.rep",
            version_string="1.04",
            version_number=104,
            frame_count=1,
            start_time=1,
            end_time=2,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="Foreign",
            seed=4,
            header_json={},
            lifecycle_state="parsed",
            created_at=now,
            updated_at=now,
        )
        session.add(foreign)
        session.flush()
        evidence = EvidenceItem(
            public_id=stable_uuid(f"foreign-{target}-evidence"),
            replay_id=foreign.id,
            tier="observed",
            source_kind="parser_command",
            source_key=f"foreign:{target}",
            schema_version=1,
            created_at=now,
        )
        session.add(evidence)
        session.flush()
        if target == "feature":
            row = session.scalar(select(Feature).where(Feature.name == "income_total"))
            assert row is not None
            session.add(FeatureEvidence(feature_id=row.id, evidence_item_id=evidence.id, role="supporting"))
        else:
            row = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "rule"))
            assert row is not None
            session.add(AssessmentEvidence(assessment_id=row.id, evidence_item_id=evidence.id, role="contradicting"))
        session.commit()

    with pytest.raises(ReportContractError, match="cross-replay"):
        _service(report_database).create(
            ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, publish=False)
        )


def test_non_rule_deterministic_assessment_is_not_task8_evidence(report_database: SeededReportDatabase) -> None:
    now = datetime(2026, 8, 22, 15, 30, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        player = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == report_database.replay_player_public_id)
        )
        assert replay is not None and player is not None
        evidence = EvidenceItem(
            public_id=stable_uuid("manual-derived"),
            replay_id=replay.id,
            tier="derived",
            source_kind="manual",
            source_key="manual:claim",
            schema_version=1,
            created_at=now,
        )
        session.add(evidence)
        session.flush()
        session.add(
            StrategyAssessment(
                public_id=stable_uuid("manual-assessment"),
                evidence_item_id=evidence.id,
                replay_id=replay.id,
                replay_player_id=player.id,
                method="manual",
                strategy_label="manual_claim",
                phase="opening",
                frame_start=0,
                frame_end=1,
                quality="available",
                confidence=0.5,
                details_json={},
                created_at=now,
            )
        )
        session.commit()
    document = (
        _service(report_database)
        .create(ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, publish=False))
        .document
    )
    assert all(value.label != "manual_claim" for value in document.derived)


def test_longitudinal_requires_current_canonical_player_revision(report_database: SeededReportDatabase) -> None:
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay_player = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == report_database.replay_player_public_id)
        )
        assert replay_player is not None and replay_player.player_id is not None
        canonical = session.get(Player, replay_player.player_id)
        assert canonical is not None
        canonical.identity_revision += 1
        session.commit()
    document = (
        _service(report_database)
        .create(ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, publish=False))
        .document
    )
    assert all(value.section != "longitudinal" for value in document.derived)


def test_default_excludes_prose_and_unavailable_attempts_are_status_only(
    report_database: SeededReportDatabase,
) -> None:
    service = _service(report_database)
    default = service.create(ReportRequest(report_database.replay_public_id, publish=False))
    assert default.document.ollama.status == "not_requested"
    assert default.document.inferred == ()

    requested = service.create(
        ReportRequest(report_database.replay_public_id, include_validated_ollama=True, publish=False)
    )
    assert requested.document.ollama.status == "unavailable"
    assert requested.document.ollama.validated_prose is None
    assert requested.document.inferred == ()


def test_publication_persists_two_verified_assets_and_cache_hit_is_identical(
    report_database: SeededReportDatabase,
) -> None:
    service = _service(report_database)
    request = ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, True, True)
    first = service.create(request)
    second = service.create(request)

    assert first.cache_hit is False and second.cache_hit is True
    assert document_to_mapping(first.document) == document_to_mapping(second.document)
    assert first.structured_asset == second.structured_asset
    assert first.presentation_bundle_asset == second.presentation_bundle_asset
    assert first.structured_asset is not None and first.presentation_bundle_asset is not None
    assert first.structured_asset.sha256 == first.structured_asset.sha256.lower()
    for asset in (first.structured_asset, first.presentation_bundle_asset):
        stored = ContentAddressedStore(report_database.settings.cache_directory / "reports").verify(asset.sha256)
        assert stored.size == asset.size_bytes
    with report_database.session_factory() as session:  # type: ignore[operator]
        assert session.scalar(select(func.count()).select_from(Report)) == 1
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 2
        row = session.scalar(select(Report))
        assert row is not None
        assert row.structured_asset_id is not None and row.rendered_asset_id is not None


def test_existing_analytical_identity_with_resource_or_cache_drift_fails_closed(
    report_database: SeededReportDatabase,
) -> None:
    service = _service(report_database)
    request = ReportRequest(report_database.replay_public_id, publish=True)
    service.create(request)
    with report_database.session_factory() as session:  # type: ignore[operator]
        row = session.scalar(select(Report))
        assert row is not None
        row.cache_key = "9" * 64
        session.commit()

    with pytest.raises(ReportContractError, match="drift"):
        service.create(request)


def test_first_publication_revalidates_reused_asset_deterministic_public_id(
    report_database: SeededReportDatabase,
) -> None:
    service = _service(report_database)
    request = ReportRequest(report_database.replay_public_id, publish=True)
    service.create(request)
    with report_database.session_factory() as session:  # type: ignore[operator]
        report = session.scalar(select(Report))
        asset = session.scalar(select(ManagedAsset).where(ManagedAsset.kind == "report_structured_json"))
        assert report is not None and asset is not None
        session.delete(report)
        asset.public_id = stable_uuid("forged-first-publication-asset")
        session.commit()

    with pytest.raises(ReportContractError, match="asset identity drift"):
        service.create(request)


@pytest.mark.parametrize("tamper", ["source_kind", "member_evidence", "missing_public_statistics"])
def test_longitudinal_result_requires_exact_task9_result_member_evidence_graph(
    report_database: SeededReportDatabase,
    tamper: str,
) -> None:
    with report_database.session_factory() as session:  # type: ignore[operator]
        result = session.scalar(select(LongitudinalResult))
        member = session.scalar(select(LongitudinalMember))
        assert result is not None and member is not None
        if tamper == "source_kind":
            evidence = session.get(EvidenceItem, result.evidence_item_id)
            assert evidence is not None
            evidence.source_kind = "feature"
        elif tamper == "member_evidence":
            foreign = session.scalar(select(EvidenceItem).where(EvidenceItem.source_kind == "strategy_rule"))
            assert foreign is not None
            member.evidence_item_id = foreign.id
        else:
            statistics = dict(result.statistics_json)
            statistics.pop("public_statistics", None)
            result.statistics_json = statistics
        session.commit()

    with pytest.raises(ReportContractError, match="longitudinal"):
        _service(report_database).create(
            ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, publish=False)
        )


@pytest.mark.parametrize(
    "tamper", ["replay_player", "analysis_run", "swapped_assets", "asset_metadata", "corrupt", "missing"]
)
def test_cache_hit_revalidates_exact_report_asset_links_metadata_and_cas_bytes(
    report_database: SeededReportDatabase,
    tamper: str,
) -> None:
    service = _service(report_database)
    request = ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, True, True)
    receipt = service.create(request)
    assert receipt.structured_asset is not None and receipt.presentation_bundle_asset is not None
    expected_asset_namespace = uuid5(NAMESPACE_URL, "replay-report-managed-asset-v1")
    assert receipt.structured_asset.public_id == str(
        uuid5(expected_asset_namespace, f"report_structured_json:{receipt.structured_asset.sha256}")
    )
    with report_database.session_factory() as session:  # type: ignore[operator]
        row = session.scalar(select(Report))
        assert row is not None and row.structured_asset_id is not None and row.rendered_asset_id is not None
        structured = session.get(ManagedAsset, row.structured_asset_id)
        assert structured is not None
        if tamper == "replay_player":
            row.replay_player_id = None
        elif tamper == "analysis_run":
            row.analysis_run_id = None
        elif tamper == "swapped_assets":
            row.structured_asset_id, row.rendered_asset_id = row.rendered_asset_id, row.structured_asset_id
        elif tamper == "asset_metadata":
            structured.kind = "wrong_kind"
            structured.media_type = "text/plain"
            structured.size_bytes += 1
        elif tamper in ("corrupt", "missing"):
            path = report_database.settings.data_root / structured.relative_path
            if tamper == "corrupt":
                path.write_bytes(b"corrupt")
            else:
                path.unlink()
        session.commit()
    with pytest.raises(ReportContractError, match="asset|link|player|analysis"):
        service.create(request)


def test_unknown_replay_or_player_is_path_free(report_database: SeededReportDatabase) -> None:
    service = _service(report_database)
    with pytest.raises(ReportNotFoundError, match="replay") as caught:
        service.create(ReportRequest(stable_uuid("unknown-replay"), publish=False))
    assert str(report_database.settings.data_root) not in str(caught.value)
    with pytest.raises(ReportNotFoundError, match="player"):
        service.create(ReportRequest(report_database.replay_public_id, stable_uuid("unknown-player"), publish=False))


class _FailingStore(ContentAddressedStore):
    def store_bytes(self, data: bytes, *, expected_sha256: str | None = None):  # type: ignore[no-untyped-def]
        stored = super().store_bytes(data, expected_sha256=expected_sha256)
        if json.loads(data).get("schema_version") == "report-presentation-bundle-v1":
            raise RuntimeError("simulated publication failure")
        return stored


def test_publication_failure_leaves_immutable_orphan_and_rolls_back_database(
    report_database: SeededReportDatabase,
) -> None:
    store = _FailingStore(report_database.settings.cache_directory / "reports")
    service = ReportService(report_database.session_factory, settings=report_database.settings, store=store)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="simulated"):
        service.create(ReportRequest(report_database.replay_public_id, publish=True))

    managed_files = [path for path in store.root.rglob("*") if path.is_file()]
    assert len(managed_files) == 2  # SQLite WAL files are outside this content root; object plus failed bundle object.
    with report_database.session_factory() as session:  # type: ignore[operator]
        assert session.scalar(select(func.count()).select_from(Report)) == 0
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 0
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == path.name for path in managed_files)


def test_concurrent_winner_is_returned_unchanged(report_database: SeededReportDatabase) -> None:
    request = ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, True, True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(lambda _index: _service(report_database).create(request), range(2)))

    assert sorted(receipt.cache_hit for receipt in receipts) == [False, True]
    assert receipts[0].document == receipts[1].document
    with report_database.session_factory() as session:  # type: ignore[operator]
        assert session.scalar(select(func.count()).select_from(Report)) == 1


def test_invalid_stored_ollama_citation_is_status_only(report_database: SeededReportDatabase) -> None:
    now = datetime(2026, 8, 22, 13, 0, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay_id = session.scalar(select(Report.replay_id).limit(1))
        if replay_id is None:
            from generals_replay_analyzer.db.models import Replay

            replay_id = session.scalar(select(Replay.id).where(Replay.public_id == report_database.replay_public_id))
        assert replay_id is not None
        run = AnalysisRun(
            run_id=stable_uuid("invalid-citation-run"),
            replay_id=replay_id,
            replay_player_id=None,
            provider="ollama",
            model_name="qwen3.6:27b",
            model_digest="7" * 64,
            prompt_version="strategy-report-v1",
            prompt_digest="c2603f4f4cff1b3563801d83a6c2e567700eba4a86fe3af83524bcb98d6693d7",
            response_schema_version="strategy-report-response-v1",
            response_schema_digest="a758faf24d931094b18ade9cf37c49bbe032686f7499180872f1d7bd7669b76e",
            settings_digest="a" * 64,
            input_digest="b" * 64,
            cache_key="c" * 64,
            status="running",
            validated_response_json=_full_llm_response(bad_strength_evidence=stable_uuid("absent")),
            diagnostics_json=[],
            created_at=now,
        )
        session.add(run)
        session.flush()
        run.status = "succeeded"
        run.completed_at = now
        session.commit()

    receipt = _service(report_database).create(
        ReportRequest(report_database.replay_public_id, include_validated_ollama=True, publish=False)
    )
    assert receipt.document.ollama.status == "invalid"
    assert receipt.document.ollama.validated_prose is None
    assert "analysis_unknown_evidence_id" in receipt.document.ollama.diagnostic_codes
    assert receipt.document.inferred == ()


def test_report_selects_exact_requested_analysis_run_amid_other_attempts(
    report_database: SeededReportDatabase,
) -> None:
    now = datetime(2026, 8, 22, 13, 30, tzinfo=UTC)
    selected_run_id = stable_uuid("selected-exact-analysis-run")
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        assert replay is not None
        session.add(
            AnalysisRun(
                run_id=selected_run_id,
                replay_id=replay.id,
                replay_player_id=None,
                provider="ollama",
                model_name="qwen3.6:27b",
                model_digest="7" * 64,
                prompt_version="strategy-report-v1",
                prompt_digest="c2603f4f4cff1b3563801d83a6c2e567700eba4a86fe3af83524bcb98d6693d7",
                response_schema_version="strategy-report-response-v1",
                response_schema_digest="a758faf24d931094b18ade9cf37c49bbe032686f7499180872f1d7bd7669b76e",
                settings_digest="a" * 64,
                input_digest="b" * 64,
                cache_key="f" * 64,
                status="unavailable",
                validated_response_json=None,
                diagnostics_json=[{"code": "selected_unavailable"}],
                created_at=now,
                completed_at=now,
            )
        )
        session.commit()

    receipt = _service(report_database).create(
        ReportRequest(
            report_database.replay_public_id,
            include_validated_ollama=True,
            publish=False,
            analysis_run_id=selected_run_id,
        )
    )

    assert receipt.document.ollama.analysis_run_id == selected_run_id
    assert receipt.document.ollama.status == "unavailable"


def test_succeeded_task10_run_requires_pinned_prompt_and_response_resources(
    report_database: SeededReportDatabase,
) -> None:
    now = datetime(2026, 8, 22, 16, 0, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        assert replay is not None
        run = AnalysisRun(
            run_id=stable_uuid("resource-drift-run"),
            replay_id=replay.id,
            replay_player_id=None,
            provider="ollama",
            model_name="qwen3.6:27b",
            model_digest="7" * 64,
            prompt_version="strategy-report-v1",
            prompt_digest="8" * 64,
            response_schema_version="strategy-report-response-v1",
            response_schema_digest="9" * 64,
            settings_digest="a" * 64,
            input_digest="b" * 64,
            cache_key="d" * 64,
            status="running",
            validated_response_json=_full_llm_response(),
            diagnostics_json=[],
            created_at=now,
        )
        session.add(run)
        session.flush()
        run.status = "succeeded"
        run.completed_at = now
        session.commit()
    receipt = _service(report_database).create(
        ReportRequest(report_database.replay_public_id, include_validated_ollama=True, publish=False)
    )
    assert receipt.document.ollama.status == "invalid"
    assert "analysis_resource_mismatch" in receipt.document.ollama.diagnostic_codes


def test_succeeded_task10_strategy_claim_requires_exact_persisted_inferred_graph(
    report_database: SeededReportDatabase,
) -> None:
    now = datetime(2026, 8, 22, 16, 30, tzinfo=UTC)
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        assert replay is not None
        run = AnalysisRun(
            run_id=stable_uuid("missing-inferred-graph-run"),
            replay_id=replay.id,
            replay_player_id=None,
            provider="ollama",
            model_name="qwen3.6:27b",
            model_digest="7" * 64,
            prompt_version="strategy-report-v1",
            prompt_digest="c2603f4f4cff1b3563801d83a6c2e567700eba4a86fe3af83524bcb98d6693d7",
            response_schema_version="strategy-report-response-v1",
            response_schema_digest="a758faf24d931094b18ade9cf37c49bbe032686f7499180872f1d7bd7669b76e",
            settings_digest="a" * 64,
            input_digest="b" * 64,
            cache_key="e" * 64,
            status="running",
            validated_response_json=_full_llm_response(strategy_evidence=report_database.observed_evidence_id),
            diagnostics_json=[],
            created_at=now,
        )
        session.add(run)
        session.flush()
        run.status = "succeeded"
        run.completed_at = now
        session.commit()
    receipt = _service(report_database).create(
        ReportRequest(report_database.replay_public_id, include_validated_ollama=True, publish=False)
    )
    assert receipt.document.ollama.status == "invalid"
    assert "analysis_graph_mismatch" in receipt.document.ollama.diagnostic_codes
    assert receipt.document.inferred == ()


@pytest.mark.parametrize(
    ("violation", "diagnostic"),
    [
        ("duplicate_claim_id", "analysis_duplicate_claim_id"),
        ("duplicate_citation", "analysis_duplicate_citation"),
        ("reversed_window", "analysis_unsupported_window"),
        ("too_many_claims", "analysis_response_oversize"),
    ],
)
def test_succeeded_task10_response_uses_exact_domain_validation(
    report_database: SeededReportDatabase,
    violation: str,
    diagnostic: str,
) -> None:
    with report_database.session_factory() as session:  # type: ignore[operator]
        replay = session.scalar(select(Replay).where(Replay.public_id == report_database.replay_public_id))
        assert replay is not None
        response = _full_llm_response()
        citation_id = report_database.observed_evidence_id
        if violation == "duplicate_claim_id":
            response["comparative_observations"] = [
                {
                    "claim_id": "duplicate",
                    "text": "First claim.",
                    "confidence": 0.5,
                    "evidence_ids": [citation_id],
                }
            ]
            response["strengths"] = [
                {
                    "claim_id": "duplicate",
                    "text": "Duplicate identifier.",
                    "confidence": 0.5,
                    "evidence_ids": [citation_id],
                }
            ]
        elif violation == "duplicate_citation":
            response["strengths"] = [
                {
                    "claim_id": "duplicate-citation",
                    "text": "Duplicate citation.",
                    "confidence": 0.5,
                    "evidence_ids": [citation_id, citation_id],
                }
            ]
        elif violation == "reversed_window":
            response["phase_assessments"] = [
                {
                    "claim_id": "reversed",
                    "phase": "mid",
                    "window": {"frame_start": 900, "frame_end": 300},
                    "assessment": "Reversed window.",
                    "confidence": 0.5,
                    "evidence_ids": [citation_id],
                }
            ]
        else:
            for section_index, section in enumerate(
                ("comparative_observations", "strengths", "vulnerabilities", "uncertainty_notes")
            ):
                response[section] = [
                    {
                        "claim_id": f"claim-{section_index}-{index}",
                        "text": "Evidence-bound claim.",
                        "confidence": 0.5,
                        "evidence_ids": [citation_id],
                    }
                    for index in range(64)
                ]
            response["phase_assessments"] = [
                {
                    "claim_id": "claim-256",
                    "phase": "mid",
                    "window": {"frame_start": 0, "frame_end": 1},
                    "assessment": "One claim beyond the domain maximum.",
                    "confidence": 0.5,
                    "evidence_ids": [citation_id],
                }
            ]
        now = datetime(2026, 8, 22, 17, 0, tzinfo=UTC)
        run = AnalysisRun(
            run_id=stable_uuid(f"domain-{violation}"),
            replay_id=replay.id,
            replay_player_id=None,
            provider="ollama",
            model_name="qwen3.6:27b",
            model_digest="7" * 64,
            prompt_version="strategy-report-v1",
            prompt_digest="c2603f4f4cff1b3563801d83a6c2e567700eba4a86fe3af83524bcb98d6693d7",
            response_schema_version="strategy-report-response-v1",
            response_schema_digest="a758faf24d931094b18ade9cf37c49bbe032686f7499180872f1d7bd7669b76e",
            settings_digest="a" * 64,
            input_digest="b" * 64,
            cache_key=hashlib.sha256(violation.encode()).hexdigest(),
            status="running",
            validated_response_json=response,
            diagnostics_json=[],
            created_at=now,
        )
        session.add(run)
        session.flush()
        run.status = "succeeded"
        run.completed_at = now
        session.commit()

    receipt = _service(report_database).create(
        ReportRequest(
            report_database.replay_public_id,
            include_validated_ollama=True,
            publish=False,
        )
    )
    assert receipt.document.ollama.status == "invalid"
    assert diagnostic in receipt.document.ollama.diagnostic_codes


def test_database_failure_keeps_published_content_but_rolls_back_asset_metadata(
    report_database: SeededReportDatabase,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    engine = factory.kw["bind"]

    def fail_report_insert(_connection, _cursor, statement, _parameters, _context, _many):  # type: ignore[no-untyped-def]
        if statement.startswith("INSERT INTO reports"):
            raise RuntimeError("simulated database failure")

    event.listen(engine, "before_cursor_execute", fail_report_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated database failure"):
            _service(report_database).create(ReportRequest(report_database.replay_public_id, publish=True))
    finally:
        event.remove(engine, "before_cursor_execute", fail_report_insert)

    store = ContentAddressedStore(report_database.settings.cache_directory / "reports")
    managed_files = [path for path in store.root.rglob("*") if path.is_file()]
    assert len(managed_files) == 2
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Report)) == 0
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 0
