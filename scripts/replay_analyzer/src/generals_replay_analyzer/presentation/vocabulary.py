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
        "AmericaBuildingFirebase": "Firebase",
        "AmericaStrategyCenter": "Strategy Center",
        "AmericaVehicleHumvee": "Humvee",
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

_FEATURE_LABELS = MappingProxyType(
    {
        "build.completed_count": "Structures completed",
        "build.completed_sequence": "Build order",
        "combat.applied_damage_taken": "Damage taken",
        "economy.supply_collected_total": "Supply collected",
        "economy.supply_collection_rate": "Supply income",
        "production.completed_composition": "Army composition",
        "production.completed_count": "Units completed",
        "state.final_result": "Match result",
    }
)

_REASON_LABELS = MappingProxyType(
    {
        "ambiguous_feature_value": "The replay contains conflicting evidence",
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


def feature_label(feature_name: str) -> str:
    """Return a player-facing metric name while retaining honest unknown handling."""

    known = _FEATURE_LABELS.get(feature_name)
    return known if known is not None else f"{_readable_identity(feature_name)} (unrecognized)"


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
