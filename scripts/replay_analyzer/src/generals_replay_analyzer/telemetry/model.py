"""Pydantic models for immutable, versioned replay telemetry observations."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, model_validator

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
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
    replay_name: Annotated[str, Field(min_length=1)] | None
    player_index: NonNegativeInt
    team_id: int
    faction_template_name: Annotated[str, Field(min_length=1)] | None
    color: NonNegativeInt | None
    start_position_status: Literal["resolved", "unknown"] | None = None
    start_position: RawPosition | None = None
    controller: Literal["human", "ai"] | None = None
    is_human: bool
    is_local_player: bool

    @model_validator(mode="after")
    def _require_explicit_resolution_and_controller_state(self) -> "PlayerObservation":
        if self.start_position_status is not None and (
            (self.start_position_status == "resolved") != (self.start_position is not None)
        ):
            raise ValueError("start_position must be present exactly when start_position_status is resolved")
        if self.controller is not None and (self.controller == "human") != self.is_human:
            raise ValueError("controller and is_human must describe the same engine slot state")
        return self


class PlayerSlotObservation(BaseModel):
    """One explicit v2 replay slot, whether occupied, unresolved, open, or closed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    slot_index: Annotated[int, Field(ge=0, le=7)]
    slot_state: Literal["open", "closed", "easy_ai", "medium_ai", "brutal_ai", "human"]
    occupied: bool
    resolution_status: Literal["resolved", "unresolved", "not_applicable"]
    replay_name: Annotated[str, Field(min_length=1)] | None
    player_index: NonNegativeInt | None
    team_id: int | None
    faction_template_name: Annotated[str, Field(min_length=1)] | None
    color: NonNegativeInt | None
    start_position_status: Literal["resolved", "unknown", "not_applicable"]
    start_position: RawPosition | None
    controller: Literal["human", "ai"] | None
    is_human: bool
    is_header_local_slot: bool
    is_resolved_local_player: bool | None

    @model_validator(mode="after")
    def _require_coherent_slot_and_resolution_state(self) -> "PlayerSlotObservation":
        occupied_state = self.slot_state in {"human", "easy_ai", "medium_ai", "brutal_ai"}
        if self.occupied != occupied_state:
            raise ValueError("unoccupied slot states cannot be occupied and occupied slot states cannot be empty")
        if not self.occupied:
            if (
                self.resolution_status != "not_applicable"
                or self.replay_name is not None
                or self.player_index is not None
                or self.team_id is not None
                or self.faction_template_name is not None
                or self.color is not None
                or self.start_position_status != "not_applicable"
                or self.start_position is not None
                or self.controller is not None
                or self.is_human
                or self.is_resolved_local_player is not None
            ):
                raise ValueError("unoccupied slot must expose null engine and controller fields")
            return self
        expected_controller = "human" if self.slot_state == "human" else "ai"
        if self.controller != expected_controller or self.is_human != (expected_controller == "human"):
            raise ValueError("occupied slot controller must agree with its replay slot state")
        if self.replay_name is None or self.team_id is None:
            raise ValueError("occupied slot must preserve replay header name and team")
        if self.resolution_status == "resolved":
            if self.player_index is None or self.faction_template_name is None or self.is_resolved_local_player is None:
                raise ValueError("resolved slot must expose its engine player mapping")
        elif self.resolution_status == "unresolved":
            if self.player_index is not None or self.faction_template_name is not None or self.is_resolved_local_player is not None:
                raise ValueError("unresolved slot must not claim resolved engine player fields")
        else:
            raise ValueError("occupied slot cannot use not_applicable resolution status")
        if (self.start_position_status == "resolved") != (self.start_position is not None):
            raise ValueError("start_position must be present exactly when start_position_status is resolved")
        if self.start_position_status == "not_applicable":
            raise ValueError("occupied slot must report resolved or unknown start position")
        return self


class PlayersInitializedPayload(OpenPayload):
    players: list[PlayerObservation] | None = None
    header_local_slot_index: int | None = None
    slots: list[PlayerSlotObservation] | None = None
    game_data_catalog: "GameDataCatalogReference | MapAssetReference"

    @model_validator(mode="after")
    def _require_one_ordered_slot_snapshot(self) -> "PlayersInitializedPayload":
        if self.slots is None:
            if self.players is None:
                raise ValueError("players_initialized must contain a v1 players list or v2 slots")
            return self
        if self.players is not None:
            raise ValueError("v2 slots and historical v1 players cannot coexist")
        if [slot.slot_index for slot in self.slots] != list(range(8)):
            raise ValueError("slots must be ordered by slot_index 0 through 7")
        resolved_indexes = [slot.player_index for slot in self.slots if slot.player_index is not None]
        if len(resolved_indexes) != len(set(resolved_indexes)):
            raise ValueError("resolved occupied slots must have unique player_index mappings")
        header_slots = [slot.slot_index for slot in self.slots if slot.is_header_local_slot]
        expected_header_slots = [] if self.header_local_slot_index is None else [self.header_local_slot_index]
        if header_slots != expected_header_slots:
            raise ValueError("header local slot flags must equal header_local_slot_index")
        return self


