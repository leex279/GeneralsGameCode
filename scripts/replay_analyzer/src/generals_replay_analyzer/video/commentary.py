"""Deterministic, evidence-bound commentary plans for replay broadcasts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from generals_replay_analyzer.report.model import CanonicalValue, ReportValue, thaw_report_value
from generals_replay_analyzer.report.read_model import PublishedReportGraphDTO
from generals_replay_analyzer.video.contracts import (
    CameraPlanV1,
    CommentaryEnrichmentSentenceV1,
    CommentaryEventV1,
    CommentaryPlanV1,
    EvidenceCitationV1,
    ValidatedCommentaryEnrichmentV1,
)

_NAMESPACE = uuid5(NAMESPACE_URL, "generals-replay-analyzer:commentary-plan-v1")
_TEMPLATE_VERSION: Literal["commentary-template-v1"] = "commentary-template-v1"


class CommentaryPlanContractError(ValueError):
    """A fixed report and camera plan cannot support truthful commentary."""


@dataclass(frozen=True, slots=True)
class _Claim:
    value: ReportValue
    start_frame: int
    end_frame: int
    citations: tuple[EvidenceCitationV1, ...]


def _claim_evidence(value: ReportValue, horizon: int) -> tuple[EvidenceCitationV1, ...]:
    if value.frame_window is None:
        raise CommentaryPlanContractError("commentary claims require an explicit evidence frame window")
    start, end = value.frame_window
    if end > horizon:
        raise CommentaryPlanContractError("commentary claim exceeds the accepted evidence horizon")
    if not value.evidence:
        raise CommentaryPlanContractError("commentary claims require accepted evidence")
    if any(reference.tier not in ("observed", "derived") for reference in value.evidence):
        raise CommentaryPlanContractError("commentary claims require observed or derived evidence")
    return tuple(
        EvidenceCitationV1(
            evidence_public_id=reference.public_id,
            tier=cast(Literal["observed", "derived"], reference.tier),
            frame_start=start,
            frame_end=end,
        )
        for reference in sorted(value.evidence, key=lambda item: (item.public_id, item.tier))
    )


def _claims(report: PublishedReportGraphDTO, horizon: int) -> tuple[_Claim, ...]:
    output: list[_Claim] = []
    # TheSuperHackers @bugfix Leex 24/08/2026 Narrate only claims accepted by the selected report authority. (#TBD)
    document = report.selected.document
    for value in (*document.observed, *document.derived):
        if type(value) is not ReportValue or value.availability != "available" or value.frame_window is None:
            continue
        start, end = value.frame_window
        if end > horizon:
            continue
        output.append(_Claim(value, start, end, _claim_evidence(value, horizon)))
    return tuple(sorted(output, key=lambda item: (item.start_frame, item.end_frame, item.value.claim_id)))


def _mapping(value: CanonicalValue) -> dict[str, object]:
    thawed = thaw_report_value(value)
    return thawed if isinstance(thawed, dict) else {}


def _spoken_map_name(value: str) -> str:
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    while leaf.startswith("[") and "]" in leaf:
        leaf = leaf.split("]", 1)[1].strip()
    if not leaf:
        return value
    return leaf.title() if leaf.islower() else leaf


def _camera_segment_id(camera: CameraPlanV1, start: int, end: int) -> str:
    for segment in camera.segments:
        if segment.start_frame <= start and end <= segment.end_frame:
            return segment.segment_id
    raise CommentaryPlanContractError("commentary event is not covered by a camera segment")


def _tier(citations: tuple[EvidenceCitationV1, ...]) -> Literal["observed", "derived"]:
    return "observed" if all(item.tier == "observed" for item in citations) else "derived"


# TheSuperHackers @feature Leex 23/08/2026 Generate offline-complete commentary from accepted report claims and camera evidence only. (#TBD)
class CommentaryPlanService:
    def create(
        self,
        report: PublishedReportGraphDTO,
        camera: CameraPlanV1,
        *,
        enrichment: ValidatedCommentaryEnrichmentV1 | None = None,
    ) -> CommentaryPlanV1:
        if type(report) is not PublishedReportGraphDTO or type(camera) is not CameraPlanV1:
            raise TypeError("commentary requires fixed report and camera plan models")
        self._validate_identity(report, camera)
        horizon = camera.authority.evidence_horizon.frame_end
        claims = _claims(report, horizon)
        events = self._deterministic_events(report, camera, claims, horizon)
        plan = CommentaryPlanV1(
            logic_hz=camera.authority.logic_frames_per_second,
            replay_public_id=report.replay_public_id,
            report_public_id=report.selected.document.report_public_id,
            evidence_horizon=camera.authority.evidence_horizon,
            events=tuple(events),
        )
        return self._apply_enrichment(plan, enrichment)

    @staticmethod
    def _validate_identity(report: PublishedReportGraphDTO, camera: CameraPlanV1) -> None:
        authority = camera.authority
        # TheSuperHackers @bugfix Leex 24/08/2026 Synchronize commentary with the same selected report as camera direction. (#TBD)
        document = report.selected.document
        if report.replay_public_id != authority.replay_public_id or document.replay_public_id != authority.replay_public_id:
            raise CommentaryPlanContractError("report and camera replay identities differ")
        if document.report_public_id != authority.report_public_id or report.selected_report_public_id != authority.report_public_id:
            raise CommentaryPlanContractError("report and camera report identities differ")
        if document.replay_sha256 != authority.replay_sha256:
            raise CommentaryPlanContractError("report and camera replay hashes differ")

    def _deterministic_events(
        self, report: PublishedReportGraphDTO, camera: CameraPlanV1, claims: tuple[_Claim, ...], horizon: int
    ) -> list[CommentaryEventV1]:
        players = report.identity.players
        player_names = {player.public_id: player.display_name for player in players}
        events: list[CommentaryEventV1] = []
        partial = report.identity.duration_frames is not None and horizon < report.identity.duration_frames
        for claim in claims:
            if claim.start_frame == 0:
                continue
            rendered = self._render_claim(claim, player_names)
            if rendered is None:
                continue
            role, text, player_ids, strategy = rendered
            events.append(self._event(camera, claim, role, text, player_ids, strategy))
        events.sort(key=lambda item: (item.start_frame, item.event_id))
        intro_latest_end = events[0].start_frame - 1 if events else horizon
        if intro_latest_end < 0:
            raise CommentaryPlanContractError("commentary has no frame window for its match introduction")
        # TheSuperHackers @bugfix Leex 24/08/2026 Cite shared camera context when a player report omits replay-wide map-start claims. (#TBD)
        events.insert(0, self._intro_event(report, camera, intro_latest_end, horizon, partial))
        return self._allocate_speech_windows(events, camera, horizon)

    @staticmethod
    def _intro_event(
        report: PublishedReportGraphDTO,
        camera: CameraPlanV1,
        latest_end_frame: int,
        horizon: int,
        partial: bool,
    ) -> CommentaryEventV1:
        segment = next((item for item in camera.segments if item.start_frame == 0), None)
        if segment is None or not segment.evidence:
            raise CommentaryPlanContractError("commentary introduction requires cited camera context at frame zero")
        players = report.identity.players
        player_text = " versus ".join(player.display_name for player in players)
        map_name = _spoken_map_name(report.identity.map_name or report.identity.label)
        if partial and horizon <= camera.authority.logic_frames_per_second * 4:
            # TheSuperHackers @feature Leex 24/08/2026 Keep very short diagnostic previews audible without truncating narration. (#TBD)
            text = f"{map_name}. Preview."
        elif partial:
            text = f"{map_name}: {player_text}. Diagnostic opening through frame {horizon}."
        else:
            text = f"Welcome to {map_name}. {' and '.join(player.display_name for player in players)} are on the field."
        evidence_ids = ",".join(item.evidence_public_id for item in segment.evidence)
        event_id = str(
            uuid5(
                _NAMESPACE,
                f"{camera.authority.replay_public_id}:{camera.authority.report_public_id}:intro:0:{latest_end_frame}:{evidence_ids}",
            )
        )
        return CommentaryEventV1(
            event_id=event_id,
            start_frame=0,
            latest_end_frame=latest_end_frame,
            text=text,
            subtitle_text=text,
            role="intro",
            player_public_ids=tuple(player.public_id for player in players),
            strategy_identity=None,
            evidence=segment.evidence,
            confidence_tier=_tier(segment.evidence),
            camera_segment_id=segment.segment_id,
            template_version=_TEMPLATE_VERSION,
        )

    @staticmethod
    def _allocate_speech_windows(
        events: list[CommentaryEventV1], camera: CameraPlanV1, horizon: int
    ) -> list[CommentaryEventV1]:
        segments = {segment.segment_id: segment for segment in camera.segments}
        scheduled: list[CommentaryEventV1] = []
        for index, event in enumerate(events):
            latest_end = events[index + 1].start_frame - 1 if index + 1 < len(events) else horizon
            segment = segments.get(event.camera_segment_id)
            if segment is None:
                raise CommentaryPlanContractError("commentary references an unknown camera segment")
            # TheSuperHackers @bugfix Leex 24/08/2026 Keep each spoken event inside its cited camera shot. (#TBD)
            latest_end = min(latest_end, segment.end_frame)
            if latest_end < event.start_frame:
                raise CommentaryPlanContractError("commentary evidence anchors leave no non-overlapping speech window")
            payload = event.model_dump(mode="python")
            payload["latest_end_frame"] = latest_end
            scheduled.append(CommentaryEventV1.model_validate(payload))
        return CommentaryPlanService._deduplicate_nonoverlapping(scheduled)

    @staticmethod
    def _render_claim(
        claim: _Claim, player_names: dict[str, str]
    ) -> tuple[Literal["play_by_play", "analysis", "outro"], str, tuple[str, ...], str | None] | None:
        value = claim.value
        raw = _mapping(value.raw_value)
        if value.section == "strategy" or value.claim_id.startswith("strategy:"):
            player_id = raw.get("player_public_id")
            player_name = player_names.get(player_id, "The player") if isinstance(player_id, str) else "The player"
            strategy = raw.get("strategy_label", value.label)
            if not isinstance(strategy, str):
                strategy = value.label
            return "analysis", f"{player_name} opens with {strategy}.", (player_id,) if isinstance(player_id, str) else (), strategy
        if value.section in ("build_order", "production", "economy"):
            return "play_by_play", f"{value.label} marks a key development.", (), None
        if value.section == "combat" or "engagement" in value.claim_id:
            return "play_by_play", f"{value.label} becomes the key fight.", (), None
        if value.section == "outcome" or value.claim_id.startswith("outcome."):
            winner = raw.get("winner")
            winner_name = player_names.get(winner) if isinstance(winner, str) else None
            if winner_name is None:
                return None
            winner_id = cast(str, winner)
            return "outro", f"The accepted result is {winner_name} wins.", (winner_id,), None
        return None

    def _event(
        self,
        camera: CameraPlanV1,
        claim: _Claim,
        role: Literal["intro", "play_by_play", "analysis", "transition", "outro"],
        text: str,
        player_ids: tuple[str, ...],
        strategy: str | None,
    ) -> CommentaryEventV1:
        evidence_ids = ",".join(item.evidence_public_id for item in claim.citations)
        event_id = str(uuid5(_NAMESPACE, f"{camera.authority.replay_public_id}:{claim.value.claim_id}:{role}:{claim.start_frame}:{claim.end_frame}:{evidence_ids}"))
        return CommentaryEventV1(
            event_id=event_id,
            start_frame=claim.end_frame,
            latest_end_frame=claim.end_frame,
            text=text,
            subtitle_text=text,
            role=role,
            player_public_ids=player_ids,
            strategy_identity=strategy,
            evidence=claim.citations,
            confidence_tier=_tier(claim.citations),
            camera_segment_id=_camera_segment_id(camera, claim.start_frame, claim.end_frame),
            template_version=_TEMPLATE_VERSION,
        )

    @staticmethod
    def _deduplicate_nonoverlapping(events: list[CommentaryEventV1]) -> list[CommentaryEventV1]:
        output: list[CommentaryEventV1] = []
        for event in events:
            if output and event.start_frame <= output[-1].latest_end_frame:
                raise CommentaryPlanContractError("supported commentary claim windows overlap")
            output.append(event)
        return output

    @staticmethod
    def _apply_enrichment(
        plan: CommentaryPlanV1, enrichment: ValidatedCommentaryEnrichmentV1 | None
    ) -> CommentaryPlanV1:
        if enrichment is None:
            return plan
        by_event = {item.event_id: item for item in plan.events}
        replacements: dict[str, CommentaryEnrichmentSentenceV1] = {}
        for sentence in enrichment.sentences:
            event = by_event.get(sentence.event_id)
            if event is None or sentence.frame_start != event.start_frame or sentence.frame_end != event.latest_end_frame:
                return plan
            if sentence.evidence_public_ids != tuple(item.evidence_public_id for item in event.evidence):
                return plan
            replacements[event.event_id] = sentence
        events = tuple(
            event.model_copy(update={
                "text": replacements[event.event_id].text,
                "subtitle_text": replacements[event.event_id].text,
                "ollama_run_public_id": enrichment.ollama_run_public_id,
            }) if event.event_id in replacements else event
            for event in plan.events
        )
        return plan.model_copy(update={"events": events})
