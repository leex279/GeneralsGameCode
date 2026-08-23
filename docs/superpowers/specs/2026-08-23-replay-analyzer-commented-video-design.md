# Generals Replay Analyzer Commented Video Design

Date: 23 August 2026

Status: approved by the resumed complete-product objective

## 1. Product outcome

For an accepted Zero Hour replay, the analyzer produces a downloadable MP4 that plays the real match in the native 3D engine, follows evidence-backed action with an automated broadcast camera, and carries synchronized spoken commentary and subtitles derived from the same immutable analysis graph shown in the Web application.

The product is complete only when the pipeline proves this with a full replay. A video ending at a CRC boundary is a diagnostic preview, not a completed cast.

## 2. Truth and determinism boundaries

The video pipeline consumes a frozen replay identity, a successful or explicitly partial telemetry run, a fixed report, and immutable evidence references. It never writes into `GameLogic`, changes simulation inputs, or uses camera, audio, filesystem, clock, model, or rendering state to decide gameplay.

Camera direction and frame capture are `GameClient`/device concerns. Capture-on and capture-off runs must produce identical replay terminal facts and semantic telemetry. Every camera focus and every factual narration sentence cites accepted evidence. No plan item may exceed the validated evidence horizon.

Ollama may improve language only after its closed output validates against the frozen evidence graph. Deterministic template commentary is the offline source of truth and remains fully usable when Ollama is unavailable.

## 3. Pipeline and immutable artifacts

```text
Replay + accepted telemetry + fixed report
  -> camera-plan-v1.json
  -> commentary-plan-v1.json
  -> measured narration clips + narration.wav
  -> native replay capture + gameplay.mp4
  -> safe FFmpeg mux + final.mp4
  -> ffprobe/media/landmark verification
  -> immutable video-manifest-v1.json
  -> Web production status, preview, and download
```

Each render owns a never-reused directory below the product data root. Published artifacts are copied into the content-addressed store and addressed by SHA-256. A retry either reuses an identical successful artifact graph or creates a new render identity; it never overwrites prior evidence.

## 4. Camera plan contract

`camera-plan-v1` uses integer replay frames at the authoritative 30 Hz logic rate. It contains:

- replay public ID, replay SHA-256, telemetry run ID, trace SHA-256, map identity, and evidence horizon;
- ordered segments with stable IDs;
- start and end frame;
- target world `x`, `y`, `z`;
- zoom, pitch, and yaw;
- transition kind (`cut`, `ease`, `track`, or `hold`);
- focus kind and plain-language reason;
- cited evidence public IDs and evidence quality;
- one terminal segment that reaches the accepted replay end.

The generator prioritizes, in order: decisive engagement clusters, active damage locations, attacks with target positions, production or tech milestones, expansions and resource contests, and quiet-phase base context. It applies dwell/cooldown rules so rapid events do not produce camera jitter. It never invents entity positions from replay commands when authoritative telemetry samples or locations are absent.

Validation rejects duplicate/unsorted frames, gaps at the beginning or end, non-finite values, coordinates outside map bounds, invalid zoom/pitch/yaw, references to missing evidence, and segments beyond the accepted horizon.

## 5. Commentary plan contract

`commentary-plan-v1` contains ordered, non-overlapping events with:

- stable event ID;
- start and latest-end replay frame;
- narration text and subtitle text;
- commentary role (`intro`, `play_by_play`, `analysis`, `transition`, `outro`);
- player and strategy identities when applicable;
- cited evidence public IDs;
- confidence/evidence quality;
- camera segment ID;
- deterministic template version and optional validated Ollama run ID.

The deterministic generator covers match introduction, opening intent, build/production milestones, economy changes, engagements, strategic transitions, turning points, and outcome. It speaks only about supported phases. For partial evidence it explicitly announces the boundary and never supplies an outro that implies a winner.

Commentary uses player-friendly Zero Hour vocabulary and explains why an event matters. It does not read raw telemetry labels, schema identities, UUIDs, or generic metric tables aloud.

## 6. Voice and timing

The voice layer is provider-neutral. A configured provider receives one commentary event and returns a lossless mono clip plus provider metadata. Every clip is measured before scheduling. Scheduling may move a later event within its evidence-valid window, shorten only optional filler, or fail with a typed overlap error; it never time-stretches speech until it becomes unnatural or lets two clips overlap.

The final narration WAV is exactly the video duration. Silence fills uncovered windows. A render cannot be called fully commented if required events lack audio. Provider, voice, model/settings, source text hash, clip hash, measured sample count, and scheduled frame range are recorded.

Subtitles use the exact scheduled event windows and are muxed as a selectable track or burned in by an explicit render option. Subtitle timing and spoken timing share the same measured schedule.

## 7. Native rendered replay

Zero Hour exposes the documented product switches:

- `-replay <path.rep>`
- `-autocamera <camera-plan.txt>`
- `-recordVideo <gameplay.mp4>`
- `-videoRes <W>x<H>`
- `-videoFps <30|60>`

The initial native implementation may keep the existing prototype switches as compatibility aliases, but the product uses the switches above.

The engine reads a strictly validated camera file before playback. At 30 FPS it captures one encoded frame per logic frame. At 60 FPS it emits two presentation frames for each 30 Hz logic frame without advancing simulation twice. Replay completion, not the last camera keyframe, controls shutdown.

The D3D8 writer validates the actual backbuffer format and pitch, converts padded X8R8G8B8/A8R8G8B8 rows to tightly packed RGB24, and launches FFmpeg without a shell. It validates every lock, copy, write, pipe close, process exit, and output file. Unsupported formats or dimensions fail explicitly.

Modern capture code is excluded or shimmed for VC6 without changing legacy simulation behavior. Zero Hour is implemented and verified before any Generals backport.

## 8. Safe mux and manifest

FFmpeg is launched with a fixed executable and argv vector, never a composed shell string. Muxing does not use `-shortest` as an implicit duration policy. The requested authoritative duration determines the video and narration bounds.

`video-manifest-v1` records:

- replay, telemetry, report, camera, commentary, voice, engine, FFmpeg, gameplay video, final video, and subtitle hashes;
- requested and observed resolution, FPS, frame count, duration, codecs, pixel format, and audio properties;
- stage statuses, timestamps, diagnostics, and cancellation state;
- verification results and landmark frames.

A final MP4 is published only after `ffprobe` and content checks pass.

## 9. Web production experience

Every fixed replay report offers `Generate commented cast` when the evidence and local capabilities permit it. The production page shows:

- evidence horizon and whether a full cast is possible;
- camera plan preview with cited moments;
- commentary script and voice selection;
- render settings;
- durable stages: camera plan, commentary, voice, engine capture, mux, verify;
- live progress, elapsed time, actionable diagnostics, retry/cancel;
- final video metadata, manifest evidence, in-browser preview, and download.

Video status also appears in the library and job history. Operational detail is secondary to the finished cast and its most important moments.

## 10. Failure behavior

- Replay CRC mismatch: allow an explicitly labelled diagnostic preview only through the observed boundary; block completed-cast claims.
- Missing full telemetry: explain which camera/commentary facts are unavailable.
- Ollama unavailable or invalid: use deterministic commentary without loss of factual coverage.
- Voice provider unavailable: retain the script and camera plan, mark voice/render blocked, and never publish a silent result as fully commented.
- Engine/FFmpeg launch failure, timeout, cancellation, broken pipe, partial write, unsafe output, or nonzero exit: keep typed diagnostics and never publish the final hash.
- Invalid map/camera coordinate: omit that focus candidate or fail plan validation; never clamp a materially wrong target silently.

## 11. Acceptance evidence

### Deterministic plans

- Repeated runs over identical immutable inputs produce byte-identical camera and deterministic commentary plans.
- Every factual commentary event and camera focus resolves to accepted evidence.
- Partial traces produce no post-horizon plan item or winner claim.
- Hostile values, invalid coordinates, non-finite numbers, duplicate frames, and broken evidence references are rejected.

### Native capture

- Capture-on and capture-off terminal facts and semantic telemetry match.
- Modern Win32 Release and Debug build and link.
- VC6 compatibility is compiled or explicitly blocked by an unavailable toolchain; static exclusion alone is not represented as a successful VC6 build.
- Synthetic row-pitch/format tests cover padded X8R8G8B8 and A8R8G8B8 surfaces.
- FFmpeg argv injection, launch failure, partial write, broken pipe, timeout, cancellation, and nonzero exit are covered.

### Finished media

- A clean full retail replay completes without CRC mismatch.
- `ffprobe` confirms H.264, yuv420p, requested dimensions, constant 30 or 60 FPS, expected frame count/duration, AAC audio, and a non-silent narration track.
- Narration landmarks align with their cited replay frames within one output video frame.
- Key screenshots are non-black, visually distinct, and centered near cited world positions.
- The final manifest verifies every hash from replay through MP4.

### Product journey

- Installed-wheel clean-root journey imports a real replay, produces a full analysis, requests a cast, observes every durable stage, previews the result, downloads it, and independently verifies its manifest.
- Keyboard, accessibility, CSP, offline assets, hostile-origin, and cancellation/retry gates pass.

## 12. Completion boundary

The commented-video capability is complete only when the Web application produces and verifies at least one full native replay MP4 with synchronized evidence-backed commentary. A camera-plan JSON, a synthetic map animation, a silent capture, a fixture-only browser flow, or a partial CRC-boundary clip is progress evidence but not completion.
