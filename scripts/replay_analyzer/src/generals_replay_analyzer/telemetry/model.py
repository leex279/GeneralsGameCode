"""Pydantic models for immutable, versioned replay telemetry observations."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, model_validator

SCHEMA_VERSION = 1

EVENT_TYPES = (
    "manifest",
    "players_initialized",
    "object_created",
    "construction_started",
    "construction_completed",
    "owner_changed",
    "sold",
    "object_destroyed",
    "production_queued",
    "production_cancelled",
    "production_completed",
    "upgrade_queued",
    "upgrade_cancelled",
    "upgrade_completed",
    "science_purchased",
    "special_power_used",
    "cash_changed",
    "supply_collected",
    "damage_applied",
    "healing_applied",
    "veterancy_changed",
    "player_defeated",
    "player_surrendered",
    "player_disconnected",
    "match_outcome",
    "order_issued",
    "entity_state_changed",
    "entity_sample",
    "complete",
)

OBSERVATION_EVENT_TYPES = tuple(event_type for event_type in EVENT_TYPES if event_type not in {"manifest", "complete"})
ObservationEventType = Literal[
    "players_initialized",
    "object_created",
    "construction_started",
    "construction_completed",
    "owner_changed",
    "sold",
    "object_destroyed",
    "production_queued",
    "production_cancelled",
    "production_completed",
    "upgrade_queued",
    "upgrade_cancelled",
    "upgrade_completed",
    "science_purchased",
    "special_power_used",
    "cash_changed",
    "supply_collected",
    "damage_applied",
    "healing_applied",
    "veterancy_changed",
    "player_defeated",
    "player_surrendered",
    "player_disconnected",
    "match_outcome",
    "order_issued",
    "entity_state_changed",
    "entity_sample",
]


class ManifestPayload(BaseModel):
    """Closed provenance payload emitted before any observed match fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_build: str = Field(min_length=1)
    replay_version: str = Field(min_length=1)
    map_identity: str = Field(min_length=1)
    initial_seed: int
    exporter_settings: dict[str, object]


class MapAssetReference(BaseModel):
    """Content-addressed asset reference retained by the terminal record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompletePayload(BaseModel):
    """Terminal exporter status and content identity for a complete trace."""

    model_config = ConfigDict(extra="allow", frozen=True)

    final_frame: NonNegativeInt
    command_count: NonNegativeInt
    event_counts: dict[str, NonNegativeInt]
    crc_mismatch: bool
    replay_truncated: bool
    clean_shutdown: bool
    writer_error: str | None
    trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    map_assets: list[MapAssetReference]


class TelemetryEnvelope(BaseModel):
    """Shared, immutable evidence identity and replay-time coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    run_id: UUID
    sequence: NonNegativeInt
    frame: NonNegativeInt
    logic_time_seconds: float

    @model_validator(mode="after")
    def _require_logic_time_for_30_fps(self) -> "TelemetryEnvelope":
        """Reject elapsed-time fields that cannot represent the authoritative frame."""
        if self.logic_time_seconds != self.frame / 30.0:
            raise ValueError("logic_time_seconds must equal frame / 30.0")
        return self


class ManifestRecord(TelemetryEnvelope):
    """First trace record, carrying closed exporter provenance."""

    event_type: Literal["manifest"]
    payload: ManifestPayload


class ObservationRecord(TelemetryEnvelope):
    """Forward-compatible observed event from an authoritative exporter seam."""

    event_type: ObservationEventType
    payload: dict[str, Any]


class CompleteRecord(TelemetryEnvelope):
    """Last trace record, carrying the completion and integrity state."""

    event_type: Literal["complete"]
    payload: CompletePayload

    @model_validator(mode="after")
    def _require_terminal_frame_match(self) -> "CompleteRecord":
        """Keep the terminal envelope and final replay frame inseparable."""
        if self.payload.final_frame != self.frame:
            raise ValueError("complete payload final_frame must equal envelope frame")
        return self


TelemetryRecord = Annotated[ManifestRecord | ObservationRecord | CompleteRecord, Field(discriminator="event_type")]
