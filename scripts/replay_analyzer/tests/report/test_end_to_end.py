from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from telemetry.test_economy_production_contract import ENGINE_IDENTITY, RUN_ID, _valid_trace

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    AssessmentEvidence,
    EconomyEvent,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    Replay,
    ReplayPlayer,
    StrategyAssessment,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.identity.service import PlayerIdentityService
from generals_replay_analyzer.importing import (
    AcquisitionDiagnostic,
    ImportRequest,
    ImportService,
    StageExecutionContext,
    StageHandlerRegistration,
    TelemetryArtifact,
    TerminalDependencyPolicy,
)
from generals_replay_analyzer.importing.identity_import import IdentityResolvingParserObservationImporter
from generals_replay_analyzer.importing.parser_import import ParserObservationImporter
from generals_replay_analyzer.importing.telemetry_import import (
    ObservationImportHandler,
    TelemetryObservationImporter,
)
from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.report.model import ReportRequest
from generals_replay_analyzer.report.render_html import render_html
from generals_replay_analyzer.report.render_json import render_json
from generals_replay_analyzer.report.render_text import render_text
from generals_replay_analyzer.report.service import ReportService
from generals_replay_analyzer.storage import ContentAddressedStore
from generals_replay_analyzer.telemetry import load_validated_telemetry_bundle

from .conftest import SeededReportDatabase


class _DeterministicUUIDs:
    def __init__(self, start: int) -> None:
        self._value = start

    def __call__(self) -> UUID:
        value = UUID(int=self._value, version=4)
        self._value += 1
        return value


def _uuid(value: int) -> str:
    return str(UUID(int=value, version=4))


def test_offline_persisted_evidence_to_all_formats_and_managed_assets_is_byte_stable(
    report_database: SeededReportDatabase,
) -> None:
    engine_runner_before = sys.modules.get("generals_replay_analyzer.engine.runner")
    service = ReportService(
        report_database.session_factory,  # type: ignore[arg-type]
        settings=report_database.settings,
        store=ContentAddressedStore(report_database.settings.cache_directory / "reports"),
    )
    request = ReportRequest(
        report_database.replay_public_id,
        report_database.replay_player_public_id,
        include_validated_ollama=False,
        publish=False,
    )
    first = service.create(request)
    second = service.create(request)
    first_bytes = (
        render_json(first.document),
        render_html(first.document).encode(),
        render_text(first.document).encode(),
    )
    second_bytes = (
        render_json(second.document),
        render_html(second.document).encode(),
        render_text(second.document).encode(),
    )
    assert first_bytes == second_bytes
    assert first.document.ollama.status == "not_requested"
    assert engine_runner_before is sys.modules.get("generals_replay_analyzer.engine.runner")

    published = service.create(
        ReportRequest(report_database.replay_public_id, report_database.replay_player_public_id, publish=True)
    )
    assert published.structured_asset is not None and published.presentation_bundle_asset is not None
    store = ContentAddressedStore(report_database.settings.cache_directory / "reports")
    structured = store.verify(published.structured_asset.sha256).path.read_bytes()
    bundle_bytes = store.verify(published.presentation_bundle_asset.sha256).path.read_bytes()
    assert structured == first_bytes[0]
    bundle = json.loads(bundle_bytes)
    assert bundle["schema_version"] == "report-presentation-bundle-v1"
    assert hashlib.sha256(bundle["html"]["text"].encode()).hexdigest() == bundle["html"]["sha256"]
    assert hashlib.sha256(bundle["text"]["text"].encode()).hexdigest() == bundle["text"]["sha256"]
    assert "path" not in bundle and "created_at" not in bundle


