"""Speaking in a call, in one of two voices, only when he approved it.

The bug this file exists for was not a crash. `say_in_call` generated the audio,
discarded it, and returned "said it in the call" — no device, no playback, no
error. Arun would have believed a point was made in a call where nothing was
said, and found out from the person on the other end. Its own docstring warned
about a milder version of the same failure.

So the tests here are mostly about the ways speaking can fail QUIETLY:
generation returning nothing, the device vanishing, the instruction being read
out loud, and — the one with a person on the other end of it — his voice being
used when he did not ask for it.
"""

from __future__ import annotations

import io
import wave

import pytest

from app import meetings, store, voice


def _wav(seconds: float = 0.2, rate: int = 24000, hz: int = 220) -> bytes:
    """A real WAV, so playback code paths get real frames rather than a mock."""
    import math
    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = int(12000 * math.sin(2 * math.pi * hz * i / rate))
        frames += v.to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


# --- which voice he asked for -------------------------------------------------

@pytest.mark.parametrize("said,expected", [
    ("talk like me, tell him the build passed", voice.VOICE_MINE),
    ("say it in my voice", voice.VOICE_MINE),
    ("reply as me", voice.VOICE_MINE),
    ("talk like assistant", voice.VOICE_ASSISTANT),
    ("answer as the assistant", voice.VOICE_ASSISTANT),
    ("speak as asta", voice.VOICE_ASSISTANT),
])
def test_he_gets_the_voice_he_named(said, expected):
    assert voice.pick_voice(said) == expected


def test_saying_nothing_about_voice_keeps_the_current_one():
    assert voice.pick_voice("tell him the build passed", voice.VOICE_MINE) == voice.VOICE_MINE
    assert voice.pick_voice("tell him", voice.VOICE_ASSISTANT) == voice.VOICE_ASSISTANT


def test_the_default_is_the_assistant_not_him():
    """Nothing said, no current voice — must not reach for his."""
    assert voice.pick_voice("tell him the build passed") == voice.VOICE_ASSISTANT
    assert voice.pick_voice("", "") == voice.VOICE_ASSISTANT


def test_a_contradictory_instruction_falls_back_to_the_assistant():
    """Two instructions in one sentence is unclear, not a coin toss.

    The safe reading of an unclear instruction about whose voice to use is
    "not his" — there is a person on the other end who cannot check.
    """
    assert voice.pick_voice("talk like me but as assistant") == voice.VOICE_ASSISTANT
    assert voice.pick_voice("as assistant, talk like me") == voice.VOICE_ASSISTANT


def test_garbage_current_voice_is_not_trusted():
    assert voice.pick_voice("tell him", "wharrgarbl") == voice.VOICE_ASSISTANT


def test_each_voice_maps_to_its_own_engine():
    p_mine, e_mine = voice.voice_settings(voice.VOICE_MINE)
    p_asst, e_asst = voice.voice_settings(voice.VOICE_ASSISTANT)
    assert e_mine == "chatterbox" and p_mine == voice.CLONE_PROFILE
    assert (p_asst, e_asst) != (p_mine, e_mine)


# --- the instruction must not be spoken --------------------------------------

def test_the_instruction_is_stripped_from_what_gets_said():
    """Otherwise the other person hears "talk like me, tell him the build passed"."""
    out = voice.strip_voice_instruction("talk like me, tell him the build passed")
    assert "talk like me" not in out.lower()
    assert "tell him the build passed" in out


def test_stripping_leaves_ordinary_words_alone():
    text = "tell him the amend flow is fine"
    assert voice.strip_voice_instruction(text) == text


def test_an_instruction_with_no_content_leaves_nothing():
    assert voice.strip_voice_instruction("talk like me") == ""


# --- finding the device -------------------------------------------------------

def test_a_device_is_found_by_partial_name(monkeypatch):
    """macOS names it slightly differently across versions."""
    monkeypatch.setattr(voice, "output_devices", lambda: ["BlackHole 2ch"])
    fake = [{"name": "MacBook Pro Speakers", "max_output_channels": 2},
            {"name": "BlackHole 2ch", "max_output_channels": 2}]
    import sys, types
    sd = types.SimpleNamespace(query_devices=lambda: fake)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    assert voice.find_device("blackhole") == 1
    assert voice.find_device("BlackHole 2ch") == 1


def test_an_input_only_device_is_not_offered_for_output(monkeypatch):
    import sys, types
    fake = [{"name": "BlackHole 2ch", "max_output_channels": 0}]
    monkeypatch.setitem(sys.modules, "sounddevice",
                        types.SimpleNamespace(query_devices=lambda: fake))
    assert voice.find_device("blackhole") is None


def test_an_unknown_device_is_not_guessed(monkeypatch):
    import sys, types
    monkeypatch.setitem(sys.modules, "sounddevice",
                        types.SimpleNamespace(query_devices=lambda: []))
    assert voice.find_device("BlackHole 2ch") is None


