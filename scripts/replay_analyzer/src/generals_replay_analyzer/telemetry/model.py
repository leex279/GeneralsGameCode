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
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

from generals_replay_analyzer.telemetry.combat_types import require_combat_type_pair
from generals_replay_analyzer.telemetry.engine_state_catalog import (
    AI_STATE_NAMES,
    LAYER_NAMES,
    LOCOMOTOR_SET_NAMES,
    STATE_SOURCES,
)
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
UINT32_MAX = 4_294_967_295
# Largest finite float32 after the engine writer's nine-significant-digit JSON serialization.
FLOAT32_MAX = 3.40282347e38
EVENT_TYPES = (
    "manifest", "players_initialized", "object_created", "construction_started", "construction_completed",
    "owner_changed", "sold", "object_destroyed", "production_queued", "production_cancelled",
    "production_completed", "upgrade_queued", "upgrade_cancelled", "upgrade_completed", "science_purchased",
    "special_power_used", "cash_changed", "supply_collected", "damage_applied", "healing_applied",
    "veterancy_changed", "player_defeated", "player_surrendered", "player_disconnected", "match_outcome",
    "order_issued", "entity_state_changed", "entity_sample", "complete",
)
TASK7_STATE_NAMES = frozenset(STATE_SOURCES)


def _validation_schema_version(info: ValidationInfo) -> int:
    """Default direct model use to current v2 while honoring a reader-selected historical schema."""
    if isinstance(info.context, dict):
        version = info.context.get("schema_version")
        if type(version) is int and version in SUPPORTED_SCHEMA_VERSIONS:
            return version
    return SCHEMA_VERSION


def _require_v2_engine_real(field: str, value: float) -> None:
    """Constrain serialized engine Real observations without tightening frozen v1."""
    if not math.isfinite(value) or abs(value) > FLOAT32_MAX:
        raise ValueError(f"v2 {field} must be a finite float32 engine Real")


def _require_v2_engine_enum_name(
    numeric_id: int | None,
    stable_name: str | None,
    status: str | None,
    catalog: dict[int, str],
    label: str,
) -> None:
    """Bind raw IDs to exact stable names without guessing unavailable or unknown values."""
    if status not in {"stable", "unknown_engine_value", "unavailable_no_ai"}:
        raise ValueError(f"v2 {label} name provenance status is required")
    if status == "stable" and (
        numeric_id is None or numeric_id not in catalog or stable_name != catalog[numeric_id]
    ):
        raise ValueError(f"stable {label} provenance requires its exact numeric and symbolic identity")
    if status == "unavailable_no_ai" and (numeric_id is not None or stable_name is not None):
        raise ValueError(f"{label} identity must be null when no AI interface exposes it")
    if status == "unknown_engine_value" and (
        numeric_id is None or numeric_id in catalog or stable_name is not None
    ):
        raise ValueError(f"unknown {label} value requires an unmapped raw numeric ID and no guessed name")


def _require_v2_state_classification(
    classification: str,
    source: str | None,
    ai_state_id: int | None,
    is_engine_moving: bool | None,
) -> None:
    """Require a direct producer classification/source pair and its exposed engine prerequisites."""
    if classification not in STATE_SOURCES or source not in STATE_SOURCES[classification]:
        raise ValueError("v2 state classification/source is not a direct canonical engine mapping")
    if source == "ai_interface_unavailable" and ai_state_id is not None:
        raise ValueError("AI-unavailable state classification cannot claim a raw AI state")
    if source is not None and source.startswith("ai_") and source != "ai_interface_unavailable" and ai_state_id is None:
        raise ValueError("AI-derived state classification requires a raw AI state ID")
    if source == "ai_moving_state" and is_engine_moving is not True:
        raise ValueError("moving classification requires the direct engine-moving flag")
    if source == "ai_idle_state" and is_engine_moving is not False:
        raise ValueError("idle classification contradicts the direct engine-moving flag")
    if source == "ai_guard_state" and ai_state_id not in {16, 21, 43}:
        raise ValueError("guarding classification requires an exact guard AI state")


