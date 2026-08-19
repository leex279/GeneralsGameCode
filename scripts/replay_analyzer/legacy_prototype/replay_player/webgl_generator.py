# Copyright 2026 TheSuperHackers
#
# Standalone 3D WebGL / Three.js Replay Player Generator for Zero Hour Replays.
# Creates an offline, self-contained 3D RTS player with procedural 3D models, terrain, and camera director.

import json
from typing import Dict, Any
from .simulator import StandaloneReplaySimulator
from scripts.replay_analyzer.parser import ParsedReplay
from scripts.replay_analyzer.spatial import SpatialAnalyzer
from scripts.replay_analyzer.map_loader import MapPreviewLoader

class WebGLPlayerGenerator:
    """Generates an elite standalone 3D WebGL RTS replay player using Three.js."""

    def __init__(self, replay: ParsedReplay):
        self.replay = replay
        self.meta = replay.metadata
        self.spatial = SpatialAnalyzer(replay).analyze()
        self.sim = StandaloneReplaySimulator(replay, self.spatial)

    def generate_html(self, output_path: str = "replay_3d_player.html") -> str:
        sim_data = self.sim.simulate_all_entities()

        # Load real in-game map preview
        map_loader = MapPreviewLoader(self.meta.map_name, self.spatial.map_bounds)
        sim_data["metadata"]["map_image_uri"] = map_loader.get_base64_data_uri((1024, 1024))

        json_payload = json.dumps(sim_data)

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>C&C Zero Hour 3D Replay Player — {self.meta.filename}</title>
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<!-- Three.js & OrbitControls -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>

<style>
:root {{
  --bg-dark: #070a10;
  --bg-panel: rgba(13, 19, 31, 0.88);
  --border: #1e2d4a;
  --cyan: #00f0ff;
  --gold: #fbbf24;
  --red: #f87171;
  --green: #34d399;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: var(--bg-dark);
  color: #fff;
  font-family: 'Chakra Petch', sans-serif;
  overflow: hidden;
  width: 100vw;
  height: 100vh;
}}

#webgl-canvas {{
  width: 100%;
  height: 100%;
  display: block;
}}

/* Top Status Header */
.top-hud {{
  position: absolute;
  top: 16px;
  left: 20px;
  right: 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none;
}}

