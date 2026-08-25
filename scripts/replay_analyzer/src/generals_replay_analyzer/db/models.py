"""Complete Replay Analyzer V2 persistence schema."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, overload

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from generals_replay_analyzer.db.base import Base, CreatedAtMixin, IntegerPrimaryKeyMixin, PublicIdMixin, utc_now
from generals_replay_analyzer.db.types import CanonicalJSON, lowercase_sha256_check

JSON = dict[str, Any] | list[Any]
RUN_STATUSES = "'pending','running','succeeded','failed'"
JOB_STATUSES = "'pending','running','succeeded','failed','cancelled'"
QUALITY_VALUES = "'available','unavailable','partial'"


@overload
def _fk(target: str, ondelete: str, *, nullable: Literal[False] = False) -> MappedColumn[int]: ...


@overload
def _fk(target: str, ondelete: str, *, nullable: Literal[True]) -> MappedColumn[int | None]: ...


def _fk(target: str, ondelete: str, *, nullable: bool = False) -> MappedColumn[int] | MappedColumn[int | None]:
    return mapped_column(ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


def _run_id_column() -> Mapped[str]:
    return mapped_column(
        String(36),
        CheckConstraint(
            "length(run_id) = 36 AND run_id = lower(run_id) "
            "AND substr(run_id, 9, 1) = '-' AND substr(run_id, 14, 1) = '-' "
            "AND substr(run_id, 19, 1) = '-' AND substr(run_id, 24, 1) = '-' "
            "AND length(replace(run_id, '-', '')) = 32 "
            "AND replace(run_id, '-', '') NOT GLOB '*[^0-9a-f]*'",
            name="run_id_lowercase_uuid",
        ),
        nullable=False,
        unique=True,
    )


# TheSuperHackers @feature Leex 21/08/2026 Persist immutable assets and source provenance separately from replay identity. (#TBD)
class ManagedAsset(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "managed_assets"
    __table_args__ = (
        lowercase_sha256_check("sha256"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        CheckConstraint(
            "length(relative_path) > 0 AND substr(relative_path, 1, 1) <> '/' "
            "AND substr(relative_path, -1, 1) <> '/' AND instr(relative_path, '\\') = 0 "
            "AND instr(relative_path, ':') = 0 AND instr(relative_path, char(0)) = 0 "
            "AND instr(relative_path, '//') = 0 AND relative_path NOT IN ('.', '..') "
            "AND relative_path NOT LIKE './%' AND relative_path NOT LIKE '../%' "
            "AND relative_path NOT LIKE '%/./%' AND relative_path NOT LIKE '%/../%' "
            "AND relative_path NOT LIKE '%/.' AND relative_path NOT LIKE '%/..'",
            name="relative_path_product_relative",
        ),
        Index("ix_managed_assets_kind_created_at", "kind", "created_at"),
    )

    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255))


class Source(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("file_size_bytes IS NULL OR file_size_bytes >= 0", name="file_size_bytes_nonnegative"),
        Index("ix_sources_replay_discovered_at", "replay_id", "discovered_at"),
        Index("ix_sources_strata_identity", "strata_match_id", "strata_source_user_token"),
    )

    replay_id: Mapped[int | None] = _fk("replays.id", "SET NULL", nullable=True)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    original_locator: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    strata_match_id: Mapped[str | None] = mapped_column(String(255))
    strata_source_user_token: Mapped[str | None] = mapped_column(String(255))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    provenance_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class Map(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "maps"
    __table_args__ = (
        lowercase_sha256_check("content_sha256"),
        CheckConstraint("schema_version >= 0", name="schema_version_nonnegative"),
        CheckConstraint("pathing_width >= 0 AND pathing_height >= 0", name="pathing_dimensions_nonnegative"),
        CheckConstraint("terrain_width >= 0 AND terrain_height >= 0", name="terrain_dimensions_nonnegative"),
        CheckConstraint("pathing_cell_size > 0 AND terrain_cell_size > 0", name="cell_sizes_positive"),
        Index("ix_maps_engine_data_map_identity", "engine_data_identity", "map_identity"),
    )

    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    manifest_asset_id: Mapped[int] = _fk("managed_assets.id", "RESTRICT")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_data_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    map_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    exporter_version: Mapped[str] = mapped_column(String(255), nullable=False)
    min_x: Mapped[float] = mapped_column(Float, nullable=False)
    min_y: Mapped[float] = mapped_column(Float, nullable=False)
    min_z: Mapped[float] = mapped_column(Float, nullable=False)
    max_x: Mapped[float] = mapped_column(Float, nullable=False)
    max_y: Mapped[float] = mapped_column(Float, nullable=False)
    max_z: Mapped[float] = mapped_column(Float, nullable=False)
    pathing_width: Mapped[int] = mapped_column(Integer, nullable=False)
    pathing_height: Mapped[int] = mapped_column(Integer, nullable=False)
    pathing_cell_size: Mapped[float] = mapped_column(Float, nullable=False)
    terrain_width: Mapped[int] = mapped_column(Integer, nullable=False)
    terrain_height: Mapped[int] = mapped_column(Integer, nullable=False)
    terrain_cell_size: Mapped[float] = mapped_column(Float, nullable=False)
    metadata_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class MapResource(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "map_resources"
    __table_args__ = (
        UniqueConstraint("map_id", "stable_key", name="uq_map_resources_map_stable_key"),
        CheckConstraint("source_object_id IS NULL OR source_object_id >= 0", name="source_object_id_nonnegative"),
        CheckConstraint("owner_player_index IS NULL OR owner_player_index >= 0", name="owner_player_index_nonnegative"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount_nonnegative"),
        Index("ix_map_resources_map_resource_kind", "map_id", "resource_kind"),
    )

    map_id: Mapped[int] = _fk("maps.id", "CASCADE")
    stable_key: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_id: Mapped[int | None] = mapped_column(Integer)
    template_name: Mapped[str | None] = mapped_column(String(255))
    owner_player_index: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[float | None] = mapped_column(Float)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    z: Mapped[float | None] = mapped_column(Float)
    payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class MapRegion(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "map_regions"
    __table_args__ = (
        UniqueConstraint("map_id", "stable_key", name="uq_map_regions_map_stable_key"),
        Index("ix_map_regions_map_region_kind", "map_id", "region_kind"),
    )

    map_id: Mapped[int] = _fk("maps.id", "CASCADE")
    stable_key: Mapped[str] = mapped_column(String(255), nullable=False)
    region_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    geometry_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    relationships_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class Replay(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "replays"
    __table_args__ = (
        lowercase_sha256_check("sha256"),
        CheckConstraint("version_number >= 0", name="version_number_nonnegative"),
        CheckConstraint("frame_count >= 0", name="frame_count_nonnegative"),
        CheckConstraint("starting_cash IS NULL OR starting_cash >= 0", name="starting_cash_nonnegative"),
        CheckConstraint(
            "lifecycle_state IN ('discovered','parsed','engine_verified','partial','desynced','unsupported','failed')",
            name="lifecycle_state_valid",
        ),
        Index("ix_replays_lifecycle_map_version_start", "lifecycle_state", "map_id", "version_number", "start_time"),
    )

    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    managed_asset_id: Mapped[int | None] = _fk("managed_assets.id", "RESTRICT", nullable=True)
    map_id: Mapped[int | None] = _fk("maps.id", "SET NULL", nullable=True)
    replay_name: Mapped[str] = mapped_column(Text, nullable=False)
    version_string: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[int] = mapped_column(Integer, nullable=False)
    end_time: Mapped[int] = mapped_column(Integer, nullable=False)
    exe_crc: Mapped[int] = mapped_column(Integer, nullable=False)
    ini_crc: Mapped[int] = mapped_column(Integer, nullable=False)
    map_crc: Mapped[int] = mapped_column(Integer, nullable=False)
    map_name: Mapped[str] = mapped_column(Text, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    starting_cash: Mapped[int | None] = mapped_column(Integer)
    header_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


# TheSuperHackers @feature Leex 21/08/2026 Separate immutable parser evidence from replay lifecycle projection. (#TBD)
class ParserRun(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "parser_runs"
    __table_args__ = (
        lowercase_sha256_check("input_sha256"),
        lowercase_sha256_check("result_sha256"),
        CheckConstraint(f"status IN ({RUN_STATUSES})", name="status_valid"),
        CheckConstraint(
            "completion_status IS NULL OR completion_status IN ('complete','truncated','unsupported','failed')",
            name="completion_status_valid",
        ),
        CheckConstraint("schema_version >= 0", name="schema_version_nonnegative"),
        CheckConstraint(
            "command_stream_offset IS NULL OR command_stream_offset >= 0", name="command_stream_offset_nonnegative"
        ),
        CheckConstraint("end_offset IS NULL OR end_offset >= 0", name="end_offset_nonnegative"),
        Index("ix_parser_runs_replay_status_version", "replay_id", "status", "parser_version"),
        Index(
            "uq_parser_runs_successful_identity",
            "replay_id",
            "parser_version",
            "schema_version",
            "input_sha256",
            unique=True,
            sqlite_where=text("status = 'succeeded'"),
        ),
    )

    run_id: Mapped[str] = _run_id_column()
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    parser_version: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    completion_status: Mapped[str | None] = mapped_column(String(32))
    command_stream_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    warnings_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    error_json: Mapped[JSON | None] = mapped_column(CanonicalJSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Player(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "players"
    __table_args__ = (CheckConstraint("identity_revision >= 0", name="identity_revision_nonnegative"),)

    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # TheSuperHackers @feature Leex 25/08/2026 Keep optional public profile metadata on canonical players only. (#TBD)
    external_profile_url: Mapped[str | None] = mapped_column(Text)
    external_profile_source: Mapped[str | None] = mapped_column(String(64))
    identity_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlayerAlias(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "player_aliases"
    __table_args__ = (
        UniqueConstraint("namespace", "normalized_name", name="uq_player_aliases_namespace_normalized_name"),
        Index("ix_player_aliases_player_namespace", "player_id", "namespace"),
    )

    player_id: Mapped[int] = _fk("players.id", "CASCADE")
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    external_subject: Mapped[str | None] = mapped_column(Text)


# TheSuperHackers @feature Leex 22/08/2026 Preserve every canonical identity change as an immutable audit operation. (#TBD)
class PlayerIdentityOperation(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "player_identity_operations"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('auto_link','merge_players','split_alias','attach_external_alias','inverse')",
            name="operation_kind_valid",
        ),
        CheckConstraint("length(trim(actor)) > 0", name="actor_nonempty"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_nonempty"),
        Index("ix_player_identity_operations_created_kind", "created_at", "operation_kind"),
        Index("ix_player_identity_operations_inverse_of", "inverse_of_operation_id"),
    )

    operation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    inverse_of_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("player_identity_operations.id", ondelete="RESTRICT"), nullable=True
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    after_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    inverse_payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    affected_revisions_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class ReplayPlayer(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "replay_players"
    __table_args__ = (
        UniqueConstraint("parser_run_id", "slot_index", name="uq_replay_players_parser_slot"),
        CheckConstraint("slot_index >= 0", name="slot_index_nonnegative"),
        CheckConstraint("player_index IS NULL OR player_index >= 0", name="player_index_nonnegative"),
        Index("ix_replay_players_player_replay", "player_id", "replay_id"),
        Index("ix_replay_players_replay_player_index", "replay_id", "player_index"),
    )

    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    parser_run_id: Mapped[int] = _fk("parser_runs.id", "CASCADE")
    player_id: Mapped[int | None] = _fk("players.id", "SET NULL", nullable=True)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text)
    normalized_name: Mapped[str | None] = mapped_column(Text)
    player_index: Mapped[int | None] = mapped_column(Integer)
    team_id: Mapped[int | None] = mapped_column(Integer)
    faction: Mapped[str | None] = mapped_column(String(255))
    color: Mapped[str | None] = mapped_column(String(255))
    start_position: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str | None] = mapped_column(String(64))
    observed_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class ReplayCommand(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint("parser_run_id", "command_index", name="uq_commands_parser_command_index"),
        UniqueConstraint("evidence_item_id", name="uq_commands_evidence_item_id"),
        CheckConstraint("command_index >= 0", name="command_index_nonnegative"),
        CheckConstraint("frame >= 0", name="frame_nonnegative"),
        CheckConstraint("player_index >= 0", name="player_index_nonnegative"),
        CheckConstraint("message_type >= 0", name="message_type_nonnegative"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_valid"),
        Index("ix_commands_replay_frame_message_type", "replay_id", "frame", "message_type"),
        Index("ix_commands_replay_player_frame", "replay_id", "player_index", "frame"),
        Index("ix_commands_parser_start_offset", "parser_run_id", "start_offset"),
    )

    parser_run_id: Mapped[int] = _fk("parser_runs.id", "CASCADE")
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    command_index: Mapped[int] = mapped_column(Integer, nullable=False)
    frame: Mapped[int] = mapped_column(Integer, nullable=False)
    player_index: Mapped[int] = mapped_column(Integer, nullable=False)
    message_type: Mapped[int] = mapped_column(Integer, nullable=False)
    message_name: Mapped[str | None] = mapped_column(String(255))
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    arguments_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    evidence_item_id: Mapped[int] = _fk("evidence_items.id", "RESTRICT")


# TheSuperHackers @feature Leex 21/08/2026 Preserve validated engine telemetry as immutable versioned observations. (#TBD)
class TelemetryRun(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "telemetry_runs"
    __table_args__ = (
        lowercase_sha256_check("engine_executable_sha256"),
        lowercase_sha256_check("trace_sha256"),
        CheckConstraint("schema_version >= 0", name="schema_version_nonnegative"),
        CheckConstraint(f"status IN ({RUN_STATUSES})", name="status_valid"),
        CheckConstraint("process_exit_code IS NULL OR process_exit_code >= 0", name="process_exit_code_nonnegative"),
        CheckConstraint("final_frame IS NULL OR final_frame >= 0", name="final_frame_nonnegative"),
        CheckConstraint("command_count IS NULL OR command_count >= 0", name="command_count_nonnegative"),
        Index("ix_telemetry_runs_replay_status_started", "replay_id", "status", "started_at"),
    )

    run_id: Mapped[str] = _run_id_column()
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    trace_asset_id: Mapped[int | None] = _fk("managed_assets.id", "RESTRICT", nullable=True)
    catalog_asset_id: Mapped[int | None] = _fk("managed_assets.id", "RESTRICT", nullable=True)
    map_asset_id: Mapped[int | None] = _fk("managed_assets.id", "RESTRICT", nullable=True)
    map_id: Mapped[int | None] = _fk("maps.id", "SET NULL", nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_build: Mapped[str] = mapped_column(String(255), nullable=False)
    engine_executable_sha256: Mapped[str | None] = mapped_column(String(64))
    settings_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    runner_status: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_analysis_scope: Mapped[str | None] = mapped_column(String(64))
    process_exit_code: Mapped[int | None] = mapped_column(Integer)
    final_frame: Mapped[int | None] = mapped_column(Integer)
    command_count: Mapped[int | None] = mapped_column(Integer)
    trace_sha256: Mapped[str | None] = mapped_column(String(64))
    diagnostics_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelemetryEvent(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "telemetry_events"
    __table_args__ = (
        UniqueConstraint("telemetry_run_id", "sequence", name="uq_telemetry_events_run_sequence"),
        UniqueConstraint("evidence_item_id", name="uq_telemetry_events_evidence_item_id"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("frame >= 0", name="frame_nonnegative"),
        CheckConstraint("logic_time_seconds >= 0", name="logic_time_seconds_nonnegative"),
        CheckConstraint("schema_version >= 0", name="schema_version_nonnegative"),
        Index("ix_telemetry_events_run_frame_type", "telemetry_run_id", "frame", "event_type"),
        Index("ix_telemetry_events_run_type_frame", "telemetry_run_id", "event_type", "frame"),
    )

    telemetry_run_id: Mapped[int] = _fk("telemetry_runs.id", "CASCADE")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    frame: Mapped[int] = mapped_column(Integer, nullable=False)
    logic_time_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    raw_record_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    evidence_item_id: Mapped[int] = _fk("evidence_items.id", "RESTRICT")


class Entity(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("telemetry_run_id", "object_id", name="uq_entities_run_object_id"),
        CheckConstraint("object_id >= 0", name="object_id_nonnegative"),
        CheckConstraint(
            "initial_owner_player_index IS NULL OR initial_owner_player_index >= 0", name="initial_owner_nonnegative"
        ),
        CheckConstraint("initial_team_id IS NULL OR initial_team_id >= 0", name="initial_team_nonnegative"),
        CheckConstraint("creation_sequence IS NULL OR creation_sequence >= 0", name="creation_sequence_nonnegative"),
        CheckConstraint("creation_frame IS NULL OR creation_frame >= 0", name="creation_frame_nonnegative"),
        CheckConstraint(
            "destruction_sequence IS NULL OR destruction_sequence >= 0", name="destruction_sequence_nonnegative"
        ),
        CheckConstraint("destruction_frame IS NULL OR destruction_frame >= 0", name="destruction_frame_nonnegative"),
        Index("ix_entities_replay_template_name", "replay_id", "template_name"),
        Index("ix_entities_replay_initial_owner", "replay_id", "initial_owner_player_index"),
    )

    telemetry_run_id: Mapped[int] = _fk("telemetry_runs.id", "CASCADE")
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    object_id: Mapped[int] = mapped_column(Integer, nullable=False)
    template_name: Mapped[str] = mapped_column(String(255), nullable=False)
    initial_owner_player_index: Mapped[int | None] = mapped_column(Integer)
    initial_team_id: Mapped[int | None] = mapped_column(Integer)
    kind_of_flags_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    creation_sequence: Mapped[int | None] = mapped_column(Integer)
    creation_frame: Mapped[int | None] = mapped_column(Integer)
    destruction_sequence: Mapped[int | None] = mapped_column(Integer)
    destruction_frame: Mapped[int | None] = mapped_column(Integer)
    observed_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class EntitySample(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "entity_samples"
    __table_args__ = (
        UniqueConstraint("telemetry_event_id", name="uq_entity_samples_telemetry_event_id"),
        UniqueConstraint("entity_id", "sequence", name="uq_entity_samples_entity_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("frame >= 0", name="frame_nonnegative"),
        Index("ix_entity_samples_entity_frame", "entity_id", "frame"),
    )

    telemetry_run_id: Mapped[int] = _fk("telemetry_runs.id", "CASCADE")
    entity_id: Mapped[int] = _fk("entities.id", "CASCADE")
    telemetry_event_id: Mapped[int] = _fk("telemetry_events.id", "CASCADE")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    frame: Mapped[int] = mapped_column(Integer, nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    z: Mapped[float] = mapped_column(Float, nullable=False)
    orientation: Mapped[float] = mapped_column(Float, nullable=False)
    speed: Mapped[float | None] = mapped_column(Float)
    layer: Mapped[str | None] = mapped_column(String(64))
    locomotor_name: Mapped[str | None] = mapped_column(String(255))
    order_type: Mapped[str | None] = mapped_column(String(255))
    path_goal_x: Mapped[float | None] = mapped_column(Float)
    path_goal_y: Mapped[float | None] = mapped_column(Float)
    path_goal_z: Mapped[float | None] = mapped_column(Float)
    current_state: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    sample_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class ProductionEvent(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "production_events"
    __table_args__ = (
        UniqueConstraint("telemetry_event_id", name="uq_production_events_telemetry_event_id"),
        CheckConstraint("frame >= 0", name="frame_nonnegative"),
        CheckConstraint("queue_position IS NULL OR queue_position >= 0", name="queue_position_nonnegative"),
        CheckConstraint("queued_frame IS NULL OR queued_frame >= 0", name="queued_frame_nonnegative"),
        CheckConstraint("terminal_frame IS NULL OR terminal_frame >= 0", name="terminal_frame_nonnegative"),
        CheckConstraint("cost IS NULL OR cost >= 0", name="cost_nonnegative"),
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        Index("ix_production_events_replay_player_frame", "replay_id", "replay_player_id", "frame"),
        Index("ix_production_events_run_type_frame", "telemetry_run_id", "event_type", "frame"),
    )

    telemetry_run_id: Mapped[int] = _fk("telemetry_runs.id", "CASCADE")
    telemetry_event_id: Mapped[int] = _fk("telemetry_events.id", "CASCADE")
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    producer_entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    frame: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    item_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False)
    production_id: Mapped[int | None] = mapped_column(Integer)
    upgrade_id: Mapped[int | None] = mapped_column(Integer)
    queue_position: Mapped[int | None] = mapped_column(Integer)
    queued_frame: Mapped[int | None] = mapped_column(Integer)
    terminal_frame: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class EconomyEvent(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "economy_events"
    __table_args__ = (
        UniqueConstraint("telemetry_event_id", name="uq_economy_events_telemetry_event_id"),
        CheckConstraint("frame >= 0", name="frame_nonnegative"),
        Index("ix_economy_events_replay_player_frame", "replay_id", "replay_player_id", "frame"),
        Index("ix_economy_events_run_type_frame", "telemetry_run_id", "event_type", "frame"),
    )

    telemetry_run_id: Mapped[int] = _fk("telemetry_runs.id", "CASCADE")
    telemetry_event_id: Mapped[int] = _fk("telemetry_events.id", "CASCADE")
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    collector_entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    source_entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    dropoff_entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    frame: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    balance_before: Mapped[float | None] = mapped_column(Float)
    amount_delta: Mapped[float | None] = mapped_column(Float)
    balance_after: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(String(255))
    location_x: Mapped[float | None] = mapped_column(Float)
    location_y: Mapped[float | None] = mapped_column(Float)
    location_z: Mapped[float | None] = mapped_column(Float)
    payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class CombatEvent(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "combat_events"
    __table_args__ = (
        UniqueConstraint("telemetry_event_id", name="uq_combat_events_telemetry_event_id"),
        CheckConstraint("frame >= 0", name="frame_nonnegative"),
        CheckConstraint("killing_blow IS NULL OR killing_blow IN (0, 1)", name="killing_blow_boolean"),
        Index("ix_combat_events_replay_frame_type", "replay_id", "frame", "event_type"),
        Index("ix_combat_events_attacker_frame", "attacker_entity_id", "frame"),
        Index("ix_combat_events_victim_frame", "victim_entity_id", "frame"),
    )

    telemetry_run_id: Mapped[int] = _fk("telemetry_runs.id", "CASCADE")
    telemetry_event_id: Mapped[int] = _fk("telemetry_events.id", "CASCADE")
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    frame: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    attacker_entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    victim_entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    source_entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    attacker_replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    victim_replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    weapon_name: Mapped[str | None] = mapped_column(String(255))
    damage_type: Mapped[str | None] = mapped_column(String(255))
    death_type: Mapped[str | None] = mapped_column(String(255))
    attempted_amount: Mapped[float | None] = mapped_column(Float)
    calculated_amount: Mapped[float | None] = mapped_column(Float)
    applied_amount: Mapped[float | None] = mapped_column(Float)
    health_before: Mapped[float | None] = mapped_column(Float)
    health_after: Mapped[float | None] = mapped_column(Float)
    killing_blow: Mapped[bool | None] = mapped_column(Boolean)
    location_x: Mapped[float | None] = mapped_column(Float)
    location_y: Mapped[float | None] = mapped_column(Float)
    location_z: Mapped[float | None] = mapped_column(Float)
    payload_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class EvidenceItem(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        UniqueConstraint("source_kind", "source_key", name="uq_evidence_items_source_identity"),
        CheckConstraint("tier IN ('observed','derived','inferred')", name="tier_valid"),
        CheckConstraint("schema_version >= 0", name="schema_version_nonnegative"),
        Index("ix_evidence_items_replay_tier_source", "replay_id", "tier", "source_kind"),
    )

    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    parser_run_id: Mapped[int | None] = _fk("parser_runs.id", "RESTRICT", nullable=True)
    telemetry_run_id: Mapped[int | None] = _fk("telemetry_runs.id", "RESTRICT", nullable=True)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)


class ReplayQualityIssue(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "replay_quality_issues"
    __table_args__ = (Index("ix_replay_quality_issues_replay_code_detected", "replay_id", "issue_code", "detected_at"),)

    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    parser_run_id: Mapped[int | None] = _fk("parser_runs.id", "RESTRICT", nullable=True)
    telemetry_run_id: Mapped[int | None] = _fk("telemetry_runs.id", "RESTRICT", nullable=True)
    evidence_item_id: Mapped[int | None] = _fk("evidence_items.id", "RESTRICT", nullable=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_code: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    details_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# TheSuperHackers @feature Leex 21/08/2026 Store deterministic features with typed values and universal evidence links. (#TBD)
class FeatureSet(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "feature_sets"
    __table_args__ = (
        lowercase_sha256_check("input_digest"),
        lowercase_sha256_check("cache_key"),
        CheckConstraint(f"status IN ({RUN_STATUSES})", name="status_valid"),
        Index(
            "uq_feature_sets_successful_cache_key",
            "cache_key",
            unique=True,
            sqlite_where=text("status = 'succeeded'"),
        ),
        Index("ix_feature_sets_replay_extractor_status", "replay_id", "extractor_name", "extractor_version", "status"),
    )

    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    extractor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(255), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    settings_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[JSON | None] = mapped_column(CanonicalJSON)


class Feature(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("evidence_item_id", name="uq_features_evidence_item_id"),
        UniqueConstraint(
            "feature_set_id",
            "name",
            "scope_type",
            "scope_key",
            "frame_start",
            "frame_end",
            name="uq_features_logical_identity",
        ),
        CheckConstraint(
            "value_type IN ('integer','real','text','boolean','json')",
            name="value_type_valid",
        ),
        CheckConstraint(f"quality IN ({QUALITY_VALUES})", name="quality_valid"),
        CheckConstraint("frame_start >= 0 AND frame_end >= frame_start", name="frame_window_valid"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_bounded"),
        CheckConstraint("boolean_value IS NULL OR boolean_value IN (0, 1)", name="boolean_value_boolean"),
        CheckConstraint(
            "(quality = 'available' AND quality_reason IS NULL AND "
            "((value_type = 'integer' AND integer_value IS NOT NULL AND real_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND json_value IS NULL) OR "
            "(value_type = 'real' AND integer_value IS NULL AND real_value IS NOT NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND json_value IS NULL) OR "
            "(value_type = 'text' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NOT NULL "
            "AND boolean_value IS NULL AND json_value IS NULL) OR "
            "(value_type = 'boolean' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NOT NULL AND json_value IS NULL) OR "
            "(value_type = 'json' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND json_value IS NOT NULL))) OR "
            "(quality = 'partial' AND quality_reason IS NOT NULL AND length(trim(quality_reason)) > 0 AND "
            "((value_type = 'integer' AND integer_value IS NOT NULL AND real_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND json_value IS NULL) OR "
            "(value_type = 'real' AND integer_value IS NULL AND real_value IS NOT NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND json_value IS NULL) OR "
            "(value_type = 'text' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NOT NULL "
            "AND boolean_value IS NULL AND json_value IS NULL) OR "
            "(value_type = 'boolean' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NOT NULL AND json_value IS NULL) OR "
            "(value_type = 'json' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND json_value IS NOT NULL))) OR "
            "(quality = 'unavailable' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND json_value IS NULL AND quality_reason IS NOT NULL "
            "AND length(trim(quality_reason)) > 0)",
            name="typed_value_matches_quality",
        ),
        Index("ix_features_name_scope_window", "name", "scope_type", "scope_key", "frame_start", "frame_end"),
    )

    feature_set_id: Mapped[int] = _fk("feature_sets.id", "CASCADE")
    evidence_item_id: Mapped[int] = _fk("evidence_items.id", "RESTRICT")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    integer_value: Mapped[int | None] = mapped_column(Integer)
    real_value: Mapped[float | None] = mapped_column(Float)
    text_value: Mapped[str | None] = mapped_column(Text)
    boolean_value: Mapped[bool | None] = mapped_column(Boolean)
    json_value: Mapped[JSON | None] = mapped_column(CanonicalJSON)
    unit: Mapped[str | None] = mapped_column(String(64))
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer)
    entity_id: Mapped[int | None] = _fk("entities.id", "SET NULL", nullable=True)
    frame_start: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    explanation: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class FeatureEvidence(Base):
    __tablename__ = "feature_evidence"
    __table_args__ = (CheckConstraint("role IN ('input','supporting','contradicting')", name="role_valid"),)

    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id", ondelete="CASCADE"), primary_key=True, index=True)
    evidence_item_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_items.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)


class AnalysisRun(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        lowercase_sha256_check("model_digest"),
        lowercase_sha256_check("prompt_digest"),
        lowercase_sha256_check("response_schema_digest"),
        lowercase_sha256_check("settings_digest"),
        lowercase_sha256_check("input_digest"),
        lowercase_sha256_check("cache_key"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','invalid','unavailable')",
            name="status_valid",
        ),
        Index(
            "uq_analysis_runs_successful_cache_key",
            "cache_key",
            unique=True,
            sqlite_where=text("status = 'succeeded'"),
        ),
        Index("ix_analysis_runs_replay_status_created", "replay_id", "status", "created_at"),
    )

    run_id: Mapped[str] = _run_id_column()
    replay_id: Mapped[int | None] = _fk("replays.id", "CASCADE", nullable=True)
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_schema_version: Mapped[str] = mapped_column(String(255), nullable=False)
    response_schema_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    settings_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_response_asset_id: Mapped[int | None] = _fk("managed_assets.id", "RESTRICT", nullable=True)
    validated_response_json: Mapped[JSON | None] = mapped_column(CanonicalJSON)
    diagnostics_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    error_json: Mapped[JSON | None] = mapped_column(CanonicalJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyAssessment(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "strategy_assessments"
    __table_args__ = (
        UniqueConstraint("evidence_item_id", name="uq_strategy_assessments_evidence_item_id"),
        CheckConstraint("method IN ('rule','statistical','llm','manual')", name="method_valid"),
        CheckConstraint("frame_start >= 0 AND frame_end >= frame_start", name="frame_window_valid"),
        CheckConstraint(f"quality IN ({QUALITY_VALUES})", name="quality_valid"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_bounded"),
        Index("ix_strategy_assessments_replay_label_phase_method", "replay_id", "strategy_label", "phase", "method"),
    )

    evidence_item_id: Mapped[int] = _fk("evidence_items.id", "RESTRICT")
    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    analysis_run_id: Mapped[int | None] = _fk("analysis_runs.id", "SET NULL", nullable=True)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_label: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy_version: Mapped[str | None] = mapped_column(String(255))
    rule_version: Mapped[str | None] = mapped_column(String(255))
    model_version: Mapped[str | None] = mapped_column(String(255))
    frame_start: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    details_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class AssessmentEvidence(Base):
    __tablename__ = "assessment_evidence"
    __table_args__ = (CheckConstraint("role IN ('supporting','contradicting')", name="role_valid"),)

    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_assessments.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    evidence_item_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_items.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)


class LongitudinalRun(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "longitudinal_runs"
    __table_args__ = (
        lowercase_sha256_check("input_digest"),
        lowercase_sha256_check("cache_key"),
        CheckConstraint("identity_revision >= 0", name="identity_revision_nonnegative"),
        CheckConstraint(f"status IN ({RUN_STATUSES})", name="status_valid"),
        Index(
            "uq_longitudinal_runs_successful_cache_key",
            "cache_key",
            unique=True,
            sqlite_where=text("status = 'succeeded'"),
        ),
        Index("ix_longitudinal_runs_player_revision_status", "player_id", "identity_revision", "status"),
    )

    run_id: Mapped[str] = _run_id_column()
    player_id: Mapped[int] = _fk("players.id", "CASCADE")
    identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    analyzer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(255), nullable=False)
    segment_key_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    settings_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[JSON | None] = mapped_column(CanonicalJSON)


class LongitudinalResult(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "longitudinal_results"
    __table_args__ = (
        UniqueConstraint("evidence_item_id", name="uq_longitudinal_results_evidence_item_id"),
        CheckConstraint("sample_count >= 0 AND missing_count >= 0", name="counts_nonnegative"),
        CheckConstraint(f"quality IN ({QUALITY_VALUES})", name="quality_valid"),
        CheckConstraint(
            "(quality = 'unavailable' AND quality_reason IS NOT NULL) OR quality != 'unavailable'",
            name="unavailable_reason_required",
        ),
        Index("ix_longitudinal_results_run_result_name", "longitudinal_run_id", "result_name"),
    )

    longitudinal_run_id: Mapped[int] = _fk("longitudinal_runs.id", "CASCADE")
    evidence_item_id: Mapped[int] = _fk("evidence_items.id", "RESTRICT")
    result_name: Mapped[str] = mapped_column(String(255), nullable=False)
    result_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_reason: Mapped[str | None] = mapped_column(Text)
    statistics_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class LongitudinalMember(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "longitudinal_members"
    __table_args__ = (
        UniqueConstraint(
            "longitudinal_result_id",
            "evidence_item_id",
            name="uq_longitudinal_members_result_evidence",
        ),
    )

    longitudinal_result_id: Mapped[int] = _fk("longitudinal_results.id", "CASCADE")
    replay_id: Mapped[int] = _fk("replays.id", "RESTRICT")
    replay_player_id: Mapped[int] = _fk("replay_players.id", "RESTRICT")
    feature_set_id: Mapped[int] = _fk("feature_sets.id", "RESTRICT")
    feature_id: Mapped[int | None] = _fk("features.id", "RESTRICT", nullable=True)
    strategy_assessment_id: Mapped[int | None] = _fk("strategy_assessments.id", "RESTRICT", nullable=True)
    evidence_item_id: Mapped[int] = _fk("evidence_items.id", "RESTRICT")


class Report(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (
        lowercase_sha256_check("input_digest"),
        lowercase_sha256_check("cache_key"),
        UniqueConstraint("replay_id", "report_version", "input_digest", name="uq_reports_replay_version_input"),
        Index("ix_reports_replay_created_at", "replay_id", "created_at"),
    )

    replay_id: Mapped[int] = _fk("replays.id", "CASCADE")
    replay_player_id: Mapped[int | None] = _fk("replay_players.id", "SET NULL", nullable=True)
    analysis_run_id: Mapped[int | None] = _fk("analysis_runs.id", "SET NULL", nullable=True)
    report_version: Mapped[str] = mapped_column(String(255), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    report_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    structured_asset_id: Mapped[int | None] = _fk("managed_assets.id", "RESTRICT", nullable=True)
    rendered_asset_id: Mapped[int | None] = _fk("managed_assets.id", "RESTRICT", nullable=True)


# TheSuperHackers @feature Leex 21/08/2026 Persist resumable idempotent jobs with explicit dependency edges and leases. (#TBD)
class Job(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"status IN ({JOB_STATUSES})", name="status_valid"),
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint(
            "max_attempts >= 1 AND attempt_count >= 0 AND attempt_count <= max_attempts", name="attempts_valid"
        ),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        CheckConstraint("retryable IN (0, 1)", name="retryable_boolean"),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled') AND completed_at IS NOT NULL) OR "
            "(status IN ('pending','running') AND completed_at IS NULL)",
            name="terminal_state_valid",
        ),
        CheckConstraint("status NOT IN ('succeeded','cancelled') OR retryable = 0", name="terminal_retry_valid"),
        lowercase_sha256_check("lease_token_sha256"),
        CheckConstraint(
            "lease_execution_public_id IS NULL OR (length(lease_execution_public_id) = 36 "
            "AND lease_execution_public_id = lower(lease_execution_public_id) "
            "AND substr(lease_execution_public_id, 9, 1) = '-' "
            "AND substr(lease_execution_public_id, 14, 1) = '-' "
            "AND substr(lease_execution_public_id, 19, 1) = '-' "
            "AND substr(lease_execution_public_id, 24, 1) = '-' "
            "AND length(replace(lease_execution_public_id, '-', '')) = 32 "
            "AND replace(lease_execution_public_id, '-', '') NOT GLOB '*[^0-9a-f]*')",
            name="lease_execution_uuid",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL AND length(trim(lease_owner)) > 0 "
            "AND lease_expires_at IS NOT NULL AND lease_token_sha256 IS NOT NULL "
            "AND lease_execution_public_id IS NOT NULL AND last_heartbeat_at IS NOT NULL) OR "
            "(status != 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND lease_token_sha256 IS NULL AND lease_execution_public_id IS NULL AND last_heartbeat_at IS NULL)",
            name="lease_state_valid",
        ),
        CheckConstraint(
            "(cancel_requested_at IS NULL AND cancel_requested_by IS NULL AND cancel_reason_code IS NULL) OR "
            "(cancel_requested_at IS NOT NULL AND cancel_requested_by IS NOT NULL "
            "AND length(trim(cancel_requested_by)) > 0 AND cancel_reason_code IS NOT NULL "
            "AND length(trim(cancel_reason_code)) > 0 AND status IN ('running','failed','cancelled'))",
            name="cancellation_facts_all_or_none",
        ),
        CheckConstraint(
            "(progress_completed IS NULL AND progress_total IS NULL AND progress_unit IS NULL "
            "AND progress_updated_at IS NULL) OR (progress_completed IS NOT NULL AND progress_total IS NOT NULL "
            "AND progress_unit IS NOT NULL AND progress_updated_at IS NOT NULL AND progress_completed >= 0 "
            "AND progress_total > 0 AND progress_completed <= progress_total "
            "AND length(trim(progress_unit)) BETWEEN 1 AND 64)",
            name="progress_all_or_none",
        ),
        Index("ix_jobs_claim", "status", "stage", "available_at", "priority"),
        Index("ix_jobs_recovery", "status", "lease_expires_at"),
        Index("ix_jobs_cancellation", "status", "cancel_requested_at"),
        Index("ix_jobs_ui_listing", "replay_id", "status", "created_at"),
    )

    replay_id: Mapped[int | None] = _fk("replays.id", "SET NULL", nullable=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    component_version: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)
    output_json: Mapped[JSON | None] = mapped_column(CanonicalJSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details_json: Mapped[JSON | None] = mapped_column(CanonicalJSON)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token_sha256: Mapped[str | None] = mapped_column(String(64))
    lease_execution_public_id: Mapped[str | None] = mapped_column(String(36))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_by: Mapped[str | None] = mapped_column(String(255))
    cancel_reason_code: Mapped[str | None] = mapped_column(String(128))
    progress_completed: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    progress_unit: Mapped[str | None] = mapped_column(String(64))
    progress_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobDependency(Base):
    __tablename__ = "job_dependencies"
    __table_args__ = (
        CheckConstraint("job_id != depends_on_job_id", name="not_self_edge"),
        Index("ix_job_dependencies_depends_on_job_id", "depends_on_job_id"),
    )

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True, index=True)
    depends_on_job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


# TheSuperHackers @feature Leex 22/08/2026 Retain crash-safe stage output before lifecycle settlement. (#TBD)
class JobStageResult(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "job_stage_results"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_job_stage_results_job_id"),
        UniqueConstraint("job_id", "idempotency_key", name="uq_job_stage_results_job_identity"),
        Index("ix_job_stage_results_job_identity", "job_id", "idempotency_key", unique=True),
    )

    job_id: Mapped[int] = _fk("jobs.id", "RESTRICT")
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    component_version: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[JSON] = mapped_column(CanonicalJSON, nullable=False)


class JobEvent(IntegerPrimaryKeyMixin, PublicIdMixin, Base):
    __tablename__ = "job_events"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="attempt_nonnegative"),
        CheckConstraint(
            "event_kind IN ('claimed','progress','cancel_requested','cancelled','succeeded','failed',"
            "'retry_requested','lease_expired','worker_shutdown','dependency_failed','dependency_cancelled','result_reused')",
            name="kind_valid",
        ),
        CheckConstraint(f"state IN ({JOB_STATUSES})", name="state_valid"),
        UniqueConstraint("job_id", "revision", name="uq_job_events_job_revision"),
        Index("ix_job_events_job_revision", "job_id", "revision"),
    )

    job_id: Mapped[int] = _fk("jobs.id", "RESTRICT")
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class JobLogSnapshot(IntegerPrimaryKeyMixin, PublicIdMixin, CreatedAtMixin, Base):
    __tablename__ = "job_log_snapshots"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_nonnegative"),
        CheckConstraint("label IN ('stdout','stderr','supervisor')", name="label_valid"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("media_type = 'text/plain'", name="media_type_text"),
        CheckConstraint("byte_count >= 0", name="byte_count_nonnegative"),
        CheckConstraint("length(trim(redaction_version)) > 0", name="redaction_nonempty"),
        CheckConstraint("integrity_version = 'sha256-merkle-v1'", name="integrity_version_closed"),
        lowercase_sha256_check("integrity_root_sha256"),
        CheckConstraint("integrity_chunk_size = 4096", name="integrity_chunk_size_fixed"),
        UniqueConstraint(
            "job_id", "attempt_count", "label", "sequence", name="uq_job_log_snapshots_stream_sequence"
        ),
        Index("ix_job_log_snapshots_stream", "job_id", "attempt_count", "label", "sequence"),
    )

    job_id: Mapped[int] = _fk("jobs.id", "RESTRICT")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    managed_asset_id: Mapped[int] = _fk("managed_assets.id", "RESTRICT")
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    redaction_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # TheSuperHackers @feature Leex 22/08/2026 Persist a closed root descriptor for bounded authenticated log pages. (#TBD)
    integrity_version: Mapped[str] = mapped_column(String(32), nullable=False)
    integrity_root_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    integrity_chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)


def _abort_trigger(name: str, timing: str, table: str, when: str, message: str) -> str:
    return f"CREATE TRIGGER {name} BEFORE {timing} ON {table} WHEN {when} BEGIN SELECT RAISE(ABORT, '{message}'); END"


def _child_triggers(table: str, run_column: str, run_table: str) -> list[tuple[str, str]]:
    stem = f"trg_{table}_succeeded"
    old_succeeded = f"EXISTS (SELECT 1 FROM {run_table} WHERE id = OLD.{run_column} AND status = 'succeeded')"
    new_succeeded = f"EXISTS (SELECT 1 FROM {run_table} WHERE id = NEW.{run_column} AND status = 'succeeded')"
    return [
        (
            f"{stem}_no_insert",
            _abort_trigger(
                f"{stem}_no_insert", "INSERT", table, new_succeeded, "successful observations cannot be appended"
            ),
        ),
        (
            f"{stem}_no_update",
            _abort_trigger(
                f"{stem}_no_update",
                "UPDATE",
                table,
                f"{old_succeeded} OR {new_succeeded}",
                "successful observations cannot be updated",
            ),
        ),
        (
            f"{stem}_no_delete",
            _abort_trigger(
                f"{stem}_no_delete", "DELETE", table, old_succeeded, "successful observations cannot be deleted"
            ),
        ),
    ]


def immutability_triggers() -> list[tuple[str, str]]:
    """Return deterministic trigger names and DDL for successful observation immutability."""
    triggers: list[tuple[str, str]] = []
    for table in ("parser_runs", "telemetry_runs"):
        stem = f"trg_{table}_succeeded"
        for timing in ("UPDATE", "DELETE"):
            name = f"{stem}_no_{timing.lower()}"
            triggers.append(
                (
                    name,
                    _abort_trigger(
                        name,
                        timing,
                        table,
                        "OLD.status = 'succeeded'",
                        "successful observation run is immutable",
                    ),
                )
            )

    triggers.extend(_child_triggers("commands", "parser_run_id", "parser_runs"))
    for table in (
        "telemetry_events",
        "entities",
        "entity_samples",
        "production_events",
        "economy_events",
        "combat_events",
    ):
        triggers.extend(_child_triggers(table, "telemetry_run_id", "telemetry_runs"))

    evidence_stem = "trg_evidence_items_observed"
    old_observed = (
        "OLD.tier = 'observed' AND ("
        "EXISTS (SELECT 1 FROM parser_runs WHERE id = OLD.parser_run_id AND status = 'succeeded') OR "
        "EXISTS (SELECT 1 FROM telemetry_runs WHERE id = OLD.telemetry_run_id AND status = 'succeeded'))"
    )
    new_observed = (
        "NEW.tier = 'observed' AND ("
        "EXISTS (SELECT 1 FROM parser_runs WHERE id = NEW.parser_run_id AND status = 'succeeded') OR "
        "EXISTS (SELECT 1 FROM telemetry_runs WHERE id = NEW.telemetry_run_id AND status = 'succeeded'))"
    )
    for timing, when, message in (
        ("INSERT", new_observed, "successful observed evidence cannot be appended"),
        ("UPDATE", f"({old_observed}) OR ({new_observed})", "successful observed evidence cannot be updated"),
        ("DELETE", old_observed, "successful observed evidence cannot be deleted"),
    ):
        name = f"{evidence_stem}_no_{timing.lower()}"
        triggers.append((name, _abort_trigger(name, timing, "evidence_items", when, message)))

    old_parent_succeeded = "EXISTS (SELECT 1 FROM parser_runs WHERE id = OLD.parser_run_id AND status = 'succeeded')"
    new_parent_succeeded = "EXISTS (SELECT 1 FROM parser_runs WHERE id = NEW.parser_run_id AND status = 'succeeded')"
    changed = " OR ".join(
        f"NEW.{column} IS NOT OLD.{column}"
        for column in (
            "public_id",
            "replay_id",
            "parser_run_id",
            "slot_index",
            "slot_kind",
            "original_name",
            "normalized_name",
            "player_index",
            "team_id",
            "faction",
            "color",
            "start_position",
            "result",
            "observed_json",
        )
    )
    triggers.extend(
        [
            (
                "trg_replay_players_succeeded_no_insert",
                _abort_trigger(
                    "trg_replay_players_succeeded_no_insert",
                    "INSERT",
                    "replay_players",
                    new_parent_succeeded,
                    "successful parser player evidence cannot be appended",
                ),
            ),
            (
                "trg_replay_players_succeeded_no_delete",
                _abort_trigger(
                    "trg_replay_players_succeeded_no_delete",
                    "DELETE",
                    "replay_players",
                    old_parent_succeeded,
                    "successful parser player evidence cannot be deleted",
                ),
            ),
            (
                "trg_replay_players_succeeded_no_observation_update",
                _abort_trigger(
                    "trg_replay_players_succeeded_no_observation_update",
                    "UPDATE",
                    "replay_players",
                    f"({old_parent_succeeded}) AND ({changed})",
                    "successful parser player observations cannot be updated",
                ),
            ),
        ]
    )
    for table in ("job_stage_results", "job_events", "job_log_snapshots"):
        for timing in ("UPDATE", "DELETE"):
            name = f"trg_{table}_no_{timing.lower()}"
            triggers.append(
                (
                    name,
                    (
                        f"CREATE TRIGGER {name} BEFORE {timing} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END"
                    ),
                )
            )
    return triggers


BASELINE_TABLE_NAMES = (
    "managed_assets",
    "sources",
    "maps",
    "map_resources",
    "map_regions",
    "replays",
    "replay_quality_issues",
    "parser_runs",
    "players",
    "player_aliases",
    "replay_players",
    "commands",
    "telemetry_runs",
    "telemetry_events",
    "entities",
    "entity_samples",
    "production_events",
    "economy_events",
    "combat_events",
    "evidence_items",
    "feature_sets",
    "features",
    "feature_evidence",
    "analysis_runs",
    "strategy_assessments",
    "assessment_evidence",
    "longitudinal_runs",
    "longitudinal_results",
    "longitudinal_members",
    "reports",
    "jobs",
    "job_dependencies",
)


def baseline_tables() -> list[Table]:
    """Return only tables owned by the immutable 0001 migration baseline."""
    return [Base.metadata.tables[name] for name in BASELINE_TABLE_NAMES]
