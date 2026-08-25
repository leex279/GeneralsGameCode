"""Read-only canonical player history query contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
    LongitudinalRun,
    ParserRun,
    Player,
    PlayerAlias,
    Replay,
    ReplayPlayer,
    Report,
)
from generals_replay_analyzer.identity.query import PlayerIndexQuery, PlayerProfileSelection, PlayerQueryService

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _id(value: int) -> str:
    return str(UUID(int=value))


def _seed(factory: sessionmaker[Session]) -> tuple[str, str]:
    with factory.begin() as session:
        replay = Replay(
            public_id=_id(1),
            sha256="a" * 64,
            replay_name="private.rep",
            version_string="1.04",
            version_number=104,
            frame_count=900,
            start_time=1_700_000_000,
            end_time=1_700_000_900,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="Tournament Desert",
            seed=4,
            header_json={"patch_identity": "1.04"},
            lifecycle_state="engine_verified",
            updated_at=NOW,
            created_at=NOW,
        )
        player = Player(public_id=_id(2), display_name="Leex279", identity_revision=3, updated_at=NOW, created_at=NOW)
        opponent = Player(public_id=_id(3), display_name="FOX27", identity_revision=1, updated_at=NOW, created_at=NOW)
        session.add_all((replay, player, opponent))
        session.flush()
        run = ParserRun(
            run_id=_id(4),
            replay_id=replay.id,
            parser_version="parser-v1",
            schema_version=1,
            input_sha256="a" * 64,
            result_sha256="b" * 64,
            status="running",
            completion_status="complete",
            command_stream_offset=10,
            end_offset=20,
            warnings_json=[],
            started_at=NOW,
            completed_at=NOW,
        )
        session.add(run)
        session.flush()
        session.add_all(
            (
                ReplayPlayer(
                    public_id=_id(5),
                    replay_id=replay.id,
                    parser_run_id=run.id,
                    player_id=player.id,
                    slot_index=0,
                    slot_kind="human",
                    original_name="LEEX279",
                    faction="USA",
                    start_position=1,
                    result="win",
                    observed_json={"subfaction": "USA"},
                ),
                ReplayPlayer(
                    public_id=_id(6),
                    replay_id=replay.id,
                    parser_run_id=run.id,
                    player_id=opponent.id,
                    slot_index=1,
                    slot_kind="human",
                    original_name="FOX27",
                    faction="GLA",
                    start_position=2,
                    result="loss",
                    observed_json={"subfaction": "GLA"},
                ),
                PlayerAlias(
                    public_id=_id(7),
                    player_id=player.id,
                    namespace="embedded_replay_name",
                    normalized_name="leex279",
                    original_name="LEEX279",
                    created_at=NOW,
                ),
            )
        )
        session.flush()
        run.status = "succeeded"
    return _id(2), _id(5)


def test_player_index_and_resolution_bind_current_revision_without_leaking_source_paths(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch mutable latest lookups or filename/provenance fields entering the public read model."""
    player_id, replay_player_id = _seed(identity_session_factory)
    service = PlayerQueryService(identity_session_factory)

    page = service.list_players(PlayerIndexQuery(search=" leex ", faction="USA"))
    assert [
        (item.player_public_id, item.display_name, item.identity_revision, item.match_count) for item in page.items
    ] == [(player_id, "Leex279", 3, 1)]
    resolution = service.resolve_profile(PlayerProfileSelection(player_public_id=player_id))
    assert resolution.state == "resolved"
    assert resolution.fixed_query is not None
    assert resolution.fixed_query.expected_identity_revision == 3
    profile = service.get_profile(resolution.fixed_query)
    assert profile.player.player_public_id == player_id
    assert profile.embedded_aliases[0].original_name == "LEEX279"
    assert profile.history[0].replay_player_public_id == replay_player_id
    assert profile.history[0].opponent_player_public_ids == (_id(3),)
    assert profile.history[0].opponent_factions == ("GLA",)
    assert "private.rep" not in repr(profile)


