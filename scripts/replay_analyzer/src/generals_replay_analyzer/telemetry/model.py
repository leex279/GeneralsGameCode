"""Pydantic models for immutable, versioned replay telemetry observations."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, model_validator

SCHEMA_VERSION = 1
EVENT_TYPES = (
    "manifest", "players_initialized", "object_created", "construction_started", "construction_completed",
    "owner_changed", "sold", "object_destroyed", "production_queued", "production_cancelled",
    "production_completed", "upgrade_queued", "upgrade_cancelled", "upgrade_completed", "science_purchased",
    "special_power_used", "cash_changed", "supply_collected", "damage_applied", "healing_applied",
    "veterancy_changed", "player_defeated", "player_surrendered", "player_disconnected", "match_outcome",
    "order_issued", "entity_state_changed", "entity_sample", "complete",
)


class OpenPayload(BaseModel):
    """Forward-compatible observed payload with required v1 evidence fields."""

    model_config = ConfigDict(extra="allow", frozen=True)


class RawPosition(OpenPayload):
    """Untransformed engine-world coordinate retained as observed evidence."""

    x: float
    y: float
    z: float


class PlayerObservation(OpenPayload):
    replay_name: str = Field(min_length=1)
    player_index: NonNegativeInt
    team_id: int
    faction_template_name: str = Field(min_length=1)
    color: NonNegativeInt
    is_human: bool
    is_local_player: bool


class PlayersInitializedPayload(OpenPayload):
    players: list[PlayerObservation] = Field(min_length=1)
    game_data_catalog: "MapAssetReference"


class ObjectCreatedPayload(OpenPayload):
    object_id: NonNegativeInt
    template_name: str = Field(min_length=1)
    owner_player_index: NonNegativeInt
    team_id: int
    position: RawPosition
    orientation: float
    kind_of_flags: list[str]
    creation_source: str = Field(min_length=1)


class ConstructionStartedPayload(OpenPayload):
    object_id: NonNegativeInt
    owner_player_index: NonNegativeInt


class ConstructionCompletedPayload(OpenPayload):
    object_id: NonNegativeInt
    owner_player_index: NonNegativeInt


class OwnerChangedPayload(OpenPayload):
    object_id: NonNegativeInt
    previous_owner_player_index: NonNegativeInt
    new_owner_player_index: NonNegativeInt


class SoldPayload(OpenPayload):
    object_id: NonNegativeInt
    owner_player_index: NonNegativeInt


class ObjectDestroyedPayload(OpenPayload):
    object_id: NonNegativeInt
    owner_player_index: NonNegativeInt


class ProductionPayload(OpenPayload):
    production_id: NonNegativeInt
    producer_object_id: NonNegativeInt
    player_index: NonNegativeInt
    template_name: str = Field(min_length=1)


class UpgradePayload(OpenPayload):
    upgrade_name: str = Field(min_length=1)
    player_index: NonNegativeInt


class SciencePurchasedPayload(OpenPayload):
    science_name: str = Field(min_length=1)
    player_index: NonNegativeInt


class SpecialPowerUsedPayload(OpenPayload):
    special_power_name: str = Field(min_length=1)
    player_index: NonNegativeInt


class CashChangedPayload(OpenPayload):
    player_index: NonNegativeInt
    before: int
    delta: int
    after: int
    track_income: bool
    reason: str = Field(min_length=1)


class SupplyCollectedPayload(OpenPayload):
    collector_object_id: NonNegativeInt
    source_object_id: NonNegativeInt
    player_index: NonNegativeInt
    amount: NonNegativeFloat
    location: RawPosition


class DamageAppliedPayload(OpenPayload):
    victim_object_id: NonNegativeInt
    attacker_object_id: NonNegativeInt | None
    weapon_name: str | None
    attempted_amount: NonNegativeFloat
    applied_amount: NonNegativeFloat
    prior_health: NonNegativeFloat
    new_health: NonNegativeFloat
    damage_type: str = Field(min_length=1)
    death_type: str = Field(min_length=1)
    location: RawPosition
    killing_blow: bool


class HealingAppliedPayload(OpenPayload):
    target_object_id: NonNegativeInt
    applied_amount: NonNegativeFloat
    prior_health: NonNegativeFloat
    new_health: NonNegativeFloat


class VeterancyChangedPayload(OpenPayload):
    object_id: NonNegativeInt
    previous_level: int
    new_level: int


class PlayerStatusPayload(OpenPayload):
    player_index: NonNegativeInt


class MatchOutcomePayload(OpenPayload):
    outcome: str = Field(min_length=1)
    winner_player_index: NonNegativeInt | None


class OrderIssuedPayload(OpenPayload):
    message_type: int
    message_name: str | None
    source_player_index: NonNegativeInt
    selected_object_ids: list[NonNegativeInt]
    target_object_id: NonNegativeInt | None
    target_location: RawPosition | None
    command_source: str = Field(min_length=1)


class EntityStateChangedPayload(OpenPayload):
    object_id: NonNegativeInt
    previous_state: str = Field(min_length=1)
    current_state: str = Field(min_length=1)


class EntitySamplePayload(OpenPayload):
    object_id: NonNegativeInt
    position: RawPosition
    orientation: float
    layer: int
    speed: NonNegativeFloat
    current_state: str = Field(min_length=1)


class ManifestPayload(BaseModel):
    """Closed provenance payload emitted before any observed match fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    engine_build: str = Field(min_length=1)
    replay_version: str = Field(min_length=1)
    map_identity: str = Field(min_length=1)
    initial_seed: int
    exporter_settings: dict[str, object]


