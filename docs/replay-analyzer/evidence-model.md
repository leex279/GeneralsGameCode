# Replay Analyzer evidence model

The analyzer separates what the replay or engine directly exposed from deterministic calculations and optional model interpretation. A polished narrative never upgrades evidence quality.

## Observed

Observed evidence is immutable source material with a stable public ID and source identity. Examples include replay header fields and commands, engine telemetry records, completion/CRC status, validated map assets, entity samples, economy events, construction and production events, and combat events.

Observed does not mean complete or correct for every use. Each observation remains bound to parser/exporter versions, replay hash, run ID, frame, player/object identity where applicable, and quality diagnostics. A CRC mismatch or truncated trace can make an observed record valid while limiting the match-level conclusion that may use it.

## Derived

Derived evidence is deterministic output from accepted observed or derived inputs. It records:

- extractor or rule name and version;
- exact replay/player/window scope;
- formula or typed value;
- input evidence references;
- quality and exclusion reasons;
- feature-definition and identity revisions.

Build orders, income totals, composition counts, effective actions per minute, engagement summaries, spatial density, strategy candidates, and longitudinal distributions belong here. Re-running the same versioned inputs must produce the same canonical result.

## Inferred

Inferred evidence is optional Ollama interpretation. It records model name, digest/build identity where available, prompt/schema version, confidence, citations, validation status, and failure status. It may summarize or explain accepted deterministic evidence, but it cannot create players, events, map positions, outcomes, timings, or metrics.

If Ollama is unavailable or returns invalid data, the inferred section is unavailable and deterministic reports remain valid.

## Quality and availability

Evidence tier and analysis availability are separate dimensions:

- `complete`: required inputs completed and passed the applicable integrity contract.
- `partial`: some accepted evidence exists, but a CRC mismatch, truncation, bounded presentation, missing optional family, or other diagnostic limits coverage.
- `unavailable`: the required evidence or compatible version does not exist.
- `failed`: the stage attempted work but could not publish a valid immutable result.

The UI must show quality warnings above claims. Unavailable panels include reason codes and never substitute zeroes, generic prose, procedural terrain, straight-line paths, or synthetic unit activity.

## Identity and provenance

Replay-local player slots come from replay/engine evidence. Canonical player identities are revisioned links over those slots. External filenames, Strata match IDs, source user tokens, provider accounts, and import paths are provenance—not player identity—unless a human performs an explicit audited attachment supported by the identity workflow.

## Spatial evidence

Spatial output is allowed only when validated engine map assets and telemetry share the accepted replay/run/data identities. The report records raw coordinates, map normalization, player-relative transforms, map bounds, locomotor/pathability class, sampling interval, and deterministic downsampling counts. Missing navigation or start-position evidence disables the dependent view.

## Immutability and links

Published observations, features, assessments, inferred documents, and reports are immutable. Superseding work creates a new version and fixed public URL. Every displayed claim links to an evidence page that exposes the source record or deterministic derivation inputs; evidence references cannot cross replay, player, report, or version boundaries.

Synthetic fixtures are permitted only in tests and demos that label them as such. They must never enter a trusted user library or be presented as factual analysis of a real match.
