# Copyright 2026 TheSuperHackers
#
# Spatial & Coordinate intelligence engine for Generals & Zero Hour replays.

import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from .parser import ParsedReplay, GameCommand
from .constants import GameMessageType

@dataclass
class MapCoordinate:
    x: float
    y: float
    z: float = 0.0

@dataclass
class CombatHotspot:
    center_x: float
    center_y: float
    radius: float
    intensity: int
    first_time_sec: float
    last_time_sec: float
    involved_players: List[str]
    description: str

@dataclass
class PlayerSpatialProfile:
    player_id: int
    player_name: str
    base_center: Optional[MapCoordinate]
    forward_proxy_structures: List[Dict[str, Any]] = field(default_factory=list)
    action_coordinates: List[MapCoordinate] = field(default_factory=list)
    combat_coordinates: List[MapCoordinate] = field(default_factory=list)
    movement_coordinates: List[MapCoordinate] = field(default_factory=list)
    territory_bounds: Dict[str, float] = field(default_factory=dict) # min_x, max_x, min_y, max_y

@dataclass
class SpatialAnalysis:
    map_bounds: Dict[str, float] # min_x, max_x, min_y, max_y, width, height
    player_profiles: Dict[int, PlayerSpatialProfile] = field(default_factory=dict)
    player_bases: Dict[str, Dict[str, float]] = field(default_factory=dict)
    hotspots: List[CombatHotspot] = field(default_factory=list)
    proxy_events: List[Dict[str, Any]] = field(default_factory=list)


