# Copyright 2026 TheSuperHackers
#
# Match report generator for GeneralsGameCode replay analysis.

import json
from typing import Dict, Any
from .parser import ParsedReplay
from .metrics import MatchMetrics
from .heuristics import StrategyAnalyzer, PlayerSkillReport, MatchQualityScorecard
from .spatial import SpatialAnalyzer, SpatialAnalysis
from .commentary import CasterCommentaryGenerator
from .html_generator import HTMLReportGenerator
from .constants import ENTITY_NAMES

class ReplayReporter:
    """Formats and renders match analysis into Terminal, Markdown, JSON, and HTML."""

    def __init__(self, replay: ParsedReplay, metrics: MatchMetrics):
        self.replay = replay
        self.meta = replay.metadata
        self.metrics = metrics
        
        # 1. Spatial Analysis
        self.spatial = SpatialAnalyzer(replay).analyze()
        
        # 2. Player Skill Reports
        self.player_reports = {}
        for pid, p in metrics.players.items():
            self.player_reports[p.player_name] = StrategyAnalyzer.evaluate_player(p, metrics, self.spatial)
            
        # 3. Match Quality & Caster Scorecard
        self.scorecard = StrategyAnalyzer.analyze_match_quality(metrics, self.player_reports, self.spatial)
        
        # 4. AI Caster Commentary
        self.commentary = CasterCommentaryGenerator(
            replay, metrics, self.player_reports, self.scorecard, self.spatial
        ).generate()

    def _render_sparkline(self, numbers: list, width: int = 24) -> str:
        if not numbers:
            return ""
        chars = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        min_v = min(numbers)
        max_v = max(numbers)
        if max_v == min_v:
            return chars[0] * min(len(numbers), width)
        
        # Resample to width
        step = max(len(numbers) / width, 1)
        resampled = []
        for i in range(width):
            idx = int(i * step)
            if idx < len(numbers):
                resampled.append(numbers[idx])
            else:
                break
                
        out = []
        for v in resampled:
            norm = int((v - min_v) / (max_v - min_v) * (len(chars) - 1))
            norm = max(0, min(norm, len(chars) - 1))
            out.append(chars[norm])
        return "".join(out)

    def to_terminal(self) -> str:
        lines = []
        sep = "=" * 80
        sub_sep = "-" * 80

        lines.append(sep)
        lines.append(f"  C&C GENERALS: ZERO HOUR - REPLAY INTELLIGENCE & CASTER REPORT")
        lines.append(sep)
        lines.append(f"File:           {self.meta.filename}")
        lines.append(f"Map:            {self.meta.map_name}")
        lines.append(f"Duration:       {self.metrics.duration_minutes:.1f} mins ({self.meta.frame_count:,} frames)")
        lines.append(f"Starting Cash:  ${self.meta.starting_cash:,}")
        lines.append(f"Game Version:   {self.meta.version_string} (Build: {self.meta.build_time})")
        lines.append("")
        lines.append(f"★ CASTER SCORE: {self.scorecard.caster_score}/100 [{self.scorecard.verdict_badge}]")
        lines.append(f"  Verdict:      {self.scorecard.verdict}")
        lines.append(f"  Summary:      {self.scorecard.summary}")
        lines.append("")

        lines.append(sub_sep)
        lines.append(f"  PLAYER PERFORMANCE & SKILL BREAKDOWN")
        lines.append(sub_sep)

        for pname, rep in self.player_reports.items():
            p_raw = next((p for p in self.metrics.players.values() if p.player_name == pname), None)
            apm_series = [pt["apm"] for pt in p_raw.apm_timeline] if p_raw else []
            spark = self._render_sparkline(apm_series, 20)

            lines.append(f"• Player: {pname}")
            lines.append(f"  Faction:        {rep.faction_name}")
            lines.append(f"  Skill Tier:     {rep.skill_tier} (Score: {rep.skill_score}/100)")
            lines.append(f"  Archetype:      {rep.archetype}")
            lines.append(f"  Opening Meta:   {rep.detected_opening} ({rep.opening_speed_rating})")
            if p_raw:
                lines.append(f"  Average APM:    {p_raw.avg_apm:.1f} | Peak APM: {p_raw.peak_apm:.1f} | EAPM: {p_raw.effective_apm:.1f}")
                lines.append(f"  APM Curve:      [{spark}]")
                d = p_raw.command_distribution
                lines.append(f"  Distribution:   Move: {d.get('Move', 0)} | Attack: {d.get('Attack', 0)} | Build: {d.get('Build', 0)} | Train: {d.get('Train', 0)} | Select: {d.get('Selection', 0)}")
                fa = p_raw.first_actions
                f_dozer = f"{fa['first_dozer_order']:.1f}s" if fa.get("first_dozer_order") is not None else "N/A"
                f_unit = f"{fa['first_unit_queued']:.1f}s" if fa.get("first_unit_queued") is not None else "N/A"
                f_atk = f"{fa['first_attack_issued']:.1f}s" if fa.get("first_attack_issued") is not None else "N/A"
                lines.append(f"  Milestones:     1st Build: {f_dozer} | 1st Train: {f_unit} | 1st Attack: {f_atk}")

            if rep.strengths:
                lines.append("  Strengths:")
                for s in rep.strengths:
                    lines.append(f"    [+] {s}")
            if rep.blunders:
                lines.append("  Blunders / Inefficiencies:")
                for b in rep.blunders:
                    lines.append(f"    [-] {b}")
            if rep.coaching_tips:
                lines.append("  Coaching Tips:")
                for c in rep.coaching_tips:
                    lines.append(f"    💡 {c}")
            lines.append("")

        if self.spatial.hotspots:
            lines.append(sub_sep)
            lines.append("  KEY COMBAT HOTSPOTS & TACTICAL ENGAGEMENTS")
            lines.append(sub_sep)
            for idx, h in enumerate(self.spatial.hotspots[:4]):
                mins = int(h.first_time_sec // 60)
                involved = " vs ".join(h.involved_players)
                lines.append(f"  #{idx+1} [{mins:02d}:00] {h.description} | {h.intensity} attack orders ({involved})")
            lines.append("")

        if self.spatial.proxy_events:
            lines.append(sub_sep)
            lines.append("  FORWARD PROXIES & FLANKING MANEUVERS")
            lines.append(sub_sep)
            for pe in self.spatial.proxy_events:
                tname = ENTITY_NAMES.get(pe["template_id"], f"Structure #{pe['template_id']}")
                lines.append(f"  ⚠️ [{int(pe['time_sec']//60):02d}:{int(pe['time_sec']%60):02d}] {pe['player']} placed {tname} ({pe['distance_from_base']:.0f} units from base)")
            lines.append("")

        lines.append(sub_sep)
        lines.append("  AI BROADCAST COMMENTARY (DoMiNaToR Style)")
        lines.append(sub_sep)
        lines.append(f"  [Intro]    {self.commentary['match_intro']}")
        lines.append(f"  [Opening]  {self.commentary['opening_phase']}")
        lines.append(f"  [Mid-Game] {self.commentary['midgame_phase']}")
        lines.append(f"  [Climax]   {self.commentary['climax_phase']}")
        lines.append("")

        lines.append(sub_sep)
        lines.append("  OPENING BUILD ORDER TIMELINE (First 3 Minutes)")
        lines.append(sub_sep)

        early_events = [e for e in self.metrics.chronological_timeline if e.timestamp_sec <= 180.0]
        if early_events:
            for ev in early_events:
                details_str = ""
                if "template_id" in ev.details:
                    tid = ev.details["template_id"]
                    tname = ENTITY_NAMES.get(tid, f"Structure #{tid}")
                    details_str += f" [{tname}]"
                if "unit_id" in ev.details:
                    uid = ev.details["unit_id"]
                    uname = ENTITY_NAMES.get(uid, f"Unit #{uid}")
                    details_str += f" [{uname}]"
                if "upgrade_id" in ev.details:
                    details_str += f" [Upgrade #{ev.details['upgrade_id']}]"
                if "science_id" in ev.details:
                    details_str += f" [Science #{ev.details['science_id']}]"
                if "location" in ev.details and ev.details["location"]:
                    loc = ev.details["location"]
                    details_str += f" @ ({loc.get('x',0)}, {loc.get('y',0)})"

                lines.append(f"  [{ev.timestamp_formatted}] {ev.player_name:12s} | {ev.event_category:13s} | {ev.event_name}{details_str}")
        else:
            lines.append("  No opening construction/training events found in first 3 minutes.")

        lines.append(sep)
        return "\n".join(lines)

    def to_json(self) -> str:
        data = {
            "metadata": {
                "filename": self.meta.filename,
                "map": self.meta.map_name,
                "duration_seconds": self.meta.duration_seconds,
                "duration_minutes": self.metrics.duration_minutes,
                "frame_count": self.meta.frame_count,
                "starting_cash": self.meta.starting_cash,
                "seed": self.meta.seed,
                "version": self.meta.version_string,
                "build_time": self.meta.build_time
            },
            "caster_scorecard": {
                "score": self.scorecard.caster_score,
                "verdict": self.scorecard.verdict,
                "badge": self.scorecard.verdict_badge,
                "is_high_level": self.scorecard.is_high_level_game,
                "summary": self.scorecard.summary,
                "breakdown": {
                    "skill_balance": self.scorecard.skill_balance_score,
                    "combat_intensity": self.scorecard.combat_intensity_score,
                    "meta_adherence": self.scorecard.meta_adherence_score,
                    "pacing": self.scorecard.pacing_and_drama_score
                }
            },
            "players": {
                pname: {
                    "skill_score": rep.skill_score,
                    "skill_tier": rep.skill_tier,
                    "archetype": rep.archetype,
                    "opening_strategy": rep.detected_opening,
                    "opening_speed": rep.opening_speed_rating,
                    "strengths": rep.strengths,
                    "blunders": rep.blunders,
                    "coaching_tips": rep.coaching_tips,
                    "metrics": {
                        "avg_apm": p_raw.avg_apm,
                        "peak_apm": p_raw.peak_apm,
                        "effective_apm": p_raw.effective_apm,
                        "total_commands": p_raw.total_commands,
                        "distribution": p_raw.command_distribution,
                        "first_actions": p_raw.first_actions
                    } if (p_raw := next((p for p in self.metrics.players.values() if p.player_name == pname), None)) else {}
                }
                for pname, rep in self.player_reports.items()
            },
            "spatial": {
                "bounds": self.spatial.map_bounds,
                "hotspots": [
                    {
                        "x": h.center_x,
                        "y": h.center_y,
                        "intensity": h.intensity,
                        "description": h.description,
                        "involved": h.involved_players
                    }
                    for h in self.spatial.hotspots
                ],
                "proxies": self.spatial.proxy_events
            },
            "commentary": self.commentary,
            "timeline": [
                {
                    "frame": ev.frame,
                    "time_sec": ev.timestamp_sec,
                    "time": ev.timestamp_formatted,
                    "player": ev.player_name,
                    "category": ev.event_category,
                    "event": ev.event_name,
                    "details": ev.details
                }
                for ev in self.metrics.chronological_timeline
            ]
        }
        return json.dumps(data, indent=2)

    def to_markdown(self) -> str:
        md = []
        md.append(f"# C&C Generals Zero Hour Match Report: {self.meta.filename}\n")
        md.append(f"**Map**: `{self.meta.map_name}` | **Duration**: `{self.metrics.duration_minutes:.1f} mins` | **Starting Cash**: `${self.meta.starting_cash:,}`\n")
        md.append(f"### 🏆 Caster Scorecard: **{self.scorecard.caster_score}/100** (`{self.scorecard.verdict_badge}`)\n")
        md.append(f"> **{self.scorecard.verdict}**\n>\n> {self.scorecard.summary}\n")

        md.append("## Players Performance & Skill Rating\n")
        md.append("| Player | Faction | Skill Tier (Score) | Opening Strategy | Avg APM | Peak APM |")
        md.append("|---|---|---|---|---|---|")
        for pname, rep in self.player_reports.items():
            p_raw = next((p for p in self.metrics.players.values() if p.player_name == pname), None)
            avg_apm = f"{p_raw.avg_apm:.0f}" if p_raw else "-"
            peak_apm = f"{p_raw.peak_apm:.0f}" if p_raw else "-"
            md.append(f"| **{pname}** | {rep.faction_name} | **{rep.skill_tier}** ({rep.skill_score}/100) | {rep.detected_opening} | {avg_apm} | {peak_apm} |")
        md.append("")

        for pname, rep in self.player_reports.items():
            md.append(f"### {pname} ({rep.faction_name})")
            md.append(f"- **Archetype**: {rep.archetype}")
            md.append(f"- **Opening Speed**: {rep.opening_speed_rating}")
            if rep.strengths:
                md.append("- **Strengths**:")
                for s in rep.strengths:
                    md.append(f"  - `[+]` {s}")
            if rep.blunders:
                md.append("- **Blunders / Areas for Improvement**:")
                for b in rep.blunders:
                    md.append(f"  - `[-]` {b}")
            if rep.coaching_tips:
                md.append("- **Coaching Tip**:")
                for c in rep.coaching_tips:
                    md.append(f"  - 💡 {c}")
            md.append("")

        md.append("## 🎙️ AI Broadcast Commentary (DoMiNaToR Style)\n")
        md.append(f"**Intro**: {self.commentary['match_intro']}\n")
        md.append(f"**Opening Phase**: {self.commentary['opening_phase']}\n")
        md.append(f"**Mid-Game**: {self.commentary['midgame_phase']}\n")
        md.append(f"**Climax**: {self.commentary['climax_phase']}\n")

        return "\n".join(md)

    def to_html(self) -> str:
        generator = HTMLReportGenerator(
            self.replay, self.metrics, self.player_reports,
            self.scorecard, self.spatial, self.commentary
        )
        return generator.generate()