class MapAssetReference(BaseModel):
    """Content-addressed asset reference retained by terminal and catalog observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompletePayload(OpenPayload):
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
    """Shared immutable evidence identity and authoritative replay-time coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    run_id: UUID
    sequence: NonNegativeInt
    frame: NonNegativeInt
    logic_time_seconds: float

    @model_validator(mode="after")
    def _require_logic_time_for_30_fps(self) -> "TelemetryEnvelope":
        if self.logic_time_seconds != self.frame / 30.0:
            raise ValueError("logic_time_seconds must equal frame / 30.0")
        return self


class ManifestRecord(TelemetryEnvelope):
    event_type: Literal["manifest"]
    payload: ManifestPayload


class PlayersInitializedRecord(TelemetryEnvelope):
    event_type: Literal["players_initialized"]
    payload: PlayersInitializedPayload


class ObjectCreatedRecord(TelemetryEnvelope):
    event_type: Literal["object_created"]
    payload: ObjectCreatedPayload


class ConstructionStartedRecord(TelemetryEnvelope):
    event_type: Literal["construction_started"]
    payload: ConstructionStartedPayload


class ConstructionCompletedRecord(TelemetryEnvelope):
    event_type: Literal["construction_completed"]
    payload: ConstructionCompletedPayload


class OwnerChangedRecord(TelemetryEnvelope):
    event_type: Literal["owner_changed"]
    payload: OwnerChangedPayload


class SoldRecord(TelemetryEnvelope):
    event_type: Literal["sold"]
    payload: SoldPayload


class ObjectDestroyedRecord(TelemetryEnvelope):
    event_type: Literal["object_destroyed"]
    payload: ObjectDestroyedPayload


class ProductionQueuedRecord(TelemetryEnvelope):
    event_type: Literal["production_queued"]
    payload: ProductionPayload


class ProductionCancelledRecord(TelemetryEnvelope):
    event_type: Literal["production_cancelled"]
    payload: ProductionPayload


class ProductionCompletedRecord(TelemetryEnvelope):
    event_type: Literal["production_completed"]
    payload: ProductionPayload


class UpgradeQueuedRecord(TelemetryEnvelope):
    event_type: Literal["upgrade_queued"]
    payload: UpgradePayload


class UpgradeCancelledRecord(TelemetryEnvelope):
    event_type: Literal["upgrade_cancelled"]
    payload: UpgradePayload


class UpgradeCompletedRecord(TelemetryEnvelope):
    event_type: Literal["upgrade_completed"]
    payload: UpgradePayload


class SciencePurchasedRecord(TelemetryEnvelope):
    event_type: Literal["science_purchased"]
    payload: SciencePurchasedPayload


class SpecialPowerUsedRecord(TelemetryEnvelope):
    event_type: Literal["special_power_used"]
    payload: SpecialPowerUsedPayload


class CashChangedRecord(TelemetryEnvelope):
    event_type: Literal["cash_changed"]
    payload: CashChangedPayload


class SupplyCollectedRecord(TelemetryEnvelope):
    event_type: Literal["supply_collected"]
    payload: SupplyCollectedPayload


class DamageAppliedRecord(TelemetryEnvelope):
    event_type: Literal["damage_applied"]
    payload: DamageAppliedPayload


class HealingAppliedRecord(TelemetryEnvelope):
    event_type: Literal["healing_applied"]
    payload: HealingAppliedPayload


class VeterancyChangedRecord(TelemetryEnvelope):
    event_type: Literal["veterancy_changed"]
    payload: VeterancyChangedPayload


class PlayerDefeatedRecord(TelemetryEnvelope):
    event_type: Literal["player_defeated"]
    payload: PlayerStatusPayload


class PlayerSurrenderedRecord(TelemetryEnvelope):
    event_type: Literal["player_surrendered"]
    payload: PlayerStatusPayload


class PlayerDisconnectedRecord(TelemetryEnvelope):
    event_type: Literal["player_disconnected"]
    payload: PlayerStatusPayload


class MatchOutcomeRecord(TelemetryEnvelope):
    event_type: Literal["match_outcome"]
    payload: MatchOutcomePayload


class OrderIssuedRecord(TelemetryEnvelope):
    event_type: Literal["order_issued"]
    payload: OrderIssuedPayload


class EntityStateChangedRecord(TelemetryEnvelope):
    event_type: Literal["entity_state_changed"]
    payload: EntityStateChangedPayload


class EntitySampleRecord(TelemetryEnvelope):
    event_type: Literal["entity_sample"]
    payload: EntitySamplePayload


class CompleteRecord(TelemetryEnvelope):
    event_type: Literal["complete"]
    payload: CompletePayload

    @model_validator(mode="after")
    def _require_terminal_frame_match(self) -> "CompleteRecord":
        if self.payload.final_frame != self.frame:
            raise ValueError("complete payload final_frame must equal envelope frame")
        return self


TelemetryRecord = Annotated[
    ManifestRecord | PlayersInitializedRecord | ObjectCreatedRecord | ConstructionStartedRecord | ConstructionCompletedRecord
    | OwnerChangedRecord | SoldRecord | ObjectDestroyedRecord | ProductionQueuedRecord | ProductionCancelledRecord
    | ProductionCompletedRecord | UpgradeQueuedRecord | UpgradeCancelledRecord | UpgradeCompletedRecord | SciencePurchasedRecord
    | SpecialPowerUsedRecord | CashChangedRecord | SupplyCollectedRecord | DamageAppliedRecord | HealingAppliedRecord
    | VeterancyChangedRecord | PlayerDefeatedRecord | PlayerSurrenderedRecord | PlayerDisconnectedRecord | MatchOutcomeRecord
    | OrderIssuedRecord | EntityStateChangedRecord | EntitySampleRecord | CompleteRecord,
    Field(discriminator="event_type"),
]