class SpatialAnalyzer:
    """Analyzes 2D spatial coordinates for base expansion, proxy buildings, and combat hotspots."""

    def __init__(self, replay: ParsedReplay):
        self.replay = replay
        self.meta = replay.metadata

    def _dist(self, p1: MapCoordinate, p2: MapCoordinate) -> float:
        return math.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)

    def analyze(self) -> SpatialAnalysis:
        # 1. Collect all coordinates per player
        all_coords: List[MapCoordinate] = []
        player_coords: Dict[int, List[MapCoordinate]] = {}
        player_builds: Dict[int, List[Tuple[float, MapCoordinate, int]]] = {}
        player_attacks: Dict[int, List[Tuple[float, MapCoordinate]]] = {}
        player_moves: Dict[int, List[Tuple[float, MapCoordinate]]] = {}

        for cmd in self.replay.commands:
            p_idx = cmd.player_index
            if p_idx not in player_coords:
                player_coords[p_idx] = []
                player_builds[p_idx] = []
                player_attacks[p_idx] = []
                player_moves[p_idx] = []

            # Extract coordinates from args
            for arg in cmd.args:
                if arg.arg_type.name == "LOCATION" and isinstance(arg.value, dict):
                    coord = MapCoordinate(x=arg.value.get("x", 0.0), y=arg.value.get("y", 0.0), z=arg.value.get("z", 0.0))
                    all_coords.append(coord)
                    player_coords[p_idx].append(coord)

                    if cmd.command_type in (GameMessageType.MSG_DOZER_CONSTRUCT, GameMessageType.MSG_DOZER_CONSTRUCT_LINE):
                        tid = cmd.args[0].value if len(cmd.args) > 0 and isinstance(cmd.args[0].value, int) else 0
                        player_builds[p_idx].append((cmd.timestamp_sec, coord, tid))
                    elif cmd.command_type in (GameMessageType.MSG_DO_ATTACK_OBJECT, GameMessageType.MSG_DO_ATTACKMOVETO, GameMessageType.MSG_DO_FORCE_ATTACK_GROUND, GameMessageType.MSG_DO_FORCE_ATTACK_OBJECT):
                        player_attacks[p_idx].append((cmd.timestamp_sec, coord))
                    elif cmd.command_type in (GameMessageType.MSG_DO_MOVETO, GameMessageType.MSG_DO_FORCEMOVETO, GameMessageType.MSG_ADD_WAYPOINT):
                        player_moves[p_idx].append((cmd.timestamp_sec, coord))

        # Calculate map bounding box
        if all_coords:
            xs = [c.x for c in all_coords]
            ys = [c.y for c in all_coords]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
        else:
            min_x, max_x, min_y, max_y = 0.0, 4000.0, 0.0, 4000.0

        map_bounds = {
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
            "width": max(max_x - min_x, 100.0),
            "height": max(max_y - min_y, 100.0)
        }

        # Build player spatial profiles
        player_profiles: Dict[int, PlayerSpatialProfile] = {}
        proxy_events: List[Dict[str, Any]] = []

        for p_idx, coords in player_coords.items():
            slot_idx = p_idx - 2
            p_name = f"Player_{p_idx}"
            if 0 <= slot_idx < len(self.meta.players):
                p_name = self.meta.players[slot_idx].name
            elif p_idx < len(self.meta.players):
                p_name = self.meta.players[p_idx].name

            # Determine Main Base Center from initial 3 buildings
            builds = player_builds.get(p_idx, [])
            base_center = None
            if builds:
                early_builds = builds[:min(3, len(builds))]
                avg_bx = sum(b[1].x for b in early_builds) / len(early_builds)
                avg_by = sum(b[1].y for b in early_builds) / len(early_builds)
                base_center = MapCoordinate(x=round(avg_bx, 2), y=round(avg_by, 2))

            # Detect Forward Proxy Buildings (> 1200 distance from base center in first 4 minutes)
            proxies = []
            if base_center:
                for t_sec, b_coord, tid in builds:
                    if t_sec <= 240.0:
                        dist = self._dist(base_center, b_coord)
                        if dist > 1200.0:
                            proxy_info = {
                                "player": p_name,
                                "time_sec": t_sec,
                                "template_id": tid,
                                "coord": {"x": b_coord.x, "y": b_coord.y},
                                "distance_from_base": round(dist, 1)
                            }
                            proxies.append(proxy_info)
                            proxy_events.append(proxy_info)

            # Territory bounds
            p_xs = [c.x for c in coords] if coords else [0.0]
            p_ys = [c.y for c in coords] if coords else [0.0]

            profile = PlayerSpatialProfile(
                player_id=p_idx,
                player_name=p_name,
                base_center=base_center,
                forward_proxy_structures=proxies,
                action_coordinates=coords,
                combat_coordinates=[a[1] for a in player_attacks.get(p_idx, [])],
                movement_coordinates=[m[1] for m in player_moves.get(p_idx, [])],
                territory_bounds={
                    "min_x": min(p_xs),
                    "max_x": max(p_xs),
                    "min_y": min(p_ys),
                    "max_y": max(p_ys)
                }
            )
            player_profiles[p_idx] = profile

        # Collect player base centers
        player_bases = {}
        for prof in player_profiles.values():
            if prof.base_center:
                player_bases[prof.player_name] = {"x": prof.base_center.x, "y": prof.base_center.y}

        # Cluster combat coordinates to find battle hotspots
        hotspots = self._cluster_combat_hotspots(player_attacks, player_profiles, map_bounds)

        return SpatialAnalysis(
            map_bounds=map_bounds,
            player_profiles=player_profiles,
            player_bases=player_bases,
            hotspots=hotspots,
            proxy_events=proxy_events
        )


    def _cluster_combat_hotspots(
        self,
        player_attacks: Dict[int, List[Tuple[float, MapCoordinate]]],
        profiles: Dict[int, PlayerSpatialProfile],
        bounds: Dict[str, float]
    ) -> List[CombatHotspot]:
        all_attacks: List[Tuple[float, MapCoordinate, str]] = []
        for p_idx, atks in player_attacks.items():
            p_name = profiles[p_idx].player_name if p_idx in profiles else f"Player_{p_idx}"
            for t_sec, coord in atks:
                all_attacks.append((t_sec, coord, p_name))

        if not all_attacks:
            return []

        # Simple grid clustering (bin size ~ 500 units)
        grid_size = 500.0
        grid: Dict[Tuple[int, int], List[Tuple[float, MapCoordinate, str]]] = {}

        for t_sec, coord, pname in all_attacks:
            gx = int(coord.x // grid_size)
            gy = int(coord.y // grid_size)
            key = (gx, gy)
            if key not in grid:
                grid[key] = []
            grid[key].append((t_sec, coord, pname))

        hotspots: List[CombatHotspot] = []
        for (gx, gy), items in grid.items():
            if len(items) >= 5: # At least 5 attack actions in this sector
                avg_x = sum(it[1].x for it in items) / len(items)
                avg_y = sum(it[1].y for it in items) / len(items)
                times = [it[0] for it in items]
                involved = list(set(it[2] for it in items))
                
                # Context description
                mid_x = (bounds["min_x"] + bounds["max_x"]) / 2.0
                mid_y = (bounds["min_y"] + bounds["max_y"]) / 2.0
                dist_to_center = math.sqrt((avg_x - mid_x)**2 + (avg_y - mid_y)**2)
                
                if dist_to_center < 800.0:
                    desc = "Contested Map Center & Supply Area"
                elif avg_y > mid_y + 600:
                    desc = "Northern Flank / Base Approach"
                elif avg_y < mid_y - 600:
                    desc = "Southern Flank / Base Approach"
                else:
                    desc = "Key Chokepoint & Flank Corridor"

                hotspots.append(CombatHotspot(
                    center_x=round(avg_x, 1),
                    center_y=round(avg_y, 1),
                    radius=round(grid_size / 2, 1),
                    intensity=len(items),
                    first_time_sec=min(times),
                    last_time_sec=max(times),
                    involved_players=involved,
                    description=desc
                ))

        hotspots.sort(key=lambda h: h.intensity, reverse=True)
        return hotspots[:6]
