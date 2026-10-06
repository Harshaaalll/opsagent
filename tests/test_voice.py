"""Voice input: transcript handling and the confirm-before-run safety step (no mic, no network)."""

from __future__ import annotations

import pytest

from opsagent import voice
from opsagent.llm.base import LLMError


def test_clean_transcript_handles_empty_quotes_and_no_speech():
    assert voice.clean_transcript('  "Enter the Acme invoice."  ') == "Enter the Acme invoice."
    assert voice.clean_transcript("") == "" and voice.clean_transcript(None) == ""
    assert voice.clean_transcript("NO_SPEECH") == ""


def test_confirm_accepts_cancels_or_takes_a_correction():
    assert voice.confirm("do x", ask=lambda _: "") == "do x"
    assert voice.confirm("do x", ask=lambda _: "Y") == "do x"
    assert voice.confirm("do x", ask=lambda _: "n") is None
    assert voice.confirm("do x", ask=lambda _: "do y instead") == "do y instead"


def test_mime_for_known_and_unknown_extensions():
    assert voice.mime_for("a.mp3") == "audio/mp3" and voice.mime_for("a.FLAC") == "audio/flac"
    assert voice.mime_for("a.unknown") == "audio/wav"


def test_missing_recorder_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(voice.shutil, "which", lambda _: None)
    with pytest.raises(LLMError, match="no audio recorder"):
        voice._recorder_cmd(tmp_path / "x.wav")
