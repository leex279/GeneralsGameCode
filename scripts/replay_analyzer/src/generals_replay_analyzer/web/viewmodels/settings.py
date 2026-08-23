"""Frozen, path-free presentation values for analyzer settings and diagnostics."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.web.ports import DiagnosticKind, SettingsSnapshotDTO, SettingValueDTO
from generals_replay_analyzer.web.presentation.shell import ShellContextDTO, feature_shell


class SettingsControlViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    setting: SettingValueDTO
    label: str
    help_text: str
    input_kind: Literal["text", "number", "select"]
    rendered_value: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()


class DiagnosticActionViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DiagnosticKind
    label: str
    description: str


class SettingsPageViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: SettingsSnapshotDTO
    controls: tuple[SettingsControlViewModel, ...]
    diagnostics: tuple[DiagnosticActionViewModel, ...]


_CONTROL_METADATA: dict[str, tuple[str, str, Literal["text", "number", "select"], int | None, int | None, tuple[str, ...]]] = {
    "import_mode": (
        "Replay import mode",
        "Applies only to future replay imports; existing provenance stays unchanged.",
        "select",
        None,
        None,
        ("copy", "reference"),
    ),
    "minimum_longitudinal_sample_size": (
        "Minimum longitudinal sample size",
        "Controls when multi-match claims become eligible; observations are not rewritten.",
        "number",
        1,
        100_000,
        (),
    ),
    "movement_sample_frames": (
        "Movement sample interval (frames)",
        "Changes telemetry identity and every downstream spatial analysis for future runs.",
        "number",
        1,
        3600,
        (),
    ),
    "ollama_model": (
        "Local Ollama model",
        "Exact local model identifier used only when optional model analysis is requested.",
        "text",
        None,
        None,
        (),
    ),
    "ollama_url": (
        "Local Ollama endpoint",
        "Only an explicit literal IPv4 or IPv6 loopback endpoint is accepted.",
        "text",
        None,
        None,
        (),
    ),
}

_DIAGNOSTICS = (
    DiagnosticActionViewModel(
        kind="data_root_writable",
        label="Test data-root write",
        description="Creates, verifies, and removes one bounded owned probe file.",
    ),
    DiagnosticActionViewModel(
        kind="sqlite_integrity",
        label="Check database integrity",
        description="Runs accepted read-only schema, foreign-key, and integrity checks.",
    ),
    DiagnosticActionViewModel(
        kind="engine_launch_version",
        label="Probe engine version",
        description="Uses only an accepted replay-free fixed version probe when available.",
    ),
    DiagnosticActionViewModel(
        kind="ollama_model_available",
        label="Check exact Ollama model",
        description="Checks exact local model availability and digest through the bounded adapter.",
    ),
    DiagnosticActionViewModel(
        kind="ollama_minimal_generation",
        label="Run minimal Ollama generation",
        description="Runs a fixed schema-constrained local generation with no replay data.",
    ),
)


# TheSuperHackers @feature Leex 23/08/2026 Present only immutable safe settings and explicit diagnostic consequences. (#TBD)
def settings_view(snapshot: SettingsSnapshotDTO) -> SettingsPageViewModel:
    controls = []
    for setting in snapshot.values:
        label, help_text, input_kind, minimum, maximum, choices = _CONTROL_METADATA[setting.key]
        controls.append(
            SettingsControlViewModel(
                setting=setting,
                label=label,
                help_text=help_text,
                input_kind=input_kind,
                rendered_value=str(setting.value),
                minimum=minimum,
                maximum=maximum,
                choices=choices,
            )
        )
    return SettingsPageViewModel(snapshot=snapshot, controls=tuple(controls), diagnostics=_DIAGNOSTICS)


def settings_shell(snapshot: SettingsSnapshotDTO) -> ShellContextDTO:
    return feature_shell(
        page_title="Analyzer settings | Generals Replay Analyzer",
        current_path="/settings",
        availability=snapshot.availability,
    )
