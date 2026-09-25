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

import asyncio
import io
import wave

import pytest

from app import conversation, meetings, store, voice


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


# --------------------------------------------------------------------------------------
"""Whose voice a whole CALL is in — decided once, when it is placed.

The file above this line is about one line at a time. This part is about the
call: a call could be held in his cloned voice only by naming "mine" on every
sentence, because `synth`, the opener, the holding lines and the "mm-hm" each
defaulted to the assistant on their own. So "ring Vinish in my voice" produced
a call that was half his voice and half Asta's — the one outcome nobody can
hear a fair test through.

The rules here: the voice is set once and every mouth reads it from the same
place; an explicit voice on one line still wins; it is forgotten when the call
ends so the NEXT call is never his by inheritance; a call in his voice SAYS so
in its first sentence; and his clone is routed by script to the engine that can
actually speak it in time.
"""

@pytest.fixture(autouse=True)
def _assistant_again():
    """No test may leak his voice into the next one — nor into a real call."""
    yield
    voice.in_voice(voice.VOICE_ASSISTANT)
    meetings._VOICE_CACHE.clear()


def _watch(monkeypatch) -> list[dict]:
    asked: list[dict] = []

    async def speak(text, profile="", engine="", voice_name="", **kw):
        asked.append({"text": text, "voice": kw.get("voice", voice_name),
                      "profile": profile, "engine": engine})
        return b"RIFFwav"

    monkeypatch.setattr(voice, "speak", speak)
    meetings._VOICE_CACHE.clear()
    return asked


# --- set once, read everywhere ---------------------------------------------------------

def test_a_call_speaks_in_the_assistant_voice_unless_told_otherwise(monkeypatch):
    asked = _watch(monkeypatch)
    asyncio.run(meetings.synth("Hello Vinish."))
    assert asked[0]["voice"] == voice.VOICE_ASSISTANT


def test_a_call_placed_in_his_voice_speaks_every_line_in_it(monkeypatch):
    asked = _watch(monkeypatch)
    voice.in_voice(voice.VOICE_MINE)
    asyncio.run(meetings.synth("Hello Vinish."))
    asyncio.run(meetings.synth("One moment."))          # a holding line
    assert [a["voice"] for a in asked] == [voice.VOICE_MINE, voice.VOICE_MINE]


def test_the_two_voices_never_share_cached_audio(monkeypatch):
    """Same sentence, two voices: the cache must not hand his leg Asta's audio
    (or the comparison being tested is between a voice and itself)."""
    asked = _watch(monkeypatch)
    asyncio.run(meetings.synth("Can you hear me?"))
    voice.in_voice(voice.VOICE_MINE)
    asyncio.run(meetings.synth("Can you hear me?"))
    assert [a["voice"] for a in asked] == [voice.VOICE_ASSISTANT, voice.VOICE_MINE]


def test_an_unrecognised_voice_is_never_his(monkeypatch):
    asked = _watch(monkeypatch)
    voice.in_voice("Arun's voice please")
    asyncio.run(meetings.synth("Hello."))
    assert asked[0]["voice"] == voice.VOICE_ASSISTANT


def test_a_line_may_still_name_its_own_voice(monkeypatch):
    """Mid-call, "say that bit as yourself" has to beat the call's setting."""
    asked = _watch(monkeypatch)
    voice.in_voice(voice.VOICE_MINE)
    asyncio.run(meetings.synth("Asta here.", voice.VOICE_ASSISTANT))
    assert asked[0]["voice"] == voice.VOICE_ASSISTANT


def test_the_ack_is_in_the_same_voice_as_the_answer(monkeypatch):
    """The "mm-hm" covering the clone's synthesis time, in Asta's voice, is the
    most audible way to give away that two people are talking."""
    from app import call_rtc
    asked = _watch(monkeypatch)
    said: list[bytes] = []
    monkeypatch.setitem(meetings._CALL, "ctx", object())
    monkeypatch.setattr(call_rtc, "say", lambda ctx, wav, **kw: _done(said.append(wav)))
    voice.in_voice(voice.VOICE_MINE)
    asyncio.run(call_rtc.say_quick("Mm-hm."))
    assert asked[0]["voice"] == voice.VOICE_MINE and said == [b"RIFFwav"]


def _done(value=None):
    async def _f():
        return value
    return _f()


# --- forgotten when the call ends ------------------------------------------------------

def test_the_call_voice_is_forgotten_when_the_call_ends(monkeypatch):
    asked = _watch(monkeypatch)
    voice.in_voice(voice.VOICE_MINE)
    voice.in_voice(voice.VOICE_ASSISTANT)            # what leaving a call does
    asyncio.run(meetings.synth("Hi, Asta here."))
    assert asked[0]["voice"] == voice.VOICE_ASSISTANT