class ObjectCreatedPayload(OpenPayload):
    object_id: NonNegativeInt
    template_name: str = Field(min_length=1)
    owner_player_index: NonNegativeInt | None
    team_id: int | None
    position_status: Literal["placed", "unplaced"] | None = None
    position: RawPosition | None
    orientation: float
    kind_of_flags: list[str]
    initial_status: list[str] | None = None
    creation_source: str = Field(min_length=1)
    creation_context: "ObjectCreationContext | None" = None

    @model_validator(mode="after")
    def _require_explicit_placement_state(self) -> "ObjectCreatedPayload":
        if self.position_status == "placed" and self.position is None:
            raise ValueError("placed creation must contain an observed position")
        if self.position_status == "unplaced" and self.position is not None:
            raise ValueError("unplaced creation must not fabricate a position")
        return self


class ObjectCreationContext(OpenPayload):
    registration_frame: NonNegativeInt
    producer_object_id: Annotated[int, Field(gt=0)] | None
    producer_player_index: NonNegativeInt | None


class ConstructionPayload(OpenPayload):
    object_id: NonNegativeInt
    owner_player_index: NonNegativeInt | None
    team_id: int | None = None
    producer_object_id: Annotated[int, Field(gt=0)] | None = None
    builder_object_id: Annotated[int, Field(gt=0)] | None = None
    responsible_player_index: NonNegativeInt | None = None


class ConstructionStartedPayload(ConstructionPayload):
    previous_state: Literal["not_present", "complete"] | None = None
    new_state: Literal["under_construction"] | None = None


class ConstructionCompletedPayload(ConstructionPayload):
    previous_state: Literal["under_construction"] | None = None
    new_state: Literal["complete"] | None = None


class OwnerChangedPayload(OpenPayload):
    object_id: NonNegativeInt
    previous_owner_player_index: NonNegativeInt | None
    new_owner_player_index: NonNegativeInt | None
    previous_team_id: int | None = None
    new_team_id: int | None = None


class SoldPayload(OpenPayload):
    object_id: NonNegativeInt
    previous_state: Literal["available"] | None = None
    new_state: Literal["sold"] | None = None
    owner_player_index: NonNegativeInt | None
    team_id: int | None = None


class ObjectDestroyedPayload(OpenPayload):
    object_id: NonNegativeInt
    previous_state: Literal["alive", "sold"] | None = None
    new_state: Literal["destroyed"] | None = None
    owner_player_index: NonNegativeInt | None
    team_id: int | None = None
    destruction_source: Literal["destroy_object"] | None = None


class ProductionPayload(OpenPayload):
    production_id: NonNegativeInt
    producer_object_id: NonNegativeInt
    player_index: NonNegativeInt
    template_name: str = Field(min_length=1)
    engine_production_id: NonNegativeInt | None = None
    queue_position: NonNegativeInt | None = None
    queued_frame: NonNegativeInt | None = None
    cost: NonNegativeInt | None = None
    quantity: NonNegativeInt | None = None
    state: Literal["queued", "cancelled", "completed"] | None = None
    terminal_frame: NonNegativeInt | None = None


class UpgradePayload(OpenPayload):
    upgrade_name: str = Field(min_length=1)
    player_index: NonNegativeInt
    upgrade_queue_id: NonNegativeInt | None = None
    producer_object_id: NonNegativeInt | None = None
    queue_position: NonNegativeInt | None = None
    queued_frame: NonNegativeInt | None = None
    cost: NonNegativeInt | None = None
    state: Literal["queued", "cancelled", "completed"] | None = None
    terminal_frame: NonNegativeInt | None = None


class SciencePurchasedPayload(OpenPayload):
    science_name: str = Field(min_length=1)
    player_index: NonNegativeInt
    purchase_cost_points: NonNegativeInt | None = None
    points_before: NonNegativeInt | None = None
    points_after: NonNegativeInt | None = None
    source_object_id: NonNegativeInt | None = None


class SpecialPowerUsedPayload(OpenPayload):
    special_power_name: str = Field(min_length=1)
    player_index: NonNegativeInt
    source_object_id: NonNegativeInt | None = None
    target_object_id: NonNegativeInt | None = None
    target_location: RawPosition | None = None


class CashChangedPayload(OpenPayload):
    player_index: NonNegativeInt
    before: int
    delta: int
    after: int
    track_income: bool
    reason: str = Field(min_length=1)


