# Copyright 2026 TheSuperHackers
#
# AI Voice Caster Engine: Synchronizes DoMiNaToR-style commentary audio to gameplay timeline.

import asyncio
import os
import subprocess
from typing import Dict, Any, List, Tuple
import edge_tts
from .commentary import CommentaryEvent

class TTSVoiceCaster:
    """Generates broadcast-quality AI voice commentary timed precisely to in-game match events."""

    def __init__(self, commentary_dict: Dict[str, Any], voice: str = "en-US-ChristopherNeural"):
        self.commentary = commentary_dict
        self.events: List[CommentaryEvent] = commentary_dict.get("events", [])
        self.voice = voice

    async def _synthesize_clip(self, text: str, output_path: str):
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(output_path)

    async def _synthesize_all(self, clip_tasks: List[Tuple[str, str]]):
        tasks = [self._synthesize_clip(txt, path) for txt, path in clip_tasks]
        await asyncio.gather(*tasks)

    def generate_commentary_audio(self, output_path: str = "commentary_track.mp3") -> str:
        """Generates synchronized voice track for the entire match cast."""
        clips_dir = os.path.join(os.path.dirname(output_path), "_temp_tts_clips")
        os.makedirs(clips_dir, exist_ok=True)

        if not self.events:
            return ""

        clip_tasks = []
        timed_clips: List[Tuple[float, str]] = []

        for idx, ev in enumerate(self.events):
            if not ev.text.strip():
                continue
            clip_path = os.path.join(clips_dir, f"clip_{idx:03d}.mp3")
            clip_tasks.append((ev.text, clip_path))
            timed_clips.append((ev.time_sec, clip_path))

        # Synthesize all clips in parallel
        asyncio.run(self._synthesize_all(clip_tasks))

        # Build FFmpeg filter complex with precision millisecond adelay
        input_args = []
        filter_parts = []
        mix_inputs = []

        for i, (t_sec, c_path) in enumerate(timed_clips):
            delay_ms = int(t_sec * 1000)
            input_args.extend(["-i", c_path])
            filter_parts.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]")
            mix_inputs.append(f"[a{i}]")

        mix_filter = f"{';'.join(filter_parts)};{''.join(mix_inputs)}amix=inputs={len(timed_clips)}:duration=longest[out]"

        cmd = [
            "ffmpeg", "-y",
            *input_args,
            "-filter_complex", mix_filter,
            "-map", "[out]",
            "-c:a", "libmp3lame",
            "-q:a", "2",
            output_path
        ]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        # Cleanup temporary clips
        for _, path in clip_tasks:
            if os.path.exists(path):
                os.remove(path)
        if os.path.exists(clips_dir):
            try:
                os.rmdir(clips_dir)
            except Exception:
                pass

        return output_path
