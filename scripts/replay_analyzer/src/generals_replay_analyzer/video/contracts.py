"""Immutable public contracts for deterministic replay camera and video plans."""

from __future__ import annotations

import math
import re
from itertools import pairwise
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _public_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("value must be a canonical public UUID") from None
    if str(parsed) != value:
        raise ValueError("value must be a canonical public UUID")
    return value


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("value must be a lowercase SHA-256")
    return value


def _finite(value: float) -> float:
    if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
        raise ValueError("camera values must be finite and cannot use negative zero")
    return value


PublicId = Annotated[str, AfterValidator(_public_id)]
Sha256 = Annotated[str, AfterValidator(_sha256)]
FiniteFloat = Annotated[float, AfterValidator(_finite)]


class VideoContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvidenceHorizonV1(VideoContract):
    frame_start: Literal[0] = 0
    frame_end: int = Field(ge=0)


class EvidenceCitationV1(VideoContract):
    evidence_public_id: PublicId
    tier: Literal["observed", "derived"]
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered_window(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("citation frame window must be ordered")
        return self


# TheSuperHackers @feature Leex 24/08/2026 Bind camera generation to accepted replay, telemetry, map, report, and evidence identities. (#TBD)
class CameraPlanAuthorityV1(VideoContract):
    schema_version: Literal[1] = 1
    replay_public_id: PublicId
    replay_sha256: Sha256
    report_public_id: PublicId
    telemetry_run_public_id: PublicId
    telemetry_trace_sha256: Sha256
    map_public_id: PublicId
    map_content_sha256: Sha256
    evidence_horizon: EvidenceHorizonV1


CameraFocusKind = Literal[
    "engagement",
    "damage",
    "attack_order",
    "milestone",
    "resource_contest",
    "base_context",
]


class CameraSegmentV1(VideoContract):
    segment_id: PublicId
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    target_x: FiniteFloat = Field(ge=-10_000_000.0, le=10_000_000.0)
    target_y: FiniteFloat = Field(ge=-10_000_000.0, le=10_000_000.0)
    target_z: FiniteFloat = Field(ge=-10_000_000.0, le=10_000_000.0)
    zoom: FiniteFloat = Field(ge=0.1, le=10.0)
    pitch: FiniteFloat = Field(ge=-89.0, le=0.0)
    yaw: FiniteFloat = Field(ge=-360.0, le=360.0)
    transition: Literal["cut", "ease"]
    transition_frames: int = Field(ge=0, le=300)
    focus_kind: CameraFocusKind
    label: str = Field(min_length=1, max_length=160)
    evidence: tuple[EvidenceCitationV1, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _validate_interval_and_transition(self) -> Self:
        if self.end_frame < self.start_frame:
            raise ValueError("camera segment interval must be ordered")
        length = self.end_frame - self.start_frame + 1
        if self.transition == "cut" and self.transition_frames != 0:
            raise ValueError("cut transitions must use zero frames")
        if self.transition == "ease" and not 0 < self.transition_frames <= length:
            raise ValueError("ease transition must fit inside the destination segment")
        ordered = tuple(
            sorted(
                set(self.evidence),
                key=lambda item: (item.frame_start, item.frame_end, item.evidence_public_id, item.tier),
            )
        )
        object.__setattr__(self, "evidence", ordered)
        return self


class CameraPlanV1(VideoContract):
    schema_version: Literal[1] = 1
    logic_hz: Literal[30] = 30
    authority: CameraPlanAuthorityV1
    segments: tuple[CameraSegmentV1, ...] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def _validate_timeline(self) -> Self:
        ordered = tuple(sorted(self.segments, key=lambda item: (item.start_frame, item.segment_id)))
        if ordered != self.segments:
            raise ValueError("camera segments must already be deterministically ordered")
        if ordered[0].start_frame != 0:
            raise ValueError("camera plan must begin at frame zero")
        for previous, current in pairwise(ordered):
            if current.start_frame != previous.end_frame + 1:
                raise ValueError("camera plan segments must be inclusive and gapless")
        horizon = self.authority.evidence_horizon.frame_end
        if ordered[-1].end_frame != horizon:
            raise ValueError("camera plan must end at the accepted evidence horizon")
        identities = tuple(item.segment_id for item in ordered)
        if len(identities) != len(set(identities)):
            raise ValueError("camera segment identities must be unique")
        for segment in ordered:
            for citation in segment.evidence:
                if citation.frame_end > horizon:
                    raise ValueError("camera citation exceeds the accepted evidence horizon")
        return self

    def canonical_json(self) -> str:
        return self.model_dump_json(indent=None, by_alias=True, exclude_none=True)


# TheSuperHackers @feature Leex 23/08/2026 Keep replay commentary bound to the same accepted frame evidence as camera direction. (#TBD)
CommentaryRole = Literal["intro", "play_by_play", "analysis", "transition", "outro"]


class CommentaryEventV1(VideoContract):
    event_id: PublicId
    start_frame: int = Field(ge=0)
    latest_end_frame: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=500)
    subtitle_text: str = Field(min_length=1, max_length=500)
    role: CommentaryRole
    player_public_ids: tuple[PublicId, ...] = ()
    strategy_identity: str | None = Field(default=None, min_length=1, max_length=160)
    evidence: tuple[EvidenceCitationV1, ...] = Field(min_length=1, max_length=16)
    confidence_tier: Literal["observed", "derived"]
    camera_segment_id: PublicId
    template_version: Literal["commentary-template-v1"] = "commentary-template-v1"
    ollama_run_public_id: PublicId | None = None

    @model_validator(mode="after")
    def _validate_evidence_window(self) -> Self:
        if self.latest_end_frame < self.start_frame:
            raise ValueError("commentary event frame window must be ordered")
        if any(
            citation.frame_start > self.start_frame or citation.frame_end < self.latest_end_frame
            for citation in self.evidence
        ):
            raise ValueError("commentary event must fit inside every cited evidence window")
        ordered_players = tuple(sorted(set(self.player_public_ids)))
        ordered_evidence = tuple(
            sorted(set(self.evidence), key=lambda item: (item.frame_start, item.frame_end, item.evidence_public_id, item.tier))
        )
        object.__setattr__(self, "player_public_ids", ordered_players)
        object.__setattr__(self, "evidence", ordered_evidence)
        return self


class CommentaryPlanV1(VideoContract):
    schema_version: Literal[1] = 1
    logic_hz: Literal[30] = 30
    replay_public_id: PublicId
    report_public_id: PublicId
    evidence_horizon: EvidenceHorizonV1
    events: tuple[CommentaryEventV1, ...] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def _validate_timeline(self) -> Self:
        ordered = tuple(sorted(self.events, key=lambda item: (item.start_frame, item.event_id)))
        if ordered != self.events:
            raise ValueError("commentary events must already be deterministically ordered")
        if len({item.event_id for item in ordered}) != len(ordered):
            raise ValueError("commentary event identities must be unique")
        for previous, current in pairwise(ordered):
            if current.start_frame <= previous.latest_end_frame:
                raise ValueError("commentary events must not overlap")
        if any(item.latest_end_frame > self.evidence_horizon.frame_end for item in ordered):
            raise ValueError("commentary event exceeds the accepted evidence horizon")
        return self

    def canonical_json(self) -> str:
        return self.model_dump_json(indent=None, by_alias=True, exclude_none=True)


class CommentaryEnrichmentSentenceV1(VideoContract):
    event_id: PublicId
    text: str = Field(min_length=1, max_length=500)
    evidence_public_ids: tuple[PublicId, ...] = Field(min_length=1, max_length=16)
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("enrichment sentence frame window must be ordered")
        object.__setattr__(self, "evidence_public_ids", tuple(sorted(set(self.evidence_public_ids))))
        return self


class ValidatedCommentaryEnrichmentV1(VideoContract):
    ollama_run_public_id: PublicId
    sentences: tuple[CommentaryEnrichmentSentenceV1, ...] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def _unique_sentences(self) -> Self:
        if len({item.event_id for item in self.sentences}) != len(self.sentences):
            raise ValueError("enrichment may contain one sentence per commentary event")
        return self


class VideoSettingsV1(VideoContract):
    schema_version: Literal[1] = 1
    width: int = Field(ge=640, le=7680)
    height: int = Field(ge=360, le=4320)
    fps: Literal[30, 60]
    subtitle_mode: Literal["track", "burned"]

    @model_validator(mode="after")
    def _even_dimensions(self) -> Self:
        if self.width % 2 or self.height % 2:
            raise ValueError("yuv420p video dimensions must be even")
        return self
