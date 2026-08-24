"""Local Windows SAPI voice provider with an argv-only process boundary."""

from __future__ import annotations

import base64
import os
import subprocess
from importlib import resources
from pathlib import Path
from uuid import uuid4

from generals_replay_analyzer.video.contracts import CommentaryEventV1
from generals_replay_analyzer.video.voice import NarrationScheduleError, VoiceClipV1


class VoiceProviderError(RuntimeError):
    """A configured local voice provider failed without publishing a clip."""


def packaged_script_path() -> Path:
    return Path(str(resources.files("generals_replay_analyzer.video").joinpath("windows_sapi.ps1"))).resolve()


# TheSuperHackers @feature Leex 24/08/2026 Invoke local SAPI through fixed argv fields without shell interpretation. (#TBD)
class WindowsSapiVoiceProvider:
    provider_name = "windows-sapi-v1"

    def __init__(self, powershell_executable: Path, voice_name: str) -> None:
        if not voice_name or len(voice_name) > 160:
            raise ValueError("voice_name must identify one configured SAPI voice")
        self.powershell_executable = powershell_executable
        self.voice_name = voice_name

    def render(self, event: CommentaryEventV1, destination: Path) -> VoiceClipV1:
        if type(event) is not CommentaryEventV1:
            raise TypeError("SAPI rendering requires a validated commentary event")
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.{uuid4().hex}.tmp.wav")
        encoded_text = base64.b64encode(event.text.encode("utf-8")).decode("ascii")
        argv = [
            str(self.powershell_executable),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(packaged_script_path()),
            "-Destination",
            str(temporary),
            "-VoiceName",
            self.voice_name,
            "-TextBase64",
            encoded_text,
            "-SampleRate",
            "48000",
        ]
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                diagnostic = completed.stderr.strip() or completed.stdout.strip() or f"exit_{completed.returncode}"
                raise VoiceProviderError(f"Windows SAPI failed: {diagnostic[:500]}")
            VoiceClipV1.from_wav(
                event,
                temporary,
                provider_name=self.provider_name,
                voice_name=self.voice_name,
            )
            os.replace(temporary, destination)
            return VoiceClipV1.from_wav(
                event,
                destination,
                provider_name=self.provider_name,
                voice_name=self.voice_name,
            )
        except NarrationScheduleError as error:
            raise VoiceProviderError(f"Windows SAPI produced an invalid WAV: {error}") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise VoiceProviderError(f"Windows SAPI could not run: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)
