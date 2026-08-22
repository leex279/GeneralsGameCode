"""Production analysis handler boundaries and deterministic ordering."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.codecs import (
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
from generals_replay_analyzer.analysis_pipeline.handlers import (
    PRODUCTION_EXTRACTOR_NAMES,
    AnalyzeLLMHandler,
    AssessStrategiesHandler,
    DeriveFeaturesHandler,
    RenderReportHandler,
)
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import ParserRun, Player, Replay, ReplayPlayer
from generals_replay_analyzer.importing.jobs import StageFailure
from generals_replay_analyzer.importing.service import StageDependencyOutput, StageExecutionContext
from generals_replay_analyzer.llm.evidence_bundle import build_evidence_bundle
from generals_replay_analyzer.llm.provider import OllamaClientConfig
from generals_replay_analyzer.llm.service import AnalysisOutcome, DeterministicFallback

REPLAY_ID = "00000000-0000-4000-8000-000000000231"
REPLAY_SHA = "b" * 64
PARSER_RUN_ID = "00000000-0000-4000-8000-000000000232"


def _seed_players(session_factory: sessionmaker[Session], clock: datetime) -> tuple[str, str]:
    players = (
        "00000000-0000-4000-8000-000000000242",
        "00000000-0000-4000-8000-000000000241",
    )
    with session_factory.begin() as session:
        replay = Replay(
            public_id=REPLAY_ID,
            sha256=REPLAY_SHA,
            replay_name="pipeline.rep",
            version_string="1.04",
            version_number=104,
            frame_count=10,
            start_time=0,
            end_time=10,
            exe_crc=0,
            ini_crc=0,
            map_crc=0,
            map_name="maps/test.map",
            seed=1,
            starting_cash=10000,
            header_json={},
            lifecycle_state="engine_verified",
            created_at=clock,
            updated_at=clock,
        )
        session.add(replay)
        session.flush()
        parser = ParserRun(
            run_id=PARSER_RUN_ID,
            replay_id=replay.id,
            parser_version="parser-v1",
            schema_version=1,
            input_sha256=REPLAY_SHA,
            result_sha256="c" * 64,
            status="running",
            completion_status="complete",
            command_stream_offset=0,
            end_offset=1,
            warnings_json=[],
            error_json=None,
            started_at=clock,
            completed_at=clock,
        )
        session.add(parser)
        session.flush()
        for index, public_id in enumerate(players):
            canonical = Player(
                public_id=str(uuid4()),
                display_name=f"Player {index}",
                identity_revision=0,
                created_at=clock,
                updated_at=clock,
            )
            session.add(canonical)
            session.flush()
            session.add(
                ReplayPlayer(
                    public_id=public_id,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    player_id=canonical.id,
                    slot_index=index,
                    slot_kind="human",
                    original_name=f"Player {index}",
                    normalized_name=f"player-{index}",
                    player_index=index,
                    team_id=None,
                    faction="China",
                    color=None,
                    start_position=index,
                    result=None,
                    observed_json={},
                )
            )
        session.flush()
        parser.status = "succeeded"
    return tuple(sorted(players))  # type: ignore[return-value]


def _context(
    stage: str,
    dependency_stage: str,
    output: dict[str, object],
    *,
    input_value: dict[str, object] | None = None,
) -> StageExecutionContext:
    return StageExecutionContext(
        str(uuid4()),
        "stage-idempotency-key",
        REPLAY_ID,
        REPLAY_SHA,
        stage,
        "1",
        input_value or {},
        (
            StageDependencyOutput(
                str(uuid4()),
                dependency_stage,
                "1",
                output,
            ),
        ),
    )


def _observation_output() -> dict[str, object]:
    return {
        "idempotency_key": "observation-key",
        "parser_run_id": PARSER_RUN_ID,
        "parser_command_count": 0,
        "telemetry_run_id": None,
        "telemetry_event_count": 0,
    }


def test_derive_features_iterates_players_in_public_id_order(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    expected = _seed_players(session_factory, clock)
    calls: list[tuple[str | None, tuple[str, ...]]] = []

    class Features:
        def extract(self, request: object) -> tuple[object, ...]:
            player_id = request.replay_player_public_id  # type: ignore[attr-defined]
            extractor_names = request.extractor_names  # type: ignore[attr-defined]
            calls.append((player_id, extractor_names))
            base = int(str(player_id)[-3:]) if player_id is not None else 0
            return tuple(
                SimpleNamespace(feature_set_public_id=f"00000000-0000-4000-8000-{base + offset:012d}")
                for offset in range(1, len(extractor_names) + 1)
            )

    output = DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
        _context("derive_features", "import_observations", _observation_output())
    )

    assert tuple(calls) == (
        (None, ("spatial",)),
        *((public_id, PRODUCTION_EXTRACTOR_NAMES) for public_id in expected),
    )
    assert tuple(item.replay_player_public_id for item in decode_feature_output(output)) == (None, *expected)


def test_derive_features_rejects_corrupted_observation_output_nonretryably(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    _seed_players(session_factory, clock)
    damaged = {**_observation_output(), "source_path": "C:\\private\\replay.rep"}

    with pytest.raises(StageFailure) as failure:
        DeriveFeaturesHandler(session_factory, SimpleNamespace()).__call__(  # type: ignore[arg-type]
            _context("derive_features", "import_observations", damaged)
        )

    assert failure.value.retryable is False


@pytest.mark.parametrize(
    "damage",
    [
        {"parser_run_id": "AAAAAAAA-0000-4000-8000-000000000232"},
        {"idempotency_key": "C:\\private\\observation"},
        {"telemetry_run_id": "not-a-uuid"},
    ],
)
def test_derive_features_rejects_noncanonical_observation_identity(
    session_factory: sessionmaker[Session], damage: dict[str, object]
) -> None:
    failure = None
    try:
        DeriveFeaturesHandler(session_factory, SimpleNamespace()).__call__(  # type: ignore[arg-type]
            _context("derive_features", "import_observations", {**_observation_output(), **damage})
        )
    except StageFailure as error:
        failure = error

    assert failure is not None
    assert failure.code == "dependency_contract_invalid"
    assert failure.retryable is False


def test_assess_marks_missing_canonical_player_explicitly(tmp_path: Path) -> None:
    player = PlayerFeatureSelection(
        "00000000-0000-4000-8000-000000000251",
        None,
        tuple(f"00000000-0000-4000-8000-{value:012d}" for value in range(261, 266)),
    )

    class Features:
        def extract(self, _request: object) -> tuple[object, ...]:
            return tuple(
                SimpleNamespace(feature_set_public_id=public_id, features=())
                for public_id in player.feature_set_public_ids
            )

    class Strategies:
        def assess_rule_candidates(self, *_args: object) -> object:
            return SimpleNamespace(cache_key="d" * 64, assessments=())

    class Longitudinal:
        def analyze(self, _request: object) -> object:
            raise AssertionError("unresolved canonical players must not reach longitudinal analysis")

    settings = AnalyzerSettings(data_root=tmp_path / "handler-settings")
    handler = AssessStrategiesHandler(  # type: ignore[arg-type]
        SimpleNamespace(),
        settings,
        Features(),
        Strategies(),
        Longitudinal(),
    )
    output = handler(
        _context(
            "assess_strategies",
            "derive_features",
            encode_feature_output((player,)),
        )
    )

    assert decode_assessment_output(output)[0].longitudinal_status == "canonical_player_unresolved"


def test_analyze_llm_closes_transport_and_returns_successful_unavailable_fallback(
    tmp_path: Path,
) -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    selected = PlayerAssessmentSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000271",),
        "e" * 64,
        "not_applicable",
        None,
        bundle,
    )
    closed = False

    class Transport:
        def __init__(self, config: OllamaClientConfig) -> None:
            self.client_config = config

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    class Service:
        async def analyze(self, _request: object, _cancellation: object) -> AnalysisOutcome:
            return AnalysisOutcome(
                "00000000-0000-4000-8000-000000000272",
                "unavailable",
                "model_unavailable",
                False,
                DeterministicFallback(()),
            )

    settings = AnalyzerSettings(data_root=tmp_path / "llm-settings")
    handler = AnalyzeLLMHandler(  # type: ignore[arg-type]
        settings,
        Transport,
        lambda _transport: Service(),
    )
    output = handler(
        _context(
            "analyze_llm",
            "assess_strategies",
            encode_assessment_output((selected,)),
            input_value={"allow_ollama": True},
        )
    )

    assert closed is True
    assert decode_llm_output(output) == (
        PlayerLLMSelection(None, "00000000-0000-4000-8000-000000000272", "unavailable", "model_unavailable"),
    )


def test_analyze_llm_closes_transport_when_service_construction_fails(tmp_path: Path) -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    selected = PlayerAssessmentSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000273",),
        "e" * 64,
        "not_applicable",
        None,
        bundle,
    )
    closed = False

    class Transport:
        def __init__(self, config: OllamaClientConfig) -> None:
            self.client_config = config

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    handler = AnalyzeLLMHandler(  # type: ignore[arg-type]
        AnalyzerSettings(data_root=tmp_path / "llm-construction-failure"),
        Transport,
        lambda _transport: (_ for _ in ()).throw(RuntimeError("construction failed")),
    )
    failure = None
    try:
        handler(
            _context(
                "analyze_llm",
                "assess_strategies",
                encode_assessment_output((selected,)),
                input_value={"allow_ollama": True},
            )
        )
    except StageFailure as error:
        failure = error

    assert failure is not None
    assert failure.retryable is False
    assert closed is True


def test_render_report_passes_exact_direct_analysis_run() -> None:
    selected = PlayerLLMSelection(
        "00000000-0000-4000-8000-000000000281",
        "00000000-0000-4000-8000-000000000282",
        "unavailable",
        "model_unavailable",
    )
    requests: list[object] = []

    class Reports:
        def create(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(structured_asset=None, presentation_bundle_asset=None)

    RenderReportHandler(Reports()).__call__(  # type: ignore[arg-type]
        _context("render_report", "analyze_llm", encode_llm_output((selected,)))
    )

    assert len(requests) == 1
    assert requests[0].analysis_run_id == selected.analysis_run_id  # type: ignore[attr-defined]


def test_handlers_reject_wrong_context_missing_dependency_and_missing_replay(
    session_factory: sessionmaker[Session],
) -> None:
    valid = _context("derive_features", "import_observations", _observation_output())
    failures: list[StageFailure] = []
    for context in (
        replace(valid, stage="assess_strategies"),
        replace(valid, dependencies=()),
        valid,
    ):
        try:
            DeriveFeaturesHandler(session_factory, SimpleNamespace()).__call__(  # type: ignore[arg-type]
                context
            )
        except StageFailure as error:
            failures.append(error)

    assert [failure.code for failure in failures] == [
        "stage_context_invalid",
        "dependency_contract_invalid",
        "derive_features_failed",
    ]


def test_assess_rejects_feature_cache_identity_change(tmp_path: Path) -> None:
    selected = PlayerFeatureSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000291",),
    )

    class ChangedFeatures:
        def extract(self, _request: object) -> tuple[object, ...]:
            return (
                SimpleNamespace(
                    feature_set_public_id="00000000-0000-4000-8000-000000000292",
                    features=(),
                ),
            )

    failure = None
    try:
        AssessStrategiesHandler(  # type: ignore[arg-type]
            SimpleNamespace(),
            AnalyzerSettings(data_root=tmp_path / "changed-features"),
            ChangedFeatures(),
            SimpleNamespace(),
            SimpleNamespace(),
        )(
            _context(
                "assess_strategies",
                "derive_features",
                encode_feature_output((selected,)),
            )
        )
    except StageFailure as error:
        failure = error

    assert failure is not None
    assert failure.code == "assess_strategies_failed"


def test_assess_rejects_cross_field_dependency_contradiction_nonretryably(tmp_path: Path) -> None:
    malformed = {
        "schema_version": "feature-output-v1",
        "players": [
            {
                "replay_player_public_id": None,
                "canonical_player_public_id": "00000000-0000-4000-8000-000000000221",
                "feature_set_public_ids": ["00000000-0000-4000-8000-000000000211"],
            }
        ],
    }
    failure = None
    try:
        AssessStrategiesHandler(  # type: ignore[arg-type]
            SimpleNamespace(),
            AnalyzerSettings(data_root=tmp_path / "contradictory-features"),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )(_context("assess_strategies", "derive_features", malformed))
    except StageFailure as error:
        failure = error

    assert failure is not None
    assert failure.retryable is False
    assert failure.code == "assess_strategies_failed"
    assert isinstance(failure.__cause__, PipelineCodecError)


def test_render_report_rejects_unknown_and_malformed_direct_dependencies() -> None:
    reports = SimpleNamespace(create=lambda _request: None)
    invalid_stage = _context("render_report", "derive_features", {"schema_version": "x"})
    assessment = PlayerAssessmentSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000293",),
        "f" * 64,
        "not_applicable",
        None,
        build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=()),
    )
    valid = _context(
        "render_report",
        "assess_strategies",
        encode_assessment_output((assessment,)),
    )
    wrong_version = replace(
        valid,
        dependencies=(replace(valid.dependencies[0], component_version="2"),),
    )
    failures: list[StageFailure] = []
    no_dependencies = replace(valid, dependencies=())
    for context in (invalid_stage, wrong_version, no_dependencies):
        try:
            RenderReportHandler(reports).__call__(context)  # type: ignore[arg-type]
        except StageFailure as error:
            failures.append(error)

    assert [failure.code for failure in failures] == [
        "render_report_failed",
        "dependency_contract_invalid",
        "render_report_failed",
    ]


def test_analyze_llm_preserves_direct_dependency_contract_failure(tmp_path: Path) -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    assessment = PlayerAssessmentSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000294",),
        "f" * 64,
        "not_applicable",
        None,
        bundle,
    )
    context = _context(
        "analyze_llm",
        "assess_strategies",
        encode_assessment_output((assessment,)),
        input_value={"allow_ollama": True},
    )
    context = replace(
        context,
        dependencies=(replace(context.dependencies[0], component_version="2"),),
    )
    failure = None
    try:
        AnalyzeLLMHandler(  # type: ignore[arg-type]
            AnalyzerSettings(data_root=tmp_path / "invalid-llm-dependency"),
            SimpleNamespace(),
            SimpleNamespace(),
        )(context)
    except StageFailure as error:
        failure = error

    assert failure is not None
    assert failure.code == "dependency_contract_invalid"
