"""Production analysis handler boundaries and deterministic ordering."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
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
from generals_replay_analyzer.analysis_pipeline.identity_scope import (
    CanonicalPlayerIdentityBinding,
    IdentityAnalysisScope,
)
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import EvidenceItem, ParserRun, Player, Replay, ReplayPlayer
from generals_replay_analyzer.features.base import FeatureScope, FeatureValue, FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef
from generals_replay_analyzer.identity.audit import identity_cache_digest
from generals_replay_analyzer.importing.jobs import StageFailure
from generals_replay_analyzer.importing.service import StageDependencyOutput, StageExecutionContext
from generals_replay_analyzer.importing.stages import (
    ANALYZE_LLM,
    ANALYZE_LLM_VERSION,
    ASSESS_STRATEGIES,
    ASSESS_STRATEGIES_VERSION,
    DERIVE_FEATURES,
    DERIVE_FEATURES_VERSION,
    RENDER_REPORT,
    RENDER_REPORT_VERSION,
)
from generals_replay_analyzer.llm.evidence_bundle import build_evidence_bundle
from generals_replay_analyzer.llm.provider import OllamaClientConfig
from generals_replay_analyzer.llm.service import AnalysisOutcome, DeterministicFallback
from generals_replay_analyzer.longitudinal.segments import LongitudinalResultDTO

REPLAY_ID = "00000000-0000-4000-8000-000000000231"
REPLAY_SHA = "b" * 64
PARSER_RUN_ID = "00000000-0000-4000-8000-000000000232"


def _seed_players(
    session_factory: sessionmaker[Session],
    clock: datetime,
    *,
    extra_slots: tuple[tuple[int, str, str], ...] = (),
) -> tuple[str, str]:
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
        for slot_index, slot_kind, public_id in extra_slots:
            occupied = slot_kind in {"human", "ai"}
            session.add(
                ReplayPlayer(
                    public_id=public_id,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    player_id=None,
                    slot_index=slot_index,
                    slot_kind=slot_kind,
                    original_name=f"Player {slot_index}" if occupied else None,
                    normalized_name=f"player-{slot_index}" if occupied else None,
                    player_index=slot_index if occupied else None,
                    team_id=None,
                    faction="China" if occupied else None,
                    color=None,
                    start_position=slot_index if occupied else None,
                    result=None,
                    observed_json={},
                )
            )
        session.flush()
        parser.status = "succeeded"
    return tuple(sorted(players))  # type: ignore[return-value]


def _seed_unresolved_players(
    session_factory: sessionmaker[Session],
    clock: datetime,
    *,
    extra_slots: tuple[tuple[int, str, str], ...] = (),
) -> tuple[str, str]:
    players = (
        "00000000-0000-4000-8000-000000000246",
        "00000000-0000-4000-8000-000000000245",
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
            session.add(
                ReplayPlayer(
                    public_id=public_id,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    player_id=None,
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
        for slot_index, slot_kind, public_id in extra_slots:
            session.add(
                ReplayPlayer(
                    public_id=public_id,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    player_id=None,
                    slot_index=slot_index,
                    slot_kind=slot_kind,
                    original_name=None,
                    normalized_name=None,
                    player_index=None,
                    team_id=None,
                    faction=None,
                    color=None,
                    start_position=None,
                    result=None,
                    observed_json={},
                )
            )
        session.flush()
        parser.status = "succeeded"
    return tuple(sorted(players))  # type: ignore[return-value]


def _identity_scope(
    session_factory: sessionmaker[Session],
    replay_player_public_ids: tuple[str, ...],
    *,
    kind: str = "identity_invalidation",
) -> dict[str, object]:
    with session_factory() as session:
        rows = tuple(
            session.execute(
                select(
                    ReplayPlayer.public_id,
                    Player.public_id,
                    Player.identity_revision,
                )
                .join(Player, Player.id == ReplayPlayer.player_id)
                .where(ReplayPlayer.public_id.in_(replay_player_public_ids))
                .order_by(ReplayPlayer.public_id)
            )
        )
    return IdentityAnalysisScope(
        kind,  # type: ignore[arg-type]
        tuple(
            CanonicalPlayerIdentityBinding(
                replay_player_public_id,
                player_public_id,
                revision,
                identity_cache_digest(player_public_id, revision),
            )
            for replay_player_public_id, player_public_id, revision in rows
        ),
    ).to_json()


def _context(
    stage: str,
    dependency_stage: str,
    output: dict[str, object],
    *,
    input_value: dict[str, object] | None = None,
) -> StageExecutionContext:
    versions = {
        ANALYZE_LLM: ANALYZE_LLM_VERSION,
        ASSESS_STRATEGIES: ASSESS_STRATEGIES_VERSION,
        DERIVE_FEATURES: DERIVE_FEATURES_VERSION,
        RENDER_REPORT: RENDER_REPORT_VERSION,
    }
    return StageExecutionContext(
        str(uuid4()),
        "stage-idempotency-key",
        REPLAY_ID,
        REPLAY_SHA,
        stage,
        versions.get(stage, "1"),
        input_value or {},
        (
            StageDependencyOutput(
                str(uuid4()),
                dependency_stage,
                versions.get(dependency_stage, "1"),
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


def test_full_replay_derive_excludes_unoccupied_slots_but_keeps_every_occupied_parser_subject(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    unresolved_ai = "00000000-0000-4000-8000-000000000240"
    resolved = _seed_players(
        session_factory,
        clock,
        extra_slots=(
            (2, "closed", "00000000-0000-4000-8000-000000000243"),
            (3, "open", "00000000-0000-4000-8000-000000000244"),
            (4, "ai", unresolved_ai),
        ),
    )
    calls: list[str | None] = []

    class Features:
        def extract(self, request: object) -> tuple[object, ...]:
            replay_player_public_id = request.replay_player_public_id  # type: ignore[attr-defined]
            calls.append(replay_player_public_id)
            return (SimpleNamespace(feature_set_public_id=str(uuid4())),)

    output = DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
        _context(
            "derive_features",
            "import_observations",
            _observation_output(),
            input_value={
                "identity_scope": _identity_scope(session_factory, resolved, kind="full_replay"),
                "parser_run_id": PARSER_RUN_ID,
                "parser_version": "parser-v1",
            },
        )
    )

    expected = (None, unresolved_ai, *resolved)
    assert tuple(calls) == expected
    assert tuple(item.replay_player_public_id for item in decode_feature_output(output)) == expected


def test_identity_invalidation_derives_only_bound_players_and_skips_replay_wide_work(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    players = _seed_players(session_factory, clock)
    selected = (players[0],)
    calls: list[str | None] = []

    class Features:
        def extract(self, request: object) -> tuple[object, ...]:
            replay_player_public_id = request.replay_player_public_id  # type: ignore[attr-defined]
            calls.append(replay_player_public_id)
            return (
                SimpleNamespace(feature_set_public_id="00000000-0000-4000-8000-000000000299"),
            )

    output = DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
        _context(
            "derive_features",
            "import_observations",
            _observation_output(),
            input_value={
                "identity_scope": _identity_scope(session_factory, selected),
                "parser_run_id": PARSER_RUN_ID,
                "parser_version": "parser-v1",
            },
        )
    )

    assert calls == [players[0]]
    assert tuple(item.replay_player_public_id for item in decode_feature_output(output)) == selected


def test_assess_rejects_stale_identity_scope_before_any_derived_service_call(
    session_factory: sessionmaker[Session], clock: datetime, tmp_path: Path
) -> None:
    replay_player_public_id = _seed_players(session_factory, clock)[0]
    scope = _identity_scope(session_factory, (replay_player_public_id,))
    binding = scope["bindings"][0]  # type: ignore[index]
    called = False

    class Features:
        def extract(self, _request: object) -> tuple[object, ...]:
            nonlocal called
            called = True
            return ()

    with session_factory.begin() as session:
        player = session.scalar(
            select(Player).where(Player.public_id == binding["player_public_id"])  # type: ignore[index]
        )
        assert player is not None
        player.identity_revision += 1
    selected = PlayerFeatureSelection(
        replay_player_public_id,
        binding["player_public_id"],  # type: ignore[index, arg-type]
        ("00000000-0000-4000-8000-000000000298",),
    )

    with pytest.raises(StageFailure, match="identity scope changed"):
        AssessStrategiesHandler(  # type: ignore[arg-type]
            session_factory,
            AnalyzerSettings(data_root=tmp_path / "stale-identity"),
            Features(),
            SimpleNamespace(),
            SimpleNamespace(),
        )(
            _context(
                "assess_strategies",
                "derive_features",
                encode_feature_output((selected,)),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "parser-v1",
                },
            )
        )

    assert called is False


def test_handlers_reject_malformed_or_dependency_contradicting_identity_scopes(
    session_factory: sessionmaker[Session], clock: datetime, tmp_path: Path
) -> None:
    replay_players = _seed_players(session_factory, clock)
    with pytest.raises(StageFailure) as malformed:
        DeriveFeaturesHandler(session_factory, SimpleNamespace()).__call__(  # type: ignore[arg-type]
            _context(
                "derive_features",
                "import_observations",
                _observation_output(),
                input_value={"identity_scope": {"unknown": True}},
            )
        )
    assert malformed.value.code == "identity_scope_invalid"

    scope = _identity_scope(session_factory, (replay_players[0],))
    binding = scope["bindings"][0]  # type: ignore[index]
    with session_factory() as session:
        other_player_public_id = session.scalar(
            select(Player.public_id)
            .join(ReplayPlayer, ReplayPlayer.player_id == Player.id)
            .where(ReplayPlayer.public_id == replay_players[1])
        )
    assert other_player_public_id is not None
    contradiction = PlayerFeatureSelection(
        replay_players[0],
        other_player_public_id,
        ("00000000-0000-4000-8000-000000000297",),
    )
    called = False

    class Features:
        def extract(self, _request: object) -> tuple[object, ...]:
            nonlocal called
            called = True
            return ()

    with pytest.raises(StageFailure) as stale:
        AssessStrategiesHandler(  # type: ignore[arg-type]
            session_factory,
            AnalyzerSettings(data_root=tmp_path / "contradicting-identity"),
            Features(),
            SimpleNamespace(),
            SimpleNamespace(),
        )(
            _context(
                "assess_strategies",
                "derive_features",
                encode_feature_output((contradiction,)),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "parser-v1",
                },
            )
        )
    assert binding["player_public_id"] != other_player_public_id  # type: ignore[index]
    assert stale.value.code == "identity_scope_changed"
    assert called is False


def test_derive_rejects_changed_observation_parser_id_before_service_call(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    original_replay_player_id = _seed_players(session_factory, clock)[0]
    alternate_parser_run_id = "00000000-0000-4000-8000-000000000239"
    alternate_replay_player_id = "00000000-0000-4000-8000-000000000238"
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY_ID))
        original = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == original_replay_player_id)
        )
        assert replay is not None
        assert original is not None and original.player_id is not None
        alternate = ParserRun(
            run_id=alternate_parser_run_id,
            replay_id=replay.id,
            parser_version="alternate-v1",
            schema_version=1,
            input_sha256=REPLAY_SHA,
            result_sha256="d" * 64,
            status="running",
            completion_status="complete",
            command_stream_offset=0,
            end_offset=1,
            warnings_json=[],
            error_json=None,
            started_at=clock,
            completed_at=clock,
        )
        session.add(alternate)
        session.flush()
        session.add(
            ReplayPlayer(
                public_id=alternate_replay_player_id,
                replay_id=replay.id,
                parser_run_id=alternate.id,
                player_id=original.player_id,
                slot_index=9,
                slot_kind="human",
                original_name="Alternate Player",
                normalized_name="alternate player",
                player_index=9,
                team_id=None,
                faction="China",
                color=None,
                start_position=9,
                result=None,
                observed_json={},
            )
        )
        session.flush()
        alternate.status = "succeeded"
    scope = _identity_scope(session_factory, (alternate_replay_player_id,))
    observation = {**_observation_output(), "parser_run_id": alternate_parser_run_id}
    calls: list[object] = []

    class Features:
        def extract(self, request: object) -> tuple[object, ...]:
            calls.append(request)
            return (
                SimpleNamespace(
                    feature_set_public_id="00000000-0000-4000-8000-000000000298"
                ),
            )

    with pytest.raises(StageFailure) as failure:
        DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
            _context(
                "derive_features",
                "import_observations",
                observation,
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "parser-v1",
                },
            )
        )

    assert failure.value.code == "parser_run_not_authoritative"
    assert calls == []


def test_derive_rejects_wrong_planned_parser_version_before_service_call(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay_player_public_id = _seed_players(session_factory, clock)[0]
    scope = _identity_scope(session_factory, (replay_player_public_id,))
    calls: list[object] = []

    class Features:
        def extract(self, request: object) -> tuple[object, ...]:
            calls.append(request)
            return ()

    with pytest.raises(StageFailure) as failure:
        DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
            _context(
                "derive_features",
                "import_observations",
                _observation_output(),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "wrong-parser-v1",
                },
            )
        )

    assert failure.value.code == "parser_run_not_authoritative"
    assert calls == []


def test_assess_rejects_wrong_planned_parser_version_before_service_calls(
    session_factory: sessionmaker[Session], clock: datetime, tmp_path: Path
) -> None:
    replay_player_public_id = _seed_players(session_factory, clock)[0]
    scope = _identity_scope(session_factory, (replay_player_public_id,))
    with session_factory() as session:
        canonical_player_public_id = session.scalar(
            select(Player.public_id)
            .join(ReplayPlayer, ReplayPlayer.player_id == Player.id)
            .where(ReplayPlayer.public_id == replay_player_public_id)
        )
    assert canonical_player_public_id is not None
    feature_set_public_id = "00000000-0000-4000-8000-000000000297"
    selected = PlayerFeatureSelection(
        replay_player_public_id,
        canonical_player_public_id,
        (feature_set_public_id,),
    )
    calls: list[str] = []

    class Features:
        def extract(self, _request: object) -> tuple[object, ...]:
            calls.append("features")
            return (SimpleNamespace(feature_set_public_id=feature_set_public_id, features=()),)

    class Strategies:
        def assess_rule_candidates(self, *_args: object) -> object:
            calls.append("strategies")
            return SimpleNamespace(cache_key="d" * 64, assessments=(), derived_evidence=())

    class Longitudinal:
        def analyze(self, _request: object) -> object:
            calls.append("longitudinal")
            return SimpleNamespace(run_id="00000000-0000-4000-8000-000000000296", results=())

    with pytest.raises(StageFailure) as failure:
        AssessStrategiesHandler(  # type: ignore[arg-type]
            session_factory,
            AnalyzerSettings(data_root=tmp_path / "wrong-parser-version"),
            Features(),
            Strategies(),
            Longitudinal(),
        )(
            _context(
                "assess_strategies",
                "derive_features",
                encode_feature_output((selected,)),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "wrong-parser-v1",
                },
            )
        )

    assert failure.value.code == "parser_run_not_authoritative"
    assert calls == []


def test_full_scope_ignores_historical_parser_members_outside_bound_authority(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay_players = _seed_players(session_factory, clock)
    scope = _identity_scope(session_factory, replay_players, kind="full_replay")
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY_ID))
        player_id = session.scalar(
            select(Player.id)
            .join(ReplayPlayer, ReplayPlayer.player_id == Player.id)
            .where(ReplayPlayer.public_id == replay_players[0])
        )
        assert replay is not None and player_id is not None
        parser = ParserRun(
            run_id="00000000-0000-4000-8000-000000000238",
            replay_id=replay.id,
            parser_version="historical-v1",
            schema_version=1,
            input_sha256=REPLAY_SHA,
            result_sha256="e" * 64,
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
        session.add(
            ReplayPlayer(
                public_id="00000000-0000-4000-8000-000000000237",
                replay_id=replay.id,
                parser_run_id=parser.id,
                player_id=player_id,
                slot_index=0,
                slot_kind="human",
                original_name="Historical Player",
                normalized_name="historical player",
                player_index=0,
                team_id=None,
                faction="China",
                color=None,
                start_position=0,
                result=None,
                observed_json={},
            )
        )
        session.flush()
        parser.status = "succeeded"

    class Features:
        def extract(self, request: object) -> tuple[object, ...]:
            replay_player_public_id = request.replay_player_public_id  # type: ignore[attr-defined]
            base = 290 if replay_player_public_id is None else int(str(replay_player_public_id)[-3:])
            return (
                SimpleNamespace(feature_set_public_id=f"00000000-0000-4000-8000-{base:012d}"),
            )

    output = DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
        _context(
            "derive_features",
            "import_observations",
            _observation_output(),
            input_value={
                "identity_scope": scope,
                "parser_run_id": PARSER_RUN_ID,
                "parser_version": "parser-v1",
            },
        )
    )

    assert tuple(item.replay_player_public_id for item in decode_feature_output(output)) == (
        None,
        *replay_players,
    )


def test_full_scope_rejects_omitted_authoritative_canonical_member_before_extraction(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay_players = _seed_players(session_factory, clock)
    incomplete_scope = _identity_scope(
        session_factory,
        (replay_players[0],),
        kind="full_replay",
    )
    called = False

    class Features:
        def extract(self, _request: object) -> tuple[object, ...]:
            nonlocal called
            called = True
            return ()

    with pytest.raises(StageFailure) as failure:
        DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
            _context(
                "derive_features",
                "import_observations",
                _observation_output(),
                input_value={
                    "identity_scope": incomplete_scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "parser-v1",
                },
            )
        )

    assert failure.value.code == "identity_scope_changed"
    assert called is False


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
        {"parser_run_id": 7},
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


def test_derive_rejects_missing_selected_parser_run_after_replay_resolution(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    _seed_players(session_factory, clock)
    missing = {
        **_observation_output(),
        "parser_run_id": "00000000-0000-4000-8000-000000000299",
    }

    with pytest.raises(StageFailure) as failure:
        DeriveFeaturesHandler(session_factory, SimpleNamespace()).__call__(  # type: ignore[arg-type]
            _context("derive_features", "import_observations", missing)
        )

    assert failure.value.code == "parser_run_not_authoritative"


@pytest.mark.parametrize(
    "status,completion,completed",
    (("failed", "failed", True), ("running", "truncated", True), ("running", "complete", False)),
)
def test_derive_rejects_nonterminal_parser_run_before_feature_writes(
    session_factory: sessionmaker[Session],
    clock: datetime,
    status: str,
    completion: str,
    completed: bool,
) -> None:
    _seed_players(session_factory, clock)
    run_id = str(uuid4())
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY_ID))
        assert replay is not None
        session.add(
            ParserRun(
                run_id=run_id,
                replay_id=replay.id,
                parser_version="parser-v1",
                schema_version=1,
                input_sha256=REPLAY_SHA,
                result_sha256=None,
                status=status,
                completion_status=completion,
                command_stream_offset=None,
                end_offset=None,
                warnings_json=[],
                error_json=None,
                started_at=clock,
                completed_at=clock if completed else None,
            )
        )
    calls: list[object] = []

    class Features:
        def extract(self, request: object) -> tuple[object, ...]:
            calls.append(request)
            return ()

    output = _observation_output()
    output["parser_run_id"] = run_id
    with pytest.raises(StageFailure, match="parser run is not authoritative") as error:
        DeriveFeaturesHandler(session_factory, Features()).__call__(  # type: ignore[arg-type]
            _context("derive_features", "import_observations", output)
        )

    assert error.value.code == "parser_run_not_authoritative"
    assert calls == []


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
            return SimpleNamespace(cache_key="d" * 64, assessments=(), derived_evidence=())

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


def test_assess_requests_the_complete_fixed_production_longitudinal_output_set(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    replay_player_id = "00000000-0000-4000-8000-000000000252"
    canonical_player_id = "00000000-0000-4000-8000-000000000253"
    feature_set_ids = tuple(
        f"00000000-0000-4000-8000-{value:012d}" for value in range(286, 292)
    )
    selected = PlayerFeatureSelection(replay_player_id, canonical_player_id, feature_set_ids)
    requests: list[object] = []

    class Features:
        def extract(self, _request: object) -> tuple[object, ...]:
            return tuple(
                SimpleNamespace(feature_set_public_id=public_id, features=())
                for public_id in feature_set_ids
            )

    class Strategies:
        def assess_rule_candidates(self, *_args: object) -> object:
            return SimpleNamespace(cache_key="d" * 64, assessments=(), derived_evidence=())

    class Longitudinal:
        def analyze(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(
                run_id="00000000-0000-4000-8000-000000000294",
                results=(),
            )

    output = decode_assessment_output(
        AssessStrategiesHandler(  # type: ignore[arg-type]
            session_factory,
            AnalyzerSettings(data_root=tmp_path / "complete-longitudinal-set"),
            Features(),
            Strategies(),
            Longitudinal(),
        )(
            _context(
                "assess_strategies",
                "derive_features",
                encode_feature_output((selected,)),
            )
        )
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.metric_names == ("economy.cash_change_total",)  # type: ignore[attr-defined]
    assert request.pattern_names == (  # type: ignore[attr-defined]
        "change_point.economy_cash_change_total",
        "consistency.economy_cash_change_total",
        "map_position_habits",
        "opponent_associated.economy_cash_change_total",
        "personal_baseline.economy_cash_change_total",
        "recurring_opening",
        "timing_band.build_first_completed",
        "transition_preferences",
        "trend.economy_cash_change_total",
    )
    assert request.settings.enabled_metrics == request.metric_names  # type: ignore[attr-defined]
    assert request.settings.enabled_patterns == request.pattern_names  # type: ignore[attr-defined]
    assert output[0].longitudinal_status == "succeeded"


def test_assess_namespaces_longitudinal_claim_ids_that_collide_with_feature_names(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    clock: datetime,
) -> None:
    replay_player_ids = _seed_players(session_factory, clock)
    replay_player_id = replay_player_ids[0]
    evidence_public_id = "00000000-0000-4000-8000-000000000295"
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY_ID))
        replay_player = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == replay_player_id)
        )
        assert replay is not None and replay_player is not None and replay_player.player_id is not None
        canonical_player = session.get(Player, replay_player.player_id)
        assert canonical_player is not None
        canonical_player_id = canonical_player.public_id
        session.add(
            EvidenceItem(
                public_id=evidence_public_id,
                replay_id=replay.id,
                parser_run_id=None,
                telemetry_run_id=None,
                tier="derived",
                source_kind="longitudinal_corpus",
                source_key="longitudinal:economy-cash-change-total",
                schema_version=1,
                created_at=clock,
            )
        )
    feature_set_id = "00000000-0000-4000-8000-000000000296"
    feature = FeatureValue(
        name="economy.cash_change_total",
        value_type="integer",
        raw_value=-300,
        unit="credits",
        scope=FeatureScope("player", replay_player_id, replay_player_public_id=replay_player_id),
        window=FeatureWindow(0, 20),
        quality="complete",
        quality_reason=None,
        input_evidence=(
            EvidenceRef(
                "00000000-0000-4000-8000-000000000297",
                "observed",
                "telemetry_event",
                "observed-evidence:telemetry_event:v1:sequence-8",
                "2",
            ),
        ),
    )

    class Features:
        def extract(self, _request: object) -> tuple[object, ...]:
            return (SimpleNamespace(feature_set_public_id=feature_set_id, features=(feature,)),)

    class Strategies:
        def assess_rule_candidates(self, *_args: object) -> object:
            return SimpleNamespace(cache_key="d" * 64, assessments=(), derived_evidence=())

    class Longitudinal:
        def analyze(self, _request: object) -> object:
            return SimpleNamespace(
                run_id="00000000-0000-4000-8000-000000000298",
                results=(
                    LongitudinalResultDTO(
                        public_id="00000000-0000-4000-8000-000000000299",
                        result_name="economy.cash_change_total",
                        result_kind="metric",
                        sample_count=1,
                        missing_count=0,
                        quality="complete",
                        reason=None,
                        statistics={"median": -300.0},
                        members=(),
                        evidence_public_id=evidence_public_id,
                    ),
                ),
            )

    output = decode_assessment_output(
        AssessStrategiesHandler(  # type: ignore[arg-type]
            session_factory,
            AnalyzerSettings(
                data_root=tmp_path / "longitudinal-claim-collision",
                minimum_longitudinal_sample_size=1,
            ),
            Features(),
            Strategies(),
            Longitudinal(),
        )(
            _context(
                "assess_strategies",
                "derive_features",
                encode_feature_output(
                    (
                        PlayerFeatureSelection(
                            replay_player_id,
                            canonical_player_id,
                            (feature_set_id,),
                        ),
                    )
                ),
            )
        )
    )

    assert tuple(claim.claim_id for claim in output[0].evidence_bundle.claims) == (
        "economy.cash_change_total",
        "longitudinal:economy.cash_change_total",
    )


def test_assess_uses_derived_feature_citation_when_raw_inputs_exceed_provider_cap() -> None:
    observed = tuple(
        EvidenceRef(
            f"00000000-0000-4000-8000-{index:012d}",
            "observed",
            "telemetry_event",
            f"observed-evidence:telemetry_event:v2:sequence-{index}",
            "telemetry_event-v2",
        )
        for index in range(1, 34)
    )
    derived = EvidenceRef(
        "00000000-0000-4000-8000-000000000034",
        "derived",
        "feature",
        "feature:economy-cash-change-total",
        "feature-v1",
    )
    feature = FeatureValue(
        name="economy.cash_change_total",
        value_type="integer",
        raw_value=10000,
        unit="credits",
        scope=FeatureScope("replay", REPLAY_ID),
        window=FeatureWindow(0, 100),
        quality="complete",
        quality_reason=None,
        input_evidence=observed,
    )

    claims = AssessStrategiesHandler._claims(
        (SimpleNamespace(features=(feature,), derived_evidence=(derived,)),),
        (),
        (),
    )

    assert len(claims) == 1
    assert claims[0].evidence_ids == (derived.public_id,)


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


def test_analyze_llm_requires_explicit_job_opt_in_before_dependency_use(tmp_path: Path) -> None:
    handler = AnalyzeLLMHandler(  # type: ignore[arg-type]
        AnalyzerSettings(data_root=tmp_path / "llm-not-opted-in"),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    with pytest.raises(StageFailure) as failure:
        handler(_context("analyze_llm", "assess_strategies", {}, input_value={}))

    assert failure.value.code == "ollama_not_opted_in"


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


def test_render_report_supports_direct_deterministic_assessment_without_ollama() -> None:
    assessment = PlayerAssessmentSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000283",),
        "f" * 64,
        "not_applicable",
        None,
        build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=()),
    )
    requests: list[object] = []

    class Reports:
        def create(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(structured_asset=None, presentation_bundle_asset=None)

    RenderReportHandler(Reports()).__call__(  # type: ignore[arg-type]
        _context(
            "render_report",
            "assess_strategies",
            encode_assessment_output((assessment,)),
        )
    )

    assert len(requests) == 1
    assert requests[0].analysis_run_id is None  # type: ignore[attr-defined]


def test_render_report_rejects_stale_identity_revision_before_publication(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay_player_ids = _seed_players(session_factory, clock)
    selected_id = replay_player_ids[0]
    scope = _identity_scope(session_factory, (selected_id,))
    with session_factory.begin() as session:
        row = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == selected_id)
        )
        assert row is not None and row.player_id is not None
        canonical = session.get(Player, row.player_id)
        assert canonical is not None
        canonical_id = canonical.public_id
        canonical.identity_revision += 1
    assessment = PlayerAssessmentSelection(
        selected_id,
        canonical_id,
        ("00000000-0000-4000-8000-000000000284",),
        "f" * 64,
        "unavailable",
        None,
        build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=()),
    )
    requests: list[object] = []
    reports = SimpleNamespace(create=lambda request: requests.append(request))

    with pytest.raises(StageFailure, match="identity scope changed") as error:
        RenderReportHandler(reports, session_factory=session_factory)(
            _context(
                "render_report",
                "assess_strategies",
                encode_assessment_output((assessment,)),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "parser-v1",
                },
            )
        )

    assert error.value.code == "identity_scope_changed"
    assert requests == []


def test_render_report_rejects_unrelated_assessment_subject_before_publication(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay_player_ids = _seed_players(session_factory, clock)
    selected_id, unrelated_id = replay_player_ids
    scope = _identity_scope(session_factory, (selected_id,))
    with session_factory() as session:
        unrelated = session.execute(
            select(ReplayPlayer, Player)
            .join(Player, Player.id == ReplayPlayer.player_id)
            .where(ReplayPlayer.public_id == unrelated_id)
        ).one()
        _, canonical = unrelated
    assessment = PlayerAssessmentSelection(
        unrelated_id,
        canonical.public_id,
        ("00000000-0000-4000-8000-000000000285",),
        "f" * 64,
        "unavailable",
        None,
        build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=()),
    )
    requests: list[object] = []
    reports = SimpleNamespace(create=lambda request: requests.append(request))

    with pytest.raises(StageFailure, match="assessment subjects do not match") as error:
        RenderReportHandler(reports, session_factory=session_factory)(
            _context(
                "render_report",
                "assess_strategies",
                encode_assessment_output((assessment,)),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "parser-v1",
                },
            )
        )

    assert error.value.code == "identity_scope_changed"
    assert requests == []


def test_render_report_rejects_unrelated_llm_subject_before_publication(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    selected_id, unrelated_id = _seed_players(session_factory, clock)
    scope = _identity_scope(session_factory, (selected_id,))
    selected = PlayerLLMSelection(
        unrelated_id,
        "00000000-0000-4000-8000-000000000295",
        "unavailable",
        "model_unavailable",
    )
    requests: list[object] = []
    reports = SimpleNamespace(create=lambda request: requests.append(request))

    with pytest.raises(StageFailure, match="LLM subjects do not match") as error:
        RenderReportHandler(reports, session_factory=session_factory)(
            _context(
                "render_report",
                "analyze_llm",
                encode_llm_output((selected,)),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "parser-v1",
                },
            )
        )

    assert error.value.code == "identity_scope_changed"
    assert requests == []


def test_render_report_publishes_only_the_exact_current_scoped_subject(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    selected_id = _seed_players(session_factory, clock)[0]
    scope = _identity_scope(session_factory, (selected_id,))
    with session_factory() as session:
        canonical_id = session.scalar(
            select(Player.public_id)
            .join(ReplayPlayer, ReplayPlayer.player_id == Player.id)
            .where(ReplayPlayer.public_id == selected_id)
        )
        assert canonical_id is not None
    assessment = PlayerAssessmentSelection(
        selected_id,
        canonical_id,
        ("00000000-0000-4000-8000-000000000296",),
        "f" * 64,
        "unavailable",
        None,
        build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=()),
    )
    requests: list[object] = []

    class Reports:
        def create(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(structured_asset=None, presentation_bundle_asset=None)

    RenderReportHandler(Reports(), session_factory=session_factory)(  # type: ignore[arg-type]
        _context(
            "render_report",
            "assess_strategies",
            encode_assessment_output((assessment,)),
            input_value={
                "identity_scope": scope,
                "parser_run_id": PARSER_RUN_ID,
                "parser_version": "parser-v1",
            },
        )
    )

    assert len(requests) == 1
    assert requests[0].replay_player_public_id == selected_id  # type: ignore[attr-defined]


def test_render_report_rejects_wrong_planned_parser_version_before_publication(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    selected_id = _seed_players(session_factory, clock)[0]
    scope = _identity_scope(session_factory, (selected_id,))
    with session_factory() as session:
        canonical_id = session.scalar(
            select(Player.public_id)
            .join(ReplayPlayer, ReplayPlayer.player_id == Player.id)
            .where(ReplayPlayer.public_id == selected_id)
        )
    assert canonical_id is not None
    assessment = PlayerAssessmentSelection(
        selected_id,
        canonical_id,
        ("00000000-0000-4000-8000-000000000294",),
        "f" * 64,
        "unavailable",
        None,
        build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=()),
    )
    requests: list[object] = []
    reports = SimpleNamespace(create=lambda request: requests.append(request))

    with pytest.raises(StageFailure) as failure:
        RenderReportHandler(reports, session_factory=session_factory)(
            _context(
                "render_report",
                "assess_strategies",
                encode_assessment_output((assessment,)),
                input_value={
                    "identity_scope": scope,
                    "parser_run_id": PARSER_RUN_ID,
                    "parser_version": "wrong-parser-v1",
                },
            )
        )

    assert failure.value.code == "parser_run_not_authoritative"
    assert requests == []


def test_render_report_accepts_exact_unresolved_subjects_from_the_planned_parser_run(
    session_factory: sessionmaker[Session], clock: datetime
) -> None:
    replay_player_ids = _seed_unresolved_players(
        session_factory,
        clock,
        extra_slots=(
            (2, "closed", "00000000-0000-4000-8000-000000000247"),
            (3, "open", "00000000-0000-4000-8000-000000000248"),
        ),
    )
    scope = IdentityAnalysisScope("full_replay", ()).to_json()
    subjects = (
        PlayerAssessmentSelection(
            None,
            None,
            ("00000000-0000-4000-8000-000000000297",),
            "f" * 64,
            "not_applicable",
            None,
            build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=()),
        ),
        *tuple(
            PlayerAssessmentSelection(
                replay_player_id,
                None,
                (f"00000000-0000-4000-8000-{298 + index:012d}",),
                "f" * 64,
                "canonical_player_unresolved",
                None,
                build_evidence_bundle(
                    replay_public_id=REPLAY_ID,
                    replay_sha256=REPLAY_SHA,
                    claims=(),
                ),
            )
            for index, replay_player_id in enumerate(replay_player_ids)
        ),
    )
    requests: list[object] = []

    class Reports:
        def create(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(structured_asset=None, presentation_bundle_asset=None)

    RenderReportHandler(Reports(), session_factory=session_factory)(  # type: ignore[arg-type]
        _context(
            "render_report",
            "assess_strategies",
            encode_assessment_output(subjects),
            input_value={
                "identity_scope": scope,
                "parser_run_id": PARSER_RUN_ID,
                "parser_version": "parser-v1",
            },
        )
    )

    assert tuple(request.replay_player_public_id for request in requests) == (  # type: ignore[attr-defined]
        None,
        *replay_player_ids,
    )


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
        dependencies=(replace(valid.dependencies[0], component_version="999"),),
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
        dependencies=(replace(context.dependencies[0], component_version="999"),),
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
