"""Immutable settings and diagnostics web-boundary contracts."""

from __future__ import annotations

import re
from typing import Protocol

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from generals_replay_analyzer.web.app import OneTimeFormTokenRegistry
from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import install_problem_handlers
from generals_replay_analyzer.web.ports import (
    ApplySettingsCommandDTO,
    AvailabilityDTO,
    ComponentIdentityDTO,
    DiagnosticCommandDTO,
    DiagnosticResultDTO,
    DiagnosticsCommandPort,
    ModelIdentityDTO,
    RedactedLocationDTO,
    SettingChangeDTO,
    SettingsCommandPort,
    SettingsImpactDTO,
    SettingsMutationDTO,
    SettingsPreviewCommandDTO,
    SettingsQueryPort,
    SettingsSnapshotDTO,
    SettingValueDTO,
)
from generals_replay_analyzer.web.routes import settings


def _available() -> AvailabilityDTO:
    return AvailabilityDTO(state="available")


def _values() -> tuple[SettingValueDTO, ...]:
    return (
        SettingValueDTO(key="ollama_url", value="http://127.0.0.1:11434", source="default", editable=True),
        SettingValueDTO(key="ollama_model", value="qwen3.6:27b", source="default", editable=True),
        SettingValueDTO(key="movement_sample_frames", value=15, source="default", editable=True),
        SettingValueDTO(key="import_mode", value="copy", source="default", editable=True),
        SettingValueDTO(key="minimum_longitudinal_sample_size", value=5, source="default", editable=True),
    )


def _model() -> ModelIdentityDTO:
    return ModelIdentityDTO(
        provider="ollama",
        endpoint="http://127.0.0.1:11434",
        model_name="qwen3.6:27b",
        configured_model_digest=None,
        discovered_model_digest=None,
        availability=AvailabilityDTO(state="unavailable", reason_codes=("ollama_adapter_unavailable",)),
    )


def _snapshot(revision: int = 0) -> SettingsSnapshotDTO:
    return SettingsSnapshotDTO(
        schema_version=1,
        revision=revision,
        effective_settings_digest="a" * 64,
        values=_values(),
        locations=(),
        components=(),
        model=_model(),
        restart_required=False,
        availability=_available(),
    )


def _impact(revision: int = 0) -> SettingsImpactDTO:
    return SettingsImpactDTO(
        expected_revision=revision,
        impact_digest="b" * 64,
        normalized_changes=(SettingChangeDTO(key="movement_sample_frames", value=30),),
        affected_stage_families=("report", "telemetry", "spatial", "features", "strategy", "longitudinal", "ollama"),
        invalidates_existing_results=True,
        requires_confirmation=True,
        restart_required=True,
        messages=(),
    )


def test_setting_change_normalizes_closed_values_and_preview_order() -> None:
    """Catch policy drift between browser candidates and canonical command values."""
    preview = SettingsPreviewCommandDTO(
        expected_revision=2,
        changes=(
            SettingChangeDTO(key="ollama_url", value="http://[::1]:11434/"),
            SettingChangeDTO(key="import_mode", value="reference"),
        ),
    )

    assert tuple(change.key for change in preview.changes) == ("import_mode", "ollama_url")
    assert preview.changes[1].value == "http://[::1]:11434"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("unknown", "copy"),
        ("movement_sample_frames", True),
        ("movement_sample_frames", 0),
        ("movement_sample_frames", 3601),
        ("minimum_longitudinal_sample_size", 100_001),
        ("import_mode", "move"),
        ("ollama_url", "http://localhost:11434"),
        ("ollama_model", "../model"),
    ],
)
def test_setting_change_rejects_open_or_wrong_typed_candidates(key: str, value: object) -> None:
    """Catch an unknown, path-like, aliased, bool-as-int, or out-of-range browser value."""
    with pytest.raises(ValidationError):
        SettingChangeDTO(key=key, value=value)


