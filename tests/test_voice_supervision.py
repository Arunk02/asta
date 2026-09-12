"""A device is a route. A service is a voice.

2026-09-07, minutes before a real call to a colleague: `voice_check` reported
"Cannot be heard — ConnectError: All connection attempts failed" while
`can_speak()` returned True. Nothing was listening on Voicebox's port at all —
it has no supervisor and had simply been down for who knows how long. Had the
call gone out on `can_speak()`'s word, Vinish would have answered a silent line.

This is the same lesson this module already learned once: macOS answers a denied
microphone request with a valid, correctly-labelled track full of digital
silence, so every layer looked healthy and nothing was transmitted. A thing that
exists is not a thing that works.
"""

from __future__ import annotations

import asyncio
import plistlib
from pathlib import Path

import pytest

from app import voice

AGENT = Path.home() / "Library/LaunchAgents/com.asta.voicebox.plist"


@pytest.fixture(autouse=True)
def _cold_cache():
    voice._SERVICE_UP = (0.0, False)
    yield
    voice._SERVICE_UP = (0.0, False)


def test_a_virtual_mic_alone_is_not_a_voice(monkeypatch):
    """The exact false positive: the device is configured, the service is gone."""
    monkeypatch.setattr(voice, "CALL_DEVICE", "BlackHole 2ch")
    monkeypatch.setattr(voice, "service_up", lambda *a, **k: False)
    assert voice.can_speak() is False


def test_both_halves_are_required(monkeypatch):
    monkeypatch.setattr(voice, "service_up", lambda *a, **k: True)
    monkeypatch.setattr(voice, "CALL_DEVICE", "")
    assert voice.can_speak() is False            # a voice with nowhere to go
    monkeypatch.setattr(voice, "CALL_DEVICE", "BlackHole 2ch")
    assert voice.can_speak() is True


def test_an_unreachable_service_is_not_an_exception(monkeypatch):
    """It is asked per utterance inside a live call. It must answer, not raise."""
    monkeypatch.setattr(voice, "BASE", "http://127.0.0.1:1")
    assert voice.service_up(ttl=0) is False


def test_the_answer_is_cached(monkeypatch):
    """Per-utterance in a live call — an HTTP round trip per line would put a
    stall into the middle of a conversation."""
    calls = {"n": 0}

    class _R:
        status_code = 200

    def counted(url, timeout=None):
        calls["n"] += 1
        return _R()

    monkeypatch.setattr(voice.httpx, "get", counted)
    assert voice.service_up() is True
    for _ in range(50):
        voice.service_up()
    assert calls["n"] == 1, "the cache is not holding"


def test_the_cache_expires(monkeypatch):
    """The transition that matters is Voicebox dying mid-call. Staying silent is
    correct there; claiming otherwise is how somebody talks to nobody."""
    class _R:
        status_code = 200

    monkeypatch.setattr(voice.httpx, "get", lambda *a, **k: _R())
    assert voice.service_up() is True
    monkeypatch.setattr(voice, "BASE", "http://127.0.0.1:1")
    monkeypatch.setattr(voice.httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert voice.service_up(ttl=0) is False


def test_sync_and_async_cannot_disagree(monkeypatch):
    """`available()` already existed and asked the same question its own way.
    Two answers to "is the service up" is how they drift apart."""
    class _R:
        status_code = 200

    monkeypatch.setattr(voice.httpx, "get", lambda *a, **k: _R())
    assert voice.service_up() is asyncio.run(voice.available())


# --- supervision ---------------------------------------------------------------

@pytest.mark.skipif(not AGENT.is_file(), reason="voicebox agent not installed here")
def test_voicebox_is_supervised():
    """It is a hard dependency of calls and meetings and had no supervisor, so it
    stayed down until a call discovered it — and the only thing that noticed was
    a colleague hearing nothing."""
    plist = plistlib.loads(AGENT.read_bytes())
    assert plist["KeepAlive"] is True            # comes back if it dies
    assert plist["RunAtLoad"] is True            # comes up at login
    assert "17493" in plist["ProgramArguments"]  # the port voice.BASE expects


@pytest.mark.skipif(not AGENT.is_file(), reason="voicebox agent not installed here")
def test_the_agent_points_at_the_port_asta_speaks_to():
    """A supervisor for the wrong port is worse than none: it looks healthy."""
    plist = plistlib.loads(AGENT.read_bytes())
    port = plist["ProgramArguments"][plist["ProgramArguments"].index("--port") + 1]
    assert port in voice.CONFIGURED_BASE      # BASE is pointed at nothing in tests
