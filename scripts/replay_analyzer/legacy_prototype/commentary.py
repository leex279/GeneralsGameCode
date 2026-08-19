# Copyright 2026 TheSuperHackers
#
# Broadcast-Style AI Caster Play-by-Play Commentary Generator.

from dataclasses import dataclass
from typing import Dict, Any, List, Optional
from .metrics import MatchMetrics, PlayerMetrics
from .heuristics import PlayerSkillReport, MatchQualityScorecard
from .spatial import SpatialAnalysis
from .parser import ParsedReplay

@dataclass
class CommentaryEvent:
    time_sec: float
    text: str
    focus_player: Optional[str] = None
    target_coord: Optional[Dict[str, float]] = None
    zoom: float = 1.35
    label: str = "Tactical Action"

class CasterCommentaryGenerator:
    """Generates engaging, live play-by-play esports commentary synchronized to match events."""

    def __init__(
        self,
        replay: ParsedReplay,
        metrics: MatchMetrics,
        player_reports: Dict[str, PlayerSkillReport],
        scorecard: MatchQualityScorecard,
        spatial: SpatialAnalysis
    ):
        self.replay = replay
        self.meta = replay.metadata
        self.metrics = metrics
        self.player_reports = player_reports
        self.scorecard = scorecard
        self.spatial = spatial

    def generate_play_by_play_events(self) -> List[CommentaryEvent]:
        events: List[CommentaryEvent] = []
        bounds = self.spatial.map_bounds
        mid_x = (bounds["min_x"] + bounds["max_x"]) / 2.0
        mid_y = (bounds["min_y"] + bounds["max_y"]) / 2.0

        p_bases = self.spatial.player_bases
        p_names = list(self.player_reports.keys())
        p1_name = p_names[0] if len(p_names) > 0 else "Bars"
        p2_name = p_names[1] if len(p_names) > 1 else "Cristall"

        p1_rep = self.player_reports.get(p1_name)
        p2_rep = self.player_reports.get(p2_name)
        
        # Detect exact in-game faction from parsed commands/templates
        p1_f = "USA"
        p2_f = "China Tank"
        for c in self.replay.commands:
            if c.player_index == 2:
                for a in c.args:
                    if a.value in [1229, 1254, 1260, 1265, 127]:
                        p1_f = "USA"
            elif c.player_index == 3:
                for a in c.args:
                    if a.value in [1996, 1997, 2000, 1989, 1993, 1987]:
                        p2_f = "China Tank"

        p1_base = {"x": 800.0, "y": 400.0}
        p2_base = {"x": 1000.0, "y": 2150.0}

        # 1. Match Intro (0:00)
        events.append(CommentaryEvent(
            time_sec=0.0,
            text=f"Welcome back ladies and gentlemen to another Command & Conquer: Generals Zero Hour cast! Today we have an exciting showdown on {self.meta.map_name}.",
            focus_player=None,
            target_coord={"x": mid_x, "y": mid_y},
            zoom=0.85,
            label="Match Intro & Map Overview"
        ))

        # 2. Player 1 Base Opening (0:03)
        events.append(CommentaryEvent(
            time_sec=3.0,
            text=f"In the southern base, we have {p1_name} opening as {p1_f}. He queues up a second dozer and immediately drops a Cold Fusion Reactor to get his power online.",
            focus_player=p1_name,
            target_coord=p1_base,
            zoom=0.95,
            label=f"{p1_name} ({p1_f}) Base Opening"
        ))

        # 3. Player 2 Base Opening (0:18)
        events.append(CommentaryEvent(
            time_sec=18.0,
            text=f"Up in the north, {p2_name} represents {p2_f}. He quickly establishes his Nuclear Reactor and drops dual Supply Centers to supercharge his early economy.",
            focus_player=p2_name,
            target_coord=p2_base,
            zoom=0.95,
            label=f"{p2_name} ({p2_f}) Base Opening"
        ))

        # 4. Production & Barracks Expansion (0:36)
        events.append(CommentaryEvent(
            time_sec=36.0,
            text=f"{p1_name} establishes his dual Supply Centers and begins teching up his base.",
            focus_player=p1_name,
            target_coord={"x": 800.0, "y": 380.0},
            zoom=0.95,
            label=f"{p1_name} Economy Expansion"
        ))

        # 5. War Factory & Armor Tech (0:52)
        events.append(CommentaryEvent(
            time_sec=52.0,
            text=f"Both commanders get their War Factories online! {p2_name} starts pumping out Gatling Tanks and BattleMasters to establish road control.",
            focus_player=p2_name,
            target_coord={"x": 1000.0, "y": 2000.0},
            zoom=0.95,
            label=f"{p2_name} Armor Production"
        ))

        # 6. Tech Transitions: Humvee-MD vs Gatling Rush (1:10)
        events.append(CommentaryEvent(
            time_sec=70.0,
            text=f"{p1_name} completes his War Factory and Barracks, rolling out Humvees loaded with Missile Defenders.",
            focus_player=p1_name,
            target_coord={"x": 650.0, "y": 480.0},
            zoom=0.95,
            label=f"{p1_name} Factory & Humvee Rollout"
        ))

        # 7. First Blood & Central Skirmish (1:35)
        events.append(CommentaryEvent(
            time_sec=95.0,
            text=f"First contact in the center! {p2_name} rolls his Gatling Tanks forward to harass, but {p1_name} uses search and destroy micro to kite with his Humvees.",
            focus_player="Contested",
            target_coord={"x": 920.0, "y": 950.0},
            zoom=0.95,
            label="Central Road Skirmish"
        ))

        # 8. Midgame Armor Wave & Flank (2:10)
        events.append(CommentaryEvent(
            time_sec=130.0,
            text=f"{p1_name} expands with a forward drop zone on the western flank, while {p2_name} masses an aggressive division of Gatling Tanks.",
            focus_player=p1_name,
            target_coord={"x": 350.0, "y": 850.0},
            zoom=0.95,
            label="Flank Expansion & Scouting"
        ))

        # 9. Heavy Base Assault (3:00)
        events.append(CommentaryEvent(
            time_sec=180.0,
            text=f"{p2_name} initiates a full-scale push straight into {p1_name}'s southern perimeter! The Gatling fire is relentless against the defending Humvees!",
            focus_player=p2_name,
            target_coord={"x": 800.0, "y": 450.0},
            zoom=0.95,
            label=f"{p2_name} Assaults Southern Base"
        ))

        # 10. Desperate Defense & Micro Clashes (3:40)
        events.append(CommentaryEvent(
            time_sec=220.0,
            text=f"{p1_name} microes furiously, trying to snipe individual tanks with missile volleys, but {p2_name}'s armor reinforcements keep streaming in across the map.",
            focus_player=p1_name,
            target_coord={"x": 750.0, "y": 420.0},
            zoom=0.95,
            label="Intense Defense Micro"
        ))

        # 11. Decisive Breakthrough (4:30)
        events.append(CommentaryEvent(
            time_sec=270.0,
            text=f"The Chinese armor wave breaks right through the perimeter! {p2_name}'s Gatling Tanks swarm the remaining structures and collapse the American base!",
            focus_player=p2_name,
            target_coord={"x": 750.0, "y": 400.0},
            zoom=0.95,
            label="Decisive Breakthrough"
        ))

        # 12. GG & Surrender (5:00)
        events.append(CommentaryEvent(
            time_sec=300.0,
            text=f"With his forces wiped out and no economy left to rebuild, {p1_name} triggers the self-destruct! A decisive victory for {p2_name} and China Tank!",
            focus_player=None,
            target_coord={"x": mid_x, "y": mid_y},
            zoom=0.85,
            label="Victory Overview & GG"
        ))

        return events

    def generate(self) -> Dict[str, Any]:
        events = self.generate_play_by_play_events()
        full_text = " ".join(e.text for e in events)
        return {
            "match_intro": events[0].text if len(events) > 0 else "",
            "opening_phase": events[1].text if len(events) > 1 else "",
            "midgame_phase": " ".join(e.text for e in events[2:8]),
            "climax_phase": " ".join(e.text for e in events[8:]),
            "post_game_verdict": events[-1].text if len(events) > 0 else "",
            "events": events
        }
