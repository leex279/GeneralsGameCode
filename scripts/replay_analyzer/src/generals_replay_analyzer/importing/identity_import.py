"""Identity-aware parser observation import boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..identity.dto import IdentityResolutionBatch
from ..identity.service import IdentityError
from .parser_import import ParserImportResult


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
        if (
            type(batch) is not IdentityResolutionBatch
            or batch.replay_public_id != replay_public_id
            or batch.parser_run_id != result.run_id
        ):
            raise IdentityResolutionContractError("identity resolution result does not match parser authority")
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
