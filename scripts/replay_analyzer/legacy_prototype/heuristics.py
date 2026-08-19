# Copyright 2026 TheSuperHackers
#
# Advanced Heuristics Engine: Match Quality / Caster Score & Pro vs Noob Grader.

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from .metrics import PlayerMetrics, MatchMetrics
from .spatial import SpatialAnalysis

@dataclass
class PlayerSkillReport:
    player_name: str
    faction_name: str
    skill_score: int # 0-100
    skill_tier: str # "Grandmaster / Pro", "High Competitive", "Intermediate", "Casual", "Beginner / Noob"
    archetype: str
    detected_opening: str
    opening_speed_rating: str
    micro_rating: str
    macro_rating: str
    hotkey_rating: str
    blunders: List[str] = field(default_factory=list)
    strengths: List[str] = field(default_factory=list)
    coaching_tips: List[str] = field(default_factory=list)

@dataclass
class MatchQualityScorecard:
    caster_score: int # 0-100
    verdict: str
    verdict_badge: str
    skill_balance_score: int # 0-25
    combat_intensity_score: int # 0-30
    meta_adherence_score: int # 0-25
    pacing_and_drama_score: int # 0-20
    is_high_level_game: bool
    summary: str
    key_turning_points: List[str] = field(default_factory=list)


