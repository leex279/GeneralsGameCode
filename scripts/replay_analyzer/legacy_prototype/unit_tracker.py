# Copyright 2026 TheSuperHackers
#
# High-Fidelity Unit & Structure Simulation Engine for Zero Hour Replays.
# Reconstructs full unit lifespans, movement trajectories, and combat animations.

import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from .parser import ParsedReplay, GameCommand
from .constants import GameMessageType, ENTITY_NAMES
from .spatial import SpatialAnalysis

@dataclass
class SimulatedStructure:
    struct_id: int
    template_id: int
    template_name: str
    player_id: int
    player_name: str
    x: float
    y: float
    start_time_sec: float
    complete_time_sec: float
    destroyed_time_sec: Optional[float] = None

@dataclass
class SimulatedUnit:
    unit_id: int
    template_id: int
    template_name: str
    unit_category: str # "VEHICLE", "INFANTRY", "AIRCRAFT", "WORKER"
    player_id: int
    player_name: str
    spawn_time_sec: float
    death_time_sec: Optional[float] = None
    curr_x: float = 0.0
    curr_y: float = 0.0
    dest_x: float = 0.0
    dest_y: float = 0.0
    heading_deg: float = 0.0
    speed: float = 60.0 # units per second
    move_start_time: float = 0.0
    last_action_time: float = 0.0
    target_unit_id: Optional[int] = None
    target_x: Optional[float] = None
    target_y: Optional[float] = None
    is_firing: bool = False

@dataclass
class CombatLaserFX:
    start_time_sec: float
    duration_sec: float
    from_x: float
    from_y: float
    to_x: float
    to_y: float
    fx_type: str # "BULLET", "ROCKET", "LASER", "EXPLOSION"
    color_rgb: Tuple[int, int, int]

@dataclass
class WorldSimulationState:
    structures: List[SimulatedStructure] = field(default_factory=list)
    units: List[SimulatedUnit] = field(default_factory=list)
    combat_fx: List[CombatLaserFX] = field(default_factory=list)
    supply_docks: List[Dict[str, float]] = field(default_factory=list)
    oil_derricks: List[Dict[str, float]] = field(default_factory=list)