class SupplyCollectedPayload(OpenPayload):
    collector_object_id: NonNegativeInt
    source_object_id: NonNegativeInt | None
    source_status: Literal["resolved", "unknown", "mixed"] | None = None
    dropoff_object_id: NonNegativeInt | None = None
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
    game_data_catalog: "GameDataCatalogReference | None" = None

    @model_validator(mode="after")
    def _require_catalog_engine_identity(self) -> "ManifestPayload":
        if self.game_data_catalog is not None and self.game_data_catalog.engine_data_identity != self.engine_build:
            raise ValueError("game_data_catalog engine_data_identity must equal engine_build")
        return self


class MapAssetReference(BaseModel):
    """Content-addressed asset reference retained by terminal and catalog observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GameDataCatalogReference(BaseModel):
    """Strict content identity for the semantic engine-data catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["game_data_catalog"]
    path: str = Field(pattern=r"^game-data-catalog-v1-[0-9a-f]{64}\.json$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_data_identity: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_content_addressed_path(self) -> "GameDataCatalogReference":
        if self.path != f"game-data-catalog-v1-{self.sha256}.json":
            raise ValueError("catalog path must embed its sha256 identity")
        return self


class FinalCashBalance(BaseModel):
    """One engine-observed terminal Money state, including explicit absence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    player_index: NonNegativeInt
    has_money: bool
    balance: NonNegativeInt | None

    @model_validator(mode="after")
    def _require_balance_presence(self) -> "FinalCashBalance":
        if self.has_money != (self.balance is not None):
            raise ValueError("balance must be present exactly when has_money is true")
        return self


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
    final_cash_balances: list[FinalCashBalance] | None = None


class TelemetryEnvelope(BaseModel):
    """Shared immutable evidence identity and authoritative replay-time coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1, 2]
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


def _require_v2_payload_fields(payload: BaseModel, field_names: set[str]) -> None:
    missing = sorted(field_names - payload.model_fields_set)
    if missing:
        raise ValueError(f"v2 lifecycle payload is missing required fields: {', '.join(missing)}")


class ObjectCreatedRecord(TelemetryEnvelope):
    event_type: Literal["object_created"]
    payload: ObjectCreatedPayload

    @model_validator(mode="after")
    def _require_strict_v2_creation(self) -> "ObjectCreatedRecord":
        if self.schema_version == 2:
            _require_v2_payload_fields(self.payload, {"position_status", "initial_status", "creation_context"})
            if self.payload.object_id == 0:
                raise ValueError("v2 object_id must be greater than zero")
            if self.payload.creation_source not in {"map_loaded", "starting_object", "player_production", "unknown"}:
                raise ValueError("v2 creation_source must be an authoritative source enum")
            for names in (self.payload.kind_of_flags, self.payload.initial_status or []):
                if any(not name for name in names) or len(names) != len(set(names)):
                    raise ValueError("v2 kind/status names must each be unique and nonempty")
        return self


class ConstructionStartedRecord(TelemetryEnvelope):
    event_type: Literal["construction_started"]
    payload: ConstructionStartedPayload

    @model_validator(mode="after")
    def _require_strict_v2_transition(self) -> "ConstructionStartedRecord":
        if self.schema_version == 2:
            _require_v2_payload_fields(
                self.payload,
                {
                    "previous_state",
                    "new_state",
                    "team_id",
                    "producer_object_id",
                    "builder_object_id",
                    "responsible_player_index",
                },
            )
        return self


class ConstructionCompletedRecord(TelemetryEnvelope):
    event_type: Literal["construction_completed"]
    payload: ConstructionCompletedPayload

    @model_validator(mode="after")
    def _require_strict_v2_transition(self) -> "ConstructionCompletedRecord":
        if self.schema_version == 2:
            _require_v2_payload_fields(
                self.payload,
                {
                    "previous_state",
                    "new_state",
                    "team_id",
                    "producer_object_id",
                    "builder_object_id",
                    "responsible_player_index",
                },
            )
        return self


class OwnerChangedRecord(TelemetryEnvelope):
    event_type: Literal["owner_changed"]
    payload: OwnerChangedPayload

    @model_validator(mode="after")
    def _require_strict_v2_transition(self) -> "OwnerChangedRecord":
        if self.schema_version == 2:
            _require_v2_payload_fields(self.payload, {"previous_team_id", "new_team_id"})
        return self


class SoldRecord(TelemetryEnvelope):
    event_type: Literal["sold"]
    payload: SoldPayload

    @model_validator(mode="after")
    def _require_strict_v2_transition(self) -> "SoldRecord":
        if self.schema_version == 2:
            _require_v2_payload_fields(self.payload, {"previous_state", "new_state", "team_id"})
        return self


class ObjectDestroyedRecord(TelemetryEnvelope):
    event_type: Literal["object_destroyed"]
    payload: ObjectDestroyedPayload

    @model_validator(mode="after")
    def _require_strict_v2_transition(self) -> "ObjectDestroyedRecord":
        if self.schema_version == 2:
            _require_v2_payload_fields(
                self.payload,
                {"previous_state", "new_state", "team_id", "destruction_source"},
            )
        return self


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
