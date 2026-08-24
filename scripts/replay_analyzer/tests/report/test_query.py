from __future__ import annotations

import copy
import hashlib
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.codecs import (
    PlayerAssessmentSelection,
    PlayerLLMSelection,
    encode_assessment_output,
    encode_llm_output,
)
from generals_replay_analyzer.analysis_pipeline.identity_scope import (
    CanonicalPlayerIdentityBinding,
    IdentityAnalysisScope,
)
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    Entity,
    EvidenceItem,
    Feature,
    FeatureSet,
    Job,
    JobDependency,
    JobStageResult,
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
from generals_replay_analyzer.identity.audit import identity_cache_digest
from generals_replay_analyzer.importing.stages import (
    ANALYZE_LLM,
    ANALYZE_LLM_VERSION,
    ASSESS_STRATEGIES,
    ASSESS_STRATEGIES_VERSION,
    DERIVE_FEATURES,
    DERIVE_FEATURES_VERSION,
    IMPORT_OBSERVATIONS,
    IMPORT_OBSERVATIONS_VERSION,
    PARSE,
    PARSE_VERSION,
    RENDER_REPORT,
    RENDER_REPORT_VERSION,
    TELEMETRY,
    TELEMETRY_VERSION,
    content_key,
    input_digest,
)
from generals_replay_analyzer.llm.evidence_bundle import build_evidence_bundle
from generals_replay_analyzer.llm.schema import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    RESPONSE_SCHEMA_SHA256,
    RESPONSE_SCHEMA_VERSION,
)
from generals_replay_analyzer.report import query as report_query
from generals_replay_analyzer.report.model import (
    ReportDocument,
    ReportEvidenceRef,
    ReportRequest,
    document_to_mapping,
)
from generals_replay_analyzer.report.query import (
    EvidenceQuery,
    FixedReportQuery,
    LatestReportQuery,
    ReportGraphAmbiguousError,
    ReportGraphContractError,
    ReportGraphNotFoundError,
    ReportQueryService,
    TimelineChartQuery,
)
from generals_replay_analyzer.report.read_model import (
    DerivedAssessmentEvidenceDTO,
    DerivedFeatureEvidenceDTO,
    DerivedLongitudinalEvidenceDTO,
    InferredAssessmentEvidenceDTO,
    ObservedCommandEvidenceDTO,
    ObservedTelemetryEvidenceDTO,
    TimelineChartDTO,
    TimelineEvidenceDTO,
    TimelineFamilyOptionDTO,
    TimelineIntervalDTO,
    TimelineOptionDTO,
    TimelinePointDTO,
    TimelineSeriesDTO,
)
from generals_replay_analyzer.report.service import ReportService
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import SeededReportDatabase, stable_uuid


@dataclass(frozen=True)
class PublishedGraph:
    service: ReportQueryService
    replay_public_id: str
    replay_wide_report_id: str
    player_report_id: str
    player_public_id: str
    parser_evidence_id: str
    telemetry_evidence_id: str
    feature_evidence_id: str
    inferred_evidence_id: str
    rule_evidence_id: str
    longitudinal_evidence_id: str


def _job(
    *,
    public_id: str,
    replay_id: int,
    replay_sha256: str,
    stage: str,
    input_json: dict[str, object],
    output_json: dict[str, object] | None,
    now: datetime,
    component_version: str = "1",
    idempotency_key: str | None = None,
    status: str = "succeeded",
    retryable: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
    error_details_json: dict[str, object] | None = None,
) -> Job:
    return Job(
        public_id=public_id,
        replay_id=replay_id,
        stage=stage,
        component_version=component_version,
        idempotency_key=(
            idempotency_key
            or f"{stage}:{component_version}:{replay_sha256}:{hashlib.sha256(public_id.encode('utf-8')).hexdigest()}"
        ),
        status=status,
        priority=100,
        attempt_count=1,
        max_attempts=3,
        available_at=now,
        started_at=now,
        completed_at=None if status in {"pending", "running"} else now,
        input_json=input_json,
        output_json=output_json,
        error_code=error_code,
        error_message=error_message,
        error_details_json=error_details_json,
        retryable=retryable,
        created_at=now,
    )


def _stage_result(job: Job, output: dict[str, object], now: datetime, label: str) -> JobStageResult:
    return JobStageResult(
        public_id=stable_uuid(label),
        job_id=job.id,
        stage=job.stage,
        component_version=job.component_version,
        idempotency_key=job.idempotency_key,
        output_json=output,
        created_at=now,
    )


def _production_parse_output(session: Session, replay: Replay, parser: ParserRun) -> dict[str, object]:
    warning_codes = tuple(
        sorted(
            {
                item["code"]
                for item in parser.warnings_json
                if isinstance(item, dict) and isinstance(item.get("code"), str)
            }
        )
    )
    command_count = int(
        session.scalar(
            select(func.count()).select_from(ReplayCommand).where(ReplayCommand.parser_run_id == parser.id)
        )
        or 0
    )
    return {
        "parser_version": parser.parser_version,
        "content_sha256": replay.sha256,
        "completion_status": parser.completion_status,
        "command_count": command_count,
        "warning_codes": warning_codes,
        "command_stream_offset": parser.command_stream_offset,
        "end_offset": parser.end_offset,
    }


def _empty_response() -> dict[str, object]:
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "summary": "No replay-wide interpretation was required.",
        "phase_assessments": [],
        "strategy_assessments": [],
        "comparative_observations": [],
        "strengths": [],
        "vulnerabilities": [],
        "uncertainty_notes": [],
    }