def test_real_offline_import_observation_deterministic_analytics_to_report(tmp_path: Path) -> None:
    """Exercise the public offline intake DAG before assembling and publishing the report."""
    now = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)
    settings = AnalyzerSettings(data_root=tmp_path / "product")
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    replay_path = tmp_path / "real-input.rep"
    shutil.copyfile(
        Path(__file__).parents[1] / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep",
        replay_path,
    )
    trace_root = tmp_path / "telemetry"
    trace_root.mkdir()
    trace = _valid_trace(trace_root, "trace.ndjson")
    manifest = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])["payload"]
    catalog_path = trace_root / manifest["game_data_catalog"]["path"]
    validated_bundle = load_validated_telemetry_bundle(trace)
    map_paths = validated_bundle.map_member_paths
    assert str(validated_bundle.manifest.run_id) == RUN_ID

    def fake_parser(path: Path):  # type: ignore[no-untyped-def]
        parsed = parse_replay(path)
        slots = tuple(
            replace(
                slot,
                kind="human" if slot.index == 0 else "open",
                name="Human" if slot.index == 0 else None,
                ip=None,
                port=None,
                accepted=None,
                has_map=None,
                color=0 if slot.index == 0 else None,
                player_template=None,
                start_position=0 if slot.index == 0 else None,
                team=0 if slot.index == 0 else None,
                nat_behavior=None,
                ai_difficulty=None,
            )
            for slot in parsed.header.slots
        )
        return replace(
            parsed,
            header=replace(
                parsed.header,
                slots=slots,
                local_player_index=0,
                map="maps/test.map",
                seed=7,
            ),
        )

    class FakeTelemetryAcquirer:
        def acquire(self, _replay: Path, _sha256: str) -> TelemetryArtifact:
            return TelemetryArtifact(
                RUN_ID,
                "success",
                "complete",
                "full",
                trace,
                catalog_path,
                map_paths,
                None,
                None,
                None,
                0,
                ENGINE_IDENTITY,
                "d" * 64,
                (AcquisitionDiagnostic("fixture", "offline deterministic telemetry"),),
            )

    def derive_features(context: StageExecutionContext) -> dict[str, object]:
        with factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
            telemetry = (
                session.scalar(
                    select(TelemetryRun).where(TelemetryRun.replay_id == replay.id, TelemetryRun.status == "succeeded")
                )
                if replay is not None
                else None
            )
            event_row = (
                session.execute(
                    select(EconomyEvent, EvidenceItem)
                    .join(TelemetryEvent, TelemetryEvent.id == EconomyEvent.telemetry_event_id)
                    .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                    .where(
                        EconomyEvent.telemetry_run_id == telemetry.id,
                        EconomyEvent.event_type == "cash_changed",
                        EconomyEvent.replay_player_id.is_not(None),
                    )
                    .order_by(TelemetryEvent.sequence)
                ).first()
                if telemetry is not None
                else None
            )
            event, observed = event_row if event_row is not None else (None, None)
            player = session.get(ReplayPlayer, event.replay_player_id) if event is not None else None
            assert replay is not None and telemetry is not None and player is not None and observed is not None
            feature_set = FeatureSet(
                public_id=_uuid(91_000),
                replay_id=replay.id,
                replay_player_id=player.id,
                extractor_name="offline-economy",
                extractor_version="1",
                input_digest="1" * 64,
                cache_key="2" * 64,
                status="running",
                settings_json={},
                created_at=now,
            )
            evidence = EvidenceItem(
                public_id=_uuid(91_001),
                replay_id=replay.id,
                telemetry_run_id=telemetry.id,
                tier="derived",
                source_kind="feature",
                source_key="offline:feature:income",
                schema_version=1,
                created_at=now,
            )
            session.add_all((feature_set, evidence))
            session.flush()
            feature = Feature(
                public_id=_uuid(91_002),
                feature_set_id=feature_set.id,
                evidence_item_id=evidence.id,
                name="income_total",
                value_type="integer",
                integer_value=600,
                unit="credits",
                scope_type="player",
                scope_key=player.public_id,
                replay_player_id=player.id,
                frame_start=0,
                frame_end=20,
                quality="available",
                details_json={"source": "cash_changed"},
            )
            session.add(feature)
            session.flush()
            session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=observed.id, role="input"))
            feature_set.status = "succeeded"
            feature_set.completed_at = now
            session.commit()
        return {"feature_set_public_id": _uuid(91_000)}

    def assess_strategies(context: StageExecutionContext) -> dict[str, object]:
        with factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
            feature = session.scalar(select(Feature).where(Feature.public_id == _uuid(91_002)))
            feature_evidence = session.get(EvidenceItem, feature.evidence_item_id) if feature is not None else None
            player = session.get(ReplayPlayer, feature.replay_player_id) if feature is not None else None
            assert replay is not None and player is not None and feature_evidence is not None
            evidence = EvidenceItem(
                public_id=_uuid(91_003),
                replay_id=replay.id,
                telemetry_run_id=feature_evidence.telemetry_run_id,
                tier="derived",
                source_kind="strategy_rule",
                source_key="offline:strategy:economy",
                schema_version=1,
                created_at=now,
            )
            session.add(evidence)
            session.flush()
            assessment = StrategyAssessment(
                public_id=_uuid(91_004),
                evidence_item_id=evidence.id,
                replay_id=replay.id,
                replay_player_id=player.id,
                method="rule",
                strategy_label="economy_first",
                phase="opening",
                taxonomy_version="offline-v1",
                rule_version="offline-rule-v1",
                frame_start=0,
                frame_end=20,
                quality="available",
                confidence=1.0,
                details_json={"rule": "income_positive"},
                created_at=now,
            )
            session.add(assessment)
            session.flush()
            session.add(
                AssessmentEvidence(assessment_id=assessment.id, evidence_item_id=feature_evidence.id, role="supporting")
            )
            session.commit()
        return {"assessment_public_id": _uuid(91_004)}

    observation = ObservationImportHandler(
        IdentityResolvingParserObservationImporter(
            ParserObservationImporter(
                factory,
                settings.data_root,
                parser=fake_parser,
                parser_version="offline-parser-v1",
                schema_version=1,
                clock=lambda: now,
                uuid_factory=_DeterministicUUIDs(92_000_000_000),
            ),
            PlayerIdentityService(factory, now_factory=lambda: now),
        ),
        TelemetryObservationImporter(
            factory,
            settings.data_root,
            clock=lambda: now,
            uuid_factory=_DeterministicUUIDs(93_000_000_000),
        ),
    )
    import_service = ImportService(
        factory,
        settings,
        ContentAddressedStore(settings.managed_replay_directory),
        ContentAddressedStore(settings.cache_directory / "artifacts"),
        parser=fake_parser,
        telemetry_acquirer=FakeTelemetryAcquirer(),
        clock=lambda: now,
        parser_version="offline-parser-v1",
        telemetry_acquirer_version="offline-telemetry-v1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                "1",
                observation,
                TerminalDependencyPolicy(failed_stages=frozenset({"parse", "telemetry"})),
            ),
            StageHandlerRegistration("derive_features", "1", derive_features),
            StageHandlerRegistration("assess_strategies", "1", assess_strategies),
        ),
    )
    import_service.submit(ImportRequest(replay_path, request_telemetry=True))
    completed = import_service.run_available("offline-worker", limit=20)
    statuses = {job.stage: job.status for job in completed}
    with factory() as diagnostic_session:
        telemetry_diagnostics = diagnostic_session.scalar(select(TelemetryRun.diagnostics_json))
    assert statuses.get("assess_strategies") == "succeeded", json.dumps(
        {
            "jobs": [f"{job.stage}:{job.status}:{job.error_code}" for job in completed],
            "telemetry": telemetry_diagnostics,
        },
        sort_keys=True,
    )
    with factory() as session:
        replay = session.scalar(select(Replay))
        feature = session.scalar(select(Feature).where(Feature.public_id == _uuid(91_002)))
        player = session.get(ReplayPlayer, feature.replay_player_id) if feature is not None else None
        assert replay is not None and player is not None
        request = ReportRequest(replay.public_id, player.public_id, publish=True)
    receipt = ReportService(factory, settings=settings).create(request)
    assert receipt.structured_asset is not None and receipt.presentation_bundle_asset is not None
    assert {value.label for value in receipt.document.derived} >= {"income_total", "economy_first"}
    assert {value.details[0][0] for value in receipt.document.observed}  # production evidence was projected
    assert receipt.structured_asset.sha256 == hashlib.sha256(render_json(receipt.document)).hexdigest()
    cached = ReportService(factory, settings=settings).create(request)
    assert cached.cache_hit is True and cached.structured_asset == receipt.structured_asset
    engine.dispose()
