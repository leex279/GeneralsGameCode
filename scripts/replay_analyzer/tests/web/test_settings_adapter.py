"""Production settings and diagnostics adapter contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from generals_replay_analyzer.configuration import ConfigurationStore, SettingsStoreError
from generals_replay_analyzer.diagnostics import (
    DiagnosticComponentIdentity,
    DiagnosticCoordinator,
    DiagnosticModelIdentity,
    DiagnosticsError,
    ProbeOutcome,
)
from generals_replay_analyzer.web.adapters.settings import SettingsDiagnosticsAdapter
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    ApplySettingsCommandDTO,
    AvailabilityDTO,
    ComponentIdentityDTO,
    DiagnosticCommandDTO,
    RedactedLocationDTO,
    SettingChangeDTO,
    SettingsPreviewCommandDTO,
)


def _store(tmp_path: Path, **kwargs: object) -> ConfigurationStore:
    return ConfigurationStore(configuration_root=tmp_path / "external-config", **kwargs)


def _coordinator(store: ConfigurationStore, **kwargs: object) -> DiagnosticCoordinator:
    return DiagnosticCoordinator(settings_revision=lambda: store.read().revision, **kwargs)


def _adapter(tmp_path: Path, **kwargs: object) -> SettingsDiagnosticsAdapter:
    store = _store(tmp_path)
    return SettingsDiagnosticsAdapter(
        store,
        _coordinator(store),
        startup_snapshot=store.read(),
        **kwargs,
    )


def test_snapshot_projects_canonical_values_and_safe_injected_identity(tmp_path: Path) -> None:
    location = RedactedLocationDTO(
        kind="database",
        public_id=None,
        label="Replay Analyzer database",
        configured=True,
        location_class="platform_default",
        basename="replay-analyzer.sqlite3",
        availability=AvailabilityDTO(state="unavailable", reason_codes=("sqlite_probe_unavailable",)),
    )
    component = ComponentIdentityDTO(
        component="analyzer",
        version="2.0.0",
        availability=AvailabilityDTO(state="available"),
    )
    adapter = _adapter(tmp_path, locations=(location,), components=(component,))

    snapshot = adapter.get_settings()

    assert snapshot.revision == 0
    assert tuple(value.key for value in snapshot.values) == (
        "import_mode",
        "minimum_longitudinal_sample_size",
        "movement_sample_frames",
        "ollama_model",
        "ollama_url",
    )
    assert all(value.source == "default" and value.editable for value in snapshot.values)
    assert snapshot.locations == (location,)
    assert snapshot.components == (component,)
    assert snapshot.model.endpoint == "http://127.0.0.1:11434"
    assert snapshot.model.model_name == "qwen3.6:27b"
    assert snapshot.model.availability.reason_codes == ("ollama_adapter_unavailable",)
    assert snapshot.restart_required is False
    assert snapshot.availability.state == "available"


def test_environment_and_composition_values_are_projected_read_only(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        environment={"GENERALS_REPLAY_ANALYZER_IMPORT_MODE": "reference"},
        composition_overrides={"movement_sample_frames": 30},
    )
    adapter = SettingsDiagnosticsAdapter(store, _coordinator(store), startup_snapshot=store.read())

    by_key = {value.key: value for value in adapter.get_settings().values}

    assert by_key["import_mode"].source == "environment"
    assert by_key["import_mode"].editable is False
    assert by_key["import_mode"].unavailable_reason_code == "settings_overridden_by_environment"
    assert by_key["movement_sample_frames"].source == "environment"
    assert by_key["movement_sample_frames"].editable is False
    assert by_key["movement_sample_frames"].unavailable_reason_code == "settings_overridden_by_composition"


def test_preview_is_side_effect_free_and_maps_exact_impact(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    command = SettingsPreviewCommandDTO(
        expected_revision=0,
        changes=(SettingChangeDTO(key="movement_sample_frames", value=30),),
    )

    impact = adapter.preview_settings(command)

    assert impact.expected_revision == 0
    assert len(impact.impact_digest) == 64
    assert impact.normalized_changes == command.changes
    assert impact.affected_stage_families == (
        "telemetry",
        "spatial",
        "features",
        "strategy",
        "longitudinal",
        "ollama",
        "report",
    )
    assert impact.invalidates_existing_results is True
    assert impact.requires_confirmation is True
    assert impact.restart_required is True
    assert {message.code for message in impact.messages} == {
        "settings_results_version_pinned",
        "settings_restart_required",
    }
    assert not (tmp_path / "external-config").exists()


def test_apply_rechecks_exact_impact_and_never_queues_analysis(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    changes = (SettingChangeDTO(key="import_mode", value="reference"),)
    impact = adapter.preview_settings(SettingsPreviewCommandDTO(expected_revision=0, changes=changes))

    mutation = adapter.apply_settings(
        ApplySettingsCommandDTO(
            expected_revision=0,
            changes=changes,
            expected_impact_digest=impact.impact_digest,
            confirm_invalidating_change=True,
        )
    )

    assert mutation.result_code == "updated"
    assert mutation.snapshot.revision == 1
    assert mutation.snapshot.restart_required is True
    assert mutation.impact == impact
    assert mutation.analysis_jobs_queued is False
    assert adapter.get_settings().revision == 1


def test_apply_noop_does_not_increment_revision(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    changes = (SettingChangeDTO(key="import_mode", value="copy"),)
    impact = adapter.preview_settings(SettingsPreviewCommandDTO(expected_revision=0, changes=changes))

    mutation = adapter.apply_settings(
        ApplySettingsCommandDTO(
            expected_revision=0,
            changes=changes,
            expected_impact_digest=impact.impact_digest,
            confirm_invalidating_change=True,
        )
    )

    assert mutation.result_code == "unchanged"
    assert mutation.snapshot.revision == 0
    assert mutation.snapshot.restart_required is False


@pytest.mark.parametrize(
    ("operation", "expected_status", "expected_code"),
    [
        ("preview_stale", 409, "settings_revision_conflict"),
        ("apply_digest", 409, "settings_impact_conflict"),
        ("environment", 409, "settings_overridden_by_environment"),
    ],
)
def test_store_failures_use_stable_redacted_public_problems(
    tmp_path: Path,
    operation: str,
    expected_status: int,
    expected_code: str,
) -> None:
    environment = (
        {"GENERALS_REPLAY_ANALYZER_IMPORT_MODE": "reference"} if operation == "environment" else {}
    )
    store = _store(tmp_path, environment=environment)
    adapter = SettingsDiagnosticsAdapter(store, _coordinator(store), startup_snapshot=store.read())
    changes = (SettingChangeDTO(key="import_mode", value="copy"),)

    with pytest.raises(PublicProblem) as caught:
        if operation == "preview_stale":
            adapter.preview_settings(SettingsPreviewCommandDTO(expected_revision=1, changes=changes))
        else:
            adapter.apply_settings(
                ApplySettingsCommandDTO(
                    expected_revision=0,
                    changes=changes,
                    expected_impact_digest="f" * 64,
                    confirm_invalidating_change=True,
                )
            )

    assert caught.value.status == expected_status
    assert caught.value.code == expected_code
    assert "external-config" not in caught.value.detail


def test_diagnostics_project_component_and_model_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    component = DiagnosticComponentIdentity(
        component="analyzer",
        version="2.0.0",
        content_digest="a" * 64,
        build_identity="release-2026.08",
    )
    model = DiagnosticModelIdentity(
        endpoint="http://[::1]:11434/",
        model_name="qwen3.6:27b",
        configured_model_digest="b" * 64,
        discovered_model_digest="b" * 64,
    )
    coordinator = _coordinator(
        store,
        probes={
            "engine_launch_version": lambda: ProbeOutcome("passed", "engine_version_ok", component=component),
            "ollama_model_available": lambda: ProbeOutcome("passed", "ollama_model_available", model=model),
        },
        monotonic=iter((1.0, 1.125, 2.0, 2.25)).__next__,
    )
    adapter = SettingsDiagnosticsAdapter(store, coordinator, startup_snapshot=store.read())

    engine = adapter.run_diagnostic(
        DiagnosticCommandDTO(kind="engine_launch_version", expected_settings_revision=0)
    )
    ollama = adapter.run_diagnostic(
        DiagnosticCommandDTO(kind="ollama_model_available", expected_settings_revision=0)
    )

    assert engine.component is not None
    assert engine.component.component == "analyzer"
    assert engine.component.availability.state == "available"
    assert engine.model is None
    assert engine.duration_milliseconds == 125
    assert ollama.model is not None
    assert ollama.model.endpoint == "http://[::1]:11434"
    assert ollama.model.discovered_model_digest == "b" * 64
    assert ollama.model.availability.state == "available"


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        ("settings_revision_conflict", 409),
        ("diagnostic_already_running", 429),
        ("diagnostic_rate_limited", 429),
        ("diagnostic_identity_invalid", 503),
    ],
)
def test_diagnostic_failures_use_stable_redacted_public_problems(
    tmp_path: Path,
    error_code: str,
    status: int,
) -> None:
    class FailingCoordinator:
        def run(self, *, kind: str, expected_settings_revision: int) -> object:
            del kind, expected_settings_revision
            raise DiagnosticsError(error_code)

    store = _store(tmp_path)
    adapter = SettingsDiagnosticsAdapter(  # type: ignore[arg-type]
        store,
        FailingCoordinator(),
        startup_snapshot=store.read(),
    )

    with pytest.raises(PublicProblem) as caught:
        adapter.run_diagnostic(DiagnosticCommandDTO(kind="sqlite_integrity", expected_settings_revision=0))

    assert caught.value.status == status
    assert caught.value.code == error_code
    assert error_code not in caught.value.detail


def test_unavailable_diagnostic_has_no_invented_identity(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)

    result = adapter.run_diagnostic(
        DiagnosticCommandDTO(kind="engine_launch_version", expected_settings_revision=0)
    )

    assert result.state == "unavailable"
    assert result.code == "engine_probe_not_supported"
    assert result.component is None
    assert result.model is None


def test_failed_diagnostic_identity_is_explicitly_unavailable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    model = DiagnosticModelIdentity(endpoint="http://127.0.0.1:11434", model_name="qwen3.6:27b")
    coordinator = _coordinator(
        store,
        probes={
            "ollama_model_available": lambda: ProbeOutcome(
                "failed",
                "ollama_model_unavailable",
                model=model,
            )
        },
    )
    adapter = SettingsDiagnosticsAdapter(store, coordinator, startup_snapshot=store.read())

    result = adapter.run_diagnostic(
        DiagnosticCommandDTO(kind="ollama_model_available", expected_settings_revision=0)
    )

    assert result.state == "failed"
    assert result.model is not None
    assert result.model.availability.state == "unavailable"
    assert result.model.availability.reason_codes == ("ollama_model_unavailable",)


def test_changed_model_configuration_clears_stale_discovery_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    adapter = SettingsDiagnosticsAdapter(
        store,
        _coordinator(store),
        startup_snapshot=store.read(),
        model_availability=AvailabilityDTO(state="available"),
        configured_model_digest="a" * 64,
        discovered_model_digest="a" * 64,
    )
    changes = (SettingChangeDTO(key="ollama_model", value="qwen3.6:8b"),)
    impact = adapter.preview_settings(SettingsPreviewCommandDTO(expected_revision=0, changes=changes))

    mutation = adapter.apply_settings(
        ApplySettingsCommandDTO(
            expected_revision=0,
            changes=changes,
            expected_impact_digest=impact.impact_digest,
            confirm_invalidating_change=True,
        )
    )

    assert mutation.snapshot.model.model_name == "qwen3.6:8b"
    assert mutation.snapshot.model.configured_model_digest is None
    assert mutation.snapshot.model.discovered_model_digest is None
    assert mutation.snapshot.model.availability.reason_codes == ("settings_restart_required",)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("settings_change_invalid", 422),
        ("settings_confirmation_required", 422),
        ("settings_busy", 429),
        ("settings_document_invalid", 503),
    ],
)
def test_remaining_store_failures_are_allow_listed(tmp_path: Path, code: str, status: int) -> None:
    delegate = _store(tmp_path)

    class FailingStore:
        def read(self) -> object:
            return delegate.read()

        def preview(self, **_kwargs: object) -> object:
            raise SettingsStoreError(code)

    adapter = SettingsDiagnosticsAdapter(  # type: ignore[arg-type]
        FailingStore(),
        _coordinator(delegate),
        startup_snapshot=delegate.read(),
    )

    with pytest.raises(PublicProblem) as caught:
        adapter.preview_settings(
            SettingsPreviewCommandDTO(
                expected_revision=0,
                changes=(SettingChangeDTO(key="import_mode", value="reference"),),
            )
        )

    assert caught.value.status == status
    assert caught.value.code == code


def test_unexpected_store_and_diagnostic_failures_are_redacted(tmp_path: Path) -> None:
    delegate = _store(tmp_path)

    class ExplodingStore:
        def read(self) -> object:
            raise RuntimeError("C:/private/settings.json secret-token")

    class ExplodingCoordinator:
        def run(self, **_kwargs: object) -> object:
            raise RuntimeError("stderr C:/private/engine.exe secret-token")

    adapter = SettingsDiagnosticsAdapter(  # type: ignore[arg-type]
        ExplodingStore(),
        ExplodingCoordinator(),  # type: ignore[arg-type]
        startup_snapshot=delegate.read(),
    )

    with pytest.raises(PublicProblem) as settings_problem:
        adapter.get_settings()
    with pytest.raises(PublicProblem) as diagnostic_problem:
        adapter.run_diagnostic(DiagnosticCommandDTO(kind="sqlite_integrity", expected_settings_revision=0))

    assert settings_problem.value.code == "settings_adapter_unavailable"
    assert diagnostic_problem.value.code == "diagnostics_adapter_unavailable"
    assert "private" not in settings_problem.value.detail.casefold()
    assert "private" not in diagnostic_problem.value.detail.casefold()


def test_snapshot_read_redacts_store_failure_after_frozen_startup_identity(tmp_path: Path) -> None:
    class ExplodingStore:
        def read(self) -> object:
            raise RuntimeError("C:/private/settings.json secret-token")

    adapter = SettingsDiagnosticsAdapter(  # type: ignore[arg-type]
        ExplodingStore(),
        object(),
        startup_snapshot=_store(tmp_path).read(),
    )

    with pytest.raises(PublicProblem) as caught:
        adapter.get_settings()

    assert caught.value.code == "settings_adapter_unavailable"
    assert "private" not in caught.value.detail.casefold()


def test_unknown_core_error_codes_are_not_exposed(tmp_path: Path) -> None:
    delegate = _store(tmp_path)

    class UnknownStoreFailure:
        def read(self) -> object:
            return delegate.read()

        def preview(self, **_kwargs: object) -> object:
            raise SettingsStoreError("secret-token-C:/private/settings.json")

    class UnknownDiagnosticFailure:
        def run(self, **_kwargs: object) -> object:
            raise DiagnosticsError("secret-token-C:/private/model-response")

    settings_adapter = SettingsDiagnosticsAdapter(  # type: ignore[arg-type]
        UnknownStoreFailure(),
        _coordinator(delegate),
        startup_snapshot=delegate.read(),
    )
    diagnostic_adapter = SettingsDiagnosticsAdapter(  # type: ignore[arg-type]
        delegate,
        UnknownDiagnosticFailure(),  # type: ignore[arg-type]
        startup_snapshot=delegate.read(),
    )

    with pytest.raises(PublicProblem) as settings_problem:
        settings_adapter.preview_settings(
            SettingsPreviewCommandDTO(
                expected_revision=0,
                changes=(SettingChangeDTO(key="import_mode", value="reference"),),
            )
        )
    with pytest.raises(PublicProblem) as diagnostic_problem:
        diagnostic_adapter.run_diagnostic(
            DiagnosticCommandDTO(kind="sqlite_integrity", expected_settings_revision=0)
        )

    assert settings_problem.value.code == "settings_adapter_unavailable"
    assert diagnostic_problem.value.code == "diagnostics_adapter_unavailable"
    assert "secret" not in settings_problem.value.code
    assert "secret" not in diagnostic_problem.value.code
