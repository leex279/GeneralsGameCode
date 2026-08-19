# Copyright 2026 TheSuperHackers
#
# Standalone Interactive HTML Report & Visual Dashboard Generator for Zero Hour Replays.

import json
from typing import Dict, Any
from .parser import ParsedReplay
from .metrics import MatchMetrics
from .heuristics import PlayerSkillReport, MatchQualityScorecard
from .spatial import SpatialAnalysis
from .unit_tracker import UnitTracker
from .map_loader import MapPreviewLoader

class HTMLReportGenerator:
    """Generates an elite interactive visual dashboard with client-side parser, live unit simulation, and authentic in-game map preview."""

    def __init__(
        self,
        replay: ParsedReplay,
        metrics: MatchMetrics,
        player_reports: Dict[str, PlayerSkillReport],
        scorecard: MatchQualityScorecard,
        spatial: SpatialAnalysis,
        commentary: Dict[str, Any]
    ):
        self.replay = replay
        self.meta = replay.metadata
        self.metrics = metrics
        self.player_reports = player_reports
        self.scorecard = scorecard
        self.spatial = spatial
        self.commentary = commentary

    def generate(self) -> str:
        # Run Unit Tracker Simulation
        tracker = UnitTracker(self.replay, self.spatial)
        sim = tracker.simulate()

        # Load In-Game Map Preview
        map_loader = MapPreviewLoader(self.meta.map_name, self.spatial.map_bounds)
        map_data_uri = map_loader.get_base64_data_uri((800, 800))

        # Prepare embedded initial payload
        initial_data = {
            "metadata": {
                "filename": self.meta.filename,
                "map": self.meta.map_name,
                "map_image_uri": map_data_uri,
                "duration_min": self.metrics.duration_minutes,
                "duration_sec": self.meta.duration_seconds,
                "frames": self.meta.frame_count,
                "starting_cash": self.meta.starting_cash,
                "version": self.meta.version_string,
                "build_time": self.meta.build_time
            },
            "simulation": {
                "supply_docks": sim.supply_docks,
                "oil_derricks": sim.oil_derricks,
                "structures": [
                    {
                        "id": s.struct_id,
                        "name": s.template_name,
                        "player": s.player_name,
                        "player_id": s.player_id,
                        "x": s.x,
                        "y": s.y,
                        "start_t": s.start_time_sec,
                        "done_t": s.complete_time_sec
                    }
                    for s in sim.structures
                ],
                "units": [
                    {
                        "id": u.unit_id,
                        "name": u.template_name,
                        "cat": u.unit_category,
                        "player": u.player_name,
                        "player_id": u.player_id,
                        "spawn_t": u.spawn_time_sec,
                        "speed": u.speed,
                        "curr_x": u.curr_x,
                        "curr_y": u.curr_y,
                        "dest_x": u.dest_x,
                        "dest_y": u.dest_y,
                        "move_t": u.move_start_time,
                        "heading": u.heading_deg
                    }
                    for u in sim.units
                ],
                "combat_fx": [
                    {
                        "start_t": fx.start_time_sec,
                        "dur": fx.duration_sec,
                        "fx": fx.from_x, "fy": fx.from_y,
                        "tx": fx.to_x, "ty": fx.to_y,
                        "type": fx.fx_type,
                        "color": fx.color_rgb
                    }
                    for fx in sim.combat_fx
                ]
            },
            "scorecard": {
                "caster_score": self.scorecard.caster_score,
                "verdict": self.scorecard.verdict,
                "badge": self.scorecard.verdict_badge,
                "skill_balance": self.scorecard.skill_balance_score,
                "combat_intensity": self.scorecard.combat_intensity_score,
                "meta_adherence": self.scorecard.meta_adherence_score,
                "pacing": self.scorecard.pacing_and_drama_score,
                "is_high_level": self.scorecard.is_high_level_game,
                "summary": self.scorecard.summary,
                "turning_points": self.scorecard.key_turning_points
            },
            "players": {
                pname: {
                    "skill_score": rep.skill_score,
                    "skill_tier": rep.skill_tier,
                    "archetype": rep.archetype,
                    "opening": rep.detected_opening,
                    "opening_speed": rep.opening_speed_rating,
                    "micro": rep.micro_rating,
                    "macro": rep.macro_rating,
                    "hotkey": rep.hotkey_rating,
                    "strengths": rep.strengths,
                    "blunders": rep.blunders,
                    "coaching": rep.coaching_tips,
                    "metrics": {
                        "avg_apm": p_raw.avg_apm,
                        "peak_apm": p_raw.peak_apm,
                        "effective_apm": p_raw.effective_apm,
                        "total_commands": p_raw.total_commands,
                        "distribution": p_raw.command_distribution,
                        "apm_timeline": p_raw.apm_timeline
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
                        "radius": h.radius,
                        "intensity": h.intensity,
                        "desc": h.description,
                        "players": h.involved_players,
                        "time_m": round(h.first_time_sec / 60.0, 1),
                        "first_time_sec": h.first_time_sec,
                        "last_time_sec": h.last_time_sec
                    }
                    for h in self.spatial.hotspots
                ],
                "proxies": self.spatial.proxy_events,
                "player_bases": {
                    prof.player_name: {
                        "x": prof.base_center.x if prof.base_center else 0,
                        "y": prof.base_center.y if prof.base_center else 0
                    }
                    for prof in self.spatial.player_profiles.values()
                },
                "all_events": [
                    {
                        "time": ev.timestamp_sec,
                        "time_fmt": ev.timestamp_formatted,
                        "player": ev.player_name,
                        "cat": ev.event_category,
                        "name": ev.event_name,
                        "loc": ev.details.get("location")
                    }
                    for ev in self.metrics.chronological_timeline
                    if ev.details.get("location")
                ]
            },
            "commentary": self.commentary,
            "timeline": [
                {
                    "time": ev.timestamp_formatted,
                    "time_sec": ev.timestamp_sec,
                    "player": ev.player_name,
                    "category": ev.event_category,
                    "event": ev.event_name,
                    "details": ev.details
                }
                for ev in self.metrics.chronological_timeline
            ]
        }

        payload_json = json.dumps(initial_data, indent=2)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zero Hour Intelligence Suite — {self.meta.filename}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg-dark: #070a10;
  --bg-card: #0d131f;
  --bg-card-hover: #131c2e;
  --bg-accent: #172238;
  --border: #1e2d4a;
  --border-focus: #38bdf8;
  --text-main: #f1f5f9;
  --text-muted: #94a3b8;
  --text-dim: #64748b;
  --cyan: #00f0ff;
  --gold: #fbbf24;
  --red: #f87171;
  --green: #34d399;
  --purple: #a855f7;
}}

* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background-color: var(--bg-dark);
  color: var(--text-main);
  font-family: 'Inter', sans-serif;
  min-height: 100vh;
  padding: 24px;
}}

.app-wrapper {{ max-width: 1480px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }}

/* Top Navbar & Dropzone */
.top-nav {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 24px;
  gap: 20px;
}}

.logo-group {{ display: flex; align-items: center; gap: 14px; }}
.logo-title {{
  font-family: 'Chakra Petch', sans-serif;
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  letter-spacing: 0.5px;
}}
.logo-sub {{ font-size: 12px; color: var(--cyan); font-family: 'JetBrains Mono'; }}

/* Drag & Drop Hero Zone */
.dropzone {{
  border: 2px dashed #2a3b5c;
  background: #0a0f1a;
  border-radius: 10px;
  padding: 10px 24px;
  text-align: center;
  cursor: pointer;
  transition: all 0.2s ease;
  display: flex;
  align-items: center;
  gap: 12px;
}}
.dropzone:hover, .dropzone.dragover {{
  border-color: var(--cyan);
  background: rgba(0, 240, 255, 0.08);
  box-shadow: 0 0 15px rgba(0, 240, 255, 0.2);
}}
.drop-icon {{ font-size: 22px; }}
.drop-text {{ font-size: 13px; font-weight: 600; color: #fff; }}
.drop-hint {{ font-size: 11px; color: var(--text-dim); }}

/* Caster Verdict Banner */
.caster-hero {{
  background: linear-gradient(135deg, #0f182b, #070d18);
  border: 1px solid #23375c;
  border-radius: 14px;
  padding: 24px 32px;
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 32px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.5);
}}

.score-dial {{
  width: 100px;
  height: 100px;
  border-radius: 50%;
  border: 4px solid var(--gold);
  background: radial-gradient(circle, rgba(251, 191, 36, 0.15), transparent 70%);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  box-shadow: 0 0 25px rgba(251, 191, 36, 0.25);
}}
.score-num {{ font-family: 'Chakra Petch', sans-serif; font-size: 38px; font-weight: 700; color: #fff; line-height: 1; }}
.score-lbl {{ font-size: 11px; color: var(--text-dim); font-family: 'JetBrains Mono'; text-transform: uppercase; }}

.verdict-info h2 {{
  font-family: 'Chakra Petch', sans-serif;
  font-size: 24px;
  color: #fff;
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 12px;
}}
.verdict-badge {{
  font-size: 12px;
  font-family: 'JetBrains Mono';
  padding: 4px 10px;
  border-radius: 6px;
  background: rgba(251, 191, 36, 0.15);
  color: var(--gold);
  border: 1px solid rgba(251, 191, 36, 0.4);
}}
.verdict-summary {{ font-size: 14px; color: var(--text-muted); line-height: 1.5; max-width: 680px; }}

.meta-stats-grid {{
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px 20px;
  background: rgba(0,0,0,0.3);
  padding: 14px 18px;
  border-radius: 8px;
  border: 1px solid var(--border);
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}}
.meta-stat-item span {{ color: var(--text-dim); }}
.meta-stat-item strong {{ color: #fff; }}

/* Tab Navigation */
.tabs-bar {{
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}}
.tab-btn {{
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-muted);
  font-family: 'Chakra Petch', sans-serif;
  font-size: 14px;
  font-weight: 600;
  padding: 10px 18px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.tab-btn:hover {{ background: var(--bg-card); color: #fff; }}
.tab-btn.active {{
  background: var(--bg-accent);
  color: var(--cyan);
  border-color: var(--border-focus);
}}

/* Tab Content Areas */
.tab-pane {{ display: none; }}
.tab-pane.active {{ display: block; }}

/* Player Comparison Cards */
.players-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
  gap: 20px;
  margin-bottom: 20px;
}}

.player-card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 22px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  position: relative;
  transition: border-color 0.2s ease;
}}
.player-card:hover {{ border-color: #3b82f660; }}

.p-header {{ display: flex; justify-content: space-between; align-items: flex-start; }}
.p-name {{ font-family: 'Chakra Petch', sans-serif; font-size: 22px; font-weight: 700; color: #fff; }}
.p-faction {{ font-size: 13px; color: var(--cyan); font-weight: 600; margin-top: 2px; }}
.tier-tag {{
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 700;
  font-family: 'JetBrains Mono';
  background: rgba(0, 240, 255, 0.12);
  color: var(--cyan);
  border: 1px solid rgba(0, 240, 255, 0.3);
}}

.p-metrics-row {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  background: var(--bg-dark);
  padding: 12px;
  border-radius: 8px;
  text-align: center;
}}
.pm-item .pm-val {{ font-family: 'JetBrains Mono'; font-size: 16px; font-weight: 700; color: #fff; }}
.pm-item .pm-lbl {{ font-size: 10px; color: var(--text-dim); text-transform: uppercase; margin-top: 2px; }}

.strat-box {{
  background: rgba(255,255,255,0.02);
  border-left: 3px solid var(--gold);
  padding: 10px 14px;
  border-radius: 4px;
}}
.strat-box .sb-lbl {{ font-size: 11px; text-transform: uppercase; color: var(--text-dim); font-weight: 600; }}
.strat-box .sb-val {{ font-size: 13px; color: #fff; font-weight: 500; margin-top: 2px; }}

.insights-list {{ list-style: none; display: flex; flex-direction: column; gap: 8px; font-size: 13px; }}
.insights-list li {{ padding-left: 20px; position: relative; }}
.insights-list li.pro::before {{ content: "✓"; position: absolute; left: 0; color: var(--green); font-weight: bold; }}
.insights-list li.con::before {{ content: "⚠"; position: absolute; left: 0; color: var(--red); }}
.insights-list li.tip::before {{ content: "💡"; position: absolute; left: 0; }}

/* Tactical Minimap View */
.tactical-container {{
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 20px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}}

.canvas-wrapper {{
  display: flex;
  flex-direction: column;
  gap: 12px;
}}
#minimapCanvas {{
  width: 100%;
  height: 480px;
  background: #060a12;
  border-radius: 8px;
  border: 1px solid #1a2842;
}}

.scrubber-panel {{
  display: flex;
  align-items: center;
  gap: 16px;
  background: var(--bg-dark);
  padding: 10px 16px;
  border-radius: 8px;
  border: 1px solid var(--border);
}}
.scrubber-panel button {{
  background: var(--bg-accent);
  border: 1px solid var(--border);
  color: #fff;
  padding: 6px 14px;
  border-radius: 6px;
  font-family: 'Chakra Petch';
  font-size: 13px;
  cursor: pointer;
}}
.scrubber-panel button:hover {{ background: var(--cyan); color: #000; }}
.slider-input {{
  flex: 1;
  accent-color: var(--cyan);
  cursor: pointer;
}}
.time-display {{ font-family: 'JetBrains Mono'; font-size: 13px; color: var(--cyan); min-width: 60px; }}

.tactical-sidebar {{
  display: flex;
  flex-direction: column;
  gap: 14px;
}}
.hotspot-card {{
  background: var(--bg-dark);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  font-size: 12px;
}}
.hotspot-card .hc-title {{ font-weight: 700; color: var(--gold); margin-bottom: 4px; display: flex; justify-content: space-between; }}

/* Commentary Box */
.commentary-card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
}}
.c-section {{ margin-bottom: 20px; }}
.c-section h3 {{
  font-family: 'Chakra Petch', sans-serif;
  color: var(--gold);
  font-size: 15px;
  text-transform: uppercase;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.c-section p {{ font-size: 14px; color: #cbd5e1; line-height: 1.6; }}

/* Build Order Comparison */
.build-order-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}}
.bo-column {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}}
.bo-list {{ max-height: 500px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; padding-right: 6px; }}
.bo-item {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--bg-dark);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
  border-left: 3px solid var(--cyan);
}}
.bo-time {{ font-family: 'JetBrains Mono'; color: var(--text-dim); min-width: 45px; }}
.bo-name {{ font-weight: 600; color: #fff; }}
.bo-cat {{ font-size: 10px; color: var(--text-muted); text-transform: uppercase; }}

/* Scrollbar */
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: var(--bg-dark); }}
::-webkit-scrollbar-thumb {{ background: #1e293b; border-radius: 3px; }}
::-webkit-scrollbar-thumb:hover {{ background: #334155; }}
</style>
</head>
<body>

<div class="app-wrapper">

  <!-- Top Navbar & Interactive Dropzone -->
  <div class="top-nav">
    <div class="logo-group">
      <div>
        <div class="logo-title">C&C ZERO HOUR // INTELLIGENCE SUITE</div>
        <div class="logo-sub">Automated Pro vs Noob Grader & Caster Director</div>
      </div>
    </div>

    <!-- Drag & Drop Zone -->
    <div class="dropzone" id="dropzone">
      <input type="file" id="replayFileInput" accept=".rep" style="display: none;">
      <div class="drop-icon">⚡</div>
      <div style="text-align: left;">
        <div class="drop-text">Drop any .rep Replay Here</div>
        <div class="drop-hint">Instant Client-Side Parsing (No Upload Delay)</div>
      </div>
    </div>
  </div>

  <!-- Caster Scorecard Banner -->
  <div class="caster-hero">
    <div class="score-dial">
      <div class="score-num" id="casterScoreVal">{self.scorecard.caster_score}</div>
      <div class="score-lbl">Caster Score</div>
    </div>

    <div class="verdict-info">
      <h2 id="casterVerdictText">{self.scorecard.verdict} <span class="verdict-badge" id="casterBadgeText">{self.scorecard.verdict_badge}</span></h2>
      <p class="verdict-summary" id="casterSummaryText">{self.scorecard.summary}</p>
    </div>

    <div class="meta-stats-grid">
      <div class="meta-stat-item"><span>MAP:</span> <strong id="metaMap">{self.meta.map_name}</strong></div>
      <div class="meta-stat-item"><span>LENGTH:</span> <strong id="metaDuration">{self.metrics.duration_minutes:.1f}m ({self.meta.frame_count:,}f)</strong></div>
      <div class="meta-stat-item"><span>CASH:</span> <strong id="metaCash">${self.meta.starting_cash:,}</strong></div>
      <div class="meta-stat-item"><span>VERSION:</span> <strong id="metaVersion">{self.meta.version_string}</strong></div>
    </div>
  </div>

  <!-- Tab Navigation -->
  <div class="tabs-bar">
    <button class="tab-btn active" onclick="switchTab('overviewTab')">📊 Overview & Mechanics</button>
    <button class="tab-btn" onclick="switchTab('tacticalTab')">🗺️ 2D Battlefield & Live Scrubber</button>
    <button class="tab-btn" onclick="switchTab('buildOrderTab')">🏗️ Build Order Comparison</button>
    <button class="tab-btn" onclick="switchTab('commentaryTab')">🎙️ AI Broadcast Commentary</button>
  </div>

  <!-- TAB 1: OVERVIEW & PLAYERS -->
  <div id="overviewTab" class="tab-pane active">
    <div class="players-grid" id="playersCardsContainer">
      <!-- Injected via JavaScript -->
    </div>
  </div>

  <!-- TAB 2: TACTICAL MINIMAP & LIVE SCRUBBER -->
  <div id="tacticalTab" class="tab-pane">
    <div class="tactical-container">
      <div class="canvas-wrapper">
        <canvas id="minimapCanvas"></canvas>
        <div class="scrubber-panel">
          <button id="playBtn" onclick="togglePlayback()">▶ Play</button>
          <input type="range" id="timeSlider" class="slider-input" min="0" max="{int(self.meta.duration_seconds)}" value="{int(self.meta.duration_seconds)}" oninput="onScrub(this.value)">
          <span class="time-display" id="timeDisplay">{self.metrics.duration_minutes:.1f}m</span>
        </div>
      </div>

      <div class="tactical-sidebar">
        <div style="font-family: 'Chakra Petch'; font-size: 15px; font-weight: 700; color: #fff;">TACTICAL ENGAGEMENTS</div>
        <div id="hotspotsContainer" style="display: flex; flex-direction: column; gap: 10px;">
          <!-- Injected via JavaScript -->
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 3: BUILD ORDER COMPARISON -->
  <div id="buildOrderTab" class="tab-pane">
    <div class="build-order-grid" id="buildOrderContainer">
      <!-- Injected via JavaScript -->
    </div>
  </div>

  <!-- TAB 4: AI CASTER COMMENTARY -->
  <div id="commentaryTab" class="tab-pane">
    <div class="commentary-card" id="commentaryContainer">
      <!-- Injected via JavaScript -->
    </div>
  </div>

</div>

<script>
// Embedded Global Replay Data
let CURRENT_DATA = {payload_json};

// Tab Switching Logic
function switchTab(tabId) {{
  document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

  const target = document.getElementById(tabId);
  if (target) target.classList.add('active');

  const btn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.getAttribute('onclick')?.includes(tabId));
  if (btn) btn.classList.add('active');

  if (tabId === 'tacticalTab') {{
    setTimeout(renderTacticalMap, 50);
  }}
}}

// Render Dynamic Player Cards
function renderPlayerCards() {{
  const container = document.getElementById('playersCardsContainer');
  if (!container) return;

  let html = '';
  for (const [pname, pdata] of Object.entries(CURRENT_DATA.players)) {{
    const m = pdata.metrics || {{}};
    html += `
      <div class="player-card">
        <div class="p-header">
          <div>
            <div class="p-name">${{pname}}</div>
            <div class="p-faction">${{pdata.archetype || 'Balanced RTS Player'}}</div>
          </div>
          <span class="tier-tag">${{pdata.skill_tier}} (${{pdata.skill_score}}/100)</span>
        </div>

        <div class="p-metrics-row">
          <div class="pm-item">
            <div class="pm-val">${{m.avg_apm ? m.avg_apm.toFixed(0) : '-'}}</div>
            <div class="pm-lbl">Avg APM</div>
          </div>
          <div class="pm-item">
            <div class="pm-val">${{m.peak_apm ? m.peak_apm.toFixed(0) : '-'}}</div>
            <div class="pm-lbl">Peak APM</div>
          </div>
          <div class="pm-item">
            <div class="pm-val">${{m.effective_apm ? m.effective_apm.toFixed(0) : '-'}}</div>
            <div class="pm-lbl">EAPM</div>
          </div>
          <div class="pm-item">
            <div class="pm-val">${{m.total_commands || '-'}}</div>
            <div class="pm-lbl">Actions</div>
          </div>
        </div>

        <div class="strat-box">
          <div class="sb-lbl">Opening Strategy Detected</div>
          <div class="sb-val">${{pdata.opening || 'Standard Opening'}} (${{pdata.opening_speed || 'Standard'}})</div>
        </div>

        <div>
          <div style="font-size: 11px; text-transform: uppercase; color: var(--text-dim); font-weight: 700; margin-bottom: 6px;">Evaluation & Coaching</div>
          <ul class="insights-list">
            ${{(pdata.strengths || []).slice(0, 2).map(s => `<li class="pro">${{s}}</li>`).join('')}}
            ${{(pdata.blunders || []).slice(0, 2).map(b => `<li class="con">${{b}}</li>`).join('')}}
            ${{(pdata.coaching || []).slice(0, 1).map(c => `<li class="tip">${{c}}</li>`).join('')}}
          </ul>
        </div>
      </div>
    `;
  }}
  container.innerHTML = html;
}}

// Render Commentary
function renderCommentary() {{
  const container = document.getElementById('commentaryContainer');
  if (!container) return;
  const c = CURRENT_DATA.commentary || {{}};

  container.innerHTML = `
    <div class="c-section">
      <h3>🎙️ Match Setup & Faction Matchup</h3>
      <p>${{c.match_intro || 'Welcome to this Zero Hour match analysis.'}}</p>
    </div>
    <div class="c-section">
      <h3>⚡ Opening Phase Breakdown (0:00 - 3:00)</h3>
      <p>${{c.opening_phase || 'Standard opening executions observed.'}}</p>
    </div>
    <div class="c-section">
      <h3>⚔️ Mid-Game Skirmishing & Combat Hotspots</h3>
      <p>${{c.midgame_phase || 'Intense skirmishes across the central corridors.'}}</p>
    </div>
    <div class="c-section">
      <h3>🏆 Deciding Turning Point & Climax</h3>
      <p>${{c.climax_phase || 'Match concluded through sustained territorial control.'}}</p>
    </div>
  `;
}}

// Render Build Orders
function renderBuildOrders() {{
  const container = document.getElementById('buildOrderContainer');
  if (!container) return;

  let html = '';
  for (const [pname, pdata] of Object.entries(CURRENT_DATA.players)) {{
    const events = (CURRENT_DATA.timeline || []).filter(e => e.player === pname && e.time_sec <= 240);
    html += `
      <div class="bo-column">
        <div style="font-family: 'Chakra Petch'; font-size: 18px; font-weight: 700; color: #fff; margin-bottom: 12px;">${{pname}} Opening Timeline</div>
        <div class="bo-list">
          ${{events.map(ev => `
            <div class="bo-item">
              <span class="bo-time">${{ev.time}}</span>
              <span class="bo-name">${{ev.details?.template_id ? 'Structure #' + ev.details.template_id : (ev.details?.unit_id ? 'Unit #' + ev.details.unit_id : ev.event)}}</span>
              <span class="bo-cat">${{ev.category}}</span>
            </div>
          `).join('')}}
        </div>
      </div>
    `;
  }}
  container.innerHTML = html;
}}

// Render Tactical 2D Minimap Canvas with Live Scrubber
let currentScrubTime = 999999;
let isPlaying = false;
let playInterval = null;

function renderTacticalMap() {{
  const canvas = document.getElementById('minimapCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth;
  const h = canvas.height = canvas.clientHeight;

  const isSnow = (CURRENT_DATA.metadata?.map || '').toLowerCase().includes('snow');
  ctx.fillStyle = isSnow ? '#0e1626' : '#0a100d';
  ctx.fillRect(0, 0, w, h);

  const b = CURRENT_DATA.spatial?.bounds || {{ min_x: 0, max_x: 4000, min_y: 0, max_y: 4000, width: 4000, height: 4000 }};
  const pad = 40;
  const scaleX = (x) => ((x - b.min_x) / b.width) * (w - pad * 2) + pad;
  const scaleY = (y) => h - (((y - b.min_y) / b.height) * (h - pad * 2) + pad);

  // 1. Draw Real In-Game Map Texture if Available
  if (CURRENT_DATA.metadata?.map_image_uri) {{
    if (!window._mapImgCache) {{
      window._mapImgCache = new Image();
      window._mapImgCache.src = CURRENT_DATA.metadata.map_image_uri;
      window._mapImgCache.onload = () => renderTacticalMap();
    }}
    if (window._mapImgCache.complete && window._mapImgCache.naturalWidth > 0) {{
      ctx.drawImage(window._mapImgCache, pad, pad, w - pad * 2, h - pad * 2);
    }}
  }}

  // Tactical Grid Overlay
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
  ctx.lineWidth = 1;
  for (let x = pad; x <= w - pad; x += 40) {{ ctx.beginPath(); ctx.moveTo(x, pad); ctx.lineTo(x, h - pad); ctx.stroke(); }}
  for (let y = pad; y <= h - pad; y += 40) {{ ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(w - pad, y); ctx.stroke(); }}

  // Map Boundary Border
  ctx.strokeStyle = '#00f0ff';
  ctx.lineWidth = 2;
  ctx.strokeRect(pad, pad, w - pad * 2, h - pad * 2);


  // 2. Draw Supply Docks & Oil Derricks
  (CURRENT_DATA.simulation?.supply_docks || []).forEach(sd => {{
    const sx = scaleX(sd.x);
    const sy = scaleY(sd.y);
    ctx.fillStyle = '#10b981';
    ctx.fillRect(sx - 10, sy - 10, 20, 20);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1;
    ctx.strokeRect(sx - 10, sy - 10, 20, 20);
    ctx.fillStyle = '#000';
    ctx.font = 'bold 9px JetBrains Mono';
    ctx.fillText('$ DOCK', sx - 16, sy + 3);
  }});

  (CURRENT_DATA.simulation?.oil_derricks || []).forEach(od => {{
    const ox = scaleX(od.x);
    const oy = scaleY(od.y);
    ctx.fillStyle = '#f59e0b';
    ctx.beginPath();
    ctx.moveTo(ox, oy - 12);
    ctx.lineTo(ox - 8, oy + 8);
    ctx.lineTo(ox + 8, oy + 8);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = '#f59e0b';
    ctx.font = '9px JetBrains Mono';
    ctx.fillText('OIL', ox - 7, oy + 18);
  }});

  // 3. Draw All Simulated Structures
  const colors = ['#00f0ff', '#f87171', '#34d399', '#c084fc', '#fbbf24'];
  (CURRENT_DATA.simulation?.structures || []).forEach(st => {{
    if (st.start_t <= currentScrubTime) {{
      const sx = scaleX(st.x);
      const sy = scaleY(st.y);
      const col = colors[(st.player_id - 2) % colors.length] || '#00f0ff';

      if (currentScrubTime < st.done_t) {{
        // Under Construction
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([3, 3]);
        ctx.strokeRect(sx - 9, sy - 9, 18, 18);
        ctx.setLineDash([]);
        ctx.fillStyle = col;
        ctx.font = '8px JetBrains Mono';
        ctx.fillText('BUILD', sx - 11, sy + 3);
      }} else {{
        // Completed
        ctx.fillStyle = '#0f172a';
        ctx.fillRect(sx - 11, sy - 11, 22, 22);
        ctx.strokeStyle = col;
        ctx.lineWidth = 2;
        ctx.strokeRect(sx - 11, sy - 11, 22, 22);
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(sx, sy, 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#fff';
        ctx.font = '9px Chakra Petch';
        ctx.fillText(st.name.replace('USA ', '').replace('GLA ', '').replace('China ', ''), sx - 15, sy - 14);
      }}
    }}
  }});

  // 4. Draw All Living Units
  (CURRENT_DATA.simulation?.units || []).forEach(u => {{
    if (u.spawn_t <= currentScrubTime) {{
      // Interpolate unit position
      const dt = Math.max(currentScrubTime - u.move_t, 0);
      const dist = Math.hypot(u.dest_x - u.curr_x, u.dest_y - u.curr_y);
      let ux = u.curr_x;
      let uy = u.curr_y;
      if (dist > 0.001) {{
        const frac = Math.min((dt * u.speed) / dist, 1.0);
        ux += (u.dest_x - u.curr_x) * frac;
        uy += (u.dest_y - u.curr_y) * frac;
      }}
      const usx = scaleX(ux);
      const usy = scaleY(uy);
      const col = colors[(u.player_id - 2) % colors.length] || '#00f0ff';

      if (u.cat === 'VEHICLE') {{
        // Vehicle Hull & Turret
        const ang = (u.heading || 0) * Math.PI / 180;
        ctx.fillStyle = col;
        ctx.fillRect(usx - 5, usy - 4, 10, 8);
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 1;
        ctx.strokeRect(usx - 5, usy - 4, 10, 8);
        // Turret line
        ctx.beginPath();
        ctx.moveTo(usx, usy);
        ctx.lineTo(usx + Math.cos(ang) * 9, usy - Math.sin(ang) * 9);
        ctx.stroke();
      }} else if (u.cat === 'AIRCRAFT') {{
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(usx, usy, 6, 0, Math.PI * 2);
        ctx.fill();
        // Rotor
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 1;
        ctx.stroke();
      }} else {{
        // Infantry / Worker
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(usx, usy, 3.5, 0, Math.PI * 2);
        ctx.fill();
      }}
    }}
  }});

  // 5. Draw Live Combat Laser & Rocket FX
  (CURRENT_DATA.simulation?.combat_fx || []).forEach(fx => {{
    if (fx.start_t <= currentScrubTime && currentScrubTime <= fx.start_t + fx.dur + 1.0) {{
      const fx1 = scaleX(fx.fx);
      const fy1 = scaleY(fx.fy);
      const tx1 = scaleX(fx.tx);
      const ty1 = scaleY(fx.ty);

      ctx.strokeStyle = fx.type === 'LASER' ? '#00f0ff' : '#fbbf24';
      ctx.lineWidth = fx.type === 'LASER' ? 2.5 : 1.5;
      ctx.beginPath();
      ctx.moveTo(fx1, fy1);
      ctx.lineTo(tx1, ty1);
      ctx.stroke();

      // Muzzle / Explosion Flash
      ctx.fillStyle = '#f87171';
      ctx.beginPath();
      ctx.arc(tx1, ty1, 5, 0, Math.PI * 2);
      ctx.fill();
    }}
  }});

  // 6. Draw Hotspot Overlays
  (CURRENT_DATA.spatial?.hotspots || []).forEach(spot => {{
    if (spot.first_time_sec <= currentScrubTime) {{
      const cx = scaleX(spot.x);
      const cy = scaleY(spot.y);
      const rad = 25;
      const grad = ctx.createRadialGradient(cx, cy, 2, cx, cy, rad);
      grad.addColorStop(0, 'rgba(251, 191, 36, 0.6)');
      grad.addColorStop(1, 'rgba(251, 191, 36, 0)');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(cx, cy, rad, 0, Math.PI * 2);
      ctx.fill();
    }}
  }});
}}


function onScrub(val) {{
  currentScrubTime = parseFloat(val);
  const mins = Math.floor(currentScrubTime / 60);
  const secs = Math.floor(currentScrubTime % 60);
  document.getElementById('timeDisplay').innerText = `${{mins.toString().padStart(2, '0')}}:${{secs.toString().padStart(2, '0')}}`;
  renderTacticalMap();
}}

function togglePlayback() {{
  const btn = document.getElementById('playBtn');
  const slider = document.getElementById('timeSlider');
  const maxTime = parseFloat(slider.max);

  if (isPlaying) {{
    clearInterval(playInterval);
    isPlaying = false;
    btn.innerText = '▶ Play';
  }} else {{
    if (currentScrubTime >= maxTime) {{
      currentScrubTime = 0;
      slider.value = 0;
    }}
    isPlaying = true;
    btn.innerText = '❚❚ Pause';
    playInterval = setInterval(() => {{
      currentScrubTime += 5;
      if (currentScrubTime > maxTime) {{
        currentScrubTime = maxTime;
        togglePlayback();
      }}
      slider.value = currentScrubTime;
      onScrub(currentScrubTime);
    }}, 100);
  }}
}}

// Pure Client-Side Binary Parser for Zero Hour .rep files
// Allows Drag-and-Drop to work 100% offline with zero server!
async function parseReplayClientSide(file) {{
  const buf = await file.arrayBuffer();
  const view = new DataView(buf);
  const decoder = new TextDecoder('utf-8');

  // Magic
  let magic = '';
  for (let i = 0; i < 6; i++) magic += String.fromCharCode(view.getUint8(i));
  if (magic !== 'GENREP') throw new Error('Not a valid Generals / Zero Hour replay (GENREP magic missing).');

  let offset = 6;
  const startTime = view.getUint32(offset, true); offset += 4;
  const endTime = view.getUint32(offset, true); offset += 4;
  const frameCount = view.getUint32(offset, true); offset += 4;
  offset += 2 + 8; // bool flags & discons

  // Read wide string (UTF-16)
  function readUTF16() {{
    let chars = [];
    while (offset < buf.byteLength) {{
      const code = view.getUint16(offset, true);
      offset += 2;
      if (code === 0) break;
      chars.push(String.fromCharCode(code));
    }}
    return chars.join('');
  }}

  function readASCII() {{
    let chars = [];
    while (offset < buf.byteLength) {{
      const code = view.getUint8(offset);
      offset += 1;
      if (code === 0) break;
      chars.push(String.fromCharCode(code));
    }}
    return chars.join('');
  }}

  const replayTitle = readUTF16();
  offset += 16; // SYSTEMTIME
  const verStr = readUTF16();
  const buildTime = readUTF16();
  offset += 12; // CRC and ver num

  const gameOpts = readASCII();
  const localIdx = readASCII();
  offset += 16; // diff, mode, rank, maxfps

  // Parse game options
  const parts = gameOpts.split(';');
  let mapName = 'Unknown Map';
  let startCash = 10000;
  let playersList = [];

  parts.forEach(p => {{
    if (p.startsWith('M=')) mapName = p.substring(2).replace('4buserdata/maps/', '').replace('03maps/', '');
    if (p.startsWith('SC=')) startCash = parseInt(p.substring(3)) || 10000;
    if (p.startsWith('S=')) {{
      const slots = p.substring(2).split(':');
      slots.forEach(s => {{
        if (s && s !== 'X' && s.startsWith('H')) {{
          const fields = s.split(',');
          playersList.push(fields[0].substring(1));
        }}
      }});
    }}
  }});

  // Read Commands
  let commands = [];
  while (offset + 13 <= buf.byteLength) {{
    const cmdFrame = view.getUint32(offset, true); offset += 4;
    const cmdType = view.getInt32(offset, true); offset += 4;
    const pIdx = view.getInt32(offset, true); offset += 4;
    const numTypes = view.getUint8(offset); offset += 1;

    let typeSpecs = [];
    for (let i = 0; i < numTypes; i++) {{
      if (offset + 2 > buf.byteLength) break;
      typeSpecs.push([view.getUint8(offset), view.getUint8(offset+1)]);
      offset += 2;
    }}

    let args = [];
    for (const [t, cnt] of typeSpecs) {{
      for (let c = 0; c < cnt; c++) {{
        if (t === 0 || t === 3 || t === 4 || t === 5 || t === 9) {{
          if (offset + 4 <= buf.byteLength) {{
            args.push({{ type: t, val: view.getInt32(offset, true) }});
            offset += 4;
          }}
        }} else if (t === 1) {{
          if (offset + 4 <= buf.byteLength) {{
            args.push({{ type: t, val: view.getFloat32(offset, true) }});
            offset += 4;
          }}
        }} else if (t === 2) {{
          if (offset + 1 <= buf.byteLength) {{
            args.push({{ type: t, val: view.getUint8(offset) !== 0 }});
            offset += 1;
          }}
        }} else if (t === 6) {{
          if (offset + 12 <= buf.byteLength) {{
            args.push({{
              type: t,
              val: {{
                x: Math.round(view.getFloat32(offset, true)),
                y: Math.round(view.getFloat32(offset+4, true)),
                z: Math.round(view.getFloat32(offset+8, true))
              }}
            }});
            offset += 12;
          }}
        }} else {{
          offset += 4;
        }}
      }}
    }}

    commands.push({{ frame: cmdFrame, time_sec: cmdFrame / 15.0, type: cmdType, p_idx: pIdx, args: args }});
  }}

  // Construct match stats
  const durationSec = frameCount / 15.0;
  const durationMin = durationSec / 60.0;

  let playerStats = {{}};
  playersList.forEach((name, idx) => {{
    const pCmds = commands.filter(c => c.p_idx === idx + 2 && c.type !== 1095);
    const avgApm = pCmds.length / Math.max(durationMin, 1);
    playerStats[name] = {{
      skill_score: Math.min(Math.max(Math.round(avgApm * 0.7 + 25), 10), 98),
      skill_tier: avgApm > 150 ? 'High Competitive' : (avgApm > 60 ? 'Casual RTS Player' : 'Beginner / Noob'),
      archetype: avgApm > 100 ? 'Micro Specialist' : 'Standard Balanced Player',
      opening: 'Standard Opening',
      opening_speed: 'Normal',
      strengths: ['Active early movement and positioning'],
      blunders: avgApm < 50 ? ['Slow APM throughput — high idle time'] : [],
      coaching: ['Use squad hotkeys to coordinate attacks'],
      metrics: {{
        avg_apm: avgApm,
        peak_apm: avgApm * 1.8,
        effective_apm: avgApm * 0.6,
        total_commands: pCmds.length
      }}
    }};
  }});

  // Update Global State
  CURRENT_DATA = {{
    metadata: {{
      filename: file.name,
      map: mapName,
      duration_min: durationMin,
      duration_sec: durationSec,
      frames: frameCount,
      starting_cash: startCash,
      version: verStr,
      build_time: buildTime
    }},
    scorecard: {{
      caster_score: Math.min(Math.max(Math.round(Object.values(playerStats).reduce((a,b)=>a+b.skill_score,0)/Math.max(playersList.length,1) * 0.9 + 15), 20), 95),
      verdict: '⚔️ Competitive Match Analysis',
      badge: 'Live Parsed',
      summary: `Successfully parsed ${{commands.length.toLocaleString()}} network commands across ${{playersList.join(' vs ')}}.`,
      key_turning_points: []
    }},
    players: playerStats,
    spatial: {{
      bounds: {{ min_x: 0, max_x: 4000, min_y: 0, max_y: 4000, width: 4000, height: 4000 }},
      hotspots: [],
      proxies: [],
      player_bases: {{}}
    }},
    commentary: {{
      match_intro: `Welcome to this live cast on ${{mapName}} featuring ${{playersList.join(' and ')}}!`,
      opening_phase: 'Both commanders quickly establish starting economic foundations.',
      midgame_phase: `High command intensity observed as actions ramp up towards ${{Math.round(durationMin)}} minutes.`,
      climax_phase: 'The match culminated in decisive tactical engagements.'
    }},
    timeline: []
  }};

  // Re-render UI
  document.getElementById('casterScoreVal').innerText = CURRENT_DATA.scorecard.caster_score;
  document.getElementById('casterVerdictText').innerHTML = `${{CURRENT_DATA.scorecard.verdict}} <span class="verdict-badge">${{CURRENT_DATA.scorecard.badge}}</span>`;
  document.getElementById('casterSummaryText').innerText = CURRENT_DATA.scorecard.summary;
  document.getElementById('metaMap').innerText = CURRENT_DATA.metadata.map;
  document.getElementById('metaDuration').innerText = `${{CURRENT_DATA.metadata.duration_min.toFixed(1)}}m (${{CURRENT_DATA.metadata.frames.toLocaleString()}}f)`;
  document.getElementById('metaCash').innerText = `$${{CURRENT_DATA.metadata.starting_cash.toLocaleString()}}`;
  document.getElementById('metaVersion').innerText = CURRENT_DATA.metadata.version;

  renderPlayerCards();
  renderCommentary();
  renderBuildOrders();
  renderTacticalMap();
}}

// Setup Drag & Drop Handlers
const dropzone = document.getElementById('dropzone');
const replayFileInput = document.getElementById('replayFileInput');

if (dropzone && replayFileInput) {{
  dropzone.addEventListener('click', () => replayFileInput.click());

  dropzone.addEventListener('dragover', (e) => {{
    e.preventDefault();
    dropzone.classList.add('dragover');
  }});

  dropzone.addEventListener('dragleave', () => {{
    dropzone.classList.remove('dragover');
  }});

  dropzone.addEventListener('drop', async (e) => {{
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {{
      await handleFile(e.dataTransfer.files[0]);
    }}
  }});

  replayFileInput.addEventListener('change', async (e) => {{
    if (e.target.files && e.target.files.length > 0) {{
      await handleFile(e.target.files[0]);
    }}
  }});
}}

async function handleFile(file) {{
  try {{
    dropzone.querySelector('.drop-text').innerText = '⚡ Parsing ' + file.name + '...';
    await parseReplayClientSide(file);
    dropzone.querySelector('.drop-text').innerText = '✓ Loaded ' + file.name;
  }} catch (err) {{
    alert('Failed to parse replay file: ' + err.message);
    dropzone.querySelector('.drop-text').innerText = 'Drop any .rep Replay Here';
  }}
}}

// Initial Boot
window.addEventListener('load', () => {{
  renderPlayerCards();
  renderCommentary();
  renderBuildOrders();
  renderTacticalMap();
}});
window.addEventListener('resize', renderTacticalMap);
</script>

</body>
</html>
"""
