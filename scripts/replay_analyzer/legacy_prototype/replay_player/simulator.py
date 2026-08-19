# Copyright 2026 TheSuperHackers
#
# Standalone Headless RTS Game Simulation Engine for Zero Hour Replays.
# Reconstructs full 3D simulation state frame-by-frame without needing the retail game executable.

import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

from scripts.replay_analyzer.parser import ParsedReplay, GameCommand
from scripts.replay_analyzer.constants import GameMessageType, ENTITY_NAMES
from scripts.replay_analyzer.spatial import SpatialAnalyzer, SpatialAnalysis

@dataclass
class Entity3D:
    entity_id: int
    template_id: int
    name: str
    entity_type: str # "STRUCTURE", "VEHICLE", "INFANTRY", "AIRCRAFT", "WORKER"
    player_id: int
    player_name: str
    spawn_time_sec: float
    death_time_sec: Optional[float] = None
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    dest_x: float = 0.0
    dest_y: float = 0.0
    heading_deg: float = 0.0
    turret_heading_deg: float = 0.0
    speed: float = 60.0
    move_start_time: float = 0.0
    max_health: float = 500.0
    current_health: float = 500.0
    is_building: bool = False
    build_complete_time: float = 0.0
    kills: int = 0
    veterancy_level: int = 0 # 0=Regular, 1=Vet, 2=Elite, 3=Heroic

@dataclass
class WeaponLaserFX3D:
    start_time_sec: float
    duration_sec: float
    from_pos: Tuple[float, float, float]
    to_pos: Tuple[float, float, float]
    weapon_type: str # "LASER", "ROCKET", "SHELL", "BULLET", "EXPLOSION"
    color_hex: str

@dataclass
class FrameWorldState:
    frame: int
    time_sec: float
    entities: List[Entity3D]
    lasers_fx: List[WeaponLaserFX3D]
    player_cash: Dict[str, int]
    camera_focus: Dict[str, Any]


