"""Durable upgrade coverage for parser runs imported before automatic identity wiring."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.composition import create_production_import_service
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    FeatureSet,
    Job,
    ParserRun,
    Player,
    PlayerAlias,
    PlayerIdentityOperation,
    Replay,
    ReplayPlayer,
)
from generals_replay_analyzer.identity.dto import IdentityResolutionBatch
from generals_replay_analyzer.identity.service import IdentityInvariantError
from generals_replay_analyzer.importing.identity_import import IdentityReconciliationHandler
from generals_replay_analyzer.importing.parser_import import ParserObservationImporter
from generals_replay_analyzer.importing.service import (
    ImportRequest,
    ImportService,
    StageExecutionContext,
    StageHandlerRegistration,
)
from generals_replay_analyzer.importing.stages import (
    ANALYZE_LLM,
    ASSESS_STRATEGIES,
    DERIVE_FEATURES,
    IMPORT_OBSERVATIONS,
    RECONCILE_IDENTITIES,
    RECONCILE_IDENTITIES_VERSION,
    RENDER_REPORT,
)
from generals_replay_analyzer.parser import ParsedReplay, parse_replay
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import MutableClock


def _uuid(value: int) -> str:
    return str(UUID(int=value))


def _legacy_parser(path: Path) -> ParsedReplay:
    parsed = parse_replay(path)
    slots = tuple(
        replace(
            slot,
            kind=(
                "human"
                if slot.index in {0, 1, 4}
                else "computer"
                if slot.index == 2
                else "open"
                if slot.index == 3
                else "closed"
            ),
            name={0: "leex279", 1: "FOX27", 2: "Brutal AI"}.get(slot.index),
        )
        for slot in parsed.header.slots
    )
    return replace(parsed, header=replace(parsed.header, slots=slots))


def _seed_succeeded_legacy_observation(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> tuple[str, str, tuple[object, ...]]:
    parser_importer = ParserObservationImporter(
        session_factory,
        settings.data_root,
        parser=_legacy_parser,
        parser_version="legacy-parser-v1",
        schema_version=1,
        clock=clock,
    )

    def legacy_observation(context: StageExecutionContext) -> dict[str, object]:
        result = parser_importer.import_replay(
            context.replay_sha256,
            parser_version="legacy-parser-v1",
            idempotency_key=context.idempotency_key,
        )
        assert result.status == "succeeded"
        return {
            "idempotency_key": context.idempotency_key,
            "parser_run_id": result.run_id,
            "parser_command_count": result.command_count,
            "telemetry_run_id": None,
            "telemetry_event_count": 0,
        }

    legacy = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=_legacy_parser,
        telemetry_acquirer=None,
        clock=clock,
        parser_version="legacy-parser-v1",
        telemetry_acquirer_version="legacy-telemetry-v1",
        stage_handlers=(StageHandlerRegistration(IMPORT_OBSERVATIONS, "1", legacy_observation),),
    )
    legacy.submit(ImportRequest(replay_file))
    for _ in range(8):
        completed = legacy.run_available("legacy-worker", limit=1)
        assert completed
        if completed[0].stage == IMPORT_OBSERVATIONS:
            assert completed[0].status == "succeeded"
            break
    else:
        raise AssertionError("legacy observation job did not settle")

    with session_factory.begin() as session:
        replay = session.scalar(select(Replay))
        parser_run = session.scalar(select(ParserRun).where(ParserRun.status == "succeeded"))
        first_slot = session.scalar(select(ReplayPlayer).order_by(ReplayPlayer.slot_index))
        assert replay is not None and parser_run is not None and first_slot is not None
        assert session.scalar(select(func.count()).select_from(Player)) == 0
        assert all(slot.player_id is None for slot in session.scalars(select(ReplayPlayer)))
        history = FeatureSet(
            public_id=_uuid(800_001),
            replay_id=replay.id,
            replay_player_id=first_slot.id,
            extractor_name="legacy-history",
            extractor_version="1",
            input_digest="a" * 64,
            cache_key="b" * 64,
            status="succeeded",
            settings_json={"historical": True},
            completed_at=clock(),
            error_json=None,
            created_at=clock(),
        )
        session.add(history)
        session.flush()
        history_snapshot = (
            history.public_id,
            history.replay_id,
            history.replay_player_id,
            history.cache_key,
            history.status,
            history.settings_json,
            clock().replace(tzinfo=None),
        )
        session.execute(
            delete(Job).where(
                Job.stage.in_((DERIVE_FEATURES, ASSESS_STRATEGIES, ANALYZE_LLM, RENDER_REPORT))
            )
        )
        return replay.public_id, parser_run.run_id, history_snapshot


def test_production_worker_reconciles_succeeded_v1_observations_once_without_rewriting_history(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    """Catch upgraded databases reusing succeeded v1 imports forever with null canonical identities."""
    replay_public_id, parser_run_id, history_snapshot = _seed_succeeded_legacy_observation(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        replay_file,
        clock,
    )
    service = create_production_import_service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=_legacy_parser,
        telemetry_acquirer=None,
        clock=clock,
        parser_version="legacy-parser-v1",
        telemetry_acquirer_version="legacy-telemetry-v1",
    )

    completed = service.run_available("upgrade-worker", limit=8)
    assert [(job.stage, job.status) for job in completed] == [(RECONCILE_IDENTITIES, "succeeded")]
    assert service.run_available("upgrade-retry", limit=8) == ()

    restarted = create_production_import_service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=_legacy_parser,
        telemetry_acquirer=None,
        clock=clock,
        parser_version="legacy-parser-v1",
        telemetry_acquirer_version="legacy-telemetry-v1",
    )
    assert restarted.run_available("upgrade-restart", limit=8) == ()

    with session_factory() as session:
        slots = tuple(
            session.execute(
                select(ReplayPlayer.slot_index, ReplayPlayer.slot_kind, ReplayPlayer.original_name, Player.public_id)
                .outerjoin(Player, Player.id == ReplayPlayer.player_id)
                .order_by(ReplayPlayer.slot_index)
            )
        )
        assert [(index, player_id is not None) for index, _kind, _name, player_id in slots] == [
            (0, True),
            (1, True),
            (2, False),
            (3, False),
            (4, False),
            (5, False),
            (6, False),
            (7, False),
        ]
        assert set(session.scalars(select(PlayerAlias.normalized_name))) == {"leex279", "fox27"}
        assert session.scalar(select(func.count()).select_from(PlayerIdentityOperation)) == 2
        assert sorted(session.scalars(select(Player.identity_revision))) == [1, 1]
        legacy_observation = session.scalar(select(Job).where(Job.stage == IMPORT_OBSERVATIONS))
        assert legacy_observation is not None
        assert (
            legacy_observation.component_version,
            legacy_observation.status,
            legacy_observation.error_code,
            legacy_observation.output_json["parser_run_id"],
        ) == ("1", "succeeded", None, parser_run_id)
        reconciliations = tuple(
            session.scalars(select(Job).where(Job.stage == RECONCILE_IDENTITIES))
        )
        assert len(reconciliations) == 1
        assert reconciliations[0].component_version == RECONCILE_IDENTITIES_VERSION
        assert reconciliations[0].output_json == {
            "affected_player_count": 2,
            "decision_count": 8,
            "idempotency_key": reconciliations[0].idempotency_key,
            "parser_run_id": parser_run_id,
        }
        history = session.scalar(select(FeatureSet).where(FeatureSet.public_id == _uuid(800_001)))
        assert history is not None
        assert (
            history.public_id,
            history.replay_id,
            history.replay_player_id,
            history.cache_key,
            history.status,
            history.settings_json,
            history.completed_at,
        ) == history_snapshot
        assert session.scalar(select(func.count()).select_from(FeatureSet)) == 1
        replay = session.scalar(select(Replay).where(Replay.public_id == replay_public_id))
        assert replay is not None


def test_reconciliation_failure_is_durable_sanitized_and_nonretryable(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    """Catch upgrade failures disappearing or leaking database paths through durable job output."""
    _seed_succeeded_legacy_observation(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        replay_file,
        clock,
    )

    class FailingResolver:
        def resolve_parser_run(self, **_kwargs: object) -> IdentityResolutionBatch:
            raise IdentityInvariantError("failed at C:\\private\\identity.sqlite provider-token=secret")

    service = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=_legacy_parser,
        telemetry_acquirer=None,
        clock=clock,
        parser_version="legacy-parser-v1",
        telemetry_acquirer_version="legacy-telemetry-v1",
        stage_handlers=(
            StageHandlerRegistration(
                RECONCILE_IDENTITIES,
                RECONCILE_IDENTITIES_VERSION,
                IdentityReconciliationHandler(FailingResolver()),
            ),
        ),
    )

    completed = service.run_available("failing-upgrade", limit=8)
    assert [(job.stage, job.status, job.error_code) for job in completed] == [
        (RECONCILE_IDENTITIES, "failed", "identity_resolution_failed")
    ]
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == RECONCILE_IDENTITIES))
        assert job is not None and job.retryable is False
        assert job.error_message == "player identity resolution failed"
        assert job.error_details_json == {}
        assert "private" not in str(job.output_json) and "secret" not in str(job.output_json)