def test_a_call_asked_for_in_his_voice_sets_it_before_anything_is_said(monkeypatch):
    """`converse` must set it before the first holding line is synthesised —
    after would leave the opener in the wrong voice."""
    order: list[str] = []
    monkeypatch.setattr(voice, "in_voice",
                        lambda name="": order.append(f"voice={name}") or name
                        or voice.VOICE_ASSISTANT)
    monkeypatch.setattr(conversation.meetings, "call_person",
                        lambda who: _boom(order))

    said = asyncio.run(conversation.converse("Vinish Kumar", "a test",
                                             voice_name=voice.VOICE_MINE))
    assert order[0] == "voice=mine" and order.index("rang") > 0
    assert "Nothing rang" in said


def _boom(order):
    order.append("rang")
    raise RuntimeError("stopped before ringing")


# --- warming the voice that will actually be used --------------------------------------

def test_his_clone_is_warmed_while_the_phone_rings(monkeypatch):
    """The clone's first line costs ~30s cold and ~7s warm. Paying it after
    they pick up is the whole difference between a test and a bad impression."""
    warmed: list[tuple] = []

    async def speak(text, profile="", engine="", **kw):
        warmed.append((profile, engine))
        return b"wav"

    monkeypatch.setattr(voice, "speak", speak)
    asyncio.run(voice.warm_the_voice("en,hi", voice.VOICE_MINE))
    assert (voice.CLONE_PROFILE, voice.CLONE_ENGINE) in warmed


def test_the_assistant_call_does_not_warm_his_clone(monkeypatch):
    warmed: list[tuple] = []

    async def speak(text, profile="", engine="", **kw):
        warmed.append((profile, engine))
        return b"wav"

    monkeypatch.setattr(voice, "speak", speak)
    asyncio.run(voice.warm_the_voice("en"))
    assert all(p != voice.CLONE_PROFILE for p, _ in warmed)


# --- his clone, fast enough to hold a line ---------------------------------------------

def test_his_clone_speaks_english_through_the_fast_engine():
    """Measured on his M1 Pro, same clone, same sentence: chatterbox 6-13s a
    line, luxtts 0.8-1.3s. A colleague cannot wait 13 seconds for "yes", so the
    fast engine is what English goes through."""
    assert voice.clone_settings("Yes, the retry cap is already in.") \
        == (voice.CLONE_PROFILE, voice.CLONE_ENGINE_FAST)


def test_his_clone_speaks_hindi_through_the_engine_that_can():
    """luxtts has no Devanagari frontend at all — it raises "Kernel size can't be
    greater than actual input size" on every Hindi line, short or long. So Hindi
    in his voice goes to chatterbox and costs its seconds."""
    assert voice.clone_settings("हाँ, ठीक है।") == (voice.CLONE_PROFILE, voice.CLONE_ENGINE)


def test_the_fast_clone_gets_a_lead_in_so_it_cannot_eat_the_first_word():
    """The one dangerous failure: "No, we cannot ship that today" came back as
    "We cannot ship that today" — a reversal, not a glitch. A disposable lead-in
    absorbs the garbled onset; with it, 5/5 lines kept their opening words."""
    said = voice.lead_in("No, we cannot ship that today.", voice.CLONE_ENGINE_FAST)
    assert any(said.startswith(lead) for lead in voice.clone_leads())
    assert "No, we cannot ship" in said


def test_nothing_else_is_given_a_lead_in():
    """Asta's own voice does not lose onsets, and chatterbox does not either —
    a lead-in there is just a word nobody asked for."""
    assert voice.lead_in("Hello.", voice.DEFAULT_ENGINE) == "Hello."
    assert voice.lead_in("हाँ, ठीक है।", voice.CLONE_ENGINE) == "हाँ, ठीक है।"


def test_the_lead_in_is_not_said_twice():
    """A reply that already opens with the lead-in word must not get another."""
    lead = voice.clone_leads()[0]
    assert voice.lead_in(f"{lead} the cap is in.", voice.CLONE_ENGINE_FAST) \
        == f"{lead} the cap is in."


def test_the_lead_in_is_not_the_same_word_every_time():
    """Four identical openers in a row is a tic, and a tic is the thing a
    listener notices instead of the voice."""
    leads = {voice.lead_in("Yes.", voice.CLONE_ENGINE_FAST).rsplit("Yes.", 1)[0].strip()
             for _ in range(len(voice.clone_leads()))}
    assert len(leads) == len(voice.clone_leads())