# --- playback fails loudly ----------------------------------------------------

def test_playing_with_no_device_configured_raises(monkeypatch):
    """CALL_DEVICE is cleared explicitly: this machine HAS one configured, and
    without the patch the test silently exercised the real device — it played
    a tone out of the laptop instead of asserting the refusal."""
    monkeypatch.setattr(voice, "CALL_DEVICE", "")
    with pytest.raises(RuntimeError, match="no output device"):
        voice.play_to_device(_wav(), "")


def test_playing_to_a_missing_device_raises_and_lists_what_exists(monkeypatch):
    monkeypatch.setattr(voice, "find_device", lambda n: None)
    monkeypatch.setattr(voice, "output_devices", lambda: ["MacBook Pro Speakers"])
    with pytest.raises(RuntimeError, match="not found"):
        voice.play_to_device(_wav(), "BlackHole 2ch")


def test_empty_audio_raises_rather_than_playing_silence(monkeypatch):
    monkeypatch.setattr(voice, "find_device", lambda n: 0)
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
    with pytest.raises(RuntimeError, match="no frames"):
        voice.play_to_device(buf.getvalue(), "BlackHole 2ch")


# --- say_in_call: the actual bug ---------------------------------------------

@pytest.fixture
def in_call(monkeypatch):
    store.kv_set("teams_in_call", "1")
    monkeypatch.setattr(meetings, "can_speak", lambda: True)
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "BlackHole 2ch")
    yield
    store.kv_set("teams_in_call", "")


@pytest.mark.asyncio
async def test_the_audio_is_actually_played(in_call, monkeypatch):
    """THE regression. It used to generate audio and drop it on the floor."""
    played = {}

    async def gen(text, profile="", engine="", voice=""):
        return _wav()

    def play(wav, device=""):
        played["bytes"] = len(wav)
        played["device"] = device
        return 0.2

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", play)

    out = await meetings.say_in_call("tell him the build passed")
    assert played["bytes"] > 0, "audio was generated and never played"
    assert played["device"] == "BlackHole 2ch"
    assert "said it in the call" in out


@pytest.mark.asyncio
async def test_a_playback_failure_is_not_reported_as_success(in_call, monkeypatch):
    """The whole point: he must never be told a point was made when it wasn't."""
    async def gen(text, profile="", engine="", voice=""):
        return _wav()

    def boom(wav, device=""):
        raise RuntimeError("audio device 'BlackHole 2ch' not found")

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", boom)

    with pytest.raises(RuntimeError, match="not found"):
        await meetings.say_in_call("tell him the build passed")


@pytest.mark.asyncio
async def test_empty_generation_says_so(in_call, monkeypatch):
    async def nothing(text, profile="", engine="", voice=""):
        return b""

    monkeypatch.setattr(voice, "speak", nothing)
    with pytest.raises(RuntimeError, match="said nothing"):
        await meetings.say_in_call("tell him")


@pytest.mark.asyncio
async def test_it_refuses_when_not_in_a_call(monkeypatch):
    monkeypatch.setattr(meetings, "can_speak", lambda: True)
    store.kv_set("teams_in_call", "")
    with pytest.raises(RuntimeError, match="not in a call"):
        await meetings.say_in_call("hello")


@pytest.mark.asyncio
async def test_it_refuses_with_no_virtual_mic(monkeypatch):
    monkeypatch.setattr(meetings, "can_speak", lambda: False)
    with pytest.raises(RuntimeError, match="virtual microphone"):
        await meetings.say_in_call("hello")


@pytest.mark.asyncio
async def test_the_named_voice_reaches_the_generator(in_call, monkeypatch):
    seen = {}

    async def gen(text, profile="", engine="", voice=""):
        seen["voice"] = voice
        seen["text"] = text
        return _wav()

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": 0.2)

    await meetings.say_in_call("talk like me, tell him all merged")
    assert seen["voice"] == voice.VOICE_MINE
    assert "talk like me" not in seen["text"].lower(), "the instruction was spoken aloud"


@pytest.mark.asyncio
async def test_an_instruction_with_nothing_to_say_is_refused(in_call, monkeypatch):
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": 0.2)
    with pytest.raises(RuntimeError, match="nothing left to say"):
        await meetings.say_in_call("talk like me")


@pytest.mark.asyncio
async def test_his_voice_is_never_used_by_default(in_call, monkeypatch):
    """No instruction, no clone. There is a person on the other end."""
    seen = {}

    async def gen(text, profile="", engine="", voice=""):
        seen["voice"] = voice
        return _wav()

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": 0.2)

    await meetings.say_in_call("tell him the build passed")
    assert seen["voice"] == voice.VOICE_ASSISTANT
