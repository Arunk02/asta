"""Voice notes ON WHATSAPP — Astra-class P7, the last of hands.

(Teams voice notes are tests/test_voice_note.py; this is the phone channel.)

Spoken words cannot be scrolled back to, so the tests are about honesty: what
was actually sent, and saying so when it is not the real thing.
"""

from __future__ import annotations

import asyncio
import wave

import pytest

from app import notify, voice


def _wav(path, seconds=2):
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 24000 * seconds)
    return path.read_bytes()


@pytest.fixture
def _spoken(tmp_path, monkeypatch):
    audio = _wav(tmp_path / "src.wav", seconds=3)

    async def speak(text, **kw):
        return audio

    monkeypatch.setattr(voice, "speak", speak)
    return audio


def test_it_says_which_format_it_actually_sent(_spoken, monkeypatch):
    """A hold-to-play note needs Opus, which this Mac cannot encode without
    ffmpeg. Sending AAC and calling it a voice note is the kind of small lie
    that makes every other report suspect."""
    sent: list = []

    async def wa_voice(path, seconds=1):
        sent.append((path, seconds))
        return True

    monkeypatch.setattr(notify, "wa_voice", wa_voice)
    out = asyncio.run(voice.voice_note("two lines while you walk"))
    assert out["sent"] and out["seconds"] == 3
    assert out["format"] in ("opus", "aac", "wav")
    if out["format"] != "opus":
        assert "hold-to-play" in out["note"]
    else:
        assert out["note"] == ""


def test_a_phone_that_refuses_it_is_not_reported_as_said(_spoken, monkeypatch):
    async def wa_voice(path, seconds=1):
        return False

    monkeypatch.setattr(notify, "wa_voice", wa_voice)
    assert asyncio.run(voice.voice_note("hello"))["sent"] is False

    from app import agent
    said = asyncio.run(agent.leave_voice_note("hello"))
    assert said.startswith("Not sent")


def test_the_capability_says_what_it_is_for():
    from app import capabilities
    cap = capabilities.registry()["leave_voice_note"]
    assert "walking" in cap.note and "search" in cap.note
