"""Production replay-library and configured-root adapter tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    EvidenceItem,
    Job,
    ManagedAsset,
    Map,
    ParserRun,
    Player,
    Replay,
    ReplayPlayer,
    Report,
    Source,
    StrategyAssessment,
)
from generals_replay_analyzer.watching import SnapshotIngressError, WatchedRootRegistry
from generals_replay_analyzer.web.adapters.library import AnalyticsLibraryAdapter
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import ReplayLibraryQueryDTO, RootImportCommandDTO

REPLAY_ALPHA = "123e4567-e89b-42d3-a456-426614174100"
REPLAY_BETA = "123e4567-e89b-42d3-a456-426614174101"
PLAYER_LEEX = "123e4567-e89b-42d3-a456-426614174110"
PLAYER_FOX = "123e4567-e89b-42d3-a456-426614174111"
MAP_ID = "123e4567-e89b-42d3-a456-426614174120"
SOURCE_ALPHA = "123e4567-e89b-42d3-a456-426614174130"
REPORT_OLD = "123e4567-e89b-42d3-a456-426614174140"
REPORT_FIXED = "123e4567-e89b-42d3-a456-426614174141"
PLAYER_REPORT = "123e4567-e89b-42d3-a456-426614174142"
EVIDENCE_OBSERVED = "123e4567-e89b-42d3-a456-426614174150"
EVIDENCE_DERIVED = "123e4567-e89b-42d3-a456-426614174151"


class _NoopImportService:
    def submit_verified(self, _request: object) -> object:
        raise AssertionError("read projections must not submit imports")


@dataclass(frozen=True)
class _DiscoveryJob:
    public_id: str


@dataclass(frozen=True)
class _AcceptedSubmission:
    discovery_job: _DiscoveryJob


class _RecordingImportService:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def submit_verified(self, request: object) -> _AcceptedSubmission:
        self.requests.append(request)
        return _AcceptedSubmission(_DiscoveryJob("123e4567-e89b-42d3-a456-426614174190"))


class _Readiness:
    def schema_revision(self) -> str:
        return "0005"


@pytest.fixture
def library_database(tmp_path: Path) -> Iterator[tuple[AnalyzerSettings, sessionmaker[Session]]]:
    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    _seed_library(factory)
    try:
        yield settings, factory
    finally:
        engine.dispose()


def _seed_library(factory: sessionmaker[Session]) -> None:
    observed_alpha = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    observed_beta = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    with factory.begin() as session:
        manifest = ManagedAsset(
            public_id="123e4567-e89b-42d3-a456-426614174121",
            sha256="f" * 64,
            kind="map_manifest",
            relative_path="map-assets-v1/ff/manifest.json",
            size_bytes=2,
            media_type="application/json",
        )
        session.add(manifest)
        session.flush()
        map_row = Map(
            public_id=MAP_ID,
            content_sha256="e" * 64,
            manifest_asset_id=manifest.id,
            schema_version=1,
            engine_data_identity="zh-1.04",
            map_identity="tournament-desert",
            display_name="Tournament Desert",
            exporter_version="fixture-v1",
            min_x=0.0,
            min_y=0.0,
            min_z=0.0,
            max_x=1000.0,
            max_y=1000.0,
            max_z=100.0,
            pathing_width=1,
            pathing_height=1,
            pathing_cell_size=10.0,
            terrain_width=1,
            terrain_height=1,
            terrain_cell_size=10.0,
            metadata_json={},
        )
        session.add(map_row)
        session.flush()
        alpha = _replay(REPLAY_ALPHA, "Alpha Final", "a" * 64, map_row.id, "engine_verified", observed_alpha)
        beta = _replay(REPLAY_BETA, "Beta Match", "b" * 64, None, "parsed", observed_beta)
        beta.version_string = "1.08"
        session.add_all((alpha, beta))
        session.flush()
        session.add_all(
            (
                Source(
                    public_id=SOURCE_ALPHA,
                    replay_id=alpha.id,
                    source_kind="strata",
                    original_locator=r"C:\private\league\alpha-final.rep",
                    original_filename="alpha-final.rep",
                    strata_match_id="3133811",
                    strata_source_user_token="source-token",
                    file_size_bytes=123,
                    discovered_at=observed_alpha,
                    provenance_json={},
                ),
                Source(
                    public_id="123e4567-e89b-42d3-a456-426614174131",
                    replay_id=beta.id,
                    source_kind="watched_root",
                    original_locator=r"D:\private\beta.rep",
                    original_filename="beta.rep",
                    file_size_bytes=456,
                    discovered_at=observed_beta,
                    provenance_json={},
                ),
            )
        )
        leex = Player(public_id=PLAYER_LEEX, display_name="leex279", identity_revision=0)
        fox = Player(public_id=PLAYER_FOX, display_name="FOX27", identity_revision=0)
        session.add_all((leex, fox))
        session.flush()
        alpha_run = _parser(alpha, "123e4567-e89b-42d3-a456-426614174170", "running", observed_alpha)
        poisoned_run = _parser(alpha, "123e4567-e89b-42d3-a456-426614174171", "failed", observed_alpha.replace(hour=13))
        beta_run = _parser(beta, "123e4567-e89b-42d3-a456-426614174172", "running", observed_beta)
        session.add_all((alpha_run, poisoned_run, beta_run))
        session.flush()
        closed_slot = _replay_player(alpha, alpha_run, None, 2, "Closed Slot", "China", "win", "105")
        closed_slot.slot_kind = "closed"
        leex_replay_player = _replay_player(alpha, alpha_run, leex.id, 0, "leex279", "USA", "win", "100")
        fox_replay_player = _replay_player(alpha, alpha_run, fox.id, 1, "FOX27", "GLA", "loss", "101")
        session.add_all(
            (
                leex_replay_player,
                fox_replay_player,
                closed_slot,
                _replay_player(alpha, poisoned_run, None, 0, r"C:\poison\not-a-player", "China", "win", "102"),
                _replay_player(beta, beta_run, None, 0, "Beta One", "China", "draw", "103"),
                _replay_player(beta, beta_run, None, 1, "Beta Two", "USA", "loss", "104"),
            )
        )
        session.flush()
        observed = EvidenceItem(
            public_id=EVIDENCE_OBSERVED,
            replay_id=alpha.id,
            parser_run_id=alpha_run.id,
            tier="observed",
            source_kind="parser_command",
            source_key="library:alpha:observed",
            schema_version=1,
            created_at=observed_alpha,
        )
        derived = EvidenceItem(
            public_id=EVIDENCE_DERIVED,
            replay_id=alpha.id,
            tier="derived",
            source_kind="strategy_rule",
            source_key="library:alpha:derived",
            schema_version=1,
            created_at=observed_alpha,
        )
        session.add_all((observed, derived))
        session.flush()
        session.add(
            StrategyAssessment(
                public_id="123e4567-e89b-42d3-a456-426614174160",
                evidence_item_id=derived.id,
                replay_id=alpha.id,
                method="rule",
                strategy_label="opening-rush",
                phase="opening",
                rule_version="fixture-v1",
                frame_start=0,
                frame_end=100,
                quality="available",
                confidence=0.9,
                details_json={},
                created_at=observed_alpha,
            )
        )
        alpha_run.status = "succeeded"
        alpha_run.completion_status = "complete"
        alpha_run.result_sha256 = "c" * 64
        beta_run.status = "succeeded"
        beta_run.completion_status = "complete"
        beta_run.result_sha256 = "d" * 64
        session.add_all(
            (
                _report(alpha.id, REPORT_OLD, "old-wide", observed_alpha.replace(hour=10)),
                _report(alpha.id, REPORT_FIXED, "fixed-wide", observed_alpha.replace(hour=11)),
                _report(
                    alpha.id,
                    PLAYER_REPORT,
                    "player-first",
                    observed_alpha.replace(hour=12),
                    replay_player_id=leex_replay_player.id,
                ),
            )
        )


def _replay(
    public_id: str,
    name: str,
    digest: str,
    map_id: int | None,
    lifecycle: str,
    created_at: datetime,
) -> Replay:
    return Replay(
        public_id=public_id,
        sha256=digest,
        map_id=map_id,
        replay_name=name,
        version_string="1.04",
        version_number=104,
        frame_count=1000,
        start_time=0,
        end_time=1000,
        exe_crc=1,
        ini_crc=2,
        map_crc=3,
        map_name="Tournament Desert" if map_id is not None else "Unknown Map",
        seed=4,
        header_json={},
        lifecycle_state=lifecycle,
        created_at=created_at,
        updated_at=created_at,
    )


def _parser(replay: Replay, run_id: str, status: str, completed_at: datetime) -> ParserRun:
    return ParserRun(
        run_id=run_id,
        replay_id=replay.id,
        parser_version="fixture-v1",
        schema_version=1,
        input_sha256=replay.sha256,
        result_sha256="c" * 64 if status == "succeeded" else None,
        status=status,
        completion_status="complete" if status == "succeeded" else "failed" if status == "failed" else None,
        warnings_json=[],
        error_json=None if status == "succeeded" else {"code": "poison"},
        started_at=completed_at,
        completed_at=completed_at,
    )


def _replay_player(
    replay: Replay,
    parser: ParserRun,
    player_id: int | None,
    slot: int,
    name: str,
    faction: str,
    result: str,
    suffix: str,
) -> ReplayPlayer:
    return ReplayPlayer(
        public_id=f"123e4567-e89b-42d3-a456-426614174{suffix}",
        replay_id=replay.id,
        parser_run_id=parser.id,
        player_id=player_id,
        slot_index=slot,
        slot_kind="human",
        original_name=name,
        normalized_name=name.casefold(),
        player_index=slot,
        faction=faction,
        result=result,
        observed_json={},
    )


def _report(
    replay_id: int,
    public_id: str,
    version: str,
    created_at: datetime,
    *,
    replay_player_id: int | None = None,
) -> Report:
    return Report(
        public_id=public_id,
        replay_id=replay_id,
        replay_player_id=replay_player_id,
        report_version=version,
        input_digest=public_id.replace("-", "") * 2,
        cache_key=(public_id.replace("-", "")[::-1]) * 2,
        report_json={},
        created_at=created_at,
    )


def _adapter(database: tuple[AnalyzerSettings, sessionmaker[Session]]) -> AnalyticsLibraryAdapter:
    settings, factory = database
    return AnalyticsLibraryAdapter(
        factory,
        settings=settings,
        import_service=_NoopImportService(),
        request_telemetry=False,
        clock=lambda: datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )


def test_library_uses_latest_succeeded_parser_and_opens_the_first_player_report(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    page = _adapter(library_database).list_replays(ReplayLibraryQueryDTO(page_size=1))

    assert page.total_items == 2
    assert len(page.items) == 1
    item = page.items[0]
    assert item.replay_public_id == REPLAY_ALPHA
    assert item.report_public_id == PLAYER_REPORT
    assert item.players[0].replay_player_public_id == "123e4567-e89b-42d3-a456-426614174100"
    assert item.players[0].report_public_id == PLAYER_REPORT
    assert [(player.display_name, player.slot, player.faction, player.result) for player in item.players] == [
        ("leex279", 1, "USA", "win"),
        ("FOX27", 2, "GLA", "loss"),
    ]
    assert "poison" not in item.model_dump_json()
    assert "Closed Slot" not in item.model_dump_json()
    assert _adapter(library_database).list_replays(ReplayLibraryQueryDTO(search="Closed Slot")).items == ()


@pytest.mark.parametrize(
    ("field", "matching", "missing"),
    [
        ("search", "alpha", "missing"),
        ("player_public_id", PLAYER_LEEX, "123e4567-e89b-42d3-a456-426614174999"),
        ("faction", "GLA", "Random"),
        ("matchup", "USA-v-GLA", "GLA-v-China"),
        ("map_public_id", MAP_ID, "123e4567-e89b-42d3-a456-426614174998"),
        ("result", "win", "surrender"),
        ("patch", "1.04", "1.02"),
        ("strategy_id", "opening-rush", "unsupported-strategy"),
        ("analysis_status", "engine_verified", "desynced"),
        ("evidence_tier", "derived", "inferred"),
        ("lifecycle_state", "engine_verified", "failed"),
        ("source_kind", "strata", "unsupported-source"),
    ],
)
def test_library_truthfully_enforces_every_scalar_filter(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
    field: str,
    matching: Any,
    missing: Any,
) -> None:
    adapter = _adapter(library_database)

    matching_page = adapter.list_replays(ReplayLibraryQueryDTO.model_validate({field: matching}))
    missing_page = adapter.list_replays(ReplayLibraryQueryDTO.model_validate({field: missing}))

    assert [item.replay_public_id for item in matching_page.items] == [REPLAY_ALPHA]
    assert missing_page.items == () and missing_page.total_items == 0


def test_library_enforces_date_bounds_before_deterministic_pagination(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    adapter = _adapter(library_database)
    query = ReplayLibraryQueryDTO(
        page=1,
        page_size=1,
        sort="observed_asc",
        date_from_utc=datetime(2026, 8, 22, tzinfo=UTC),
        date_to_utc=datetime(2026, 8, 23, tzinfo=UTC),
    )

    page = adapter.list_replays(query)

    assert page.query is query
    assert page.total_items == 1
    assert [item.replay_public_id for item in page.items] == [REPLAY_ALPHA]


def test_library_and_dashboard_dtos_never_expose_private_source_locators(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    adapter = _adapter(library_database)

    page_json = adapter.list_replays(ReplayLibraryQueryDTO()).model_dump_json()
    dashboard_json = adapter.dashboard().model_dump_json()

    assert r"C:\private" not in page_json + dashboard_json
    assert "original_locator" not in page_json + dashboard_json
    assert "alpha-final.rep" in page_json
    assert [(item.replay_public_id, item.label) for item in adapter.dashboard().recent_replays] == [
        (REPLAY_ALPHA, "Alpha Final"),
        (REPLAY_BETA, "Beta Match"),
    ]

    dashboard = adapter.dashboard()
    assert dashboard.replay_count == 2
    assert dashboard.analyzed_count == 1
    assert dashboard.failed_jobs_count == 0
    assert dashboard.maps_seen == 1


def test_dashboard_counts_reported_replays_as_analyzed_even_when_engine_is_partial(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    """Catch a truthful partial report being excluded from the analyzed workspace total."""
    _, factory = library_database
    with factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY_ALPHA))
        assert replay is not None
        replay.lifecycle_state = "desynced"

    dashboard = _adapter(library_database).dashboard()

    assert dashboard.analyzed_count == 1


def test_dashboard_projects_available_players_map_and_detected_opening_without_claiming_unknown_horizon(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    recent = _adapter(library_database).dashboard().recent_replays[0]

    assert recent.player_factions == ("leex279 (USA)", "FOX27 (GLA)")
    assert recent.map_name == "Tournament Desert"
    assert recent.strategy_labels == ()
    assert recent.observed_horizon is None


def test_library_and_dashboard_do_not_present_unresolved_numeric_faction_codes(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    _settings, factory = library_database
    with factory.begin() as session:
        session.execute(text("DROP TRIGGER trg_replay_players_succeeded_no_observation_update"))
        replay_player = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == "123e4567-e89b-42d3-a456-426614174100")
        )
        assert replay_player is not None
        replay_player.faction = "7"

    adapter = _adapter(library_database)
    item = adapter.list_replays(ReplayLibraryQueryDTO(page_size=1)).items[0]
    recent = adapter.dashboard().recent_replays[0]

    assert item.players[0].faction is None
    assert recent.player_factions == ("leex279", "FOX27 (GLA)")


def test_library_and_dashboard_present_a_readable_map_name_without_changing_storage(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    _settings, factory = library_database
    raw_name = "userdata/maps/[rank] sand scorpion"
    with factory.begin() as session:
        map_row = session.scalar(select(Map).where(Map.public_id == MAP_ID))
        assert map_row is not None
        map_row.display_name = raw_name

    adapter = _adapter(library_database)
    item = adapter.list_replays(ReplayLibraryQueryDTO(page_size=1)).items[0]
    recent = adapter.dashboard().recent_replays[0]

    assert item.map_display_name == "Sand Scorpion"
    assert recent.map_name == "Sand Scorpion"
    with factory() as session:
        stored = session.scalar(select(Map.display_name).where(Map.public_id == MAP_ID))
    assert stored == raw_name


def test_dashboard_uses_the_fixed_report_observed_evidence_horizon(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    _settings, factory = library_database
    with factory.begin() as session:
        report = session.scalar(select(Report).where(Report.public_id == PLAYER_REPORT))
        assert report is not None
        report.report_json = {
            "observed": [
                {
                    "availability": "partial",
                    "evidence": [{"public_id": EVIDENCE_OBSERVED, "tier": "observed"}],
                    "frame_window": [0, 105],
                }
            ]
        }

    recent = _adapter(library_database).dashboard().recent_replays[0]

    assert recent.observed_horizon == "Observed evidence through 0:03.5 (frame 105)"


def test_dashboard_omits_a_horizon_when_fixed_report_windows_are_gapped(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    _settings, factory = library_database
    with factory.begin() as session:
        report = session.scalar(select(Report).where(Report.public_id == PLAYER_REPORT))
        assert report is not None
        report.report_json = {
            "observed": [
                {
                    "availability": "available",
                    "evidence": [{"public_id": EVIDENCE_OBSERVED, "tier": "observed"}],
                    "frame_window": [0, 30],
                },
                {
                    "availability": "available",
                    "evidence": [{"public_id": EVIDENCE_OBSERVED, "tier": "observed"}],
                    "frame_window": [60, 105],
                },
            ]
        }

    assert _adapter(library_database).dashboard().recent_replays[0].observed_horizon is None


def test_dashboard_uses_only_detected_strategies_from_the_linked_fixed_report_analysis_run(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    _settings, factory = library_database
    with factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == REPLAY_ALPHA))
        current_report = session.scalar(select(Report).where(Report.public_id == PLAYER_REPORT))
        stale_report = session.scalar(select(Report).where(Report.public_id == REPORT_FIXED))
        assert replay is not None and current_report is not None and stale_report is not None
        current_run = _analysis_run(replay.id, "current")
        stale_run = _analysis_run(replay.id, "stale")
        session.add_all((current_run, stale_run))
        session.flush()
        current_report.analysis_run_id = current_run.id
        current_report.report_json = _strategy_report_json("usa_humvee_pressure", "usa_fast_strategy_center")
        stale_report.analysis_run_id = stale_run.id
        stale_report.report_json = _strategy_report_json("gla_terror_tech")

    recent = _adapter(library_database).dashboard().recent_replays[0]

    assert recent.report_public_id == PLAYER_REPORT
    assert recent.strategy_labels == ("Humvee pressure", "Strategy Center technology")


def _analysis_run(replay_id: int, label: str) -> AnalysisRun:
    return AnalysisRun(
        run_id=str(uuid5(NAMESPACE_URL, f"dashboard-analysis:{label}")),
        replay_id=replay_id,
        replay_player_id=None,
        provider="ollama",
        model_name="fixture",
        model_digest="a" * 64,
        prompt_version="fixture",
        prompt_digest="b" * 64,
        response_schema_version="fixture",
        response_schema_digest="c" * 64,
        settings_digest="d" * 64,
        input_digest="e" * 64,
        cache_key=("f" if label == "current" else "0") * 64,
        status="failed",
        validated_response_json=None,
        diagnostics_json=[],
        error_json={"code": "fixture"},
    )


def _strategy_report_json(*labels: str) -> dict[str, object]:
    return {
        "derived": [
            {
                "claim_id": f"strategy:{label}:{index}",
                "section": "strategy",
                "label": label,
                "availability": "available",
                "raw_value": {"strategy_label": label},
                "evidence": [{"public_id": EVIDENCE_DERIVED, "tier": "derived"}],
            }
            for index, label in enumerate(labels)
        ]
    }


def test_library_projection_is_read_only(
    library_database: tuple[AnalyzerSettings, sessionmaker[Session]],
) -> None:
    _settings, factory = library_database
    before = _row_counts(factory)

    _adapter(library_database).list_replays(ReplayLibraryQueryDTO())

    assert _row_counts(factory) == before


def _row_counts(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with factory() as session:
        return (
            len(tuple(session.scalars(select(Replay)))),
            len(tuple(session.scalars(select(Source)))),
            len(tuple(session.scalars(select(Job)))),
        )


def test_configured_root_reconciliation_and_verified_submission_never_expose_or_retain_absolute_path(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "external-private-root"
    source_root.mkdir()
    replay_bytes = b"verified replay bytes"
    (source_root / "league.rep").write_bytes(replay_bytes)
    settings = AnalyzerSettings.model_validate(
        {"data_root": tmp_path / "product-data", "watched_folders": (source_root,)}
    )
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    service = _RecordingImportService()
    try:
        adapter = AnalyticsLibraryAdapter(
            create_session_factory(engine),
            settings=settings,
            import_service=service,
            request_telemetry=True,
            clock=lambda: datetime(2026, 8, 23, tzinfo=UTC),
        )
        roots = adapter.import_roots()
        submission = adapter.submit_root_selection(
            RootImportCommandDTO(root_public_id=roots[0].root_public_id, relative_path="league.rep")
        )
    finally:
        engine.dispose()

    assert len(roots) == 1 and roots[0].availability.state == "available"
    assert str(source_root) not in repr(roots) + submission.model_dump_json() + repr(service.requests)
    request = service.requests[0]
    assert request.content == replay_bytes
    assert request.root_public_id == roots[0].root_public_id
    assert request.request_telemetry is True
    assert submission.submission_public_id == "123e4567-e89b-42d3-a456-426614174190"


def test_source_mutation_maps_to_stable_path_free_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "external-private-root"
    source_root.mkdir()
    (source_root / "league.rep").write_bytes(b"before")
    settings = AnalyzerSettings.model_validate(
        {"data_root": tmp_path / "product-data", "watched_folders": (source_root,)}
    )
    settings.ensure_directories()
    registry = WatchedRootRegistry(settings.data_root)
    service = _RecordingImportService()
    adapter = AnalyticsLibraryAdapter(
        lambda: (_ for _ in ()).throw(AssertionError("import must not open a database read session")),  # type: ignore[arg-type]
        settings=settings,
        import_service=service,
        registry=registry,
        request_telemetry=False,
    )
    root = adapter.import_roots()[0]
    monkeypatch.setattr(
        WatchedRootRegistry,
        "snapshot_replay",
        lambda _self, _root_id, _relative_name: (_ for _ in ()).throw(
            SnapshotIngressError("replay_source_changed")
        ),
    )

    with pytest.raises(PublicProblem) as raised:
        adapter.submit_root_selection(
            RootImportCommandDTO(root_public_id=root.root_public_id, relative_path="league.rep")
        )

    assert (raised.value.status, raised.value.code) == (409, "replay_source_changed")
    assert str(source_root) not in raised.value.detail
    assert service.requests == []


def test_unknown_root_maps_to_public_not_found_without_import_service_call(tmp_path: Path) -> None:
    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    service = _RecordingImportService()
    adapter = AnalyticsLibraryAdapter(
        lambda: (_ for _ in ()).throw(AssertionError("import must not open a database read session")),  # type: ignore[arg-type]
        settings=settings,
        import_service=service,
        request_telemetry=False,
    )

    with pytest.raises(PublicProblem) as raised:
        adapter.submit_root_selection(
            RootImportCommandDTO(
                root_public_id="123e4567-e89b-42d3-a456-426614174191",
                relative_path="league.rep",
            )
        )

    assert (raised.value.status, raised.value.code) == (404, "unknown_import_root")
    assert service.requests == []


def test_factory_root_submission_commits_one_path_free_discovery_job(tmp_path: Path) -> None:
    settings, root_path = _factory_settings(tmp_path)
    factory = AnalyticsPortFactory(settings, _Readiness(), configuration_root=tmp_path / "configuration")

    with factory() as port:
        root = port.import_roots()[0]
        submission = port.submit_root_selection(
            RootImportCommandDTO(root_public_id=root.root_public_id, relative_path="league.rep")
        )

    jobs = _jobs(settings)
    assert [job.public_id for job in jobs] == [submission.submission_public_id]
    assert str(root_path) not in repr(jobs[0].input_json)
    assert jobs[0].input_json["root_public_id"] == root.root_public_id
    assert jobs[0].input_json["relative_name"] == "league.rep"
    assert jobs[0].input_json["request_telemetry"] is False


def test_factory_root_submission_requests_telemetry_without_launching_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from generals_replay_analyzer.engine import runner as engine_runner
    from generals_replay_analyzer.importing.engine_acquirer import EngineTelemetryAcquirer

    launches: list[str] = []

    def reject_launch(*_args: object, **_kwargs: object) -> object:
        launches.append("engine")
        raise AssertionError("Web request handling must not launch the telemetry exporter")

    monkeypatch.setattr(engine_runner, "export_telemetry", reject_launch)
    monkeypatch.setattr(EngineTelemetryAcquirer, "acquire", reject_launch)
    root_path = tmp_path / "external-private-root"
    root_path.mkdir()
    (root_path / "league.rep").write_bytes(b"factory replay bytes")
    engine_path = tmp_path / "generalszh.exe"
    engine_path.write_bytes(b"engine")
    settings = AnalyzerSettings.model_validate(
        {
            "data_root": tmp_path / "product-data",
            "watched_folders": (root_path,),
            "engine_executable": engine_path,
        }
    )
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    factory = AnalyticsPortFactory(settings, _Readiness(), configuration_root=tmp_path / "configuration")

    with factory() as port:
        root = port.import_roots()[0]
        port.submit_root_selection(
            RootImportCommandDTO(root_public_id=root.root_public_id, relative_path="league.rep")
        )

    assert _jobs(settings)[0].input_json["request_telemetry"] is True
    assert launches == []


def test_factory_rolls_back_root_submission_when_request_scope_fails(tmp_path: Path) -> None:
    settings, _root_path = _factory_settings(tmp_path)
    factory = AnalyticsPortFactory(settings, _Readiness(), configuration_root=tmp_path / "configuration")

    with pytest.raises(RuntimeError, match="render failed"), factory() as port:
        root = port.import_roots()[0]
        port.submit_root_selection(
            RootImportCommandDTO(root_public_id=root.root_public_id, relative_path="league.rep")
        )
        raise RuntimeError("render failed")

    assert _jobs(settings) == ()


def _factory_settings(tmp_path: Path) -> tuple[AnalyzerSettings, Path]:
    root_path = tmp_path / "external-private-root"
    root_path.mkdir()
    (root_path / "league.rep").write_bytes(b"factory replay bytes")
    settings = AnalyzerSettings.model_validate(
        {"data_root": tmp_path / "product-data", "watched_folders": (root_path,)}
    )
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    return settings, root_path


def _jobs(settings: AnalyzerSettings) -> tuple[Job, ...]:
    engine = create_database_engine(settings.database_path)
    try:
        with create_session_factory(engine)() as session:
            return tuple(session.scalars(select(Job).order_by(Job.public_id)))
    finally:
        engine.dispose()
