"""Path-free production projections for dashboard, library, and configured-root import."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    Job,
    Map,
    ParserRun,
    Player,
    Replay,
    ReplayPlayer,
    Report,
    Source,
    StrategyAssessment,
)
from generals_replay_analyzer.watching import (
    RootRegistryError,
    WatchDiscoveryError,
    WatchedRootRegistry,
    create_analytics_watched_import_adapter,
)
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DashboardDTO,
    DashboardReplayDTO,
    ImportRootDTO,
    ImportSubmissionDTO,
    PipelineStateDTO,
    ReplayLibraryItemDTO,
    ReplayLibraryPageDTO,
    ReplayLibraryQueryDTO,
    ReplayPlayerDisplayDTO,
    ReplayProvenanceDTO,
    RootImportCommandDTO,
    TerminalQualityDTO,
)

if TYPE_CHECKING:
    from generals_replay_analyzer.importing import ImportService

_INGRESS_SELECTION_CODES = frozenset(
    {
        "replay_relative_name_invalid",
        "replay_source_unsafe",
        "replay_source_not_regular",
        "replay_source_hardlinked",
        "replay_source_oversize",
    }
)


@dataclass(frozen=True, slots=True)
class _SourceProjection:
    public_id: str
    source_kind: str
    display_filename: str | None
    strata_match_token: str | None
    strata_user_token: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _EvidenceProjection:
    public_id: str
    tier: Literal["observed", "derived", "inferred"]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ReplayProjection:
    replay_public_id: str
    content_sha256: str
    label: str
    patch: str | None
    lifecycle_state: str
    map_public_id: str | None
    map_display_name: str | None
    players: tuple[ReplayPlayerDisplayDTO, ...]
    sources: tuple[_SourceProjection, ...]
    evidence: tuple[_EvidenceProjection, ...]
    report_public_id: str | None
    pipeline: PipelineStateDTO | None

    @property
    def result(self) -> str | None:
        return next((player.result for player in self.players if player.result is not None), None)

    @property
    def observed_at(self) -> datetime | None:
        return self.sources[0].observed_at if self.sources else None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _matchup(value: str) -> tuple[str, ...] | None:
    normalized = re.sub(r"\s+(?:vs?\.?|versus)\s+", "-v-", value.strip(), flags=re.IGNORECASE)
    parts = tuple(part.strip().casefold() for part in normalized.split("-v-"))
    return tuple(sorted(parts)) if len(parts) >= 2 and all(parts) else None

def _pipeline(job: Job | None, replay_public_id: str) -> PipelineStateDTO | None:
    if job is None:
        return None
    progress = None
    if job.progress_completed is not None and job.progress_total:
        progress = job.progress_completed / job.progress_total
    return PipelineStateDTO(
        stage=job.stage,
        state=job.status,
        attempt=max(1, job.attempt_count),
        progress=progress,
        job_public_id=job.public_id,
        replay_public_id=replay_public_id,
    )


# TheSuperHackers @feature Leex 23/08/2026 Project replay evidence and verified root ingress without disclosing locators. (#TBD)
class AnalyticsLibraryAdapter:
    """Build immutable web DTOs inside one caller-owned request transaction."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: AnalyzerSettings,
        import_service: ImportService,
        request_telemetry: bool,
        registry: WatchedRootRegistry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._registry = registry or WatchedRootRegistry(settings.data_root)
        self._ingress = create_analytics_watched_import_adapter(
            self._registry,
            import_service,
            request_telemetry=request_telemetry,
        )
        self._clock = clock

    def dashboard(self) -> DashboardDTO:
        page = self.list_replays(ReplayLibraryQueryDTO(page_size=8, sort="observed_desc"))
        replay_ids = tuple(value.replay_public_id for value in page.items)
        with self._session_factory() as session:
            labels: dict[str, str] = {
                row.public_id: row.replay_name
                for row in session.execute(
                    select(Replay.public_id, Replay.replay_name).where(Replay.public_id.in_(replay_ids))
                ).all()
            }
        recent = tuple(
            DashboardReplayDTO(
                replay_public_id=value.replay_public_id,
                report_public_id=value.report_public_id,
                label=labels[value.replay_public_id],
                players=tuple(player.display_name for player in value.players),
                result=value.result,
                map_name=value.map_display_name,
                analysis_state=value.lifecycle_state,
                evidence_tier=value.provenance.evidence_tier,
                observed_at=value.observed_at_utc,
            )
            for value in page.items
            if value.players
        )[:8]
        return DashboardDTO(
            generated_at=_utc(self._clock()),
            availability=AvailabilityDTO(state="available"),
            pipeline_states=tuple(value.pipeline for value in page.items if value.pipeline is not None),
            recent_replays=recent,
        )

    def list_replays(self, query: ReplayLibraryQueryDTO) -> ReplayLibraryPageDTO:
        with self._session_factory() as session:
            statement = self._filtered_statement(query)
            total_items = session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            ) or 0
            selected_rows = tuple(
                session.scalars(
                    self._ordered_statement(statement, query)
                    .offset((query.page - 1) * query.page_size)
                    .limit(query.page_size)
                )
            )
            selected = self._hydrate(session, selected_rows, query)
        return ReplayLibraryPageDTO(
            query=query,
            items=tuple(self._item(value, query) for value in selected),
            page=query.page,
            page_size=query.page_size,
            total_items=total_items,
            availability=AvailabilityDTO(state="available"),
        )

    def import_roots(self) -> tuple[ImportRootDTO, ...]:
        try:
            roots = self._registry.reconcile(self._settings.watched_folders)
        except RootRegistryError as error:
            raise PublicProblem(
                status=503,
                code=error.code,
                detail="Configured replay roots are unavailable",
            ) from None
        return tuple(
            ImportRootDTO(
                root_public_id=root.root_public_id,
                label=root.label,
                availability=AvailabilityDTO(
                    state="available" if root.available else "unavailable",
                    reason_codes=() if root.available else (root.reason_code or "watched_root_unavailable",),
                ),
                reason_code=root.reason_code,
            )
            for root in roots
        )

    def submit_root_selection(self, command: RootImportCommandDTO) -> ImportSubmissionDTO:
        try:
            submission_public_id = self._ingress.submit_stable(command.root_public_id, command.relative_path)
        except WatchDiscoveryError as error:
            if error.code == "watched_root_unknown":
                raise PublicProblem(
                    status=404,
                    code="unknown_import_root",
                    detail="The selected replay root is unavailable",
                ) from None
            if error.code.endswith("_unavailable") or error.code == "watched_import_unavailable":
                raise PublicProblem(
                    status=503,
                    code=error.code,
                    detail="Replay import is temporarily unavailable",
                ) from None
            if error.code in _INGRESS_SELECTION_CODES:
                raise PublicProblem(
                    status=422,
                    code=error.code,
                    detail="Replay selection cannot be accepted",
                ) from None
            raise PublicProblem(
                status=409,
                code=error.code,
                detail="Replay source identity changed during verified import",
            ) from None
        return ImportSubmissionDTO(
            submission_public_id=submission_public_id,
            availability=AvailabilityDTO(state="available"),
        )

    @staticmethod
    def _latest_parser_id() -> Any:
        return (
            select(ParserRun.id)
            .where(ParserRun.replay_id == Replay.id, ParserRun.status == "succeeded")
            .order_by(ParserRun.completed_at.desc(), ParserRun.run_id.desc())
            .limit(1)
            .correlate(Replay)
            .scalar_subquery()
        )

    def _filtered_statement(self, query: ReplayLibraryQueryDTO) -> Any:
        statement = select(Replay)
        latest_parser_id = self._latest_parser_id()
        player_scope = (
            ReplayPlayer.replay_id == Replay.id,
            ReplayPlayer.parser_run_id == latest_parser_id,
            ReplayPlayer.slot_kind.in_(("human", "ai")),
        )
        if query.search is not None:
            needle = query.search.casefold()
            statement = statement.where(
                or_(
                    func.lower(Replay.replay_name).contains(needle, autoescape=True),
                    func.lower(Replay.map_name).contains(needle, autoescape=True),
                    func.lower(Replay.version_string).contains(needle, autoescape=True),
                    select(1)
                    .select_from(Source)
                    .where(Source.replay_id == Replay.id, func.lower(Source.original_filename).contains(needle, autoescape=True))
                    .exists(),
                    select(1)
                    .select_from(ReplayPlayer)
                    .outerjoin(Player, Player.id == ReplayPlayer.player_id)
                    .where(
                        *player_scope,
                        or_(
                            func.lower(Player.display_name).contains(needle, autoescape=True),
                            func.lower(ReplayPlayer.original_name).contains(needle, autoescape=True),
                        ),
                    )
                    .exists(),
                )
            )
        if query.player_public_id is not None:
            statement = statement.where(
                select(1)
                .select_from(ReplayPlayer)
                .join(Player, Player.id == ReplayPlayer.player_id)
                .where(*player_scope, Player.public_id == query.player_public_id)
                .exists()
            )
        if query.faction is not None:
            statement = statement.where(
                select(1)
                .select_from(ReplayPlayer)
                .where(*player_scope, func.lower(ReplayPlayer.faction) == query.faction.casefold())
                .exists()
            )
        if query.matchup is not None:
            matchup = _matchup(query.matchup)
            if matchup is None:
                statement = statement.where(false())
            else:
                total = (
                    select(func.count())
                    .select_from(ReplayPlayer)
                    .where(*player_scope)
                    .correlate(Replay)
                    .scalar_subquery()
                )
                statement = statement.where(total == len(matchup))
                for faction, count in Counter(matchup).items():
                    faction_count = (
                        select(func.count())
                        .select_from(ReplayPlayer)
                        .where(*player_scope, func.lower(ReplayPlayer.faction) == faction)
                        .correlate(Replay)
                        .scalar_subquery()
                    )
                    statement = statement.where(faction_count == count)
        if query.map_public_id is not None:
            statement = statement.where(
                Replay.map_id == select(Map.id).where(Map.public_id == query.map_public_id).scalar_subquery()
            )
        if query.result is not None:
            statement = statement.where(
                select(1)
                .select_from(ReplayPlayer)
                .where(*player_scope, func.lower(ReplayPlayer.result) == query.result.casefold())
                .exists()
            )
        if query.patch is not None:
            statement = statement.where(func.lower(Replay.version_string) == query.patch.casefold())
        if query.strategy_id is not None:
            statement = statement.where(
                select(1)
                .select_from(StrategyAssessment)
                .where(
                    StrategyAssessment.replay_id == Replay.id,
                    func.lower(StrategyAssessment.strategy_label) == query.strategy_id.casefold(),
                )
                .exists()
            )
        if query.analysis_status is not None:
            statement = statement.where(Replay.lifecycle_state == query.analysis_status)
        if query.evidence_tier is not None:
            statement = statement.where(
                select(1)
                .select_from(EvidenceItem)
                .where(EvidenceItem.replay_id == Replay.id, EvidenceItem.tier == query.evidence_tier)
                .exists()
            )
        if query.lifecycle_state is not None:
            statement = statement.where(func.lower(Replay.lifecycle_state) == query.lifecycle_state.casefold())
        source_predicates = self._source_predicates(query)
        if source_predicates:
            statement = statement.where(
                select(1).select_from(Source).where(Source.replay_id == Replay.id, *source_predicates).exists()
            )
        return statement

    @staticmethod
    def _source_predicates(query: ReplayLibraryQueryDTO) -> tuple[Any, ...]:
        predicates: list[Any] = []
        if query.source_kind is not None:
            predicates.append(func.lower(Source.source_kind) == query.source_kind.casefold())
        if query.date_from_utc is not None:
            predicates.append(Source.discovered_at >= query.date_from_utc)
        if query.date_to_utc is not None:
            predicates.append(Source.discovered_at <= query.date_to_utc)
        return tuple(predicates)

    def _ordered_statement(self, statement: Any, query: ReplayLibraryQueryDTO) -> Any:
        observed = (
            select(func.max(Source.discovered_at))
            .where(Source.replay_id == Replay.id, *self._source_predicates(query))
            .correlate(Replay)
            .scalar_subquery()
        )
        if query.sort == "observed_asc":
            return statement.order_by(observed.is_(None), observed.asc(), Replay.public_id.asc())
        if query.sort == "replay_asc":
            return statement.order_by(func.lower(Replay.replay_name).asc(), Replay.public_id.asc())
        if query.sort == "status_asc":
            return statement.order_by(func.lower(Replay.lifecycle_state).asc(), Replay.public_id.asc())
        return statement.order_by(observed.is_(None), observed.desc(), Replay.public_id.asc())

    def _hydrate(
        self,
        session: Session,
        replays: tuple[Replay, ...],
        query: ReplayLibraryQueryDTO,
    ) -> tuple[_ReplayProjection, ...]:
        if not replays:
            return ()
        replay_ids = tuple(replay.id for replay in replays)
        map_ids = tuple(replay.map_id for replay in replays if replay.map_id is not None)
        maps = {
            row.id: row
            for row in session.scalars(select(Map).where(Map.id.in_(map_ids)))
        } if map_ids else {}

        parser_ranked = (
            select(
                ParserRun.id.label("parser_id"),
                ParserRun.replay_id.label("replay_id"),
                func.row_number()
                .over(
                    partition_by=ParserRun.replay_id,
                    order_by=(ParserRun.completed_at.desc(), ParserRun.run_id.desc()),
                )
                .label("rank"),
            )
            .where(ParserRun.replay_id.in_(replay_ids), ParserRun.status == "succeeded")
            .subquery()
        )
        parser_by_replay: dict[int, int] = {
            row.replay_id: row.parser_id
            for row in session.execute(
                select(parser_ranked.c.replay_id, parser_ranked.c.parser_id).where(parser_ranked.c.rank == 1)
            ).all()
        }
        parser_ids = tuple(parser_by_replay.values())
        players_by_replay: dict[int, list[ReplayPlayerDisplayDTO]] = defaultdict(list)
        if parser_ids:
            player_rows = tuple(session.execute(
                select(ReplayPlayer, Player.display_name)
                .outerjoin(Player, Player.id == ReplayPlayer.player_id)
                .where(
                    ReplayPlayer.parser_run_id.in_(parser_ids),
                    ReplayPlayer.slot_kind.in_(("human", "ai")),
                )
                .order_by(ReplayPlayer.replay_id, ReplayPlayer.slot_index, ReplayPlayer.public_id)
            ))
            replay_player_ids = tuple(replay_player.id for replay_player, _canonical_name in player_rows)
            report_ranked = (
                select(
                    Report.id.label("report_id"),
                    Report.replay_player_id.label("replay_player_id"),
                    func.row_number()
                    .over(
                        partition_by=Report.replay_player_id,
                        order_by=(Report.created_at.desc(), Report.public_id.desc()),
                    )
                    .label("rank"),
                )
                .where(Report.replay_player_id.in_(replay_player_ids))
                .subquery()
            )
            reports_by_player = {
                report.replay_player_id: report
                for report in session.scalars(
                    select(Report).join(
                        report_ranked,
                        and_(Report.id == report_ranked.c.report_id, report_ranked.c.rank == 1),
                    )
                )
            }
            for replay_player, canonical_name in player_rows:
                label = canonical_name or replay_player.original_name or f"Player slot {replay_player.slot_index + 1}"
                player_report = reports_by_player.get(replay_player.id)
                players_by_replay[replay_player.replay_id].append(
                    ReplayPlayerDisplayDTO(
                        replay_player_public_id=replay_player.public_id if player_report is not None else None,
                        report_public_id=player_report.public_id if player_report is not None else None,
                        display_name=label,
                        slot=replay_player.slot_index + 1,
                        faction=replay_player.faction,
                        result=replay_player.result,
                    )
                )

        source_ranked = (
            select(
                Source.public_id,
                Source.replay_id,
                Source.source_kind,
                Source.original_filename,
                Source.strata_match_id,
                Source.strata_source_user_token,
                Source.discovered_at,
                func.row_number()
                .over(partition_by=Source.replay_id, order_by=(Source.discovered_at.desc(), Source.public_id.asc()))
                .label("rank"),
            )
            .where(Source.replay_id.in_(replay_ids), *self._source_predicates(query))
            .subquery()
        )
        sources_by_replay = {
            row.replay_id: _SourceProjection(
                row.public_id,
                row.source_kind,
                row.original_filename or None,
                row.strata_match_id,
                row.strata_source_user_token,
                _utc(row.discovered_at),
            )
            for row in session.execute(select(source_ranked).where(source_ranked.c.rank == 1))
        }

        evidence_order = case((EvidenceItem.tier == "observed", 0), (EvidenceItem.tier == "derived", 1), else_=2)
        evidence_statement = select(
            EvidenceItem.public_id,
            EvidenceItem.replay_id,
            EvidenceItem.tier,
            EvidenceItem.created_at,
            func.row_number()
            .over(
                partition_by=EvidenceItem.replay_id,
                order_by=(evidence_order.asc(), EvidenceItem.created_at.desc(), EvidenceItem.public_id.asc()),
            )
            .label("rank"),
        ).where(EvidenceItem.replay_id.in_(replay_ids))
        if query.evidence_tier is not None:
            evidence_statement = evidence_statement.where(EvidenceItem.tier == query.evidence_tier)
        evidence_ranked = evidence_statement.subquery()
        evidence_by_replay = {
            row.replay_id: _EvidenceProjection(row.public_id, row.tier, _utc(row.created_at))
            for row in session.execute(select(evidence_ranked).where(evidence_ranked.c.rank == 1))
        }

        reports_by_replay = self._latest_entities(
            session,
            Report,
            replay_ids,
            (Report.created_at.desc(), Report.public_id.desc()),
            Report.replay_player_id.is_(None),
        )
        jobs_by_replay = self._latest_entities(
            session,
            Job,
            replay_ids,
            (Job.created_at.desc(), Job.public_id.desc()),
        )
        return tuple(
            self._projection(
                replay,
                maps.get(replay.map_id) if replay.map_id is not None else None,
                tuple(players_by_replay[replay.id]),
                sources_by_replay.get(replay.id),
                evidence_by_replay.get(replay.id),
                reports_by_replay.get(replay.id),
                jobs_by_replay.get(replay.id),
            )
            for replay in replays
        )

    @staticmethod
    def _latest_entities(
        session: Session,
        model: Any,
        replay_ids: tuple[int, ...],
        ordering: tuple[Any, ...],
        *predicates: Any,
    ) -> dict[int, Any]:
        ranked = (
            select(
                model.id.label("entity_id"),
                model.replay_id.label("replay_id"),
                func.row_number().over(partition_by=model.replay_id, order_by=ordering).label("rank"),
            )
            .where(model.replay_id.in_(replay_ids), *predicates)
            .subquery()
        )
        return {
            value.replay_id: value
            for value in session.scalars(
                select(model).join(ranked, and_(model.id == ranked.c.entity_id, ranked.c.rank == 1))
            )
        }

    @staticmethod
    def _projection(
        replay: Replay,
        map_row: Map | None,
        players: tuple[ReplayPlayerDisplayDTO, ...],
        source: _SourceProjection | None,
        evidence: _EvidenceProjection | None,
        report: Report | None,
        job: Job | None,
    ) -> _ReplayProjection:
        return _ReplayProjection(
            replay_public_id=replay.public_id,
            content_sha256=replay.sha256,
            label=replay.replay_name,
            patch=replay.version_string or None,
            lifecycle_state=replay.lifecycle_state,
            map_public_id=map_row.public_id if map_row is not None else None,
            map_display_name=(map_row.display_name or replay.map_name) if map_row is not None else replay.map_name,
            players=players,
            sources=(source,) if source is not None else (),
            evidence=(evidence,) if evidence is not None else (),
            report_public_id=next(
                (player.report_public_id for player in players if player.report_public_id is not None),
                report.public_id if report is not None else None,
            ),
            pipeline=_pipeline(job, replay.public_id),
        )

    def _item(self, value: _ReplayProjection, query: ReplayLibraryQueryDTO) -> ReplayLibraryItemDTO:
        source = value.sources[0] if value.sources else None
        evidence = value.evidence[0] if value.evidence else None
        reasons = () if value.players else ("succeeded_parser_players_unavailable",)
        availability = AvailabilityDTO(state="available" if value.players else "partial", reason_codes=reasons)
        provenance_availability = AvailabilityDTO(
            state="available" if source is not None else "unavailable",
            reason_codes=() if source is not None else ("replay_provenance_unavailable",),
        )
        return ReplayLibraryItemDTO(
            replay_public_id=value.replay_public_id,
            report_public_id=value.report_public_id,
            content_sha256=value.content_sha256,
            display_filename=source.display_filename if source is not None else None,
            players=value.players,
            map_public_id=value.map_public_id,
            map_display_name=value.map_display_name,
            patch=value.patch,
            result=value.result,
            lifecycle_state=value.lifecycle_state,
            pipeline=value.pipeline,
            terminal_quality=TerminalQualityDTO(lifecycle=value.lifecycle_state),
            availability=availability,
            provenance=ReplayProvenanceDTO(
                source_public_id=source.public_id if source is not None else None,
                source_kind=source.source_kind if source is not None else None,
                strata_match_token=source.strata_match_token if source is not None else None,
                strata_user_token=source.strata_user_token if source is not None else None,
                availability=provenance_availability,
                evidence_tier=evidence.tier if evidence is not None else None,
                evidence_public_id=evidence.public_id if evidence is not None else None,
            ),
            observed_at_utc=source.observed_at if source is not None else None,
        )
