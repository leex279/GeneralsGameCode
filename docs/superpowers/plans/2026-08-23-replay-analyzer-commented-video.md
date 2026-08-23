# Generals Replay Analyzer Commented Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a verified native Zero Hour replay MP4 with evidence-backed automated camera direction, synchronized spoken commentary, subtitles, and a Web production journey.

**Architecture:** Generate immutable frame-based camera and commentary plans from accepted telemetry/report evidence, schedule measured provider-neutral voice clips, capture the real replay in the native client without changing deterministic GameLogic, then mux and verify content-addressed media. Video work is a separate durable job family that consumes fixed analysis artifacts and publishes only after media and evidence verification.

**Tech Stack:** C++98-compatible engine code plus guarded modern Win32 process code, Direct3D 8, FFmpeg/ffprobe, Python 3.12, Pydantic 2, SQLAlchemy 2, SQLite, FastAPI/Jinja2, pytest, Playwright, uv, Ruff, strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-23-replay-analyzer-commented-video-design.md`

## Global Constraints

- Zero Hour first; no gameplay or balance changes.
- Camera/capture code remains in GameClient/device paths and may not influence GameLogic.
- Camera and commentary use integer replay frames at 30 Hz and cite immutable accepted evidence.
- No factual plan item may exceed the validated evidence horizon.
- Ollama is optional wording enrichment; deterministic commentary remains complete offline.
- FFmpeg and ffprobe are launched with argv arrays and `shell=False`; output paths never enter a shell command.
- A partial CRC-boundary clip is labelled diagnostic and cannot satisfy the finished-product acceptance gate.
- New architectural/user-facing code includes `TheSuperHackers @<keyword> Leex 23/08/2026 ... (#TBD)` comments.
- Never stage or rewrite unrelated protected engine/audio changes or the protected watcher test.
- Every task follows a verified red/green cycle, then commits and pushes its own checkpoint.

---

### Task 1: Define immutable camera and video contracts

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/__init__.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/contracts.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/camera.py`
- Create: `scripts/replay_analyzer/tests/video/test_contracts.py`
- Create: `scripts/replay_analyzer/tests/video/test_camera.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`

**Interfaces:**
- Produces frozen Pydantic contracts `CameraPlanV1`, `CameraSegmentV1`, `EvidenceCitationV1`, `EvidenceHorizonV1`, and `VideoSettingsV1`.
- Produces `CameraPlanService.create(report: PublishedReportGraphDTO, scene: MapSceneReadModel) -> CameraPlanV1` from the existing report/spatial read models.
- Canonical serialization is `model_dump_json(indent=None, by_alias=True, exclude_none=True)` over an already deterministically sorted model.

- [ ] **Step 1: Write failing contract tests**

Assert schema version `1`, 30 Hz integer frames, stable IDs, sorted non-overlapping segments, start frame zero, terminal end frame equal to the accepted horizon, finite/bounded camera values, accepted transition kinds, and evidence references present in the fixed report.

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run --project . pytest tests/video/test_contracts.py tests/video/test_camera.py -q`

Expected: collection fails because the `video` package does not exist.

- [ ] **Step 3: Implement minimal contracts and deterministic priority selection**

Select candidates in the closed priority order `engagement`, `damage`, `attack_order`, `milestone`, `resource_contest`, `base_context`; then sort by `(start_frame, priority, evidence_public_id)`. Apply fixed dwell/cooldown constants, preserve map coordinates exactly, and reject rather than silently clamp invalid positions.

- [ ] **Step 4: Verify determinism and static gates**

Run: `uv run --project . pytest tests/video/test_contracts.py tests/video/test_camera.py -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/video tests/video`

Run: `uv run --project . mypy --strict src/generals_replay_analyzer/video`

- [ ] **Step 5: Commit and push**

```powershell
git add scripts/replay_analyzer/src/generals_replay_analyzer/video scripts/replay_analyzer/tests/video scripts/replay_analyzer/pyproject.toml
git commit -m "feat(video): Add evidence-backed camera plans"
git push
```

### Task 2: Generate grounded commentary plans

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/commentary.py`
- Create: `scripts/replay_analyzer/tests/video/test_commentary.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/video/contracts.py`

**Interfaces:**
- Produces `CommentaryPlanV1`, `CommentaryEventV1`, and `ValidatedCommentaryEnrichmentV1`.
- Produces `CommentaryPlanService.create(report: PublishedReportGraphDTO, camera: CameraPlanV1, *, enrichment: ValidatedCommentaryEnrichmentV1 | None = None) -> CommentaryPlanV1`.
- Accepted roles are `intro`, `play_by_play`, `analysis`, `transition`, and `outro`.

- [ ] **Step 1: Write failing complete/partial commentary tests**

The complete fixture must introduce players/map, explain opening strategies, cover major evidence-backed milestones and engagements, and close with the accepted outcome. The frame-105 fixture must announce the boundary, cite only observed evidence, omit later phases and winner language, and remain byte-identical with Ollama unavailable.

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run --project . pytest tests/video/test_commentary.py -q`

Expected: import fails because `video.commentary` does not exist.

- [ ] **Step 3: Implement versioned deterministic templates and validated enrichment**

Use closed templates keyed by exact claim/strategy/event identities and the existing presentation vocabulary. Validate optional Ollama sentences against allowed evidence IDs and frame windows; reject the entire enrichment on any unsupported reference and retain deterministic text.

- [ ] **Step 4: Verify tests and static gates**

Run: `uv run --project . pytest tests/video/test_commentary.py tests/llm -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/video/commentary.py tests/video/test_commentary.py`

Run: `uv run --project . mypy --strict src/generals_replay_analyzer/video/commentary.py`

- [ ] **Step 5: Commit and push**

```powershell
git add scripts/replay_analyzer/src/generals_replay_analyzer/video/contracts.py scripts/replay_analyzer/src/generals_replay_analyzer/video/commentary.py scripts/replay_analyzer/tests/video/test_commentary.py
git commit -m "feat(video): Generate grounded replay commentary"
git push
```

### Task 3: Schedule measured narration and subtitles

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/voice.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/windows_sapi.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/windows_sapi.ps1`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/subtitles.py`
- Create: `scripts/replay_analyzer/tests/video/test_voice.py`
- Create: `scripts/replay_analyzer/tests/video/test_windows_sapi.py`
- Create: `scripts/replay_analyzer/tests/video/test_subtitles.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`

**Interfaces:**
- Defines `VoiceProvider.render(event: CommentaryEventV1, destination: Path) -> VoiceClipV1`.
- Produces `WindowsSapiVoiceProvider(powershell_executable: Path, voice_name: str)` using `powershell.exe -NoProfile -NonInteractive -File <packaged-script> ...` as an argv-only local provider.
- Produces `NarrationScheduler.schedule(plan: CommentaryPlanV1, clips: tuple[VoiceClipV1, ...], final_frame: int) -> NarrationScheduleV1`.
- Produces `render_narration_wav(schedule, destination)` and `render_webvtt(schedule, destination)`.

- [ ] **Step 1: Write failing provider/scheduling tests**

Cover measured PCM sample counts, exact frame conversion, deterministic scheduling, non-overlap, movement only within an event's latest-end frame, typed impossible-overlap failure, exact final WAV duration, provider/voice/text/clip hashes, WebVTT windows equal to scheduled narration, SAPI argv safety, missing voice, nonzero exit, and packaged-script availability.

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run --project . pytest tests/video/test_voice.py tests/video/test_windows_sapi.py tests/video/test_subtitles.py -q`

Expected: collection fails because the voice/subtitle modules do not exist.

- [ ] **Step 3: Implement provider-neutral scheduling and a deterministic test provider**

Use the Python `wave` module for lossless mono PCM assembly. Fill gaps with zero samples, verify every source clip's declared PCM format and hash, and write atomically to the render directory. Implement Windows SAPI as the first local production provider through the packaged PowerShell script and an argv-only process boundary; no network TTS provider becomes an unconditional dependency.

- [ ] **Step 4: Verify tests and static gates**

Run: `uv run --project . pytest tests/video/test_voice.py tests/video/test_windows_sapi.py tests/video/test_subtitles.py -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/video tests/video`

Run: `uv run --project . mypy --strict src/generals_replay_analyzer/video`

- [ ] **Step 5: Commit and push**

```powershell
git add scripts/replay_analyzer/src/generals_replay_analyzer/video/voice.py scripts/replay_analyzer/src/generals_replay_analyzer/video/windows_sapi.py scripts/replay_analyzer/src/generals_replay_analyzer/video/windows_sapi.ps1 scripts/replay_analyzer/src/generals_replay_analyzer/video/subtitles.py scripts/replay_analyzer/tests/video/test_voice.py scripts/replay_analyzer/tests/video/test_windows_sapi.py scripts/replay_analyzer/tests/video/test_subtitles.py scripts/replay_analyzer/pyproject.toml
git commit -m "feat(video): Schedule narration and subtitles"
git push
```

### Task 4: Implement the validated native camera director

**Files:**
- Create: `Core/GameEngine/Include/GameClient/AutoCameraDirector.h`
- Create: `Core/GameEngine/Source/GameClient/AutoCameraDirector.cpp`
- Modify: `CMakeLists.txt`
- Modify: `Core/GameEngine/Source/Common/CommandLine.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Include/Common/GlobalData.h`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/GlobalData.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameClient/GameClient.cpp`
- Test: `scripts/replay_analyzer/tests/engine/test_autocamera_contract.py`

**Interfaces:**
- Consumes the product `-autocamera <script>` switch and a line-oriented export derived from `CameraPlanV1`.
- Each record contains `start_frame,end_frame,x,y,z,zoom,pitch,yaw,transition,segment_id`.
- `AutoCameraDirector::update()` uses `TheGameLogic->getFrame()` only as a read-only clock and never controls replay termination.

- [ ] **Step 1: Write failing source/contract tests**

Assert the public flag exists only on the modern Zero Hour analyzer path, all fields are parsed, malformed/non-finite/unsorted/out-of-range rows fail startup, yaw/pitch/zoom are applied, interpolation is frame-based and seek-safe, and no camera method calls `setQuitting` or mutates GameLogic.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/engine/test_autocamera_contract.py -q`

Expected: tests fail because the product worktree has no camera director.

- [ ] **Step 3: Implement Zero Hour-first camera parsing/lifecycle**

Port only the useful interpolation concept from the quarantined prototype. Replace time-seconds and permissive `sscanf` parsing with checked integer-frame records; validate the complete script before enabling the subsystem. Add the new source to `z_gameengine` from the top-level CMake file only under `RTS_BUILD_ZEROHOUR AND NOT IS_VS6_BUILD`; do not add it to shared interface lists or Base Generals in this checkpoint. Use the existing hardened `-replay`/`ReplaySimulation` path and do not port the dirty direct-replay startup, Recorder, GameLogic, TiVO, shroud, or fast-mode hunks.

- [ ] **Step 4: Build and verify**

Run: `uv run --project . pytest tests/engine/test_autocamera_contract.py -q`

Run: `cmake --build build/win32 --target z_generals --config Release`

Run: `cmake --build build/win32 --target z_generals --config Debug`

- [ ] **Step 5: Commit and push**

Stage only the listed camera files and their tests.

```powershell
git commit -m "feat(camera): Add validated replay direction"
git push
```

### Task 5: Implement safe D3D8 replay capture

**Files:**
- Create: `Core/GameEngineDevice/Include/W3DDevice/GameClient/W3DVideoWriter.h`
- Create: `Core/GameEngineDevice/Source/W3DDevice/GameClient/W3DVideoWriter.cpp`
- Modify: `CMakeLists.txt`
- Modify: `Core/GameEngine/Source/Common/CommandLine.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Include/Common/GlobalData.h`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/GlobalData.cpp`
- Modify: `GeneralsMD/Code/GameEngineDevice/Source/W3DDevice/GameClient/W3DDisplay.cpp`
- Test: `scripts/replay_analyzer/tests/engine/test_video_capture_contract.py`

**Interfaces:**
- Implements `-recordVideo`, `-videoRes`, and `-videoFps` with validated values.
- Emits gameplay H.264/yuv420p through a no-shell child process boundary and a typed sidecar result.
- Supports X8R8G8B8 and A8R8G8B8 row conversion with arbitrary validated positive pitch.

- [ ] **Step 1: Write failing capture/process-boundary tests**

Assert no `_popen`, `system`, or shell command composition exists; hostile paths remain one argv item; pitch-padding conversion is exact; unsupported surface format, lock/copy/write/pipe/process failures are typed; 30 FPS emits one frame per logic frame; 60 FPS emits two presentation frames without a second simulation tick.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/engine/test_video_capture_contract.py -q`

Expected: tests fail because the product worktree has no writer or flags.

- [ ] **Step 3: Implement writer and display hook**

Use an owned Win32 child process with inherited stdin pipe, quoted argv construction performed by a dedicated Windows argv encoder, explicit byte-count loops, and checked process exit. Add the source to `z_gameenginedevice` from top-level CMake only for modern Zero Hour so the protected dirty `GeneralsMD/Code/GameEngineDevice/CMakeLists.txt` remains untouched. Open only after the real backbuffer description is known, copy before `WW3D::End_Render()`/`Present()`, and emit frames once per distinct logic frame (duplicate the captured presentation frame for 60 FPS without advancing simulation). Replay completion owns shutdown; do not enable fast mode or TiVO.

- [ ] **Step 4: Build and run capture non-interference gate**

Run: `cmake --build build/win32 --target z_generals --config Release`

Run: `cmake --build build/win32 --target z_generals --config Debug`

Run: `uv run --project . pytest tests/engine/test_video_capture_contract.py tests/engine/test_telemetry_determinism.py -q`

- [ ] **Step 5: Commit and push**

Stage only the listed capture files and tests.

```powershell
git commit -m "feat(renderer): Capture deterministic replay video"
git push
```

### Task 6: Orchestrate capture, mux, verification, and manifests

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/process.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/render.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/verify.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/manifest.py`
- Create: `scripts/replay_analyzer/tests/video/test_process.py`
- Create: `scripts/replay_analyzer/tests/video/test_render.py`
- Create: `scripts/replay_analyzer/tests/video/test_verify.py`
- Create: `scripts/replay_analyzer/tests/video/test_manifest.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/config.py`
- Modify: `scripts/replay_analyzer/tests/test_config.py`

**Interfaces:**
- Produces `VideoRenderService.render(request: VideoRenderRequest) -> VideoRenderResult`.
- Launch stages are `camera_plan`, `commentary_plan`, `voice`, `engine_capture`, `mux`, and `verify`.
- Produces `video-manifest-v1.json` only after every input/output hash and ffprobe/media check succeeds.
- Adds environment-configured `ffmpeg_executable`, `ffprobe_executable`, `video_voice_provider`, `video_voice_name`, `video_width`, `video_height`, `video_fps`, and `video_subtitle_mode`; executable paths are never accepted from a Web request.

- [ ] **Step 1: Write failing no-shell/failure/manifest tests**

Cover replay and executable identity changes, malicious filenames, launch failure, timeout, cancellation, partial stdin/write, nonzero exit, duration mismatch, wrong codec/pixel format/FPS/dimensions, silent audio, missing landmarks, non-atomic publish, and complete hash binding.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/video/test_process.py tests/video/test_render.py tests/video/test_verify.py tests/video/test_manifest.py -q`

- [ ] **Step 3: Implement render directory, argv-only processes, mux, and checks**

Use one UUID render directory under `settings.data_root / "video-runs"`, ordinary non-reparse path validation, exclusive file creation, content-addressed publication, and `ffprobe -of json` parsed through a closed Pydantic model. Validate the closed video settings (`30|60` FPS, bounded dimensions, `track|burned` subtitles) at process startup. Mux to the authoritative duration and reject rather than truncate mismatched media.

- [ ] **Step 4: Verify tests and static gates**

Run: `uv run --project . pytest tests/video -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/video tests/video`

Run: `uv run --project . mypy --strict src/generals_replay_analyzer/video`

- [ ] **Step 5: Commit and push**

```powershell
git add scripts/replay_analyzer/src/generals_replay_analyzer/video scripts/replay_analyzer/tests/video
git commit -m "feat(video): Render and verify commented casts"
git push
```

### Task 7: Add durable video jobs and production UI

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/video/jobs.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/video.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/adapters/video.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/video.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/video/detail.html`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/detail.html`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/_table.html`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/web/app.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/worker.py`
- Create: `scripts/replay_analyzer/tests/web/test_video.py`
- Create: `scripts/replay_analyzer/tests/browser/test_video_journey.py`

**Interfaces:**
- Adds replay action `Generate commented cast` and durable stage/status/progress DTOs.
- Web enqueues/cancels/retries and streams verified files; only the worker launches voice, engine, FFmpeg, or ffprobe.
- Partial evidence permits only a clearly labelled diagnostic-preview request.

- [ ] **Step 1: Write failing route, adapter, and browser tests**

Assert capability/evidence gating, CSRF/origin protections, durable stage ordering, retry/cancel semantics, camera/commentary preview, actionable errors, library status, verified manifest/download identity, keyboard/accessibility, and no external browser requests.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/web/test_video.py tests/browser/test_video_journey.py -q`

- [ ] **Step 3: Implement bounded public DTOs and templates**

Reuse the existing immutable job/evidence patterns. Keep executable paths, private run directories, raw logs, and shell/process details out of templates; expose safe diagnostics and content-addressed download IDs only.

- [ ] **Step 4: Verify Web, browser, security, and static gates**

Run: `uv run --project . pytest tests/web tests/browser/test_video_journey.py tests/browser/test_security.py -q`

Run: `uv run --project . ruff check src tests/web tests/browser/test_video_journey.py`

Run: `uv run --project . mypy --strict src`

- [ ] **Step 5: Commit and push**

Stage only the listed video/Web/worker files and tests.

```powershell
git commit -m "feat(web): Add replay cast production"
git push
```

### Task 8: Prove a full commented replay MP4

**Files:**
- Create: `scripts/replay_analyzer/tests/integration/test_commented_video_acceptance.py`
- Modify: `scripts/replay_analyzer/docs/acceptance-matrix.md`
- Modify: `scripts/replay_analyzer/docs/release-status.md`
- Modify: `docs/superpowers/plans/2026-08-23-replay-analyzer-commented-video.md`

**Interfaces:**
- Consumes a fresh installed wheel, clean product root, full retail replay, configured engine, voice provider, FFmpeg, and ffprobe.
- Produces a downloadable final MP4 and a fully verified `video-manifest-v1` graph.

- [ ] **Step 1: Write the full installed-product acceptance test**

Import through production wiring, wait for a complete report, request a cast, wait for every durable stage, download final media/manifest, verify all hashes, and run independent ffprobe/landmark/audio checks. The test must reject fixture telemetry, ORM seeding, a partial horizon, a silent track, or a synthetic map renderer.

- [ ] **Step 2: Verify replay compatibility before rendering**

Run the pinned full-match replay with telemetry off and on. Both must complete without CRC mismatch and have identical terminal facts/semantic telemetry before the video gate may proceed.

- [ ] **Step 3: Run full media and product acceptance**

Run: `uv run --project . pytest tests/integration/test_commented_video_acceptance.py -q -s`

Run: `uv run --project . pytest -q`

Run: `uv run --project . ruff check src tests`

Run: `uv run --project . mypy --strict src`

Run modern Win32 Release and Debug builds and the available retail-compatibility toolchain gates.

- [ ] **Step 4: Inspect rendered evidence**

Review key screenshots at the intro, each cited engagement/turning point, and outcome; verify non-black distinct frames centered near cited world positions. Listen at every commentary landmark and verify synchronization within one output frame.

- [ ] **Step 5: Record evidence, commit, and push**

Record exact replay/executable/wheel/trace/plan/audio/gameplay/final hashes, commands, pass counts, ffprobe output, retained artifact path, toolchain status, and manual visual/audio review.

```powershell
git add scripts/replay_analyzer/tests/integration/test_commented_video_acceptance.py scripts/replay_analyzer/docs/acceptance-matrix.md scripts/replay_analyzer/docs/release-status.md docs/superpowers/plans/2026-08-23-replay-analyzer-commented-video.md
git commit -m "test(video): Verify full commented replay cast"
git push
```