def _require_v2_layer_name(layer_id: int, layer_name: str | None, status: str | None) -> None:
    """Bind exact fixed layers while leaving bridge layers explicitly dynamic."""
    if status == "stable" and (layer_id not in LAYER_NAMES or layer_name != LAYER_NAMES[layer_id]):
        raise ValueError("stable layer provenance requires its exact numeric and symbolic identity")
    if status == "dynamic_bridge_layer" and not (2 <= layer_id <= 14 and layer_name is None):
        raise ValueError("dynamic bridge layer requires a raw bridge-layer ID and no guessed stable name")
    if status == "unknown_engine_value" and (layer_id in LAYER_NAMES or 2 <= layer_id <= 14 or layer_name is not None):
        raise ValueError("unknown layer requires an unmapped raw ID and no guessed stable name")
    if status not in {"stable", "dynamic_bridge_layer", "unknown_engine_value"}:
        raise ValueError("v2 layer name provenance status is required")


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
    source_player_mask: Annotated[int, Field(ge=0, le=UINT32_MAX)] | None = None
    source_player_indices: list[NonNegativeInt] | None = None
    attacker_template_name: Annotated[str, Field(min_length=1)] | None = None
    weapon_name: Annotated[str, Field(min_length=1)] | None
    attempted_amount: float
    calculated_amount: float | None = None
    applied_amount: NonNegativeFloat
    prior_health: NonNegativeFloat
    new_health: NonNegativeFloat
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
    attempted_amount: float | None = None
    calculated_amount: float | None = None
    applied_amount: NonNegativeFloat
    prior_health: NonNegativeFloat
    new_health: NonNegativeFloat
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
        "executed_true_self_destruct",
        "replay_header_disconnect_plus_executed_false_self_destruct",
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


