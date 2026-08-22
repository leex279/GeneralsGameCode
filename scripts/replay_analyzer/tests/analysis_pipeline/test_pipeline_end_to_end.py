"""Migrated-SQLite execution proof for the centralized production pipeline."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.codecs import decode_feature_output, decode_llm_output
from generals_replay_analyzer.analysis_pipeline.composition import create_production_import_service
from generals_replay_analyzer.analysis_pipeline.planner import AnalysisPlanner
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import Feature, FeatureSet, Job, Replay
from generals_replay_analyzer.importing.service import ImportRequest
from generals_replay_analyzer.llm.provider import (
    CancellationSignal,
    JSONValue,
    OllamaClientConfig,
    ProviderError,
    TransportResponse,
)
from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.storage import ContentAddressedStore

PINNED_REPLAY = Path(__file__).parents[1] / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"


class UnavailableTransport:
    def __init__(self, config: OllamaClientConfig) -> None:
        self.client_config = config
        self.closed = False

    async def request(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        *,
        client_config: OllamaClientConfig,
        cancellation: CancellationSignal | None,
    ) -> TransportResponse:
        del method, path, payload, client_config, cancellation
        raise ProviderError("timeout")

    async def aclose(self) -> None:
        self.closed = True


def _job(session_factory: sessionmaker[Session], public_id: str) -> Job:
    with session_factory() as session:
        row = session.scalar(select(Job).where(Job.public_id == public_id))
        assert row is not None
        session.expunge(row)
        return row


def test_production_pipeline_executes_all_five_stages_with_opt_in_isolation(
    session_factory: sessionmaker[Session], clock: datetime, tmp_path: Path
) -> None:
    settings = AnalyzerSettings(data_root=tmp_path / "production-pipeline")
    settings.ensure_directories()
    transports: list[UnavailableTransport] = []

    def transport_factory(config: OllamaClientConfig) -> UnavailableTransport:
        transport = UnavailableTransport(config)
        transports.append(transport)
        return transport

    service = create_production_import_service(
        session_factory,
        settings,
        ContentAddressedStore(settings.managed_replay_directory),
        ContentAddressedStore(settings.cache_directory / "artifacts"),
        parser=parse_replay,
        telemetry_acquirer=None,
        clock=lambda: clock,
        parser_version="parser-v1",
        telemetry_acquirer_version="telemetry-v1",
        transport_factory=transport_factory,
    )
    service.submit(ImportRequest(PINNED_REPLAY))
    completed_stages: list[str] = []
    for _ in range(8):
        completed = service.run_available("pipeline-import", limit=1)
        assert completed
        completed_stages.append(completed[0].stage)
        if completed[0].stage == "import_observations":
            break
    assert completed_stages[-1] == "import_observations"

    with session_factory() as session:
        replay = session.scalar(select(Replay))
        assert replay is not None
        replay_public_id = replay.public_id

    planner = AnalysisPlanner(session_factory, clock=lambda: clock)
    deterministic = planner.ensure_analysis_plan(replay_public_id, False)
    assert deterministic.status == "planned"
    service.run_available("pipeline-opt-out", limit=32)

    assert transports == []
    assert deterministic.derive_job_public_id is not None
    assert deterministic.assess_job_public_id is not None
    assert deterministic.report_job_public_id is not None
    assert _job(session_factory, deterministic.derive_job_public_id).status == "succeeded"
    feature_selections = decode_feature_output(_job(session_factory, deterministic.derive_job_public_id).output_json)
    replay_wide = tuple(selection for selection in feature_selections if selection.replay_player_public_id is None)
    player_scoped = tuple(
        selection for selection in feature_selections if selection.replay_player_public_id is not None
    )
    assert len(replay_wide) == 1 and len(replay_wide[0].feature_set_public_ids) == 1
    assert player_scoped and all(len(selection.feature_set_public_ids) == 6 for selection in player_scoped)
    selected_feature_sets = {
        public_id for selection in feature_selections for public_id in selection.feature_set_public_ids
    }
    with session_factory() as session:
        spatial_sets = tuple(
            session.scalars(
                select(FeatureSet).where(
                    FeatureSet.public_id.in_(selected_feature_sets),
                    FeatureSet.extractor_name == "spatial",
                )
            )
        )
        assert len(spatial_sets) == len(feature_selections)
        spatial_names = set(
            session.scalars(
                select(Feature.name).where(Feature.feature_set_id.in_(tuple(item.id for item in spatial_sets)))
            )
        )
        assert "movement_density.sample_count_heatmap" in spatial_names
        assert "resource_control.observed_supply_collection_share" in spatial_names
    assessment_job = _job(session_factory, deterministic.assess_job_public_id)
    assert assessment_job.status == "succeeded", (
        assessment_job.error_code,
        assessment_job.error_message,
        assessment_job.error_details_json,
    )
    assert _job(session_factory, deterministic.report_job_public_id).status == "succeeded"

    inferred = planner.ensure_analysis_plan(replay_public_id, True)
    assert inferred.derive_job_public_id == deterministic.derive_job_public_id
    assert inferred.assess_job_public_id == deterministic.assess_job_public_id
    assert inferred.llm_job_public_id is not None
    assert inferred.report_job_public_id is not None
    service.run_available("pipeline-opt-in", limit=32)

    llm_job = _job(session_factory, inferred.llm_job_public_id)
    report_job = _job(session_factory, inferred.report_job_public_id)
    assert llm_job.status == "succeeded"
    assert report_job.status == "succeeded"
    assert transports and all(transport.closed for transport in transports)
    llm_selection = decode_llm_output(llm_job.output_json)
    assert all(item.llm_status == "unavailable" for item in llm_selection)
    report_rows = report_job.output_json["reports"]
    assert [row["analysis_run_id"] for row in report_rows] == [item.analysis_run_id for item in llm_selection]
