"""Closed telemetry-v2 contracts for engine-native replay observations."""

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema import ValidationError as JsonSchemaValidationError  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from generals_replay_analyzer.telemetry.model import EVENT_TYPES, TelemetryRecord
from generals_replay_analyzer.telemetry.reader import (
    TelemetryTraceValidationError,
    _validate_v2_engine_native_trace,
)

RUN_ID = "823e4567-e89b-12d3-a456-426614174099"
ADAPTER: TypeAdapter[TelemetryRecord] = TypeAdapter(TelemetryRecord)
SCHEMA = json.loads(
    (Path(__file__).parents[2] / "contracts" / "telemetry-v2.schema.json").read_text(
        encoding="utf-8"
    )
)
SCHEMA_VALIDATOR = Draft202012Validator(SCHEMA)


def _record(event_type: str, payload: Mapping[str, object], *, frame: int = 30) -> dict[str, object]:
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "sequence": 7,
        "frame": frame,
        "logic_time_seconds": frame / 30.0,
        "event_type": event_type,
        "payload": dict(payload),
    }


def _scorekeeper() -> dict[str, object]:
    return {
        "source": "Player::getScoreKeeper",
        "player_scope": "resolved_occupied_replay_slots",
        "scoring_enabled": True,
        "players": [
            {
                "player_index": 0,
                "money_earned": 1200,
                "money_spent": 900,
                "units_built": 2,
                "units_lost": 1,
                "units_destroyed": 3,
                "buildings_built": 2,
                "buildings_lost": 0,
                "buildings_destroyed": 1,
                "tech_buildings_captured": 0,
                "faction_buildings_captured": 0,
            }
        ],
    }


def _cash_per_minute() -> dict[str, object]:
    return {
        "source": "Money::getCashPerMinute",
        "sample_interval_frames": 30,
        "income_window_buckets": 60,
        "bucket_width_frames": 30,
        "players": [{"player_index": 0, "has_money": True, "cash_per_minute": 1200}],
    }


def _visibility() -> dict[str, object]:
    return {
        "player_index": 0,
        "object_id": 248,
        "template_name": "AmericaCommandCenter",
        "previous_status": "unseen",
        "status": "clear",
        "first_observed_clear": True,
        "position": {"x": 1.0, "y": 2.0, "z": 0.0},
        "observation_basis": "object_center_partition_cell",
        "source": "PartitionManager::getShroudStatusForPlayer",
        "sample_interval_frames": 15,
        "sampling_cycle_id": 0,
    }


def _visibility_summary() -> dict[str, object]:
    return {
        "source": "PartitionManager::getShroudStatusForPlayer",
        "sample_interval_frames": 15,
        "maximum_pairs_per_pass": 8192,
        "eligible_pair_count": 9000,
        "sampled_pair_count": 8192,
        "cursor_start": 0,
        "cursor_end": 8192,
        "sampling_cycle_id": 0,
        "cycle_complete": False,
    }