class EntityIdentity(BaseModel):
    """One current engine object identity captured at an observation seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    object_id: Annotated[int, Field(ge=1)]
    template_name: Annotated[str, Field(min_length=1)]


class OrderIssuedPayload(OpenPayload):
    order_id: Annotated[int, Field(ge=1)] | None = None
    command_frame: NonNegativeInt | None = None
    message_type: int
    message_name: str | None
    source_player_index: NonNegativeInt
    selected_object_ids: list[NonNegativeInt]
    selected_entities: list[EntityIdentity] | None = None
    target_kind: Literal["none", "object", "location"] | None = None
    target_object_id: NonNegativeInt | None
    target_template_name: Annotated[str, Field(min_length=1)] | None = None
    target_location: RawPosition | None
    command_source: str = Field(min_length=1)
    ai_command_source_id: int | None = None
    ai_command_source_name: Annotated[str, Field(min_length=1)] | None = None

    @field_validator(
        "order_id", "command_frame", "selected_entities", "target_kind", "target_template_name",
        "ai_command_source_id", "ai_command_source_name", mode="wrap",
    )
    @classmethod
    def _keep_v1_extension_values_open(
        cls, value: object, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> object:
        """Keep names that were v1 extension slots unconstrained while validating their v2 definitions."""
        if _validation_schema_version(info) == 1:
            return value
        return handler(value)


class EntityStateChangedPayload(OpenPayload):
    object_id: NonNegativeInt
    template_name: Annotated[str, Field(min_length=1)] | None = None
    owner_player_index: NonNegativeInt | None = None
    previous_state: Annotated[str, Field(min_length=1)]
    previous_state_source: str | None = None
    current_state: Annotated[str, Field(min_length=1)]
    current_state_source: str | None = None
    previous_ai_state_id: int | None = None
    current_ai_state_id: int | None = None
    previous_ai_state_name: Annotated[str, Field(min_length=1)] | None = None
    current_ai_state_name: Annotated[str, Field(min_length=1)] | None = None
    previous_ai_state_name_status: str | None = None
    current_ai_state_name_status: str | None = None
    previous_locomotor_set_id: int | None = None
    current_locomotor_set_id: int | None = None
    previous_locomotor_set_name: Annotated[str, Field(min_length=1)] | None = None
    current_locomotor_set_name: Annotated[str, Field(min_length=1)] | None = None
    previous_locomotor_set_name_status: str | None = None
    current_locomotor_set_name_status: str | None = None
    previous_is_engine_moving: bool | None = None
    current_is_engine_moving: bool | None = None
    current_order_id: Annotated[int, Field(ge=1)] | None = None
    transition_source: Literal["end_of_game_logic_update"] | None = None

    @field_validator(
        "template_name", "owner_player_index", "previous_state_source", "current_state_source",
        "previous_ai_state_id", "current_ai_state_id", "previous_ai_state_name", "current_ai_state_name",
        "previous_ai_state_name_status", "current_ai_state_name_status", "previous_locomotor_set_id",
        "current_locomotor_set_id", "previous_locomotor_set_name", "current_locomotor_set_name",
        "previous_locomotor_set_name_status", "current_locomotor_set_name_status",
        "previous_is_engine_moving", "current_is_engine_moving", "current_order_id", "transition_source",
        mode="wrap",
    )
    @classmethod
    def _keep_v1_extension_values_open(
        cls, value: object, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> object:
        """Keep names that were v1 extension slots unconstrained while validating their v2 definitions."""
        if _validation_schema_version(info) == 1:
            return value
        return handler(value)


class EntitySamplePayload(OpenPayload):
    object_id: NonNegativeInt
    template_name: Annotated[str, Field(min_length=1)] | None = None
    owner_player_index: NonNegativeInt | None = None
    position: RawPosition
    orientation: float
    layer: int | None = None
    layer_id: int | None = None
    layer_name: Annotated[str, Field(min_length=1)] | None = None
    layer_name_status: Literal["stable", "dynamic_bridge_layer", "unknown_engine_value"] | None = None
    position_bounds_policy: Literal[
        "pathfinder_xy_closed",
        "exempt_kindof_aircraft",
        "exempt_kindof_bridge",
        "exempt_kindof_projectile",
        "exempt_locomotor_air_surface",
        "exempt_module_wander_ai",
        "exempt_physics_without_ai_pathing",
    ] | None = None
    speed_status: Literal["measured_physics_velocity", "unavailable_no_physics"] | None = None
    speed: NonNegativeFloat | None
    current_state: Annotated[str, Field(min_length=1)]
    current_state_source: str | None = None
    ai_state_id: int | None = None
    ai_state_name: Annotated[str, Field(min_length=1)] | None = None
    ai_state_name_status: str | None = None
    locomotor_set_id: int | None = None
    locomotor_set_name: Annotated[str, Field(min_length=1)] | None = None
    locomotor_set_name_status: str | None = None
    current_order_id: Annotated[int, Field(ge=1)] | None = None
    current_order_message_type: NonNegativeInt | None = None
    current_order_message_name: Annotated[str, Field(min_length=1)] | None = None
    path_goal_status: Literal[
        "path_tail", "unavailable_no_ai", "unavailable_no_path", "unavailable_empty_path"
    ] | None = None
    path_goal: RawPosition | None = None
    is_mobile: bool | None = None
    is_structure: bool | None = None
    is_disabled: bool | None = None
    is_engine_moving: bool | None = None
    sample_reason: Literal[
        "lifecycle_forced", "order_forced", "state_forced", "changed", "periodic_moving_heartbeat"
    ] | None = None

    @field_validator(
        "template_name", "owner_player_index", "layer_id", "layer_name", "layer_name_status",
        "position_bounds_policy", "speed_status",
        "current_state_source", "ai_state_id", "ai_state_name", "ai_state_name_status", "locomotor_set_id",
        "locomotor_set_name", "locomotor_set_name_status", "current_order_id", "current_order_message_type",
        "current_order_message_name", "path_goal_status", "path_goal", "is_mobile", "is_structure",
        "is_disabled", "is_engine_moving", "sample_reason", mode="wrap",
    )
    @classmethod
    def _keep_v1_extension_values_open(
        cls, value: object, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> object:
        """Keep names that were v1 extension slots unconstrained while validating their v2 definitions."""
        if _validation_schema_version(info) == 1:
            return value
        return handler(value)


class ManifestPayload(BaseModel):
    """Closed provenance payload emitted before any observed match fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    engine_build: str = Field(min_length=1)
    replay_version: str = Field(min_length=1)
    map_identity: str = Field(min_length=1)
    initial_seed: int
    exporter_settings: dict[str, object]
    game_data_catalog: "GameDataCatalogReference | None" = None
    map_asset: "MapAssetReference | None" = None

    @model_validator(mode="after")
    def _require_catalog_engine_identity(self) -> "ManifestPayload":
        if self.game_data_catalog is not None and self.game_data_catalog.engine_data_identity != self.engine_build:
            raise ValueError("game_data_catalog engine_data_identity must equal engine_build")
        if self.map_asset is not None:
            if self.map_asset.engine_data_identity != self.engine_build:
                raise ValueError("map_asset engine_data_identity must equal engine_build")
            if self.map_asset.map_identity != self.map_identity:
                raise ValueError("map_asset map_identity must equal manifest map_identity")
        return self