def test_a_call_in_his_voice_routes_each_line_by_its_script(monkeypatch):
    """One call, both languages, one voice: the engine changes under it, his
    voice does not."""
    asked: list[dict] = []

    async def post(text, profile, engine, **kw):
        asked.append({"text": text, "profile": profile, "engine": engine})
        return b"RIFFwav"

    monkeypatch.setattr(voice, "_render", post, raising=False)
    monkeypatch.setattr(voice, "speak", post)
    voice.in_voice(voice.VOICE_MINE)
    meetings._VOICE_CACHE.clear()
    # What the call actually does — synth resolves the voice, speak resolves the
    # engine — so this asserts the pair a real line would be rendered with.
    assert voice.clone_settings("Yes.")[1] != voice.clone_settings("हाँ।")[1]


def test_the_http_door_can_place_a_call_in_his_voice(monkeypatch):
    """A capability only the chat agent can reach is one most of his traffic
    cannot: the CLI brains and the MCP server come in over HTTP."""
    import inspect

    from app import main
    src = inspect.getsource(main.api_discuss_in_call)
    for field in ("minutes", "agenda", "languages", "voice"):
        assert f'b.get("{field}"' in src or f'b["{field}"]' in src, field
    assert "voice_name=" in src


def test_a_call_in_his_voice_opens_as_him():
    """It used to bolt "it's his assistant, on his voice" onto the greeting. He
    asked for it twice: in his voice, talk like him. The brain writes the
    opener and nothing is appended to it."""
    import inspect
    src = inspect.getsource(conversation.converse)
    assert "disclosed" not in src
    assert 'f"Hi, is now a good time for {topic}?" if his_voice' in src


def test_the_greeting_is_a_short_line_of_its_own():
    """Live, 25 Sep 11:07: she picked up and heard NOTHING for two minutes ten
    seconds. The opener had grown to three sentences, his clone needs seconds
    per sentence, and the whole paragraph was synthesised as ONE line before a
    sound left the call. So the greeting is split off and said by itself: short
    enough to be ready, and said first."""
    hello, rest = conversation.opening_lines(
        "Hi, it's Asta, Arun's assistant. I'm speaking with Arun's own voice today "
        "so he can hear how it sounds. Is now a good time for a voice test?")
    assert hello == "Hi, it's Asta, Arun's assistant."
    assert len(hello) < 60 and "own voice" in rest and "good time" in rest


def test_a_one_sentence_opener_has_nothing_left_over():
    hello, rest = conversation.opening_lines("Hi Harika, is now a good time?")
    assert hello == "Hi Harika, is now a good time?" and rest == ""


def test_the_greeting_is_made_before_anything_else_in_the_call(monkeypatch):
    """Order of synthesis IS the dead air: whatever is made first is what can be
    said first. The greeting goes to the front of the queue, ahead of the
    reactions, the fillers and the rest of the opener."""
    made: list[str] = []

    async def synth(text, voice_name=""):
        made.append(text)
        return b"wav"

    monkeypatch.setattr(meetings, "synth", synth)
    voice.in_voice(voice.VOICE_MINE)
    asyncio.run(conversation._prepare_opening(
        "Hi, it's Asta, Arun's assistant. I'm speaking with Arun's own voice today."))
    assert made[0].startswith("Hi, it's Asta, Arun's assistant")
    assert "own voice" not in made[0] and any("own voice" in m for m in made[1:])


# --- re-cloning, when the clone does not sound like him ---------------------------------

def test_the_clone_scripts_cover_more_than_one_delivery():
    """25 Sep: both existing clones came back "doesn't sound like me", and the
    reference is what decides that. A clone copies the PROSODY it hears, so a
    reference of nothing but calm statements produces a voice that cannot ask a
    question or answer in one word — which is most of a phone call."""
    scripts = voice.CLONE_SCRIPTS
    assert len(scripts) >= 5
    assert any("?" in text for text in scripts.values()), "no question in the reference"
    quick = scripts.get("3-quick", "")
    assert quick and max(len(s.split()) for s in quick.split(".") if s.strip()) < 6
    # And a prompt per script, because talking beats reading: somebody reading a
    # sentence they would never say produces read-aloud rhythm for ever.
    assert set(voice.CLONE_PROMPTS) >= set(scripts)


def test_a_take_can_carry_its_own_corrected_words():
    """The reference text is what the engine ALIGNS the audio against, and it
    came from Whisper — which heard "done bro" as "Damn bro", "latency" as "the
    legacy" and "billing workflow" as "building workflow" on his 25 Sep takes.
    Every one of those teaches the clone a sound against the wrong word. A take
    may now carry the words he actually said."""
    import inspect
    src = inspect.getsource(voice.clone_from_sample)
    assert "len(sample) == 3" in src or "sample[2]" in src


