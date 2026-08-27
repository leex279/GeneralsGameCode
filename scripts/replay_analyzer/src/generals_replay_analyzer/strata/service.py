"""End-to-end name and replay identity resolution orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from generals_replay_analyzer.errors import ReplayParseError

from .acquisition import AcquisitionIncompleteError, StrataAcquirer
from .cache import SOURCE, ResolverCache
from .contracts import (
    AliasRecord,
    DownloadedReplayEvidence,
    MatchDocument,
    MatchEvidence,
    NameResolution,
    PlayerCandidate,
    ProfileDocument,
    QueryName,
    ReplayContext,
    ReplayPlayerResolution,
    ReplayResolution,
    ResolutionStatus,
)
from .matching import evaluate_match, rank_match_evidence
from .matching import resolve_name as apply_name_policy
from .normalization import normalize_query_name
from .ports import Clock, StrataHttpPort
from .replay_context import build_replay_context


def _aliases(profile: ProfileDocument) -> tuple[AliasRecord, ...]:
    return tuple(
        AliasRecord(
            source=SOURCE,
            player_id=profile.player_id,
            profile_url=profile.profile_url,
            most_known_name=profile.most_known_name,
            alias_raw=alias.name_raw,
            alias_nfc=alias.name_nfc,
            alias_casefold=alias.name_casefold,
            occurrence_count=alias.occurrence_count,
            source_rank=alias.source_rank,
        )
        for alias in profile.aliases
    )


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _candidates(result: NameResolution) -> tuple[PlayerCandidate, ...]:
    values: list[PlayerCandidate] = []
    if result.selected is not None:
        values.append(result.selected)
    values.extend(result.alternatives)
    values.extend(result.case_insensitive_suggestions)
    seen: set[int] = set()
    unique: list[PlayerCandidate] = []
    for value in values:
        if value.player_id not in seen:
            seen.add(value.player_id)
            unique.append(value)
    return tuple(unique)


class StrataResolver:
    """Resolve exact replay names and correlate all slots through one shared Strata match."""

    def __init__(
        self,
        acquirer: StrataAcquirer,
        cache: ResolverCache,
        http: StrataHttpPort,
        clock: Clock,
    ) -> None:
        self.acquirer = acquirer
        self.cache = cache
        self.http = http
        self.clock = clock

    def _resolve_name(
        self,
        query: QueryName,
        *,
        refresh: bool,
        offline: bool,
        include_fuzzy: bool,
    ) -> tuple[NameResolution, tuple[AliasRecord, ...]]:
        discovery = self.acquirer.search(query, refresh=refresh, offline=offline)
        aliases: list[AliasRecord] = []
        reasons = list(discovery.reason_codes)
        complete = discovery.complete
        for player_id in dict.fromkeys(discovery.player_ids):
            try:
                aliases.extend(_aliases(self.acquirer.profile(player_id, refresh=refresh, offline=offline)))
            except AcquisitionIncompleteError as error:
                complete = False
                reasons.append(error.code)
        result = apply_name_policy(
            query,
            aliases,
            search_complete=complete,
            checked_at=self.clock.now(),
            include_fuzzy=include_fuzzy,
        )
        if reasons:
            result = replace(result, reason_codes=_unique((*result.reason_codes, *reasons)))
        return result, tuple(aliases)

    # TheSuperHackers @feature Leex 27/08/2026 Return every exact alias owner and append immutable evidence for each lookup.
    def resolve_name(
        self,
        value: str,
        *,
        refresh: bool = False,
        offline: bool = False,
        include_fuzzy: bool = False,
    ) -> NameResolution:
        query = normalize_query_name(value)
        result, _ = self._resolve_name(
            query,
            refresh=refresh,
            offline=offline,
            include_fuzzy=include_fuzzy,
        )
        self.cache.append_resolution(
            None,
            0,
            result,
            {
                "acquisition": [
                    {
                        "kind": decision.kind,
                        "key": decision.key,
                        "source": decision.source,
                        "stale": decision.stale,
                        "reason_code": decision.reason_code,
                    }
                    for decision in self.acquirer.decisions
                ]
            },
        )
        return result

    def _downloaded_evidence(self, match: MatchDocument) -> tuple[DownloadedReplayEvidence, ...]:
        evidence: list[DownloadedReplayEvidence] = []
        urls = tuple(dict.fromkeys(item.replay_url for item in match.participants if item.replay_url is not None))
        with TemporaryDirectory(prefix="strata-resolver-") as directory:
            root = Path(directory)
            for index, url in enumerate(urls):
                try:
                    data = self.http.download_replay(url)
                    path = root / f"candidate-{index}.rep"
                    path.write_bytes(data)
                    context = build_replay_context(path)
                except (OSError, ReplayParseError, RuntimeError, ValueError):
                    continue
                evidence.append(
                    DownloadedReplayEvidence(
                        replay_url=url,
                        replay_sha256=context.replay_sha256,
                        command_stream_sha256=context.command_stream_sha256,
                        match_signature_sha256=context.match_signature_sha256,
                    )
                )
        return tuple(evidence)

    def _evaluate(
        self,
        context: ReplayContext,
        match_id: int,
        aliases_by_slot: dict[int, tuple[AliasRecord, ...]],
        *,
        refresh: bool,
        offline: bool,
    ) -> MatchEvidence | None:
        try:
            match = self.acquirer.match(match_id, refresh=refresh, offline=offline)
        except AcquisitionIncompleteError:
            return None
        metadata = evaluate_match(context, match, (), slot_aliases=aliases_by_slot)
        if not metadata.viable or offline:
            return metadata
        return evaluate_match(
            context,
            match,
            self._downloaded_evidence(match),
            slot_aliases=aliases_by_slot,
        )

    # TheSuperHackers @feature Leex 27/08/2026 Resolve external player IDs only after one shared match maps every replay slot uniquely.
    def resolve_replay(
        self,
        path: Path,
        *,
        refresh: bool = False,
        offline: bool = False,
        include_fuzzy: bool = False,
    ) -> ReplayResolution:
        context = build_replay_context(path)
        name_results: list[NameResolution] = []
        aliases_by_slot: dict[int, tuple[AliasRecord, ...]] = {}
        reasons: list[str] = []
        acquisition_complete = True
        for slot in context.human_players:
            if slot.name_raw is None:
                continue
            result, aliases = self._resolve_name(
                normalize_query_name(slot.name_raw),
                refresh=refresh,
                offline=offline,
                include_fuzzy=include_fuzzy,
            )
            name_results.append(result)
            aliases_by_slot[slot.slot_index] = aliases
            if not result.search_complete:
                acquisition_complete = False
                reasons.extend(result.reason_codes)

        evidence_by_id: dict[int, MatchEvidence] = {}
        if context.hinted_strata_match_id is not None:
            hinted = self._evaluate(
                context,
                context.hinted_strata_match_id,
                aliases_by_slot,
                refresh=refresh,
                offline=offline,
            )
            if hinted is None:
                acquisition_complete = False
                reasons.append("hint_match_acquisition_failed")
            else:
                evidence_by_id[hinted.match_id] = hinted

        hinted_resolution = rank_match_evidence(tuple(evidence_by_id.values()))
        if hinted_resolution.status is not ResolutionStatus.RESOLVED:
            match_ids: set[int] = set()
            for result in name_results:
                for candidate in _candidates(result):
                    discovery = self.acquirer.candidate_matches(
                        candidate.player_id,
                        refresh=refresh,
                        offline=offline,
                    )
                    match_ids.update(discovery.match_ids)
                    if not discovery.complete:
                        acquisition_complete = False
                        reasons.extend(discovery.reason_codes)
            for match_id in sorted(match_ids):
                if match_id in evidence_by_id:
                    continue
                evidence = self._evaluate(
                    context,
                    match_id,
                    aliases_by_slot,
                    refresh=refresh,
                    offline=offline,
                )
                if evidence is None:
                    acquisition_complete = False
                    reasons.append("match_acquisition_failed")
                else:
                    evidence_by_id[match_id] = evidence

        match_resolution = rank_match_evidence(tuple(evidence_by_id.values()))
        selected_evidence = next(
            (
                item
                for item in match_resolution.evidence
                if item.match_id == match_resolution.selected_match_id
            ),
            None,
        )
        assignments = dict(match_resolution.assignments)
        players: list[ReplayPlayerResolution] = []
        final_name_results: list[NameResolution] = []
        by_slot = {slot.slot_index: slot for slot in context.human_players}
        for slot, name_result in zip(context.human_players, name_results, strict=True):
            selected = None
            status = name_result.status
            confidence = name_result.confidence
            assigned_id = assignments.get(slot.slot_index)
            if assigned_id is not None and selected_evidence is not None:
                selected = next(
                    (candidate for candidate in _candidates(name_result) if candidate.player_id == assigned_id),
                    None,
                )
                if selected is not None:
                    selected = replace(
                        selected,
                        selection_reason="unique player assignment from confirmed shared Strata match",
                    )
                    status = ResolutionStatus.RESOLVED
                    confidence = match_resolution.confidence
            final_name = replace(
                name_result,
                status=status,
                selected=selected if selected is not None else name_result.selected,
                confidence=confidence,
                needs_replay_context=status is not ResolutionStatus.RESOLVED,
            )
            final_name_results.append(final_name)
            players.append(
                ReplayPlayerResolution(
                    slot_index=slot.slot_index,
                    player_index=slot.player_index,
                    query=name_result.query,
                    status=status,
                    selected=selected if selected is not None else name_result.selected,
                    alternatives=name_result.alternatives,
                    case_insensitive_suggestions=name_result.case_insensitive_suggestions,
                    fuzzy_suggestions=name_result.fuzzy_suggestions,
                    confidence=confidence,
                    name_resolution=name_result,
                    match_evidence=selected_evidence,
                )
            )

        audit_ids = tuple(
            self.cache.append_resolution(
                context.replay_sha256,
                slot_index,
                result,
                {
                    "match_resolution": match_resolution.to_dict(),
                    "replay_player": by_slot[slot_index].to_dict(),
                },
            )
            for slot_index, result in zip((item.slot_index for item in players), final_name_results, strict=True)
        )
        return ReplayResolution(
            replay=context,
            match_resolution=match_resolution,
            players=tuple(players),
            acquisition_complete=acquisition_complete,
            checked_at=self.clock.now(),
            reason_codes=_unique(reasons),
            audit_ids=audit_ids,
        )
