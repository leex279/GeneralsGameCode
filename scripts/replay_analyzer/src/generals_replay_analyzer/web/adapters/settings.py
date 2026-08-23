"""Production projection for revisioned settings and explicit diagnostics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from generals_replay_analyzer.configuration import (
    ConfigurationStore,
    EffectiveSettingsSnapshot,
    SettingChange,
    SettingsImpact,
    SettingsStoreError,
)
from generals_replay_analyzer.diagnostics import (
    DiagnosticComponentIdentity,
    DiagnosticCoordinator,
    DiagnosticModelIdentity,
    DiagnosticResult,
    DiagnosticsError,
)
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    ApplySettingsCommandDTO,
    AvailabilityDTO,
    ComponentIdentityDTO,
    DiagnosticCommandDTO,
    DiagnosticDTO,
    DiagnosticResultDTO,
    EditableSettingKey,
    ModelIdentityDTO,
    RedactedLocationDTO,
    SettingChangeDTO,
    SettingsImpactDTO,
    SettingsMutationDTO,
    SettingsPreviewCommandDTO,
    SettingsSnapshotDTO,
    SettingsSource,
    SettingValueDTO,
)

_SETTINGS_CONFLICT_CODES = frozenset(
    {
        "settings_revision_conflict",
        "settings_impact_conflict",
        "settings_overridden_by_environment",
        "settings_overridden_by_composition",
    }
)
_SETTINGS_INPUT_CODES = frozenset({"settings_change_invalid", "settings_confirmation_required"})
_SETTINGS_UNAVAILABLE_CODES = frozenset(
    {
        "settings_document_invalid",
        "settings_environment_invalid",
        "settings_location_unsafe",
        "settings_write_failed",
    }
)
_DIAGNOSTIC_CONFLICT_CODES = frozenset({"settings_revision_conflict"})
_DIAGNOSTIC_RATE_CODES = frozenset({"diagnostic_already_running", "diagnostic_rate_limited"})
_DIAGNOSTIC_UNAVAILABLE_CODES = frozenset(
    {"diagnostic_identity_invalid", "diagnostic_probe_result_invalid"}
)
_DEFAULT_MODEL_AVAILABILITY = AvailabilityDTO(
    state="unavailable",
    reason_codes=("ollama_adapter_unavailable",),
)


def _settings_problem(error: SettingsStoreError) -> PublicProblem:
    if error.code in _SETTINGS_CONFLICT_CODES:
        return PublicProblem(
            status=409,
            code=error.code,
            detail="The settings command conflicts with the current configuration",
        )
    if error.code in _SETTINGS_INPUT_CODES:
        return PublicProblem(status=422, code=error.code, detail="The settings command is invalid")
    if error.code == "settings_busy":
        return PublicProblem(status=429, code=error.code, detail="The settings store is busy")
    if error.code in _SETTINGS_UNAVAILABLE_CODES:
        return PublicProblem(
            status=503,
            code=error.code,
            detail="Analyzer settings are temporarily unavailable",
        )
    return _unexpected_settings_problem()


def _diagnostic_problem(error: DiagnosticsError) -> PublicProblem:
    if error.code in _DIAGNOSTIC_CONFLICT_CODES:
        return PublicProblem(
            status=409,
            code=error.code,
            detail="The diagnostic does not match the current settings revision",
        )
    if error.code in _DIAGNOSTIC_RATE_CODES:
        return PublicProblem(status=429, code=error.code, detail="The diagnostic cannot run at this time")
    if error.code == "diagnostic_kind_invalid":
        return PublicProblem(status=422, code=error.code, detail="The diagnostic command is invalid")
    if error.code in _DIAGNOSTIC_UNAVAILABLE_CODES:
        return PublicProblem(status=503, code=error.code, detail="The requested diagnostic is unavailable")
    return _unexpected_diagnostic_problem()


def _unexpected_settings_problem() -> PublicProblem:
    return PublicProblem(
        status=503,
        code="settings_adapter_unavailable",
        detail="Analyzer settings are temporarily unavailable",
    )


def _unexpected_diagnostic_problem() -> PublicProblem:
    return PublicProblem(
        status=503,
        code="diagnostics_adapter_unavailable",
        detail="The requested diagnostic is unavailable",
    )


def _setting_changes(changes: Sequence[SettingChangeDTO]) -> tuple[SettingChange, ...]:
    return tuple(SettingChange(change.key, change.value) for change in changes)


def _impact_messages(impact: SettingsImpact) -> tuple[DiagnosticDTO, ...]:
    messages: list[DiagnosticDTO] = []
    if impact.invalidates_existing_results:
        messages.append(
            DiagnosticDTO(
                code="settings_results_version_pinned",
                message="Existing immutable results remain version-pinned; re-analysis is an explicit action.",
            )
        )
    if impact.restart_required:
        messages.append(
            DiagnosticDTO(
                code="settings_restart_required",
                message="Restart the analyzer to activate this configuration revision.",
            )
        )
    return tuple(messages)


def _web_impact(impact: SettingsImpact) -> SettingsImpactDTO:
    return SettingsImpactDTO(
        expected_revision=impact.expected_revision,
        impact_digest=impact.impact_digest,
        normalized_changes=tuple(
            SettingChangeDTO(
                key=cast(EditableSettingKey, change.key),
                value=cast(int | str, change.value),
            )
            for change in impact.normalized_changes
        ),
        affected_stage_families=impact.affected_stage_families,
        invalidates_existing_results=impact.invalidates_existing_results,
        requires_confirmation=impact.requires_confirmation,
        restart_required=impact.restart_required,
        messages=_impact_messages(impact),
    )


def _identity_availability(state: str, code: str) -> AvailabilityDTO:
    if state == "passed":
        return AvailabilityDTO(state="available")
    return AvailabilityDTO(state="unavailable", reason_codes=(code,))


def _component_identity(
    identity: DiagnosticComponentIdentity,
    *,
    state: str,
    code: str,
) -> ComponentIdentityDTO:
    return ComponentIdentityDTO(
        component=identity.component,
        version=identity.version,
        content_digest=identity.content_digest,
        build_identity=identity.build_identity,
        availability=_identity_availability(state, code),
    )


def _model_identity(
    identity: DiagnosticModelIdentity,
    *,
    state: str,
    code: str,
) -> ModelIdentityDTO:
    return ModelIdentityDTO(
        provider="ollama",
        endpoint=identity.endpoint,
        model_name=identity.model_name,
        configured_model_digest=identity.configured_model_digest,
        discovered_model_digest=identity.discovered_model_digest,
        availability=_identity_availability(state, code),
    )


# TheSuperHackers @feature Leex 23/08/2026 Project revisioned settings and bounded diagnostics without exposing private locators. (#TBD)
class SettingsDiagnosticsAdapter:
    """Expose the accepted settings store and diagnostic coordinator to Web ports."""

    def __init__(
        self,
        store: ConfigurationStore,
        diagnostics: DiagnosticCoordinator,
        *,
        startup_snapshot: EffectiveSettingsSnapshot,
        locations: Sequence[RedactedLocationDTO] = (),
        components: Sequence[ComponentIdentityDTO] = (),
        model_availability: AvailabilityDTO = _DEFAULT_MODEL_AVAILABILITY,
        configured_model_digest: str | None = None,
        discovered_model_digest: str | None = None,
    ) -> None:
        self._store = store
        self._diagnostics = diagnostics
        self._locations = tuple(locations)
        self._components = tuple(components)
        self._model_availability = model_availability
        self._configured_model_digest = configured_model_digest
        self._discovered_model_digest = discovered_model_digest
        self._startup_digest = startup_snapshot.effective_settings_digest

    def get_settings(self) -> SettingsSnapshotDTO:
        try:
            snapshot = self._store.read()
        except SettingsStoreError as error:
            raise _settings_problem(error) from None
        except Exception:  # noqa: BLE001 - redact every infrastructure failure at the adapter boundary.
            raise _unexpected_settings_problem() from None
        return self._snapshot(snapshot)

    def preview_settings(self, command: SettingsPreviewCommandDTO) -> SettingsImpactDTO:
        try:
            impact = self._store.preview(
                expected_revision=command.expected_revision,
                changes=_setting_changes(command.changes),
            )
        except SettingsStoreError as error:
            raise _settings_problem(error) from None
        except Exception:  # noqa: BLE001 - redact every infrastructure failure at the adapter boundary.
            raise _unexpected_settings_problem() from None
        return _web_impact(impact)

    def apply_settings(self, command: ApplySettingsCommandDTO) -> SettingsMutationDTO:
        changes = _setting_changes(command.changes)
        try:
            impact = self._store.preview(expected_revision=command.expected_revision, changes=changes)
            mutation = self._store.apply_confirmed(
                expected_revision=command.expected_revision,
                changes=changes,
                expected_impact_digest=command.expected_impact_digest,
                confirm_invalidating_change=command.confirm_invalidating_change,
            )
        except SettingsStoreError as error:
            raise _settings_problem(error) from None
        except Exception:  # noqa: BLE001 - redact every infrastructure failure at the adapter boundary.
            raise _unexpected_settings_problem() from None
        return SettingsMutationDTO(
            result_code=mutation.result_code,
            snapshot=self._snapshot(mutation.snapshot),
            impact=_web_impact(impact),
            analysis_jobs_queued=False,
        )

    def run_diagnostic(self, command: DiagnosticCommandDTO) -> DiagnosticResultDTO:
        try:
            result = self._diagnostics.run(
                kind=command.kind,
                expected_settings_revision=command.expected_settings_revision,
            )
        except DiagnosticsError as error:
            raise _diagnostic_problem(error) from None
        except Exception:  # noqa: BLE001 - redact every injected probe failure at the adapter boundary.
            raise _unexpected_diagnostic_problem() from None
        return self._diagnostic_result(result)

    def _snapshot(self, snapshot: EffectiveSettingsSnapshot) -> SettingsSnapshotDTO:
        restart_required = snapshot.effective_settings_digest != self._startup_digest
        source_by_key = dict(snapshot.sources)
        values = tuple(
            self._setting_value(cast(EditableSettingKey, key), value, source_by_key[key])
            for key, value in snapshot.values
        )
        model_availability = self._model_availability
        configured_digest = self._configured_model_digest
        discovered_digest = self._discovered_model_digest
        if restart_required:
            model_availability = AvailabilityDTO(state="unavailable", reason_codes=("settings_restart_required",))
            configured_digest = None
            discovered_digest = None
        return SettingsSnapshotDTO(
            schema_version=snapshot.schema_version,
            revision=snapshot.revision,
            effective_settings_digest=snapshot.effective_settings_digest,
            values=values,
            locations=self._locations,
            components=self._components,
            model=ModelIdentityDTO(
                provider="ollama",
                endpoint=cast(str, snapshot.value("ollama_url")),
                model_name=cast(str, snapshot.value("ollama_model")),
                configured_model_digest=configured_digest,
                discovered_model_digest=discovered_digest,
                availability=model_availability,
            ),
            restart_required=restart_required,
            availability=AvailabilityDTO(state="available"),
        )

    @staticmethod
    def _setting_value(key: EditableSettingKey, value: int | str, source: str) -> SettingValueDTO:
        if source == "composition":
            return SettingValueDTO(
                key=key,
                value=value,
                source="environment",
                editable=False,
                unavailable_reason_code="settings_overridden_by_composition",
            )
        public_source = cast(SettingsSource, source)
        if public_source == "environment":
            return SettingValueDTO(
                key=key,
                value=value,
                source=public_source,
                editable=False,
                unavailable_reason_code="settings_overridden_by_environment",
            )
        return SettingValueDTO(key=key, value=value, source=public_source, editable=True)

    @staticmethod
    def _diagnostic_result(result: DiagnosticResult) -> DiagnosticResultDTO:
        component = (
            None
            if result.component is None
            else _component_identity(result.component, state=result.state, code=result.code)
        )
        model = (
            None
            if result.model is None
            else _model_identity(result.model, state=result.state, code=result.code)
        )
        return DiagnosticResultDTO(
            kind=result.kind,
            state=result.state,
            code=result.code,
            message=result.message,
            settings_revision=result.settings_revision,
            component=component,
            model=model,
            duration_milliseconds=result.duration_milliseconds,
        )