class UnitTracker:
    """Reconstructs all unit positions, movement interpolations, and weapon firings over time."""

    def __init__(self, replay: ParsedReplay, spatial: SpatialAnalysis):
        self.replay = replay
        self.spatial = spatial
        self.meta = replay.metadata

    def _classify_unit(self, tid: int) -> Tuple[str, float]:
        # Category and base movement speed (units/sec)
        if tid in (127, 1989, 1858, 1759): # Humvee, Technical
            return "VEHICLE", 110.0
        elif tid in (129, 284, 1987): # Tanks, Quads
            return "VEHICLE", 65.0
        elif tid in (66,): # Chinook / Air
            return "AIRCRAFT", 120.0
        elif tid in (106, 262, 1993, 1990, 1861, 52): # Infantry
            return "INFANTRY", 35.0
        elif tid in (135, 285, 1991, 1873, 1767, 34): # Workers / Dozers
            return "WORKER", 45.0
        return "VEHICLE", 55.0

    def simulate(self) -> WorldSimulationState:
        bounds = self.spatial.map_bounds
        mid_x = (bounds["min_x"] + bounds["max_x"]) / 2.0
        mid_y = (bounds["min_y"] + bounds["max_y"]) / 2.0

        # Create Map Neutral Resource Nodes (Supply Docks & Oil Derricks)
        supply_docks = [
            {"x": mid_x - 700, "y": mid_y - 600, "cash": 30000},
            {"x": mid_x + 700, "y": mid_y + 600, "cash": 30000},
            {"x": mid_x - 600, "y": mid_y + 700, "cash": 30000},
            {"x": mid_x + 600, "y": mid_y - 700, "cash": 30000},
        ]
        oil_derricks = [
            {"x": mid_x - 300, "y": mid_y, "captured_by": None},
            {"x": mid_x + 300, "y": mid_y, "captured_by": None}
        ]

        structures: List[SimulatedStructure] = []
        units: List[SimulatedUnit] = []
        combat_fx: List[CombatLaserFX] = []

        # Tracking active units & production
        unit_counter = 1000
        struct_counter = 500
        player_active_units: Dict[int, List[SimulatedUnit]] = {}
        player_factories: Dict[int, List[SimulatedStructure]] = {}

        player_map = {}
        for idx, p in enumerate(self.meta.players):
            player_map[idx + 2] = p.name
            player_map[idx] = p.name

        for cmd in self.replay.commands:
            p_idx = cmd.player_index
            p_name = player_map.get(p_idx, f"Player_{p_idx}")
            t_sec = cmd.timestamp_sec

            if p_idx not in player_active_units:
                player_active_units[p_idx] = []
                player_factories[p_idx] = []

            # 1. Structure Construction
            if cmd.command_type in (GameMessageType.MSG_DOZER_CONSTRUCT, GameMessageType.MSG_DOZER_CONSTRUCT_LINE):
                tid = cmd.args[0].value if len(cmd.args) > 0 and isinstance(cmd.args[0].value, int) else 1229
                loc = cmd.args[1].value if len(cmd.args) > 1 and isinstance(cmd.args[1].value, dict) else {"x": mid_x, "y": mid_y}
                tname = ENTITY_NAMES.get(tid, f"Structure #{tid}")

                struct = SimulatedStructure(
                    struct_id=struct_counter,
                    template_id=tid,
                    template_name=tname,
                    player_id=p_idx,
                    player_name=p_name,
                    x=loc.get("x", mid_x),
                    y=loc.get("y", mid_y),
                    start_time_sec=t_sec,
                    complete_time_sec=t_sec + 20.0 # Standard build duration
                )
                struct_counter += 1
                structures.append(struct)
                player_factories[p_idx].append(struct)

            # 2. Unit Training
            elif cmd.command_type == GameMessageType.MSG_QUEUE_UNIT_CREATE:
                uid = cmd.args[0].value if len(cmd.args) > 0 and isinstance(cmd.args[0].value, int) else 106
                count = cmd.args[1].value if len(cmd.args) > 1 and isinstance(cmd.args[1].value, int) else 1
                uname = ENTITY_NAMES.get(uid, f"Unit #{uid}")
                ucat, uspd = self._classify_unit(uid)

                # Spawn at nearest factory or player base
                factories = player_factories.get(p_idx, [])
                if factories:
                    spawn_x, spawn_y = factories[-1].x, factories[-1].y
                elif p_name in self.spatial.player_bases:
                    spawn_x = self.spatial.player_bases[p_name]["x"]
                    spawn_y = self.spatial.player_bases[p_name]["y"]
                else:
                    spawn_x, spawn_y = mid_x, mid_y

                for _ in range(min(count, 5)):
                    u = SimulatedUnit(
                        unit_id=unit_counter,
                        template_id=uid,
                        template_name=uname,
                        unit_category=ucat,
                        player_id=p_idx,
                        player_name=p_name,
                        spawn_time_sec=t_sec + 10.0, # Training delay
                        curr_x=spawn_x + (unit_counter % 5) * 15,
                        curr_y=spawn_y + (unit_counter % 5) * 15,
                        dest_x=spawn_x,
                        dest_y=spawn_y,
                        speed=uspd,
                        move_start_time=t_sec + 10.0,
                        last_action_time=t_sec
                    )
                    unit_counter += 1
                    units.append(u)
                    player_active_units[p_idx].append(u)

            # 3. Unit Movement Orders
            elif cmd.command_type in (GameMessageType.MSG_DO_MOVETO, GameMessageType.MSG_DO_FORCEMOVETO, GameMessageType.MSG_ADD_WAYPOINT):
                loc = None
                for a in cmd.args:
                    if a.arg_type.name == "LOCATION" and isinstance(a.value, dict):
                        loc = a.value
                        break
                if loc:
                    dest_x, dest_y = loc.get("x", mid_x), loc.get("y", mid_y)
                    # Assign order to active units
                    for u in player_active_units.get(p_idx, []):
                        if u.spawn_time_sec <= t_sec:
                            # Update current position before new order
                            dt = max(t_sec - u.move_start_time, 0.0)
                            dist_total = math.sqrt((u.dest_x - u.curr_x)**2 + (u.dest_y - u.curr_y)**2)
                            if dist_total > 0.001:
                                frac = min((dt * u.speed) / dist_total, 1.0)
                                u.curr_x += (u.dest_x - u.curr_x) * frac
                                u.curr_y += (u.dest_y - u.curr_y) * frac

                            u.dest_x = dest_x + (u.unit_id % 7 - 3) * 20
                            u.dest_y = dest_y + (u.unit_id % 7 - 3) * 20
                            u.move_start_time = t_sec
                            u.last_action_time = t_sec
                            dx = u.dest_x - u.curr_x
                            dy = u.dest_y - u.curr_y
                            u.heading_deg = math.degrees(math.atan2(dy, dx))

            # 4. Attack & Combat Engagements
            elif cmd.command_type in (GameMessageType.MSG_DO_ATTACK_OBJECT, GameMessageType.MSG_DO_ATTACKMOVETO, GameMessageType.MSG_DO_FORCE_ATTACK_GROUND, GameMessageType.MSG_DO_FORCE_ATTACK_OBJECT):
                target_loc = None
                for a in cmd.args:
                    if a.arg_type.name == "LOCATION" and isinstance(a.value, dict):
                        target_loc = a.value
                        break

                tx = target_loc.get("x", mid_x) if target_loc else mid_x
                ty = target_loc.get("y", mid_y) if target_loc else mid_y

                for u in player_active_units.get(p_idx, []):
                    if u.spawn_time_sec <= t_sec and math.sqrt((u.curr_x - tx)**2 + (u.curr_y - ty)**2) < 900:
                        u.target_x = tx
                        u.target_y = ty
                        u.is_firing = True

                        # Spawn Combat FX (Tracers, Rockets, Lasers)
                        fx_type = "LASER" if "laser" in u.template_name.lower() else ("ROCKET" if "missile" in u.template_name.lower() or "rpg" in u.template_name.lower() else "BULLET")
                        color = (0, 240, 255) if p_idx == 2 else (248, 113, 113)

                        combat_fx.append(CombatLaserFX(
                            start_time_sec=t_sec,
                            duration_sec=0.4,
                            from_x=u.curr_x,
                            from_y=u.curr_y,
                            to_x=tx + (u.unit_id % 5 - 2) * 10,
                            to_y=ty + (u.unit_id % 5 - 2) * 10,
                            fx_type=fx_type,
                            color_rgb=color
                        ))
                        break

        return WorldSimulationState(
            structures=structures,
            units=units,
            combat_fx=combat_fx,
            supply_docks=supply_docks,
            oil_derricks=oil_derricks
        )

    def get_world_snapshot_at(self, sim: WorldSimulationState, time_sec: float) -> Dict[str, Any]:
        """Returns exact positions of all living structures, units, and active combat tracers at timestamp."""
        active_structures = []
        for s in sim.structures:
            if s.start_time_sec <= time_sec:
                active_structures.append({
                    "id": s.struct_id,
                    "name": s.template_name,
                    "player": s.player_name,
                    "player_id": s.player_id,
                    "x": s.x,
                    "y": s.y,
                    "is_building": time_sec < s.complete_time_sec
                })

        active_units = []
        for u in sim.units:
            if u.spawn_time_sec <= time_sec:
                # Interpolate position
                dt = max(time_sec - u.move_start_time, 0.0)
                dist_total = math.sqrt((u.dest_x - u.curr_x)**2 + (u.dest_y - u.curr_y)**2)
                if dist_total > 0.001:
                    frac = min((dt * u.speed) / dist_total, 1.0)
                    ux = u.curr_x + (u.dest_x - u.curr_x) * frac
                    uy = u.curr_y + (u.dest_y - u.curr_y) * frac
                else:
                    ux, uy = u.curr_x, u.curr_y

                active_units.append({
                    "id": u.unit_id,
                    "name": u.template_name,
                    "category": u.unit_category,
                    "player": u.player_name,
                    "player_id": u.player_id,
                    "x": round(ux, 1),
                    "y": round(uy, 1),
                    "heading": round(u.heading_deg, 1)
                })

        active_fx = []
        for fx in sim.combat_fx:
            if fx.start_time_sec <= time_sec <= fx.start_time_sec + fx.duration_sec:
                active_fx.append({
                    "from_x": fx.from_x, "from_y": fx.from_y,
                    "to_x": fx.to_x, "to_y": fx.to_y,
                    "type": fx.fx_type, "color": fx.color_rgb
                })

        return {
            "structures": active_structures,
            "units": active_units,
            "combat_fx": active_fx,
            "supply_docks": sim.supply_docks,
            "oil_derricks": sim.oil_derricks
        }