def test_preview_rejects_empty_and_duplicate_changes() -> None:
    """Catch ambiguous last-write-wins commands or meaningless previews."""
    with pytest.raises(ValidationError):
        SettingsPreviewCommandDTO(expected_revision=0, changes=())
    with pytest.raises(ValidationError):
        SettingsPreviewCommandDTO(
            expected_revision=0,
            changes=(
                SettingChangeDTO(key="import_mode", value="copy"),
                SettingChangeDTO(key="import_mode", value="reference"),
            ),
        )


def test_environment_setting_is_read_only_and_requires_a_stable_reason() -> None:
    """Catch an environment-owned effective value being presented as editable."""
    with pytest.raises(ValidationError):
        SettingValueDTO(
            key="ollama_model",
            value="model:1",
            source="environment",
            editable=True,
        )
    value = SettingValueDTO(
        key="ollama_model",
        value="model:1",
        source="environment",
        editable=False,
        unavailable_reason_code="settings_overridden_by_environment",
    )
    assert value.editable is False


def test_snapshot_contains_exactly_one_canonical_value_per_safe_key_and_is_frozen() -> None:
    """Catch incomplete, duplicate, nondeterministic, or mutable effective settings snapshots."""
    snapshot = _snapshot()

    assert tuple(value.key for value in snapshot.values) == (
        "import_mode",
        "minimum_longitudinal_sample_size",
        "movement_sample_frames",
        "ollama_model",
        "ollama_url",
    )
    with pytest.raises(ValidationError):
        SettingsSnapshotDTO(
            schema_version=1,
            revision=0,
            effective_settings_digest="a" * 64,
            values=_values()[:-1],
            locations=(),
            components=(),
            model=_model(),
            restart_required=False,
            availability=_available(),
        )
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.revision = 1  # type: ignore[misc]


def test_redacted_location_and_component_reject_paths_and_unapproved_basenames() -> None:
    """Catch an absolute locator or arbitrary source-directory basename reaching the browser."""
    with pytest.raises(ValidationError):
        RedactedLocationDTO(
            kind="database",
            public_id=None,
            label="C:/Users/private/replay.sqlite3",
            configured=True,
            location_class="custom_external",
            basename="replay-analyzer.sqlite3",
            availability=_available(),
        )
    with pytest.raises(ValidationError):
        RedactedLocationDTO(
            kind="watched_folder",
            public_id="123e4567-e89b-42d3-a456-426614174000",
            label="Replay folder 1",
            configured=True,
            location_class="custom_external",
            basename="private-folder",
            availability=_available(),
        )
    with pytest.raises(ValidationError):
        ComponentIdentityDTO(
            component="analyzer",
            version="C:/private/build",
            content_digest=None,
            build_identity=None,
            availability=_available(),
        )


def test_model_identity_enforces_literal_loopback_model_grammar_and_exact_digests() -> None:
    """Catch a remote endpoint, path-like model, or fabricated digest in the public identity."""
    model = ModelIdentityDTO(
        provider="ollama",
        endpoint="http://[::1]:11434/",
        model_name="namespace/model:latest",
        configured_model_digest="c" * 64,
        discovered_model_digest="c" * 64,
        availability=_available(),
    )
    assert model.endpoint == "http://[::1]:11434"
    for changes in (
        {"endpoint": "http://localhost:11434", "model_name": "model:1"},
        {"endpoint": "http://127.0.0.1:11434", "model_name": "../model"},
        {"endpoint": "http://127.0.0.1:11434", "model_name": "model:1", "discovered_model_digest": "bad"},
    ):
        with pytest.raises(ValidationError):
            ModelIdentityDTO(provider="ollama", availability=_available(), **changes)


def test_impact_and_apply_are_canonical_and_require_exact_confirmation() -> None:
    """Catch template-side impact sorting or apply commands without literal invalidation consent."""
    impact = _impact()
    assert impact.affected_stage_families == (
        "telemetry",
        "spatial",
        "features",
        "strategy",
        "longitudinal",
        "ollama",
        "report",
    )
    with pytest.raises(ValidationError):
        ApplySettingsCommandDTO(
            expected_revision=0,
            changes=impact.normalized_changes,
            expected_impact_digest=impact.impact_digest,
            confirm_invalidating_change=False,
        )


