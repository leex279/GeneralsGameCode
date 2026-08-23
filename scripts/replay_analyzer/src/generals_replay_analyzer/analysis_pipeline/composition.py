"""Single production composition root for replay analysis stages."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from ..config import AnalyzerSettings
from ..features.activity import ActivityExtractor
from ..features.build_order import BuildOrderExtractor
from ..features.combat import CombatExtractor
from ..features.economy import EconomyExtractor
from ..features.production import ProductionExtractor
from ..features.service import FeatureExtractionService, RegisteredExtractor
from ..importing.engine_acquirer import EngineTelemetryAcquirer
from ..importing.parser_import import ParserObservationImporter
from ..importing.service import (
    ImportService,
    StageHandlerRegistration,
    TelemetryAcquirer,
    TerminalDependencyPolicy,
)
from ..importing.stages import RENDER_REPORT_VERSION
from ..importing.telemetry_import import ObservationImportHandler, TelemetryObservationImporter
from ..llm.provider import OllamaClientConfig
from ..llm.service import HttpxOllamaTransport, OllamaAnalysisService
from ..longitudinal.service import LongitudinalAnalysisService
from ..parser import ParsedReplay
from ..report.service import ReportService
from ..spatial.features import SPATIAL_REGISTRY, SpatialFeatureExtractor
from ..storage import ContentAddressedStore
from ..strategy.service import StrategyAssessmentService
from .handlers import (
    AnalyzeLLMHandler,
    AssessStrategiesHandler,
    ClosableOllamaTransport,
    DeriveFeaturesHandler,
    RenderReportHandler,
)

ENGINE_TELEMETRY_ACQUIRER_VERSION = "engine-telemetry-v1"


# TheSuperHackers @feature Leex 23/08/2026 Enable engine telemetry only in explicitly configured launch-capable processes. (#TBD)
def configured_engine_telemetry_acquirer(settings: AnalyzerSettings) -> EngineTelemetryAcquirer | None:
    """Return the configured engine adapter, or preserve parser-only composition."""
    if settings.engine_executable is None:
        return None
    return EngineTelemetryAcquirer(settings)


# TheSuperHackers @feature Leex 22/08/2026 Centralize the exact production analysis registration graph. (#TBD)
def create_production_import_service(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    *,
    parser: Callable[[Path], ParsedReplay],
    telemetry_acquirer: TelemetryAcquirer | None,
    clock: Callable[[], datetime],
    parser_version: str,
    telemetry_acquirer_version: str,
    transport_factory: Callable[[OllamaClientConfig], ClosableOllamaTransport] = HttpxOllamaTransport,
) -> ImportService:
    """Return one ImportService with the sole production registration tuple."""
    extractors = cast(
        tuple[RegisteredExtractor, ...],
        (
            ActivityExtractor(),
            BuildOrderExtractor(),
            CombatExtractor(),
            EconomyExtractor(),
            ProductionExtractor(),
            SpatialFeatureExtractor(),
        ),
    )
    features = FeatureExtractionService(
        session_factory,
        extractors=extractors,
        registry=SPATIAL_REGISTRY,
    )
    strategies = StrategyAssessmentService(session_factory, data_root=settings.data_root)
    longitudinal = LongitudinalAnalysisService(session_factory, analyzer_settings=settings)
    reports = ReportService(session_factory, settings=settings)
    observation = ObservationImportHandler(
        ParserObservationImporter(
            session_factory,
            settings.data_root,
            parser=parser,
            parser_version=parser_version,
            schema_version=1,
            clock=clock,
        ),
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=clock,
        ),
    )

    def analysis_service(transport: ClosableOllamaTransport) -> OllamaAnalysisService:
        return OllamaAnalysisService(
            session_factory,
            settings=settings,
            store=ContentAddressedStore(settings.cache_directory / "ollama"),
            transport=transport,
            clock=clock,
        )

    registrations = (
        StageHandlerRegistration(
            "import_observations",
            "1",
            observation,
            TerminalDependencyPolicy(failed_stages=frozenset({"parse", "telemetry"})),
        ),
        StageHandlerRegistration(
            "derive_features",
            "1",
            DeriveFeaturesHandler(session_factory, features),
        ),
        StageHandlerRegistration(
            "assess_strategies",
            "1",
            AssessStrategiesHandler(
                session_factory,
                settings,
                features,
                strategies,
                longitudinal,
            ),
        ),
        StageHandlerRegistration(
            "analyze_llm",
            "1",
            AnalyzeLLMHandler(settings, transport_factory, analysis_service),
        ),
        StageHandlerRegistration(
            "render_report",
            RENDER_REPORT_VERSION,
            RenderReportHandler(reports, session_factory=session_factory),
        ),
    )
    return ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parser,
        telemetry_acquirer=telemetry_acquirer,
        clock=clock,
        parser_version=parser_version,
        telemetry_acquirer_version=telemetry_acquirer_version,
        stage_handlers=registrations,
    )
