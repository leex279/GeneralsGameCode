"""Stable player-facing vocabulary for engine identities and analysis codes."""

from __future__ import annotations

import re
from types import MappingProxyType

_GAME_LABELS = MappingProxyType(
    {
        "FactionAmerica": "USA",
        "FactionAmericaAirForceGeneral": "USA Air Force General",
        "FactionAmericaLaserGeneral": "USA Laser General",
        "FactionAmericaSuperWeaponGeneral": "USA Superweapon General",
        "FactionChina": "China",
        "FactionChinaInfantryGeneral": "China Infantry General",
        "FactionChinaNukeGeneral": "China Nuke General",
        "FactionChinaTankGeneral": "China Tank General",
        "FactionGLA": "GLA",
        "FactionGLADemolitionGeneral": "GLA Demolition General",
        "FactionGLAStealthGeneral": "GLA Stealth General",
        "FactionGLAToxinGeneral": "GLA Toxin General",
        "AmericaAirfield": "Airfield",
        "AmericaBarracks": "Barracks",
        "AmericaBuildingFirebase": "Firebase",
        "AmericaTankCrusader": "Crusader",
        "AmericaPowerPlant": "Power Plant",
        "AmericaStrategyCenter": "Strategy Center",
        "AmericaSupplyCenter": "Supply Center",
        "AmericaVehicleHumvee": "Humvee",
        "AmericaWarFactory": "War Factory",
        "AirF_AmericaVehicleCombatChinook": "Combat Chinook",
        "ChinaInfantryRedguard": "Red Guard",
        "ChinaPropagandaCenter": "Propaganda Center",
        "ChinaVehicleHelix": "Helix",
        "ChinaWarFactory": "War Factory",
        "GLAArmsDealer": "Arms Dealer",
        "GLAInfantryTerrorist": "Terrorist",
        "GLAPalace": "Palace",
        "GLATunnelNetwork": "Tunnel Network",
        "GLAVehicleTechnical": "Technical",
        "TechOilDerrick": "Oil Derrick",
    }
)

# TheSuperHackers @feature Leex 24/08/2026 Name observed science and special-power timing metrics without rewriting engine identities. (#TBD)
# TheSuperHackers @feature Leex 24/08/2026 Label evidence-backed engagement swing candidates without implying strategic causality. (#TBD)
_FEATURE_LABELS = MappingProxyType(
    {
        "activity.effective_actions_per_minute": "Observed effective APM",
        "activity.supported_order_action_count": "Supported orders",
        "build.completed_count": "Structures completed",
        "build.completed_sequence": "Build order",
        "combat.applied_damage_taken": "Damage taken",
        "combat.observed_kill_timing": "Observed kills",
        "combat.turning_point_timing": "Evidence-backed engagement swing candidates",
        "economy.supply_collected_total": "Supply collected",
        "economy.supply_collection_rate": "Supply income",
        "production.completed_composition": "Army composition",
        "production.completed_count": "Units completed",
        "production.science_purchase_timing": "Science purchase timing",
        "production.special_power_timing": "Special power timing",
        "scouting.first_observed_clear_timing": "First scouting clears",
        "scouting.visibility_transition_count": "Visibility transitions",
        "state.final_result": "Match result",
    }
)

_STRATEGY_LABELS = MappingProxyType(
    {
        "all_in_aggression": "Massing Humvees",
        "china_dual_war_factory_pressure": "Dual War Factory pressure",
        "china_fast_propaganda_center": "Propaganda Center technology",
        "china_helix_pressure": "Helix pressure",
        "china_infantry_pressure": "Red Guard infantry pressure",
        "defensive_opening": "Layered Firebase defense",
        "economic_expansion": "Second Supply Center expansion",
        "gla_dual_arms_dealer_pressure": "Dual Arms Dealer pressure",
        "gla_fast_palace": "Palace technology",
        "gla_forward_tunnel_pressure": "Forward Tunnel pressure",
        "gla_technical_aggression": "Technical aggression",
        "gla_terror_tech": "Terror Tech",
        "oil_capture": "Oil capture",
        "unknown_or_mixed": "No named strategy established",
        "usa_combat_chinook_pressure": "Combat Chinook pressure",
        "usa_defensive_firebase_expansion": "Firebase expansion",
        "usa_dual_airfield": "Dual Airfield",
        "usa_fast_strategy_center": "Strategy Center technology",
        "usa_humvee_pressure": "Humvee pressure",
    }
)