def _publish_graph(database: SeededReportDatabase) -> PublishedGraph:
    factory = database.session_factory  # type: ignore[assignment]
    now = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    replay_wide_analysis_id = stable_uuid("query-replay-wide-analysis")
    replay_wide_feature_set_id = stable_uuid("query-replay-wide-feature-set")
    replay_wide_strategy_cache = "2" * 64
    player_strategy_cache = "3" * 64

    with factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == database.replay_public_id))
        player = session.scalar(select(ReplayPlayer).where(ReplayPlayer.public_id == database.replay_player_public_id))
        existing_feature_set = session.scalar(select(FeatureSet).where(FeatureSet.replay_player_id == player.id))
        assert replay is not None and player is not None and existing_feature_set is not None
        rule = session.scalar(
            select(StrategyAssessment).where(
                StrategyAssessment.replay_id == replay.id,
                StrategyAssessment.method == "rule",
            )
        )
        assert rule is not None
        rule_evidence = session.get(EvidenceItem, rule.evidence_item_id)
        assert rule_evidence is not None

        # Normalize the inherited Task 11 fixture to the production source identities
        # that this read-model is responsible for validating.
        rule_source_key = (
            f"strategy-rule:{replay.public_id}:{player.public_id}:{player_strategy_cache}:"
            f"{rule.strategy_label}:{rule.phase}:{rule.frame_start}:{rule.frame_end}"
        )
        rule_evidence.source_key = rule_source_key
        rule_evidence.public_id = str(uuid5(NAMESPACE_URL, f"evidence:{rule_source_key}"))
        rule.public_id = str(uuid5(NAMESPACE_URL, f"strategy-assessment:{rule_source_key}"))

        replay_wide_feature_set = FeatureSet(
            public_id=replay_wide_feature_set_id,
            replay_id=replay.id,
            replay_player_id=None,
            extractor_name="activity",
            extractor_version="activity-v1",
            input_digest="4" * 64,
            cache_key="5" * 64,
            status="succeeded",
            settings_json={},
            completed_at=now,
            created_at=now,
        )
        session.add(replay_wide_feature_set)
        session.add(
            AnalysisRun(
                run_id=replay_wide_analysis_id,
                replay_id=replay.id,
                replay_player_id=None,
                provider="ollama",
                model_name="qwen3.6:27b",
                model_digest="6" * 64,
                prompt_version=PROMPT_VERSION,
                prompt_digest=PROMPT_SHA256,
                response_schema_version=RESPONSE_SCHEMA_VERSION,
                response_schema_digest=RESPONSE_SCHEMA_SHA256,
                settings_digest="7" * 64,
                input_digest="8" * 64,
                cache_key="9" * 64,
                status="succeeded",
                validated_response_json=_empty_response(),
                diagnostics_json=[],
                created_at=now,
                completed_at=now,
            )
        )

    reports = ReportService(
        factory,
        settings=database.settings,
        store=ContentAddressedStore(database.settings.cache_directory / "reports"),
    )
    player_receipt = reports.create(
        ReportRequest(
            database.replay_public_id,
            database.replay_player_public_id,
            include_validated_ollama=True,
            publish=True,
            analysis_run_id=database.analysis_run_id,
        )
    )
    replay_receipt = reports.create(
        ReportRequest(
            database.replay_public_id,
            include_validated_ollama=True,
            publish=True,
            analysis_run_id=replay_wide_analysis_id,
        )
    )
    assert player_receipt.structured_asset is not None
    assert player_receipt.presentation_bundle_asset is not None
    assert replay_receipt.structured_asset is not None
    assert replay_receipt.presentation_bundle_asset is not None

    with factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == database.replay_public_id))
        player = session.scalar(select(ReplayPlayer).where(ReplayPlayer.public_id == database.replay_player_public_id))
        feature_set = session.scalar(select(FeatureSet).where(FeatureSet.replay_player_id == player.id))
        feature = session.scalar(select(Feature).where(Feature.feature_set_id == feature_set.id))
        parser = session.scalar(select(ParserRun).where(ParserRun.replay_id == replay.id))
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.replay_id == replay.id))
        rule_evidence = session.scalar(select(EvidenceItem).where(EvidenceItem.source_kind == "strategy_rule"))
        longitudinal_evidence = session.scalar(
            select(EvidenceItem).where(EvidenceItem.source_kind == "longitudinal_corpus")
        )
        assert (
            replay is not None
            and player is not None
            and feature_set is not None
            and parser is not None
            and telemetry is not None
        )
        assert feature is not None and rule_evidence is not None and longitudinal_evidence is not None
        if parser.command_stream_offset is None or parser.end_offset is None:
            session.execute(text("DROP TRIGGER trg_parser_runs_succeeded_no_update"))
            session.execute(
                text("UPDATE parser_runs SET command_stream_offset = 0, end_offset = 1 WHERE id = :id"),
                {"id": parser.id},
            )
            session.execute(
                text(
                    "CREATE TRIGGER trg_parser_runs_succeeded_no_update BEFORE UPDATE ON parser_runs "
                    "WHEN OLD.status = 'succeeded' BEGIN SELECT RAISE(ABORT, "
                    "'successful observation run is immutable'); END"
                )
            )
            session.expire(parser)
        canonical_player = session.get(Player, player.player_id)
        assert canonical_player is not None
        bundle = build_evidence_bundle(
            replay_public_id=replay.public_id,
            replay_sha256=replay.sha256,
            claims=(),
        )
        assessment_output = encode_assessment_output(
            (
                PlayerAssessmentSelection(
                    None,
                    None,
                    (replay_wide_feature_set_id,),
                    replay_wide_strategy_cache,
                    "not_applicable",
                    None,
                    bundle,
                ),
                PlayerAssessmentSelection(
                    player.public_id,
                    stable_uuid("canonical-player"),
                    (feature_set.public_id,),
                    player_strategy_cache,
                    "succeeded",
                    stable_uuid("longitudinal-run"),
                    bundle,
                ),
            )
        )
        llm_output = encode_llm_output(
            (
                PlayerLLMSelection(None, replay_wide_analysis_id, "succeeded", "analysis_succeeded"),
                PlayerLLMSelection(
                    player.public_id,
                    database.analysis_run_id,
                    "succeeded",
                    "analysis_succeeded",
                ),
            )
        )
        report_output = {
            "schema_version": "report-output-v1",
            "reports": [
                {
                    "replay_player_public_id": None,
                    "analysis_run_id": replay_wide_analysis_id,
                    "structured_asset_public_id": replay_receipt.structured_asset.public_id,
                    "presentation_asset_public_id": replay_receipt.presentation_bundle_asset.public_id,
                },
                {
                    "replay_player_public_id": player.public_id,
                    "analysis_run_id": database.analysis_run_id,
                    "structured_asset_public_id": player_receipt.structured_asset.public_id,
                    "presentation_asset_public_id": player_receipt.presentation_bundle_asset.public_id,
                },
            ],
        }
        branch_recipe = {
            "import_observations_version": IMPORT_OBSERVATIONS_VERSION,
            "import_mode": "copy",
            "parse": {
                "import_mode": "copy",
                "parse_version": PARSE_VERSION,
                "parser_version": parser.parser_version,
            },
            "telemetry": {
                "acquirer_version": "fixture-telemetry",
                "import_mode": "copy",
                "telemetry_version": TELEMETRY_VERSION,
            },
        }
        parse_input = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "import_mode": "copy",
        }
        parse_output = _production_parse_output(session, replay, parser)
        parse_job = _job(
            public_id=stable_uuid("query-parse-job"),
            replay_id=replay.id,
            replay_sha256=replay.sha256,
            stage=PARSE,
            component_version=PARSE_VERSION,
            idempotency_key=content_key(PARSE, PARSE_VERSION, replay.sha256, branch_recipe["parse"]),
            input_json=parse_input,
            output_json=parse_output,
            now=now,
        )
        telemetry_output = {
            "artifacts": [],
            "diagnostics": [],
            "engine_build": telemetry.engine_build,
            "engine_executable_sha256": telemetry.engine_executable_sha256,
            "exit_code": telemetry.process_exit_code,
            "replay_quality": "complete",
            "run_id": telemetry.run_id,
            "runner_status": telemetry.runner_status,
            "strategy_analysis_scope": "full",
        }
        telemetry_job = _job(
            public_id=stable_uuid("query-telemetry-job"),
            replay_id=replay.id,
            replay_sha256=replay.sha256,
            stage=TELEMETRY,
            component_version=TELEMETRY_VERSION,
            idempotency_key=content_key(
                TELEMETRY,
                TELEMETRY_VERSION,
                replay.sha256,
                cast(dict[str, object], branch_recipe["telemetry"]),
            ),
            input_json=parse_input,
            output_json=telemetry_output,
            now=now,
        )
        selected_identity = {
            "replay_sha256": replay.sha256,
            "branch_recipe": branch_recipe,
            "dependencies": [
                {
                    "stage": PARSE,
                    "component_version": PARSE_VERSION,
                    "status": "succeeded",
                    "input": {"import_mode": "copy", "replay_sha256": replay.sha256},
                    "output": parse_output,
                },
                {
                    "stage": TELEMETRY,
                    "component_version": TELEMETRY_VERSION,
                    "status": "succeeded",
                    "input": {"import_mode": "copy", "replay_sha256": replay.sha256},
                    "output": telemetry_output,
                },
            ],
        }
        selected_dependency_digest = input_digest(selected_identity)
        observation_idempotency_key = content_key(
            IMPORT_OBSERVATIONS,
            IMPORT_OBSERVATIONS_VERSION,
            replay.sha256,
            selected_identity,
        )
        session.execute(text("DROP TRIGGER trg_telemetry_runs_succeeded_no_update"))
        telemetry.strategy_analysis_scope = "full"
        telemetry.settings_json = {
            "replay_quality": "complete",
            "attempt_engine_build": telemetry.engine_build,
            "parser_run_id": parser.run_id,
            "artifact_manifest": [],
            "upstream_failure_code": None,
            "upstream_quality_issue_code": None,
            "upstream_failure_message": None,
            "import_observations_idempotency_key": observation_idempotency_key,
        }
        session.flush()
        session.execute(
            text(
                "CREATE TRIGGER trg_telemetry_runs_succeeded_no_update BEFORE UPDATE ON telemetry_runs "
                "WHEN OLD.status = 'succeeded' BEGIN SELECT RAISE(ABORT, "
                "'successful observation run is immutable'); END"
            )
        )
        base_input = {
            "analysis_plan_version": 1,
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "parser_run_id": parser.run_id,
            "parser_version": parser.parser_version,
            "selected_dependency_digest": selected_dependency_digest,
            "identity_scope": IdentityAnalysisScope(
                "full_replay",
                (
                    CanonicalPlayerIdentityBinding(
                        player.public_id,
                        canonical_player.public_id,
                        canonical_player.identity_revision,
                        identity_cache_digest(
                            canonical_player.public_id,
                            canonical_player.identity_revision,
                        ),
                    ),
                ),
            ).to_json(),
        }
        observation_job = _job(
            public_id=stable_uuid("query-observation-job"),
            replay_id=replay.id,
            replay_sha256=replay.sha256,
            stage=IMPORT_OBSERVATIONS,
            component_version=IMPORT_OBSERVATIONS_VERSION,
            idempotency_key=observation_idempotency_key,
            input_json={
                "replay_public_id": replay.public_id,
                "replay_sha256": replay.sha256,
                "branch_recipe": branch_recipe,
                "dependency_identity_bound": True,
                "provisional_idempotency_key": content_key(
                    IMPORT_OBSERVATIONS,
                    IMPORT_OBSERVATIONS_VERSION,
                    replay.sha256,
                    branch_recipe,
                ),
                "selected_dependency_digest": selected_dependency_digest,
            },
            output_json={
                "idempotency_key": observation_idempotency_key,
                "parser_run_id": parser.run_id,
                "parser_command_count": 1,
                "telemetry_run_id": telemetry.run_id,
                "telemetry_event_count": 1,
            },
            now=now,
        )
        base_identity = {
            "analysis_plan_version": 1,
            "observation_job_key": observation_job.idempotency_key,
            "selected_dependency_digest": base_input["selected_dependency_digest"],
            "identity_scope": base_input["identity_scope"],
        }
        derive_job = _job(
            public_id=stable_uuid("query-derive-job"),
            replay_id=replay.id,
            replay_sha256=replay.sha256,
            stage=DERIVE_FEATURES,
            component_version=DERIVE_FEATURES_VERSION,
            idempotency_key=content_key(
                DERIVE_FEATURES,
                DERIVE_FEATURES_VERSION,
                replay.sha256,
                base_identity,
            ),
            input_json=base_input,
            output_json={"schema_version": "feature-output-v1"},
            now=now,
        )
        assess_identity = {**base_identity, "derive_job_key": derive_job.idempotency_key}
        assess_job = _job(
            public_id=stable_uuid("query-assess-job"),
            replay_id=replay.id,
            replay_sha256=replay.sha256,
            stage=ASSESS_STRATEGIES,
            component_version=ASSESS_STRATEGIES_VERSION,
            idempotency_key=content_key(
                ASSESS_STRATEGIES,
                ASSESS_STRATEGIES_VERSION,
                replay.sha256,
                assess_identity,
            ),
            input_json={**base_input, "derive_input_digest": input_digest(assess_identity)},
            output_json=assessment_output,
            now=now,
        )
        llm_identity = {
            **base_identity,
            "assess_job_key": assess_job.idempotency_key,
            "provider_mode": "ollama",
        }
        llm_job = _job(
            public_id=stable_uuid("query-llm-job"),
            replay_id=replay.id,
            replay_sha256=replay.sha256,
            stage=ANALYZE_LLM,
            component_version=ANALYZE_LLM_VERSION,
            idempotency_key=content_key(
                ANALYZE_LLM,
                ANALYZE_LLM_VERSION,
                replay.sha256,
                llm_identity,
            ),
            input_json={
                **base_input,
                "allow_ollama": True,
                "assess_input_digest": input_digest(llm_identity),
            },
            output_json=llm_output,
            now=now,
        )
        report_identity = {
            **base_identity,
            "allow_ollama": True,
            "assess_job_key": assess_job.idempotency_key,
            "llm_job_key": llm_job.idempotency_key,
        }
        report_job = _job(
            public_id=stable_uuid("query-report-job"),
            replay_id=replay.id,
            replay_sha256=replay.sha256,
            stage=RENDER_REPORT,
            component_version=RENDER_REPORT_VERSION,
            idempotency_key=content_key(
                RENDER_REPORT,
                RENDER_REPORT_VERSION,
                replay.sha256,
                report_identity,
            ),
            input_json={
                **base_input,
                "allow_ollama": True,
                "report_input_digest": input_digest(report_identity),
            },
            output_json=report_output,
            now=now,
        )
        session.add_all((parse_job, telemetry_job, observation_job, derive_job, assess_job, llm_job, report_job))
        session.flush()
        session.add_all(
            (
                JobDependency(job_id=observation_job.id, depends_on_job_id=parse_job.id, created_at=now),
                JobDependency(job_id=observation_job.id, depends_on_job_id=telemetry_job.id, created_at=now),
                JobDependency(job_id=telemetry_job.id, depends_on_job_id=parse_job.id, created_at=now),
                JobDependency(job_id=derive_job.id, depends_on_job_id=observation_job.id, created_at=now),
                JobDependency(job_id=assess_job.id, depends_on_job_id=derive_job.id, created_at=now),
                JobDependency(job_id=llm_job.id, depends_on_job_id=assess_job.id, created_at=now),
                JobDependency(job_id=report_job.id, depends_on_job_id=llm_job.id, created_at=now),
                _stage_result(parse_job, parse_output, now, "query-parse-result"),
                _stage_result(telemetry_job, telemetry_output, now, "query-telemetry-result"),
                _stage_result(
                    observation_job,
                    {
                        "idempotency_key": observation_idempotency_key,
                        "parser_run_id": parser.run_id,
                        "parser_command_count": 1,
                        "telemetry_run_id": telemetry.run_id,
                        "telemetry_event_count": 1,
                    },
                    now,
                    "query-observation-result",
                ),
                _stage_result(
                    derive_job,
                    {"schema_version": "feature-output-v1"},
                    now,
                    "query-derive-result",
                ),
                _stage_result(assess_job, assessment_output, now, "query-assess-result"),
                _stage_result(llm_job, llm_output, now, "query-llm-result"),
                _stage_result(report_job, report_output, now, "query-report-result"),
            )
        )

    return PublishedGraph(
        ReportQueryService(
            factory,
            settings=database.settings,
            store=ContentAddressedStore(database.settings.cache_directory / "reports"),
        ),
        database.replay_public_id,
        replay_receipt.document.report_public_id,
        player_receipt.document.report_public_id,
        database.replay_player_public_id,
        database.parser_observed_evidence_id,
        database.observed_evidence_id,
        database.derived_evidence_id,
        database.inferred_evidence_id,
        rule_evidence.public_id,
        longitudinal_evidence.public_id,
    )


