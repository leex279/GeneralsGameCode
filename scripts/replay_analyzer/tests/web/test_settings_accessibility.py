"""Semantic and no-JavaScript acceptance for analyzer settings."""

from __future__ import annotations

from html.parser import HTMLParser

from fastapi import FastAPI
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import OneTimeFormTokenRegistry
from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import install_problem_handlers
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


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {name: value or "" for name, value in attrs}))

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text_parts.append(data.strip())

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)


def _snapshot() -> SettingsSnapshotDTO:
    return SettingsSnapshotDTO(
        schema_version=1,
        revision=0,
        effective_settings_digest="a" * 64,
        values=(
            SettingValueDTO(key="import_mode", value="copy", source="default", editable=True),
            SettingValueDTO(key="minimum_longitudinal_sample_size", value=5, source="default", editable=True),
            SettingValueDTO(key="movement_sample_frames", value=15, source="default", editable=True),
            SettingValueDTO(key="ollama_model", value="qwen3.6:27b", source="default", editable=True),
            SettingValueDTO(key="ollama_url", value="http://127.0.0.1:11434", source="default", editable=True),
        ),
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


class _Port:
    def get_settings(self) -> SettingsSnapshotDTO:
        return _snapshot()

    def preview_settings(self, command: SettingsPreviewCommandDTO) -> SettingsImpactDTO:
        return SettingsImpactDTO(
            expected_revision=command.expected_revision,
            impact_digest="b" * 64,
            normalized_changes=command.changes,
            affected_stage_families=("telemetry", "spatial", "features", "strategy", "longitudinal", "ollama", "report"),
            invalidates_existing_results=True,
            requires_confirmation=True,
            restart_required=True,
            messages=(),
        )

    def apply_settings(self, command: ApplySettingsCommandDTO) -> SettingsMutationDTO:
        raise AssertionError(command)

    def run_diagnostic(self, command: DiagnosticCommandDTO) -> DiagnosticResultDTO:
        raise AssertionError(command)


def _client() -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(settings.router)
    app.state.form_csrf_token_registry = OneTimeFormTokenRegistry()
    app.dependency_overrides[application_port] = _Port
    return TestClient(app)


def _parse(html: str) -> _Document:
    document = _Document()
    document.feed(html)
    return document


def test_settings_page_has_one_h1_labelled_sections_tables_and_textual_states() -> None:
    """Catch card-only structure, missing headings, or availability communicated by color alone."""
    document = _parse(_client().get("/settings").text)

    assert sum(tag == "h1" for tag, _attrs in document.tags) == 1
    for heading in (
        "Configuration identity",
        "Editable settings",
        "Redacted locations",
        "Component identities",
        "Optional local model",
        "Diagnostics",
    ):
        assert heading in document.text
    assert sum(tag == "caption" for tag, _attrs in document.tags) == 4
    assert "unavailable" in document.text.casefold()
    assert "ollama_adapter_unavailable" in document.text


def test_every_editable_control_has_a_visible_label_help_and_native_submit_action() -> None:
    """Catch unlabeled controls or a JavaScript-only settings workflow."""
    document = _parse(_client().get("/settings").text)
    control_ids = {
        attrs["id"]
        for tag, attrs in document.tags
        if tag in {"input", "select"} and attrs.get("type") != "hidden" and attrs.get("name") == "setting_value"
    }
    label_targets = {attrs["for"] for tag, attrs in document.tags if tag == "label" and attrs.get("for")}
    forms = [attrs for tag, attrs in document.tags if tag == "form"]
    buttons = [text for text in document.text_parts if text.startswith(("Preview ", "Run "))]

    assert len(control_ids) == 5
    assert control_ids <= label_targets
    assert all(f"help-{control_id.removeprefix('setting-')}" in {attrs.get("aria-describedby") for attrs in forms} for control_id in control_ids)
    assert len([attrs for attrs in forms if attrs.get("action") == "/settings/preview"]) == 5
    assert len([attrs for attrs in forms if attrs.get("action", "").startswith("/settings/diagnostics/")]) == 5
    assert len(buttons) >= 10


def test_status_regions_optional_offline_message_and_skip_link_remain_visible() -> None:
    """Catch hidden-only feedback, lost shell keyboard navigation, or Ollama presented as mandatory."""
    document = _parse(_client().get("/settings").text)
    live_regions = [attrs for _tag, attrs in document.tags if attrs.get("role") == "status"]
    skip_links = [attrs for tag, attrs in document.tags if tag == "a" and attrs.get("class") == "skip-link"]
    current_links = [attrs for tag, attrs in document.tags if tag == "a" and attrs.get("aria-current") == "page"]

    assert len(live_regions) >= 2
    assert all(region.get("aria-live") == "polite" for region in live_regions)
    assert skip_links == [{"class": "skip-link", "href": "#main-content"}]
    assert len(current_links) <= 1
    assert "Ollama is optional" in document.text
    assert "remain available offline" in document.text


def test_impact_fragment_has_explicit_stage_confirmation_and_no_javascript_dependency() -> None:
    """Catch an apply action that omits impact, confirmation, or native form semantics."""
    response = _client().post(
        "/settings/preview",
        data={"expected_revision": "0", "setting_key": "movement_sample_frames", "setting_value": "30"},
        headers={"x-csrf-token": "middleware-validated"},
    )
    document = _parse(response.text)

    assert response.status_code == 200
    assert "Affected analysis stages" in document.text
    for family in ("telemetry", "spatial", "features", "strategy", "longitudinal", "ollama", "report"):
        assert family in document.text
    apply_forms = [attrs for tag, attrs in document.tags if tag == "form" and attrs.get("action") == "/settings/apply"]
    confirmations = [
        attrs
        for tag, attrs in document.tags
        if tag == "input" and attrs.get("name") == "confirm_invalidating_change" and "required" in attrs
    ]
    assert len(apply_forms) == 1
    assert len(confirmations) == 1
    assert "Apply confirmed change" in document.text
    assert all(tag != "script" for tag, _attrs in document.tags)
