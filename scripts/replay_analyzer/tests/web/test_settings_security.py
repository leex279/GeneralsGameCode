"""Settings-route input, side-effect, redaction, and capability security."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import OneTimeFormTokenRegistry
from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, install_problem_handlers
from generals_replay_analyzer.web.ports import (
    ApplySettingsCommandDTO,
    AvailabilityDTO,
    DiagnosticCommandDTO,
    DiagnosticResultDTO,
    ModelIdentityDTO,
    SettingsImpactDTO,
    SettingsMutationDTO,
    SettingsPreviewCommandDTO,
    SettingsSnapshotDTO,
    SettingValueDTO,
)
from generals_replay_analyzer.web.routes import settings


def _snapshot() -> SettingsSnapshotDTO:
    values = (
        SettingValueDTO(key="import_mode", value="copy", source="default", editable=True),
        SettingValueDTO(key="minimum_longitudinal_sample_size", value=5, source="default", editable=True),
        SettingValueDTO(key="movement_sample_frames", value=15, source="default", editable=True),
        SettingValueDTO(key="ollama_model", value="qwen3.6:27b", source="default", editable=True),
        SettingValueDTO(key="ollama_url", value="http://127.0.0.1:11434", source="default", editable=True),
    )
    return SettingsSnapshotDTO(
        schema_version=1,
        revision=0,
        effective_settings_digest="a" * 64,
        values=values,
        locations=(),
        components=(),
        model=ModelIdentityDTO(
            provider="ollama",
            endpoint="http://127.0.0.1:11434",
            model_name="qwen3.6:27b",
            availability=AvailabilityDTO(state="unavailable", reason_codes=("ollama_adapter_unavailable",)),
        ),
        restart_required=False,
        availability=AvailabilityDTO(state="available"),
    )


class _SecurityPort:
    def __init__(self, diagnostic_problem: PublicProblem | None = None) -> None:
        self.calls: list[str] = []
        self._diagnostic_problem = diagnostic_problem

    def get_settings(self) -> SettingsSnapshotDTO:
        self.calls.append("get")
        return _snapshot()

    def preview_settings(self, command: SettingsPreviewCommandDTO) -> SettingsImpactDTO:
        self.calls.append("preview")
        return SettingsImpactDTO(
            expected_revision=command.expected_revision,
            impact_digest="b" * 64,
            normalized_changes=command.changes,
            affected_stage_families=("future_imports",),
            invalidates_existing_results=False,
            requires_confirmation=True,
            restart_required=True,
            messages=(),
        )

    def apply_settings(self, command: ApplySettingsCommandDTO) -> SettingsMutationDTO:
        self.calls.append("apply")
        raise AssertionError(command)

    def run_diagnostic(self, command: DiagnosticCommandDTO) -> DiagnosticResultDTO:
        self.calls.append("diagnostic")
        if self._diagnostic_problem is not None:
            raise self._diagnostic_problem
        raise AssertionError(command)


def _client(port: object) -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(settings.router)
    app.state.form_csrf_token_registry = OneTimeFormTokenRegistry()
    app.dependency_overrides[application_port] = lambda: port
    return TestClient(app)


def test_native_form_csrf_rejection_happens_before_port_call() -> None:
    """Catch a direct form submission reaching an application capability without one-time CSRF proof."""
    port = _SecurityPort()

    response = _client(port).post(
        "/settings/preview",
        data={"expected_revision": "0", "setting_key": "import_mode", "setting_value": "reference"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"
    assert port.calls == []


def test_unknown_repeated_and_oversize_form_values_fail_before_port_call() -> None:
    """Catch scalar collapse, extra fields, or unbounded candidates at the form boundary."""
    candidates = (
        {"expected_revision": "0", "setting_key": "import_mode", "setting_value": "copy", "data_root": "x"},
        {"expected_revision": ["0", "1"], "setting_key": "import_mode", "setting_value": "copy"},
        {"expected_revision": "0", "setting_key": "ollama_model", "setting_value": "x" * 513},
        {
            "expected_revision": "0",
            "setting_key": ["import_mode", "import_mode"],
            "setting_value": ["copy", "reference"],
        },
    )
    for data in candidates:
        port = _SecurityPort()
        response = _client(port).post(
            "/settings/preview",
            data=data,
            headers={"x-csrf-token": "middleware-validated"},
        )
        assert response.status_code == 422
        assert port.calls == []


def test_invalid_candidate_is_never_echoed_in_problem_body() -> None:
    """Catch a rejected path, credential, or raw model candidate leaking through a generic problem."""
    candidate = "http://user:secret@remote.invalid:11434/C:/private/response"
    port = _SecurityPort()

    response = _client(port).post(
        "/settings/preview",
        data={"expected_revision": "0", "setting_key": "ollama_url", "setting_value": candidate},
        headers={"x-csrf-token": "middleware-validated"},
    )

    assert response.status_code == 422
    assert candidate not in response.text
    assert "secret" not in response.text.casefold()
    assert "private" not in response.text.casefold()
    assert port.calls == []


def test_diagnostic_problem_statuses_remain_stable_and_redacted() -> None:
    """Catch concurrency or dependency refusal being collapsed into success or raw adapter detail."""
    for status, code in ((429, "diagnostic_already_running"), (503, "ollama_adapter_unavailable")):
        port = _SecurityPort(
            PublicProblem(status=status, code=code, detail="The requested diagnostic is unavailable")
        )
        response = _client(port).post(
            "/settings/diagnostics/ollama_model_available",
            data={"expected_settings_revision": "0"},
            headers={"x-csrf-token": "middleware-validated"},
        )
        assert response.status_code == status
        assert response.json()["code"] == code
        assert port.calls == ["diagnostic"]


def test_active_diagnostics_are_post_only_and_unknown_kind_is_rejected() -> None:
    """Catch a crawler-triggered GET or open diagnostic name invoking a capability."""
    port = _SecurityPort()
    client = _client(port)

    get_response = client.get("/settings/diagnostics/sqlite_integrity")
    unknown_response = client.post(
        "/settings/diagnostics/run-arbitrary-command",
        data={"expected_settings_revision": "0"},
        headers={"x-csrf-token": "middleware-validated"},
    )

    assert get_response.status_code in {400, 404, 405}
    assert unknown_response.status_code == 422
    assert port.calls == []


def test_settings_html_contains_only_local_assets_and_no_private_locator() -> None:
    """Catch a CDN, remote script, or path-bearing hidden value on the settings page."""
    port = _SecurityPort()

    response = _client(port).get("/settings")

    assert response.status_code == 200
    assert 'src="/static/' in response.text
    assert 'href="/static/' in response.text
    assert "https://" not in response.text
    assert "C:/" not in response.text
    assert "\\Users\\" not in response.text
    assert port.calls == ["get"]