@pytest.fixture
def published_graph(report_database: SeededReportDatabase) -> PublishedGraph:
    return _publish_graph(report_database)


def _row_counts(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with factory() as session:
        return (
            session.scalar(select(func.count()).select_from(Report)) or 0,
            session.scalar(select(func.count()).select_from(Job)) or 0,
            session.scalar(select(func.count()).select_from(ManagedAsset)) or 0,
        )


def _add_legacy_report_claimant(
    session: Session,
    original: Job,
    now: datetime,
    *,
    label: str,
    poison: str | None = None,
) -> Job:
    direct_id = session.scalar(select(JobDependency.depends_on_job_id).where(JobDependency.job_id == original.id))
    assert direct_id is not None
    direct = session.get(Job, direct_id)
    assert direct is not None
    if direct.stage == ANALYZE_LLM:
        assess_id = session.scalar(select(JobDependency.depends_on_job_id).where(JobDependency.job_id == direct.id))
        assert assess_id is not None
        assess = session.get(Job, assess_id)
    else:
        assess = direct
    assert assess is not None and isinstance(assess.output_json, dict)
    derive_id = session.scalar(select(JobDependency.depends_on_job_id).where(JobDependency.job_id == assess.id))
    assert derive_id is not None
    derive = session.get(Job, derive_id)
    assert derive is not None and isinstance(derive.output_json, dict)
    observation_id = session.scalar(select(JobDependency.depends_on_job_id).where(JobDependency.job_id == derive.id))
    assert observation_id is not None
    observation = session.get(Job, observation_id)
    assert observation is not None and isinstance(observation.output_json, dict)
    parser_run_id = observation.output_json["parser_run_id"]
    parser = session.scalar(select(ParserRun).where(ParserRun.run_id == parser_run_id))
    assert parser is not None
    import_mode = "reference" if poison == "parse_run_mismatch" else "copy"
    telemetry_variant = poison if poison in {
        "cross_branch",
        "telemetry_success",
        "telemetry_failed",
        "telemetry_run_mismatch",
        "telemetry_retryable",
        "telemetry_nonterminal",
        "telemetry_failure_mismatch",
        "telemetry_dependency_failed",
        "telemetry_dependency_failed_claims_run",
        "observation_extra_key",
        "observation_parser_count_mismatch",
        "observation_telemetry_count_mismatch",
    } else (
        "telemetry_selected"
        if poison in {None, "invalid_result", "divergent_assets"}
        else None
    )
    branch_recipe = {
        "import_mode": import_mode,
        "import_observations_version": IMPORT_OBSERVATIONS_VERSION,
        "parse": {
            "import_mode": import_mode,
            "parse_version": PARSE_VERSION,
            "parser_version": parser.parser_version,
        },
        "telemetry": (
            {
                "acquirer_version": (
                    "fixture-telemetry"
                    if telemetry_variant == "telemetry_selected"
                    else f"fixture-telemetry-{label}"
                ),
                "import_mode": import_mode,
                "telemetry_version": TELEMETRY_VERSION,
            }
            if telemetry_variant is not None
            else None
        ),
    }
    if poison == "permissive_recipe":
        branch_recipe["unexpected"] = "poison"
    replay_input = dict(original.input_json)
    replay_input = {
        "replay_public_id": replay_input["replay_public_id"],
        "replay_sha256": replay_input["replay_sha256"],
    }
    parse_input = {**replay_input, "import_mode": import_mode}
    replay = session.get(Replay, original.replay_id)
    assert replay is not None
    parse_output = _production_parse_output(session, replay, parser)
    if poison == "parse_run_mismatch":
        parse_output["parser_version"] = "mismatched-parser-version"
    legacy_parse = _job(
        public_id=stable_uuid(f"{label}-parse"),
        replay_id=original.replay_id,  # type: ignore[arg-type]
        replay_sha256=cast(str, replay_input["replay_sha256"]),
        stage=PARSE,
        input_json=parse_input,
        output_json=parse_output,
        now=now,
        idempotency_key=content_key(
            PARSE,
            PARSE_VERSION,
            cast(str, replay_input["replay_sha256"]),
            branch_recipe["parse"],
        ),
    )
    existing_parse = session.scalar(select(Job).where(Job.idempotency_key == legacy_parse.idempotency_key))
    parse_is_new = existing_parse is None
    if existing_parse is not None:
        legacy_parse = existing_parse
        assert isinstance(legacy_parse.output_json, dict)
        parse_output = dict(legacy_parse.output_json)
    dependency_identity: dict[str, object] = {
        "stage": PARSE,
        "component_version": PARSE_VERSION,
        "status": "succeeded",
        "input": {key: value for key, value in parse_input.items() if key != "replay_public_id"},
        "output": parse_output,
    }
    cross_parse = None
    legacy_telemetry = None
    telemetry_run = None
    telemetry_parent = None
    telemetry_observation_run_id = None
    dependency_identities: list[dict[str, object]] = [dependency_identity]
    if telemetry_variant is not None:
        telemetry_parser_run_id = parser.run_id
        telemetry_parent = legacy_parse
        selected_telemetry_run_id = observation.output_json.get("telemetry_run_id")
        selected_telemetry = (
            None
            if telemetry_variant != "telemetry_selected"
            else session.scalar(
                select(TelemetryRun).where(TelemetryRun.run_id == selected_telemetry_run_id)
            )
        )
        if telemetry_variant == "telemetry_selected":
            assert selected_telemetry is not None
        telemetry_run_id = (
            selected_telemetry.run_id
            if selected_telemetry is not None
            else stable_uuid(f"{label}-telemetry-run")
        )
        telemetry_observation_run_id = (
            stable_uuid(f"{label}-mismatched-telemetry-run")
            if telemetry_variant == "telemetry_run_mismatch"
            else telemetry_run_id
        )
        telemetry_attempt = {
            "artifacts": [],
            "diagnostics": [],
            "engine_build": (
                selected_telemetry.engine_build
                if selected_telemetry is not None
                else "fixture-engine-v1"
            ),
            "engine_executable_sha256": (
                selected_telemetry.engine_executable_sha256
                if selected_telemetry is not None
                else "e" * 64
            ),
            "exit_code": (
                selected_telemetry.process_exit_code
                if selected_telemetry is not None
                else 0
            ),
            "replay_quality": "complete",
            "run_id": telemetry_run_id,
            "runner_status": "success",
            "strategy_analysis_scope": "full",
        }
        telemetry_status = "succeeded"
        telemetry_retryable = False
        telemetry_error_code = None
        telemetry_error_message = None
        telemetry_error_details: dict[str, object] | None = None
        cross_parse_identity = {
            "import_mode": "copy",
            "parse_version": PARSE_VERSION,
            "parser_version": "cross-branch-parser",
        }
        if telemetry_variant == "cross_branch":
            cross_parser = ParserRun(
                run_id=stable_uuid(f"{label}-cross-parser-run"),
                replay_id=replay.id,
                parser_version="cross-branch-parser",
                schema_version=1,
                input_sha256=replay.sha256,
                result_sha256="f" * 64,
                status="succeeded",
                completion_status="complete",
                command_stream_offset=0,
                end_offset=0,
                warnings_json=[],
                error_json=None,
                started_at=now,
                completed_at=now,
            )
            session.add(cross_parser)
            session.flush()
            telemetry_parser_run_id = cross_parser.run_id
            cross_parse = _job(
                public_id=stable_uuid(f"{label}-cross-parse"),
                replay_id=original.replay_id,  # type: ignore[arg-type]
                replay_sha256=cast(str, replay_input["replay_sha256"]),
                stage=PARSE,
                input_json=parse_input,
                output_json=_production_parse_output(session, replay, cross_parser),
                now=now,
                idempotency_key=content_key(
                    PARSE,
                    PARSE_VERSION,
                    cast(str, replay_input["replay_sha256"]),
                    cross_parse_identity,
                ),
            )
            telemetry_parent = cross_parse
        if telemetry_variant in {
            "telemetry_failed",
            "telemetry_retryable",
            "telemetry_failure_mismatch",
            "telemetry_dependency_failed",
            "telemetry_dependency_failed_claims_run",
        }:
            telemetry_attempt["runner_status"] = "crash"
            telemetry_attempt["exit_code"] = 1
            telemetry_status = "failed"
            telemetry_retryable = telemetry_variant == "telemetry_retryable"
            dependency_failed = telemetry_variant in {
                "telemetry_dependency_failed",
                "telemetry_dependency_failed_claims_run",
            }
            telemetry_error_code = "dependency_failed" if dependency_failed else "exporter_failure"
            telemetry_error_message = (
                "selected parser dependency failed"
                if dependency_failed
                else "telemetry acquisition did not succeed"
            )
            envelope_failure_code = (
                "invalid_telemetry_artifact"
                if telemetry_variant == "telemetry_failure_mismatch"
                else telemetry_error_code
            )
            telemetry_error_details = (
                {"dependency_stage": PARSE}
                if dependency_failed
                else {
                    "failure_envelope": {
                        "attempt": telemetry_attempt,
                        "failure_code": envelope_failure_code,
                        "failure_message": telemetry_error_message,
                        "quality_issue_code": "exporter_failure",
                        "type": "telemetry_artifact_failure",
                        "version": 1,
                    }
                }
            )
            if telemetry_variant == "telemetry_dependency_failed":
                telemetry_observation_run_id = None
            telemetry_output = None
            dependency_telemetry_evidence: dict[str, object] = {
                "stage": TELEMETRY,
                "component_version": TELEMETRY_VERSION,
                "status": "failed",
                "input": {key: value for key, value in parse_input.items() if key != "replay_public_id"},
                "error": {
                    "code": telemetry_error_code,
                    "message": telemetry_error_message,
                    "details": telemetry_error_details,
                },
            }
        elif telemetry_variant == "telemetry_nonterminal":
            telemetry_status = "pending"
            telemetry_output = None
            dependency_telemetry_evidence = {
                "stage": TELEMETRY,
                "component_version": TELEMETRY_VERSION,
                "status": "pending",
                "input": {key: value for key, value in parse_input.items() if key != "replay_public_id"},
            }
        else:
            telemetry_output = telemetry_attempt
            dependency_telemetry_evidence = {
                "stage": TELEMETRY,
                "component_version": TELEMETRY_VERSION,
                "status": "succeeded",
                "input": {key: value for key, value in parse_input.items() if key != "replay_public_id"},
                "output": telemetry_output,
            }
        legacy_telemetry = _job(
            public_id=stable_uuid(f"{label}-telemetry"),
            replay_id=original.replay_id,  # type: ignore[arg-type]
            replay_sha256=cast(str, replay_input["replay_sha256"]),
            stage=TELEMETRY,
            input_json=parse_input,
            output_json=telemetry_output,
            now=now,
            status=telemetry_status,
            retryable=telemetry_retryable,
            error_code=telemetry_error_code,
            error_message=telemetry_error_message,
            error_details_json=telemetry_error_details,
            idempotency_key=content_key(
                TELEMETRY,
                TELEMETRY_VERSION,
                cast(str, replay_input["replay_sha256"]),
                cast(dict[str, object], branch_recipe["telemetry"]),
            ),
        )
        existing_telemetry = session.scalar(
            select(Job).where(Job.idempotency_key == legacy_telemetry.idempotency_key)
        )
        telemetry_is_new = existing_telemetry is None
        if existing_telemetry is not None:
            legacy_telemetry = existing_telemetry
        dependency_identities.append(dependency_telemetry_evidence)
    selected_identity = {
        "replay_sha256": replay_input["replay_sha256"],
        "branch_recipe": branch_recipe,
        "dependencies": dependency_identities,
    }
    selected_digest = input_digest(selected_identity)
    legacy_observation_idempotency_key = content_key(
        IMPORT_OBSERVATIONS,
        IMPORT_OBSERVATIONS_VERSION,
        cast(str, replay_input["replay_sha256"]),
        selected_identity,
    )
    observation_output = {
        "idempotency_key": legacy_observation_idempotency_key,
        "parser_run_id": parser.run_id,
        "parser_command_count": parse_output["command_count"],
        "telemetry_run_id": telemetry_observation_run_id,
        "telemetry_event_count": 1 if telemetry_variant == "telemetry_selected" else 0,
    }
    if poison == "observation_extra_key":
        observation_output["unexpected"] = "poison"
    elif poison == "observation_parser_count_mismatch":
        observation_output["parser_command_count"] = cast(int, parse_output["command_count"]) + 1
    elif poison == "observation_telemetry_count_mismatch":
        observation_output["telemetry_event_count"] = 1
    legacy_observation = _job(
        public_id=stable_uuid(f"{label}-observation"),
        replay_id=original.replay_id,  # type: ignore[arg-type]
        replay_sha256=cast(str, replay_input["replay_sha256"]),
        stage=IMPORT_OBSERVATIONS,
        input_json={
            **replay_input,
            "branch_recipe": branch_recipe,
            "dependency_identity_bound": True,
            "provisional_idempotency_key": content_key(
                IMPORT_OBSERVATIONS,
                IMPORT_OBSERVATIONS_VERSION,
                cast(str, replay_input["replay_sha256"]),
                branch_recipe,
            ),
            "selected_dependency_digest": selected_digest,
        },
        output_json=observation_output,
        now=now,
        idempotency_key=legacy_observation_idempotency_key,
    )
    existing_observation = session.scalar(
        select(Job).where(Job.idempotency_key == legacy_observation.idempotency_key)
    )
    observation_is_new = existing_observation is None
    if existing_observation is not None:
        legacy_observation = existing_observation
        assert isinstance(legacy_observation.output_json, dict)
        observation_output = dict(legacy_observation.output_json)
    if telemetry_variant is not None and telemetry_variant not in {
        "telemetry_selected",
        "telemetry_dependency_failed",
        "telemetry_dependency_failed_claims_run",
        "telemetry_nonterminal",
    }:
        assert legacy_telemetry is not None and telemetry_parent is not None
        failed_telemetry = telemetry_status == "failed"
        telemetry_run = TelemetryRun(
            run_id=telemetry_run_id,
            replay_id=replay.id,
            trace_asset_id=None,
            catalog_asset_id=None,
            map_asset_id=None,
            map_id=None,
            schema_version=0 if failed_telemetry else 1,
            engine_build=cast(str, telemetry_attempt["engine_build"]),
            engine_executable_sha256=cast(str, telemetry_attempt["engine_executable_sha256"]),
            settings_json={
                "replay_quality": telemetry_attempt["replay_quality"],
                "attempt_engine_build": telemetry_attempt["engine_build"],
                "parser_run_id": telemetry_parser_run_id,
                "artifact_manifest": telemetry_attempt["artifacts"],
                "upstream_failure_code": telemetry_error_code if failed_telemetry else None,
                "upstream_quality_issue_code": "exporter_failure" if failed_telemetry else None,
                "upstream_failure_message": telemetry_error_message if failed_telemetry else None,
                "import_observations_idempotency_key": legacy_observation.idempotency_key,
            },
            status="failed" if failed_telemetry else "succeeded",
            runner_status=cast(str, telemetry_attempt["runner_status"]),
            strategy_analysis_scope=cast(str, telemetry_attempt["strategy_analysis_scope"]),
            process_exit_code=cast(int, telemetry_attempt["exit_code"]),
            final_frame=None if failed_telemetry else 0,
            command_count=None if failed_telemetry else 0,
            trace_sha256=None,
            diagnostics_json=[],
            started_at=now,
            completed_at=now,
        )
    legacy_derive = _job(
        public_id=stable_uuid(f"{label}-derive"),
        replay_id=original.replay_id,  # type: ignore[arg-type]
        replay_sha256=cast(str, replay_input["replay_sha256"]),
        stage=DERIVE_FEATURES,
        input_json=replay_input,
        output_json=dict(derive.output_json),
        now=now,
        idempotency_key=content_key(
            DERIVE_FEATURES,
            DERIVE_FEATURES_VERSION,
            cast(str, replay_input["replay_sha256"]),
            {"derive_features_version": DERIVE_FEATURES_VERSION, "observations": branch_recipe},
        ),
    )
    legacy_assess = _job(
        public_id=stable_uuid(f"{label}-assess"),
        replay_id=original.replay_id,  # type: ignore[arg-type]
        replay_sha256=cast(str, replay_input["replay_sha256"]),
        stage=ASSESS_STRATEGIES,
        input_json=replay_input,
        output_json=dict(assess.output_json),
        now=now,
        idempotency_key=content_key(
            ASSESS_STRATEGIES,
            ASSESS_STRATEGIES_VERSION,
            cast(str, replay_input["replay_sha256"]),
            {"assess_strategies_version": ASSESS_STRATEGIES_VERSION, "observations": branch_recipe},
        ),
    )
    legacy_llm = None
    if direct.stage == ANALYZE_LLM:
        assert isinstance(direct.output_json, dict)
        legacy_llm = _job(
            public_id=stable_uuid(f"{label}-llm"),
            replay_id=original.replay_id,  # type: ignore[arg-type]
            replay_sha256=cast(str, replay_input["replay_sha256"]),
            stage=ANALYZE_LLM,
            input_json=replay_input,
            output_json=dict(direct.output_json),
            now=now,
            idempotency_key=content_key(
                ANALYZE_LLM,
                ANALYZE_LLM_VERSION,
                cast(str, replay_input["replay_sha256"]),
                {"analyze_llm_version": ANALYZE_LLM_VERSION, "observations": branch_recipe},
            ),
        )
    report_output = copy.deepcopy(original.output_json)
    assert isinstance(report_output, dict)
    if poison == "divergent_assets":
        reports = report_output["reports"]
        assert isinstance(reports, list) and len(reports) == 2
        assert isinstance(reports[0], dict) and isinstance(reports[1], dict)
        reports[0]["presentation_asset_public_id"] = reports[1]["presentation_asset_public_id"]
    legacy_report = _job(
        public_id=stable_uuid(f"{label}-report"),
        replay_id=original.replay_id,  # type: ignore[arg-type]
        replay_sha256=cast(str, replay_input["replay_sha256"]),
        stage=RENDER_REPORT,
        component_version=RENDER_REPORT_VERSION,
        input_json=replay_input,
        output_json=report_output,
        now=now,
        idempotency_key=content_key(
            RENDER_REPORT,
            RENDER_REPORT_VERSION,
            cast(str, replay_input["replay_sha256"]),
            {"render_report_version": RENDER_REPORT_VERSION, "observations": branch_recipe},
        ),
    )
    jobs: list[object] = [legacy_derive, legacy_assess, legacy_report]
    if observation_is_new:
        jobs.append(legacy_observation)
    if parse_is_new:
        jobs.append(legacy_parse)
    if cross_parse is not None:
        jobs.append(cross_parse)
    if legacy_telemetry is not None and telemetry_is_new:
        jobs.append(legacy_telemetry)
    if telemetry_run is not None:
        jobs.append(telemetry_run)
    if legacy_llm is not None:
        jobs.append(legacy_llm)
    session.add_all(jobs)
    session.flush()
    session.add_all(
        (
            JobDependency(job_id=legacy_derive.id, depends_on_job_id=legacy_observation.id, created_at=now),
            JobDependency(job_id=legacy_assess.id, depends_on_job_id=legacy_derive.id, created_at=now),
            JobDependency(job_id=legacy_report.id, depends_on_job_id=legacy_assess.id, created_at=now),
            _stage_result(legacy_derive, dict(derive.output_json), now, f"{label}-derive-result"),
            _stage_result(legacy_assess, dict(assess.output_json), now, f"{label}-assess-result"),
            _stage_result(
                legacy_report,
                {"schema_version": "report-output-v1", "reports": []}
                if poison == "invalid_result"
                else report_output,
                now,
                f"{label}-report-result",
            ),
        )
    )
    if observation_is_new:
        session.add(JobDependency(job_id=legacy_observation.id, depends_on_job_id=legacy_parse.id, created_at=now))
        session.add(
            _stage_result(legacy_observation, observation_output, now, f"{label}-observation-result")
        )
    if parse_is_new:
        session.add(_stage_result(legacy_parse, parse_output, now, f"{label}-parse-result"))
    if legacy_telemetry is not None and telemetry_is_new:
        assert telemetry_parent is not None
        session.add_all(
            (
                JobDependency(job_id=legacy_observation.id, depends_on_job_id=legacy_telemetry.id, created_at=now),
                JobDependency(job_id=legacy_telemetry.id, depends_on_job_id=telemetry_parent.id, created_at=now),
            )
        )
        if legacy_telemetry.status == "succeeded":
            assert isinstance(legacy_telemetry.output_json, dict)
            session.add(
                _stage_result(
                    legacy_telemetry,
                    dict(legacy_telemetry.output_json),
                    now,
                    f"{label}-telemetry-result",
                )
            )
    if cross_parse is not None:
        assert isinstance(cross_parse.output_json, dict)
        session.add(
            _stage_result(cross_parse, dict(cross_parse.output_json), now, f"{label}-cross-parse-result")
        )
    if legacy_llm is not None:
        assert isinstance(legacy_llm.output_json, dict)
        session.add(JobDependency(job_id=legacy_llm.id, depends_on_job_id=legacy_assess.id, created_at=now))
        session.add(_stage_result(legacy_llm, dict(legacy_llm.output_json), now, f"{label}-llm-result"))
    return legacy_report


def test_fixed_query_verifies_assets_and_aggregates_replay_and_players_without_writes(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    before = _row_counts(report_database.session_factory)  # type: ignore[arg-type]

    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )

    assert graph.selected_report_public_id == published_graph.player_report_id
    assert graph.replay_wide.document.report_public_id == published_graph.replay_wide_report_id
    assert [item.document.replay_player_public_id for item in graph.player_reports] == [
        published_graph.player_public_id
    ]
    assert graph.selected.document == graph.player_reports[0].document
    assert graph.output_schema_version == "report-output-v1"
    assert graph.replay_wide.html.startswith("<!doctype html>")
    assert graph.replay_wide.text.startswith("Replay analysis report\n")
    assert graph.replay_wide.structured_asset.kind == "report_structured_json"
    assert graph.replay_wide.presentation_asset.kind == "report_presentation_bundle"
    assert _row_counts(report_database.session_factory) == before  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        graph.selected_report_public_id = published_graph.replay_wide_report_id  # type: ignore[misc]
    assert str(report_database.settings.data_root) not in repr(graph)


def test_latest_resolution_selects_exact_replay_and_player_reports_without_writes(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    before = _row_counts(report_database.session_factory)  # type: ignore[arg-type]

    replay_wide = published_graph.service.resolve_latest(
        LatestReportQuery(published_graph.replay_public_id)
    )
    player = published_graph.service.resolve_latest(
        LatestReportQuery(
            published_graph.replay_public_id,
            published_graph.player_public_id,
        )
    )

    assert replay_wide.report_public_id == published_graph.replay_wide_report_id
    assert replay_wide.replay_player_public_id is None
    assert player.report_public_id == published_graph.player_report_id
    assert player.replay_player_public_id == published_graph.player_public_id
    assert replay_wide.report_version == player.report_version == "replay-report-v1"
    assert _row_counts(report_database.session_factory) == before  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="canonical lowercase UUID"):
        LatestReportQuery(published_graph.replay_public_id.upper())
    with pytest.raises(ReportGraphNotFoundError):
        published_graph.service.resolve_latest(
            LatestReportQuery(published_graph.replay_public_id, stable_uuid("unknown-report-player"))
        )


def test_fixed_query_validates_each_immutable_document_once_before_trusted_rendering(
    published_graph: PublishedGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    original_validate = report_query.validate_document
    original_json = report_query._render_json_validated
    original_html = report_query._render_html_validated
    original_text = report_query._render_text_validated

    def count_validation(document: ReportDocument) -> None:
        calls.append(("validate", document.report_public_id))
        original_validate(document)

    def count_json(document: ReportDocument) -> bytes:
        calls.append(("json", document.report_public_id))
        return original_json(document)

    def count_html(document: ReportDocument) -> str:
        calls.append(("html", document.report_public_id))
        return original_html(document)

    def count_text(document: ReportDocument) -> str:
        calls.append(("text", document.report_public_id))
        return original_text(document)

    monkeypatch.setattr(report_query, "validate_document", count_validation)
    monkeypatch.setattr(report_query, "_render_json_validated", count_json)
    monkeypatch.setattr(report_query, "_render_html_validated", count_html)
    monkeypatch.setattr(report_query, "_render_text_validated", count_text)

    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.replay_wide_report_id)
    )

    expected_ids = {
        item.document.report_public_id for item in (graph.replay_wide, *graph.player_reports)
    }
    assert len(calls) == len(expected_ids) * 4
    for report_public_id in expected_ids:
        assert [stage for stage, value in calls if value == report_public_id] == [
            "validate",
            "json",
            "html",
            "text",
        ]


