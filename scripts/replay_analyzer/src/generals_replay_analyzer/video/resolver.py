"""Worker-only resolution of opaque replay cast job identities."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import ManagedAsset, Replay, TelemetryRun
from generals_replay_analyzer.features.evidence import thaw_canonical
from generals_replay_analyzer.report.query import FixedReportQuery, ReportQueryService
from generals_replay_analyzer.spatial.query import MapSceneQueryService, MapSceneReadQuery
from generals_replay_analyzer.video.contracts import CameraPlanAuthorityV1, EvidenceHorizonV1
from generals_replay_analyzer.video.render import VideoRenderRequest


class VideoResolutionError(ValueError):
    """The durable request does not resolve to one complete compatible authority."""


# TheSuperHackers @bugfix Leex 24/08/2026 Convert replay and telemetry post-update boundaries into the last presentable frame. (#TBD)
def _renderable_frame_end(frame_count: int, accepted_evidence_end: int) -> int:
    if type(frame_count) is not int or frame_count < 1:
        raise VideoResolutionError("replay has no presentable video frames")
    if type(accepted_evidence_end) is not int or accepted_evidence_end < 1:
        raise VideoResolutionError("accepted evidence horizon has no presentable video frames")
    return min(frame_count, accepted_evidence_end) - 1


# TheSuperHackers @fix Leex 24/08/2026 Refuse video timing that disagrees with imported engine clock authority. (#TBD)
def _resolve_authoritative_logic_fps(
    replay_header: object,
    telemetry_timebases: tuple[tuple[int, Mapping[str, object]], ...],
    *,
    requested: object | None = None,
) -> Literal[30, 60]:
    if requested is not None and requested not in (30, 60):
        raise VideoResolutionError("video job logic timebase is invalid")
    candidates: set[int] = set()
    header = replay_header if isinstance(replay_header, Mapping) else {}
    timebase = header.get("timebase")
    if isinstance(timebase, Mapping):
        fps = timebase.get("logic_frames_per_second")
        if timebase.get("source") == "engine_manifest" and fps in (30, 60):
            candidates.add(fps)
    has_v2 = False
    historical_v1 = False
    for schema_version, settings in telemetry_timebases:
        has_v2 = has_v2 or schema_version >= 2
        fps = settings.get("logic_frames_per_second")
        source = settings.get("logic_timebase_source")
        if source == "engine_manifest" and fps in (30, 60):
            candidates.add(fps)
        historical_v1 = historical_v1 or (
            schema_version == 1 and source == "historical_v1_contract" and fps == 30
        )
    if len(candidates) > 1:
        raise VideoResolutionError("conflicting engine logic timebase authorities")
    if candidates:
        authoritative = candidates.pop()
    elif historical_v1 and not has_v2:
        authoritative = 30
    else:
        raise VideoResolutionError("engine logic timebase is unavailable")
    if requested is not None and requested != authoritative:
        raise VideoResolutionError("video job logic timebase differs from engine authority")
    return cast(Literal[30, 60], authoritative)


# TheSuperHackers @fix Leex 24/08/2026 Validate every worker stage identity through the closed UUID and lowercase SHA contract. (#TBD)
def _authority_from_payload(
    replay_id: str,
    report_id: str,
    replay_sha256: str,
    payload: Mapping[str, object],
    accepted_end: int,
    logic_frames_per_second: Literal[30, 60],
) -> CameraPlanAuthorityV1:
    try:
        identities = {name: payload[name] for name in ("telemetry_run_public_id", "telemetry_trace_sha256", "map_public_id", "map_content_sha256")}
        if any(type(value) is not str for value in identities.values()):
            raise TypeError("stage identities must be strings")
        return CameraPlanAuthorityV1(
            replay_public_id=replay_id, replay_sha256=replay_sha256, report_public_id=report_id,
            telemetry_run_public_id=cast(str, identities["telemetry_run_public_id"]), telemetry_trace_sha256=cast(str, identities["telemetry_trace_sha256"]),
            map_public_id=cast(str, identities["map_public_id"]), map_content_sha256=cast(str, identities["map_content_sha256"]),
            evidence_horizon=EvidenceHorizonV1(frame_end=accepted_end),
            logic_frames_per_second=logic_frames_per_second,
        )
    except (KeyError, TypeError, ValidationError) as error:
        raise VideoResolutionError("map scene authority identities are invalid") from error


# TheSuperHackers @fix Leex 24/08/2026 Keep managed replay resolution inside the configured root after symlink resolution. (#TBD)
def _managed_replay_path(root: Path, relative_path: str) -> Path:
    candidate = (root / Path(*relative_path.split("/"))).resolve()
    managed_root = root.resolve()
    try:
        candidate.relative_to(managed_root)
    except ValueError as error:
        raise VideoResolutionError("managed replay path escapes configured root") from error
    return candidate


class VideoRequestResolver:
    """Resolve only worker-side storage and read models; Web never receives these capabilities."""

    def __init__(self, session_factory: sessionmaker[Session], settings: AnalyzerSettings) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._reports = ReportQueryService(session_factory, settings=settings)
        self._scenes = MapSceneQueryService(session_factory, self._reports)

    # TheSuperHackers @feature Leex 24/08/2026 Resolve cast authority from immutable replay, report, telemetry, and map evidence. (#TBD)
    def resolve(self, values: Mapping[str, object]) -> VideoRenderRequest:
        try:
            replay_id = values["replay_public_id"]
            replay_sha256 = values["replay_sha256"]
            report_id = values["report_public_id"]
            horizon = values["evidence_horizon"]
            preview = values["diagnostic_preview"]
            logic_frames_per_second = values["logic_frames_per_second"]
        except KeyError as error:
            raise VideoResolutionError("video job authority is incomplete") from error
        if (
            not isinstance(replay_id, str)
            or not isinstance(replay_sha256, str)
            or len(replay_sha256) != 64
            or any(character not in "0123456789abcdef" for character in replay_sha256)
            or not isinstance(report_id, str)
            or horizon not in {"complete", "partial"}
            or type(preview) is not bool
            or logic_frames_per_second not in {30, 60}
        ):
            raise VideoResolutionError("video job authority is invalid")
        graph = self._reports.get_report(FixedReportQuery(replay_id, report_id))
        with self._sessions() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == replay_id))
            if replay is None or replay.managed_asset_id is None:
                raise VideoResolutionError("managed replay is unavailable")
            # TheSuperHackers @bugfix Leex 24/08/2026 Reject durable jobs whose frozen replay digest differs from current storage authority. (#TBD)
            if replay.sha256 != replay_sha256:
                raise VideoResolutionError("video job replay SHA-256 differs from managed replay authority")
            asset = session.get(ManagedAsset, replay.managed_asset_id)
            if asset is None or asset.sha256 != replay_sha256:
                raise VideoResolutionError("managed replay identity is invalid")
            # TheSuperHackers @bugfix Leex 24/08/2026 Resolve managed-asset paths from their canonical product-root scope. (#TBD)
            replay_path = _managed_replay_path(self._settings.data_root, asset.relative_path)
            if not replay_path.is_file() or hashlib.sha256(replay_path.read_bytes()).hexdigest() != replay_sha256:
                raise VideoResolutionError("managed replay bytes are invalid")
            telemetry_timebases = tuple(
                (
                    run.schema_version,
                    run.settings_json if isinstance(run.settings_json, Mapping) else {},
                )
                for run in session.scalars(
                    select(TelemetryRun)
                    .where(TelemetryRun.replay_id == replay.id, TelemetryRun.status == "succeeded")
                    .order_by(TelemetryRun.run_id)
                )
            )
            logic_frames_per_second = _resolve_authoritative_logic_fps(
                replay.header_json,
                telemetry_timebases,
                requested=logic_frames_per_second,
            )
            frame_end = replay.frame_count
        # TheSuperHackers @bugfix Leex 24/08/2026 Direct diagnostic casts only across the report's verified telemetry horizon. (#TBD)
        canonical_scene = self._scenes.get_canonical_scene(MapSceneReadQuery(replay_id, report_id, 0, 0))
        # TheSuperHackers @bugfix Leex 24/08/2026 Validate the immutable scene's thawed mapping instead of its storage wrapper. (#TBD)
        payload = thaw_canonical(canonical_scene.payload)
        if not isinstance(payload, Mapping):
            raise VideoResolutionError("map scene is invalid")
        available = payload.get("available_frame_window")
        if not isinstance(available, Mapping) or available.get("frame_start") != 0 or type(available.get("frame_end")) is not int:
            raise VideoResolutionError("map scene horizon is invalid")
        accepted_end = available["frame_end"]
        if horizon == "complete" and accepted_end != frame_end:
            raise VideoResolutionError("production cast requires complete telemetry horizon")
        if horizon == "partial" and not preview:
            raise VideoResolutionError("partial cast requires diagnostic preview")
        renderable_end = _renderable_frame_end(frame_end, accepted_end)
        authority = _authority_from_payload(
            replay_id, report_id, replay_sha256, payload, renderable_end, logic_frames_per_second
        )
        # TheSuperHackers @bugfix Leex 24/08/2026 Bind camera input to the exact presentable window after validating the canonical telemetry horizon. (#TBD)
        scene = self._scenes.get_scene(MapSceneReadQuery(replay_id, report_id, 0, renderable_end))
        return VideoRenderRequest(authority=authority, report=graph, scene=scene, replay_path=replay_path)
