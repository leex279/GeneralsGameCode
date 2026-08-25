"""Deterministic, evidence-bound commentary plans for replay broadcasts."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from generals_replay_analyzer.report.model import CanonicalValue, ReportValue, freeze_report_value, thaw_report_value
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
    end = min(end, horizon)
    if start > end:
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
        if start > horizon:
            continue
        if start == 0 and end > 0:
            continue
        output.append(_Claim(value, start, min(end, horizon), _claim_evidence(value, horizon)))
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


def _friendly_identity(value: str) -> str:
    cleaned = value
    for prefix in ("AirF_America", "AFG_America", "America", "China", "GLA"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    for category in ("Infantry", "Vehicle", "Tank", "Building"):
        if cleaned.startswith(category):
            cleaned = cleaned[len(category) :]
            break
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", cleaned).replace("_", " ").strip()
    return words or value


def _strategy_anchor(label: str) -> tuple[str, int, Literal["build", "observed"]] | None:
    anchors: dict[str, tuple[str, int, Literal["build", "observed"]]] = {
        "gla_forward_tunnel_pressure": ("TunnelNetwork", 1, "build"),
        "gla_technical_aggression": ("VehicleTechnical", 1, "observed"),
        "gla_terror_tech": ("InfantryTerrorist", 1, "observed"),
        "gla_dual_arms_dealer_pressure": ("ArmsDealer", 2, "build"),
        "gla_fast_palace": ("Palace", 1, "build"),
    }
    return anchors.get(label)


def _strategy_text(player_name: str, label: str) -> str:
    text = {
        "gla_forward_tunnel_pressure": f"{player_name} establishes forward Tunnel pressure.",
        "gla_technical_aggression": f"{player_name} reveals early Technical aggression.",
        "gla_terror_tech": f"{player_name} assembles the Terror Tech threat.",
        "gla_dual_arms_dealer_pressure": f"{player_name} commits to dual Arms Dealer pressure.",
        "gla_fast_palace": f"{player_name} unlocks fast Palace tech.",
    }
    return text.get(label, f"{player_name} reveals {_friendly_identity(label)}.")


def _point_citations(
    value: ReportValue, frame: int, *, include_derived: bool = False
) -> tuple[EvidenceCitationV1, ...]:
    references = [
        item
        for item in value.evidence
        if item.tier in ("observed", "derived") and (include_derived or item.tier == "observed")
    ]
    references.sort(key=lambda item: (0 if item.tier == "derived" else 1, item.public_id))
    return tuple(
        EvidenceCitationV1(
            evidence_public_id=item.public_id,
            tier=item.tier,
            frame_start=0 if item.tier == "derived" else frame,
            frame_end=frame,
        )
        for item in references[:16]
        if item.tier in ("observed", "derived")
    )


def _timed_feature_claims(report: PublishedReportGraphDTO, horizon: int, logic_hz: int) -> tuple[_Claim, ...]:
    document = report.selected.document
    default_player_id = document.replay_player_public_id
    observed_templates: dict[str, list[ReportValue]] = {}
    for value in document.observed:
        if value.availability != "available" or value.frame_window is None or value.frame_window[0] <= 0:
            continue
        raw = _mapping(value.raw_value)
        template = raw.get("template_name")
        if isinstance(template, str):
            observed_templates.setdefault(template, []).append(value)
    for values in observed_templates.values():
        values.sort(key=lambda item: (item.frame_window or (horizon + 1, horizon + 1), item.claim_id))

    build_sequences = [
        value
        for value in document.derived
        if value.availability == "available" and value.label == "build.completed_sequence"
    ]
    build_templates: dict[str, list[tuple[int, ReportValue]]] = {}
    for value in build_sequences:
        raw_sequence = thaw_report_value(value.raw_value)
        if not isinstance(raw_sequence, list):
            continue
        for item in raw_sequence:
            if not isinstance(item, dict):
                continue
            frame = item.get("frame")
            template = item.get("template_name")
            if type(frame) is int and 0 < frame <= horizon and isinstance(template, str):
                build_templates.setdefault(template, []).append((frame, value))

    strategy_claims: list[_Claim] = []
    occupied_frames: set[int] = set()
    for value in document.derived:
        if value.availability != "available" or value.section != "strategy":
            continue
        raw = _mapping(value.raw_value)
        strategy = raw.get("strategy_label", value.label)
        if not isinstance(strategy, str):
            continue
        anchor = _strategy_anchor(strategy)
        if anchor is None:
            continue
        template_token, occurrence, source_kind = anchor
        anchor_matches: list[tuple[int, str, ReportValue]]
        if source_kind == "build":
            anchor_matches = [
                (frame, template, source)
                for template, candidates in build_templates.items()
                if template_token in template
                for frame, source in candidates
            ]
        else:
            anchor_matches = [
                (candidate.frame_window[0], template, candidate)
                for template, candidates in observed_templates.items()
                if template_token in template
                for candidate in candidates
                if candidate.frame_window is not None and candidate.frame_window[0] <= horizon
            ]
        # TheSuperHackers @bugfix Leex 25/08/2026 Break equal strategy anchors by stable claim identity instead of comparing report objects. (#TBD)
        anchor_matches.sort(key=lambda item: (item[0], item[1], item[2].claim_id))
        if len(anchor_matches) < occurrence:
            continue
        frame, matched_template, matched = anchor_matches[occurrence - 1]
        observed_citations = _point_citations(matched, frame, include_derived=source_kind == "build")
        derived_citations = _point_citations(value, frame, include_derived=True)
        strategy_citations = tuple(dict.fromkeys((*observed_citations, *derived_citations)))[:16]
        if not strategy_citations:
            continue
        synthetic = replace(
            value,
            claim_id=f"{value.claim_id}:commentary:{frame}",
            frame_window=(frame, frame),
            raw_value=freeze_report_value(
                {
                    "player_public_id": default_player_id,
                    "strategy_label": strategy,
                    "template_name": matched_template,
                }
            ),
            evidence=tuple(matched.evidence),
        )
        strategy_claims.append(_Claim(synthetic, frame, frame, strategy_citations))
        occupied_frames.add(frame)

    build_claims: list[_Claim] = []
    seen_templates: set[str] = set()
    for value in build_sequences:
        raw_sequence = thaw_report_value(value.raw_value)
        if not isinstance(raw_sequence, list):
            continue
        for item in raw_sequence:
            if not isinstance(item, dict):
                continue
            frame = item.get("frame")
            template = item.get("template_name")
            if type(frame) is not int or frame <= 0 or frame > horizon or not isinstance(template, str):
                continue
            if frame in occupied_frames or template in seen_templates:
                continue
            observed_matches = [
                candidate
                for candidate in observed_templates.get(template, [])
                if candidate.frame_window == (frame, frame)
            ]
            citation_source = observed_matches[0] if observed_matches else value
            build_citations = _point_citations(citation_source, frame, include_derived=not observed_matches)
            if not build_citations:
                continue
            synthetic = replace(
                value,
                claim_id=f"{value.claim_id}:commentary:{frame}:{template}",
                section="build_order",
                label=_friendly_identity(template),
                raw_value=freeze_report_value({"player_public_id": default_player_id, "template_name": template}),
                frame_window=(frame, frame),
                evidence=tuple(citation_source.evidence),
            )
            build_claims.append(_Claim(synthetic, frame, frame, build_citations))
            seen_templates.add(template)
            occupied_frames.add(frame)
            if len(build_claims) >= 10:
                break

    combat_claims: list[_Claim] = []
    specific_kill_claims: list[_Claim] = []
    for value in document.derived:
        if value.availability != "available" or value.label != "combat.observed_kill_timing":
            continue
        raw_kills = thaw_report_value(value.raw_value)
        if not isinstance(raw_kills, list):
            continue
        for item in raw_kills:
            if not isinstance(item, dict):
                continue
            frame = item.get("frame")
            attacker = item.get("attacker_template_name")
            victim = item.get("victim_template_name")
            if type(frame) is not int or frame <= 0 or frame > horizon:
                continue
            if not isinstance(attacker, str) or not isinstance(victim, str):
                continue
            citations = _point_citations(value, frame, include_derived=True)
            if not citations:
                continue
            specific_kill_claims.append(
                _Claim(
                    replace(
                        value,
                        claim_id=f"{value.claim_id}:commentary:{frame}:{attacker}:{victim}",
                        section="combat",
                        raw_value=freeze_report_value(
                            {
                                "frame": frame,
                                "attacker_template_name": attacker,
                                "victim_template_name": victim,
                            }
                        ),
                        frame_window=(frame, frame),
                    ),
                    frame,
                    frame,
                    citations,
                )
            )
    kill_bins: dict[int, list[_Claim]] = {}
    for claim in specific_kill_claims:
        kill_bins.setdefault(claim.start_frame // (logic_hz * 30), []).append(claim)
    for kill_values in kill_bins.values():
        kill_values.sort(key=lambda claim: (claim.start_frame, claim.value.claim_id))
    ranked_kill_bins = sorted(
        kill_bins.values(),
        key=lambda values: (-len(values), values[-1].start_frame, values[-1].value.claim_id),
    )
    busiest_kill_bins = ranked_kill_bins[:5]
    # TheSuperHackers @bugfix Leex 25/08/2026 Reserve one sparse commentary slot for the latest supported combat fact. (#TBD)
    if ranked_kill_bins:
        latest_kill_bin = max(
            ranked_kill_bins,
            key=lambda values: (values[-1].start_frame, values[-1].value.claim_id),
        )
        if busiest_kill_bins and all(values is not latest_kill_bin for values in busiest_kill_bins):
            busiest_kill_bins[-1] = latest_kill_bin
    specific_kill_claims = [
        values[-1]
        for values in sorted(
            busiest_kill_bins,
            key=lambda values: (values[-1].start_frame, values[-1].value.claim_id),
        )
    ]
    combat_claims.extend(specific_kill_claims)
    destruction_bins: dict[int, list[ReportValue]] = {}
    for value in document.observed:
        if value.availability == "available" and value.label == "object_destroyed" and value.frame_window is not None:
            frame = value.frame_window[0]
            if 0 < frame <= horizon - logic_hz * 60:
                destruction_bins.setdefault(frame // 1800, []).append(value)
    busiest = sorted(
        (values for values in destruction_bins.values() if len(values) >= 4),
        key=lambda values: (-len(values), values[0].frame_window or (horizon, horizon)),
    )[:5]
    for values in sorted(busiest, key=lambda items: items[-1].frame_window or (horizon, horizon)):
        values.sort(key=lambda item: (item.frame_window or (horizon, horizon), item.claim_id))
        final_window = values[-1].frame_window
        assert final_window is not None
        frame = final_window[1]
        combat_citations: list[EvidenceCitationV1] = []
        for source in values[-16:]:
            source_window = source.frame_window
            assert source_window is not None
            combat_citations.extend(_point_citations(source, source_window[1]))
        # TheSuperHackers @bugfix Leex 25/08/2026 Prefer engine-observed attacker and victim identities over repetitive cumulative destruction narration. (#TBD)
        if specific_kill_claims or frame in occupied_frames or not combat_citations:
            continue
        synthetic = replace(
            values[-1],
            claim_id=f"combat:destruction_cluster:{frame}",
            section="combat",
            label=f"{len(values)} confirmed destructions",
            raw_value=freeze_report_value({"destruction_count": len(values)}),
            frame_window=(frame, frame),
            evidence=tuple(reference for source in values[-16:] for reference in source.evidence),
        )
        combat_claims.append(_Claim(synthetic, frame, frame, tuple(combat_citations[:16])))
        occupied_frames.add(frame)

    return tuple(
        sorted(
            (*strategy_claims, *build_claims, *combat_claims), key=lambda item: (item.start_frame, item.value.claim_id)
        )
    )


def _camera_segment_id(camera: CameraPlanV1, start: int, end: int) -> str:
    for segment in camera.segments:
        if segment.start_frame <= start and end <= segment.end_frame:
            return segment.segment_id
    raise CommentaryPlanContractError("commentary event is not covered by a camera segment")


def _minimum_speech_frames(text: str, logic_hz: int) -> int:
    return int((len(text.split()) * 0.6 + 0.8) * logic_hz)


def _speech_anchor(camera: CameraPlanV1, claim: _Claim, text: str) -> _Claim | None:
    if camera.authority.evidence_horizon.frame_end <= camera.authority.logic_frames_per_second * 10:
        return claim
    minimum_frames = _minimum_speech_frames(text, camera.authority.logic_frames_per_second)
    for segment in camera.segments:
        start = max(claim.end_frame, segment.start_frame)
        if start <= segment.end_frame and segment.end_frame - start + 1 >= minimum_frames:
            return _Claim(claim.value, start, start, claim.citations)
    return None


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
        claims = (
            *_claims(report, horizon),
            *_timed_feature_claims(report, horizon, camera.authority.logic_frames_per_second),
        )
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
        if (
            report.replay_public_id != authority.replay_public_id
            or document.replay_public_id != authority.replay_public_id
        ):
            raise CommentaryPlanContractError("report and camera replay identities differ")
        if (
            document.report_public_id != authority.report_public_id
            or report.selected_report_public_id != authority.report_public_id
        ):
            raise CommentaryPlanContractError("report and camera report identities differ")
        if document.replay_sha256 != authority.replay_sha256:
            raise CommentaryPlanContractError("report and camera replay hashes differ")

    def _deterministic_events(
        self, report: PublishedReportGraphDTO, camera: CameraPlanV1, claims: tuple[_Claim, ...], horizon: int
    ) -> list[CommentaryEventV1]:
        players = report.identity.players
        player_names = {player.public_id: player.display_name for player in players}
        default_player_id = report.selected.document.replay_player_public_id
        events: list[CommentaryEventV1] = []
        partial = report.identity.duration_frames is not None and horizon < report.identity.duration_frames - 2
        occupied_frames: set[int] = set()
        spoken_production: set[str] = set()
        ordered_claims = sorted(
            claims,
            key=lambda item: (
                item.end_frame,
                0 if item.value.section == "strategy" else 1 if item.value.section == "build_order" else 2,
                item.value.claim_id,
            ),
        )
        for claim in ordered_claims:
            if claim.start_frame == 0:
                continue
            if claim.end_frame in occupied_frames:
                continue
            rendered = self._render_claim(claim, player_names, default_player_id)
            if rendered is None:
                continue
            role, text, player_ids, strategy = rendered
            raw = _mapping(claim.value.raw_value)
            if claim.value.section == "timeline" and claim.value.label == "production_completed":
                template = raw.get("template_name")
                if isinstance(template, str) and template in spoken_production:
                    continue
                if isinstance(template, str):
                    spoken_production.add(template)
            anchored = _speech_anchor(camera, claim, text)
            if anchored is None or anchored.end_frame in occupied_frames:
                continue
            events.append(self._event(camera, anchored, role, text, player_ids, strategy))
            occupied_frames.add(anchored.end_frame)
        events.sort(key=lambda item: (item.start_frame, item.event_id))
        events = self._keep_speakable_events(events, camera)
        events = self._ensure_terminal_exchange(events, camera, horizon)
        intro_latest_end = events[0].start_frame - 1 if events else horizon
        if intro_latest_end < 0:
            raise CommentaryPlanContractError("commentary has no frame window for its match introduction")
        # TheSuperHackers @bugfix Leex 24/08/2026 Cite shared camera context when a player report omits replay-wide map-start claims. (#TBD)
        events.insert(0, self._intro_event(report, camera, intro_latest_end, horizon, partial))
        return self._allocate_speech_windows(events, camera, horizon)

    @staticmethod
    def _keep_speakable_events(events: list[CommentaryEventV1], camera: CameraPlanV1) -> list[CommentaryEventV1]:
        if camera.authority.evidence_horizon.frame_end <= camera.authority.logic_frames_per_second * 10:
            return events
        segments = {segment.segment_id: segment for segment in camera.segments}
        selected: list[CommentaryEventV1] = []
        next_start: int | None = None
        for event in reversed(events):
            segment = segments[event.camera_segment_id]
            latest_end = segment.end_frame if next_start is None else min(segment.end_frame, next_start - 1)
            # TheSuperHackers @bugfix Leex 25/08/2026 Keep evidence commentary sparse enough for measured narration to fit before camera cuts. (#TBD)
            minimum_frames = _minimum_speech_frames(event.text, camera.authority.logic_frames_per_second)
            if latest_end - event.start_frame + 1 < minimum_frames:
                continue
            selected.append(event)
            next_start = event.start_frame
        return list(reversed(selected))

    @staticmethod
    def _ensure_terminal_exchange(
        events: list[CommentaryEventV1], camera: CameraPlanV1, horizon: int
    ) -> list[CommentaryEventV1]:
        logic_hz = camera.authority.logic_frames_per_second
        if horizon <= logic_hz * 10 or (
            events and events[-1].start_frame >= horizon - logic_hz * 30
        ):
            return events
        target_start = horizon - logic_hz * 25
        segment = next(
            (
                item
                for item in reversed(camera.segments)
                if item.focus_kind == "damage"
                and item.evidence
                and item.start_frame <= target_start <= item.end_frame
            ),
            None,
        )
        if segment is None:
            return events
        start_frame = max(
            segment.start_frame,
            target_start,
            max(item.frame_end for item in segment.evidence) + 1,
            0 if not events else events[-1].latest_end_frame + 1,
        )
        text = "A late confirmed exchange sets up the closing moments."
        if segment.end_frame - start_frame + 1 < _minimum_speech_frames(text, logic_hz):
            return events
        evidence_ids = ",".join(item.evidence_public_id for item in segment.evidence)
        event_id = str(
            uuid5(
                _NAMESPACE,
                f"{camera.authority.replay_public_id}:{camera.authority.report_public_id}:terminal-exchange:{start_frame}:{segment.end_frame}:{evidence_ids}",
            )
        )
        # TheSuperHackers @feature Leex 25/08/2026 Close long casts with a late evidence-cited battle call inside the production silence budget. (#TBD)
        return [
            *events,
            CommentaryEventV1(
                event_id=event_id,
                start_frame=start_frame,
                latest_end_frame=segment.end_frame,
                text=text,
                subtitle_text=text,
                role="outro",
                player_public_ids=(),
                strategy_identity=None,
                evidence=segment.evidence,
                confidence_tier=_tier(segment.evidence),
                camera_segment_id=segment.segment_id,
                template_version=_TEMPLATE_VERSION,
            ),
        ]

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
        claim: _Claim, player_names: dict[str, str], default_player_id: str | None
    ) -> tuple[Literal["play_by_play", "analysis", "outro"], str, tuple[str, ...], str | None] | None:
        value = claim.value
        raw = _mapping(value.raw_value)
        if value.section == "strategy" or value.claim_id.startswith("strategy:"):
            player_id = raw.get("player_public_id", default_player_id)
            player_name = player_names.get(player_id, "The player") if isinstance(player_id, str) else "The player"
            strategy = raw.get("strategy_label", value.label)
            if not isinstance(strategy, str):
                strategy = value.label
            return (
                "analysis",
                _strategy_text(player_name, strategy),
                (player_id,) if isinstance(player_id, str) else (),
                strategy,
            )
        if value.section == "timeline" and value.label == "production_completed":
            template = raw.get("template_name")
            if not isinstance(template, str):
                return None
            player_id = default_player_id
            player_name = player_names.get(player_id, "The player") if isinstance(player_id, str) else "The player"
            return (
                "play_by_play",
                f"{player_name} fields the {_friendly_identity(template)}, expanding the army composition.",
                (player_id,) if isinstance(player_id, str) else (),
                None,
            )
        if value.section in ("build_order", "production", "economy"):
            template = raw.get("template_name")
            if isinstance(template, str):
                player_id = raw.get("player_public_id", default_player_id)
                player_name = player_names.get(player_id, "The player") if isinstance(player_id, str) else "The player"
                return (
                    "play_by_play",
                    f"{player_name} completes the {_friendly_identity(template)}.",
                    (player_id,) if isinstance(player_id, str) else (),
                    None,
                )
            return "play_by_play", f"{value.label} marks a key development.", (), None
        if value.section == "combat" or "engagement" in value.claim_id:
            attacker = raw.get("attacker_template_name")
            victim = raw.get("victim_template_name")
            if isinstance(attacker, str) and isinstance(victim, str):
                return (
                    "play_by_play",
                    f"The {_friendly_identity(attacker)} scores a confirmed kill on the {_friendly_identity(victim)}.",
                    (),
                    None,
                )
            destruction_count = raw.get("destruction_count")
            if type(destruction_count) is int:
                return (
                    "play_by_play",
                    f"Major exchange: {destruction_count} confirmed destructions in thirty seconds.",
                    (),
                    None,
                )
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
        event_id = str(
            uuid5(
                _NAMESPACE,
                f"{camera.authority.replay_public_id}:{claim.value.claim_id}:{role}:{claim.start_frame}:{claim.end_frame}:{evidence_ids}",
            )
        )
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
            if (
                event is None
                or sentence.frame_start != event.start_frame
                or sentence.frame_end != event.latest_end_frame
            ):
                return plan
            if sentence.evidence_public_ids != tuple(item.evidence_public_id for item in event.evidence):
                return plan
            replacements[event.event_id] = sentence
        events = tuple(
            event.model_copy(
                update={
                    "text": replacements[event.event_id].text,
                    "subtitle_text": replacements[event.event_id].text,
                    "ollama_run_public_id": enrichment.ollama_run_public_id,
                }
            )
            if event.event_id in replacements
            else event
            for event in plan.events
        )
        return plan.model_copy(update={"events": events})
