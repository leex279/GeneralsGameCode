"""Injected, revision-scoped, redacted component diagnostic contracts."""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError

import pytest

from generals_replay_analyzer.diagnostics import (
    DiagnosticComponentIdentity,
    DiagnosticCoordinator,
    DiagnosticModelIdentity,
    DiagnosticsError,
    ProbeOutcome,
)


class _Probe:
    def __init__(self, outcome: ProbeOutcome | BaseException) -> None:
        self.outcome = outcome
        self.calls = 0

    def __call__(self) -> ProbeOutcome:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("data_root_writable", "data_root_probe_unavailable"),
        ("sqlite_integrity", "sqlite_probe_unavailable"),
        ("engine_launch_version", "engine_probe_not_supported"),
        ("ollama_model_available", "ollama_adapter_unavailable"),
        ("ollama_minimal_generation", "ollama_adapter_unavailable"),
    ],
)
def test_missing_probe_is_truthfully_unavailable(kind: str, code: str) -> None:
    """Catch configured dependencies being reported green without an accepted active probe."""
    result = DiagnosticCoordinator(settings_revision=lambda: 7).run(
        kind=kind,
        expected_settings_revision=7,
    )

    assert result.state == "unavailable"
    assert result.code == code
    assert result.settings_revision == 7
    assert result.component is None
    assert result.model is None


@pytest.mark.parametrize(
    ("kind", "outcome", "expected_state"),
    [
        ("data_root_writable", ProbeOutcome("passed", "data_root_writable"), "passed"),
        ("data_root_writable", ProbeOutcome("failed", "data_root_not_writable"), "failed"),
        ("data_root_writable", ProbeOutcome("failed", "data_root_identity_changed"), "failed"),
        ("sqlite_integrity", ProbeOutcome("passed", "sqlite_integrity_ok"), "passed"),
        ("sqlite_integrity", ProbeOutcome("failed", "sqlite_foreign_keys_invalid"), "failed"),
        ("sqlite_integrity", ProbeOutcome("failed", "sqlite_schema_incompatible"), "failed"),
        ("sqlite_integrity", ProbeOutcome("failed", "sqlite_integrity_failed"), "failed"),
        ("engine_launch_version", ProbeOutcome("failed", "engine_probe_timeout"), "failed"),
        ("engine_launch_version", ProbeOutcome("failed", "engine_process_unsettled"), "failed"),
        ("ollama_model_available", ProbeOutcome("failed", "ollama_model_unavailable"), "failed"),
        ("ollama_model_available", ProbeOutcome("failed", "model_digest_unavailable"), "failed"),
        ("ollama_model_available", ProbeOutcome("failed", "ollama_transport_refused"), "failed"),
        ("ollama_model_available", ProbeOutcome("failed", "ollama_response_too_large"), "failed"),
        ("ollama_minimal_generation", ProbeOutcome("passed", "ollama_generation_ok"), "passed"),
        ("ollama_minimal_generation", ProbeOutcome("failed", "ollama_response_invalid"), "failed"),
        ("ollama_minimal_generation", ProbeOutcome("failed", "ollama_model_mismatch"), "failed"),
        ("ollama_minimal_generation", ProbeOutcome("failed", "ollama_generation_incomplete"), "failed"),
    ],
)
def test_coordinator_preserves_closed_probe_outcomes(
    kind: str,
    outcome: ProbeOutcome,
    expected_state: str,
) -> None:
    """Catch one diagnostic family being conflated with another or a failed bound becoming success."""
    probe = _Probe(outcome)
    coordinator = DiagnosticCoordinator(settings_revision=lambda: 3, probes={kind: probe})

    result = coordinator.run(kind=kind, expected_settings_revision=3)

    assert result.kind == kind
    assert result.state == expected_state
    assert result.code == outcome.code
    assert result.message
    assert result.duration_milliseconds is not None
    assert probe.calls == 1