class MapAssetReference(BaseModel):
    """A legacy generic asset reference or the closed content-addressed v2 map reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    type: Literal["map_asset"] | None = None
    schema_version: Literal[1] | None = None
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    engine_data_identity: str | None = Field(default=None, min_length=1)
    map_identity: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _bind_strict_content_address(self) -> "MapAssetReference":
        strict = (
            self.type,
            self.schema_version,
            self.content_sha256,
            self.engine_data_identity,
            self.map_identity,
        )
        if any(value is not None for value in strict):
            if any(value is None for value in strict):
                raise ValueError("map asset reference identity fields must be jointly present")
            if self.path != f"map-assets-v1/{self.content_sha256}/manifest.json":
                raise ValueError("map asset path must embed its content_sha256 identity")
        return self

    def require_strict_v2(self) -> None:
        """Reject a historical two-field reference where telemetry v2 requires a map identity."""
        if self.type != "map_asset":
            raise ValueError("v2 requires a strict map_asset reference")


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
    terminal_reason: Literal["clean_completion", "crc_mismatch", "replay_truncated", "interrupted"] | None = None
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

    @model_validator(mode="after")
    def _require_bounded_v2_exporter_settings(self) -> "ManifestRecord":
        if self.schema_version == 2:
            if self.payload.map_asset is None:
                raise ValueError("v2 manifest requires a strict map_asset reference")
            self.payload.map_asset.require_strict_v2()
            interval = self.payload.exporter_settings.get("movement_sample_frames")
            if type(interval) is not int or not 1 <= interval <= 3600:
                raise ValueError("v2 movement_sample_frames must be an integer from 1 through 3600")
            if self.payload.exporter_settings.get("order_coverage") != canonical_order_coverage():
                raise ValueError("v2 order_coverage must equal the canonical closed supported-order coverage")
        return self


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
                "source_player_mask",
                "source_player_indices",
                "attacker_template_name",
                "calculated_amount",
                "damage_type_id",
                "death_type_id",
            },
        )
        if self.payload.victim_object_id == 0 or self.payload.attacker_object_id == 0:
            raise ValueError("v2 combat object IDs must be greater than zero")
        source_mask = self.payload.source_player_mask
        source_indices = self.payload.source_player_indices
        if source_mask is None or source_indices is None:
            raise ValueError("v2 damage requires source_player_mask and source_player_indices")
        if source_indices != sorted(set(source_indices)):
            raise ValueError("source_player_indices must be strictly ordered and unique")
        mask_indices = [index for index in range(32) if source_mask & (1 << index)]
        if source_indices != mask_indices:
            raise ValueError("source_player_indices must exactly represent source_player_mask")
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
        for field, value in (
            ("attempted_amount", self.payload.attempted_amount),
            ("calculated_amount", calculated),
            ("applied_amount", self.payload.applied_amount),
            ("prior_health", self.payload.prior_health),
            ("new_health", self.payload.new_health),
            ("location.x", self.payload.location.x),
            ("location.y", self.payload.location.y),
            ("location.z", self.payload.location.z),
        ):
            _require_v2_engine_real(field, value)
        if self.payload.damage_type_id is None or self.payload.death_type_id is None:
            raise ValueError("v2 damage requires numeric damage and death type IDs")
        require_combat_type_pair("damage", self.payload.damage_type_id, self.payload.damage_type)
        require_combat_type_pair("death", self.payload.death_type_id, self.payload.death_type)
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
        attempted = self.payload.attempted_amount
        calculated = self.payload.calculated_amount
        if attempted is None or calculated is None or calculated <= 0 or self.payload.applied_amount <= 0:
            raise ValueError("v2 healing requires attempted, positive calculated, and applied transition values")
        if self.payload.applied_amount > calculated and not math.isclose(
            self.payload.applied_amount, calculated, rel_tol=1e-6, abs_tol=1e-5
        ):
            raise ValueError("applied healing cannot exceed authoritative calculated healing")
        expected_applied = self.payload.new_health - self.payload.prior_health
        if not math.isclose(self.payload.applied_amount, expected_applied, rel_tol=1e-6, abs_tol=1e-5):
            raise ValueError("healing health arithmetic does not match applied_amount")
        for field, value in (
            ("attempted_amount", attempted),
            ("calculated_amount", calculated),
            ("applied_amount", self.payload.applied_amount),
            ("prior_health", self.payload.prior_health),
            ("new_health", self.payload.new_health),
        ):
            _require_v2_engine_real(field, value)
        if self.payload.location is not None:
            for axis, value in (
                ("x", self.payload.location.x),
                ("y", self.payload.location.y),
                ("z", self.payload.location.z),
            ):
                _require_v2_engine_real(f"location.{axis}", value)
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
                or self.payload.source != "executed_true_self_destruct"
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
                or self.payload.source != "replay_header_disconnect_plus_executed_false_self_destruct"
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

    @model_validator(mode="after")
    def _require_strict_v2_order(self) -> "OrderIssuedRecord":
        if self.schema_version != 2:
            return self
        _require_v2_closed_payload(self.payload, ("target_location",))
        _require_v2_payload_fields(
            self.payload,
            {
                "order_id", "command_frame", "selected_entities", "target_kind", "target_template_name",
                "ai_command_source_id", "ai_command_source_name",
            },
        )
        if self.payload.order_id is None or self.payload.command_frame != self.frame:
            raise ValueError("command_frame must equal the authoritative order envelope frame")
        if self.payload.message_type < 0:
            raise ValueError("v2 order message_type must be nonnegative")
        if not self.payload.message_name:
            raise ValueError("v2 order message_name must preserve the packaged symbolic command name")
        if self.payload.command_source != "recorded_network_player_command":
            raise ValueError("v2 command_source must identify recorded network player dispatch")
        if self.payload.ai_command_source_id != 0 or self.payload.ai_command_source_name != "CMD_FROM_PLAYER":
            raise ValueError("v2 order AI command source must preserve CMD_FROM_PLAYER numeric identity")
        selected_entities = self.payload.selected_entities or []
        selected_ids = self.payload.selected_object_ids
        if (
            not selected_ids
            or any(object_id == 0 for object_id in selected_ids)
            or len(selected_ids) != len(set(selected_ids))
            or [identity.object_id for identity in selected_entities] != selected_ids
        ):
            raise ValueError("selected_entities must exactly preserve unique selected_object_ids source order")
        if any(identity.model_extra for identity in selected_entities):
            raise ValueError("selected_entities must contain closed object identity records")
        target_kind = self.payload.target_kind
        if target_kind == "object":
            if self.payload.target_object_id in {None, 0} or self.payload.target_template_name is None:
                raise ValueError("object-target order requires current target identity")
            if self.payload.target_location is not None:
                raise ValueError("object-target order cannot claim a raw target location")
        elif target_kind == "location":
            if self.payload.target_location is None:
                raise ValueError("location-target order requires its raw replay location")
            if self.payload.target_object_id is not None or self.payload.target_template_name is not None:
                raise ValueError("location-target order cannot claim a target object")
        elif target_kind == "none":
            if (
                self.payload.target_object_id is not None
                or self.payload.target_template_name is not None
                or self.payload.target_location is not None
            ):
                raise ValueError("target-free order must keep all target fields null")
        else:
            raise ValueError("v2 order requires an explicit supported target_kind")
        if self.payload.target_location is not None:
            for axis, value in (
                ("x", self.payload.target_location.x),
                ("y", self.payload.target_location.y),
                ("z", self.payload.target_location.z),
            ):
                _require_v2_engine_real(f"target_location.{axis}", value)
        return self


class EntityStateChangedRecord(TelemetryEnvelope):
    event_type: Literal["entity_state_changed"]
    payload: EntityStateChangedPayload

    @model_validator(mode="after")
    def _require_strict_v2_engine_transition(self) -> "EntityStateChangedRecord":
        if self.schema_version != 2:
            return self
        _require_v2_closed_payload(self.payload)
        _require_v2_payload_fields(
            self.payload,
            {
                "template_name", "owner_player_index", "previous_state_source", "current_state_source",
                "previous_ai_state_id", "current_ai_state_id", "previous_ai_state_name", "current_ai_state_name",
                "previous_ai_state_name_status", "current_ai_state_name_status",
                "previous_locomotor_set_id", "current_locomotor_set_id",
                "previous_locomotor_set_name", "current_locomotor_set_name",
                "previous_locomotor_set_name_status", "current_locomotor_set_name_status",
                "previous_is_engine_moving", "current_is_engine_moving", "current_order_id", "transition_source",
            },
        )
        if self.payload.object_id == 0 or self.payload.template_name is None:
            raise ValueError("v2 entity state transition requires a current object identity")
        if self.payload.previous_state not in TASK7_STATE_NAMES or self.payload.current_state not in TASK7_STATE_NAMES:
            raise ValueError("v2 entity state transition requires a supported direct classification")
        if (
            self.payload.previous_state_source is None
            or self.payload.current_state_source is None
            or self.payload.previous_ai_state_name_status is None
            or self.payload.current_ai_state_name_status is None
            or self.payload.previous_is_engine_moving is None
            or self.payload.current_is_engine_moving is None
            or self.payload.transition_source is None
        ):
            raise ValueError("v2 entity state transition requires explicit engine provenance and movement flags")
        _require_v2_engine_enum_name(
            self.payload.previous_ai_state_id,
            self.payload.previous_ai_state_name,
            self.payload.previous_ai_state_name_status,
            AI_STATE_NAMES,
            "AI state",
        )
        _require_v2_engine_enum_name(
            self.payload.current_ai_state_id,
            self.payload.current_ai_state_name,
            self.payload.current_ai_state_name_status,
            AI_STATE_NAMES,
            "AI state",
        )
        _require_v2_engine_enum_name(
            self.payload.previous_locomotor_set_id,
            self.payload.previous_locomotor_set_name,
            self.payload.previous_locomotor_set_name_status,
            LOCOMOTOR_SET_NAMES,
            "locomotor set",
        )
        _require_v2_engine_enum_name(
            self.payload.current_locomotor_set_id,
            self.payload.current_locomotor_set_name,
            self.payload.current_locomotor_set_name_status,
            LOCOMOTOR_SET_NAMES,
            "locomotor set",
        )
        _require_v2_state_classification(
            self.payload.previous_state,
            self.payload.previous_state_source,
            self.payload.previous_ai_state_id,
            self.payload.previous_is_engine_moving,
        )
        _require_v2_state_classification(
            self.payload.current_state,
            self.payload.current_state_source,
            self.payload.current_ai_state_id,
            self.payload.current_is_engine_moving,
        )
        before = (
            self.payload.previous_state,
            self.payload.previous_state_source,
            self.payload.previous_ai_state_id,
            self.payload.previous_locomotor_set_id,
            self.payload.previous_is_engine_moving,
        )
        after = (
            self.payload.current_state,
            self.payload.current_state_source,
            self.payload.current_ai_state_id,
            self.payload.current_locomotor_set_id,
            self.payload.current_is_engine_moving,
        )
        if before == after:
            raise ValueError("entity_state_changed requires an actual engine state transition")
        return self


class EntitySampleRecord(TelemetryEnvelope):
    event_type: Literal["entity_sample"]
    payload: EntitySamplePayload

    @model_validator(mode="after")
    def _require_strict_v2_engine_sample(self) -> "EntitySampleRecord":
        if self.schema_version != 2:
            return self
        _require_v2_closed_payload(self.payload, ("position", "path_goal"))
        _require_v2_payload_fields(
            self.payload,
            {
                "template_name", "owner_player_index", "layer_id", "layer_name", "layer_name_status",
                "position_bounds_policy",
                "speed_status", "current_state_source", "ai_state_id", "ai_state_name", "ai_state_name_status",
                "locomotor_set_id", "locomotor_set_name", "locomotor_set_name_status", "current_order_id",
                "current_order_message_type", "current_order_message_name",
                "path_goal_status", "path_goal", "is_mobile", "is_structure", "is_disabled", "is_engine_moving",
                "sample_reason",
            },
        )
        if self.payload.object_id == 0 or self.payload.template_name is None or self.payload.layer_id is None:
            raise ValueError("v2 entity sample requires current object, template, and layer identity")
        if self.payload.current_state not in TASK7_STATE_NAMES:
            raise ValueError("v2 entity sample requires a supported direct classification")
        if (
            self.payload.layer_name_status is None
            or self.payload.position_bounds_policy is None
            or self.payload.speed_status is None
            or self.payload.current_state_source is None
            or self.payload.ai_state_name_status is None
            or self.payload.path_goal_status is None
            or self.payload.is_mobile is None
            or self.payload.is_structure is None
            or self.payload.is_disabled is None
            or self.payload.is_engine_moving is None
            or self.payload.sample_reason is None
        ):
            raise ValueError("v2 entity sample requires explicit source and sampling provenance")
        for field, value in (
            ("position.x", self.payload.position.x),
            ("position.y", self.payload.position.y),
            ("position.z", self.payload.position.z),
            ("orientation", self.payload.orientation),
        ):
            _require_v2_engine_real(field, value)
        if self.payload.path_goal is not None:
            for axis, value in (
                ("x", self.payload.path_goal.x),
                ("y", self.payload.path_goal.y),
                ("z", self.payload.path_goal.z),
            ):
                _require_v2_engine_real(f"path_goal.{axis}", value)
        if (self.payload.path_goal_status == "path_tail") != (self.payload.path_goal is not None):
            raise ValueError("path_goal must be present exactly for path_tail provenance")
        if (self.payload.speed_status == "measured_physics_velocity") != (self.payload.speed is not None):
            raise ValueError("speed must be present exactly when measured from PhysicsBehavior")
        if self.payload.speed is not None:
            _require_v2_engine_real("speed", self.payload.speed)
        _require_v2_engine_enum_name(
            self.payload.ai_state_id,
            self.payload.ai_state_name,
            self.payload.ai_state_name_status,
            AI_STATE_NAMES,
            "AI state",
        )
        _require_v2_engine_enum_name(
            self.payload.locomotor_set_id,
            self.payload.locomotor_set_name,
            self.payload.locomotor_set_name_status,
            LOCOMOTOR_SET_NAMES,
            "locomotor set",
        )
        _require_v2_state_classification(
            self.payload.current_state,
            self.payload.current_state_source,
            self.payload.ai_state_id,
            self.payload.is_engine_moving,
        )
        order_fields = (
            self.payload.current_order_id,
            self.payload.current_order_message_type,
            self.payload.current_order_message_name,
        )
        if any(value is None for value in order_fields) != all(value is None for value in order_fields):
            raise ValueError("current order ID and numeric/symbolic references must be jointly present or null")
        _require_v2_layer_name(self.payload.layer_id, self.payload.layer_name, self.payload.layer_name_status)
        if self.payload.is_disabled != (self.payload.current_state == "disabled"):
            raise ValueError("disabled classification must exactly follow engine disabled state")
        if self.payload.sample_reason == "periodic_moving_heartbeat" and not (
            self.payload.is_mobile
            and not self.payload.is_structure
            and not self.payload.is_disabled
            and self.payload.is_engine_moving
        ):
            raise ValueError("periodic heartbeat is only valid for enabled live moving mobile entities")
        return self


class CompleteRecord(TelemetryEnvelope):
    event_type: Literal["complete"]
    payload: CompletePayload

    @model_validator(mode="after")
    def _require_terminal_frame_match(self) -> "CompleteRecord":
        if self.payload.final_frame != self.frame:
            raise ValueError("complete payload final_frame must equal envelope frame")
        if self.schema_version == 2:
            if len(self.payload.map_assets) != 1:
                raise ValueError("v2 complete requires exactly one authoritative map asset")
            self.payload.map_assets[0].require_strict_v2()
            _require_v2_closed_payload(self.payload)
            _require_v2_payload_fields(
                self.payload,
                {
                    "terminal_reason",
                    "crc_mismatch_frame",
                    "quit_early",
                    "replay_header_desync",
                    "replay_header_disconnected_slots",
                },
            )
            if self.payload.crc_mismatch != (self.payload.crc_mismatch_frame is not None):
                raise ValueError("crc_mismatch_frame must be present exactly for a CRC mismatch")
            expected_flags = {
                "clean_completion": (True, False, False),
                "crc_mismatch": (False, True, False),
                "replay_truncated": (False, False, True),
                "interrupted": (False, False, False),
            }
            actual_flags = (self.payload.clean_shutdown, self.payload.crc_mismatch, self.payload.replay_truncated)
            if self.payload.terminal_reason is None or actual_flags != expected_flags[self.payload.terminal_reason]:
                raise ValueError("terminal_reason must exactly agree with completion termination flags")
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