_PHASE_LABELS = MappingProxyType(
    {
        "opening": "Opening",
        "early": "Early game",
        "mid": "Mid game",
        "late": "Late game",
        "cross_phase": "Across observed phases",
    }
)

_REASON_LABELS = MappingProxyType(
    {
        "ambiguous_feature_value": "The replay contains conflicting evidence",
        "crc_mismatch": "Replay playback desynchronized at the reported frame",
        "incompatible_feature_value": "This evidence cannot support that conclusion",
        "insufficient_feature_quality": "The available evidence is incomplete",
        "map_identity_mismatch": "This strategy is not defined for the detected map",
        "minimum_sample_not_met": "More analyzed matches are needed",
        "missing_catalog_semantics": "Verified game-data meaning is unavailable",
        "missing_direct_observed_evidence": "Direct replay evidence is unavailable",
        "missing_feature_value": "The replay does not expose this measurement",
        "missing_observed_map_identity": "The map could not be verified",
    }
)

_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NUMERIC_SLOT_CODE = re.compile(r"[-+]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)\Z")


def _readable_identity(value: str) -> str:
    chunks = (chunk for chunk in re.split(r"[_\s./\\-]+", value.strip()) if chunk)
    words: list[str] = []
    for chunk in chunks:
        words.extend(part for part in _WORD_BOUNDARY.split(chunk) if part)
    return " ".join(words) or "Unknown"


# TheSuperHackers @feature Leex 23/08/2026 Translate verified engine identities without changing their evidence values. (#TBD)
def game_label(identity: str) -> str:
    """Return a known Zero Hour label or mark a safely split identity as unknown."""

    known = _GAME_LABELS.get(identity)
    return known if known is not None else f"{_readable_identity(identity)} (unrecognized)"


# TheSuperHackers @fix Leex 24/08/2026 Keep unresolved replay slot codes out of player-facing faction labels. (#TBD)
def faction_label(identity: str | None) -> str | None:
    """Present a resolved faction name, or no claim while the engine identity is unresolved."""

    if identity is None:
        return None
    normalized = identity.strip()
    if not normalized or _NUMERIC_SLOT_CODE.fullmatch(normalized) is not None:
        return None
    return game_label(normalized) if normalized.startswith("Faction") else normalized


def feature_label(feature_name: str) -> str:
    """Return a player-facing metric name while retaining honest unknown handling."""

    known = _FEATURE_LABELS.get(feature_name)
    return known if known is not None else f"{_readable_identity(feature_name)} (unrecognized)"


def strategy_label(strategy_id: str) -> str:
    """Return the taxonomy's stable player-facing strategy title."""

    known = _STRATEGY_LABELS.get(strategy_id)
    return known if known is not None else f"{_readable_identity(strategy_id)} (unrecognized)"


def phase_label(phase: str) -> str:
    """Return a consistent phase label without inferring an unseen phase."""

    known = _PHASE_LABELS.get(phase)
    return known if known is not None else f"{_readable_identity(phase)} (unrecognized)"


def reason_label(reason: str) -> str:
    """Translate a machine reason into an actionable evidence explanation."""

    known = _REASON_LABELS.get(reason)
    return known if known is not None else f"{_readable_identity(reason)} (unrecognized)"


def format_frame(frame: int, *, frames_per_second: int = 30) -> str:
    """Show player time and exact deterministic frame together."""

    if type(frame) is not int or frame < 0:
        raise ValueError("frame must be a non-negative integer")
    if type(frames_per_second) is not int or frames_per_second <= 0:
        raise ValueError("frames_per_second must be a positive integer")
    total_tenths = (frame * 10) // frames_per_second
    minutes, remainder = divmod(total_tenths, 600)
    seconds, tenths = divmod(remainder, 10)
    return f"{minutes}:{seconds:02d}.{tenths} (frame {frame})"