def test_stale_revision_is_rejected_before_rate_or_probe_calls() -> None:
    """Catch active I/O occurring against settings different from the submitted snapshot."""
    probe = _Probe(ProbeOutcome("passed", "data_root_writable"))
    rate_calls: list[str] = []
    coordinator = DiagnosticCoordinator(
        settings_revision=lambda: 8,
        probes={"data_root_writable": probe},
        rate_limiter=lambda kind: not rate_calls.append(kind),
    )

    with pytest.raises(DiagnosticsError) as caught:
        coordinator.run(kind="data_root_writable", expected_settings_revision=7)

    assert caught.value.code == "settings_revision_conflict"
    assert probe.calls == 0
    assert rate_calls == []


def test_unknown_kind_is_rejected_before_any_probe() -> None:
    """Catch an open-ended diagnostic action becoming a process or network dispatch surface."""
    probe = _Probe(ProbeOutcome("passed", "data_root_writable"))
    coordinator = DiagnosticCoordinator(settings_revision=lambda: 1, probes={"data_root_writable": probe})

    with pytest.raises(DiagnosticsError) as caught:
        coordinator.run(kind="run_arbitrary_command", expected_settings_revision=1)

    assert caught.value.code == "diagnostic_kind_invalid"
    assert probe.calls == 0


def test_rate_refusal_is_stable_and_prevents_probe_call() -> None:
    """Catch rate limiting that still performs the active diagnostic side effect."""
    probe = _Probe(ProbeOutcome("passed", "sqlite_integrity_ok"))
    coordinator = DiagnosticCoordinator(
        settings_revision=lambda: 1,
        probes={"sqlite_integrity": probe},
        rate_limiter=lambda _kind: False,
    )

    with pytest.raises(DiagnosticsError) as caught:
        coordinator.run(kind="sqlite_integrity", expected_settings_revision=1)

    assert caught.value.code == "diagnostic_rate_limited"
    assert probe.calls == 0


def test_duplicate_diagnostic_kind_is_serialized() -> None:
    """Catch concurrent duplicate diagnostics bypassing the per-kind active-action bound."""
    entered = threading.Event()
    release = threading.Event()

    def blocking_probe() -> ProbeOutcome:
        entered.set()
        release.wait(timeout=5)
        return ProbeOutcome("passed", "data_root_writable")

    coordinator = DiagnosticCoordinator(
        settings_revision=lambda: 1,
        probes={"data_root_writable": blocking_probe},
    )
    thread = threading.Thread(
        target=lambda: coordinator.run(kind="data_root_writable", expected_settings_revision=1)
    )
    thread.start()
    assert entered.wait(timeout=2)

    with pytest.raises(DiagnosticsError) as caught:
        coordinator.run(kind="data_root_writable", expected_settings_revision=1)

    release.set()
    thread.join(timeout=5)
    assert caught.value.code == "diagnostic_already_running"


def test_probe_exception_is_redacted_to_a_closed_failure() -> None:
    """Catch private path, command, stderr, response, or secret text escaping a probe exception."""
    private = "C:/Users/private/replay.sqlite3 token=secret raw-response"
    probe = _Probe(RuntimeError(private))
    coordinator = DiagnosticCoordinator(settings_revision=lambda: 2, probes={"sqlite_integrity": probe})

    result = coordinator.run(kind="sqlite_integrity", expected_settings_revision=2)

    assert result.state == "failed"
    assert result.code == "sqlite_integrity_probe_failed"
    assert private not in result.message
    assert "private" not in result.message.casefold()
    assert "secret" not in result.message.casefold()


def test_unknown_probe_code_is_not_forwarded() -> None:
    """Catch an adapter inventing a message/code channel that can contain private material."""
    probe = _Probe(ProbeOutcome("failed", "C:/private/raw-response"))
    coordinator = DiagnosticCoordinator(settings_revision=lambda: 2, probes={"sqlite_integrity": probe})

    result = coordinator.run(kind="sqlite_integrity", expected_settings_revision=2)

    assert result.code == "sqlite_integrity_probe_failed"
    assert "private" not in result.message.casefold()


def test_probe_code_and_state_must_belong_to_the_requested_diagnostic() -> None:
    """Catch one probe family borrowing another family's green code or a failure code marked passed."""
    wrong_family = _Probe(ProbeOutcome("passed", "ollama_generation_ok"))
    wrong_state = _Probe(ProbeOutcome("passed", "sqlite_integrity_failed"))

    family_result = DiagnosticCoordinator(
        settings_revision=lambda: 2,
        probes={"data_root_writable": wrong_family},
    ).run(kind="data_root_writable", expected_settings_revision=2)
    state_result = DiagnosticCoordinator(
        settings_revision=lambda: 2,
        probes={"sqlite_integrity": wrong_state},
    ).run(kind="sqlite_integrity", expected_settings_revision=2)

    assert family_result.code == "data_root_writable_probe_failed"
    assert family_result.state == "failed"
    assert state_result.code == "sqlite_integrity_probe_failed"
    assert state_result.state == "failed"


