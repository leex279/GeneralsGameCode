# Copyright 2026 TheSuperHackers
#
# Metrics computation engine for Generals & Zero Hour replay telemetry.

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from .parser import ParsedReplay, GameCommand, PlayerSlot
from .constants import GameMessageType

@dataclass
class TimelineEvent:
    frame: int
    timestamp_sec: float
    timestamp_formatted: str
    player_id: int
    player_name: str
    event_category: str  # "BUILD", "TRAIN", "UPGRADE", "SCIENCE", "SPECIAL_POWER", "TACTICAL"
    event_name: str
    details: Dict[str, Any] = field(default_factory=dict)

@dataclass
class PlayerMetrics:
    player_id: int
    player_name: str
    faction_name: str
    team: int
    total_commands: int
    avg_apm: float
    peak_apm: float
    effective_apm: float
    command_distribution: Dict[str, int] = field(default_factory=dict)
    apm_timeline: List[Dict[str, Any]] = field(default_factory=list) # [{time_sec, apm}]
    build_order: List[TimelineEvent] = field(default_factory=list)
    first_actions: Dict[str, Optional[float]] = field(default_factory=dict)

@dataclass
class MatchMetrics:
    duration_minutes: float
    total_commands: int
    players: Dict[int, PlayerMetrics] = field(default_factory=dict)
    battle_hotspots: List[Dict[str, Any]] = field(default_factory=list)
    chronological_timeline: List[TimelineEvent] = field(default_factory=list)


