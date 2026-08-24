"""Worker-only resolution of opaque replay cast job identities."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import ManagedAsset, Replay
from generals_replay_analyzer.report.query import FixedReportQuery, ReportQueryService
from generals_replay_analyzer.spatial.query import MapSceneQueryService, MapSceneReadQuery
from generals_replay_analyzer.video.contracts import CameraPlanAuthorityV1, EvidenceHorizonV1
from generals_replay_analyzer.video.render import VideoRenderRequest


class VideoResolutionError(ValueError):
    """The durable request does not resolve to one complete compatible authority."""


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
            report_id = values["report_public_id"]
            horizon = values["evidence_horizon"]
            preview = values["diagnostic_preview"]
        except KeyError as error:
            raise VideoResolutionError("video job authority is incomplete") from error
        if not isinstance(replay_id, str) or not isinstance(report_id, str) or horizon not in {"complete", "partial"} or type(preview) is not bool:
            raise VideoResolutionError("video job authority is invalid")
        graph = self._reports.get_report(FixedReportQuery(replay_id, report_id))
        with self._sessions() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == replay_id))
            if replay is None or replay.managed_asset_id is None:
                raise VideoResolutionError("managed replay is unavailable")
            asset = session.get(ManagedAsset, replay.managed_asset_id)
            if asset is None or asset.sha256 != replay.sha256:
                raise VideoResolutionError("managed replay identity is invalid")
            replay_path = (self._settings.managed_replay_directory / Path(*asset.relative_path.split("/"))).resolve()
            if not replay_path.is_file() or hashlib.sha256(replay_path.read_bytes()).hexdigest() != replay.sha256:
                raise VideoResolutionError("managed replay bytes are invalid")
            frame_end = replay.frame_count
        scene = self._scenes.get_scene(MapSceneReadQuery(replay_id, report_id, 0, frame_end))
        payload = scene.payload
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
        authority = CameraPlanAuthorityV1(
            replay_public_id=replay_id, replay_sha256=replay.sha256, report_public_id=report_id,
            telemetry_run_public_id=payload["telemetry_run_public_id"], telemetry_trace_sha256=payload["telemetry_trace_sha256"],
            map_public_id=payload["map_public_id"], map_content_sha256=payload["map_content_sha256"],
            evidence_horizon=EvidenceHorizonV1(frame_end=accepted_end),
        )
        return VideoRenderRequest(authority=authority, report=graph, scene=scene, replay_path=replay_path)