def test_a_take_with_no_words_given_is_still_transcribed(monkeypatch):
    """Correcting is optional: a take handed over without text behaves exactly
    as before, or every existing caller breaks."""
    asked: list[str] = []

    async def hear(data, filename="", language=""):
        asked.append(filename)
        return "what whisper heard"

    posts: list[dict] = []

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "p1", "name": "test"}

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, files=None, data=None):
            posts.append({"url": url, "data": data})
            return _R()

    monkeypatch.setattr(voice, "transcribe", hear)
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **k: _C())
    out = asyncio.run(voice.clone_from_sample(
        "test", [("a.wav", b"x"), ("b.wav", b"y", "the words he actually said")]))
    assert asked == ["a.wav"], "transcribed a take that came with its own words"
    said = [p["data"]["reference_text"] for p in posts if p["data"]]
    assert said == ["what whisper heard", "the words he actually said"]
    assert out["samples"][1]["words"] == 5


# --- the rumble that made the clone sound like somebody else ---------------------------

def _tone(hz: float, seconds: float = 1.0, rate: int = 16000, amp: float = 0.4) -> bytes:
    import math
    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = int(32000 * amp * math.sin(2 * math.pi * hz * i / rate))
        frames += max(-32000, min(32000, v)).to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def test_rumble_under_the_voice_is_taken_out_before_cloning():
    """Measured on his 25 Sep takes: 10-15% of their energy sits below 80Hz —
    desk rumble from recording close on a phone, against 0.5% in the August
    ones. It dragged his measured pitch from 152Hz to 77Hz, and the clone learnt
    a voice with a low end he does not have. That is what "the accent is
    different" and "doesn't sound like me" were both describing."""
    rumble = _tone(40, 1.0, amp=0.6)
    assert voice.loudness(voice.without_rumble(rumble)) < 0.05, \
        "40Hz rumble survived the filter"


def test_taking_the_rumble_out_keeps_the_voice_itself():
    """A high-pass that eats his actual pitch would be worse than the rumble:
    his voice sits at 155Hz, which is why the cut is at 100 and not at 150."""
    voiced = _tone(155, 1.0, amp=0.6)
    # 155Hz sits above a 100Hz corner but not far above it, so it loses about a
    # sixth of its level. Measured: 0.345 -> 0.294, against 0.345 -> 0.009 for
    # 40Hz rumble — a 39x difference between what is kept and what is removed.
    assert voice.loudness(voice.without_rumble(voiced)) > 0.4 * 0.6


def test_the_voice_survives_the_rumble_being_removed():
    """Both together, which is what a real take is: what comes out has to be
    the 155Hz part, not a filter ringing at its corner."""
    both = voice.mixed(_tone(40, 1.0, amp=0.5), _tone(155, 1.0, amp=0.5))
    clean = voice.without_rumble(both)
    assert voice.loudness(clean) > 0.25
    assert voice.loudness(voice.without_rumble(_tone(40, 1.0, amp=0.5))) \
        < voice.loudness(clean) / 5


def test_audio_that_is_not_a_wav_is_handed_back_untouched():
    """An .m4a or a truncated file must not be silently mangled into noise."""
    assert voice.without_rumble(b"not a wav at all") == b"not a wav at all"


def test_every_take_is_cleaned_on_its_way_into_a_clone():
    import inspect
    assert "without_rumble" in inspect.getsource(voice.clone_from_sample)


# --- in his voice, talk the way he talks -----------------------------------------------

def test_the_brain_is_told_to_talk_like_him_when_it_is_his_voice():
    """His clone reading assistant-register English is the uncanny bit: the
    voice is his and the words are nobody's. His actual idiom is on record in
    voice.CLONE_SCRIPTS — short sentences, "bro", "na" as a tag question."""
    from app import call_mind
    his = call_mind.persona("Harika", "a quick word", as_him=True)
    assert "arun's own voice" in his.lower()
    assert '"na" as a tag question' in his and "does not pad" in his
    plain = call_mind.persona("Harika", "a quick word")
    assert "talk the way" not in plain, "the assistant voice still talks as Asta"


def test_it_does_not_answer_to_being_him():
    """Speaking in his voice is his call and he made it. Saying "yes, this is
    Arun" to someone who asks outright is a different person's decision about
    what they are told, and she is not in the room to make it."""
    from app import call_mind
    his = call_mind.persona("Harika", "a quick word", as_him=True)
    assert "do not deny it" in his and "Never state that you are Arun" in his


def test_he_is_never_made_to_call_a_colleague_bro_by_default():
    """Live, 25 Sep: the brief told it to say "bro", so it opened a call to
    Harika with "quick one bro". That is what he calls Vinish. The brain cannot
    tell which colleagues those are, so it uses no term of address at all until
    they use one first."""
    from app import call_mind
    his = call_mind.persona("Harika", "a quick word", as_him=True)
    assert "NO term of address unless they use one with you first" in his
    assert "guessing wrong is worse" in his
