"""Pydantic models for immutable, versioned replay telemetry observations."""

import math
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
UINT32_MAX = 4_294_967_295
FLOAT32_MAX = 3.402823466e38
EVENT_TYPES = (
    "manifest", "players_initialized", "object_created", "construction_started", "construction_completed",
    "owner_changed", "sold", "object_destroyed", "production_queued", "production_cancelled",
    "production_completed", "upgrade_queued", "upgrade_cancelled", "upgrade_completed", "science_purchased",
    "special_power_used", "cash_changed", "supply_collected", "damage_applied", "healing_applied",
    "veterancy_changed", "player_defeated", "player_surrendered", "player_disconnected", "match_outcome",
    "order_issued", "entity_state_changed", "entity_sample", "complete",
)


def _validation_schema_version(info: ValidationInfo) -> int:
    """Default direct model use to current v2 while honoring a reader-selected historical schema."""
    if isinstance(info.context, dict):
        version = info.context.get("schema_version")
        if type(version) is int and version in SUPPORTED_SCHEMA_VERSIONS:
            return version
    return SCHEMA_VERSION


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
    engine_player_indices: list[NonNegativeInt] | None = None
    game_data_catalog: "GameDataCatalogReference | MapAssetReference"

    @model_validator(mode="after")
    def _require_one_ordered_slot_snapshot(self) -> "PlayersInitializedPayload":
        if self.slots is None:
            if self.players is None:
                raise ValueError("players_initialized must contain a v1 players list or v2 slots")
            return self
        if self.players is not None:
            raise ValueError("v2 slots and historical v1 players cannot coexist")
        if not self.engine_player_indices:
            raise ValueError("v2 players_initialized must contain the full engine player domain")
        if self.engine_player_indices != sorted(set(self.engine_player_indices)):
            raise ValueError("engine_player_indices must be strictly ordered and unique")
        if [slot.slot_index for slot in self.slots] != list(range(8)):
            raise ValueError("slots must be ordered by slot_index 0 through 7")
        resolved_indexes = [slot.player_index for slot in self.slots if slot.player_index is not None]
        if len(resolved_indexes) != len(set(resolved_indexes)):
            raise ValueError("resolved occupied slots must have unique player_index mappings")
        engine_player_indices = set(self.engine_player_indices)
        if any(player_index not in engine_player_indices for player_index in resolved_indexes):
            raise ValueError("resolved replay slot player_index must belong to the engine player domain")
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

    @field_validator("before", "after")
    @classmethod
    def _require_current_uint32_range(cls, value: int, info: ValidationInfo) -> int:
        if _validation_schema_version(info) == SCHEMA_VERSION and not 0 <= value <= UINT32_MAX:
            raise ValueError(f"v2 cash value must be between 0 and {UINT32_MAX}")
        return value


class SupplyCollectedPayload(OpenPayload):
    collector_object_id: NonNegativeInt
    source_object_id: NonNegativeInt | None
    source_status: Literal["resolved", "unknown", "mixed"] | None = None
    dropoff_object_id: NonNegativeInt | None = None
    player_index: NonNegativeInt
    amount: Annotated[int | float, Field(ge=0)]
    location: RawPosition

    @field_validator("amount")
    @classmethod
    def _require_schema_specific_amount(cls, value: float, info: ValidationInfo) -> float:
        if _validation_schema_version(info) == 1:
            return float(value)
        if type(value) is not int or not 0 < value <= UINT32_MAX:
            raise ValueError(f"v2 supply amount must be an integer between 1 and {UINT32_MAX}")
        return value


class DamageAppliedPayload(OpenPayload):
    victim_object_id: NonNegativeInt
    victim_player_index: NonNegativeInt | None = None
    attacker_object_id: NonNegativeInt | None
    attacker_player_index: NonNegativeInt | None = None
    attacker_template_name: Annotated[str, Field(min_length=1)] | None = None
    weapon_name: str | None
    attempted_amount: Annotated[float, Field(ge=-FLOAT32_MAX, le=FLOAT32_MAX)]
    calculated_amount: Annotated[float, Field(gt=0, le=FLOAT32_MAX)] | None = None
    applied_amount: Annotated[float, Field(ge=0, le=FLOAT32_MAX)]
    prior_health: Annotated[float, Field(ge=0, le=FLOAT32_MAX)]
    new_health: Annotated[float, Field(ge=0, le=FLOAT32_MAX)]
    damage_type_id: NonNegativeInt | None = None
    damage_type: str = Field(min_length=1)
    death_type_id: NonNegativeInt | None = None
    death_type: str = Field(min_length=1)
    location: RawPosition
    killing_blow: bool


