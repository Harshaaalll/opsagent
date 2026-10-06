"""Voice input: speak the request, see the transcript, confirm, then the normal agent runs.

Capture uses the system recorder (arecord, or pw-record); transcription uses Gemini audio input (English output,
Hindi/Hinglish is translated). The transcript is ALWAYS shown and confirmed before anything runs: a misheard amount or
vendor name must never start a financial action.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .llm.base import LLMError


def _recorder_cmd(path: Path) -> list[str]:
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", str(path)]
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", "16000", "--channels", "1", str(path)]
    raise LLMError("no audio recorder found (install alsa-utils for arecord, or use --audio file.wav)")


def record(seconds: Optional[float] = None) -> bytes:
    """Record from the default microphone. With no `seconds`, press Enter to stop."""
    path = Path(tempfile.mkdtemp(prefix="opsagent-voice-")) / "req.wav"
    proc = subprocess.Popen(_recorder_cmd(path), stderr=subprocess.DEVNULL)
    try:
        if seconds:
            print(f"Recording for {seconds:.0f}s... speak now.", flush=True)
            try:
                proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                pass
        else:
            print("Recording... speak your request, then press Enter to stop.", flush=True)
            t = threading.Thread(target=input, daemon=True)
            t.start()
            while t.is_alive() and proc.poll() is None:
                t.join(0.2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    data = path.read_bytes() if path.exists() else b""
    if len(data) < 2000:
        raise LLMError("no audio captured (check the microphone)")
    return data


def mime_for(path: str) -> str:
    return {".wav": "audio/wav", ".mp3": "audio/mp3", ".flac": "audio/flac", ".ogg": "audio/ogg",
            ".m4a": "audio/mp4", ".aac": "audio/aac"}.get(Path(path).suffix.lower(), "audio/wav")


def clean_transcript(text: str) -> str:
    """Normalise model output; empty string means 'no usable speech'."""
    t = (text or "").strip().strip('"').strip()
    return "" if (not t or t.upper().startswith("NO_SPEECH")) else t


def confirm(transcript: str, ask=input) -> Optional[str]:
    """Show what was heard. Enter/y accepts, n cancels, anything else is taken as the corrected request."""
    print(f'\nHeard: "{transcript}"')
    ans = ask("Run this? [Enter = yes, n = cancel, or type a corrected request] ").strip()
    if ans.lower() in ("", "y", "yes"):
        return transcript
    if ans.lower() in ("n", "no"):
        return None
    return ans
