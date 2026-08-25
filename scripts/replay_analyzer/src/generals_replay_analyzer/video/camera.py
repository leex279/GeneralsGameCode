"""Deterministic evidence-backed camera planning for native replay playback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from generals_replay_analyzer.features.evidence import thaw_canonical
from generals_replay_analyzer.report.model import ReportValue
from generals_replay_analyzer.report.read_model import PublishedReportGraphDTO
from generals_replay_analyzer.spatial.query import MapSceneReadModel
from generals_replay_analyzer.video.contracts import (
    CameraFocusKind,
    CameraPlanAuthorityV1,
    CameraPlanV1,
    CameraSegmentV1,
    EvidenceCitationV1,
)
from generals_replay_analyzer.web.ports import MapSceneDTO

_NAMESPACE = uuid5(NAMESPACE_URL, "generals-replay-analyzer:camera-plan-v1")
_PRIORITY: dict[CameraFocusKind, int] = {
    "engagement": 0,
    "damage": 1,
    "attack_order": 2,
    "milestone": 3,
    "resource_contest": 4,
    "base_context": 5,
}
# TheSuperHackers @feature Leex 25/08/2026 Hold native replay shots long enough to show a meaningful nearby scene instead of hopping across repeated damage samples. (#TBD)
_COOLDOWN_SECONDS = 15


class CameraPlanContractError(ValueError):
    """Fixed report, scene, or authority cannot produce a truthful camera plan."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    frame: int
    kind: CameraFocusKind
    label: str
    x: float
    y: float
    z: float
    evidence: tuple[EvidenceCitationV1, ...]
    zoom: float = 1.0
    two_sided_damage: bool = False


def _wins_cooldown(candidate: _Candidate, current: _Candidate) -> bool:
    # TheSuperHackers @feature Leex 25/08/2026 Prefer a two-sided battle view only inside its local camera cooldown conflict set. (#TBD)
    if candidate.two_sided_damage != current.two_sided_damage:
        return candidate.two_sided_damage
    return candidate.frame == current.frame and _PRIORITY[candidate.kind] < _PRIORITY[current.kind]


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CameraPlanContractError(f"{label} must be a mapping")
    return cast(dict[str, object], value)


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise CameraPlanContractError(f"{label} must be a list")
    return cast(list[object], value)


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CameraPlanContractError(f"{label} must be a nonnegative integer")
    return value


