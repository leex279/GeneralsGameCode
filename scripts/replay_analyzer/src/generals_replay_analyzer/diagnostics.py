"""Revision-scoped coordinator for injected, pre-bounded component probes."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias, cast

from generals_replay_analyzer.configuration import (
    SettingsStoreError,
    normalize_ollama_endpoint,
    validate_ollama_model_name,
)

DiagnosticKind: TypeAlias = Literal[
    "data_root_writable",
    "sqlite_integrity",
    "engine_launch_version",
    "ollama_model_available",
    "ollama_minimal_generation",
]
DiagnosticState: TypeAlias = Literal["passed", "failed", "unavailable"]
ComponentKind: TypeAlias = Literal[
    "analyzer",
    "parser",
    "telemetry_schema",
    "exporter",
    "message_catalog",
    "game_data_catalog",
    "map_asset",
    "database_schema",
]

_KINDS: Final[tuple[DiagnosticKind, ...]] = (
    "data_root_writable",
    "sqlite_integrity",
    "engine_launch_version",
    "ollama_model_available",
    "ollama_minimal_generation",
)
_MISSING_CODES: Final[dict[DiagnosticKind, str]] = {
    "data_root_writable": "data_root_probe_unavailable",
    "sqlite_integrity": "sqlite_probe_unavailable",
    "engine_launch_version": "engine_probe_not_supported",
    "ollama_model_available": "ollama_adapter_unavailable",
    "ollama_minimal_generation": "ollama_adapter_unavailable",
}
_ALLOWED_OUTCOMES: Final[dict[DiagnosticKind, dict[str, DiagnosticState]]] = {
    "data_root_writable": {
        "data_root_probe_unavailable": "unavailable",
        "data_root_writable": "passed",
        "data_root_not_writable": "failed",
        "data_root_identity_changed": "failed",
    },
    "sqlite_integrity": {
        "sqlite_probe_unavailable": "unavailable",
        "sqlite_integrity_ok": "passed",
        "sqlite_foreign_keys_invalid": "failed",
        "sqlite_schema_incompatible": "failed",
        "sqlite_integrity_failed": "failed",
    },
    "engine_launch_version": {
        "engine_probe_not_supported": "unavailable",
        "engine_version_ok": "passed",
        "engine_probe_timeout": "failed",
        "engine_process_unsettled": "failed",
    },
    "ollama_model_available": {
        "ollama_adapter_unavailable": "unavailable",
        "ollama_model_available": "passed",
        "ollama_model_unavailable": "failed",
        "model_digest_unavailable": "failed",
        "ollama_transport_refused": "failed",
        "ollama_response_too_large": "failed",
    },
    "ollama_minimal_generation": {
        "ollama_adapter_unavailable": "unavailable",
        "ollama_generation_ok": "passed",
        "ollama_response_invalid": "failed",
        "ollama_model_mismatch": "failed",
        "ollama_generation_incomplete": "failed",
        "ollama_transport_refused": "failed",
        "ollama_response_too_large": "failed",
    },
}
_MESSAGES: Final[dict[str, str]] = {
    "data_root_probe_unavailable": "The data-root diagnostic adapter is unavailable.",
    "data_root_writable": "The configured product data root passed its bounded write check.",
    "data_root_not_writable": "The configured product data root failed its bounded write check.",
    "data_root_identity_changed": "The product data root identity changed during the write check.",
    "sqlite_probe_unavailable": "The database integrity diagnostic adapter is unavailable.",
    "sqlite_integrity_ok": "The configured database passed its read-only integrity checks.",
    "sqlite_foreign_keys_invalid": "The configured database failed its foreign-key check.",
    "sqlite_schema_incompatible": "The configured database schema identity is incompatible.",
    "sqlite_integrity_failed": "The configured database failed its read-only integrity check.",
    "engine_probe_not_supported": "A replay-free engine version probe is not available.",
    "engine_version_ok": "The accepted replay-free engine version probe passed.",
    "engine_probe_timeout": "The accepted replay-free engine version probe timed out.",
    "engine_process_unsettled": "The engine probe process tree did not settle safely.",
    "ollama_adapter_unavailable": "The optional local Ollama diagnostic adapter is unavailable.",
    "ollama_model_available": "The exact configured local Ollama model and digest are available.",
    "ollama_model_unavailable": "The exact configured local Ollama model is unavailable.",
    "model_digest_unavailable": "The configured local Ollama model did not provide an exact digest.",
    "ollama_transport_refused": "The bounded local Ollama transport refused the diagnostic request.",
    "ollama_response_too_large": "The local Ollama response exceeded the diagnostic bound.",
    "ollama_generation_ok": "The fixed schema-constrained local Ollama probe passed.",
    "ollama_response_invalid": "The local Ollama response failed the fixed schema validation.",
    "ollama_model_mismatch": "The local Ollama response did not match the configured model identity.",
    "ollama_generation_incomplete": "The local Ollama diagnostic generation did not complete.",
    "data_root_writable_probe_failed": "The data-root diagnostic failed safely.",
    "sqlite_integrity_probe_failed": "The database integrity diagnostic failed safely.",
    "engine_launch_version_probe_failed": "The replay-free engine diagnostic failed safely.",
    "ollama_model_available_probe_failed": "The local model-availability diagnostic failed safely.",
    "ollama_minimal_generation_probe_failed": "The local model-generation diagnostic failed safely.",
}
_SAFE_IDENTITY_TEXT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+ -]{0,254}", re.ASCII)
_SHA256: Final = re.compile(r"[0-9a-f]{64}", re.ASCII)


class DiagnosticsError(RuntimeError):
    """Stable diagnostic command failure with no private exception detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _optional_identity_text(value: str | None) -> str | None:
    if value is None:
        return None
    if _SAFE_IDENTITY_TEXT.fullmatch(value) is None:
        raise DiagnosticsError("diagnostic_identity_invalid")
    return value