def test_timeline_query_is_frame_canonical_sorted_and_report_scoped(
    published_graph: PublishedGraph,
) -> None:
    first = published_graph.service.timeline_chart(
        TimelineChartQuery(
            published_graph.replay_public_id,
            published_graph.player_report_id,
            (published_graph.player_public_id, published_graph.player_public_id),
            ("strategy", "build_order", "strategy"),
        )
    )
    second = published_graph.service.timeline_chart(
        TimelineChartQuery(
            published_graph.replay_public_id,
            published_graph.player_report_id,
            (published_graph.player_public_id,),
            ("build_order", "strategy"),
        )
    )

    assert first == second
    assert first.schema_version == "replay-report-timeline-v1"
    assert first.frames_per_second is None
    assert first.seconds_display_policy_version == "frame-only-authority-unavailable-v2"
    assert first.replay_public_id == published_graph.replay_public_id
    assert first.report_public_id == published_graph.player_report_id
    assert first.selected_player_public_ids == (published_graph.player_public_id,)
    assert first.selected_families == ("build_order", "strategy")
    assert tuple((series.family, series.player_public_id or "", series.series_id) for series in first.series) == tuple(
        sorted((series.family, series.player_public_id or "", series.series_id) for series in first.series)
    )
    assert any(series.family == "build_order" for series in first.series)
    assert all(point.frame >= 0 and not hasattr(point, "second") for series in first.series for point in series.points)
    with pytest.raises(ReportGraphNotFoundError):
        published_graph.service.timeline_chart(
            TimelineChartQuery(
                stable_uuid("cross-timeline-replay"),
                published_graph.player_report_id,
            )
        )


