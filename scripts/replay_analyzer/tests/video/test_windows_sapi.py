"""Windows SAPI is invoked through one packaged argv-only boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from generals_replay_analyzer.video.contracts import CommentaryEventV1, EvidenceCitationV1
from generals_replay_analyzer.video.windows_sapi import (
    VoiceProviderError,
    WindowsSapiVoiceProvider,
    packaged_script_path,
)


def _event(text: str = "Safe; $(not-a-shell) 'quoted'") -> CommentaryEventV1:
    return CommentaryEventV1(
        event_id="50000000-0000-4000-8000-000000000001",
        start_frame=0,
        latest_end_frame=90,
        text=text,
        subtitle_text=text,
        role="intro",
        evidence=(EvidenceCitationV1(
            evidence_public_id="30000000-0000-4000-8000-000000000001",
            tier="observed",
            frame_start=0,
            frame_end=90,
        ),),
        confidence_tier="observed",
        camera_segment_id="40000000-0000-4000-8000-000000000001",
    )


def test_packaged_sapi_script_exists() -> None:
    assert packaged_script_path().is_file()
    assert "System.Speech" in packaged_script_path().read_text(encoding="utf-8")


def test_sapi_uses_argv_without_shell_and_returns_measured_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        destination = Path(argv[argv.index("-Destination") + 1])
        import wave
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48_000)
            output.writeframes(bytes(9_600))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = WindowsSapiVoiceProvider(Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), "Microsoft David")
    clip = provider.render(_event(), tmp_path / "voice.wav")

    argv, kwargs = calls[0]
    assert argv[:6] == [
        str(provider.powershell_executable),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ]
    assert "Safe; $(not-a-shell) 'quoted'" not in argv
    assert kwargs["shell"] is False
    assert clip.sample_count == 4_800
    assert clip.voice_name == "Microsoft David"


def test_sapi_surfaces_missing_voice_or_process_failure_without_publishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 21, "", "voice_not_found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    destination = tmp_path / "voice.wav"
    provider = WindowsSapiVoiceProvider(Path("powershell.exe"), "Missing Voice")
    with pytest.raises(VoiceProviderError, match="voice_not_found"):
        provider.render(_event(), destination)
    assert not destination.exists()
