"""Canonical closed Zero Hour order-export capability for telemetry v2."""

from typing import Final

SUPPORTED_ORDER_COVERAGE: Final[tuple[tuple[int, str, str, int | None], ...]] = (
    (1056, "MSG_COMBATDROP_AT_LOCATION", "location", 0),
    (1057, "MSG_COMBATDROP_AT_OBJECT", "object", 0),
    (1059, "MSG_DO_ATTACK_OBJECT", "object", 0),
    (1060, "MSG_DO_FORCE_ATTACK_OBJECT", "object", 0),
    (1061, "MSG_DO_FORCE_ATTACK_GROUND", "location", 0),
    (1062, "MSG_GET_REPAIRED", "object", 0),
    (1063, "MSG_GET_HEALED", "object", 0),
    (1064, "MSG_DO_REPAIR", "object", 0),
    (1065, "MSG_RESUME_CONSTRUCTION", "object", 0),
    (1066, "MSG_ENTER", "object", 1),
    (1067, "MSG_DOCK", "object", 0),
    (1068, "MSG_DO_MOVETO", "location", 0),
    (1069, "MSG_DO_ATTACKMOVETO", "location", 0),
    (1070, "MSG_DO_FORCEMOVETO", "location", 0),
    (1071, "MSG_ADD_WAYPOINT", "location", 0),
    (1072, "MSG_DO_GUARD_POSITION", "location", 0),
    (1073, "MSG_DO_GUARD_OBJECT", "object", 0),
    (1074, "MSG_DO_STOP", "none", None),
    (1075, "MSG_DO_SCATTER", "none", None),
    (1087, "MSG_DO_SALVAGE", "location", 0),
    (1094, "MSG_CREATE_FORMATION", "none", None),
)


def canonical_order_coverage() -> dict[str, object]:
    """Return a fresh exact manifest capability so callers cannot mutate the canonical tuple."""
    return {
        "coverage": "closed_supported_subset",
        "dispatch_seam": "GameLogic::logicMessageDispatcher_post_resolution",
        "command_frame_source": "GameLogic::getFrame",
        "source_player_policy": "message_player_resolved_to_engine_player",
        "selected_reference_policy": "current_live_post_dispatch_source_order",
        "target_reference_policy": "current_live_post_dispatch",
        "historical_provenance_policy": "order_facts_remain_historical_after_entity_destruction",
        "sample_order_reference_policy": "last_supported_post_dispatch_order_not_execution_state",
        "supported_commands": [
            {
                "message_type": message_type,
                "message_name": message_name,
                "target_kind": target_kind,
                "target_argument_index": target_argument_index,
            }
            for message_type, message_name, target_kind, target_argument_index in SUPPORTED_ORDER_COVERAGE
        ],
    }