def test_engine_success_exposes_only_validated_component_identity() -> None:
    """Catch raw stdout or command text being used as engine version identity."""
    component = DiagnosticComponentIdentity(
        component="analyzer",
        version="1.04-modern",
        content_digest="a" * 64,
        build_identity="win32-release",
    )
    probe = _Probe(ProbeOutcome("passed", "engine_version_ok", component=component))
    coordinator = DiagnosticCoordinator(settings_revision=lambda: 4, probes={"engine_launch_version": probe})

    result = coordinator.run(kind="engine_launch_version", expected_settings_revision=4)

    assert result.state == "passed"
    assert result.component == component
    assert result.model is None


def test_ollama_success_exposes_normalized_exact_model_identity() -> None:
    """Catch endpoint aliases, approximate model matches, or fabricated digests in diagnostics."""
    model = DiagnosticModelIdentity(
        endpoint="http://[::1]:11434/",
        model_name="qwen3.6:27b",
        configured_model_digest="b" * 64,
        discovered_model_digest="b" * 64,
    )
    probe = _Probe(ProbeOutcome("passed", "ollama_model_available", model=model))
    coordinator = DiagnosticCoordinator(settings_revision=lambda: 5, probes={"ollama_model_available": probe})

    result = coordinator.run(kind="ollama_model_available", expected_settings_revision=5)

    assert result.model is not None
    assert result.model.endpoint == "http://[::1]:11434"
    assert result.model.model_name == "qwen3.6:27b"
    assert result.model.discovered_model_digest == "b" * 64


@pytest.mark.parametrize(
    "identity",
    [
        lambda: DiagnosticComponentIdentity(component="analyzer", version="C:/private/build"),
        lambda: DiagnosticComponentIdentity(component="analyzer", build_identity="stderr\nsecret"),
        lambda: DiagnosticComponentIdentity(component="analyzer", content_digest="NOT-A-DIGEST"),
        lambda: DiagnosticModelIdentity(endpoint="http://localhost:11434", model_name="model:1"),
        lambda: DiagnosticModelIdentity(endpoint="http://127.0.0.1:11434", model_name="../model"),
        lambda: DiagnosticModelIdentity(
            endpoint="http://127.0.0.1:11434",
            model_name="model:1",
            discovered_model_digest="C:/private",
        ),
    ],
)
def test_diagnostic_identities_reject_path_control_and_invalid_digest_values(identity: object) -> None:
    """Catch private adapter details crossing the immutable coordinator boundary."""
    with pytest.raises(DiagnosticsError) as caught:
        identity()  # type: ignore[operator]
    assert caught.value.code == "diagnostic_identity_invalid"


def test_result_and_probe_values_are_immutable() -> None:
    """Catch diagnostic state changing after the route receives it."""
    outcome = ProbeOutcome("passed", "data_root_writable")
    result = DiagnosticCoordinator(
        settings_revision=lambda: 1,
        probes={"data_root_writable": lambda: outcome},
    ).run(kind="data_root_writable", expected_settings_revision=1)

    with pytest.raises(FrozenInstanceError):
        outcome.code = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.code = "changed"  # type: ignore[misc]


def test_ollama_absence_does_not_disable_deterministic_diagnostic() -> None:
    """Catch the optional model dependency becoming a global diagnostics prerequisite."""
    data_probe = _Probe(ProbeOutcome("passed", "data_root_writable"))
    coordinator = DiagnosticCoordinator(settings_revision=lambda: 9, probes={"data_root_writable": data_probe})

    model = coordinator.run(kind="ollama_model_available", expected_settings_revision=9)
    data = coordinator.run(kind="data_root_writable", expected_settings_revision=9)

    assert model.state == "unavailable"
    assert data.state == "passed"
    assert data_probe.calls == 1