class StrategyAnalyzer:
    """Classifies player strategies, detects blunders, rates skill, and scores caster potential."""

    @staticmethod
    def _detect_opening_strategy(p: PlayerMetrics, spatial: Optional[SpatialAnalysis] = None) -> str:
        f = p.faction_name.lower()
        early_events = [e for e in p.build_order if e.timestamp_sec <= 180.0]
        struct_ids = [e.details.get("template_id") for e in early_events if e.event_category == "BUILD"]
        unit_ids = [e.details.get("unit_id") for e in early_events if e.event_category == "TRAIN"]

        # Check for forward proxy
        has_proxy = False
        if spatial and p.player_id in spatial.player_profiles:
            has_proxy = len(spatial.player_profiles[p.player_id].forward_proxy_structures) > 0

        # USA Openings
        if "usa" in f or 1229 in struct_ids or 40 in struct_ids:
            num_supply = struct_ids.count(1254) + struct_ids.count(45)
            has_wf = 1265 in struct_ids or 49 in struct_ids
            has_barracks = 1250 in struct_ids or 48 in struct_ids
            has_humvee = 127 in unit_ids
            has_chinook = 66 in unit_ids

            if num_supply >= 2 and has_wf and has_humvee:
                return "USA Dual Supply Fast Humvee Rush (Standard High-Level Meta)"
            elif num_supply >= 2 and has_chinook:
                return "USA Dual Supply Chinook Economic Boom"
            elif has_wf and has_humvee:
                return "USA Fast War Factory Aggressive Humvee / Missile Harass"
            elif has_barracks and not has_wf:
                return "USA Barracks Opening / Fast Infantry Creep"
            elif num_supply >= 2:
                return "USA Standard Dual Supply Opening"
            return "USA Standard Opening"

        # China Openings
        elif "china" in f or 1253 in struct_ids or 1264 in struct_ids:
            has_wf = 1264 in struct_ids
            has_barracks = 1234 in struct_ids
            has_gats = 284 in unit_ids or 262 in unit_ids
            has_prop = 1282 in struct_ids

            if has_wf and has_gats:
                return "China Fast War Factory (Gatling & Battlemaster Tank Push)"
            elif has_wf and has_prop:
                return "China Fast War Factory into Propaganda Tech"
            elif has_barracks and not has_wf:
                return "China Dual Barracks Infantry Swarm / Capture"
            return "China Standard Supply & Vehicle Opening"

        # GLA Openings
        elif "gla" in f or 1996 in struct_ids or 1889 in struct_ids or 1774 in struct_ids:
            num_supply = struct_ids.count(1996) + struct_ids.count(1889) + struct_ids.count(1774)
            num_barracks = struct_ids.count(1997) + struct_ids.count(1885) + struct_ids.count(1776)
            num_tunnels = struct_ids.count(2000) + struct_ids.count(1883) + struct_ids.count(1775)
            has_arms = 1998 in struct_ids or 1887 in struct_ids or 1777 in struct_ids
            has_tech = 1989 in unit_ids or 1858 in unit_ids

            if has_proxy or num_tunnels >= 2:
                return "GLA Forward Tunnel Network Flank & Map Containment"
            elif num_barracks >= 2 and num_tunnels >= 1:
                return "GLA Dual Barracks RPG & Tunnel Harassment"
            elif has_arms and has_tech:
                return "GLA Fast Arms Dealer (Technical & Scorpion Mobility)"
            elif num_supply >= 2:
                return "GLA Dual Supply Stash Economic Opening"
            return "GLA Standard Tunnel & Worker Opening"

        return "Standard RTS Faction Opening"

    @staticmethod
    def evaluate_player(p: PlayerMetrics, match: MatchMetrics, spatial: Optional[SpatialAnalysis] = None) -> PlayerSkillReport:
        dist = p.command_distribution
        total = max(p.total_commands, 1)

        micro_cmds = dist.get("Move", 0) + dist.get("Attack", 0) + dist.get("Tactics", 0)
        macro_cmds = dist.get("Build", 0) + dist.get("Train", 0) + dist.get("Upgrade", 0) + dist.get("Science", 0)
        selection_cmds = dist.get("Selection", 0)
        hotkey_cmds = dist.get("Hotkey", 0)

        micro_pct = (micro_cmds / total) * 100
        macro_pct = (macro_cmds / total) * 100
        selection_pct = (selection_cmds / total) * 100
        hotkey_pct = (hotkey_cmds / total) * 100

        blunders = []
        strengths = []
        coaching = []

        # 1. Opening Speed & Execution (0-25)
        opening_pts = 20
        fa = p.first_actions
        f_build = fa.get("first_dozer_order")
        f_train = fa.get("first_unit_queued")

        if f_build is None or f_build > 12.0:
            opening_pts -= 12
            blunders.append(f"Severe opening delay: First building placed after {f_build:.1f}s (Pro standard: < 5s)")
            coaching.append("Queue starting worker/dozer and place your power/supply within the first 3 seconds.")
        elif f_build <= 5.0:
            opening_pts += 5
            strengths.append(f"Instant high-level opening execution ({f_build:.1f}s initial placement)")
            opening_speed_rating = "Excellent (< 5s)"
        else:
            opening_speed_rating = f"Moderate ({f_build:.1f}s)"

        # 2. APM & Mechanics Rating (0-35)
        apm_pts = 0
        if p.avg_apm >= 220:
            apm_pts = 35
            strengths.append(f"World-class APM pace ({p.avg_apm:.0f} APM with {p.peak_apm:.0f} peak)")
            mechanics_tier = "Grandmaster / Pro"
        elif p.avg_apm >= 150:
            apm_pts = 28
            strengths.append(f"High competitive APM ({p.avg_apm:.0f} APM)")
            mechanics_tier = "High Competitive"
        elif p.avg_apm >= 90:
            apm_pts = 20
            mechanics_tier = "Intermediate Competitor"
        elif p.avg_apm >= 50:
            apm_pts = 12
            mechanics_tier = "Casual RTS Player"
            coaching.append("Increase command rate by using rally points and shift-queuing move/attack orders.")
        else:
            apm_pts = 5
            blunders.append(f"Very slow command throughput ({p.avg_apm:.0f} APM) — high idling detected")
            mechanics_tier = "Beginner / Noob"
            coaching.append("Avoid watching units fight without issuing repositioning/stutter-step orders.")

        # 3. Micro vs Macro Balance (0-25)
        balance_pts = 15
        if selection_pct > 65:
            balance_pts -= 8
            blunders.append(f"Excessive spam clicking: {selection_pct:.1f}% of all actions were redundant selections")
            coaching.append("Focus on giving actionable movement and attack target orders rather than repeatedly clicking units.")
        elif micro_pct >= 35:
            balance_pts += 10
            strengths.append(f"High micro density: {micro_pct:.1f}% of commands were dedicated unit control orders")

        # 4. Hotkeys & Tactical Depth (0-15)
        hotkey_pts = 5
        if hotkey_pct >= 10:
            hotkey_pts = 15
            strengths.append(f"Excellent squad hotkey usage ({hotkey_pct:.1f}% control group orders)")
            hotkey_rating = "Advanced (Ctrl+1-9 squads)"
        elif hotkey_pct >= 3:
            hotkey_pts = 10
            hotkey_rating = "Moderate"
        else:
            hotkey_pts = 2
            hotkey_rating = "Minimal / None"
            coaching.append("Assign primary assault units and production buildings to hotkeys (Ctrl+1..9).")

        # Total Skill Score (0-100)
        raw_score = opening_pts + apm_pts + balance_pts + hotkey_pts
        skill_score = max(5, min(100, raw_score))

        # Archetype
        if micro_pct > 38:
            archetype = "Micro Specialist (Heavy Unit Control & Stutter-Stepping)"
        elif macro_pct > 15:
            archetype = "Macro General (Multi-Depot Eco & Unit Production)"
        elif hotkey_pct > 12:
            archetype = "Hotkey Tactician (Squad-Based Coordination)"
        else:
            archetype = "Standard Balanced Player"

        detected_opening = StrategyAnalyzer._detect_opening_strategy(p, spatial)

        return PlayerSkillReport(
            player_name=p.player_name,
            faction_name=p.faction_name,
            skill_score=skill_score,
            skill_tier=mechanics_tier,
            archetype=archetype,
            detected_opening=detected_opening,
            opening_speed_rating=opening_speed_rating if 'opening_speed_rating' in locals() else "Standard",
            micro_rating=f"{micro_pct:.1f}% ({dist.get('Move',0) + dist.get('Attack',0)} commands)",
            macro_rating=f"{macro_pct:.1f}% ({dist.get('Build',0) + dist.get('Train',0)} orders)",
            hotkey_rating=hotkey_rating,
            blunders=blunders,
            strengths=strengths,
            coaching_tips=coaching
        )

    @staticmethod
    def analyze_match_quality(
        match: MatchMetrics,
        player_reports: Dict[str, PlayerSkillReport],
        spatial: Optional[SpatialAnalysis] = None
    ) -> MatchQualityScorecard:
        players = list(player_reports.values())
        if not players:
            return MatchQualityScorecard(
                caster_score=10,
                verdict="Empty Match",
                verdict_badge="No Data",
                skill_balance_score=0,
                combat_intensity_score=0,
                meta_adherence_score=0,
                pacing_and_drama_score=0,
                is_high_level_game=False,
                summary="No player actions recorded."
            )

        # 1. Skill Balance Score (0-25)
        scores = [p.skill_score for p in players]
        avg_skill = sum(scores) / len(scores)
        skill_diff = max(scores) - min(scores) if len(scores) > 1 else 0

        # Close games between high-skill players get max points
        balance_pts = max(0, 25 - int(skill_diff * 0.4))
        if avg_skill > 60:
            balance_pts = min(25, balance_pts + 5)

        # 2. Combat Intensity Score (0-30)
        total_attacks = sum(p_raw.command_distribution.get("Attack", 0) for p_raw in match.players.values())
        attacks_per_min = total_attacks / max(match.duration_minutes, 1.0)
        
        if attacks_per_min >= 20.0:
            combat_pts = 30
        elif attacks_per_min >= 10.0:
            combat_pts = 22
        elif attacks_per_min >= 4.0:
            combat_pts = 14
        else:
            combat_pts = 6

        # 3. Meta Adherence & Skill Level (0-25)
        meta_pts = int(avg_skill * 0.25)

        # 4. Pacing & Drama (0-20)
        pacing_pts = 10
        if 8.0 <= match.duration_minutes <= 25.0:
            pacing_pts = 20 # Ideal competitive broadcast length
        elif match.duration_minutes < 5.0:
            pacing_pts = 8 # Quick rush / early quit
        elif match.duration_minutes > 35.0:
            pacing_pts = 14 # Long attrition war

        total_caster_score = balance_pts + combat_pts + meta_pts + pacing_pts
        caster_score = max(5, min(100, total_caster_score))

        # Determine Verdict & Badge
        is_high_level = avg_skill >= 65 and all(p.skill_score >= 45 for p in players)

        if caster_score >= 85 and is_high_level:
            verdict = "🌟 S-Tier Match: Top-Level Thriller (Certified Casting Gold)"
            badge = "S-Tier Caster Gold"
            summary = "Both players demonstrate high competitive mechanics, meta openings, and high-tempo back-and-forth engagements. Highly recommended for YouTube / Twitch casting!"
        elif caster_score >= 70:
            verdict = "⚔️ A-Tier Match: High-Skill Competitive Battle"
            badge = "A-Tier Competitive"
            summary = "Fast-paced match featuring crisp build orders, solid micro engagements, and active map control."
        elif caster_score >= 50:
            verdict = "📊 B-Tier Match: Decent Skirmish / Moderate Skill"
            badge = "B-Tier Decent"
            summary = "Entertaining match with good moments, though some strategic inefficiencies or slower command execution are present."
        elif caster_score >= 35:
            verdict = "📉 C-Tier Match: One-Sided Stomp / Heavy Skill Imbalance"
            badge = "C-Tier Imbalanced"
            summary = "One player significantly outclassed the other; early advantage led to a swift, uncompetitive victory."
        else:
            verdict = "💤 D-Tier Match: Low-Skill / Inactive Casual Game"
            badge = "D-Tier Low Skill"
            summary = "Significant idle times, build order mistakes, and minimal tactical micro. Not recommended for competitive highlight casting."

        # Key turning points
        turning_points = []
        if spatial and spatial.hotspots:
            top_spot = spatial.hotspots[0]
            turning_points.append(f"Major combat concentration at {top_spot.description} ({top_spot.intensity} attack engagements).")
        if spatial and spatial.proxy_events:
            for pe in spatial.proxy_events:
                turning_points.append(f"Aggressive forward proxy placed by {pe['player']} at {pe['time_sec']:.0f}s ({pe['distance_from_base']:.0f} units from base).")

        return MatchQualityScorecard(
            caster_score=caster_score,
            verdict=verdict,
            verdict_badge=badge,
            skill_balance_score=balance_pts,
            combat_intensity_score=combat_pts,
            meta_adherence_score=meta_pts,
            pacing_and_drama_score=pacing_pts,
            is_high_level_game=is_high_level,
            summary=summary,
            key_turning_points=turning_points
        )
