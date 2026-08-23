# Replay Analyzer Automatic Identity Wiring Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Follow test-first RED/GREEN and independent review.

**Goal:** Resolve authoritative parser player slots to canonical player identities automatically inside the durable production import pipeline so feature, strategy, longitudinal, scouting, and report stages no longer depend on browser-fixture seeding.

**Architecture:** Keep identity resolution inside the existing `import_observations` durability boundary. Wrap `ParserObservationImporter` with a narrow identity-aware port: parser observations commit first, exact-match identity resolution commits second, telemetry observations import third, and only then may the stage succeed and unlock derivation. Web remains enqueue-only. Do not create a new durable stage or expose identity decisions in job output.

**Constraints:**

- Exact embedded-name linking only; ambiguous/manual-review/ineligible decisions remain unresolved without inventing an identity.
- Parser failures never invoke identity resolution.
- SQLite busy is retryable; all other identity failures are sanitized and nonretryable.
- The existing five-field `import_observations` job output remains unchanged and contains no player names, aliases, canonical IDs, paths, or provider tokens.
- Retries are idempotent and may not duplicate players, aliases, operations, or revision changes.
- Existing already-succeeded jobs are not silently rewritten; a later reconciliation/versioning task owns persisted upgrades.

## Task 1: Resolve identities inside observation import

**Files:**

- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/identity_import.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/__init__.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/telemetry_import.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/analysis_pipeline/composition.py`
- Create: `scripts/replay_analyzer/tests/importing/test_identity_import.py`
- Modify: `scripts/replay_analyzer/tests/importing/test_telemetry_import.py`
- Modify: `scripts/replay_analyzer/tests/analysis_pipeline/test_composition.py`
- Modify: `scripts/replay_analyzer/tests/analysis_pipeline/test_pipeline_end_to_end.py`
- Modify: `scripts/replay_analyzer/tests/browser/populated_fixture.py`
- Modify only if required by fixture acceptance: `scripts/replay_analyzer/tests/browser/test_strategy_first_journey.py`

**Interfaces:**

- Define `ParserObservationImportPort` with the exact successful and failed-dependency methods consumed by `ObservationImportHandler`.
- Define `IdentityResolutionPort.resolve_parser_run(*, replay_public_id, parser_run_id, actor) -> IdentityResolutionBatch`.
- Produce `IdentityResolvingParserObservationImporter(delegate, identity_resolver)` whose successful import method accepts explicit `replay_public_id`, delegates parser import, resolves only a successful run, validates returned replay/run identities, and returns the unchanged `ParserImportResult`.
- Use fixed actor `pipeline:import-observations`.
- Map `IdentityBusyError` to `StageFailure("identity_resolution_busy", ..., retryable=True)` and all other `IdentityError` or contract mismatch outcomes to sanitized nonretryable failures. Telemetry may not run after either failure.
- Construct the wrapper with `PlayerIdentityService(session_factory)` in `create_production_import_service`; worker and foreground CLI inherit it, while Web still only enqueues.

- [ ] Write failing unit tests for parser→identity→telemetry order, cached success, retry idempotency, failed parser/dependency behavior, busy/nonbusy failure mapping, returned batch identity validation, and accepted unresolved decisions.
- [ ] Add a failing production composition/end-to-end test proving both named human slots are linked immediately after `import_observations`; AI/open/unnamed slots remain unlinked; downstream feature selections expose canonical player IDs; the job output remains private and unchanged.
- [ ] Verify RED against the current direct `ParserObservationImporter` wiring and manual browser-fixture resolution.
- [ ] Implement the port/decorator, handler error boundary, and production composition with required `TheSuperHackers @feature/@fix` comments.
- [ ] Remove the browser fixture's manual second parser import and direct `resolve_parser_run()` workaround; prove the normal production composition already linked `leex279` and `FOX27`.
- [ ] Run focused import/composition/end-to-end/browser tests, then the broader pipeline/importing/identity/Web suite, Ruff, and strict mypy.
- [ ] Commit `feat(analyzer): Resolve imported player identities` and push.

## Acceptance

- Normal configured and parser-only imports both resolve exact human identities before feature derivation.
- A second attempt is a no-op with stable identity revisions and audit operations.
- Ambiguous/ineligible slots remain explicitly unresolved and do not fail replay-specific analysis.
- No Web request invokes parser, identity, telemetry, or engine work.
- The populated browser fixture contains no manual identity-resolution call.
- Protected unrelated files remain unstaged.