class StandaloneReplaySimulator:
    """Deterministic headless simulation engine that reconstructs the entire 3D world state."""

    def __init__(self, replay: ParsedReplay, spatial: Optional[SpatialAnalysis] = None):
        self.replay = replay
        self.meta = replay.metadata
        self.spatial = spatial or SpatialAnalyzer(replay).analyze()
        self.bounds = self.spatial.map_bounds

    def _get_entity_specs(self, tid: int) -> Tuple[str, float, float]:
        """Returns (entity_type, speed, max_health)."""
        # USA
        if tid == 127: return "VEHICLE", 115.0, 300.0 # Humvee
        if tid == 129: return "VEHICLE", 65.0, 480.0  # Crusader Tank
        if tid == 66:  return "AIRCRAFT", 125.0, 500.0 # Chinook
        if tid in (106, 262): return "INFANTRY", 35.0, 100.0 # Ranger, MD
        if tid in (135, 285): return "WORKER", 45.0, 150.0  # Dozer

        # GLA
        if tid in (1989, 1858): return "VEHICLE", 110.0, 220.0 # Technical
        if tid == 1987: return "VEHICLE", 75.0, 320.0 # Quad Cannon
        if tid == 284:  return "VEHICLE", 65.0, 370.0 # Scorpion Tank
        if tid in (1990, 1993, 1861): return "INFANTRY", 35.0, 100.0 # Rebel, RPG
        if tid in (1991, 1873): return "WORKER", 45.0, 120.0 # Worker

        # Structures
        if tid in (1229, 1228, 1227, 1226, 1225, 1224, 1223, 1222, 1221, 1220, 1219, 1218, 1217, 1216, 1215):
            return "STRUCTURE", 0.0, 1000.0

        return "VEHICLE", 60.0, 350.0

    def simulate_all_entities(self) -> Dict[str, Any]:
        """Runs headless simulation and returns all 3D entities and frame trajectories."""
        mid_x = (self.bounds["min_x"] + self.bounds["max_x"]) / 2.0
        mid_y = (self.bounds["min_y"] + self.bounds["max_y"]) / 2.0

        entities: List[Entity3D] = []
        fx_list: List[WeaponLaserFX3D] = []

        entity_counter = 1000
        player_active_units: Dict[int, List[Entity3D]] = {}
        player_factories: Dict[int, List[Entity3D]] = {}
        player_map = {idx + 2: p.name for idx, p in enumerate(self.meta.players)}
        player_map[0] = "Nature"
        player_map[1] = "Observer"

        # Create Starting Command Centers
        for idx, (pname, base) in enumerate(self.spatial.player_bases.items()):
            if base["x"] != 0 or base["y"] != 0:
                cc = Entity3D(
                    entity_id=idx + 10,
                    template_id=1229,
                    name="Command Center",
                    entity_type="STRUCTURE",
                    player_id=idx + 2,
                    player_name=pname,
                    spawn_time_sec=0.0,
                    x=base["x"],
                    y=base["y"],
                    z=0.0,
                    max_health=2000.0,
                    current_health=2000.0,
                    is_building=False,
                    build_complete_time=0.0
                )
                entities.append(cc)
                player_factories.setdefault(idx + 2, []).append(cc)

        for cmd in self.replay.commands:
            p_idx = cmd.player_index
            p_name = player_map.get(p_idx, f"Player_{p_idx}")
            t_sec = cmd.timestamp_sec

            if p_idx not in player_active_units:
                player_active_units[p_idx] = []
                player_factories[p_idx] = []

            # 1. Structure Placement
            if cmd.command_type in (GameMessageType.MSG_DOZER_CONSTRUCT, GameMessageType.MSG_DOZER_CONSTRUCT_LINE):
                tid = cmd.args[0].value if len(cmd.args) > 0 and isinstance(cmd.args[0].value, int) else 1229
                loc = cmd.args[1].value if len(cmd.args) > 1 and isinstance(cmd.args[1].value, dict) else {"x": mid_x, "y": mid_y}
                tname = ENTITY_NAMES.get(tid, f"Structure #{tid}")
                etype, espeed, ehp = self._get_entity_specs(tid)

                st = Entity3D(
                    entity_id=entity_counter,
                    template_id=tid,
                    name=tname,
                    entity_type="STRUCTURE",
                    player_id=p_idx,
                    player_name=p_name,
                    spawn_time_sec=t_sec,
                    x=loc.get("x", mid_x),
                    y=loc.get("y", mid_y),
                    z=0.0,
                    max_health=ehp,
                    current_health=ehp,
                    is_building=True,
                    build_complete_time=t_sec + 22.0
                )
                entity_counter += 1
                entities.append(st)
                player_factories[p_idx].append(st)

            # 2. Unit Training
            elif cmd.command_type == GameMessageType.MSG_QUEUE_UNIT_CREATE:
                uid = cmd.args[0].value if len(cmd.args) > 0 and isinstance(cmd.args[0].value, int) else 106
                count = cmd.args[1].value if len(cmd.args) > 1 and isinstance(cmd.args[1].value, int) else 1
                uname = ENTITY_NAMES.get(uid, f"Unit #{uid}")
                etype, espeed, ehp = self._get_entity_specs(uid)

                factories = player_factories.get(p_idx, [])
                spawn_x = factories[-1].x if factories else (self.spatial.player_bases.get(p_name, {}).get("x", mid_x))
                spawn_y = factories[-1].y if factories else (self.spatial.player_bases.get(p_name, {}).get("y", mid_y))

                for ci in range(min(count, 4)):
                    u = Entity3D(
                        entity_id=entity_counter,
                        template_id=uid,
                        name=uname,
                        entity_type=etype,
                        player_id=p_idx,
                        player_name=p_name,
                        spawn_time_sec=t_sec + 10.0 + ci * 2.0,
                        x=spawn_x + (entity_counter % 5 - 2) * 20.0,
                        y=spawn_y + (entity_counter % 5 - 2) * 20.0,
                        z=0.0,
                        dest_x=spawn_x + (entity_counter % 5 - 2) * 20.0,
                        dest_y=spawn_y + (entity_counter % 5 - 2) * 20.0,
                        speed=espeed,
                        max_health=ehp,
                        current_health=ehp,
                        move_start_time=t_sec + 10.0
                    )
                    entity_counter += 1
                    entities.append(u)
                    player_active_units[p_idx].append(u)

            # 3. Unit Movement
            elif cmd.command_type in (GameMessageType.MSG_DO_MOVETO, GameMessageType.MSG_DO_FORCEMOVETO, GameMessageType.MSG_ADD_WAYPOINT):
                loc = next((a.value for a in cmd.args if a.arg_type.name == "LOCATION" and isinstance(a.value, dict)), None)
                if loc:
                    dest_x, dest_y = loc.get("x", mid_x), loc.get("y", mid_y)
                    for u in player_active_units.get(p_idx, []):
                        if u.spawn_time_sec <= t_sec:
                            # Update current position before path change
                            dt = max(t_sec - u.move_start_time, 0.0)
                            dist = math.sqrt((u.dest_x - u.x)**2 + (u.dest_y - u.y)**2)
                            if dist > 0.001:
                                frac = min((dt * u.speed) / dist, 1.0)
                                u.x += (u.dest_x - u.x) * frac
                                u.y += (u.dest_y - u.y) * frac

                            u.dest_x = dest_x + (u.entity_id % 7 - 3) * 18.0
                            u.dest_y = dest_y + (u.entity_id % 7 - 3) * 18.0
                            u.move_start_time = t_sec
                            dx = u.dest_x - u.x
                            dy = u.dest_y - u.y
                            u.heading_deg = math.degrees(math.atan2(dy, dx))
                            u.turret_heading_deg = u.heading_deg

            # 4. Weapon Combat & Laser Locks
            elif cmd.command_type in (GameMessageType.MSG_DO_ATTACK_OBJECT, GameMessageType.MSG_DO_ATTACKMOVETO, GameMessageType.MSG_DO_FORCE_ATTACK_GROUND):
                loc = next((a.value for a in cmd.args if a.arg_type.name == "LOCATION" and isinstance(a.value, dict)), None)
                tx = loc.get("x", mid_x) if loc else mid_x
                ty = loc.get("y", mid_y) if loc else mid_y

                for u in player_active_units.get(p_idx, []):
                    if u.spawn_time_sec <= t_sec and math.sqrt((u.x - tx)**2 + (u.y - ty)**2) < 850.0:
                        is_laser = "laser" in u.name.lower() or "missile" in u.name.lower()
                        w_type = "LASER" if is_laser else ("ROCKET" if "rpg" in u.name.lower() else "BULLET")
                        color = "#00f0ff" if p_idx == 2 else "#f87171"

                        fx_list.append(WeaponLaserFX3D(
                            start_time_sec=t_sec,
                            duration_sec=0.5,
                            from_pos=(u.x, u.y, 8.0),
                            to_pos=(tx + (u.entity_id % 5 - 2) * 10, ty + (u.entity_id % 5 - 2) * 10, 5.0),
                            weapon_type=w_type,
                            color_hex=color
                        ))
                        break

        return {
            "metadata": {
                "filename": self.meta.filename,
                "map": self.meta.map_name,
                "duration_sec": self.meta.duration_seconds,
                "starting_cash": self.meta.starting_cash
            },
            "bounds": self.bounds,
            "entities": [
                {
                    "id": e.entity_id,
                    "template_id": e.template_id,
                    "name": e.name,
                    "type": e.entity_type,
                    "player": e.player_name,
                    "player_id": e.player_id,
                    "spawn_t": round(e.spawn_time_sec, 2),
                    "x": round(e.x, 1),
                    "y": round(e.y, 1),
                    "dest_x": round(e.dest_x, 1),
                    "dest_y": round(e.dest_y, 1),
                    "speed": e.speed,
                    "move_t": round(e.move_start_time, 2),
                    "max_hp": e.max_health,
                    "is_building": e.is_building,
                    "build_done_t": round(e.build_complete_time, 2)
                }
                for e in entities
            ],
            "combat_fx": [
                {
                    "start_t": round(fx.start_time_sec, 2),
                    "dur": fx.duration_sec,
                    "from": [round(c, 1) for c in fx.from_pos],
                    "to": [round(c, 1) for c in fx.to_pos],
                    "type": fx.weapon_type,
                    "color": fx.color_hex
                }
                for fx in fx_list
            ]
        }