class HealingAppliedPayload(OpenPayload):
    target_object_id: NonNegativeInt
    target_player_index: NonNegativeInt | None = None
    source_object_id: NonNegativeInt | None = None
    source_player_index: NonNegativeInt | None = None
    attempted_amount: Annotated[float, Field(ge=-FLOAT32_MAX, le=FLOAT32_MAX)] | None = None
    calculated_amount: Annotated[float, Field(gt=0, le=FLOAT32_MAX)] | None = None
    applied_amount: Annotated[float, Field(ge=0, le=FLOAT32_MAX)]
    prior_health: Annotated[float, Field(ge=0, le=FLOAT32_MAX)]
    new_health: Annotated[float, Field(ge=0, le=FLOAT32_MAX)]
    location: RawPosition | None = None


class VeterancyChangedPayload(OpenPayload):
    object_id: NonNegativeInt
    owner_player_index: NonNegativeInt | None = None
    previous_level_id: NonNegativeInt | None = None
    previous_level: int | Literal["REGULAR", "VETERAN", "ELITE", "HEROIC"]
    new_level_id: NonNegativeInt | None = None
    new_level: int | Literal["REGULAR", "VETERAN", "ELITE", "HEROIC"]


class PlayerStatusPayload(OpenPayload):
    player_index: NonNegativeInt
    previous_status: Literal["active"] | None = None
    new_status: Literal["defeated", "surrendered", "disconnected"] | None = None
    source: Literal[
        "victory_conditions",
        "script_action",
        "replay_command",
        "replay_header_disconnect_plus_executed_surrender",
    ] | None = None
    replay_slot_index: Annotated[int, Field(ge=0, le=7)] | None = None


class MatchOutcomePayload(OpenPayload):
    outcome: Annotated[str, Field(min_length=1)] | None = None
    winner_player_index: NonNegativeInt | None = None
    status: Literal["decided", "unknown"] | None = None
    source: Literal["victory_conditions", "unavailable"] | None = None
    winner_player_indices: list[NonNegativeInt] | None = None
    loser_player_indices: list[NonNegativeInt] | None = None
    engine_player_indices: list[NonNegativeInt] | None = None
    terminal_reason: Literal["clean_completion", "crc_mismatch", "replay_truncated", "interrupted"] | None = None
    quit_early: bool | None = None
    replay_header_desync: bool | None = None
    replay_header_disconnected_slots: list[Annotated[int, Field(ge=0, le=7)]] | None = None
    crc_mismatch: bool | None = None
    crc_mismatch_frame: NonNegativeInt | None = None
    clean_shutdown: bool | None = None


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

    @field_validator("balance")
    @classmethod
    def _require_current_uint32_range(cls, value: int | None, info: ValidationInfo) -> int | None:
        if value is not None and _validation_schema_version(info) == SCHEMA_VERSION and value > UINT32_MAX:
            raise ValueError(f"v2 final balance must be at most {UINT32_MAX}")
        return value

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
    crc_mismatch_frame: NonNegativeInt | None = None
    replay_truncated: bool
    quit_early: bool | None = None
    replay_header_desync: bool | None = None
    replay_header_disconnected_slots: list[Annotated[int, Field(ge=0, le=7)]] | None = None
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