def _number(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise CameraPlanContractError(f"{label} must be numeric")
    result = float(cast(int | float, value))
    if not math.isfinite(result) or (result == 0.0 and math.copysign(1.0, result) < 0):
        raise CameraPlanContractError(f"{label} must be finite")
    return result


def _report_evidence(
    report: PublishedReportGraphDTO,
) -> dict[str, tuple[str, tuple[tuple[int, int], ...]]]:
    collected: dict[str, tuple[str, set[tuple[int, int]]]] = {}

    def collect(value: object) -> None:
        if type(value) is not ReportValue:
            return
        frame_start, frame_end = (0, 0) if value.frame_window is None else value.frame_window
        for evidence in value.evidence:
            if evidence.tier not in ("observed", "derived"):
                continue
            prior = collected.get(evidence.public_id)
            if prior is None:
                collected[evidence.public_id] = (evidence.tier, {(frame_start, frame_end)})
            elif prior[0] != evidence.tier:
                raise CameraPlanContractError("fixed report assigns conflicting evidence tiers")
            else:
                prior[1].add((frame_start, frame_end))

    # TheSuperHackers @bugfix Leex 24/08/2026 Bind camera events to the selected report while sharing only replay-wide frame-zero context. (#TBD)
    document = report.selected.document
    for value in (*document.evidence_availability, *document.observed, *document.derived):
        collect(value)
    if document.report_public_id != report.replay_wide.document.report_public_id:
        replay_wide = report.replay_wide.document
        for value in (*replay_wide.evidence_availability, *replay_wide.observed, *replay_wide.derived):
            if type(value) is ReportValue and value.frame_window in (None, (0, 0)):
                collect(value)
    return {
        public_id: (tier, tuple(sorted(windows)))
        for public_id, (tier, windows) in sorted(collected.items())
    }


def _position(item: dict[str, object], field: str) -> tuple[float, float, float]:
    holder = _mapping(item.get(field), field)
    raw = _mapping(holder.get("raw"), f"{field} raw position")
    return (_number(raw.get("x"), "position x"), _number(raw.get("y"), "position y"), _number(raw.get("z"), "position z"))


def _citations(
    item: dict[str, object],
    accepted: dict[str, tuple[str, tuple[tuple[int, int], ...]]],
    horizon: int,
    event_start: int,
    event_end: int,
) -> tuple[EvidenceCitationV1, ...]:
    output: list[EvidenceCitationV1] = []
    for raw_reference in _sequence(item.get("evidence"), "camera evidence"):
        reference = _mapping(raw_reference, "camera evidence reference")
        public_id = reference.get("evidence_public_id")
        if not isinstance(public_id, str) or public_id not in accepted:
            raise CameraPlanContractError("camera candidate cites evidence outside the fixed report")
        tier, windows = accepted[public_id]
        if reference.get("tier") != tier:
            raise CameraPlanContractError("camera candidate evidence tier disagrees with the fixed report")
        support_role = reference.get("support_role")
        observed_frame = reference.get("observed_frame")
        if support_role is not None:
            if support_role not in ("event_timing", "position") or type(observed_frame) is not int:
                raise CameraPlanContractError("role-aware camera evidence is malformed")
            if support_role == "event_timing" and not event_start <= observed_frame <= event_end:
                raise CameraPlanContractError("camera event timing evidence disagrees with the candidate frame")
            citation_start = observed_frame
            citation_end = observed_frame
        else:
            if observed_frame is not None:
                raise CameraPlanContractError("camera evidence frame requires an explicit support role")
            citation_start = event_start
            citation_end = event_end
        # The report may close an evidence window on a post-update boundary that
        # has no presentable camera frame.  Keep the event bounds strict, but
        # clip only the citation's accepted end to the last presentable frame.
        # TheSuperHackers @bugfix Leex 25/08/2026 Clip report citation ends to the presentable camera horizon without accepting out-of-horizon events. (#TBD)
        covering = tuple(
            (window[0], min(window[1], horizon))
            for window in windows
            if window[0] <= citation_start and citation_end <= min(window[1], horizon)
        )
        if not covering:
            raise CameraPlanContractError("camera candidate frame is outside its cited evidence window")
        frame_start, frame_end = min(
            covering,
            key=lambda window: (window[1] - window[0], window[0], window[1]),
        )
        output.append(
            EvidenceCitationV1(
                evidence_public_id=public_id,
                tier=cast(Literal["observed", "derived"], tier),
                frame_start=frame_start,
                frame_end=frame_end,
                support_role=support_role,
                observed_frame=observed_frame,
            )
        )
    # TheSuperHackers @feature Leex 25/08/2026 Keep camera event timing separate from the earlier observation that proves its spatial target. (#TBD)
    return tuple(
        sorted(
            set(output),
            key=lambda value: (
                value.observed_frame if value.observed_frame is not None else value.frame_start,
                value.support_role or "",
                value.evidence_public_id,
            ),
        )
    )


def _bounds(payload: dict[str, object]) -> tuple[float, float, float, float, float, float]:
    transforms = _mapping(payload.get("transforms"), "scene transforms")
    raw = _mapping(transforms.get("raw"), "scene raw transform")
    if raw.get("coordinate_version") != "engine-world-xyz-v1":
        raise CameraPlanContractError("scene raw coordinate contract is unsupported")
    minimum = _mapping(raw.get("minimum"), "scene minimum")
    maximum = _mapping(raw.get("maximum"), "scene maximum")
    values = (
        _number(minimum.get("x"), "minimum x"),
        _number(minimum.get("y"), "minimum y"),
        _number(minimum.get("z"), "minimum z"),
        _number(maximum.get("x"), "maximum x"),
        _number(maximum.get("y"), "maximum y"),
        _number(maximum.get("z"), "maximum z"),
    )
    if not values[0] < values[3] or not values[1] < values[4] or values[2] > values[5]:
        raise CameraPlanContractError("scene raw bounds are invalid")
    return values


def _inside_planar_map(position: tuple[float, float, float], bounds: tuple[float, float, float, float, float, float]) -> bool:
    x, y, _z = position
    min_x, min_y, _min_z, max_x, max_y, _max_z = bounds
    # TheSuperHackers @bugfix Leex 25/08/2026 Preserve engine-validated airspace targets while enforcing the authoritative planar map extent. (#TBD)
    return min_x <= x <= max_x and min_y <= y <= max_y


# TheSuperHackers @feature Leex 24/08/2026 Generate a gapless native camera plan only from report-accepted spatial evidence. (#TBD)
class CameraPlanService:
    def create(
        self,
        authority: CameraPlanAuthorityV1,
        report: PublishedReportGraphDTO,
        scene: MapSceneReadModel,
    ) -> CameraPlanV1:
        if type(authority) is not CameraPlanAuthorityV1:
            raise TypeError("authority must be CameraPlanAuthorityV1")
        if type(report) is not PublishedReportGraphDTO or type(scene) is not MapSceneReadModel:
            raise TypeError("camera inputs must use fixed report and scene read models")
        payload_value = thaw_canonical(scene.payload)
        try:
            validated_scene = MapSceneDTO.model_validate(payload_value)
        except ValidationError as exc:
            raise CameraPlanContractError("map scene does not satisfy replay-map-scene-v2") from exc
        payload = validated_scene.model_dump(mode="json")
        self._validate_authority(authority, report, payload)
        accepted = _report_evidence(report)
        horizon = authority.evidence_horizon.frame_end
        bounds = _bounds(payload)
        candidates = self._candidates(payload, accepted, horizon, bounds)
        if not candidates:
            raise CameraPlanContractError("camera plan has no report-cited spatial evidence")
        cooldown_frames = authority.logic_frames_per_second * _COOLDOWN_SECONDS
        selected: list[_Candidate] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (item.frame, _PRIORITY[item.kind], item.evidence[0].evidence_public_id),
        ):
            if (
                selected
                and selected[-1].kind != "base_context"
                and candidate.frame - selected[-1].frame < cooldown_frames
            ):
                if _wins_cooldown(candidate, selected[-1]):
                    selected[-1] = candidate
                continue
            selected.append(candidate)
        if selected[0].frame != 0:
            base = next((item for item in candidates if item.kind == "base_context"), None)
            if base is None:
                raise CameraPlanContractError("camera plan requires cited base context at frame zero")
            selected.insert(0, base)
        selected = [item for index, item in enumerate(selected) if index == 0 or item.frame > selected[index - 1].frame]
        segments: list[CameraSegmentV1] = []
        for index, candidate in enumerate(selected):
            start = 0 if index == 0 else candidate.frame
            end = horizon if index + 1 == len(selected) else selected[index + 1].frame - 1
            length = end - start + 1
            transition: Literal["cut", "ease"] = "cut" if index == 0 or length == 1 else "ease"
            transition_frames = 0 if transition == "cut" else min(30, length)
            if any(
                item.support_role is not None or item.observed_frame is not None
                for item in candidate.evidence
            ):
                evidence_ids = ",".join(
                    f"{item.evidence_public_id}:{item.support_role}:{item.observed_frame}"
                    for item in candidate.evidence
                )
            else:
                # TheSuperHackers @bugfix Leex 25/08/2026 Preserve legacy segment UUID seeds when citations have no role-aware provenance. (#TBD)
                evidence_ids = ",".join(
                    item.evidence_public_id for item in candidate.evidence
                )
            segment_id = str(
                uuid5(
                    _NAMESPACE,
                    f"{authority.replay_public_id}:{authority.telemetry_trace_sha256}:{start}:{end}:{candidate.kind}:{evidence_ids}",
                )
            )
            segments.append(
                CameraSegmentV1(
                    segment_id=segment_id,
                    start_frame=start,
                    end_frame=end,
                    target_x=candidate.x,
                    target_y=candidate.y,
                    target_z=candidate.z,
                    zoom=candidate.zoom,
                    pitch=-45.0,
                    yaw=0.0,
                    transition=transition,
                    transition_frames=transition_frames,
                    focus_kind=candidate.kind,
                    label=candidate.label,
                    evidence=candidate.evidence,
                )
            )
        return CameraPlanV1(authority=authority, segments=tuple(segments))

    @staticmethod
    def _validate_authority(
        authority: CameraPlanAuthorityV1,
        report: PublishedReportGraphDTO,
        payload: dict[str, object],
    ) -> None:
        if report.replay_public_id != authority.replay_public_id:
            raise CameraPlanContractError("report replay does not match camera authority")
        # TheSuperHackers @bugfix Leex 24/08/2026 Validate player-scoped casts against their selected immutable report. (#TBD)
        document = report.selected.document
        if document.report_public_id != authority.report_public_id or document.replay_sha256 != authority.replay_sha256:
            raise CameraPlanContractError("fixed report does not match camera authority")
        for key, expected in (
            ("schema_version", "replay-map-scene-v2"),
            ("replay_public_id", authority.replay_public_id),
            ("report_public_id", authority.report_public_id),
            ("telemetry_run_public_id", authority.telemetry_run_public_id),
            ("telemetry_trace_sha256", authority.telemetry_trace_sha256),
            ("map_public_id", authority.map_public_id),
            ("map_content_sha256", authority.map_content_sha256),
        ):
            if payload.get(key) != expected:
                raise CameraPlanContractError(f"scene {key} does not match camera authority")
        query = _mapping(payload.get("query"), "scene query")
        horizon = authority.evidence_horizon.frame_end
        if query.get("frame_start") != 0 or query.get("frame_end") != horizon:
            raise CameraPlanContractError("scene query must equal the accepted evidence horizon")
        if any(
            query.get(key) not in (None, [])
            for key in (
                "replay_player_public_ids",
                "entity_public_ids",
                "event_families",
                "locomotor_surface",
                "player_centric_subject_public_id",
            )
        ) or query.get("coordinate_display") != "raw":
            raise CameraPlanContractError("camera planning requires an unfiltered replay-wide raw scene")
        available = _mapping(payload.get("available_frame_window"), "available frame window")
        if available.get("frame_start") != 0 or not isinstance(available.get("frame_end"), int) or cast(int, available["frame_end"]) < horizon:
            raise CameraPlanContractError("scene does not cover the accepted evidence horizon")

    @staticmethod
    def _candidates(
        payload: dict[str, object],
        accepted: dict[str, tuple[str, tuple[tuple[int, int], ...]]],
        horizon: int,
        bounds: tuple[float, float, float, float, float, float],
    ) -> list[_Candidate]:
        output: list[_Candidate] = []
        definitions: tuple[tuple[str, CameraFocusKind, str, str], ...] = (
            ("starts", "base_context", "position", "Base context"),
            ("engagements", "engagement", "centroid", "Engagement"),
            ("casualties", "damage", "position", "Damage location"),
            ("orders", "attack_order", "target_position", "Attack order"),
            ("structures", "milestone", "position", "Production or tech milestone"),
            ("resources", "resource_contest", "position", "Resource context"),
        )
        for collection, kind, position_field, label in definitions:
            for raw_item in _sequence(payload.get(collection, []), f"scene {collection}"):
                item = _mapping(raw_item, f"scene {collection} item")
                if item.get(position_field) is None:
                    continue
                if kind == "base_context":
                    frame = 0
                    event_end = 0
                else:
                    raw_frame = item.get("frame_start", item.get("frame"))
                    if type(raw_frame) is not int:
                        continue
                    frame = _integer(raw_frame, f"{kind} frame")
                    event_end = _integer(item.get("frame_end", frame), f"{kind} end frame")
                if frame > horizon:
                    raise CameraPlanContractError("camera candidate exceeds the accepted horizon")
                if event_end < frame or event_end > horizon:
                    raise CameraPlanContractError("camera candidate frame window exceeds the accepted horizon")
                citations = _citations(item, accepted, horizon, frame, event_end)
                if not citations:
                    raise CameraPlanContractError("camera candidate has no fixed-report evidence")
                if (
                    kind == "milestone"
                    and item.get("source_kind") == "construction_completed"
                    and {citation.support_role for citation in citations}
                    != {"event_timing", "position"}
                ):
                    raise CameraPlanContractError(
                        "completed construction camera focus requires timing and position provenance"
                    )
                if kind == "milestone" and item.get("source_kind") == "construction_completed":
                    position_frames = tuple(
                        citation.observed_frame
                        for citation in citations
                        if citation.support_role == "position"
                        and citation.observed_frame is not None
                    )
                    timing_frames = tuple(
                        citation.observed_frame
                        for citation in citations
                        if citation.support_role == "event_timing"
                        and citation.observed_frame is not None
                    )
                    if any(
                        position_frame > timing_frame or position_frame > frame
                        for position_frame in position_frames
                        for timing_frame in timing_frames
                    ):
                        raise CameraPlanContractError(
                            "milestone position evidence cannot occur after event timing"
                        )
                position = _position(item, position_field)
                zoom = 1.0
                two_sided_damage = False
                # TheSuperHackers @feature Leex 25/08/2026 Frame opposing combat sides by their accepted spatial bounds and widen modestly for spread. (#TBD)
                if kind == "damage" and item.get("opposing_position") is not None:
                    two_sided_damage = True
                    opposing = _position(item, "opposing_position")
                    span = math.hypot(opposing[0] - position[0], opposing[1] - position[1])
                    position = (
                        (position[0] + opposing[0]) / 2.0,
                        (position[1] + opposing[1]) / 2.0,
                        (position[2] + opposing[2]) / 2.0,
                    )
                    map_span = max(bounds[3] - bounds[0], bounds[4] - bounds[1])
                    # TheSuperHackers @bugfix Leex 25/08/2026 Increase W3D height-based zoom to show both combat sides instead of zooming further in. (#TBD)
                    zoom = 1.05 + min(0.10, max(0.0, span / map_span * 0.15))
                if not _inside_planar_map(position, bounds):
                    raise CameraPlanContractError("cited camera position is outside authoritative map bounds")
                output.append(
                    _Candidate(
                        frame,
                        kind,
                        label,
                        *position,
                        citations,
                        zoom,
                        two_sided_damage,
                    )
                )
        return output