.hud-title-card {{
  background: var(--bg-panel);
  border: 1px solid var(--border);
  backdrop-filter: blur(8px);
  padding: 10px 20px;
  border-radius: 8px;
  pointer-events: auto;
}}
.ht-main {{ font-size: 18px; font-weight: 700; color: var(--cyan); letter-spacing: 0.5px; }}
.ht-sub {{ font-size: 12px; color: #94a3b8; font-family: 'JetBrains Mono'; }}

.cam-selector {{
  display: flex;
  gap: 6px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 6px 10px;
  border-radius: 8px;
  pointer-events: auto;
}}
.cam-btn {{
  background: transparent;
  border: 1px solid transparent;
  color: #94a3b8;
  padding: 6px 12px;
  border-radius: 6px;
  font-family: 'Chakra Petch';
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}}
.cam-btn:hover {{ color: #fff; background: rgba(255,255,255,0.05); }}
.cam-btn.active {{
  background: rgba(0, 240, 255, 0.15);
  color: var(--cyan);
  border-color: rgba(0, 240, 255, 0.4);
}}

/* Bottom Playback Control Bar */
.bottom-hud {{
  position: absolute;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  width: min(1000px, 92vw);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  backdrop-filter: blur(10px);
  border-radius: 12px;
  padding: 14px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.6);
}}

.timeline-row {{
  display: flex;
  align-items: center;
  gap: 16px;
}}
.timeline-slider {{
  flex: 1;
  accent-color: var(--cyan);
  cursor: pointer;
  height: 6px;
}}
.time-label {{
  font-family: 'JetBrains Mono';
  font-size: 14px;
  color: var(--cyan);
  min-width: 60px;
}}

.controls-row {{
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.btn-group {{ display: flex; align-items: center; gap: 8px; }}

.ctrl-btn {{
  background: #172338;
  border: 1px solid #2a3e63;
  color: #fff;
  padding: 6px 16px;
  border-radius: 6px;
  font-family: 'Chakra Petch';
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.2s;
}}
.ctrl-btn:hover {{ background: var(--cyan); color: #000; }}

.speed-select {{
  background: #172338;
  border: 1px solid #2a3e63;
  color: var(--cyan);
  padding: 6px 10px;
  border-radius: 6px;
  font-family: 'JetBrains Mono';
  font-size: 12px;
  cursor: pointer;
}}

.info-badge {{
  font-size: 12px;
  font-family: 'JetBrains Mono';
  color: #94a3b8;
}}
.info-badge strong {{ color: #fff; }}
</style>
</head>
<body>

<canvas id="webgl-canvas"></canvas>

<!-- Top Status Header -->
<div class="top-hud">
  <div class="hud-title-card">
    <div class="ht-main">C&C ZERO HOUR // 3D STANDALONE REPLAY PLAYER</div>
    <div class="ht-sub">{self.meta.map_name} | ${self.meta.starting_cash:,} | {self.meta.duration_seconds/60.0:.1f}m</div>
  </div>

  <div class="cam-selector">
    <button class="cam-btn active" id="btnCamDirector" onclick="setCameraMode('DIRECTOR')">🎥 AI Director (DoMiNaToR)</button>
    <button class="cam-btn" id="btnCamOrbit" onclick="setCameraMode('ORBIT')">🕹️ Free 3D Orbit</button>
    <button class="cam-btn" id="btnCamTop" onclick="setCameraMode('TOP')">🗺️ Top-Down Tactical</button>
  </div>
</div>

<!-- Bottom Control Bar -->
<div class="bottom-hud">
  <div class="timeline-row">
    <input type="range" id="timeSlider" class="timeline-slider" min="0" max="{int(self.meta.duration_seconds)}" step="0.5" value="0" oninput="onScrub(this.value)">
    <span class="time-label" id="timeDisplay">00:00</span>
  </div>

  <div class="controls-row">
    <div class="btn-group">
      <button class="ctrl-btn" id="playBtn" onclick="togglePlay()">❚❚ Pause</button>
      <button class="ctrl-btn" onclick="restart()">⏮ Restart</button>
      <select id="speedSelect" class="speed-select" onchange="setPlaybackSpeed(this.value)">
        <option value="0.5">0.5x Speed</option>
        <option value="1.0" selected>1.0x Realtime</option>
        <option value="2.0">2.0x Fast</option>
        <option value="4.0">4.0x Turbo</option>
        <option value="8.0">8.0x Hyper</option>
      </select>
    </div>

    <div class="info-badge">
      ACTIVE UNITS: <strong id="livingUnitsCount">0</strong> | COMBAT CLASHES: <strong id="clashesCount" style="color: var(--gold);">0</strong>
    </div>
  </div>
</div>

<script>
const SIM_DATA = {json_payload};

let scene, camera, renderer, controls;
let currentSimTime = 0.0;
let isPlaying = true;
let playbackSpeed = 1.0;
let cameraMode = 'DIRECTOR'; // 'DIRECTOR', 'ORBIT', 'TOP'

// 3D Object Meshes Cache
const meshPool = new Map(); // entity_id -> THREE.Group
const fxObjects = [];

function init3D() {{
  try {{
    const canvas = document.getElementById('webgl-canvas');
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0a0e17);
    scene.fog = new THREE.FogExp2(0x0a0e17, 0.0003);

    const b = SIM_DATA.bounds || {{ min_x: 0, max_x: 4000, min_y: 0, max_y: 4000 }};
    const midX = (b.min_x + b.max_x) / 2;
    const midY = (b.min_y + b.max_y) / 2;

    // Camera
    camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 10, 15000);
    camera.position.set(midX, 1600, midY + 1800);

    // Renderer
    renderer = new THREE.WebGLRenderer({{ canvas: canvas, antialias: true, powerPreference: "high-performance" }});
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;

    // Controls
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;
    controls.maxPolarAngle = Math.PI / 2 - 0.05;
    controls.target.set(midX, 0, midY);

    // Lighting
    const ambient = new THREE.AmbientLight(0xddeeff, 0.7);
    scene.add(ambient);

    const sun = new THREE.DirectionalLight(0xfffaed, 1.3);
    sun.position.set(midX + 500, 2500, midY + 1000);
    sun.castShadow = true;
    scene.add(sun);

    build3DTerrain();

    window.addEventListener('resize', onWindowResize);
    requestAnimationFrame(renderLoop);
  }} catch (err) {{
    console.error('Three.js Init Error:', err);
    alert('3D Init Error: ' + err.message);
  }}
}}

function build3DTerrain() {{
  const b = SIM_DATA.bounds || {{ min_x: 0, max_x: 4000, min_y: 0, max_y: 4000 }};
  const w = Math.max((b.max_x - b.min_x) + 600, 1000);
  const h = Math.max((b.max_y - b.min_y) + 600, 1000);
  const midX = (b.min_x + b.max_x) / 2;
  const midY = (b.min_y + b.max_y) / 2;

  // Terrain Plane
  const terrainGeo = new THREE.PlaneGeometry(w, h, 64, 64);
  terrainGeo.rotateX(-Math.PI / 2);

  const isSnow = (SIM_DATA.metadata?.map || '').toLowerCase().includes('snow');
  
  let terrainMat;
  if (SIM_DATA.metadata?.map_image_uri) {{
    const tex = new THREE.TextureLoader().load(SIM_DATA.metadata.map_image_uri);
    tex.wrapS = THREE.ClampToEdgeWrapping;
    tex.wrapT = THREE.ClampToEdgeWrapping;
    terrainMat = new THREE.MeshStandardMaterial({{
      map: tex,
      roughness: 0.8,
      metalness: 0.1
    }});
  }} else {{
    terrainMat = new THREE.MeshStandardMaterial({{
      color: isSnow ? 0xd0e0f0 : 0x4a6b3d,
      roughness: 0.8,
      metalness: 0.1
    }});
  }}

  const terrainMesh = new THREE.Mesh(terrainGeo, terrainMat);
  terrainMesh.position.set(midX, 0, midY);
  terrainMesh.receiveShadow = true;
  scene.add(terrainMesh);

  // River Water Plane
  const waterGeo = new THREE.PlaneGeometry(w, 200);
  waterGeo.rotateX(-Math.PI / 2);
  const waterMat = new THREE.MeshStandardMaterial({{ color: 0x1d4e89, roughness: 0.2, metalness: 0.8, transparent: true, opacity: 0.85 }});
  const waterMesh = new THREE.Mesh(waterGeo, waterMat);
  waterMesh.position.set(midX, 1, midY);
  scene.add(waterMesh);

  // Grid Lines
  const grid = new THREE.GridHelper(Math.max(w, h), 40, 0x00f0ff, 0x1e2d4a);
  grid.position.set(midX, 2, midY);
  scene.add(grid);
}}

// Procedural 3D Entity Mesh Builder
function createEntityMesh(e) {{
  const grp = new THREE.Group();
  const isP1 = e.player_id === 2;
  const col = isP1 ? 0x00f0ff : 0xf87171;

  if (e.type === 'STRUCTURE') {{
    // Building Base
    const bGeo = new THREE.BoxGeometry(70, 40, 70);
    const bMat = new THREE.MeshStandardMaterial({{ color: 0x1e293b, roughness: 0.5 }});
    const bMesh = new THREE.Mesh(bGeo, bMat);
    bMesh.position.y = 20;
    bMesh.castShadow = true;
    grp.add(bMesh);

    // Glowing Faction Roof Trim
    const rGeo = new THREE.BoxGeometry(74, 8, 74);
    const rMat = new THREE.MeshStandardMaterial({{ color: col, emissive: col, emissiveIntensity: 0.3 }});
    const rMesh = new THREE.Mesh(rGeo, rMat);
    rMesh.position.y = 42;
    grp.add(rMesh);

  }} else if (e.type === 'VEHICLE') {{
    // Tank / Humvee Chassis
    const cGeo = new THREE.BoxGeometry(26, 12, 38);
    const cMat = new THREE.MeshStandardMaterial({{ color: col, roughness: 0.4 }});
    const cMesh = new THREE.Mesh(cGeo, cMat);
    cMesh.position.y = 8;
    cMesh.castShadow = true;
    grp.add(cMesh);

    // Rotating Turret
    const tGeo = new THREE.CylinderGeometry(8, 9, 6, 12);
    const tMat = new THREE.MeshStandardMaterial({{ color: 0x334155 }});
    const tMesh = new THREE.Mesh(tGeo, tMat);
    tMesh.position.y = 15;

    // Gun Barrel
    const gGeo = new THREE.CylinderGeometry(2, 2, 22, 8);
    gGeo.rotateX(Math.PI / 2);
    const gMat = new THREE.MeshStandardMaterial({{ color: 0x0f172a }});
    const gMesh = new THREE.Mesh(gGeo, gMat);
    gMesh.position.set(0, 0, 14);
    tMesh.add(gMesh);

    grp.add(tMesh);

  }} else if (e.type === 'AIRCRAFT') {{
    // Chinook Helicopter Body
    const aGeo = new THREE.BoxGeometry(18, 16, 50);
    const aMat = new THREE.MeshStandardMaterial({{ color: col }});
    const aMesh = new THREE.Mesh(aGeo, aMat);
    aMesh.position.y = 35;
    grp.add(aMesh);

    // Dual 3D Rotor Blades
    const rotGeo = new THREE.BoxGeometry(45, 1, 4);
    const rotMat = new THREE.MeshBasicMaterial({{ color: 0xffffff }});
    const rot1 = new THREE.Mesh(rotGeo, rotMat);
    rot1.position.set(0, 46, -15);
    const rot2 = new THREE.Mesh(rotGeo, rotMat);
    rot2.position.set(0, 46, 15);
    grp.add(rot1);
    grp.add(rot2);
    grp.userData.rotors = [rot1, rot2];

  }} else {{
    // Infantry Soldier Token
    const iGeo = new THREE.CylinderGeometry(4, 4, 12, 8);
    const iMat = new THREE.MeshStandardMaterial({{ color: col }});
    const iMesh = new THREE.Mesh(iGeo, iMat);
    iMesh.position.y = 6;
    grp.add(iMesh);
  }}

  // Selection Ring Projected on Ground
  const ringGeo = new THREE.RingGeometry(18, 20, 24);
  ringGeo.rotateX(-Math.PI / 2);
  const ringMat = new THREE.MeshBasicMaterial({{ color: col, side: THREE.DoubleSide }});
  const ringMesh = new THREE.Mesh(ringGeo, ringMat);
  ringMesh.position.y = 1;
  grp.add(ringMesh);

  scene.add(grp);
  return grp;
}}

function updateSimulationState(timeSec) {{
  let livingCount = 0;
  let activeClashes = 0;

  // Update Entities
  SIM_DATA.entities.forEach(e => {{
    let grp = meshPool.get(e.id);
    if (!grp) {{
      grp = createEntityMesh(e);
      meshPool.set(e.id, grp);
    }}

    if (e.spawn_t <= timeSec) {{
      grp.visible = true;
      livingCount++;

      // Interpolate Position
      const dt = Math.max(timeSec - e.move_t, 0);
      const dist = Math.hypot(e.dest_x - e.x, e.dest_y - e.y);
      let px = e.x;
      let py = e.y;
      if (dist > 0.001) {{
        const frac = Math.min((dt * e.speed) / dist, 1.0);
        px += (e.dest_x - e.x) * frac;
        py += (e.dest_y - e.y) * frac;
      }}
      grp.position.set(px, 0, py);

      // Rotate towards destination
      if (dist > 1.0) {{
        const angle = Math.atan2(e.dest_x - e.x, e.dest_y - e.y);
        grp.rotation.y = angle;
      }}

      // Spin Helicopter Rotors
      if (grp.userData.rotors) {{
        grp.userData.rotors.forEach(r => r.rotation.y += 0.3);
      }}

    }} else {{
      grp.visible = false;
    }}
  }});

  // Update Combat Lasers & Projectiles
  fxObjects.forEach(fx => scene.remove(fx));
  fxObjects.length = 0;

  SIM_DATA.combat_fx.forEach(fx => {{
    if (fx.start_t <= timeSec && timeSec <= fx.start_t + fx.dur) {{
      activeClashes++;
      // Draw 3D Laser Beam Line
      const pts = [
        new THREE.Vector3(fx.from[0], fx.from[2] + 10, fx.from[1]),
        new THREE.Vector3(fx.to[0], fx.to[2] + 8, fx.to[1])
      ];
      const lineGeo = new THREE.BufferGeometry().setFromPoints(pts);
      const lineMat = new THREE.LineBasicMaterial({{ color: fx.color === '#00f0ff' ? 0x00f0ff : 0xf87171, linewidth: 3 }});
      const line = new THREE.Line(lineGeo, lineMat);
      scene.add(line);
      fxObjects.push(line);

      // 3D Explosion Spark
      const sGeo = new THREE.SphereGeometry(6, 8, 8);
      const sMat = new THREE.MeshBasicMaterial({{ color: 0xffaa00 }});
      const spark = new THREE.Mesh(sGeo, sMat);
      spark.position.set(fx.to[0], fx.to[2] + 8, fx.to[1]);
      scene.add(spark);
      fxObjects.push(spark);
    }}
  }});

  document.getElementById('livingUnitsCount').innerText = livingCount;
  document.getElementById('clashesCount').innerText = activeClashes;

  // AI Camera Director Trajectory
  if (cameraMode === 'DIRECTOR') {{
    const b = SIM_DATA.bounds || {{ min_x: 2000, max_x: 2000, min_y: 2000, max_y: 2000 }};
    const midX = (b.min_x + b.max_x) / 2;
    const midY = (b.min_y + b.max_y) / 2;
    const targetX = midX + Math.sin(timeSec * 0.06) * 500;
    const targetZ = midY + Math.cos(timeSec * 0.06) * 500;

    controls.target.lerp(new THREE.Vector3(targetX, 0, targetZ), 0.04);
    camera.position.lerp(new THREE.Vector3(targetX, 1400, targetZ + 1600), 0.04);
  }}
}}

let lastTimestamp = performance.now();
function renderLoop(now) {{
  const dt = (now - lastTimestamp) / 1000.0;
  lastTimestamp = now;

  if (isPlaying) {{
    currentSimTime += dt * playbackSpeed;
    const maxTime = SIM_DATA.metadata?.duration_sec || 600;
    if (currentSimTime > maxTime) {{
      currentSimTime = maxTime;
      isPlaying = false;
      document.getElementById('playBtn').innerText = '▶ Play';
    }}
    document.getElementById('timeSlider').value = currentSimTime;
    updateTimeDisplay();
  }}

  updateSimulationState(currentSimTime);
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(renderLoop);
}}

function updateTimeDisplay() {{
  const mins = Math.floor(currentSimTime / 60);
  const secs = Math.floor(currentSimTime % 60);
  document.getElementById('timeDisplay').innerText = `${{mins.toString().padStart(2, '0')}}:${{secs.toString().padStart(2, '0')}}`;
}}

function onScrub(val) {{
  currentSimTime = parseFloat(val);
  updateTimeDisplay();
}}

function togglePlay() {{
  isPlaying = !isPlaying;
  document.getElementById('playBtn').innerText = isPlaying ? '❚❚ Pause' : '▶ Play';
}}

function restart() {{
  currentSimTime = 0.0;
  isPlaying = true;
  document.getElementById('playBtn').innerText = '❚❚ Pause';
}}

function setPlaybackSpeed(val) {{
  playbackSpeed = parseFloat(val);
}}

function setCameraMode(mode) {{
  cameraMode = mode;
  document.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('active'));

  const b = SIM_DATA.bounds || {{ min_x: 2000, max_x: 2000, min_y: 2000, max_y: 2000 }};
  const midX = (b.min_x + b.max_x) / 2;
  const midY = (b.min_y + b.max_y) / 2;

  if (mode === 'DIRECTOR') {{
    document.getElementById('btnCamDirector').classList.add('active');
  }} else if (mode === 'ORBIT') {{
    document.getElementById('btnCamOrbit').classList.add('active');
  }} else if (mode === 'TOP') {{
    document.getElementById('btnCamTop').classList.add('active');
    camera.position.set(midX, 3200, midY);
    controls.target.set(midX, 0, midY);
  }}
}}

function onWindowResize() {{
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}}

window.addEventListener('load', init3D);
</script>

</body>
</html>
"""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        return output_path