def _require_v2_closed_payload(payload: BaseModel, nested_fields: tuple[str, ...] = ()) -> None:
    unexpected = sorted((payload.model_extra or {}).keys())
    for field_name in nested_fields:
        nested = getattr(payload, field_name)
        if isinstance(nested, BaseModel):
            unexpected.extend(f"{field_name}.{name}" for name in sorted((nested.model_extra or {}).keys()))
    if unexpected:
        raise ValueError(f"v2 payload contains unexpected fields: {', '.join(unexpected)}")


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

    @model_validator(mode="after")
    def _require_strict_v2_damage(self) -> "DamageAppliedRecord":
        if self.schema_version != 2:
            return self
        _require_v2_closed_payload(self.payload, ("location",))
        _require_v2_payload_fields(
            self.payload,
            {
                "victim_player_index",
                "attacker_player_index",
                "attacker_template_name",
                "calculated_amount",
                "damage_type_id",
                "death_type_id",
            },
        )
        if self.payload.victim_object_id == 0 or self.payload.attacker_object_id == 0:
            raise ValueError("v2 combat object IDs must be greater than zero")
        if self.payload.attacker_object_id is None and (
            self.payload.attacker_player_index is not None or self.payload.attacker_template_name is not None
        ):
            raise ValueError("attacker identity fields require attacker_object_id")
        calculated = self.payload.calculated_amount
        if calculated is None or calculated <= 0 or self.payload.applied_amount <= 0 or self.payload.prior_health <= 0:
            raise ValueError("v2 damage requires a positive calculated, applied, and prior-health transition")
        if self.payload.applied_amount > calculated and not math.isclose(
            self.payload.applied_amount, calculated, rel_tol=1e-6, abs_tol=1e-5
        ):
            raise ValueError("applied damage cannot exceed authoritative calculated damage")
        expected_applied = self.payload.prior_health - self.payload.new_health
        if not math.isclose(self.payload.applied_amount, expected_applied, rel_tol=1e-6, abs_tol=1e-5):
            raise ValueError("damage health arithmetic does not match applied_amount")
        if self.payload.killing_blow != (self.payload.new_health == 0):
            raise ValueError("killing_blow must exactly describe the positive-to-zero health transition")
        return self


class HealingAppliedRecord(TelemetryEnvelope):
    event_type: Literal["healing_applied"]
    payload: HealingAppliedPayload

    @model_validator(mode="after")
    def _require_strict_v2_healing(self) -> "HealingAppliedRecord":
        if self.schema_version != 2:
            return self
        _require_v2_closed_payload(self.payload, ("location",))
        _require_v2_payload_fields(
            self.payload,
            {
                "target_player_index",
                "source_object_id",
                "source_player_index",
                "attempted_amount",
                "calculated_amount",
                "location",
            },
        )
        if self.payload.target_object_id == 0 or self.payload.source_object_id == 0:
            raise ValueError("v2 healing object IDs must be greater than zero")
        if self.payload.source_object_id is None and self.payload.source_player_index is not None:
            raise ValueError("source_player_index requires source_object_id")
        calculated = self.payload.calculated_amount
        if calculated is None or calculated <= 0 or self.payload.applied_amount <= 0:
            raise ValueError("v2 healing requires a positive calculated and applied transition")
        if self.payload.applied_amount > calculated and not math.isclose(
            self.payload.applied_amount, calculated, rel_tol=1e-6, abs_tol=1e-5
        ):
            raise ValueError("applied healing cannot exceed authoritative calculated healing")
        expected_applied = self.payload.new_health - self.payload.prior_health
        if not math.isclose(self.payload.applied_amount, expected_applied, rel_tol=1e-6, abs_tol=1e-5):
            raise ValueError("healing health arithmetic does not match applied_amount")
        return self


class VeterancyChangedRecord(TelemetryEnvelope):
    event_type: Literal["veterancy_changed"]
    payload: VeterancyChangedPayload

    @model_validator(mode="after")
    def _require_strict_v2_veterancy(self) -> "VeterancyChangedRecord":
        if self.schema_version != 2:
            return self
        _require_v2_closed_payload(self.payload)
        _require_v2_payload_fields(
            self.payload,
            {"owner_player_index", "previous_level_id", "new_level_id"},
        )
        names = ("REGULAR", "VETERAN", "ELITE", "HEROIC")
        previous_id = self.payload.previous_level_id
        new_id = self.payload.new_level_id
        if (
            self.payload.object_id == 0
            or previous_id is None
            or new_id is None
            or previous_id >= len(names)
            or new_id >= len(names)
            or self.payload.previous_level != names[previous_id]
            or self.payload.new_level != names[new_id]
            or previous_id == new_id
        ):
            raise ValueError("v2 veterancy IDs and stable names must describe one real level change")
        return self


