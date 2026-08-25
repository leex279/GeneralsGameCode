"""Immutable, revision-bound canonical player history queries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
    EvidenceItem,
    LongitudinalResult,
    LongitudinalRun,
    Map,
    Player,
    PlayerAlias,
    PlayerIdentityOperation,
    Replay,
    ReplayPlayer,
    Report,
    Source,
)

_PROFILE_NAMESPACE = UUID("87f3e764-6f54-57d7-a97c-d15547c4881d")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("profile datetime must use UTC")
        return value.isoformat().replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class Availability:
    state: Literal["available", "partial", "unavailable"]
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlayerIndexQuery:
    page: int = 1
    page_size: int = 25
    search: str | None = None
    faction: str | None = None
    opponent_faction: str | None = None
    map_public_id: str | None = None
    patch: str | None = None
    active_only: bool = True
    sort: Literal["display_name", "recent_match", "match_count"] = "display_name"

    def __post_init__(self) -> None:
        if self.page < 1 or not 1 <= self.page_size <= 100:
            raise ValueError("invalid player page")
        for name in ("search", "faction", "opponent_faction", "patch"):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None or not value.strip() else value.strip())


@dataclass(frozen=True, slots=True)
class PlayerSummary:
    player_public_id: str
    display_name: str
    identity_revision: int
    state: Literal["active", "retired"]
    match_count: int
    latest_match_at_utc: datetime | None
    availability: Availability
    external_profile_url: str | None = None
    external_profile_source: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerIndexPage:
    query: PlayerIndexQuery
    items: tuple[PlayerSummary, ...]
    total_items: int
    availability: Availability


@dataclass(frozen=True, slots=True)
class PlayerProfileSelection:
    player_public_id: str
    page: int = 1
    page_size: int = 25
    faction: str | None = None
    opponent_faction: str | None = None
    opponent_player_public_id: str | None = None
    map_public_id: str | None = None
    patch: str | None = None
    result: str | None = None
    start_position: str | None = None
    date_from_utc: datetime | None = None
    date_to_utc: datetime | None = None
    quality_policy_digest: str | None = None


@dataclass(frozen=True, slots=True)
class FixedPlayerProfileQuery(PlayerProfileSelection):
    expected_identity_revision: int = 0
    longitudinal_run_ids: tuple[str, ...] = ()
    report_public_ids: tuple[str, ...] = ()
    definition_binding_digest: str = "0" * 64
    profile_input_digest: str = "0" * 64


@dataclass(frozen=True, slots=True)
class PlayerProfileResolution:
    state: Literal["resolved", "unavailable"]
    fixed_query: FixedPlayerProfileQuery | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EmbeddedAlias:
    alias_public_id: str
    original_name: str
    normalized_name: str


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    alias_public_id: str
    provider_namespace: str
    external_subject: str
    attachment_operation_public_id: str


@dataclass(frozen=True, slots=True)
class StrataProvenance:
    source_public_id: str
    replay_public_id: str
    strata_match_id: str | None
    strata_source_user_token: str | None
    availability: Availability


@dataclass(frozen=True, slots=True)
class FixedReportReference:
    replay_public_id: str
    replay_player_public_id: str
    report_public_id: str
    document_schema_version: str
    report_version: str
    display_policy_version: str
    input_digest: str


@dataclass(frozen=True, slots=True)
class ReplayHistoryItem:
    replay_public_id: str
    replay_player_public_id: str
    observed_name: str
    faction: str | None
    opponent_factions: tuple[str, ...]
    opponent_player_public_ids: tuple[str, ...]
    map_public_id: str | None
    map_display_name: str | None
    patch: str | None
    start_position: str | None
    result: str | None
    started_at_utc: datetime | None
    lifecycle_state: str
    quality_issue_codes: tuple[str, ...]
    fixed_report: FixedReportReference | None
    availability: Availability


@dataclass(frozen=True, slots=True)
class LongitudinalBinding:
    run_id: str
    player_public_id: str
    identity_revision: int
    analyzer_name: str
    analyzer_version: str
    segment_digest: str
    quality_policy_digest: str
    input_digest: str
    cache_key: str
    statistics_algorithm_versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DefinitionBinding:
    definition_kind: Literal["feature", "opening", "strategy", "trend", "match_metric"]
    definition_id: str
    definition_version: str
    unit: str | None
    scope_type: str
    window_policy_version: str
    faction_comparability: Literal["same_faction_only", "declared_cross_faction"]
    taxonomy_version: str | None = None


@dataclass(frozen=True, slots=True)
class DistributionInterval:
    lower: float
    upper: float
    confidence_level: float
    method: str
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class PlayerInsight:
    insight_kind: str
    result_public_id: str
    definition: DefinitionBinding
    label: str
    raw_value: object | None
    unit: str | None
    frame_start: int | None
    frame_end: int | None
    sample_count: int
    missing_count: int
    interval: DistributionInterval | None
    quality_exclusion_codes: tuple[str, ...]
    availability: Availability
    evidence_public_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlayerProfile:
    profile_public_id: str
    input_digest: str
    query: FixedPlayerProfileQuery
    player: PlayerSummary
    longitudinal: tuple[LongitudinalBinding, ...]
    fixed_reports: tuple[FixedReportReference, ...]
    definition_bindings: tuple[DefinitionBinding, ...]
    embedded_aliases: tuple[EmbeddedAlias, ...]
    provider_identities: tuple[ProviderIdentity, ...]
    strata_provenance: tuple[StrataProvenance, ...]
    history: tuple[ReplayHistoryItem, ...]
    history_total_items: int
    insights: tuple[PlayerInsight, ...]
    availability: Availability


# TheSuperHackers @feature Leex 23/08/2026 Expose revision-bound player history without rewriting replay observations. (#TBD)
class PlayerQueryService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_players(self, query: PlayerIndexQuery) -> PlayerIndexPage:
        with self._session_factory() as session:
            rows = list(session.scalars(select(Player)))
            summaries = [item for row in rows if (item := self._summary(session, row, query)) is not None]
            if query.search:
                needle = query.search.casefold()
                summaries = [item for item in summaries if needle in item.display_name.casefold()]
            if query.active_only:
                summaries = [item for item in summaries if item.state == "active"]
            sort_key = {
                "display_name": lambda item: (item.display_name.casefold(), item.player_public_id),
                "recent_match": lambda item: (
                    item.latest_match_at_utc is None,
                    -(item.latest_match_at_utc.timestamp() if item.latest_match_at_utc else 0),
                    item.player_public_id,
                ),
                "match_count": lambda item: (-item.match_count, item.player_public_id),
            }[query.sort]
            summaries.sort(key=sort_key)
            total = len(summaries)
            start = (query.page - 1) * query.page_size
            return PlayerIndexPage(
                query, tuple(summaries[start : start + query.page_size]), total, Availability("available")
            )

    def _summary(self, session: Session, player: Player, query: PlayerIndexQuery) -> PlayerSummary | None:
        statement = (
            select(Replay, ReplayPlayer)
            .join(ReplayPlayer, ReplayPlayer.replay_id == Replay.id)
            .where(ReplayPlayer.player_id == player.id)
        )
        rows = list(session.execute(statement))
        if query.faction:
            rows = [row for row in rows if row[1].faction == query.faction]
        if query.map_public_id:
            map_id = session.scalar(select(Map.id).where(Map.public_id == query.map_public_id))
            rows = [row for row in rows if row[0].map_id == map_id]
        if query.patch:
            rows = [row for row in rows if _mapping(row[0].header_json).get("patch_identity") == query.patch]
        if query.opponent_faction:
            replay_ids = {replay.id for replay, _ in rows}
            eligible = set(
                session.scalars(
                    select(ReplayPlayer.replay_id).where(
                        ReplayPlayer.replay_id.in_(replay_ids),
                        ReplayPlayer.player_id != player.id,
                        ReplayPlayer.faction == query.opponent_faction,
                    )
                )
            )
            rows = [row for row in rows if row[0].id in eligible]
        if (query.faction or query.opponent_faction or query.map_public_id or query.patch) and not rows:
            return None
        starts = [self._start_time(replay.start_time) for replay, _row in rows]
        return PlayerSummary(
            player.public_id,
            player.display_name,
            player.identity_revision,
            "retired" if player.retired_at is not None else "active",
            len({replay.id for replay, _row in rows}),
            max((value for value in starts if value is not None), default=None),
            Availability("available"),
            player.external_profile_url,
            player.external_profile_source,
        )

    def resolve_profile(self, selection: PlayerProfileSelection) -> PlayerProfileResolution:
        with self._session_factory() as session:
            player = session.scalar(select(Player).where(Player.public_id == selection.player_public_id))
            if player is None:
                return PlayerProfileResolution("unavailable", None, ("player_not_found",))
            runs = tuple(
                session.scalars(
                    select(LongitudinalRun)
                    .where(
                        LongitudinalRun.player_id == player.id,
                        LongitudinalRun.identity_revision == player.identity_revision,
                        LongitudinalRun.status == "succeeded",
                    )
                    .order_by(LongitudinalRun.run_id)
                )
            )
            if selection.quality_policy_digest is not None:
                runs = tuple(
                    row
                    for row in runs
                    if _digest(_mapping(row.segment_key_json).get("quality_policy", {}))
                    == selection.quality_policy_digest
                )
            # TheSuperHackers @bugfix Leex 26/08/2026 Bind each replay-player history row to its newest report. (#TBD)
            report_ranks = (
                select(
                    Report.id.label("report_id"),
                    func.row_number()
                    .over(
                        partition_by=Report.replay_player_id,
                        order_by=(Report.created_at.desc(), Report.public_id.desc()),
                    )
                    .label("report_rank"),
                )
                .join(ReplayPlayer, Report.replay_player_id == ReplayPlayer.id)
                .where(ReplayPlayer.player_id == player.id)
                .subquery()
            )
            report_rows = tuple(
                session.scalars(
                    select(Report)
                    .join(report_ranks, Report.id == report_ranks.c.report_id)
                    .where(report_ranks.c.report_rank == 1)
                    .order_by(Report.public_id)
                )
            )
            report_ids = tuple(row.public_id for row in report_rows)
            definitions = tuple(
                sorted(
                    (definition for row in runs for definition in self._definition_bindings(row)),
                    key=lambda item: (item.definition_kind, item.definition_id, item.definition_version),
                )
            )
            definition_digest = _digest([self._definition_dict(item) for item in definitions])
            provisional = FixedPlayerProfileQuery(
                **{name: getattr(selection, name) for name in selection.__dataclass_fields__},
                expected_identity_revision=player.identity_revision,
                longitudinal_run_ids=tuple(row.run_id for row in runs),
                report_public_ids=report_ids,
                definition_binding_digest=definition_digest,
            )
            profile_digest = self._profile_digest(session, player, provisional, runs, report_rows)
            return PlayerProfileResolution(
                "resolved",
                FixedPlayerProfileQuery(
                    **{name: getattr(selection, name) for name in selection.__dataclass_fields__},
                    expected_identity_revision=player.identity_revision,
                    longitudinal_run_ids=tuple(row.run_id for row in runs),
                    report_public_ids=report_ids,
                    definition_binding_digest=definition_digest,
                    profile_input_digest=profile_digest,
                ),
            )

    def get_profile(self, query: FixedPlayerProfileQuery) -> PlayerProfile:
        with self._session_factory() as session:
            player = session.scalar(select(Player).where(Player.public_id == query.player_public_id))
            if player is None:
                raise LookupError("player_not_found")
            historical = player.identity_revision != query.expected_identity_revision
            run_rows = (
                tuple(
                    session.scalars(
                        select(LongitudinalRun)
                        .where(LongitudinalRun.run_id.in_(query.longitudinal_run_ids))
                        .order_by(LongitudinalRun.run_id)
                    )
                )
                if query.longitudinal_run_ids
                else ()
            )
            if tuple(row.run_id for row in run_rows) != query.longitudinal_run_ids or any(
                row.player_id != player.id
                or row.identity_revision != query.expected_identity_revision
                or row.status != "succeeded"
                for row in run_rows
            ):
                raise ValueError("stale_or_cross_player_longitudinal_binding")
            report_rows = (
                tuple(
                    session.scalars(
                        select(Report).where(Report.public_id.in_(query.report_public_ids)).order_by(Report.public_id)
                    )
                )
                if query.report_public_ids
                else ()
            )
            if tuple(row.public_id for row in report_rows) != query.report_public_ids:
                raise ValueError("stale_report_binding")
            for report in report_rows:
                replay_player = session.get(ReplayPlayer, report.replay_player_id) if report.replay_player_id else None
                if replay_player is None or replay_player.player_id != player.id:
                    raise ValueError("cross_player_report_binding")
            history_all = self._history(session, player, report_rows, query)
            aliases = tuple(session.scalars(select(PlayerAlias).where(PlayerAlias.player_id == player.id)))
            embedded = tuple(
                sorted(
                    (
                        EmbeddedAlias(item.public_id, item.original_name, item.normalized_name)
                        for item in aliases
                        if item.namespace == "embedded_replay_name"
                    ),
                    key=lambda item: (item.normalized_name, item.original_name, item.alias_public_id),
                )
            )
            provider_candidates = (
                self._provider(session, item)
                for item in aliases
                if item.namespace.startswith("external:") and not item.namespace.startswith("external:detached:")
            )
            providers = tuple(
                sorted(
                    (item for item in provider_candidates if item is not None),
                    key=lambda item: (
                        item.provider_namespace,
                        item.external_subject,
                        item.alias_public_id,
                    ),
                )
            )
            provenance = self._provenance(session, history_all)
            bindings = tuple(self._run_binding(player.public_id, row) for row in run_rows)
            definitions = tuple(
                sorted(
                    (definition for row in run_rows for definition in self._definition_bindings(row)),
                    key=lambda item: (item.definition_kind, item.definition_id, item.definition_version),
                )
            )
            if _digest([self._definition_dict(item) for item in definitions]) != query.definition_binding_digest:
                raise ValueError("definition_binding_digest_mismatch")
            if self._profile_digest(session, player, query, run_rows, report_rows) != query.profile_input_digest:
                raise ValueError("profile_input_digest_mismatch")
            reports = tuple(self._report_reference(session, row) for row in report_rows)
            insights = tuple(
                sorted(
                    (insight for row in run_rows for insight in self._insights(session, row)),
                    key=lambda item: (
                        item.insight_kind,
                        item.definition.definition_id,
                        item.frame_start or -1,
                        item.result_public_id,
                    ),
                )
            )
            availability = (
                Availability("partial", ("historical_identity_revision",)) if historical else Availability("available")
            )
            summary = PlayerSummary(
                player.public_id,
                player.display_name,
                query.expected_identity_revision,
                "retired" if player.retired_at else "active",
                len({item.replay_public_id for item in history_all}),
                max((item.started_at_utc for item in history_all if item.started_at_utc), default=None),
                availability,
                player.external_profile_url,
                player.external_profile_source,
            )
            profile_public_id = str(uuid5(_PROFILE_NAMESPACE, query.profile_input_digest))
            start = (query.page - 1) * query.page_size
            return PlayerProfile(
                profile_public_id,
                query.profile_input_digest,
                query,
                summary,
                bindings,
                reports,
                definitions,
                embedded,
                providers,
                provenance,
                history_all[start : start + query.page_size],
                len(history_all),
                insights,
                availability,
            )

    def _profile_digest(
        self,
        session: Session,
        player: Player,
        query: FixedPlayerProfileQuery,
        runs: tuple[LongitudinalRun, ...],
        reports: tuple[Report, ...],
    ) -> str:
        history = self._history(session, player, reports, query)
        aliases = tuple(
            session.scalars(
                select(PlayerAlias).where(PlayerAlias.player_id == player.id).order_by(PlayerAlias.public_id)
            )
        )
        embedded = tuple(
            EmbeddedAlias(item.public_id, item.original_name, item.normalized_name)
            for item in aliases
            if item.namespace == "embedded_replay_name"
        )
        providers = tuple(
            provider
            for item in aliases
            if item.namespace.startswith("external:") and not item.namespace.startswith("external:detached:")
            for provider in (self._provider(session, item),)
            if provider is not None
        )
        definitions = tuple(
            sorted(
                (definition for run in runs for definition in self._definition_bindings(run)),
                key=lambda item: (item.definition_kind, item.definition_id, item.definition_version),
            )
        )
        insights = tuple(
            sorted(
                (insight for run in runs for insight in self._insights(session, run)),
                key=lambda item: (
                    item.insight_kind,
                    item.definition.definition_id,
                    item.frame_start or -1,
                    item.result_public_id,
                ),
            )
        )
        selection = {name: getattr(query, name) for name in PlayerProfileSelection.__dataclass_fields__}
        return _digest(
            {
                "schema": "fixed-player-profile-input-v1",
                "selection": selection,
                "expected_identity_revision": query.expected_identity_revision,
                "player": {
                    "public_id": player.public_id,
                    "display_name": player.display_name,
                    "retired": player.retired_at is not None,
                },
                "longitudinal": tuple(self._run_binding(player.public_id, run) for run in runs),
                "reports": tuple(self._report_reference(session, report) for report in reports),
                "definitions": definitions,
                "embedded_aliases": embedded,
                "provider_identities": providers,
                "strata_provenance": self._provenance(session, history),
                "history": history,
                "insights": insights,
            }
        )

    def _history(
        self, session: Session, player: Player, reports: tuple[Report, ...], query: FixedPlayerProfileQuery
    ) -> tuple[ReplayHistoryItem, ...]:
        rows = list(
            session.execute(
                select(ReplayPlayer, Replay, Map)
                .join(Replay, ReplayPlayer.replay_id == Replay.id)
                .outerjoin(Map, Replay.map_id == Map.id)
                .where(ReplayPlayer.player_id == player.id)
            )
        )
        result: list[ReplayHistoryItem] = []
        report_by_player = {row.replay_player_id: row for row in reports if row.replay_player_id is not None}
        for replay_player, replay, map_row in rows:
            opponents = tuple(
                session.execute(
                    select(ReplayPlayer, Player)
                    .outerjoin(Player, ReplayPlayer.player_id == Player.id)
                    .where(ReplayPlayer.replay_id == replay.id, ReplayPlayer.id != replay_player.id)
                )
            )
            opponent_ids = tuple(
                sorted({canonical.public_id for _other, canonical in opponents if canonical is not None})
            )
            opponent_factions = tuple(sorted({other.faction for other, _canonical in opponents if other.faction}))
            started = self._start_time(replay.start_time)
            patch = cast(str | None, _mapping(replay.header_json).get("patch_identity"))
            if (
                query.faction
                and replay_player.faction != query.faction
                or query.opponent_faction
                and query.opponent_faction not in opponent_factions
                or query.opponent_player_public_id
                and query.opponent_player_public_id not in opponent_ids
                or query.map_public_id
                and (map_row is None or map_row.public_id != query.map_public_id)
                or query.patch
                and patch != query.patch
                or query.result
                and replay_player.result != query.result
                or query.start_position
                and str(replay_player.start_position) != query.start_position
                or query.date_from_utc
                and (started is None or started < query.date_from_utc)
                or query.date_to_utc
                and (started is None or started >= query.date_to_utc)
            ):
                continue
            report = report_by_player.get(replay_player.id)
            result.append(
                ReplayHistoryItem(
                    replay.public_id,
                    replay_player.public_id,
                    replay_player.original_name or "Unknown player",
                    replay_player.faction,
                    opponent_factions,
                    opponent_ids,
                    None if map_row is None else map_row.public_id,
                    None if map_row is None else map_row.display_name,
                    patch,
                    None if replay_player.start_position is None else str(replay_player.start_position),
                    replay_player.result,
                    started,
                    replay.lifecycle_state,
                    (),
                    None if report is None else self._report_reference(session, report),
                    Availability(
                        "available" if replay.lifecycle_state == "engine_verified" else "partial",
                        () if replay.lifecycle_state == "engine_verified" else (replay.lifecycle_state,),
                    ),
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.started_at_utc is None,
                    -(item.started_at_utc.timestamp() if item.started_at_utc else 0),
                    item.replay_public_id,
                    item.replay_player_public_id,
                ),
            )
        )

    @staticmethod
    def _start_time(value: int) -> datetime | None:
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _definitions(run: LongitudinalRun) -> object:
        return _mapping(run.settings_json).get("definitions", [])

    def _definition_bindings(self, run: LongitudinalRun) -> tuple[DefinitionBinding, ...]:
        result: list[DefinitionBinding] = []
        for value in cast(list[object], self._definitions(run)):
            item = _mapping(value)
            name = item.get("public_name")
            version = item.get("definition_version")
            if not isinstance(name, str) or not isinstance(version, str):
                continue
            kind: Literal["feature", "opening", "strategy", "trend", "match_metric"] = "feature"
            if name.startswith("recurring_opening"):
                kind = "opening"
            elif name.startswith("trend"):
                kind = "trend"
            elif "strategy" in name:
                kind = "strategy"
            scope_types = item.get("scope_types")
            scope = "player" if not isinstance(scope_types, list) or not scope_types else str(scope_types[0])
            result.append(
                DefinitionBinding(
                    kind,
                    name,
                    version,
                    cast(str | None, item.get("unit")),
                    scope,
                    "inclusive-frame-window-v1",
                    "same_faction_only",
                    cast(str | None, item.get("taxonomy_version")),
                )
            )
        return tuple(result)

    @staticmethod
    def _definition_dict(value: DefinitionBinding) -> dict[str, object]:
        return {name: getattr(value, name) for name in value.__dataclass_fields__}

    def _run_binding(self, player_public_id: str, run: LongitudinalRun) -> LongitudinalBinding:
        settings = _mapping(run.settings_json)
        algorithms = _mapping(settings.get("settings"))
        versions = tuple(
            sorted(
                {
                    str(value)
                    for key, value in algorithms.items()
                    if key.endswith("algorithm_version") and isinstance(value, str)
                }
            )
        ) or ("unknown-v1",)
        segment = _mapping(run.segment_key_json)
        quality = _mapping(segment.get("quality_policy"))
        return LongitudinalBinding(
            run.run_id,
            player_public_id,
            run.identity_revision,
            run.analyzer_name,
            run.analyzer_version,
            _digest(segment),
            _digest(quality),
            run.input_digest,
            run.cache_key,
            versions,
        )

    def _insights(self, session: Session, run: LongitudinalRun) -> tuple[PlayerInsight, ...]:
        definitions = {item.definition_id: item for item in self._definition_bindings(run)}
        rows = tuple(
            session.scalars(select(LongitudinalResult).where(LongitudinalResult.longitudinal_run_id == run.id))
        )
        result: list[PlayerInsight] = []
        kind_map = {
            "recurring_opening": "recurring_opening",
            "timing_band": "timing_distribution",
            "transition_preference": "transition_preference",
            "map_position_habit": "spatial_habit",
            "personal_baseline": "personal_baseline_deviation",
            "opponent_associated": "opponent_associated_difference",
            "trend": "trend",
            "change_point": "change_point_candidate",
            "consistency": "consistency",
        }
        for row in rows:
            storage = _mapping(row.statistics_json)
            stats = _mapping(storage.get("public_statistics"))
            raw = stats.get("value", stats.get("median", stats.get("difference", stats.get("share"))))
            interval_value = _mapping(stats.get("interval"))
            interval = None
            if isinstance(interval_value.get("lower"), (int, float)) and isinstance(
                interval_value.get("upper"), (int, float)
            ):
                interval = DistributionInterval(
                    float(interval_value["lower"]),
                    float(interval_value["upper"]),
                    float(interval_value.get("confidence_level", 0.95)),
                    str(interval_value.get("method", "accepted")),
                    str(interval_value.get("algorithm_version", "accepted-v1")),
                )
            prefix = row.result_name.split(".", 1)[0]
            insight_kind = kind_map.get(prefix, "timing_distribution" if row.result_kind == "metric" else "consistency")
            definition = definitions.get(
                row.result_name,
                DefinitionBinding(
                    "feature",
                    row.result_name,
                    "accepted-v1",
                    cast(str | None, stats.get("unit")),
                    "player",
                    "inclusive-frame-window-v1",
                    "same_faction_only",
                ),
            )
            evidence = session.get(EvidenceItem, row.evidence_item_id)
            result.append(
                PlayerInsight(
                    insight_kind,
                    row.public_id,
                    definition,
                    row.result_name.replace("_", " "),
                    raw if row.quality != "unavailable" else None,
                    definition.unit,
                    None,
                    None,
                    row.sample_count,
                    row.missing_count,
                    interval,
                    () if row.quality_reason is None else (row.quality_reason,),
                    Availability(
                        "available" if row.quality == "available" else cast(Any, row.quality),
                        () if row.quality_reason is None else (row.quality_reason,),
                    ),
                    () if evidence is None else (evidence.public_id,),
                )
            )
        return tuple(result)

    def _provider(self, session: Session, alias: PlayerAlias) -> ProviderIdentity | None:
        for operation in session.scalars(
            select(PlayerIdentityOperation).where(PlayerIdentityOperation.operation_kind == "attach_external_alias")
        ):
            after = _mapping(operation.after_json)
            aliases = after.get("aliases")
            if isinstance(aliases, list) and any(
                _mapping(item).get("alias_public_id") == alias.public_id for item in aliases
            ):
                return ProviderIdentity(
                    alias.public_id,
                    alias.namespace.removeprefix("external:"),
                    alias.external_subject or alias.normalized_name,
                    operation.public_id,
                )
        return None

    def _provenance(self, session: Session, history: tuple[ReplayHistoryItem, ...]) -> tuple[StrataProvenance, ...]:
        replay_ids = {item.replay_public_id for item in history}
        rows = (
            session.execute(
                select(Source, Replay.public_id)
                .join(Replay, Source.replay_id == Replay.id)
                .where(Replay.public_id.in_(replay_ids))
            )
            if replay_ids
            else ()
        )
        return tuple(
            sorted(
                (
                    StrataProvenance(
                        source.public_id,
                        replay_id,
                        source.strata_match_id,
                        source.strata_source_user_token,
                        Availability(
                            "available" if source.strata_match_id or source.strata_source_user_token else "unavailable",
                            ()
                            if source.strata_match_id or source.strata_source_user_token
                            else ("strata_provenance_unavailable",),
                        ),
                    )
                    for source, replay_id in rows
                    if source.source_kind == "strata"
                ),
                key=lambda item: (item.replay_public_id, item.source_public_id),
            )
        )

    @staticmethod
    def _report_reference(session: Session, report: Report) -> FixedReportReference:
        replay = session.get(Replay, report.replay_id)
        player = session.get(ReplayPlayer, report.replay_player_id) if report.replay_player_id is not None else None
        if replay is None or player is None:
            raise ValueError("player report binding is incomplete")
        return FixedReportReference(
            replay.public_id,
            player.public_id,
            report.public_id,
            "replay-report-v1",
            report.report_version,
            "replay-player-profile-display-v1",
            report.input_digest,
        )