def test_mutation_can_never_claim_automatic_jobs() -> None:
    """Catch external settings persistence pretending to atomically queue SQLite analysis work."""
    mutation = SettingsMutationDTO(
        result_code="updated",
        snapshot=_snapshot(revision=1),
        impact=_impact(),
        analysis_jobs_queued=False,
    )
    assert mutation.analysis_jobs_queued is False
    with pytest.raises(ValidationError):
        SettingsMutationDTO(
            result_code="updated",
            snapshot=_snapshot(revision=1),
            impact=_impact(),
            analysis_jobs_queued=True,
        )


def test_diagnostic_command_and_result_are_closed_revision_scoped_and_redacted() -> None:
    """Catch open diagnostic actions, stale-negative revisions, or private details in a result."""
    command = DiagnosticCommandDTO(kind="sqlite_integrity", expected_settings_revision=4)
    result = DiagnosticResultDTO(
        kind=command.kind,
        state="failed",
        code="sqlite_integrity_failed",
        message="The configured database failed its read-only integrity check.",
        settings_revision=4,
        component=None,
        model=None,
        duration_milliseconds=12,
    )
    assert result.settings_revision == 4
    for changes in (
        {"kind": "run_command", "expected_settings_revision": 4},
        {"kind": "sqlite_integrity", "expected_settings_revision": -1},
    ):
        with pytest.raises(ValidationError):
            DiagnosticCommandDTO(**changes)
    with pytest.raises(ValidationError):
        DiagnosticResultDTO(
            kind="sqlite_integrity",
            state="failed",
            code="sqlite_integrity_failed",
            message="C:/Users/private/replay.sqlite3 token=secret",
            settings_revision=4,
            component=None,
            model=None,
            duration_milliseconds=12,
        )


def test_three_settings_capabilities_are_runtime_protocols() -> None:
    """Catch route seams that cannot be injected or checked without concrete infrastructure types."""
    assert issubclass(SettingsQueryPort, Protocol)
    assert issubclass(SettingsCommandPort, Protocol)
    assert issubclass(DiagnosticsCommandPort, Protocol)
    assert getattr(SettingsQueryPort, "_is_runtime_protocol", False)
    assert getattr(SettingsCommandPort, "_is_runtime_protocol", False)
    assert getattr(DiagnosticsCommandPort, "_is_runtime_protocol", False)


class _SettingsPort:
    def __init__(self) -> None:
        self.snapshot = _snapshot()
        self.get_calls = 0
        self.previews: list[SettingsPreviewCommandDTO] = []
        self.applies: list[ApplySettingsCommandDTO] = []
        self.diagnostics: list[DiagnosticCommandDTO] = []

    def get_settings(self) -> SettingsSnapshotDTO:
        self.get_calls += 1
        return self.snapshot

    def preview_settings(self, command: SettingsPreviewCommandDTO) -> SettingsImpactDTO:
        self.previews.append(command)
        return _impact(command.expected_revision).model_copy(update={"normalized_changes": command.changes})

    def apply_settings(self, command: ApplySettingsCommandDTO) -> SettingsMutationDTO:
        self.applies.append(command)
        self.snapshot = _snapshot(revision=command.expected_revision + 1).model_copy(update={"restart_required": True})
        return SettingsMutationDTO(
            result_code="updated",
            snapshot=self.snapshot,
            impact=_impact(command.expected_revision).model_copy(update={"normalized_changes": command.changes}),
            analysis_jobs_queued=False,
        )

    def run_diagnostic(self, command: DiagnosticCommandDTO) -> DiagnosticResultDTO:
        self.diagnostics.append(command)
        return DiagnosticResultDTO(
            kind=command.kind,
            state="passed",
            code="diagnostic_passed",
            message="The explicit bounded diagnostic passed.",
            settings_revision=command.expected_settings_revision,
            component=None,
            model=None,
            duration_milliseconds=4,
        )


def _client(port: object) -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(settings.router)
    app.state.form_csrf_token_registry = OneTimeFormTokenRegistry()

    def override_port() -> object:
        return port

    app.dependency_overrides[application_port] = override_port
    return TestClient(app)


