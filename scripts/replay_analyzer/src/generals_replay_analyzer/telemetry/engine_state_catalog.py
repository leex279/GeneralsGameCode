"""Stable engine enum identities and direct Task 7 classification provenance."""

from typing import Final

AI_STATE_NAMES: Final[dict[int, str]] = dict(
    enumerate(
        (
            "AI_IDLE",
            "AI_MOVE_TO",
            "AI_FOLLOW_WAYPOINT_PATH_AS_TEAM",
            "AI_FOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS",
            "AI_FOLLOW_WAYPOINT_PATH_AS_TEAM_EXACT",
            "AI_FOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS_EXACT",
            "AI_FOLLOW_PATH",
            "AI_FOLLOW_EXITPRODUCTION_PATH",
            "AI_WAIT",
            "AI_ATTACK_POSITION",
            "AI_ATTACK_OBJECT",
            "AI_FORCE_ATTACK_OBJECT",
            "AI_ATTACK_AND_FOLLOW_OBJECT",
            "AI_DEAD",
            "AI_DOCK",
            "AI_ENTER",
            "AI_GUARD",
            "AI_HUNT",
            "AI_WANDER",
            "AI_PANIC",
            "AI_ATTACK_SQUAD",
            "AI_GUARD_TUNNEL_NETWORK",
            "AI_GET_REPAIRED",
            "AI_MOVE_OUT_OF_THE_WAY",
            "AI_MOVE_AND_TIGHTEN",
            "AI_MOVE_AND_EVACUATE",
            "AI_MOVE_AND_EVACUATE_AND_EXIT",
            "AI_MOVE_AND_DELETE",
            "AI_ATTACK_AREA",
            "AI_HACK_INTERNET",
            "AI_ATTACK_MOVE_TO",
            "AI_ATTACKFOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS",
            "AI_ATTACKFOLLOW_WAYPOINT_PATH_AS_TEAM",
            "AI_FACE_OBJECT",
            "AI_FACE_POSITION",
            "AI_RAPPEL_INTO",
            "AI_COMBATDROP",
            "AI_EXIT",
            "AI_PICK_UP_CRATE",
            "AI_MOVE_AWAY_FROM_REPULSORS",
            "AI_WANDER_IN_PLACE",
            "AI_BUSY",
            "AI_EXIT_INSTANTLY",
            "AI_GUARD_RETALIATE",
        )
    )
)

LOCOMOTOR_SET_NAMES: Final[dict[int, str]] = {
    -1: "LOCOMOTORSET_INVALID",
    0: "LOCOMOTORSET_NORMAL",
    1: "LOCOMOTORSET_NORMAL_UPGRADED",
    2: "LOCOMOTORSET_FREEFALL",
    3: "LOCOMOTORSET_WANDER",
    4: "LOCOMOTORSET_PANIC",
    5: "LOCOMOTORSET_TAXIING",
    6: "LOCOMOTORSET_SUPERSONIC",
    7: "LOCOMOTORSET_SLUGGISH",
}

LAYER_NAMES: Final[dict[int, str]] = {
    0: "LAYER_INVALID",
    1: "LAYER_GROUND",
    15: "LAYER_WALL",
}

STATE_SOURCES: Final[dict[str, frozenset[str]]] = {
    "disabled": frozenset({"object_disabled"}),
    "garrisoned": frozenset({"enclosing_garrison_container"}),
    "attacking": frozenset({"ai_attack_state"}),
    "guarding": frozenset({"ai_guard_state"}),
    "moving": frozenset({"ai_moving_state"}),
    "idle": frozenset({"ai_idle_state"}),
    "unknown": frozenset({"ai_state_unclassified", "ai_interface_unavailable"}),
}
