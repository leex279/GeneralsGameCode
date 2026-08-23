from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    ParserRun,
    Player,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    ReplayQualityIssue,
    StrategyAssessment,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.importing.evidence_identity import (
    parser_command_evidence_identity,
    telemetry_event_evidence_identity,
)
from generals_replay_analyzer.longitudinal.segments import LongitudinalEvidenceDTO, LongitudinalMemberDTO


def stable_uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"task11:{label}"))


@dataclass(frozen=True)
class SeededReportDatabase:
    settings: AnalyzerSettings
    session_factory: object
    replay_public_id: str
    replay_player_public_id: str
    observed_evidence_id: str
    parser_observed_evidence_id: str
    derived_evidence_id: str
    inferred_evidence_id: str
    analysis_run_id: str


@pytest.fixture
def report_database(tmp_path: Path) -> SeededReportDatabase:
    settings = AnalyzerSettings(data_root=tmp_path / "external-task11-data")
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    replay_public_id = stable_uuid("replay")
    replay_player_public_id = stable_uuid("replay-player")
    parser_identity = parser_command_evidence_identity(replay_public_id, "parser-v1", 10)
    telemetry_run_public_id = stable_uuid("telemetry")
    telemetry_identity = telemetry_event_evidence_identity(telemetry_run_public_id, 0)
    observed_public_id = telemetry_identity.public_id
    parser_observed_public_id = parser_identity.public_id
    derived_public_id = stable_uuid("derived")
    analysis_run_id = stable_uuid("analysis-run")
    inferred_source_key = f"analysis-run:{analysis_run_id}:pressure"
    inferred_public_id = str(uuid5(NAMESPACE_URL, f"evidence:{inferred_source_key}"))
    longitudinal_run_id = stable_uuid("longitudinal-run")
    longitudinal_cache_key = "8" * 64
    longitudinal_source_key = (
        f"longitudinal:{longitudinal_run_id}:{longitudinal_cache_key}:income_average:metric:0:1200"
    )

    with factory() as session:
        replay = Replay(
            public_id=replay_public_id,
            sha256="a" * 64,
            replay_name="fixture.rep",
            version_string="1.04",
            version_number=104,
            frame_count=1200,
            start_time=1,
            end_time=2,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="Tournament Desert",
            seed=4,
            starting_cash=10000,
            header_json={"map_name": "Tournament Desert", "frame_count": 1200},
            lifecycle_state="partial",
            created_at=now,
            updated_at=now,
        )
        session.add(replay)
        session.flush()
        parser = ParserRun(
            run_id=stable_uuid("parser"),
            replay_id=replay.id,
            parser_version="parser-v1",
            schema_version=1,
            input_sha256=replay.sha256,
            result_sha256="b" * 64,
            status="running",
            completion_status=None,
            warnings_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(parser)
        session.flush()
        canonical_player = Player(
            public_id=stable_uuid("canonical-player"),
            display_name="Player",
            identity_revision=0,
            created_at=now,
            updated_at=now,
        )
        session.add(canonical_player)
        session.flush()
        player = ReplayPlayer(
            public_id=replay_player_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            player_id=canonical_player.id,
            slot_index=0,
            slot_kind="human",
            original_name="Player",
            normalized_name="player",
            player_index=0,
            observed_json={"faction": "USA", "team_id": 1},
        )
        session.add(player)
        session.flush()
        telemetry = TelemetryRun(
            run_id=telemetry_run_public_id,
            replay_id=replay.id,
            schema_version=2,
            engine_build="zh-1.04",
            engine_executable_sha256="c" * 64,
            settings_json={"parser_run_id": parser.run_id},
            status="running",
            runner_status="success",
            final_frame=1200,
            command_count=100,
            trace_sha256="d" * 64,
            diagnostics_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(telemetry)
        session.flush()
        parser_observed = EvidenceItem(
            public_id=parser_observed_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            tier="observed",
            source_kind="parser_command",
            source_key=parser_identity.source_key,
            schema_version=1,
            created_at=now,
        )
        observed = EvidenceItem(
            public_id=observed_public_id,
            replay_id=replay.id,
            telemetry_run_id=telemetry.id,
            tier="observed",
            source_kind="telemetry_event",
            source_key=telemetry_identity.source_key,
            schema_version=2,
            created_at=now,
        )
        derived = EvidenceItem(
            public_id=derived_public_id,
            replay_id=replay.id,
            telemetry_run_id=telemetry.id,
            tier="derived",
            source_kind="feature",
            source_key="feature:income:0",
            schema_version=1,
            created_at=now,
        )
        inferred = EvidenceItem(
            public_id=inferred_public_id,
            replay_id=replay.id,
            tier="inferred",
            source_kind="llm",
            source_key=inferred_source_key,
            schema_version=1,
            created_at=now,
        )
        rule_evidence = EvidenceItem(
            public_id=stable_uuid("rule-derived"),
            replay_id=replay.id,
            telemetry_run_id=telemetry.id,
            tier="derived",
            source_kind="strategy_rule",
            source_key="strategy:oil_grab:0",
            schema_version=1,
            created_at=now,
        )
        longitudinal_evidence = EvidenceItem(
            public_id=str(uuid5(NAMESPACE_URL, longitudinal_source_key)),
            replay_id=replay.id,
            tier="derived",
            source_kind="longitudinal_corpus",
            source_key=longitudinal_source_key,
            schema_version=1,
            created_at=now,
        )
        session.add_all((parser_observed, observed, derived, inferred, rule_evidence, longitudinal_evidence))
        session.flush()
        session.add(
            ReplayCommand(
                parser_run_id=parser.id,
                replay_id=replay.id,
                replay_player_id=player.id,
                command_index=0,
                frame=20,
                player_index=0,
                message_type=100,
                message_name="MSG_DOZER_CONSTRUCT",
                start_offset=10,
                end_offset=20,
                arguments_json={"template": "AmericaPowerPlant"},
                evidence_item_id=parser_observed.id,
            )
        )
        session.add(
            TelemetryEvent(
                telemetry_run_id=telemetry.id,
                sequence=0,
                frame=30,
                logic_time_seconds=1.0,
                schema_version=2,
                event_type="economy_sample",
                payload_json={"player_index": 0, "cash": 9000},
                raw_record_json={"type": "economy_sample"},
                evidence_item_id=observed.id,
            )
        )
        feature_set = FeatureSet(
            public_id=stable_uuid("feature-set"),
            replay_id=replay.id,
            replay_player_id=player.id,
            extractor_name="economy",
            extractor_version="economy-v1",
            input_digest="e" * 64,
            cache_key="f" * 64,
            status="running",
            settings_json={},
            completed_at=now,
            created_at=now,
        )
        session.add(feature_set)
        session.flush()
        feature = Feature(
            public_id=stable_uuid("feature"),
            feature_set_id=feature_set.id,
            evidence_item_id=derived.id,
            name="income_total",
            value_type="real",
            real_value=1234.56789,
            unit="credits",
            scope_type="player",
            scope_key=replay_player_public_id,
            replay_player_id=player.id,
            frame_start=0,
            frame_end=1200,
            quality="partial",
            quality_reason="sample_window_gap",
            details_json={"sample_count": 5},
        )
        session.add(feature)
        session.flush()
        session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=observed.id, role="input"))
        rule_assessment = StrategyAssessment(
            public_id=stable_uuid("rule-assessment"),
            evidence_item_id=rule_evidence.id,
            replay_id=replay.id,
            replay_player_id=player.id,
            method="rule",
            strategy_label="oil_grab",
            phase="opening",
            taxonomy_version="strategy-taxonomy-v1",
            rule_version="opening-rules-v1",
            frame_start=0,
            frame_end=300,
            quality="partial",
            confidence=0.75,
            details_json={"reason": "mixed_supported_candidates", "rule_score": 0.75},
            created_at=now,
        )
        session.add(rule_assessment)
        session.flush()
        session.add(
            AssessmentEvidence(assessment_id=rule_assessment.id, evidence_item_id=observed.id, role="supporting")
        )
        longitudinal_run = LongitudinalRun(
            run_id=longitudinal_run_id,
            player_id=canonical_player.id,
            identity_revision=0,
            analyzer_name="longitudinal-player-analysis",
            analyzer_version="longitudinal-player-analysis-v1",
            segment_key_json={"faction": "USA"},
            settings_json={
                "storage_schema": "longitudinal-run-settings-v1",
                "player_public_id": canonical_player.public_id,
                "settings": {"minimum_sample_size": 1},
                "metric_names": ["income_average"],
                "pattern_names": [],
                "definitions": [],
                "exclusions": [],
            },
            input_digest="7" * 64,
            cache_key=longitudinal_cache_key,
            status="running",
            created_at=now,
        )
        session.add(longitudinal_run)
        session.flush()
        longitudinal_member = LongitudinalMemberDTO(
            replay_public_id=replay.public_id,
            replay_sha256=replay.sha256,
            replay_player_public_id=player.public_id,
            feature_set_public_id=feature_set.public_id,
            feature_public_id=feature.public_id,
            evidence_public_id=derived.public_id,
            feature_name=feature.name,
            raw_value=feature.real_value,
            unit=feature.unit,
            frame_start=feature.frame_start,
            frame_end=feature.frame_end,
            quality="partial",
            reason=feature.quality_reason,
            replay_start_time=replay.start_time,
            replay_version=replay.version_string,
            lifecycle_state=replay.lifecycle_state,
            feature_set_extractor_name=feature_set.extractor_name,
            feature_set_extractor_version=feature_set.extractor_version,
            feature_set_input_digest=feature_set.input_digest,
            feature_set_settings=feature_set.settings_json,
            feature_value_type=feature.value_type,
            feature_scope_type=feature.scope_type,
            feature_scope_key=feature.scope_key,
            feature_details=feature.details_json,
            derived_evidence_source_kind=derived.source_kind,
            derived_evidence_source_key=derived.source_key,
            derived_evidence_schema_version=derived.schema_version,
            direct_evidence=(
                LongitudinalEvidenceDTO(
                    observed.public_id,
                    observed.tier,
                    observed.source_kind,
                    observed.source_key,
                    observed.schema_version,
                ),
            ),
        )
        longitudinal_result = LongitudinalResult(
            public_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"{longitudinal_run_id}:{longitudinal_cache_key}:metric:income_average",
                )
            ),
            longitudinal_run_id=longitudinal_run.id,
            evidence_item_id=longitudinal_evidence.id,
            result_name="income_average",
            result_kind="metric",
            sample_count=1,
            missing_count=0,
            quality="available",
            statistics_json={
                "storage_schema": "longitudinal-result-storage-v1",
                "public_statistics": {"mean": 1234.56789},
                "member_snapshots": [longitudinal_member.as_canonical()],
                "evidence_anchor": {
                    "role": "schema_required_corpus_anchor",
                    "replay_public_id": replay.public_id,
                    "replay_sha256": replay.sha256,
                    "replay_player_public_id": player.public_id,
                },
            },
        )
        session.add(longitudinal_result)
        session.flush()
        session.add(
            LongitudinalMember(
                longitudinal_result_id=longitudinal_result.id,
                replay_id=replay.id,
                replay_player_id=player.id,
                feature_set_id=feature_set.id,
                feature_id=feature.id,
                strategy_assessment_id=None,
                evidence_item_id=derived.id,
            )
        )
        session.add(
            ReplayQualityIssue(
                public_id=stable_uuid("issue"),
                replay_id=replay.id,
                telemetry_run_id=telemetry.id,
                evidence_item_id=observed.id,
                stage="telemetry",
                issue_code="sample_gap",
                severity="warning",
                details_json={"missing_count": 1},
                detected_at=now,
            )
        )
        analysis = AnalysisRun(
            run_id=analysis_run_id,
            replay_id=replay.id,
            replay_player_id=player.id,
            provider="ollama",
            model_name="qwen3.6:27b",
            model_digest="1" * 64,
            prompt_version="strategy-report-v1",
            prompt_digest="c2603f4f4cff1b3563801d83a6c2e567700eba4a86fe3af83524bcb98d6693d7",
            response_schema_version="strategy-report-response-v1",
            response_schema_digest="a758faf24d931094b18ade9cf37c49bbe032686f7499180872f1d7bd7669b76e",
            settings_digest="4" * 64,
            input_digest="5" * 64,
            cache_key="6" * 64,
            status="running",
            validated_response_json={
                "schema_version": "strategy-report-response-v1",
                "summary": "Evidence-bound pressure assessment.",
                "phase_assessments": [],
                "strategy_assessments": [
                    {
                        "claim_id": "pressure",
                        "strategy_label": "pressure",
                        "phase": "mid",
                        "window": {"frame_start": 300, "frame_end": 900},
                        "assessment": "Sustained pressure.",
                        "confidence": 0.8,
                        "evidence_ids": [derived_public_id],
                    }
                ],
                "comparative_observations": [],
                "strengths": [],
                "vulnerabilities": [],
                "uncertainty_notes": [],
            },
            diagnostics_json=[{"code": "ok"}],
            created_at=now,
        )
        session.add(analysis)
        session.flush()
        assessment = StrategyAssessment(
            public_id=str(uuid5(NAMESPACE_URL, f"strategy-assessment:{inferred_source_key}")),
            evidence_item_id=inferred.id,
            replay_id=replay.id,
            replay_player_id=player.id,
            analysis_run_id=analysis.id,
            method="llm",
            strategy_label="pressure",
            phase="mid",
            model_version="1" * 64,
            frame_start=300,
            frame_end=900,
            quality="partial",
            confidence=0.8,
            details_json={
                "assessment": "Sustained pressure.",
                "claim_id": "pressure",
                "cited_quality_reasons": ["sample_window_gap"],
                "minimum_cited_quality": "partial",
                "schema_version": "llm-strategy-assessment-v1",
            },
            created_at=now,
        )
        session.add(assessment)
        session.flush()
        session.add(AssessmentEvidence(assessment_id=assessment.id, evidence_item_id=derived.id, role="supporting"))
        session.flush()
        parser.status = "succeeded"
        parser.completion_status = "complete"
        telemetry.status = "succeeded"
        feature_set.status = "succeeded"
        longitudinal_run.status = "succeeded"
        longitudinal_run.completed_at = now
        analysis.status = "succeeded"
        analysis.completed_at = now
        session.commit()

    return SeededReportDatabase(
        settings,
        factory,
        replay_public_id,
        replay_player_public_id,
        observed_public_id,
        parser_observed_public_id,
        derived_public_id,
        inferred_public_id,
        analysis_run_id,
    )