class PlayerDefeatedRecord(TelemetryEnvelope):
    event_type: Literal["player_defeated"]
    payload: PlayerStatusPayload

    @model_validator(mode="after")
    def _require_strict_v2_status(self) -> "PlayerDefeatedRecord":
        if self.schema_version == 2:
            _require_v2_closed_payload(self.payload)
            if (
                self.payload.previous_status != "active"
                or self.payload.new_status != "defeated"
                or self.payload.source not in {"victory_conditions", "script_action"}
            ):
                raise ValueError("player_defeated must preserve its authoritative active-to-defeated source")
        return self


class PlayerSurrenderedRecord(TelemetryEnvelope):
    event_type: Literal["player_surrendered"]
    payload: PlayerStatusPayload

    @model_validator(mode="after")
    def _require_strict_v2_status(self) -> "PlayerSurrenderedRecord":
        if self.schema_version == 2:
            _require_v2_closed_payload(self.payload)
            if (
                self.payload.previous_status != "active"
                or self.payload.new_status != "surrendered"
                or self.payload.source != "replay_command"
            ):
                raise ValueError("player_surrendered must preserve its authoritative replay-command transition")
        return self


class PlayerDisconnectedRecord(TelemetryEnvelope):
    event_type: Literal["player_disconnected"]
    payload: PlayerStatusPayload

    @model_validator(mode="after")
    def _require_strict_v2_status(self) -> "PlayerDisconnectedRecord":
        if self.schema_version == 2:
            _require_v2_closed_payload(self.payload)
            if (
                self.payload.previous_status != "active"
                or self.payload.new_status != "disconnected"
                or self.payload.source != "replay_header_disconnect_plus_executed_surrender"
                or self.payload.replay_slot_index is None
            ):
                raise ValueError("player_disconnected requires header metadata plus an executed transition")
        return self


class MatchOutcomeRecord(TelemetryEnvelope):
    event_type: Literal["match_outcome"]
    payload: MatchOutcomePayload

    @model_validator(mode="after")
    def _require_strict_v2_outcome(self) -> "MatchOutcomeRecord":
        if self.schema_version != 2:
            return self
        _require_v2_closed_payload(self.payload)
        if {"outcome", "winner_player_index"} & self.payload.model_fields_set:
            raise ValueError("v2 match_outcome cannot contain legacy outcome fields")
        _require_v2_payload_fields(
            self.payload,
            {
                "status",
                "source",
                "winner_player_indices",
                "loser_player_indices",
                "engine_player_indices",
                "terminal_reason",
                "quit_early",
                "replay_header_desync",
                "replay_header_disconnected_slots",
                "crc_mismatch",
                "crc_mismatch_frame",
                "clean_shutdown",
            },
        )
        winners = self.payload.winner_player_indices or []
        losers = self.payload.loser_player_indices or []
        domain = self.payload.engine_player_indices or []
        if domain != sorted(set(domain)) or winners != sorted(set(winners)) or losers != sorted(set(losers)):
            raise ValueError("outcome player indices must be strictly ordered and unique")
        if set(winners) & set(losers):
            raise ValueError("winner and loser player indices must be disjoint")
        if self.payload.status == "decided":
            if self.payload.source != "victory_conditions" or not winners:
                raise ValueError("decided outcome requires authoritative victory-condition winners")
        elif self.payload.source != "unavailable" or winners or losers:
            raise ValueError("unknown outcome cannot claim winners or losers")
        return self


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
        if self.schema_version == 2:
            _require_v2_closed_payload(self.payload)
            _require_v2_payload_fields(
                self.payload,
                {
                    "crc_mismatch_frame",
                    "quit_early",
                    "replay_header_desync",
                    "replay_header_disconnected_slots",
                },
            )
            if self.payload.crc_mismatch != (self.payload.crc_mismatch_frame is not None):
                raise ValueError("crc_mismatch_frame must be present exactly for a CRC mismatch")
            if self.payload.clean_shutdown and (self.payload.crc_mismatch or self.payload.replay_truncated):
                raise ValueError("clean completion cannot also be a CRC mismatch or truncated")
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