class MetricsCalculator:
    """Calculates APM, command distributions, build orders, and engagement telemetry."""

    def __init__(self, replay: ParsedReplay, window_seconds: float = 30.0):
        self.replay = replay
        self.meta = replay.metadata
        self.window_seconds = window_seconds

    def _format_time(self, seconds: float) -> str:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins:02d}:{secs:02d}"

    def calculate(self) -> MatchMetrics:
        duration_sec = max(self.meta.duration_seconds, 1.0)
        duration_min = duration_sec / 60.0

        # Build player lookup map
        # In Generals network commands:
        # Player 2 = Slot 0, Player 3 = Slot 1, Player 4 = Slot 2, etc.
        def get_player_slot(p_idx: int) -> Optional[PlayerSlot]:
            slot_idx = p_idx - 2
            if 0 <= slot_idx < len(self.meta.players):
                return self.meta.players[slot_idx]
            for p in self.meta.players:
                if p.slot_id == p_idx:
                    return p
            if 0 <= p_idx < len(self.meta.players):
                return self.meta.players[p_idx]
            return None

        # Group commands by player_index (filter out non-player or system commands)
        player_commands: Dict[int, List[GameCommand]] = {}
        for cmd in self.replay.commands:
            # Exclude CRC messages from APM calculation
            if cmd.command_type == GameMessageType.MSG_LOGIC_CRC:
                continue
            p_idx = cmd.player_index
            if p_idx not in player_commands:
                player_commands[p_idx] = []
            player_commands[p_idx].append(cmd)

        players_metrics: Dict[int, PlayerMetrics] = {}
        all_timeline_events: List[TimelineEvent] = []

        for p_idx, cmds in player_commands.items():
            slot_info = get_player_slot(p_idx)
            name = slot_info.name if slot_info else f"Player_{p_idx}"
            faction = slot_info.faction_name if slot_info else "Unknown"
            team = slot_info.team if slot_info else -1

            total_cmds = len(cmds)
            avg_apm = round((total_cmds / duration_min), 1)

            # Calculate APM over time windows
            max_time = int(duration_sec) + int(self.window_seconds)
            apm_timeline = []
            peak_apm = 0.0
            
            # Step every 15 seconds
            step = 15
            for t_start in range(0, max_time, step):
                t_end = t_start + self.window_seconds
                window_cmds = [c for c in cmds if t_start <= c.timestamp_sec < t_end]
                window_rate = (len(window_cmds) / (self.window_seconds / 60.0))
                window_rate = round(window_rate, 1)
                apm_timeline.append({
                    "time_sec": t_start,
                    "time_formatted": self._format_time(t_start),
                    "apm": window_rate
                })
                if window_rate > peak_apm:
                    peak_apm = window_rate

            # Command distribution & Effective APM
            category_counts = {
                "Build": 0,
                "Train": 0,
                "Upgrade": 0,
                "Science": 0,
                "SpecialPower": 0,
                "Move": 0,
                "Attack": 0,
                "Selection": 0,
                "Hotkey": 0,
                "Tactics": 0,
                "Other": 0
            }

            effective_cmd_count = 0
            player_build_order: List[TimelineEvent] = []
            first_actions = {
                "first_dozer_order": None,
                "first_unit_queued": None,
                "first_upgrade_queued": None,
                "first_attack_issued": None
            }

            for cmd in cmds:
                ctype = cmd.command_type
                cname = cmd.command_name
                t_sec = cmd.timestamp_sec
                t_fmt = self._format_time(t_sec)

                # Classify
                if ctype in (GameMessageType.MSG_DOZER_CONSTRUCT, GameMessageType.MSG_DOZER_CONSTRUCT_LINE):
                    category_counts["Build"] += 1
                    effective_cmd_count += 1
                    template_id = cmd.args[0].value if len(cmd.args) > 0 else None
                    loc = cmd.args[1].value if len(cmd.args) > 1 else None
                    event = TimelineEvent(
                        frame=cmd.frame,
                        timestamp_sec=t_sec,
                        timestamp_formatted=t_fmt,
                        player_id=p_idx,
                        player_name=name,
                        event_category="BUILD",
                        event_name="Construct Structure",
                        details={"template_id": template_id, "location": loc}
                    )
                    player_build_order.append(event)
                    all_timeline_events.append(event)
                    if first_actions["first_dozer_order"] is None:
                        first_actions["first_dozer_order"] = t_sec

                elif ctype == GameMessageType.MSG_QUEUE_UNIT_CREATE:
                    category_counts["Train"] += 1
                    effective_cmd_count += 1
                    unit_id = cmd.args[0].value if len(cmd.args) > 0 else None
                    count = cmd.args[1].value if len(cmd.args) > 1 else 1
                    event = TimelineEvent(
                        frame=cmd.frame,
                        timestamp_sec=t_sec,
                        timestamp_formatted=t_fmt,
                        player_id=p_idx,
                        player_name=name,
                        event_category="TRAIN",
                        event_name="Queue Unit",
                        details={"unit_id": unit_id, "count": count}
                    )
                    player_build_order.append(event)
                    all_timeline_events.append(event)
                    if first_actions["first_unit_queued"] is None:
                        first_actions["first_unit_queued"] = t_sec

                elif ctype == GameMessageType.MSG_QUEUE_UPGRADE:
                    category_counts["Upgrade"] += 1
                    effective_cmd_count += 1
                    upgrade_id = cmd.args[0].value if len(cmd.args) > 0 else None
                    event = TimelineEvent(
                        frame=cmd.frame,
                        timestamp_sec=t_sec,
                        timestamp_formatted=t_fmt,
                        player_id=p_idx,
                        player_name=name,
                        event_category="UPGRADE",
                        event_name="Research Upgrade",
                        details={"upgrade_id": upgrade_id}
                    )
                    player_build_order.append(event)
                    all_timeline_events.append(event)
                    if first_actions["first_upgrade_queued"] is None:
                        first_actions["first_upgrade_queued"] = t_sec

                elif ctype == GameMessageType.MSG_PURCHASE_SCIENCE:
                    category_counts["Science"] += 1
                    effective_cmd_count += 1
                    science_id = cmd.args[0].value if len(cmd.args) > 0 else None
                    event = TimelineEvent(
                        frame=cmd.frame,
                        timestamp_sec=t_sec,
                        timestamp_formatted=t_fmt,
                        player_id=p_idx,
                        player_name=name,
                        event_category="SCIENCE",
                        event_name="Purchase Science",
                        details={"science_id": science_id}
                    )
                    player_build_order.append(event)
                    all_timeline_events.append(event)

                elif ctype in (GameMessageType.MSG_DO_SPECIAL_POWER, GameMessageType.MSG_DO_SPECIAL_POWER_AT_LOCATION, GameMessageType.MSG_DO_SPECIAL_POWER_AT_OBJECT):
                    category_counts["SpecialPower"] += 1
                    effective_cmd_count += 1
                    event = TimelineEvent(
                        frame=cmd.frame,
                        timestamp_sec=t_sec,
                        timestamp_formatted=t_fmt,
                        player_id=p_idx,
                        player_name=name,
                        event_category="SPECIAL_POWER",
                        event_name="Trigger Special Power",
                        details={"type": cname}
                    )
                    all_timeline_events.append(event)

                elif ctype in (GameMessageType.MSG_DO_MOVETO, GameMessageType.MSG_DO_FORCEMOVETO, GameMessageType.MSG_ADD_WAYPOINT):
                    category_counts["Move"] += 1
                    effective_cmd_count += 1
                elif ctype in (GameMessageType.MSG_DO_ATTACKMOVETO, GameMessageType.MSG_DO_ATTACK_OBJECT, GameMessageType.MSG_DO_FORCE_ATTACK_GROUND, GameMessageType.MSG_DO_FORCE_ATTACK_OBJECT, GameMessageType.MSG_DO_ATTACKSQUAD):
                    category_counts["Attack"] += 1
                    effective_cmd_count += 1
                    if first_actions["first_attack_issued"] is None:
                        first_actions["first_attack_issued"] = t_sec
                elif ctype in (GameMessageType.MSG_CREATE_SELECTED_GROUP, GameMessageType.MSG_CREATE_SELECTED_GROUP_NO_SOUND, GameMessageType.MSG_DESTROY_SELECTED_GROUP, GameMessageType.MSG_REMOVE_FROM_SELECTED_GROUP, GameMessageType.MSG_AREA_SELECTION_DEPRECATED):
                    category_counts["Selection"] += 1
                elif (GameMessageType.MSG_CREATE_TEAM0 <= ctype <= GameMessageType.MSG_CREATE_TEAM9) or \
                     (GameMessageType.MSG_SELECT_TEAM0 <= ctype <= GameMessageType.MSG_SELECT_TEAM9) or \
                     (GameMessageType.MSG_ADD_TEAM0 <= ctype <= GameMessageType.MSG_ADD_TEAM9):
                    category_counts["Hotkey"] += 1
                    effective_cmd_count += 1
                elif ctype in (GameMessageType.MSG_DO_STOP, GameMessageType.MSG_DO_SCATTER, GameMessageType.MSG_DO_GUARD_POSITION, GameMessageType.MSG_DOCK, GameMessageType.MSG_ENTER, GameMessageType.MSG_EXIT, GameMessageType.MSG_EVACUATE, GameMessageType.MSG_SWITCH_WEAPONS):
                    category_counts["Tactics"] += 1
                    effective_cmd_count += 1
                else:
                    category_counts["Other"] += 1

            eapm = round((effective_cmd_count / duration_min), 1)

            # Infer actual faction if marked Random
            if faction in ("Random", "Unknown", "Observer"):
                usa_ids = {1229, 1254, 1250, 1265, 1260, 1290, 40, 45, 48, 49, 43, 135, 66, 106, 127, 129, 34, 52}
                china_ids = {1253, 1264, 1259, 1234, 1282, 1269, 285, 262, 284}
                gla_ids = {1996, 1997, 1998, 2000, 1889, 1885, 1883, 1887, 1882, 1774, 1776, 1775, 1777, 1771, 1991, 1993, 1990, 1989, 1987, 1873, 1861, 1858, 1767, 1759}

                built_ids = set()
                for bo in player_build_order:
                    if "template_id" in bo.details and bo.details["template_id"]:
                        built_ids.add(bo.details["template_id"])
                    if "unit_id" in bo.details and bo.details["unit_id"]:
                        built_ids.add(bo.details["unit_id"])

                if built_ids.intersection(usa_ids):
                    faction = "USA (via Random)"
                elif built_ids.intersection(china_ids):
                    faction = "China (via Random)"
                elif built_ids.intersection(gla_ids):
                    faction = "GLA (via Random)"

            players_metrics[p_idx] = PlayerMetrics(
                player_id=p_idx,
                player_name=name,
                faction_name=faction,
                team=team,
                total_commands=total_cmds,
                avg_apm=avg_apm,
                peak_apm=peak_apm,
                effective_apm=eapm,
                command_distribution=category_counts,
                apm_timeline=apm_timeline,
                build_order=player_build_order,
                first_actions=first_actions
            )


        all_timeline_events.sort(key=lambda x: x.frame)

        return MatchMetrics(
            duration_minutes=round(duration_min, 2),
            total_commands=len([c for c in self.replay.commands if c.command_type != GameMessageType.MSG_LOGIC_CRC]),
            players=players_metrics,
            chronological_timeline=all_timeline_events
        )