def test_historical_profile_query_rejects_a_revision_that_was_never_bound(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch a fixed profile silently re-resolving to a different current identity revision."""
    player_id, _ = _seed(identity_session_factory)
    service = PlayerQueryService(identity_session_factory)
    fixed = service.resolve_profile(PlayerProfileSelection(player_public_id=player_id)).fixed_query
    assert fixed is not None
    with identity_session_factory.begin() as session:
        player = session.query(Player).filter_by(public_id=player_id).one()
        player.identity_revision = 4
    profile = service.get_profile(fixed)
    assert profile.player.identity_revision == 3
    assert profile.availability.reason_codes == ("historical_identity_revision",)


def test_index_filters_and_unknown_profile_fail_closed(identity_session_factory: sessionmaker[Session]) -> None:
    """Catch opponent/patch filters being inferred in templates or unknown players becoming empty profiles."""
    player_id, _ = _seed(identity_session_factory)
    service = PlayerQueryService(identity_session_factory)
    filtered = service.list_players(
        PlayerIndexQuery(opponent_faction="GLA", patch="1.04", sort="recent_match", active_only=False)
    )
    assert [item.player_public_id for item in filtered.items] == [player_id]
    absent = service.list_players(PlayerIndexQuery(faction="China", sort="match_count"))
    assert absent.items == ()
    resolution = service.resolve_profile(PlayerProfileSelection(player_public_id=_id(999)))
    assert resolution.state == "unavailable"
    assert resolution.reason_codes == ("player_not_found",)


def test_profile_omits_external_alias_without_a_durable_attachment_operation(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch fabricated provider provenance when an alias row has no accepted operation."""
    player_id, _ = _seed(identity_session_factory)
    with identity_session_factory.begin() as session:
        player = session.query(Player).filter_by(public_id=player_id).one()
        session.add(
            PlayerAlias(
                public_id=_id(8),
                player_id=player.id,
                namespace="external:strata",
                normalized_name="subject-8",
                original_name="subject-8",
                external_subject="subject-8",
                created_at=NOW,
            )
        )

    service = PlayerQueryService(identity_session_factory)
    fixed = service.resolve_profile(PlayerProfileSelection(player_public_id=player_id)).fixed_query
    assert fixed is not None
    assert service.get_profile(fixed).provider_identities == ()


def test_fixed_profile_recomputes_query_definition_and_report_ownership_bindings(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch forged digests or an opponent report being accepted by a player fixed URL."""
    player_id, _ = _seed(identity_session_factory)
    with identity_session_factory.begin() as session:
        replay = session.query(Replay).one()
        opponent = session.query(ReplayPlayer).filter_by(public_id=_id(6)).one()
        session.add(
            Report(
                public_id=_id(8),
                replay_id=replay.id,
                replay_player_id=opponent.id,
                report_version="replay-report-v1",
                input_digest="8" * 64,
                cache_key="9" * 64,
                report_json={"schema_version": "replay-report-v1"},
                created_at=NOW,
            )
        )
    service = PlayerQueryService(identity_session_factory)
    fixed = service.resolve_profile(PlayerProfileSelection(player_public_id=player_id)).fixed_query
    assert fixed is not None

    with pytest.raises(ValueError, match="profile_input_digest_mismatch"):
        service.get_profile(replace(fixed, profile_input_digest="f" * 64))
    with pytest.raises(ValueError, match="definition_binding_digest_mismatch"):
        service.get_profile(replace(fixed, definition_binding_digest="f" * 64))
    with pytest.raises(ValueError, match="cross_player_report_binding"):
        service.get_profile(replace(fixed, report_public_ids=(_id(8),)))


def test_profile_binds_only_the_newest_report_for_each_replay_player(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch UUID ordering selecting an obsolete report after an analysis rerun."""
    player_id, replay_player_public_id = _seed(identity_session_factory)
    with identity_session_factory.begin() as session:
        replay = session.query(Replay).one()
        replay_player = session.query(ReplayPlayer).filter_by(public_id=replay_player_public_id).one()
        session.add_all(
            (
                Report(
                    public_id=_id(20),
                    replay_id=replay.id,
                    replay_player_id=replay_player.id,
                    report_version="replay-report-v1",
                    input_digest="8" * 64,
                    cache_key="9" * 64,
                    report_json={"schema_version": "replay-report-v1"},
                    created_at=NOW,
                ),
                Report(
                    public_id=_id(10),
                    replay_id=replay.id,
                    replay_player_id=replay_player.id,
                    report_version="replay-report-v2",
                    input_digest="a" * 64,
                    cache_key="b" * 64,
                    report_json={"schema_version": "replay-report-v2"},
                    created_at=NOW + timedelta(minutes=1),
                ),
            )
        )

    service = PlayerQueryService(identity_session_factory)
    fixed = service.resolve_profile(PlayerProfileSelection(player_public_id=player_id)).fixed_query
    assert fixed is not None
    assert fixed.report_public_ids == (_id(10),)
    report = service.get_profile(fixed).history[0].fixed_report
    assert report is not None
    assert report.report_public_id == _id(10)


def test_profile_dates_are_canonical_and_quality_policy_selects_only_exact_runs(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch aware datetimes crashing fixed digests or quality-policy filters being ignored."""
    player_id, _ = _seed(identity_session_factory)
    quality = {"quality_floor": "partial"}
    quality_digest = hashlib.sha256(json.dumps(quality, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with identity_session_factory.begin() as session:
        player = session.query(Player).filter_by(public_id=player_id).one()
        session.add(
            LongitudinalRun(
                run_id=_id(9),
                player_id=player.id,
                identity_revision=player.identity_revision,
                analyzer_name="longitudinal-player-analysis",
                analyzer_version="v1",
                segment_key_json={"schema_version": "longitudinal-segment-v1", "quality_policy": quality},
                settings_json={"definitions": [], "settings": {}},
                input_digest="a" * 64,
                cache_key="b" * 64,
                status="succeeded",
                created_at=NOW,
                completed_at=NOW,
            )
        )
    service = PlayerQueryService(identity_session_factory)
    matching = service.resolve_profile(
        PlayerProfileSelection(
            player_public_id=player_id,
            date_from_utc=datetime(2023, 1, 1, tzinfo=UTC),
            date_to_utc=datetime(2025, 1, 1, tzinfo=UTC),
            quality_policy_digest=quality_digest,
        )
    )
    assert matching.fixed_query is not None
    assert matching.fixed_query.longitudinal_run_ids == (_id(9),)
    rejected = service.resolve_profile(
        PlayerProfileSelection(player_public_id=player_id, quality_policy_digest="f" * 64)
    )
    assert rejected.fixed_query is not None
    assert rejected.fixed_query.longitudinal_run_ids == ()


def test_fixed_profile_rejects_mutable_alias_state_behind_an_old_url(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch an old fixed URL silently rereading current alias state."""
    player_id, _ = _seed(identity_session_factory)
    service = PlayerQueryService(identity_session_factory)
    fixed = service.resolve_profile(PlayerProfileSelection(player_public_id=player_id)).fixed_query
    assert fixed is not None
    with identity_session_factory.begin() as session:
        player = session.query(Player).filter_by(public_id=player_id).one()
        session.add(
            PlayerAlias(
                public_id=_id(10),
                player_id=player.id,
                namespace="embedded_replay_name",
                normalized_name="new-name",
                original_name="NEW NAME",
                created_at=NOW,
            )
        )
    with pytest.raises(ValueError, match="profile_input_digest_mismatch"):
        service.get_profile(fixed)
