"""Identity-aware parser observation import boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from ..identity.dto import IdentityResolutionBatch
from ..identity.service import IdentityBusyError, IdentityError
from .jobs import StageFailure
from .parser_import import ParserImportResult
from .service import StageDependencyOutput, StageExecutionContext
from .stages import (
    IMPORT_OBSERVATIONS,
    IMPORT_OBSERVATIONS_VERSION,
    RECONCILE_IDENTITIES,
    RECONCILE_IDENTITIES_VERSION,
)


class ParserObservationImportPort(Protocol):
    """Narrow parser persistence contract consumed by observation import."""

    def import_replay(
        self,
        replay_sha256: str,
        *,
        replay_public_id: str,
        parser_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> ParserImportResult: ...

    def record_failed_dependency(
        self,
        replay_sha256: str,
        *,
        parser_version: str,
        idempotency_key: str,
        error_code: str,
        error_message: str,
        error_details: Mapping[str, object],
    ) -> ParserImportResult: ...


class _ParserObservationDelegatePort(Protocol):
    """Existing parser importer contract decorated by this boundary."""

    def import_replay(
        self,
        replay_sha256: str,
        *,
        parser_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> ParserImportResult: ...

    def record_failed_dependency(
        self,
        replay_sha256: str,
        *,
        parser_version: str,
        idempotency_key: str,
        error_code: str,
        error_message: str,
        error_details: Mapping[str, object],
    ) -> ParserImportResult: ...


class IdentityResolutionPort(Protocol):
    """Exact parser-run identity resolver used after parser persistence."""

    def resolve_parser_run(
        self,
        *,
        replay_public_id: str,
        parser_run_id: str,
        actor: str,
    ) -> IdentityResolutionBatch: ...


class IdentityResolutionContractError(IdentityError):
    """Raised when a resolver confirms a different replay or parser run."""


def _identity_failure(error: IdentityError) -> StageFailure:
    if isinstance(error, IdentityBusyError):
        return StageFailure(
            "identity_resolution_busy",
            "player identity resolution is busy",
            retryable=True,
        )
    if isinstance(error, IdentityResolutionContractError):
        return StageFailure(
            "identity_resolution_contract_invalid",
            "player identity resolution contract is invalid",
            retryable=False,
        )
    return StageFailure(
        "identity_resolution_failed",
        "player identity resolution failed",
        retryable=False,
    )


def _validated_batch(
    batch: object,
    *,
    replay_public_id: str,
    parser_run_id: str,
) -> IdentityResolutionBatch:
    if (
        type(batch) is not IdentityResolutionBatch
        or batch.replay_public_id != replay_public_id
        or batch.parser_run_id != parser_run_id
    ):
        raise IdentityResolutionContractError("identity resolution result does not match parser authority")
    return batch


# TheSuperHackers @feature Leex 23/08/2026 Resolve exact player identities after parser commit and before telemetry import. (#TBD)
class IdentityResolvingParserObservationImporter:
    """Decorate parser persistence with exact, idempotent player resolution."""

    def __init__(
        self,
        delegate: _ParserObservationDelegatePort,
        identity_resolver: IdentityResolutionPort,
    ) -> None:
        self._delegate = delegate
        self._identity_resolver = identity_resolver

    def import_replay(
        self,
        replay_sha256: str,
        *,
        replay_public_id: str,
        parser_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> ParserImportResult:
        result = self._delegate.import_replay(
            replay_sha256,
            parser_version=parser_version,
            idempotency_key=idempotency_key,
        )
        if type(result) is not ParserImportResult:
            raise IdentityResolutionContractError("parser observation result is invalid")
        if result.status != "succeeded":
            return result
        batch = self._identity_resolver.resolve_parser_run(
            replay_public_id=replay_public_id,
            parser_run_id=result.run_id,
            actor="pipeline:import-observations",
        )
        _validated_batch(
            batch,
            replay_public_id=replay_public_id,
            parser_run_id=result.run_id,
        )
        return result

    def record_failed_dependency(
        self,
        replay_sha256: str,
        *,
        parser_version: str,
        idempotency_key: str,
        error_code: str,
        error_message: str,
        error_details: Mapping[str, object],
    ) -> ParserImportResult:
        result = self._delegate.record_failed_dependency(
            replay_sha256,
            parser_version=parser_version,
            idempotency_key=idempotency_key,
            error_code=error_code,
            error_message=error_message,
            error_details=error_details,
        )
        if type(result) is not ParserImportResult:
            raise IdentityResolutionContractError("parser observation result is invalid")
        return result


# TheSuperHackers @feature Leex 23/08/2026 Reconcile succeeded pre-wiring parser runs through one durable versioned job. (#TBD)
class IdentityReconciliationHandler:
    """Resolve identities for an immutable succeeded observation dependency."""

    def __init__(self, identity_resolver: IdentityResolutionPort) -> None:
        self._identity_resolver = identity_resolver

    def __call__(self, context: StageExecutionContext) -> Mapping[str, object]:
        if (
            context.stage != RECONCILE_IDENTITIES
            or context.component_version != RECONCILE_IDENTITIES_VERSION
            or type(context.dependencies) is not tuple
            or len(context.dependencies) != 1
        ):
            raise StageFailure(
                "identity_reconciliation_contract_invalid",
                "identity reconciliation execution context is invalid",
                retryable=False,
            )
        dependency = context.dependencies[0]
        if (
            type(dependency) is not StageDependencyOutput
            or dependency.stage != IMPORT_OBSERVATIONS
            or dependency.component_version != IMPORT_OBSERVATIONS_VERSION
            or dependency.status != "succeeded"
            or dependency.output is None
            or dependency.error_code is not None
            or dependency.error_message is not None
            or dependency.error_details is not None
            or context.input.get("observation_job_public_id") != dependency.job_public_id
            or context.input.get("replay_sha256") != context.replay_sha256
            or set(context.input) != {"observation_job_public_id", "replay_sha256"}
        ):
            raise StageFailure(
                "identity_reconciliation_dependency_invalid",
                "identity reconciliation dependency is invalid",
                retryable=False,
            )
        output = dependency.output
        if set(output) != {
            "idempotency_key",
            "parser_run_id",
            "parser_command_count",
            "telemetry_run_id",
            "telemetry_event_count",
        }:
            raise StageFailure(
                "identity_reconciliation_dependency_invalid",
                "identity reconciliation dependency is invalid",
                retryable=False,
            )
        parser_run_id = output.get("parser_run_id")
        try:
            parsed_run_id = UUID(parser_run_id) if isinstance(parser_run_id, str) else None
        except ValueError:
            parsed_run_id = None
        if parsed_run_id is None or str(parsed_run_id) != parser_run_id:
            raise StageFailure(
                "identity_reconciliation_dependency_invalid",
                "identity reconciliation dependency is invalid",
                retryable=False,
            )
        try:
            batch = self._identity_resolver.resolve_parser_run(
                replay_public_id=context.replay_public_id,
                parser_run_id=parser_run_id,
                actor="pipeline:reconcile-identities",
            )
            batch = _validated_batch(
                batch,
                replay_public_id=context.replay_public_id,
                parser_run_id=parser_run_id,
            )
        except IdentityError as error:
            raise _identity_failure(error) from error
        return {
            "affected_player_count": len(batch.affected_player_revisions),
            "decision_count": len(batch.decisions),
            "idempotency_key": context.idempotency_key,
            "parser_run_id": parser_run_id,
        }