def _partition_grid() -> dict[str, object]:
    x_positions = [sample * 63 // 15 for sample in range(16)]
    y_positions = [sample * 63 // 7 for sample in range(8)]
    return {
        "player_index": 0,
        "sample_interval_frames": 300,
        "sampling_scheme": "uniform_partition_lattice_v1",
        "heuristic_semantics": "engine_ai_owner_contribution_heuristic",
        "grid": {
            "cell_count_x": 64,
            "cell_count_y": 64,
            "total_cell_count": 4096,
            "sampled_cell_count": 128,
            "maximum_sampled_cells": 128,
            "complete": False,
        },
        "providers": {
            "shroud": "PartitionCell::getShroudStatusForPlayer",
            "threat": "PartitionCell::getThreatValue",
            "cash": "PartitionCell::getCashValue",
        },
        "cells": [
            {
                "cell_x": cell_x,
                "cell_y": cell_y,
                "world_position": {
                    "x": float(cell_x),
                    "y": float(cell_y),
                    "z": 0.0,
                },
                "shroud_status": "clear",
                "threat_value": 0,
                "cash_value": 0,
            }
            for cell_y in y_positions
            for cell_x in x_positions
        ],
    }


@pytest.mark.parametrize(
    ("event_type", "payload", "payload_type"),
    (
        ("scorekeeper_snapshot", _scorekeeper(), "ScoreKeeperSnapshotPayload"),
        ("cash_per_minute_snapshot", _cash_per_minute(), "CashPerMinuteSnapshotPayload"),
        ("object_visibility_changed", _visibility(), "ObjectVisibilityChangedPayload"),
        ("visibility_sampling_summary", _visibility_summary(), "VisibilitySamplingSummaryPayload"),
        ("partition_engine_grid_sample", _partition_grid(), "PartitionEngineGridSamplePayload"),
    ),
)
def test_engine_native_observation_payloads_are_closed_and_typed(
    event_type: str,
    payload: dict[str, object],
    payload_type: str,
) -> None:
    raw_record = _record(event_type, payload)
    SCHEMA_VALIDATOR.validate(raw_record)
    record = ADAPTER.validate_python(raw_record)

    assert record.event_type == event_type
    assert type(record.payload).__name__ == payload_type
    assert event_type in EVENT_TYPES

    damaged = deepcopy(payload)
    damaged["invented"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ADAPTER.validate_python(_record(event_type, damaged))


def test_scorekeeper_players_are_strictly_ordered_and_unique() -> None:
    payload = _scorekeeper()
    player = deepcopy(payload["players"][0])
    player["player_index"] = 2
    payload["players"] = [player, deepcopy(payload["players"][0])]

    with pytest.raises(ValidationError, match="strictly ordered and unique"):
        ADAPTER.validate_python(_record("scorekeeper_snapshot", payload))


@pytest.mark.parametrize(
    ("has_money", "cash_per_minute"),
    ((False, 1200), (True, None)),
)
def test_cash_per_minute_presence_matches_money_availability(
    has_money: bool,
    cash_per_minute: int | None,
) -> None:
    payload = _cash_per_minute()
    payload["players"] = [
        {"player_index": 0, "has_money": has_money, "cash_per_minute": cash_per_minute}
    ]

    with pytest.raises(ValidationError, match="cash_per_minute must be present exactly"):
        ADAPTER.validate_python(_record("cash_per_minute_snapshot", payload))


@pytest.mark.parametrize(
    ("previous_status", "status", "first_observed_clear"),
    (("clear", "fogged", True), ("unseen", "clear", False)),
)
def test_visibility_first_clear_flag_describes_the_transition(
    previous_status: str,
    status: str,
    first_observed_clear: bool,
) -> None:
    payload = _visibility()
    payload.update(
        previous_status=previous_status,
        status=status,
        first_observed_clear=first_observed_clear,
    )

    with pytest.raises(ValidationError, match="first_observed_clear"):
        ADAPTER.validate_python(_record("object_visibility_changed", payload))
    with pytest.raises(JsonSchemaValidationError):
        SCHEMA_VALIDATOR.validate(_record("object_visibility_changed", payload))


def test_visibility_sampling_summary_cannot_claim_more_than_its_cap() -> None:
    payload = _visibility_summary()
    payload["sampled_pair_count"] = 8193

    with pytest.raises(ValidationError, match="sampled pair count"):
        ADAPTER.validate_python(_record("visibility_sampling_summary", payload))

    underfilled = _visibility_summary()
    underfilled.update(sampled_pair_count=1, cursor_end=1)
    with pytest.raises(ValidationError, match="exhaust the pass cap"):
        ADAPTER.validate_python(_record("visibility_sampling_summary", underfilled))


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_cell", "count_mismatch", "cell_outside_grid", "non_row_major_cells"],
)
def test_partition_grid_counts_cells_exactly_once_inside_its_bounds(mutation: str) -> None:
    payload = _partition_grid()
    if mutation == "duplicate_cell":
        payload["cells"][1] = deepcopy(payload["cells"][0])
    elif mutation == "count_mismatch":
        payload["grid"]["sampled_cell_count"] = 127
    elif mutation == "cell_outside_grid":
        payload["cells"][0]["cell_x"] = 64
    else:
        payload["cells"][0], payload["cells"][1] = payload["cells"][1], payload["cells"][0]

    with pytest.raises(ValidationError, match="grid cells"):
        ADAPTER.validate_python(_record("partition_engine_grid_sample", payload))


def test_partition_grid_is_a_two_dimensional_edge_inclusive_lattice() -> None:
    payload = _partition_grid()
    cells = payload["cells"]
    coordinates = {(cell["cell_x"], cell["cell_y"]) for cell in cells}

    assert len({cell["cell_x"] for cell in cells}) == 16
    assert len({cell["cell_y"] for cell in cells}) == 8
    assert {(0, 0), (63, 0), (0, 63), (63, 63)} <= coordinates


def test_cash_changed_bucket_provenance_is_optional_for_old_v2_traces_but_joint_when_present() -> None:
    historical = {
        "player_index": 0,
        "before": 0,
        "delta": 300,
        "after": 300,
        "track_income": True,
        "reason": "supply_income",
    }
    old_record = ADAPTER.validate_python(_record("cash_changed", historical))
    assert old_record.payload.tracked_income_amount is None
    assert old_record.payload.income_bucket_index is None

    current = {**historical, "tracked_income_amount": 300, "income_bucket_index": 17}
    current_record = ADAPTER.validate_python(_record("cash_changed", current))
    assert current_record.payload.tracked_income_amount == 300
    assert current_record.payload.income_bucket_index == 17

    for missing in ("tracked_income_amount", "income_bucket_index"):
        damaged = dict(current)
        damaged.pop(missing)
        with pytest.raises(ValidationError, match="bucket provenance fields must be jointly present"):
            ADAPTER.validate_python(_record("cash_changed", damaged))

    untracked = {**current, "track_income": False, "reason": "unit_refund"}
    with pytest.raises(ValidationError, match="requires track_income"):
        ADAPTER.validate_python(_record("cash_changed", untracked))

    inconsistent = {**current, "tracked_income_amount": 299}
    with pytest.raises(ValidationError, match="unsigned cash mutation"):
        ADAPTER.validate_python(_record("cash_changed", inconsistent))


def _validated(
    event_type: str,
    payload: Mapping[str, object],
    *,
    frame: int,
    sequence: int,
) -> TelemetryRecord:
    raw = _record(event_type, payload, frame=frame)
    raw["sequence"] = sequence
    return ADAPTER.validate_python(raw)


def _outcome(*, frame: int, sequence: int) -> TelemetryRecord:
    return _validated(
        "match_outcome",
        {
            "status": "decided",
            "source": "victory_conditions",
            "winner_player_indices": [0],
            "loser_player_indices": [],
            "engine_player_indices": [0],
            "terminal_reason": "clean_completion",
            "quit_early": False,
            "replay_header_desync": False,
            "replay_header_disconnected_slots": [],
            "crc_mismatch": False,
            "crc_mismatch_frame": None,
            "clean_shutdown": True,
        },
        frame=frame,
        sequence=sequence,
    )


def _creation(*, frame: int = 0, sequence: int = 1) -> TelemetryRecord:
    return _validated(
        "object_created",
        {
            "object_id": 248,
            "template_name": "AmericaCommandCenter",
            "owner_player_index": 0,
            "team_id": 1,
            "position_status": "placed",
            "position": {"x": 1.0, "y": 2.0, "z": 0.0},
            "orientation": 0.0,
            "kind_of_flags": ["STRUCTURE"],
            "initial_status": [],
            "creation_source": "map_loaded",
            "initialization_snapshot_status": "present",
            "creation_context": {
                "registration_frame": 0,
                "producer_object_id": None,
                "producer_player_index": None,
            },
        },
        frame=frame,
        sequence=sequence,
    )


def _validate_native(records: list[TelemetryRecord], *, final_frame: int) -> None:
    _validate_v2_engine_native_trace(
        Path("engine-native.ndjson"),
        tuple(records),
        final_frame,
        frozenset({0}),
        frozenset({0}),
    )


def test_terminal_scorekeeper_is_unique_complete_and_immediately_precedes_outcome() -> None:
    score = _validated("scorekeeper_snapshot", _scorekeeper(), frame=100, sequence=1)
    outcome = _outcome(frame=100, sequence=2)
    _validate_native([score, outcome], final_frame=100)

    with pytest.raises(TelemetryTraceValidationError, match="exactly one scorekeeper_snapshot"):
        _validate_native([score, score, outcome], final_frame=100)
    with pytest.raises(TelemetryTraceValidationError, match="immediately precede match_outcome"):
        _validate_native(
            [
                score,
                _validated("visibility_sampling_summary", _visibility_summary(), frame=90, sequence=3),
                outcome,
            ],
            final_frame=100,
        )


def test_cash_per_minute_family_requires_exact_cadence_final_domain_and_unsigned_bucket_fold() -> None:
    cash = {
        "player_index": 0,
        "before": 0,
        "delta": -1,
        "after": 4_294_967_295,
        "track_income": True,
        "reason": "supply_income",
        "tracked_income_amount": 4_294_967_295,
        "income_bucket_index": 0,
    }
    wrapped = {**cash, "before": 4_294_967_295, "delta": 2, "after": 1, "tracked_income_amount": 2}
    sample = _cash_per_minute()
    sample["players"] = [{"player_index": 0, "has_money": True, "cash_per_minute": 1}]
    records = [
        _validated("cash_changed", cash, frame=29, sequence=1),
        _validated("cash_changed", wrapped, frame=29, sequence=2),
        _validated("cash_per_minute_snapshot", sample, frame=30, sequence=3),
    ]
    _validate_native(records, final_frame=30)

    zero_sample = _cash_per_minute()
    zero_sample["players"] = [{"player_index": 0, "has_money": True, "cash_per_minute": 0}]
    with pytest.raises(TelemetryTraceValidationError, match="cadence"):
        _validate_native(
            [_validated("cash_per_minute_snapshot", zero_sample, frame=30, sequence=4)],
            final_frame=60,
        )
    damaged = _cash_per_minute()
    damaged["players"] = []
    with pytest.raises(ValidationError):
        _validated("cash_per_minute_snapshot", damaged, frame=30, sequence=4)


def test_cash_per_minute_accepts_deposits_before_and_after_the_boundary_rotation() -> None:
    before_rotation = {
        "player_index": 0,
        "before": 0,
        "delta": 100,
        "after": 100,
        "track_income": True,
        "reason": "supply_income",
        "tracked_income_amount": 100,
        "income_bucket_index": 0,
    }
    after_rotation = {
        **before_rotation,
        "before": 100,
        "delta": 50,
        "after": 150,
        "tracked_income_amount": 50,
        "income_bucket_index": 1,
    }
    sample = _cash_per_minute()
    sample["players"] = [{"player_index": 0, "has_money": True, "cash_per_minute": 150}]
    records = [
        _validated("cash_changed", before_rotation, frame=30, sequence=1),
        _validated("cash_changed", after_rotation, frame=30, sequence=2),
        _validated("cash_per_minute_snapshot", sample, frame=30, sequence=3),
    ]

    _validate_native(records, final_frame=30)

    with pytest.raises(TelemetryTraceValidationError, match="before or after frame rotation"):
        _validate_native([*records[:2], records[0], records[2]], final_frame=30)


def test_engine_native_player_scope_uses_resolved_slots_not_the_full_engine_domain() -> None:
    payload = _scorekeeper()
    second = deepcopy(payload["players"][0])
    second["player_index"] = 1
    payload["players"] = [payload["players"][0], second]
    score = _validated("scorekeeper_snapshot", payload, frame=100, sequence=1)
    outcome = _outcome(frame=100, sequence=2)

    _validate_v2_engine_native_trace(
        Path("engine-native.ndjson"),
        (score, outcome),
        100,
        frozenset({0, 1, 12, 13}),
        frozenset({0, 1}),
    )


def test_v2_only_events_and_income_provenance_are_rejected_by_direct_v1_models() -> None:
    score = _record("scorekeeper_snapshot", _scorekeeper())
    score["schema_version"] = 1
    with pytest.raises(ValidationError, match="require schema_version 2"):
        ADAPTER.validate_python(score, context={"schema_version": 1})

    cash = {
        "player_index": 0,
        "before": 0,
        "delta": 100,
        "after": 100,
        "track_income": True,
        "reason": "supply_income",
        "tracked_income_amount": 100,
        "income_bucket_index": 0,
    }
    historical = _record("cash_changed", cash)
    historical["schema_version"] = 1
    with pytest.raises(ValidationError, match="provenance requires schema_version 2"):
        ADAPTER.validate_python(historical, context={"schema_version": 1})


def test_visibility_requires_live_object_exact_prior_state_and_one_first_clear() -> None:
    creation = _creation()
    first = _validated("object_visibility_changed", _visibility(), frame=15, sequence=2)
    fogged = _visibility()
    fogged.update(previous_status="clear", status="fogged", first_observed_clear=False)
    second = _validated("object_visibility_changed", fogged, frame=30, sequence=3)
    first_summary = _validated("visibility_sampling_summary", _visibility_summary(), frame=15, sequence=4)
    second_summary_payload = _visibility_summary()
    second_summary_payload.update(
        sampled_pair_count=808,
        cursor_start=8192,
        cursor_end=9000,
        cycle_complete=True,
    )
    second_summary = _validated(
        "visibility_sampling_summary", second_summary_payload, frame=30, sequence=5
    )
    _validate_native(
        [creation, first, first_summary, second, second_summary],
        final_frame=30,
    )

    stale = _visibility()
    stale.update(previous_status="unseen", status="fogged", first_observed_clear=False)
    with pytest.raises(TelemetryTraceValidationError, match="previous visibility status"):
        _validate_native(
            [
                creation,
                first,
                first_summary,
                _validated("object_visibility_changed", stale, frame=30, sequence=6),
                second_summary,
            ],
            final_frame=30,
        )
    with pytest.raises(TelemetryTraceValidationError, match="live object"):
        _validate_native([first], final_frame=15)


def test_visibility_summaries_and_partition_samples_use_exact_sampling_domains() -> None:
    summary = _validated("visibility_sampling_summary", _visibility_summary(), frame=15, sequence=1)
    grid = _validated("partition_engine_grid_sample", _partition_grid(), frame=300, sequence=2)
    _validate_native([summary], final_frame=15)
    _validate_native([grid], final_frame=300)

    with pytest.raises(TelemetryTraceValidationError, match="visibility sampling cadence"):
        _validate_native(
            [_validated("visibility_sampling_summary", _visibility_summary(), frame=16, sequence=3)],
            final_frame=16,
        )
    with pytest.raises(TelemetryTraceValidationError, match="one summary for every 15-frame pass"):
        _validate_native([summary], final_frame=30)

    inconsistent_summary = _visibility_summary()
    inconsistent_summary["cursor_end"] = 8000
    with pytest.raises(TelemetryTraceValidationError, match="cursor span"):
        _validate_native(
            [
                _validated(
                    "visibility_sampling_summary",
                    inconsistent_summary,
                    frame=15,
                    sequence=4,
                )
            ],
            final_frame=15,
        )
    with pytest.raises(TelemetryTraceValidationError, match="partition sample player domain"):
        _validate_native([grid], final_frame=600)


def test_partition_world_geometry_must_match_across_players_in_one_frame() -> None:
    first_payload = _partition_grid()
    second_payload = deepcopy(first_payload)
    second_payload["player_index"] = 1
    second_payload["cells"][0]["world_position"]["x"] = 999.0
    records = (
        _validated("partition_engine_grid_sample", first_payload, frame=300, sequence=1),
        _validated("partition_engine_grid_sample", second_payload, frame=300, sequence=2),
    )

    with pytest.raises(TelemetryTraceValidationError, match="world geometry"):
        _validate_v2_engine_native_trace(
            Path("engine-native.ndjson"),
            records,
            300,
            frozenset({0, 1}),
            frozenset({0, 1}),
        )
