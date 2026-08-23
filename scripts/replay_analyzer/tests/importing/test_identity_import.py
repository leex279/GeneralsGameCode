"""Automatic identity resolution at the observation-import durability boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from generals_replay_analyzer.identity.dto import IdentityDecision, IdentityResolutionBatch
from generals_replay_analyzer.identity.service import IdentityBusyError, IdentityInvariantError
from generals_replay_analyzer.importing.identity_import import (
    IdentityResolutionContractError,
    IdentityResolvingParserObservationImporter,
)
from generals_replay_analyzer.importing.jobs import StageFailure
from generals_replay_analyzer.importing.parser_import import ParserImportResult
from generals_replay_analyzer.importing.service import StageDependencyOutput, StageExecutionContext
from generals_replay_analyzer.importing.telemetry_import import (
    ObservationImportHandler,
    TelemetryImportResult,
)

REPLAY_PUBLIC_ID = "00000000-0000-0000-0000-000000000201"
PARSER_RUN_ID = "00000000-0000-0000-0000-000000000202"
PARSE_JOB_PUBLIC_ID = "00000000-0000-0000-0000-000000000203"


def _batch(
    *,
    replay_public_id: str = REPLAY_PUBLIC_ID,
    parser_run_id: str = PARSER_RUN_ID,
    unresolved: bool = False,
) -> IdentityResolutionBatch:
    decisions = ()
    if unresolved:
        decisions = (
            IdentityDecision(
                replay_player_public_id="00000000-0000-0000-0000-000000000204",
                normalized_name="ambiguous",
                outcome="manual_review",
                player_public_id=None,
                alias_public_id=None,
                operation_public_id=None,
                reason_code="ambiguous_exact_embedded_alias",
            ),
        )
    return IdentityResolutionBatch(replay_public_id, parser_run_id, decisions, ())


class _Delegate:
    def __init__(self, result: ParserImportResult) -> None:
        self.result = result
        self.calls: list[tuple[str, object]] = []

    def import_replay(
        self,
        replay_sha256: str,
        *,
        parser_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> ParserImportResult:
        self.calls.append(("import", (replay_sha256, parser_version, idempotency_key)))
        return self.result

    def record_failed_dependency(self, replay_sha256: str, **kwargs: object) -> ParserImportResult:
        self.calls.append(("failed_dependency", (replay_sha256, kwargs)))
        return self.result


class _Resolver:
    def __init__(self, batches: tuple[IdentityResolutionBatch, ...]) -> None:
        self._batches = iter(batches)
        self.calls: list[tuple[str, str, str]] = []

    def resolve_parser_run(
        self, *, replay_public_id: str, parser_run_id: str, actor: str
    ) -> IdentityResolutionBatch:
        self.calls.append((replay_public_id, parser_run_id, actor))
        return next(self._batches)


def test_successful_and_cached_parser_results_are_resolved_without_changing_parser_output() -> None:
    """Catch a cache-hit shortcut bypassing identity resolution or mutating the parser result contract."""
    result = ParserImportResult(PARSER_RUN_ID, "succeeded", "complete", 9, True)
    delegate = _Delegate(result)
    resolver = _Resolver((_batch(), _batch()))
    importer = IdentityResolvingParserObservationImporter(delegate, resolver)

    first = importer.import_replay(
        "a" * 64,
        replay_public_id=REPLAY_PUBLIC_ID,
        parser_version="parser-v1",
        idempotency_key="observations:key",
    )
    second = importer.import_replay(
        "a" * 64,
        replay_public_id=REPLAY_PUBLIC_ID,
        parser_version="parser-v1",
        idempotency_key="observations:key",
    )

    assert first is result and second is result
    assert resolver.calls == [
        (REPLAY_PUBLIC_ID, PARSER_RUN_ID, "pipeline:import-observations"),
        (REPLAY_PUBLIC_ID, PARSER_RUN_ID, "pipeline:import-observations"),
    ]


def test_failed_parser_result_and_failed_dependency_never_resolve_identity() -> None:
    """Catch failed or dependency-only parser shells being treated as player-mapping authority."""
    result = ParserImportResult(PARSER_RUN_ID, "failed", "failed", 0, False)
    delegate = _Delegate(result)
    resolver = _Resolver(())
    importer = IdentityResolvingParserObservationImporter(delegate, resolver)

    assert (
        importer.import_replay(
            "a" * 64,
            replay_public_id=REPLAY_PUBLIC_ID,
            parser_version="parser-v1",
            idempotency_key="observations:key",
        )
        is result
    )
    assert (
        importer.record_failed_dependency(
            "a" * 64,
            parser_version="parser-v1",
            idempotency_key="observations:key",
            error_code="parser_failed",
            error_message="parser failed",
            error_details={},
        )
        is result
    )
    assert resolver.calls == []


@pytest.mark.parametrize("mismatch", ["replay", "run"])
def test_identity_batch_must_confirm_the_requested_replay_and_parser_run(mismatch: str) -> None:
    """Catch a resolver linking another replay or parser attempt behind a successful import."""
    result = ParserImportResult(PARSER_RUN_ID, "succeeded", "complete", 9, False)
    batch = _batch(
        replay_public_id="00000000-0000-0000-0000-000000000299" if mismatch == "replay" else REPLAY_PUBLIC_ID,
        parser_run_id="00000000-0000-0000-0000-000000000298" if mismatch == "run" else PARSER_RUN_ID,
    )
    importer = IdentityResolvingParserObservationImporter(_Delegate(result), _Resolver((batch,)))

    with pytest.raises(IdentityResolutionContractError, match="identity resolution result"):
        importer.import_replay("a" * 64, replay_public_id=REPLAY_PUBLIC_ID)


def test_explicitly_unresolved_identity_decisions_do_not_fail_observation_import() -> None:
    """Catch manual-review decisions being promoted into replay-wide import failure."""
    result = ParserImportResult(PARSER_RUN_ID, "succeeded", "complete", 9, False)
    importer = IdentityResolvingParserObservationImporter(
        _Delegate(result),
        _Resolver((_batch(unresolved=True),)),
    )

    assert importer.import_replay("a" * 64, replay_public_id=REPLAY_PUBLIC_ID) is result


def _context() -> StageExecutionContext:
    parse_output = {
        "parser_version": "parser-v1",
        "content_sha256": "a" * 64,
        "completion_status": "complete",
        "command_count": 9,
        "warning_codes": (),
        "command_stream_offset": 1,
        "end_offset": 2,
    }
    return StageExecutionContext(
        "import-job",
        "observations:key",
        REPLAY_PUBLIC_ID,
        "a" * 64,
        "import_observations",
        "1",
        {},
        (StageDependencyOutput(PARSE_JOB_PUBLIC_ID, "parse", "1", parse_output),),
    )


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (IdentityBusyError("database at C:\\private\\identity.sqlite is locked"), "identity_resolution_busy", True),
        (IdentityInvariantError("provider-token=secret"), "identity_resolution_failed", False),
        (
            IdentityResolutionContractError("wrong run C:\\private\\trace"),
            "identity_resolution_contract_invalid",
            False,
        ),
    ],
)
def test_identity_failures_are_sanitized_and_stop_before_telemetry(
    error: Exception, code: str, retryable: bool
) -> None:
    """Catch retry classification, secret leakage, or telemetry continuing after identity failure."""
    calls: list[str] = []

    class Parser:
        def import_replay(self, *_args: object, **_kwargs: object) -> ParserImportResult:
            calls.append("parser")
            raise error

        def record_failed_dependency(self, *_args: object, **_kwargs: object) -> ParserImportResult:
            raise AssertionError("successful parser dependency must not use failure recording")

    class Telemetry:
        def import_replay(self, *_args: object, **_kwargs: object) -> TelemetryImportResult:
            calls.append("telemetry")
            return TelemetryImportResult("telemetry-run", "succeeded", 1, False)

    handler = ObservationImportHandler(Parser(), Telemetry())  # type: ignore[arg-type]
    with pytest.raises(StageFailure) as failure:
        handler(_context())

    assert failure.value.code == code
    assert failure.value.retryable is retryable
    assert failure.value.message == (
        "player identity resolution is busy"
        if retryable
        else "player identity resolution contract is invalid"
        if code == "identity_resolution_contract_invalid"
        else "player identity resolution failed"
    )
    assert failure.value.details is None
    assert calls == ["parser"]
    assert "private" not in str(failure.value) and "secret" not in str(failure.value)


def test_handler_orders_parser_identity_boundary_before_telemetry() -> None:
    """Catch telemetry becoming visible before parser-backed canonical identities commit."""
    calls: list[str] = []

    class Parser:
        def import_replay(
            self, replay_sha256: str, *, replay_public_id: str, **_kwargs: object
        ) -> ParserImportResult:
            assert replay_sha256 == "a" * 64 and replay_public_id == REPLAY_PUBLIC_ID
            calls.extend(("parser", "identity"))
            return ParserImportResult(PARSER_RUN_ID, "succeeded", "complete", 9, False)

        def record_failed_dependency(self, *_args: object, **_kwargs: object) -> ParserImportResult:
            raise AssertionError("successful parser dependency must not use failure recording")

    class Telemetry:
        def import_replay(self, *_args: object, **_kwargs: object) -> TelemetryImportResult:
            calls.append("telemetry")
            return TelemetryImportResult("telemetry-run", "succeeded", 1, False)

    handler = ObservationImportHandler(Parser(), Telemetry())  # type: ignore[arg-type]
    telemetry_dependency = StageDependencyOutput(
        "00000000-0000-0000-0000-000000000205",
        "telemetry",
        "1",
        {
            "run_id": "00000000-0000-0000-0000-000000000206",
            "runner_status": "success",
            "replay_quality": "complete",
            "strategy_analysis_scope": "full",
            "exit_code": 0,
            "engine_build": "test-engine",
            "engine_executable_sha256": "b" * 64,
            "diagnostics": (),
            "artifacts": (),
        },
    )

    output = handler(replace(_context(), dependencies=(*_context().dependencies, telemetry_dependency)))

    assert calls == ["parser", "identity", "telemetry"]
    assert output == {
        "idempotency_key": "observations:key",
        "parser_run_id": PARSER_RUN_ID,
        "parser_command_count": 9,
        "telemetry_run_id": "telemetry-run",
        "telemetry_event_count": 1,
    }