def test_settings_get_reads_only_snapshot_and_renders_safe_identity() -> None:
    """Catch GET running a diagnostic/mutation or omitting immutable configuration identity."""
    port = _SettingsPort()

    response = _client(port).get("/settings", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert port.get_calls == 1
    assert port.previews == []
    assert port.applies == []
    assert port.diagnostics == []
    assert "Configuration identity" in response.text
    assert "Revision 0" in response.text
    assert "a" * 64 in response.text
    assert "Ollama is optional" in response.text
    assert response.cookies.get("_csrf")
    tokens = re.findall(r'name="_csrf" value="([^"]+)"', response.text)
    assert len(tokens) > 1
    assert len(tokens) == len(set(tokens))


def test_settings_preview_normalizes_one_change_without_writing() -> None:
    """Catch preview mutating settings or passing raw, unnormalized browser values."""
    port = _SettingsPort()

    response = _client(port).post(
        "/settings/preview",
        data={"expected_revision": "0", "setting_key": "ollama_url", "setting_value": "http://[::1]:11434/"},
        headers={"accept": "text/html", "x-csrf-token": "accepted-by-isolated-route"},
    )

    assert response.status_code == 200
    assert len(port.previews) == 1
    assert port.previews[0].changes == (SettingChangeDTO(key="ollama_url", value="http://[::1]:11434"),)
    assert port.applies == []
    assert "Affected analysis stages" in response.text
    assert "Apply confirmed change" in response.text


def test_settings_apply_requires_exact_preview_identity_and_returns_refreshed_page() -> None:
    """Catch apply redirect repetition, missing confirmation, or hidden automatic job claims."""
    port = _SettingsPort()

    response = _client(port).post(
        "/settings/apply",
        data={
            "expected_revision": "0",
            "setting_key": "movement_sample_frames",
            "setting_value": "30",
            "expected_impact_digest": "b" * 64,
            "confirm_invalidating_change": "true",
        },
        headers={"accept": "text/html", "x-csrf-token": "accepted-by-isolated-route"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert len(port.applies) == 1
    assert port.applies[0].changes == (SettingChangeDTO(key="movement_sample_frames", value=30),)
    assert "Revision 1" in response.text
    assert "Existing outputs remain version-pinned" in response.text
    assert "No analysis jobs were queued" in response.text
    assert response.headers.get("location") is None


def test_settings_diagnostic_uses_closed_path_kind_and_current_revision() -> None:
    """Catch browser-supplied diagnostic inputs or a GET-like active action."""
    port = _SettingsPort()

    response = _client(port).post(
        "/settings/diagnostics/sqlite_integrity",
        data={"expected_settings_revision": "0"},
        headers={"accept": "text/html", "x-csrf-token": "accepted-by-isolated-route"},
    )

    assert response.status_code == 200
    assert port.diagnostics == [DiagnosticCommandDTO(kind="sqlite_integrity", expected_settings_revision=0)]
    assert "explicit bounded diagnostic passed" in response.text


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/settings/preview", {"expected_revision": "0", "setting_key": "data_root", "setting_value": "x"}),
        ("/settings/preview", {"expected_revision": "0", "setting_key": "movement_sample_frames", "setting_value": "true"}),
        (
            "/settings/apply",
            {
                "expected_revision": "0",
                "setting_key": "import_mode",
                "setting_value": "copy",
                "expected_impact_digest": "b" * 64,
            },
        ),
        ("/settings/diagnostics/run-command", {"expected_settings_revision": "0"}),
    ],
)
def test_settings_routes_reject_invalid_forms_before_port_calls(path: str, data: dict[str, str]) -> None:
    """Catch unknown/path-bearing/malformed settings or diagnostic actions reaching a port."""
    port = _SettingsPort()

    response = _client(port).post(
        path,
        data=data,
        headers={"accept": "text/html", "x-csrf-token": "accepted-by-isolated-route"},
    )

    assert response.status_code == 422
    assert port.previews == []
    assert port.applies == []
    assert port.diagnostics == []
