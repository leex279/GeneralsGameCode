"""Production handlers for the five-stage replay-analysis pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import AnalyzerSettings
from ..db.models import EvidenceItem, ParserRun, Player, Replay, ReplayPlayer
from ..features.evidence import EvidenceRef
from ..features.registry import FeatureRegistry
from ..features.service import ExtractFeaturesRequest, FeatureSetReceipt
from ..importing.jobs import StageFailure
from ..importing.service import StageDependencyOutput, StageExecutionContext
from ..importing.stages import (
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
)
from ..llm.evidence_bundle import EvidenceClaim, build_evidence_bundle
from ..llm.provider import CancellationSignal, OllamaClientConfig, OllamaTransport
from ..llm.service import AnalysisOutcome, AnalysisRequest
from ..longitudinal.segments import (
    LongitudinalEvidenceDTO,
    LongitudinalRequest,
    LongitudinalResultDTO,
    LongitudinalRunReceipt,
    LongitudinalSettings,
    SegmentKey,
)
from ..longitudinal.service import LongitudinalAnalysisError
from ..report.model import ReportReceipt, ReportRequest
from ..spatial.features import SPATIAL_REGISTRY
from ..strategy.rules import RuleAssessment
from ..strategy.service import StrategyAssessmentReceipt
from .codecs import (
    LongitudinalStatus,
    PipelineCodecError,
    PlayerAssessmentSelection,
    PlayerFeatureSelection,
    PlayerLLMSelection,
    decode_assessment_output,
    decode_feature_output,
    decode_llm_output,
    encode_assessment_output,
    encode_feature_output,
    encode_llm_output,
)

PRODUCTION_EXTRACTOR_NAMES = ("activity", "build", "combat", "economy", "production", "spatial")
_LONGITUDINAL_METRICS = ("economy.cash_change_total",)


def _extractor_names(replay_player_public_id: str | None) -> tuple[str, ...]:
    return ("spatial",) if replay_player_public_id is None else PRODUCTION_EXTRACTOR_NAMES


class ClosableOllamaTransport(OllamaTransport, Protocol):
    async def aclose(self) -> None: ...


class FeatureService(Protocol):
    def extract(self, request: ExtractFeaturesRequest) -> tuple[FeatureSetReceipt, ...]: ...


class StrategyService(Protocol):
    def assess_rule_candidates(
        self,
        replay_public_id: str,
        replay_player_public_id: str | None,
        feature_set_public_ids: tuple[str, ...],
        registry: FeatureRegistry,
    ) -> StrategyAssessmentReceipt: ...


class LongitudinalService(Protocol):
    def analyze(self, request: LongitudinalRequest) -> LongitudinalRunReceipt: ...


class AnalysisService(Protocol):
    async def analyze(self, request: AnalysisRequest, cancellation: CancellationSignal | None) -> AnalysisOutcome: ...


class Reports(Protocol):
    def create(self, request: ReportRequest) -> ReportReceipt: ...


def _failure(code: str, message: str, error: Exception | None = None) -> StageFailure:
    del error
    return StageFailure(code, message, retryable=False)


def _dependency(
    context: StageExecutionContext,
    *,
    stage: str,
    version: str,
) -> Mapping[str, object]:
    if type(context) is not StageExecutionContext or type(context.dependencies) is not tuple:
        raise _failure("dependency_contract_invalid", "stage context is not the frozen production contract")
    if len(context.dependencies) != 1:
        raise _failure("dependency_contract_invalid", "stage requires one exact direct dependency")
    dependency = context.dependencies[0]
    if (
        type(dependency) is not StageDependencyOutput
        or dependency.stage != stage
        or dependency.component_version != version
        or dependency.status != "succeeded"
        or not isinstance(dependency.output, Mapping)
        or any(
            value is not None for value in (dependency.error_code, dependency.error_message, dependency.error_details)
        )
    ):
        raise _failure("dependency_contract_invalid", "direct dependency output is invalid")
    return cast(Mapping[str, object], dependency.output)


def _context(context: StageExecutionContext, stage: str, version: str) -> None:
    if type(context) is not StageExecutionContext or context.stage != stage or context.component_version != version:
        raise _failure("stage_context_invalid", "stage execution context is invalid")


def _observation_output(value: Mapping[str, object]) -> str:
    expected = {
        "idempotency_key",
        "parser_run_id",
        "parser_command_count",
        "telemetry_run_id",
        "telemetry_event_count",
    }
    if set(value) != expected:
        raise _failure("dependency_contract_invalid", "observation output fields are invalid")
    parser_run_id = value.get("parser_run_id")
    idempotency_key = value.get("idempotency_key")
    telemetry_run_id = value.get("telemetry_run_id")
    if (
        not _canonical_uuid(parser_run_id)
        or type(idempotency_key) is not str
        or not idempotency_key
        or idempotency_key != idempotency_key.strip()
        or len(idempotency_key.encode("utf-8")) > 512
        or any(marker in idempotency_key for marker in ("/", "\\", "\x00"))
        or type(value.get("parser_command_count")) is not int
        or cast(int, value["parser_command_count"]) < 0
        or type(value.get("telemetry_event_count")) is not int
        or cast(int, value["telemetry_event_count"]) < 0
        or (telemetry_run_id is not None and not _canonical_uuid(telemetry_run_id))
    ):
        raise _failure("dependency_contract_invalid", "observation output values are invalid")
    return cast(str, parser_run_id)


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


# TheSuperHackers @feature Leex 22/08/2026 Derive exact registered features for sorted replay-player identities. (#TBD)
class DeriveFeaturesHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        feature_service: FeatureService,
    ) -> None:
        self._session_factory = session_factory
        self._features = feature_service

    def __call__(self, context: StageExecutionContext) -> Mapping[str, object]:
        _context(context, DERIVE_FEATURES, DERIVE_FEATURES_VERSION)
        parser_run_id = _observation_output(
            _dependency(
                context,
                stage=IMPORT_OBSERVATIONS,
                version=IMPORT_OBSERVATIONS_VERSION,
            )
        )
        try:
            with self._session_factory() as session:
                replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
                if replay is None:
                    raise ValueError("observation parser graph is unavailable")
                parser = session.scalar(
                    select(ParserRun).where(
                        ParserRun.run_id == parser_run_id,
                        ParserRun.replay_id == replay.id,
                    )
                )
                if parser is None:
                    raise ValueError("observation parser graph is unavailable")
                rows = tuple(
                    session.execute(
                        select(ReplayPlayer.public_id, Player.public_id)
                        .outerjoin(Player, Player.id == ReplayPlayer.player_id)
                        .where(ReplayPlayer.replay_id == replay.id, ReplayPlayer.parser_run_id == parser.id)
                        .order_by(ReplayPlayer.public_id)
                    )
                )
            subjects: tuple[tuple[str | None, str | None], ...] = (
                (None, None),
                *(tuple((row[0], row[1]) for row in rows)),
            )
            selections: list[PlayerFeatureSelection] = []
            for replay_player_public_id, canonical_player_public_id in subjects:
                receipts = self._features.extract(
                    ExtractFeaturesRequest(
                        context.replay_public_id,
                        replay_player_public_id,
                        _extractor_names(replay_player_public_id),
                    )
                )
                selections.append(
                    PlayerFeatureSelection(
                        replay_player_public_id,
                        canonical_player_public_id,
                        tuple(sorted(receipt.feature_set_public_id for receipt in receipts)),
                    )
                )
            return encode_feature_output(tuple(selections))
        except StageFailure:
            raise
        except Exception as error:
            raise _failure("derive_features_failed", "deterministic feature derivation failed", error) from error


# TheSuperHackers @feature Leex 22/08/2026 Assess strategies and longitudinal context through accepted public services. (#TBD)
class AssessStrategiesHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AnalyzerSettings,
        feature_service: FeatureService,
        strategy_service: StrategyService,
        longitudinal_service: LongitudinalService,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._features = feature_service
        self._strategies = strategy_service
        self._longitudinal = longitudinal_service

    def __call__(self, context: StageExecutionContext) -> Mapping[str, object]:
        _context(context, ASSESS_STRATEGIES, ASSESS_STRATEGIES_VERSION)
        try:
            selected = decode_feature_output(
                _dependency(context, stage=DERIVE_FEATURES, version=DERIVE_FEATURES_VERSION)
            )
            players: list[PlayerAssessmentSelection] = []
            for player in selected:
                receipts = self._features.extract(
                    ExtractFeaturesRequest(
                        context.replay_public_id,
                        player.replay_player_public_id,
                        _extractor_names(player.replay_player_public_id),
                    )
                )
                feature_set_ids = tuple(sorted(receipt.feature_set_public_id for receipt in receipts))
                if feature_set_ids != player.feature_set_public_ids:
                    raise PipelineCodecError("feature cache selection changed across direct stages")
                strategy = self._strategies.assess_rule_candidates(
                    context.replay_public_id,
                    player.replay_player_public_id,
                    feature_set_ids,
                    SPATIAL_REGISTRY,
                )
                claims = self._claims(receipts, strategy.assessments)
                longitudinal_status: LongitudinalStatus = "not_applicable"
                longitudinal_run_id = None
                if player.replay_player_public_id is not None and player.canonical_player_public_id is None:
                    longitudinal_status = "canonical_player_unresolved"
                elif player.canonical_player_public_id is not None:
                    try:
                        settings = LongitudinalSettings(
                            minimum_sample_size=self._settings.minimum_longitudinal_sample_size,
                            bootstrap_resamples=100,
                            confidence_level=0.95,
                            enabled_metrics=_LONGITUDINAL_METRICS,
                        )
                        longitudinal = self._longitudinal.analyze(
                            LongitudinalRequest(
                                player.canonical_player_public_id,
                                SegmentKey(),
                                _LONGITUDINAL_METRICS,
                                (),
                                settings,
                            )
                        )
                        longitudinal_status = "succeeded"
                        longitudinal_run_id = longitudinal.run_id
                        claims.extend(self._longitudinal_claims(longitudinal.results))
                    except LongitudinalAnalysisError:
                        longitudinal_status = "unavailable"
                bundle = build_evidence_bundle(
                    replay_public_id=context.replay_public_id,
                    replay_sha256=context.replay_sha256,
                    claims=tuple(sorted(claims, key=lambda claim: (claim.kind, claim.claim_id))),
                )
                players.append(
                    PlayerAssessmentSelection(
                        player.replay_player_public_id,
                        player.canonical_player_public_id,
                        feature_set_ids,
                        strategy.cache_key,
                        longitudinal_status,
                        longitudinal_run_id,
                        bundle,
                    )
                )
            return encode_assessment_output(tuple(players))
        except StageFailure:
            raise
        except Exception as error:
            raise _failure("assess_strategies_failed", "deterministic strategy assessment failed", error) from error

    @staticmethod
    def _claims(
        receipts: tuple[FeatureSetReceipt, ...], assessments: tuple[RuleAssessment, ...]
    ) -> list[EvidenceClaim]:
        values = tuple(value for receipt in receipts for value in receipt.features)
        refs: dict[str, EvidenceRef] = {}
        for value in values:
            for ref in value.input_evidence + value.supporting_evidence + value.contradicting_evidence:
                refs[ref.public_id] = ref
        for assessment in assessments:
            for ref in assessment.supporting_evidence + assessment.contradicting_evidence:
                refs[ref.public_id] = ref
        authorized = tuple(sorted(refs.values(), key=lambda ref: ref.public_id))
        claims = [
            EvidenceClaim.from_feature(value, authorized_evidence=authorized)
            for value in values
            if value.input_evidence + value.supporting_evidence + value.contradicting_evidence
        ]
        claims.extend(
            EvidenceClaim.from_rule_assessment(assessment, authorized_evidence=authorized)
            for assessment in assessments
            if assessment.supporting_evidence + assessment.contradicting_evidence
        )
        return claims

    def _longitudinal_claims(self, results: tuple[LongitudinalResultDTO, ...]) -> list[EvidenceClaim]:
        claims: list[EvidenceClaim] = []
        with self._session_factory() as session:
            for result in results:
                evidence = session.scalar(
                    select(EvidenceItem).where(EvidenceItem.public_id == result.evidence_public_id)
                )
                if evidence is None:
                    raise ValueError("longitudinal evidence is unavailable")
                dto = LongitudinalEvidenceDTO(
                    evidence.public_id,
                    evidence.tier,
                    evidence.source_kind,
                    evidence.source_key,
                    evidence.schema_version,
                )
                claims.append(EvidenceClaim.from_longitudinal_result(result, evidence=dto))
        return claims


# TheSuperHackers @feature Leex 22/08/2026 Keep optional Ollama calls loopback-only and close owned transports. (#TBD)
class AnalyzeLLMHandler:
    def __init__(
        self,
        settings: AnalyzerSettings,
        transport_factory: Callable[[OllamaClientConfig], ClosableOllamaTransport],
        service_factory: Callable[[ClosableOllamaTransport], AnalysisService],
    ) -> None:
        self._settings = settings
        self._transport_factory = transport_factory
        self._service_factory = service_factory

    def __call__(self, context: StageExecutionContext) -> Mapping[str, object]:
        _context(context, ANALYZE_LLM, ANALYZE_LLM_VERSION)
        if context.input.get("allow_ollama") is not True:
            raise _failure("ollama_not_opted_in", "Ollama analysis requires exact per-job opt-in")
        try:
            selected = decode_assessment_output(
                _dependency(context, stage=ASSESS_STRATEGIES, version=ASSESS_STRATEGIES_VERSION)
            )
            return asyncio.run(self._analyze(context, selected))
        except StageFailure:
            raise
        except Exception as error:
            raise _failure("analyze_llm_failed", "optional Ollama analysis failed before fallback", error) from error

    async def _analyze(
        self,
        context: StageExecutionContext,
        selected: tuple[PlayerAssessmentSelection, ...],
    ) -> Mapping[str, object]:
        config = OllamaClientConfig(self._settings.ollama_url)
        transport = self._transport_factory(config)
        try:
            service = self._service_factory(transport)
            players: list[PlayerLLMSelection] = []
            for player in selected:
                outcome = await service.analyze(
                    AnalysisRequest(
                        context.replay_public_id,
                        player.replay_player_public_id,
                        player.evidence_bundle,
                        allow_ollama=True,
                    ),
                    None,
                )
                players.append(
                    PlayerLLMSelection(
                        player.replay_player_public_id,
                        outcome.run_id,
                        outcome.llm_status,
                        outcome.code,
                    )
                )
            return encode_llm_output(tuple(players))
        finally:
            await transport.aclose()


# TheSuperHackers @feature Leex 22/08/2026 Render exact direct deterministic or LLM-selected report graphs. (#TBD)
class RenderReportHandler:
    def __init__(self, report_service: Reports) -> None:
        self._reports = report_service

    def __call__(self, context: StageExecutionContext) -> Mapping[str, object]:
        _context(context, RENDER_REPORT, RENDER_REPORT_VERSION)
        try:
            dependency = context.dependencies[0] if len(context.dependencies) == 1 else None
            if dependency is None:
                raise PipelineCodecError("report requires one direct dependency")
            if dependency.stage == ASSESS_STRATEGIES:
                deterministic = decode_assessment_output(
                    _dependency(context, stage=ASSESS_STRATEGIES, version=ASSESS_STRATEGIES_VERSION)
                )
                selected: tuple[tuple[str | None, str | None], ...] = tuple(
                    (item.replay_player_public_id, None) for item in deterministic
                )
            elif dependency.stage == ANALYZE_LLM:
                inferred = decode_llm_output(_dependency(context, stage=ANALYZE_LLM, version=ANALYZE_LLM_VERSION))
                selected = tuple((item.replay_player_public_id, item.analysis_run_id) for item in inferred)
            else:
                raise PipelineCodecError("report dependency stage is invalid")
            reports = []
            for replay_player_public_id, analysis_run_id in selected:
                receipt = self._reports.create(
                    ReportRequest(
                        context.replay_public_id,
                        replay_player_public_id,
                        include_validated_ollama=analysis_run_id is not None,
                        publish=True,
                        analysis_run_id=analysis_run_id,
                    )
                )
                reports.append(
                    {
                        "replay_player_public_id": replay_player_public_id,
                        "analysis_run_id": analysis_run_id,
                        "structured_asset_public_id": None
                        if receipt.structured_asset is None
                        else receipt.structured_asset.public_id,
                        "presentation_asset_public_id": None
                        if receipt.presentation_bundle_asset is None
                        else receipt.presentation_bundle_asset.public_id,
                    }
                )
            return {"schema_version": "report-output-v1", "reports": reports}
        except StageFailure:
            raise
        except Exception as error:
            raise _failure("render_report_failed", "report rendering failed", error) from error