def _optional_digest(value: str | None) -> str | None:
    if value is not None and _SHA256.fullmatch(value) is None:
        raise DiagnosticsError("diagnostic_identity_invalid")
    return value


@dataclass(frozen=True, slots=True)
class DiagnosticComponentIdentity:
    """Validated component identity without a locator or process detail."""

    component: ComponentKind
    version: str | None = None
    content_digest: str | None = None
    build_identity: str | None = None

    def __post_init__(self) -> None:
        if self.component not in cast(tuple[str, ...], (
            "analyzer",
            "parser",
            "telemetry_schema",
            "exporter",
            "message_catalog",
            "game_data_catalog",
            "map_asset",
            "database_schema",
        )):
            raise DiagnosticsError("diagnostic_identity_invalid")
        _optional_identity_text(self.version)
        _optional_digest(self.content_digest)
        _optional_identity_text(self.build_identity)


@dataclass(frozen=True, slots=True)
class DiagnosticModelIdentity:
    """Validated exact local model identity without transport response data."""

    endpoint: str
    model_name: str
    configured_model_digest: str | None = None
    discovered_model_digest: str | None = None

    def __post_init__(self) -> None:
        try:
            endpoint = normalize_ollama_endpoint(self.endpoint)
            model_name = validate_ollama_model_name(self.model_name)
        except SettingsStoreError:
            raise DiagnosticsError("diagnostic_identity_invalid") from None
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "model_name", model_name)
        _optional_digest(self.configured_model_digest)
        _optional_digest(self.discovered_model_digest)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """Pre-redacted result supplied by one accepted bounded probe adapter."""

    state: DiagnosticState
    code: str
    component: DiagnosticComponentIdentity | None = None
    model: DiagnosticModelIdentity | None = None

    def __post_init__(self) -> None:
        if self.state not in {"passed", "failed", "unavailable"} or type(self.code) is not str or not self.code:
            raise DiagnosticsError("diagnostic_probe_result_invalid")
        if self.component is not None and self.model is not None:
            raise DiagnosticsError("diagnostic_probe_result_invalid")


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """Immutable public-safe diagnostic result for the web adapter."""

    kind: DiagnosticKind
    state: DiagnosticState
    code: str
    message: str
    settings_revision: int
    component: DiagnosticComponentIdentity | None
    model: DiagnosticModelIdentity | None
    duration_milliseconds: int | None


Probe: TypeAlias = Callable[[], ProbeOutcome]


# TheSuperHackers @feature Leex 23/08/2026 Coordinate bounded diagnostics without exposing probe inputs or private failures. (#TBD)
class DiagnosticCoordinator:
    """Serialize active diagnostics and bind every result to one settings revision."""

    def __init__(
        self,
        *,
        settings_revision: Callable[[], int],
        probes: Mapping[str, Probe] | None = None,
        rate_limiter: Callable[[DiagnosticKind], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings_revision = settings_revision
        self._probes = dict(probes or {})
        self._rate_limiter = rate_limiter or (lambda _kind: True)
        self._monotonic = monotonic
        self._active = {kind: threading.Lock() for kind in _KINDS}

    def run(self, *, kind: str, expected_settings_revision: int) -> DiagnosticResult:
        """Run one closed injected probe after revision, rate, and concurrency checks."""
        if kind not in _KINDS:
            raise DiagnosticsError("diagnostic_kind_invalid")
        accepted_kind = kind
        if type(expected_settings_revision) is not int or expected_settings_revision < 0:
            raise DiagnosticsError("settings_revision_conflict")
        current_revision = self._settings_revision()
        if type(current_revision) is not int or current_revision < 0 or current_revision != expected_settings_revision:
            raise DiagnosticsError("settings_revision_conflict")

        lock = self._active[accepted_kind]
        if not lock.acquire(blocking=False):
            raise DiagnosticsError("diagnostic_already_running")
        try:
            if not self._rate_limiter(accepted_kind):
                raise DiagnosticsError("diagnostic_rate_limited")
            started = self._monotonic()
            probe = self._probes.get(accepted_kind)
            if probe is None:
                outcome = ProbeOutcome("unavailable", _MISSING_CODES[accepted_kind])
            else:
                try:
                    outcome = probe()
                # TheSuperHackers @info Leex 23/08/2026 Redact every injected adapter failure at this trust boundary. (#TBD)
                except Exception:  # noqa: BLE001
                    outcome = ProbeOutcome("failed", self._fallback_code(accepted_kind))
            finished = self._monotonic()
            return self._result(accepted_kind, current_revision, outcome, started, finished)
        finally:
            lock.release()

    @staticmethod
    def _fallback_code(kind: DiagnosticKind) -> str:
        return f"{kind}_probe_failed"

    def _result(
        self,
        kind: DiagnosticKind,
        revision: int,
        outcome: ProbeOutcome,
        started: float,
        finished: float,
    ) -> DiagnosticResult:
        code = outcome.code
        if _ALLOWED_OUTCOMES[kind].get(code) != outcome.state:
            code = self._fallback_code(kind)
            outcome = ProbeOutcome("failed", code)
        duration = max(0, round((finished - started) * 1000))
        return DiagnosticResult(
            kind=kind,
            state=outcome.state,
            code=code,
            message=_MESSAGES[code],
            settings_revision=revision,
            component=outcome.component,
            model=outcome.model,
            duration_milliseconds=duration,
        )