def test_timeline_uses_the_authoritative_60_hz_replay_header_timebase(
    published_graph: PublishedGraph,
    report_database: SeededReportDatabase,
) -> None:
    with report_database.session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == published_graph.replay_public_id))
        assert replay is not None
        replay.header_json = {
            **replay.header_json,
            "timebase": {"logic_frames_per_second": 60, "source": "engine_manifest"},
        }

    chart = published_graph.service.timeline_chart(
        TimelineChartQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )

    assert chart.frames_per_second == 60
    assert chart.seconds_display_policy_version == "frame-div-authoritative-logic-fps-v2"


def test_latest_and_timeline_queries_reject_wrong_types_ambiguity_and_unknown_filters(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    with pytest.raises(TypeError, match="LatestReportQuery"):
        published_graph.service.resolve_latest(  # type: ignore[arg-type]
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )
    with pytest.raises(TypeError, match="TimelineChartQuery"):
        published_graph.service.timeline_chart(  # type: ignore[arg-type]
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )
    with pytest.raises(ReportGraphNotFoundError):
        published_graph.service.resolve_latest(LatestReportQuery(stable_uuid("unknown-latest-replay")))
    with pytest.raises(ReportGraphContractError, match="outside the report graph"):
        published_graph.service.timeline_chart(
            TimelineChartQuery(
                published_graph.replay_public_id,
                published_graph.player_report_id,
                (stable_uuid("unknown-timeline-player"),),
            )
        )
    with pytest.raises(TypeError, match="immutable tuple"):
        TimelineChartQuery(  # type: ignore[arg-type]
            published_graph.replay_public_id,
            published_graph.player_report_id,
            [],
        )
    with pytest.raises(TypeError, match="immutable tuple"):
        TimelineChartQuery(  # type: ignore[arg-type]
            published_graph.replay_public_id,
            published_graph.player_report_id,
            families=[],
        )
    with pytest.raises(ValueError, match="unsupported"):
        TimelineChartQuery(
            published_graph.replay_public_id,
            published_graph.player_report_id,
            families=("terrain",),  # type: ignore[arg-type]
        )

    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        original = session.scalar(
            select(Report).where(Report.public_id == published_graph.player_report_id)
        )
        assert original is not None
        session.add(
            Report(
                public_id=stable_uuid("ambiguous-latest-player-report"),
                replay_id=original.replay_id,
                replay_player_id=original.replay_player_id,
                analysis_run_id=original.analysis_run_id,
                report_version=original.report_version,
                input_digest="0" * 64,
                cache_key="1" * 64,
                report_json=original.report_json,
                structured_asset_id=original.structured_asset_id,
                rendered_asset_id=original.rendered_asset_id,
                created_at=original.created_at,
            )
        )
    with pytest.raises(ReportGraphAmbiguousError, match="latest"):
        published_graph.service.resolve_latest(
            LatestReportQuery(published_graph.replay_public_id, published_graph.player_public_id)
        )


def test_timeline_read_model_closes_scalar_geometry_and_availability_contracts(
    published_graph: PublishedGraph,
) -> None:
    chart = published_graph.service.timeline_chart(
        TimelineChartQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )
    marker = next(item for item in chart.series if item.kind == "marker")
    point = marker.points[0]
    evidence = TimelineEvidenceDTO(published_graph.parser_evidence_id, "observed")
    interval = TimelineIntervalDTO(point.frame, point.frame + 1, "phase", (evidence,))

    assert published_graph.service._timeline_scalar(0) == 0
    assert published_graph.service._timeline_scalar({"not": "scalar"}) is None  # type: ignore[arg-type]
    assert published_graph.service._timeline_family("availability", "x") == "quality"
    assert published_graph.service._timeline_family("features", "cash sample") == "economy"
    assert published_graph.service._timeline_family("features", "unit production") == "production"
    assert published_graph.service._timeline_family("features", "damage event") == "combat"
    assert published_graph.service._timeline_family("features", "phase timing") == "strategy"
    assert published_graph.service._timeline_family("features", "cursor motion") == "activity"

    invalid_values = (
        lambda: TimelinePointDTO(-1, 1, None, ()),
        lambda: TimelinePointDTO(1, -0.0, None, ()),
        lambda: TimelinePointDTO(1, True, None, ()),
        lambda: TimelineIntervalDTO(2, 1, "bad", ()),
        lambda: TimelineSeriesDTO(
            marker.series_id,
            "marker",
            marker.family,
            marker.player_public_id,
            marker.label,
            marker.unit,
            "partial",
            None,
            marker.points,
            (),
        ),
        lambda: replace(marker, availability="available", unavailable_reason="unexpected"),
        lambda: replace(marker, points=(point, point)),
        lambda: replace(marker, kind="band"),
        lambda: replace(marker, intervals=(interval,)),
        lambda: TimelineSeriesDTO(
            "zero-width-band",
            "band",
            "strategy",
            marker.player_public_id,
            "Zero width",
            None,
            "available",
            None,
            (),
            (TimelineIntervalDTO(point.frame, point.frame, "zero", ()),),
        ),
        lambda: replace(marker, availability="unavailable", unavailable_reason="no_evidence"),
        lambda: TimelineOptionDTO(published_graph.player_public_id.upper(), "Player"),
        lambda: TimelineFamilyOptionDTO("terrain", "Terrain"),  # type: ignore[arg-type]
        lambda: TimelineEvidenceDTO(published_graph.parser_evidence_id, "manual"),  # type: ignore[arg-type]
        lambda: replace(chart, schema_version="future"),
        lambda: replace(chart, selected_player_public_ids=(stable_uuid("outside-chart-player"),)),
        lambda: replace(chart, selected_families=("terrain",)),  # type: ignore[arg-type]
        lambda: replace(chart, available_players=(chart.available_players[0], chart.available_players[0])),
        lambda: replace(chart, series=(marker, marker)),
        lambda: replace(chart, availability="partial", unavailable_reason=None),
        lambda: replace(chart, availability="unavailable", unavailable_reason="no_evidence"),
    )
    for invalid in invalid_values:
        with pytest.raises((TypeError, ValueError)):
            invalid()
    with pytest.raises(FrozenInstanceError):
        chart.frames_per_second = 60  # type: ignore[misc]
    assert evidence.public_id == published_graph.parser_evidence_id
    assert isinstance(chart, TimelineChartDTO)


def test_timeline_uses_only_fixed_report_evidence_and_preserves_exact_markers(
    published_graph: PublishedGraph,
) -> None:
    chart = published_graph.service.timeline_chart(
        TimelineChartQuery(published_graph.replay_public_id, published_graph.replay_wide_report_id)
    )

    assert chart.report_public_id == published_graph.replay_wide_report_id
    assert chart.selected_player_public_ids == ()
    assert chart.available_players == ()
    assert all(item.player_public_id is None for item in chart.series)
    assert all(item.kind != "step" for item in chart.series if len(item.points) == 1)
    for series in chart.series:
        for item in (*series.points, *series.intervals):
            for cited in item.evidence:
                detail = published_graph.service.get_evidence(
                    EvidenceQuery(chart.report_public_id, cited.public_id, cited.tier)
                )
                assert detail.evidence_public_id == cited.public_id


def test_published_sqlite_timestamp_is_normalized_to_aware_utc(
    published_graph: PublishedGraph,
) -> None:
    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )

    assert graph.selected.created_at_utc.tzinfo is UTC
    assert graph.selected.created_at_utc.utcoffset() is not None


