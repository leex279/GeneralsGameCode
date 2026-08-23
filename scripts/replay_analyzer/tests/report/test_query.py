from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import func, select
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
    ReplayPlayer,
    Report,
    StrategyAssessment,
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
    RENDER_REPORT,
    RENDER_REPORT_VERSION,
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
from generals_replay_analyzer.report.model import ReportRequest, document_to_mapping
from generals_replay_analyzer.report.query import (
    EvidenceQuery,
    FixedReportQuery,
    ReportGraphAmbiguousError,
    ReportGraphContractError,
    ReportGraphNotFoundError,
    ReportQueryService,
)
from generals_replay_analyzer.report.read_model import (
    DerivedAssessmentEvidenceDTO,
    DerivedFeatureEvidenceDTO,
    DerivedLongitudinalEvidenceDTO,
    InferredAssessmentEvidenceDTO,
    ObservedCommandEvidenceDTO,
    ObservedTelemetryEvidenceDTO,
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
    output_json: dict[str, object],
    now: datetime,
    component_version: str = "1",
    idempotency_key: str | None = None,
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
        status="succeeded",
        priority=100,
        attempt_count=1,
        max_attempts=3,
        available_at=now,
        started_at=now,
        completed_at=now,
        input_json=input_json,
        output_json=output_json,
        retryable=False,
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
        rule_evidence = session.scalar(select(EvidenceItem).where(EvidenceItem.source_kind == "strategy_rule"))
        longitudinal_evidence = session.scalar(
            select(EvidenceItem).where(EvidenceItem.source_kind == "longitudinal_corpus")
        )
        assert replay is not None and player is not None and feature_set is not None and parser is not None
        assert feature is not None and rule_evidence is not None and longitudinal_evidence is not None
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
        base_input = {
            "analysis_plan_version": 1,
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "parser_run_id": parser.run_id,
            "parser_version": parser.parser_version,
            "selected_dependency_digest": "a" * 64,
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
            input_json={"replay_public_id": replay.public_id},
            output_json={
                "parser_run_id": parser.run_id,
                "selected_dependency_digest": base_input["selected_dependency_digest"],
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
        session.add_all((observation_job, derive_job, assess_job, llm_job, report_job))
        session.flush()
        session.add_all(
            (
                JobDependency(job_id=derive_job.id, depends_on_job_id=observation_job.id, created_at=now),
                JobDependency(job_id=assess_job.id, depends_on_job_id=derive_job.id, created_at=now),
                JobDependency(job_id=llm_job.id, depends_on_job_id=assess_job.id, created_at=now),
                JobDependency(job_id=report_job.id, depends_on_job_id=llm_job.id, created_at=now),
                _stage_result(
                    observation_job,
                    {
                        "parser_run_id": parser.run_id,
                        "selected_dependency_digest": base_input["selected_dependency_digest"],
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


def test_fixed_query_rejects_cross_replay_lookup_and_ambiguous_stage_graph(
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
        duplicate = _job(
            public_id=stable_uuid("query-ambiguous-report-job"),
            replay_id=original.replay_id,  # type: ignore[arg-type]
            replay_sha256="a" * 64,
            stage="render_report",
            input_json=dict(original.input_json),
            output_json=dict(original.output_json),
            now=now,
        )
        session.add(duplicate)
        session.flush()
        dependency_id = session.scalar(
            select(JobDependency.depends_on_job_id).where(JobDependency.job_id == original.id)
        )
        assert dependency_id is not None
        session.add(JobDependency(job_id=duplicate.id, depends_on_job_id=dependency_id, created_at=now))
        session.add(_stage_result(duplicate, dict(original.output_json), now, "query-ambiguous-result"))

    with pytest.raises(ReportGraphAmbiguousError):
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