def test_fixed_query_accepts_structurally_identical_successful_stage_graph_claimants(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    with pytest.raises(ReportGraphNotFoundError):
        published_graph.service.get_report(
            FixedReportQuery(stable_uuid("wrong-query-replay"), published_graph.player_report_id)
        )

    factory = report_database.session_factory  # type: ignore[assignment]
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    with factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == "render_report", Job.status == "succeeded"))
        assert original is not None and isinstance(original.output_json, dict)
        _add_legacy_report_claimant(session, original, now, label="query-equivalent-legacy")

    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )

    assert graph.selected.document.report_public_id == published_graph.player_report_id


@pytest.mark.parametrize(
    ("telemetry_variant", "expected_run_id"),
    (
        ("telemetry_success", "run"),
        ("telemetry_failed", "run"),
        ("telemetry_dependency_failed", None),
    ),
)
def test_legacy_observation_authenticates_exact_production_telemetry_success_and_terminal_failure(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    telemetry_variant: str,
    expected_run_id: str | None,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    label = f"query-production-{telemetry_variant}"
    with factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == RENDER_REPORT, Job.status == "succeeded"))
        assert original is not None and isinstance(original.output_json, dict)
        _add_legacy_report_claimant(
            session,
            original,
            datetime(2026, 8, 23, 10, 5, tzinfo=UTC),
            label=label,
            poison=telemetry_variant,
        )
        replay = session.get(Replay, original.replay_id)
        observation = session.scalar(
            select(Job).where(Job.public_id == stable_uuid(f"{label}-observation"))
        )
        assert replay is not None and observation is not None
        _branch, telemetry_run_id = published_graph.service._validate_observation_authority(
            session, replay, observation
        )

    assert telemetry_run_id == (
        stable_uuid(f"{label}-telemetry-run") if expected_run_id == "run" else None
    )


def test_report_identity_excludes_closed_slots_outside_the_exact_parser_subjects(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    with report_database.session_factory.begin() as session:  # type: ignore[union-attr]
        session.execute(text("DROP TRIGGER trg_replay_players_succeeded_no_insert"))
        replay = session.scalar(select(Replay).where(Replay.public_id == published_graph.replay_public_id))
        assert replay is not None
        parser = session.scalar(select(ParserRun).where(ParserRun.replay_id == replay.id))
        assert parser is not None
        session.add(
            ReplayPlayer(
                public_id=stable_uuid("query-closed-slot"),
                replay_id=replay.id,
                parser_run_id=parser.id,
                slot_index=7,
                slot_kind="closed",
                original_name="Fabricated Closed Slot",
                normalized_name="fabricated closed slot",
                observed_json={},
            )
        )

    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )

    assert {player.public_id for player in graph.identity.players} == {
        report_database.replay_player_public_id
    }
    assert "Fabricated Closed Slot" not in graph.identity.label


def test_modern_report_rejects_cited_evidence_owned_by_a_different_parser_branch(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    now = datetime(2026, 8, 23, 10, 15, tzinfo=UTC)
    with report_database.session_factory.begin() as session:  # type: ignore[union-attr]
        session.execute(text("DROP TRIGGER trg_evidence_items_observed_no_update"))
        replay = session.scalar(select(Replay).where(Replay.public_id == published_graph.replay_public_id))
        evidence = session.scalar(
            select(EvidenceItem).where(EvidenceItem.public_id == published_graph.parser_evidence_id)
        )
        assert replay is not None and evidence is not None
        other_parser = ParserRun(
            run_id=stable_uuid("query-cross-branch-parser"),
            replay_id=replay.id,
            parser_version="query-cross-branch-v1",
            schema_version=1,
            input_sha256=replay.sha256,
            result_sha256="8" * 64,
            status="succeeded",
            completion_status="complete",
            warnings_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(other_parser)
        session.flush()
        evidence.parser_run_id = other_parser.id

    with pytest.raises(ReportGraphContractError, match="evidence.*parser|parser.*evidence"):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


@pytest.mark.parametrize(
    "poison",
    ("extra_key", "parser_command_count", "telemetry_event_count"),
)
def test_modern_report_rejects_nonproduction_observation_outputs(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    poison: str,
) -> None:
    with report_database.session_factory.begin() as session:  # type: ignore[union-attr]
        session.execute(text("DROP TRIGGER trg_job_stage_results_no_update"))
        observation = session.scalar(
            select(Job).where(Job.stage == IMPORT_OBSERVATIONS, Job.status == "succeeded")
        )
        assert observation is not None and isinstance(observation.output_json, dict)
        result = session.scalar(select(JobStageResult).where(JobStageResult.job_id == observation.id))
        assert result is not None
        output = dict(observation.output_json)
        if poison == "extra_key":
            output["unexpected"] = "poison"
        else:
            output[poison] = cast(int, output[poison]) + 1
        observation.output_json = output
        result.output_json = output

    with pytest.raises(ReportGraphContractError, match="observation.*(schema|identity|count)"):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


@pytest.mark.parametrize("child_kind", ("telemetry_event", "entity"))
def test_legacy_failed_telemetry_rejects_materialized_event_or_typed_child(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    child_kind: str,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    label = f"query-failed-child-{child_kind}"
    now = datetime(2026, 8, 23, 10, 18, tzinfo=UTC)
    with factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == RENDER_REPORT, Job.status == "succeeded"))
        assert original is not None and isinstance(original.output_json, dict)
        _add_legacy_report_claimant(
            session,
            original,
            now,
            label=label,
            poison="telemetry_failed",
        )
        replay = session.get(Replay, original.replay_id)
        failed_run = session.scalar(
            select(TelemetryRun).where(TelemetryRun.run_id == stable_uuid(f"{label}-telemetry-run"))
        )
        parser_evidence = session.scalar(
            select(EvidenceItem).where(EvidenceItem.public_id == published_graph.parser_evidence_id)
        )
        assert replay is not None and failed_run is not None and parser_evidence is not None
        if child_kind == "telemetry_event":
            session.add(
                TelemetryEvent(
                    telemetry_run_id=failed_run.id,
                    sequence=0,
                    frame=0,
                    logic_time_seconds=0.0,
                    schema_version=1,
                    event_type="poison",
                    payload_json={},
                    raw_record_json={},
                    evidence_item_id=parser_evidence.id,
                )
            )
        else:
            session.add(
                Entity(
                    public_id=stable_uuid(f"{label}-entity"),
                    telemetry_run_id=failed_run.id,
                    replay_id=replay.id,
                    object_id=1,
                    template_name="PoisonEntity",
                    initial_owner_player_index=None,
                    initial_team_id=None,
                    kind_of_flags_json=[],
                    creation_sequence=None,
                    creation_frame=None,
                    destruction_sequence=None,
                    destruction_frame=None,
                    observed_json={},
                )
            )

    with pytest.raises(ReportGraphContractError, match="failed telemetry.*materialized"):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


def test_modern_evidence_authority_query_count_is_independent_of_evidence_count_on_the_exact_run(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )
    factory = report_database.session_factory  # type: ignore[assignment]
    now = datetime(2026, 8, 23, 10, 20, tzinfo=UTC)
    with factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == published_graph.replay_public_id))
        parser = session.scalar(select(ParserRun).where(ParserRun.replay_id == replay.id))  # type: ignore[union-attr]
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.replay_id == replay.id))  # type: ignore[union-attr]
        assert replay is not None and parser is not None and telemetry is not None
        selected_parser_run_id = parser.run_id
        selected_telemetry_run_id = telemetry.run_id
        evidence_ids: list[str] = []
        for index in range(8):
            public_id = stable_uuid(f"query-batch-evidence-{index}")
            session.add(
                EvidenceItem(
                    public_id=public_id,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    telemetry_run_id=telemetry.id,
                    tier="derived",
                    source_kind="feature",
                    source_key=f"query-batch-feature:{index}",
                    schema_version=1,
                    created_at=now,
                )
            )
            evidence_ids.append(public_id)

    anchor = graph.selected.document.derived[0]

    def with_evidence(count: int) -> ReportDocument:
        values = tuple(
            replace(
                anchor,
                claim_id=f"derived:query-batch:{index}",
                evidence=(ReportEvidenceRef(public_id, "derived"),),
            )
            for index, public_id in enumerate(evidence_ids[:count])
        )
        return replace(
            graph.selected.document,
            evidence_availability=(),
            observed=(),
            derived=values,
            inferred=(),
        )

    engine = factory.kw["bind"]

    def select_count(document: ReportDocument) -> int:
        selected: list[str] = []

        def count_selects(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _many: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                selected.append(statement)

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            with factory() as session:
                replay = session.scalar(
                    select(Replay).where(Replay.public_id == published_graph.replay_public_id)
                )
                assert replay is not None
                published_graph.service._validate_evidence_index(
                    session,
                    replay,
                    document,
                    selected_parser_run_id,
                    selected_telemetry_run_id,
                )
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)
        return len(selected) - 1  # Exclude the replay lookup outside the authority validator.

    one_count = select_count(with_evidence(1))
    many_count = select_count(with_evidence(8))

    assert one_count <= 3
    assert many_count == one_count


def test_modern_report_rejects_same_parser_evidence_from_a_different_telemetry_attempt(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    now = datetime(2026, 8, 23, 10, 25, tzinfo=UTC)
    with report_database.session_factory.begin() as session:  # type: ignore[union-attr]
        session.execute(text("DROP TRIGGER trg_evidence_items_observed_no_update"))
        replay = session.scalar(select(Replay).where(Replay.public_id == published_graph.replay_public_id))
        parser = session.scalar(select(ParserRun).where(ParserRun.replay_id == replay.id))  # type: ignore[union-attr]
        evidence = session.scalar(
            select(EvidenceItem).where(EvidenceItem.public_id == published_graph.telemetry_evidence_id)
        )
        assert replay is not None and parser is not None and evidence is not None
        other_telemetry = TelemetryRun(
            run_id=stable_uuid("query-same-parser-other-telemetry"),
            replay_id=replay.id,
            schema_version=2,
            engine_build="query-other-engine-v1",
            engine_executable_sha256="b" * 64,
            settings_json={"parser_run_id": parser.run_id},
            status="succeeded",
            runner_status="success",
            strategy_analysis_scope="full",
            process_exit_code=0,
            final_frame=1200,
            command_count=0,
            trace_sha256=None,
            diagnostics_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(other_telemetry)
        session.flush()
        evidence.telemetry_run_id = other_telemetry.id

    with pytest.raises(ReportGraphContractError, match="telemetry.*authority|authority.*telemetry"):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


def test_fixed_query_rejects_a_different_fully_validated_asset_claim(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == RENDER_REPORT, Job.status == "succeeded"))
        assert original is not None and isinstance(original.output_json, dict)
        _add_legacy_report_claimant(
            session,
            original,
            datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
            label="query-divergent-legacy",
            poison="divergent_assets",
        )

    with pytest.raises(ReportGraphAmbiguousError):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


@pytest.mark.parametrize(
    "poison",
    (
        "invalid_result",
        "permissive_recipe",
        "cross_branch",
        "parse_run_mismatch",
        "telemetry_run_mismatch",
        "telemetry_retryable",
        "telemetry_nonterminal",
        "telemetry_failure_mismatch",
        "telemetry_dependency_failed_claims_run",
    ),
)
def test_fixed_query_does_not_mask_an_invalid_successful_claimant(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    poison: str,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        original = session.scalar(select(Job).where(Job.stage == RENDER_REPORT, Job.status == "succeeded"))
        assert original is not None and isinstance(original.output_json, dict)
        _add_legacy_report_claimant(
            session,
            original,
            datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
            label=f"query-invalid-{poison}",
            poison=poison,
        )

    with pytest.raises(ReportGraphContractError):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "cache_key",
        "asset_kind",
        "cas_bytes",
        "stage_result",
        "stage_version",
        "document_schema",
        "document_noncanonical",
        "document_path",
    ],
)
def test_fixed_query_fails_closed_on_report_asset_cas_or_stage_drift(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    tamper: str,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        report = session.scalar(select(Report).where(Report.public_id == published_graph.player_report_id))
        assert report is not None and report.structured_asset_id is not None
        asset = session.get(ManagedAsset, report.structured_asset_id)
        assert asset is not None
        if tamper == "cache_key":
            report.cache_key = "0" * 64
        elif tamper == "asset_kind":
            asset.kind = "wrong_kind"
        elif tamper == "stage_result":
            job = session.scalar(select(Job).where(Job.stage == "render_report"))
            assert job is not None and isinstance(job.output_json, dict)
            job.output_json = {**job.output_json, "schema_version": "poisoned-output-v1"}
        elif tamper == "stage_version":
            job = session.scalar(select(Job).where(Job.stage == "render_report"))
            assert job is not None
            job.component_version = "wrong"
        elif tamper == "document_schema":
            report.report_json = {**report.report_json, "lifecycle": {}}
        elif tamper == "document_noncanonical":
            report.report_json = {**report.report_json, "warnings": ["z-last", "a-first"]}
        elif tamper == "document_path":
            report.report_json = {
                **report.report_json,
                "lifecycle": {
                    **report.report_json["lifecycle"],  # type: ignore[dict-item]
                    "lifecycle_state": str(report_database.settings.data_root),
                },
            }
        else:
            path = report_database.settings.data_root / asset.relative_path
            path.write_bytes(b"corrupt")

    with pytest.raises(ReportGraphContractError):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


def test_fixed_query_rejects_cross_run_stage_entry(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        job = session.scalar(select(Job).where(Job.stage == "render_report"))
        assert job is not None and isinstance(job.output_json, dict)
        poisoned = dict(job.output_json)
        reports = [dict(item) for item in poisoned["reports"]]  # type: ignore[index]
        reports[1]["analysis_run_id"] = stable_uuid("cross-run-poison")
        poisoned["reports"] = reports
        job.output_json = poisoned

    with pytest.raises(ReportGraphContractError, match="analysis|stage|graph"):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "selected_dependency_digest",
        "identity_scope",
        "report_parser_version",
        "derive_parser_version",
        "derive_input",
        "dependency",
    ],
)
def test_fixed_query_rejects_stage_identity_digest_or_dependency_drift(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    tamper: str,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        report = session.scalar(select(Job).where(Job.stage == RENDER_REPORT))
        derive = session.scalar(select(Job).where(Job.stage == DERIVE_FEATURES))
        assess = session.scalar(select(Job).where(Job.stage == ASSESS_STRATEGIES))
        observation = session.scalar(select(Job).where(Job.stage == IMPORT_OBSERVATIONS))
        assert report is not None and derive is not None and assess is not None and observation is not None
        if tamper == "selected_dependency_digest":
            report.input_json = {**report.input_json, "selected_dependency_digest": "b" * 64}
        elif tamper == "identity_scope":
            scope = dict(report.input_json["identity_scope"])  # type: ignore[arg-type]
            scope["poison"] = True
            report.input_json = {**report.input_json, "identity_scope": scope}
        elif tamper == "report_parser_version":
            report.input_json = {**report.input_json, "parser_version": "wrong-parser-version"}
        elif tamper == "derive_parser_version":
            derive.input_json = {**derive.input_json, "parser_version": "wrong-parser-version"}
        elif tamper == "derive_input":
            derive.input_json = {**derive.input_json, "selected_dependency_digest": "b" * 64}
        else:
            dependency = session.scalar(select(JobDependency).where(JobDependency.job_id == assess.id))
            assert dependency is not None
            dependency.depends_on_job_id = observation.id

    with pytest.raises(ReportGraphContractError, match="identity|digest|dependency|derive|parser"):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
        )


def test_evidence_query_projects_report_scoped_observed_derived_and_inferred_details(
    published_graph: PublishedGraph,
    tmp_path: Path,
) -> None:
    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )
    command = published_graph.service.get_evidence(
        EvidenceQuery(
            published_graph.player_report_id,
            published_graph.parser_evidence_id,
            "observed",
        )
    )
    telemetry = published_graph.service.get_evidence(
        EvidenceQuery(
            published_graph.player_report_id,
            published_graph.telemetry_evidence_id,
            "observed",
        )
    )
    feature = published_graph.service.get_evidence(
        EvidenceQuery(
            published_graph.player_report_id,
            published_graph.feature_evidence_id,
            "derived",
        )
    )
    rule = published_graph.service.get_evidence(
        EvidenceQuery(
            published_graph.player_report_id,
            published_graph.rule_evidence_id,
            "derived",
        )
    )
    longitudinal = published_graph.service.get_evidence(
        EvidenceQuery(
            published_graph.player_report_id,
            published_graph.longitudinal_evidence_id,
            "derived",
        )
    )
    inferred = published_graph.service.get_evidence(
        EvidenceQuery(
            published_graph.player_report_id,
            published_graph.inferred_evidence_id,
            "inferred",
        )
    )

    assert isinstance(command.source, ObservedCommandEvidenceDTO)
    assert command.source.start_offset == 10
    assert isinstance(telemetry.source, ObservedTelemetryEvidenceDTO)
    assert telemetry.source.sequence == 0
    assert isinstance(feature.source, DerivedFeatureEvidenceDTO)
    assert feature.source.raw_value == 1234.56789
    assert [(item.role, item.tier) for item in feature.source.inputs] == [("input", "observed")]
    assert isinstance(rule.source, DerivedAssessmentEvidenceDTO)
    assert rule.source.strategy_label == "oil_grab"
    assert [(item.role, item.tier) for item in rule.source.citations] == [("supporting", "observed")]
    assert isinstance(longitudinal.source, DerivedLongitudinalEvidenceDTO)
    assert longitudinal.source.sample_count == 1
    assert [(item.role, item.tier) for item in longitudinal.source.members] == [("input", "derived")]
    assert "source_key" not in repr(longitudinal.source.statistics)
    assert isinstance(inferred.source, InferredAssessmentEvidenceDTO)
    assert inferred.source.analysis_run_id == stable_uuid("analysis-run")
    assert [(item.role, item.public_id) for item in inferred.source.citations] == [
        ("supporting", published_graph.feature_evidence_id)
    ]

    invalid_cases = (
        lambda: replace(graph.replay_wide.structured_asset, public_id=1),
        lambda: replace(
            graph.replay_wide.structured_asset,
            public_id=graph.replay_wide.structured_asset.public_id.upper(),
        ),
        lambda: replace(graph.replay_wide.structured_asset, sha256="bad"),
        lambda: replace(graph.replay_wide.structured_asset, kind="wrong"),
        lambda: replace(graph.replay_wide.structured_asset, media_type="text/plain"),
        lambda: replace(graph.replay_wide.structured_asset, size_bytes=-1),
        lambda: replace(graph.replay_wide, document=None),
        lambda: replace(
            graph.replay_wide,
            structured_asset=graph.replay_wide.presentation_asset,
        ),
        lambda: replace(graph.replay_wide, html=""),
        lambda: replace(graph, schema_version="wrong"),
        lambda: replace(graph, output_schema_version="wrong"),
        lambda: replace(graph, replay_wide=graph.player_reports[0]),
        lambda: replace(graph, player_reports=[]),
        lambda: replace(graph, player_reports=(graph.replay_wide,)),
        lambda: replace(graph, player_reports=(graph.player_reports[0],) * 2),
        lambda: replace(graph, selected_report_public_id=stable_uuid("not-a-member")),
        lambda: replace(command.source, kind="wrong"),
        lambda: replace(command.source, start_offset=-1),
        lambda: replace(command.source, parser_version=""),
        lambda: replace(command.source, parser_version=str(tmp_path)),
        lambda: replace(telemetry.source, kind="wrong"),
        lambda: replace(telemetry.source, sequence=-1),
        lambda: replace(feature.source, kind="wrong"),
        lambda: replace(feature.source, frame_start=-1),
        lambda: replace(feature.source, availability="wrong"),
        lambda: replace(
            feature.source,
            availability="available",
            unavailable_reason="wrong",
        ),
        lambda: replace(
            feature.source,
            raw_value=None,
            availability="available",
            unavailable_reason=None,
        ),
        lambda: replace(
            feature.source,
            raw_value=None,
            availability="unavailable",
            unavailable_reason=None,
        ),
        lambda: replace(feature.source, inputs=list(feature.source.inputs)),
        lambda: replace(feature.source, inputs=feature.source.inputs * 2),
        lambda: replace(rule.source, kind="wrong"),
        lambda: replace(rule.source, frame_start=-1),
        lambda: replace(rule.source, score=1),
        lambda: replace(rule.source, score=-0.0),
        lambda: replace(longitudinal.source, kind="wrong"),
        lambda: replace(longitudinal.source, missing_count=-1),
        lambda: replace(
            longitudinal.source,
            availability="unavailable",
            unavailable_reason=None,
        ),
        lambda: replace(
            longitudinal.source,
            availability="available",
            unavailable_reason=str(tmp_path),
        ),
        lambda: replace(inferred.source, kind="wrong"),
        lambda: replace(inferred.source, model_digest="bad"),
        lambda: replace(inferred.source, confidence=2.0),
        lambda: replace(inferred.source, confidence=-0.0),
        lambda: replace(command, schema_version="wrong"),
        lambda: replace(command, tier="wrong"),
        lambda: replace(command, source_kind=""),
        lambda: replace(command, source_schema_version=-1),
        lambda: replace(command, source=tmp_path),
        lambda: replace(command, source_kind="telemetry_event"),
        lambda: replace(command, source_schema_version=command.source.parser_schema_version + 1),
    )
    for index, invalid in enumerate(invalid_cases):
        try:
            invalid()
        except (TypeError, ValueError):
            continue
        pytest.fail(f"invalid read-model case {index} was accepted")


def test_evidence_query_rejects_tier_mismatch_and_cross_report_reference(
    published_graph: PublishedGraph,
) -> None:
    with pytest.raises(ReportGraphContractError, match="tier"):
        published_graph.service.get_evidence(
            EvidenceQuery(
                published_graph.player_report_id,
                published_graph.feature_evidence_id,
                "observed",
            )
        )
    with pytest.raises(ReportGraphNotFoundError):
        published_graph.service.get_evidence(
            EvidenceQuery(
                published_graph.replay_wide_report_id,
                published_graph.inferred_evidence_id,
                "inferred",
            )
        )


def test_evidence_query_rejects_source_version_and_citation_role_poison(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        rule_evidence = session.scalar(
            select(EvidenceItem).where(EvidenceItem.public_id == published_graph.rule_evidence_id)
        )
        assert rule_evidence is not None
        rule_evidence.schema_version = 99

    with pytest.raises(ReportGraphContractError, match="source|version|schema"):
        published_graph.service.get_evidence(
            EvidenceQuery(
                published_graph.player_report_id,
                published_graph.rule_evidence_id,
                "derived",
            )
        )


def test_evidence_query_rejects_cross_replay_citation_poison(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    now = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)
    with factory.begin() as session:
        current = session.scalar(select(Replay).where(Replay.public_id == published_graph.replay_public_id))
        assessment = session.scalar(
            select(AssessmentEvidence.assessment_id)
            .join(EvidenceItem, AssessmentEvidence.evidence_item_id == EvidenceItem.id)
            .where(EvidenceItem.public_id == published_graph.telemetry_evidence_id)
        )
        assert current is not None and assessment is not None
        foreign = Replay(
            public_id=stable_uuid("query-foreign-replay"),
            sha256="f" * 64,
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
        poison = EvidenceItem(
            public_id=stable_uuid("query-foreign-evidence"),
            replay_id=foreign.id,
            tier="observed",
            source_kind="parser_command",
            source_key="foreign:poison",
            schema_version=1,
            created_at=now,
        )
        session.add(poison)
        session.flush()
        session.add(
            AssessmentEvidence(
                assessment_id=assessment,
                evidence_item_id=poison.id,
                role="contradicting",
            )
        )

    with pytest.raises(ReportGraphContractError, match="cross-replay|citation|report"):
        published_graph.service.get_evidence(
            EvidenceQuery(
                published_graph.player_report_id,
                published_graph.rule_evidence_id,
                "derived",
            )
        )


@pytest.mark.parametrize(
    ("evidence_attribute", "source_method"),
    [
        ("parser_evidence_id", "_command_source"),
        ("telemetry_evidence_id", "_telemetry_source"),
    ],
)
def test_observed_evidence_recomputes_immutable_locator_identity(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    evidence_attribute: str,
    source_method: str,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory() as session:
        evidence = session.scalar(
            select(EvidenceItem).where(
                EvidenceItem.public_id == getattr(published_graph, evidence_attribute)
            )
        )
        report = session.scalar(
            select(Report).where(Report.public_id == published_graph.player_report_id)
        )
        assert evidence is not None and report is not None
        document = published_graph.service._document(report.report_json)
        evidence.source_key = "observed-evidence:forged:v1:{}"
        with session.no_autoflush, pytest.raises(
            ReportGraphContractError,
            match="observed evidence identity",
        ):
            getattr(published_graph.service, source_method)(session, evidence, document)


@pytest.mark.parametrize("tamper", ["source_key", "source_value"])
def test_evidence_query_rejects_derived_source_identity_or_value_drift(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    tamper: str,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        evidence = session.scalar(
            select(EvidenceItem).where(EvidenceItem.public_id == published_graph.rule_evidence_id)
        )
        assessment = session.scalar(
            select(StrategyAssessment).where(StrategyAssessment.evidence_item_id == evidence.id)
        )
        assert evidence is not None and assessment is not None
        if tamper == "source_key":
            evidence.source_key = f"{evidence.source_key}:poison"
        else:
            assessment.confidence = 0.125

    with pytest.raises(ReportGraphContractError, match="source|identity|document|claim"):
        published_graph.service.get_evidence(
            EvidenceQuery(
                published_graph.player_report_id,
                published_graph.rule_evidence_id,
                "derived",
            )
        )


def test_evidence_query_rejects_longitudinal_member_snapshot_drift(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
) -> None:
    factory = report_database.session_factory  # type: ignore[assignment]
    with factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == published_graph.replay_public_id))
        assert replay is not None
        replay.version_string = "1.04-poison"

    with pytest.raises(ReportGraphContractError, match="longitudinal|snapshot|drift"):
        published_graph.service.get_evidence(
            EvidenceQuery(
                published_graph.player_report_id,
                published_graph.longitudinal_evidence_id,
                "derived",
            )
        )


def test_query_dtos_reject_noncanonical_identity_and_remain_path_free(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FixedReportQuery("NOT-A-UUID", stable_uuid("valid-report"))
    with pytest.raises(ValueError):
        EvidenceQuery(stable_uuid("valid-report"), stable_uuid("valid-evidence"), "wrong")  # type: ignore[arg-type]
    assert str(tmp_path) not in repr(FixedReportQuery(stable_uuid("valid-replay"), stable_uuid("valid-report")))


def test_query_contract_helpers_reject_unbounded_or_noncanonical_public_data(
    report_database: SeededReportDatabase,
    published_graph: PublishedGraph,
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        published_graph.service.get_report(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        published_graph.service.get_evidence(None)  # type: ignore[arg-type]
    with pytest.raises(ReportGraphNotFoundError):
        published_graph.service.get_evidence(
            EvidenceQuery(stable_uuid("missing-report"), stable_uuid("missing-evidence"), "observed")
        )
    with pytest.raises(ReportGraphNotFoundError):
        published_graph.service.get_report(
            FixedReportQuery(published_graph.replay_public_id, stable_uuid("missing-report"))
        )

    with pytest.raises(ReportGraphContractError, match="exact schema"):
        report_query._mapping({}, {"required"}, "test mapping")
    with pytest.raises(ReportGraphContractError, match="forbidden public value"):
        report_query._safe_value(tmp_path)
    nested: object = "leaf"
    for _index in range(13):
        nested = [nested]
    with pytest.raises(ReportGraphContractError, match="structural bound"):
        report_query._safe_value(nested)
    with pytest.raises(ReportGraphContractError, match="UTF-8 bound"):
        report_query._safe_value("x" * 65537)
    assert not report_query.ReportQueryService._mentions_assets(
        None,
        stable_uuid("structured"),
        stable_uuid("presentation"),
    )
    assert not report_query.ReportQueryService._mentions_assets(
        {"reports": None},
        stable_uuid("structured"),
        stable_uuid("presentation"),
    )

    graph = published_graph.service.get_report(
        FixedReportQuery(published_graph.replay_public_id, published_graph.player_report_id)
    )
    mapping = document_to_mapping(graph.selected.document)
    observed_value = dict(mapping["observed"][0])  # type: ignore[index]
    with pytest.raises(ReportGraphContractError, match="canonical array"):
        report_query.ReportQueryService._values(None)
    observed_value["evidence"] = None
    with pytest.raises(ReportGraphContractError, match="must be an array"):
        report_query.ReportQueryService._values([observed_value])
    observed_value = dict(mapping["observed"][0])  # type: ignore[index]
    observed_value["frame_window"] = [1]
    with pytest.raises(ReportGraphContractError, match="frame window"):
        report_query.ReportQueryService._values([observed_value])
    with pytest.raises(ReportGraphContractError, match="canonical array"):
        report_query.ReportQueryService._issues(None)

    bad_entries = {
        "schema_version": "report-output-v1",
        "reports": [
            {
                "replay_player_public_id": published_graph.player_public_id,
                "analysis_run_id": report_database.analysis_run_id,
                "structured_asset_public_id": stable_uuid("structured"),
                "presentation_asset_public_id": stable_uuid("presentation"),
            },
            {
                "replay_player_public_id": None,
                "analysis_run_id": None,
                "structured_asset_public_id": stable_uuid("wide-structured"),
                "presentation_asset_public_id": stable_uuid("wide-presentation"),
            },
        ],
    }
    with pytest.raises(ReportGraphContractError, match="sorted and unique"):
        report_query.ReportQueryService._report_entries(bad_entries)

    factory = report_database.session_factory  # type: ignore[assignment]
    with factory() as session:
        observed = session.scalar(
            select(EvidenceItem).where(EvidenceItem.public_id == published_graph.telemetry_evidence_id)
        )
        assert observed is not None
        with pytest.raises(ReportGraphContractError, match="conflicting"):
            report_query.ReportQueryService._validate_links(
                (("supporting", observed), ("contradicting", observed)),
                observed.replay_id,
                {observed.public_id: "observed"},
                allowed_tiers={"observed"},
            )
        with pytest.raises(ReportGraphContractError, match="tier"):
            report_query.ReportQueryService._validate_links(
                (("supporting", observed),),
                observed.replay_id,
                {observed.public_id: "observed"},
                allowed_tiers={"derived"},
            )
        with pytest.raises(ReportGraphContractError, match="selected"):
            report_query.ReportQueryService._validate_links(
                (("supporting", observed),),
                observed.replay_id,
                {},
                allowed_tiers={"observed"},
            )
        assert (
            report_query.ReportQueryService._document_player_id(
                session,
                graph.replay_wide.document,
            )
            is None
        )
