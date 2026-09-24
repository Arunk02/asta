"""Hearing and being heard, decided from the call's own readings.

What these hold: a person saying hello IS the answer; a quiet pick-up still
opens once media flows; a ringing screen means sound on the line is not a
person; a line that left no energy in the outgoing stream was not said; and a
turn ends at their pause, not at a fixed timer. See app/call_rtc.py for why the
screen and the Mac's microphone are no longer what a call is judged by.
"""

from __future__ import annotations

import asyncio
import base64
import os

import pytest

from app import call_rtc, meetings, store


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def nap(self, s):
        self.t += s


class Frame:
    """One frame of the call page, answering the way the injected script does."""

    def __init__(self, readings, gum=1, said=2.5, recorded=None):
        self.readings = list(readings)
        self.last = self.readings[-1] if self.readings else {}
        self.gum = gum
        self.said = said
        self.spoken = []
        self.recorded = recorded
        self.recording = None

    async def evaluate(self, js, arg=None):
        if "__asta.snapshot()" in js:
            if self.readings:
                self.last = self.readings.pop(0)
            return self.last
        if "__asta.tap()" in js:
            return 0
        if "__asta.record(" in js:
            self.recording = "true" in js
            return None
        if "gum: window.__asta.gum" in js:
            return {"gum": self.gum, "pcs": 1}
        if "__asta.say(b)" in js:
            self.spoken.append(base64.b64decode(arg))
            return self.said
        if "__asta.take(" in js:
            wav = self.recorded or b""
            return {"wav": base64.b64encode(wav).decode(), "seconds": 1.5 if wav else 0.0,
                    "peak": 0.5 if wav else 0.0}
        return None


class Page:
    def __init__(self, frame):
        self.frames = [frame]

    def is_closed(self):
        return False


class Ctx:
    def __init__(self, frame):
        self.pages = [Page(frame)]


def reading(**kw):
    base = {"pcs": 1, "connected": 1, "closed": 0, "inPackets": 0, "inEnergy": 0.0,
            "outBytes": 0, "outEnergy": 0.0, "gum": 1, "level": 0.0, "loudMs": 0,
            "quietMs": -1}
    base.update(kw)
    return base


def wait(ctx, clock, **kw):
    return asyncio.run(call_rtc.wait_for_them(ctx, clock=clock, nap=clock.nap, **kw))


# --- did they pick up? ----------------------------------------------------------

def test_their_hello_is_the_answer():
    """22 Sep: she picked up and talked for forty seconds. Her voice was the answer."""
    clock = Clock()
    ctx = Ctx(Frame([reading(inPackets=10), reading(inPackets=40, loudMs=900, quietMs=200)]))
    assert wait(ctx, clock, seconds=30) == "voice"
    assert clock.t < 2, "it waited instead of answering her"


def test_a_silent_pick_up_still_opens_once_media_flows():
    clock = Clock()
    ctx = Ctx(Frame([reading(inPackets=10 + 20 * i) for i in range(40)]))
    assert wait(ctx, clock, seconds=30, quiet_open=4.0) == "connected"
    assert clock.t < 6


def test_nothing_on_the_line_is_no_answer():
    clock = Clock()
    ctx = Ctx(Frame([reading(connected=0)]))
    assert wait(ctx, clock, seconds=10) == "no answer"


def test_a_call_that_closes_after_connecting_has_ended():
    clock = Clock()
    ctx = Ctx(Frame([reading(inPackets=5), reading(connected=0, closed=1)]))
    assert wait(ctx, clock, seconds=10) == "ended"


def test_sound_while_the_screen_still_rings_is_not_a_person():
    """A ringback tone is sound on the line; it is not somebody saying hello."""
    clock = Clock()
    ctx = Ctx(Frame([reading(inPackets=10 * i, loudMs=900, quietMs=100) for i in range(30)]))

    async def ringing():
        return True

    assert wait(ctx, clock, seconds=5, ringing=ringing) == "no answer"


def test_a_person_talking_outranks_a_screen_stuck_on_ringing():
    """If the screen never stops saying "ringing", her talking still answers it."""
    clock = Clock()
    ctx = Ctx(Frame([reading(inPackets=10 * i, loudMs=400 * i, quietMs=100) for i in range(30)]))

    async def ringing():
        return True

    assert wait(ctx, clock, seconds=10, ringing=ringing) == "voice"
    assert clock.t < 2.5


def test_readings_from_every_frame_make_one_view():
    merged = call_rtc.merge([reading(inPackets=5, loudMs=0), reading(inPackets=7, loudMs=500, quietMs=300)])
    assert merged["inPackets"] == 12 and merged["loudMs"] == 500 and merged["quietMs"] == 300


# --- was I heard? -----------------------------------------------------------------

def test_a_line_that_left_energy_in_the_call_was_said():
    frame = Frame([reading(outEnergy=0.0), reading(outEnergy=0.7)])
    out = asyncio.run(call_rtc.say(Ctx(frame), b"RIFFwav"))
    assert out["sent"] is True and frame.spoken == [b"RIFFwav"]


def test_a_line_that_left_nothing_was_not_said():
    frame = Frame([reading(outEnergy=0.2), reading(outEnergy=0.2)])
    out = asyncio.run(call_rtc.say(Ctx(frame), b"RIFFwav"))
    assert out["sent"] is False


def test_it_will_not_speak_if_teams_never_took_its_microphone():
    frame = Frame([reading()], gum=0)
    with pytest.raises(RuntimeError, match="never took Asta's microphone"):
        asyncio.run(call_rtc.say(Ctx(frame), b"RIFFwav"))
    assert frame.spoken == []


# --- what did they say? ------------------------------------------------------------

def test_a_turn_ends_at_their_pause_and_is_transcribed():
    clock = Clock()
    frame = Frame([reading(loudMs=0), reading(loudMs=600, quietMs=100),
                   reading(loudMs=1800, quietMs=300), reading(loudMs=1800, quietMs=1300)],
                  recorded=b"RIFFtheir-voice")
    heard = []

    async def transcribe(wav, filename="", **kw):
        heard.append((wav, filename))
        return " yes, I can hear you "

    got = asyncio.run(call_rtc.hear_turn(Ctx(frame), wait=10, clock=clock, nap=clock.nap,
                                         transcribe=transcribe))
    assert got == {"text": "yes, I can hear you", "seconds": 1.5, "spoke": True}
    assert heard == [(b"RIFFtheir-voice", "turn.wav")]
    assert frame.recording is False, "recording was left running after the turn"


def test_silence_is_not_a_turn():
    clock = Clock()
    frame = Frame([reading(loudMs=0)])

    async def transcribe(wav, filename="", **kw):
        raise AssertionError("nothing was said, so nothing is transcribed")

    got = asyncio.run(call_rtc.hear_turn(Ctx(frame), wait=5, clock=clock, nap=clock.nap,
                                         transcribe=transcribe))
    assert got["spoke"] is False and got["text"] == ""


# --- the call path on top of it ------------------------------------------------------

@pytest.fixture
def rtc_call(monkeypatch):
    monkeypatch.setenv("ASTA_CALL_RTC", "1")
    store.kv_set("teams_in_call", "call:A colleague")
    meetings._CALL.clear()
    meetings._CALL.update(ctx=object(), page=object(), rtc=True, answered_at=meetings._now())

    async def synth(text, voice_name=""):
        return b"RIFFline"

    monkeypatch.setattr(meetings, "synth", synth)
    yield
    meetings._CALL.clear()
    store.kv_set("teams_in_call", "")


def test_saying_it_reports_what_was_sent(rtc_call, monkeypatch):
    async def say(ctx, wav, **kw):
        return {"seconds": 2.0, "sent": True, "energy": 0.6}

    monkeypatch.setattr(call_rtc, "say", say)
    out = asyncio.run(meetings.say_in_call("Hi, this is Asta"))
    assert "said it in the call" in out
    assert store.kv_get("teams_last_spoken") == "Hi, this is Asta"


def test_a_line_teams_did_not_send_is_never_reported_as_said(rtc_call, monkeypatch):
    async def say(ctx, wav, **kw):
        return {"seconds": 2.0, "sent": False, "energy": 0.0}

    monkeypatch.setattr(call_rtc, "say", say)
    with pytest.raises(RuntimeError, match="NOT said"):
        asyncio.run(meetings.say_in_call("Hi, this is Asta"))


def test_hanging_up_leaves_his_microphone_alone(rtc_call, monkeypatch):
    """Nothing was borrowed, so "restoring" would only move him off his headset."""
    touched = []

    async def restore(page):
        touched.append(page)

    class Closer:
        async def close(self):
            pass

    meetings._CALL.update(ctx=Closer())
    monkeypatch.setattr(meetings.call_audio, "_restore_mic", restore)
    asyncio.run(meetings.leave())
    assert touched == []


def test_every_teams_browser_gets_astas_microphone_before_teams_loads(monkeypatch):
    from app import teams_bridge
    monkeypatch.setenv("ASTA_CALL_RTC", "1")
    added = []

    class Ctx2:
        async def add_init_script(self, script):
            added.append(script)

    async def bare(pw, channel, headless):
        return Ctx2()

    monkeypatch.setattr(teams_bridge, "_open_ctx_bare", bare)
    asyncio.run(teams_bridge._open_ctx(None, "chrome", True))
    assert added == [call_rtc.INIT_JS]


# --- the real thing, in a real browser (opt-in: ASTA_BROWSER_TESTS=1) -----------------

@pytest.mark.skipif(os.environ.get("ASTA_BROWSER_TESTS") != "1",
                    reason="drives a real Chrome; run with ASTA_BROWSER_TESTS=1")
def test_loopback_in_a_real_browser(tmp_path):
    """Asta's microphone → a real WebRTC connection → the far side hears it."""
    import io
    import math
    import wave

    from playwright.async_api import async_playwright

    page_html = """<html><body><script>
    window.loop = async () => {
      const s = await navigator.mediaDevices.getUserMedia({audio: true});
      const a = new RTCPeerConnection(), b = new RTCPeerConnection();
      a.onicecandidate = e => e.candidate && b.addIceCandidate(e.candidate);
      b.onicecandidate = e => e.candidate && a.addIceCandidate(e.candidate);
      s.getTracks().forEach(t => a.addTrack(t, s));
      const o = await a.createOffer(); await a.setLocalDescription(o); await b.setRemoteDescription(o);
      const r = await b.createAnswer(); await b.setLocalDescription(r); await a.setRemoteDescription(r);
    };</script></body></html>"""

    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"".join(int(9000 * math.sin(2 * math.pi * 300 * i / 24000)).to_bytes(2, "little", signed=True)
                               for i in range(24000 * 2)))

    async def go():
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(tmp_path / "profile"), headless=True,
                channel=os.environ.get("ASTA_BROWSER_CHANNEL", "chrome") or None,
                permissions=["microphone"],
                args=["--autoplay-policy=no-user-gesture-required",
                      "--use-fake-ui-for-media-stream"])
            try:
                await call_rtc.install(ctx)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.route("https://asta.test/**", lambda r: r.fulfill(
                    status=200, content_type="text/html", body=page_html))
                await page.goto("https://asta.test/")
                assert await call_rtc.mic_ready(page) > 0.01
                await page.evaluate("window.loop()")
                seen = []
                got = await call_rtc.wait_for_them(ctx, seconds=10, quiet_open=1.0, log=seen.append)
                assert got == "connected", seen[-3:]
                # One page hears itself here, which the call rightly treats as
                # echo — so arrival is read from the raw level, not "their voice".
                # The two-sided rehearsal (app/call_rehearsal.py) covers a real far end.
                levels = []

                async def sample():
                    for _ in range(30):
                        levels.append(await page.evaluate("window.__asta.level || 0"))
                        await asyncio.sleep(0.05)

                sampling = asyncio.ensure_future(sample())
                out = await call_rtc.say(ctx, buf.getvalue())
                await sampling
                return out, max(levels)
            finally:
                await ctx.close()

    out, loudest = asyncio.run(go())
    assert out["sent"] is True
    assert loudest > 0.05, "the far side never heard it"


# --- 22 Sep, third call: she said hello into 32 seconds of setup ------------------------

def test_her_voice_is_heard_from_the_calls_own_energy_even_if_the_tap_is_silent():
    """Two independent ears: WebRTC's energy count and Asta's tap. Either one hears her."""
    clock = Clock()
    ctx = Ctx(Frame([reading(inPackets=10, inEnergy=0.10), reading(inPackets=30, inEnergy=0.26)]))
    assert wait(ctx, clock, seconds=10) == "voice"


def test_nothing_slow_stands_between_the_ring_and_listening(monkeypatch):
    """Captions took 32 s to switch on after the call connected; she hung up before
    Asta started listening. On Asta's own microphone they are not switched on at all."""
    import inspect
    src = inspect.getsource(meetings.call_person)
    tail = src[src.index('store.kv_set("teams_in_call", f"call:{title}")'):]
    assert "if rtc:" in tail and tail.index("if rtc:") < tail.index("start_captions")


def test_a_slow_answer_is_covered_by_one_moment_not_dead_air(monkeypatch):
    from app import conversation
    said = []

    async def say(text, voice_name=""):
        said.append(text)
        return "said"

    async def slow():
        await asyncio.sleep(0.3)
        return "Yes, I can hear you fine."

    monkeypatch.setattr(meetings, "say_in_call", say)
    monkeypatch.setattr(conversation, "MOMENT_AFTER", 0.05)

    async def go():
        return await conversation._answer_without_dead_air(asyncio.get_event_loop().create_task(slow()))

    assert asyncio.run(go()) == "Yes, I can hear you fine."
    assert len(said) == 1 and said[0] in conversation._MOMENTS


# --- 22 Sep, fifth call: Teams voicemail answered, and Asta greeted a recording ---------

def _greet(frames, transcribed=""):
    clock = Clock()
    calls = []

    async def transcribe(wav, filename="", **kw):
        calls.append(filename)
        return transcribed

    got = asyncio.run(call_rtc.greeting(Ctx(Frame(frames, recorded=b"RIFFgreeting")),
                                        clock=clock, nap=clock.nap, transcribe=transcribe))
    return got, calls, clock.t


def test_a_person_says_hello_and_waits_so_asta_answers_at_once():
    got, calls, took = _greet([reading(loudMs=600, quietMs=100), reading(loudMs=700, quietMs=800)])
    assert got["voicemail"] is False and calls == [], "a short hello must not wait for a transcription"
    assert took < 1.0


def test_a_recording_that_talks_on_is_checked_and_recognised_as_voicemail():
    got, calls, _ = _greet([reading(loudMs=500 * i, quietMs=50) for i in range(1, 12)] +
                           [reading(loudMs=5500, quietMs=900)],
                           transcribed="Alex Kumar is not available. Please leave a message after the tone.")
    assert got["voicemail"] is True and calls == ["greeting.wav"]


def test_a_colleague_who_talks_a_lot_at_pick_up_is_still_a_person():
    got, _, _ = _greet([reading(loudMs=500 * i, quietMs=50) for i in range(1, 8)] +
                       [reading(loudMs=3500, quietMs=900)],
                       transcribed="Hi Arun, yes tell me, I was about to call you about the build.")
    assert got["voicemail"] is False


def test_the_teams_voicemail_prompt_that_fooled_it_is_recognised():
    assert call_rtc.is_voicemail("key for more options.")
    assert not call_rtc.is_voicemail("Yes, we can go through.")


def test_the_call_brain_never_sees_his_refused_api_key(monkeypatch):
    """Given the key, the CLI tries it instead of his subscription and stalls — the
    fifth call fell back to the slow path because of exactly that."""
    from app import call_mind
    seen = {}

    async def spawn(*cmd, **kw):
        seen["env"] = kw.get("env") or {}
        raise FileNotFoundError("no brain in tests")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-refused")
    monkeypatch.setattr(call_mind.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(FileNotFoundError):
        asyncio.run(call_mind.start("A colleague", "a quick word"))
    assert "ANTHROPIC_API_KEY" not in seen["env"]


# --- 22 Sep, sixth call: "long lag", "same sequence", "not dynamic" ------------------------

class TalkingFrame(Frame):
    """A frame where the other person starts talking while Asta speaks."""

    def __init__(self, readings):
        super().__init__(readings)
        self.stopped = False

    async def evaluate(self, js, arg=None):
        if "__asta.say(b)" in js:
            for _ in range(40):
                if self.stopped:
                    break
                await asyncio.sleep(0.01)
            return 1.0
        if "__asta.stop()" in js:
            self.stopped = True
            return True
        return await super().evaluate(js, arg)


def test_when_they_talk_over_asta_it_stops_and_listens():
    clock = Clock()
    frame = TalkingFrame([reading(outEnergy=0.0)] + [reading(outEnergy=0.3, loudMs=0)] * 5 +
                         [reading(outEnergy=0.5, loudMs=600, quietMs=100)] * 5)
    out = asyncio.run(call_rtc.say(Ctx(frame), b"RIFFline", interruptible=True,
                                   clock=clock, nap=clock.nap))
    assert out["interrupted"] is True and frame.stopped
    assert frame.recording is True, "their first words must stay recorded for the next turn"


class FakeProc:
    """A call brain that streams a reply, then says it is done 1.4 s later."""

    def __init__(self, deltas):
        import json as _j
        lines = [{"type": "stream_event", "event": {"type": "content_block_delta",
                                                    "delta": {"type": "text_delta", "text": d}}}
                 for d in deltas]
        lines += [{"type": "assistant", "message": {"content": [{"type": "text", "text": "".join(deltas)}]}},
                  "PAUSE", {"type": "result"}]
        self.lines = [x if x == "PAUSE" else (_j.dumps(x) + "\n").encode() for x in lines]
        self.stdin = self

    def write(self, data):
        pass

    async def drain(self):
        pass

    async def readline(self):
        item = self.lines.pop(0)
        if item == "PAUSE":
            await asyncio.sleep(0.2)          # the CLI's own "done" comes later
            item = self.lines.pop(0)
        return item

    @property
    def stdout(self):
        return self


def test_the_last_sentence_is_said_when_written_not_when_the_cli_says_done():
    from app import call_mind
    mind = call_mind.Mind(FakeProc(["Great, thanks. ", "How does my pace sound?"]))

    async def go():
        got, stamps = [], []
        loop = asyncio.get_event_loop()
        start = loop.time()
        async for sentence in mind.sentences("it's good"):
            got.append(sentence)
            stamps.append(loop.time() - start)
        return got, stamps

    got, stamps = asyncio.run(go())
    assert got == ["Great.", "Thanks.", "How does my pace sound?"]
    assert stamps[-1] < 0.15, "the last sentence waited for the CLI's result event"


def _reply_with(monkeypatch, mind, theirs="yes", quick=0.05):
    from app import conversation
    said = []

    async def say(text, voice_name=""):
        said.append(text)
        return "said"

    async def synth(text, voice_name=""):
        return b"RIFF"

    monkeypatch.setattr(meetings, "say_in_call", say)
    monkeypatch.setattr(meetings, "synth", synth)
    monkeypatch.setattr(conversation, "QUICK_REACTION_AFTER", quick)
    meetings._CALL.clear()
    ended, interrupted = asyncio.run(conversation._speak_reply(mind, theirs, [], [], True))
    return said, ended, interrupted


def test_a_slow_brain_is_covered_by_a_reaction_from_their_own_words(monkeypatch):
    """The brain's speed on the day decides nothing about when Asta reacts, and its
    own "Got it." after Asta already said "Great." would be a stutter."""
    class SlowMind:
        async def sentences(self, theirs, **kw):
            await asyncio.sleep(0.2)
            yield "Got it."
            yield "Anything else? [END]"

    said, ended, interrupted = _reply_with(monkeypatch, SlowMind(), theirs="Yes.")
    assert said == ["Great.", "Anything else?"] and ended is True and interrupted is False


def test_a_quick_brain_speaks_for_itself(monkeypatch):
    class QuickMind:
        async def sentences(self, theirs, **kw):
            yield "Perfect."
            yield "Anything else?"

    said, ended, interrupted = _reply_with(monkeypatch, QuickMind(), quick=1.0)
    assert said == ["Perfect.", "Anything else?"]


def test_words_are_transcribed_at_the_first_short_pause_and_reused_if_it_was_the_end():
    clock = Clock()
    frame = Frame([reading(loudMs=0), reading(loudMs=800, quietMs=100),
                   reading(loudMs=900, quietMs=400), reading(loudMs=900, quietMs=600),
                   reading(loudMs=900, quietMs=800)], recorded=b"RIFFher-words")
    calls = []

    async def transcribe(wav, filename="", **kw):
        calls.append(wav)
        return "it's good"

    got = asyncio.run(call_rtc.hear_turn(Ctx(frame), wait=10, clock=clock, nap=clock.nap,
                                         transcribe=transcribe))
    assert got["text"] == "it's good" and len(calls) == 1, "the early look should have been reused"


def test_if_they_go_on_after_the_first_pause_the_whole_turn_is_transcribed():
    clock = Clock()
    frame = Frame([reading(loudMs=0), reading(loudMs=800, quietMs=100),
                   reading(loudMs=900, quietMs=400), reading(loudMs=1500, quietMs=50),
                   reading(loudMs=1600, quietMs=800)], recorded=b"RIFFher-words")
    calls = []

    async def transcribe(wav, filename="", **kw):
        calls.append(wav)
        return "it's good, but a bit slow"

    got = asyncio.run(call_rtc.hear_turn(Ctx(frame), wait=10, clock=clock, nap=clock.nap,
                                         transcribe=transcribe))
    assert got["text"] == "it's good, but a bit slow" and len(calls) == 2


# --- 22 Sep, seventh call: her short "yes" was ignored and she hung up ----------------------

def test_a_short_yes_is_an_answer():
    """341 ms of "yes" fell under the pick-up threshold; a reply needs far less."""
    clock = Clock()
    frame = Frame([reading(loudMs=0), reading(loudMs=256, quietMs=80),
                   reading(loudMs=341, quietMs=700)], recorded=b"RIFFyes")

    async def transcribe(wav, filename="", **kw):
        return "Yes."

    got = asyncio.run(call_rtc.hear_turn(Ctx(frame), wait=10, clock=clock, nap=clock.nap,
                                         transcribe=transcribe))
    assert got == {"text": "Yes.", "seconds": 1.5, "spoke": True}


def test_the_plain_greeting_is_made_before_anything_else(monkeypatch):
    """It waited behind thirteen other lines and started seven seconds after her hello."""
    from app import call_mind, conversation
    made = []

    async def synth(text, voice_name=""):
        made.append(text)
        return b"RIFF"

    async def no_mind():
        raise RuntimeError("no brain")

    monkeypatch.setattr(meetings, "synth", synth)

    async def go():
        task = asyncio.ensure_future(no_mind())
        return await conversation._compose_opener(task, "Hi, this is Asta.")

    from app import voice
    assert asyncio.run(go()) == "Hi, this is Asta."
    assert made[0] == voice.strip_voice_instruction("Hi, this is Asta.")
    assert {voice.strip_voice_instruction(r) for r in call_mind.REACTIONS} <= set(made)


def test_a_reaction_at_the_start_of_the_reply_is_played_before_the_rest_is_written():
    """The model writes "Great, glad to hear that." — the "Great." is ready audio,
    so it goes out the moment it is written."""
    from app import call_mind
    mind = call_mind.Mind(FakeProc(["Great, glad ", "to hear that. ", "Anything else?"]))

    async def go():
        return [s async for s in mind.sentences("it's better")]

    assert asyncio.run(go()) == ["Great.", "Glad to hear that.", "Anything else?"]


# --- 22 Sep: the call brain's session limit came back as the "reply" -------------------------

class RefusingProc(FakeProc):
    """The CLI out of its window: one whole message, the limit notice, then an error."""

    def __init__(self):
        import json as _j
        self.stdin = self
        self.lines = [(_j.dumps(x) + "\n").encode() for x in (
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "You've hit your session limit · resets 2:40pm (Asia/Calcutta)"}]}},
            {"type": "result", "is_error": True, "result": "You've hit your session limit"})]


def test_a_limit_notice_is_never_spoken_as_a_reply():
    from app import call_mind
    mind = call_mind.Mind(RefusingProc())
    heard = []

    async def go():
        async for sentence in mind.sentences("Yes."):
            heard.append(sentence)

    with pytest.raises(call_mind.Unavailable):
        asyncio.run(go())
    assert heard == [], "the limit notice would have been read out to a colleague"


def test_nobody_is_rung_when_the_call_brain_is_out_of_its_window(monkeypatch):
    from app import call_mind, conversation
    rang = []

    async def out_of_window(*a, **kw):
        raise call_mind.Unavailable("You've hit your session limit · resets 2:40pm")

    async def call_person(who, video=False):
        rang.append(who)
        return who

    async def warm():
        return None

    monkeypatch.setattr(call_mind, "start", out_of_window)
    monkeypatch.setattr(meetings, "call_person", call_person)
    from app import voice
    monkeypatch.setattr(voice, "warm_the_ears", warm)
    out = asyncio.run(conversation.converse("A colleague", "a quick word"))
    assert rang == [] and "Didn't call" in out and "session limit" in out


def test_running_out_mid_call_is_a_polite_goodbye_not_an_error_read_aloud(monkeypatch):
    from app import call_mind, conversation
    said = []

    async def say(text, voice_name=""):
        said.append(text)
        return "said"

    async def synth(text, voice_name=""):
        return b"RIFF"

    class OutMind:
        async def sentences(self, theirs, **kw):
            raise call_mind.Unavailable("You've hit your session limit")
            yield  # pragma: no cover

    monkeypatch.setattr(meetings, "say_in_call", say)
    monkeypatch.setattr(meetings, "synth", synth)
    meetings._CALL.clear()
    ended, interrupted = asyncio.run(conversation._speak_reply(OutMind(), "yes", [], [], True))
    assert said == [conversation._DROPPING_OFF] and ended is True


# --- 22 Sep, the call to a second colleague: "Haruki" two hundred times ----------------------

def test_whisper_repeating_itself_is_not_an_answer():
    assert call_rtc.garbled("Haruki " * 200)
    assert call_rtc.garbled("the the the the the the the")
    assert not call_rtc.garbled("Yes, tell me, what is this call about?")
    assert not call_rtc.garbled("Yes.")


def test_a_garbled_transcription_becomes_unheard_not_a_reply():
    async def listen(wav, filename="", **kw):
        return "Haruki " * 200

    assert asyncio.run(call_rtc._quietly(listen, b"RIFF")) == ""


def test_a_transcription_that_runs_on_is_cut_off(monkeypatch):
    monkeypatch.setattr(call_rtc, "TRANSCRIBE_TIMEOUT", 0.05)

    async def listen(wav, filename="", **kw):
        await asyncio.sleep(1)
        return "too late"

    assert asyncio.run(call_rtc._quietly(listen, b"RIFF")) == ""


def test_the_greeting_cannot_be_interrupted_but_what_follows_can():
    import inspect
    from app import conversation
    src = inspect.getsource(conversation.converse)
    greeting = src.index("await meetings.say_in_call(opener)")
    assert src.rindex('meetings._CALL["barge_in"] = False', 0, greeting) < greeting
    assert src.index('meetings._CALL["barge_in"] = rtc', greeting) > greeting


def test_nobody_is_rung_when_the_voice_service_is_down(monkeypatch):
    from app import call_mind, conversation, voice
    monkeypatch.setenv("ASTA_CALL_RTC", "1")
    rang = []

    async def down():
        return False

    async def call_person(who, video=False):
        rang.append(who)
        return who

    async def mind(*a, **kw):
        raise RuntimeError("not needed")

    monkeypatch.setattr(voice, "available", down)
    monkeypatch.setattr(call_mind, "start", mind)
    monkeypatch.setattr(meetings, "call_person", call_person)
    out = asyncio.run(conversation.converse("A colleague", "a quick word"))
    assert rang == [] and "voice service" in out


def test_asta_own_words_are_taken_off_what_was_heard():
    said = "Great. Can you hear me alright on your end?"
    assert call_rtc.strip_echo("on your end? Yes.", said) == "Yes"
    assert call_rtc.strip_echo("Can you hear me alright on your end?", said) == ""
    assert call_rtc.strip_echo("Yes, I can hear you.", said) == "Yes, I can hear you"


def test_a_sentence_that_stops_mid_thought_is_given_time():
    assert call_rtc.unfinished("So what I think is")
    assert call_rtc.unfinished("I was going to check with the team and")
    assert not call_rtc.unfinished("We should try it again tomorrow morning.")
    assert not call_rtc.unfinished("Yes.")


def test_the_listeners_mm_hm_never_touches_their_recording(monkeypatch):
    """Said while their words are still being gathered: an interruptible line
    would restart the recording and wipe the sentence it is acknowledging."""
    from app import call_rtc as rtc
    calls = []

    async def say(ctx, wav, interruptible=False, **kw):
        calls.append(interruptible)
        return {"sent": True}

    async def synth(text, voice_name=""):
        return b"RIFF"

    monkeypatch.setattr(rtc, "say", say)
    monkeypatch.setattr(meetings, "synth", synth)
    meetings._CALL.clear()
    meetings._CALL.update(ctx=object(), barge_in=True)
    asyncio.run(rtc.say_quick("Mm-hm."))
    meetings._CALL.clear()
    assert calls == [False]


def test_a_line_teams_muted_is_said_again_once_unmuted(rtc_call, monkeypatch):
    from app import voice
    sent = iter([False, True])
    said = []

    async def say(ctx, wav, **kw):
        said.append(wav)
        return {"seconds": 1.0, "sent": next(sent), "energy": 0.5}

    async def muted(page):
        return {"muted": True}

    async def unmute(page):
        return True

    async def window(page):
        return page

    monkeypatch.setattr(call_rtc, "say", say)
    monkeypatch.setattr(voice, "mic_is_live", muted)
    monkeypatch.setattr(voice, "ensure_unmuted", unmute)
    monkeypatch.setattr(meetings, "_follow_call_window", window)
    assert "said it in the call" in asyncio.run(meetings.say_in_call("Hi, this is Asta"))
    assert len(said) == 2


def test_a_line_that_did_not_go_out_is_not_repeated_on_a_guess(rtc_call, monkeypatch):
    """Unreadable mute state: say it once, report it unsaid — never say it twice."""
    from app import voice
    said = []

    async def say(ctx, wav, **kw):
        said.append(wav)
        return {"seconds": 1.0, "sent": False, "energy": 0.0}

    async def unreadable(page):
        return {}

    async def window(page):
        return page

    monkeypatch.setattr(call_rtc, "say", say)
    monkeypatch.setattr(voice, "mic_is_live", unreadable)
    monkeypatch.setattr(meetings, "_follow_call_window", window)
    with pytest.raises(RuntimeError, match="NOT said"):
        asyncio.run(meetings.say_in_call("Hi, this is Asta"))
    assert len(said) == 1


# --- what the 24 Sep call with a colleague exposed -----------------------------------

def test_a_greeting_gets_no_canned_word_in_front_of_the_answer():
    """Live, 24 Sep: she said "Hello?" and Asta said "Sure." — then "Hello, how
    can I help you?" and Asta said "Sure." again. Every sentence ending in a
    question mark was getting "Sure.", a word that answers a request, not a
    question. Asta's own next sentence greets her; nothing belongs in front."""
    from app import conversation
    assert conversation.quick_reaction("Hello?") == ""
    assert conversation.quick_reaction("Hello, how can I help you?") == ""
    assert conversation.quick_reaction("hi") == ""


def test_a_question_gets_a_thinking_noise_not_an_answer_word():
    from app import conversation
    assert conversation.quick_reaction("can you send me the link?") == "Mm."


def test_the_reactions_that_do_fit_still_fire():
    from app import conversation
    assert conversation.quick_reaction("No, I didn't get a chance") == "No worries."
    assert conversation.quick_reaction("yes that works") == "Great."
    assert conversation.quick_reaction("who is this?") == "Oh, sorry."
    assert conversation.quick_reaction("thanks, bye") == "Sounds good."


@pytest.mark.parametrize("sentence, reaction, spoken", [
    # The live one: "No worries." then "No worries — do you have a rough sense…"
    ("No worries — do you have a rough sense of when?", "No worries.",
     "Do you have a rough sense of when?"),
    ("Great, I'll check with Arun.", "Great.", "I'll check with Arun."),
    ("Got it. The build is green.", "Got it.", "The build is green."),
    # Not an echo: left exactly as the brain wrote it.
    ("The build is green.", "Got it.", "The build is green."),
    ("No worries.", "No worries.", "No worries."),      # nothing left — caller drops it
])
def test_an_opener_asta_just_said_is_not_said_twice(sentence, reaction, spoken):
    from app import conversation
    assert conversation.without_echo(sentence, reaction) == spoken


def test_the_persona_forbids_introducing_itself_twice():
    from app import call_mind
    assert "do NOT introduce yourself" in call_mind._PERSONA
    assert "ask ONCE for a rough sense" in call_mind._PERSONA


def test_the_ending_she_called_out_of_sync(monkeypatch):
    """Her feedback on the 24 Sep call was that the conclusion was off. It was:

        Her:  No, apart from this can you discuss any other things?
        Asta: No worries.                      <- answers the word "no", not the question
        Asta: That's actually the main thing I called about…
        Her:  Okay
        Asta: Great.                           <- filler
        Asta: Sounds good — thanks for the update, Harika!   <- second filler, and
                                                  she had given no update

    Three separate faults, one exchange."""
    from app import conversation
    # A question wins over the word it happens to start with.
    assert conversation.quick_reaction(
        "No, apart from this can you discuss any other things?") == "Mm."
    # A bare acknowledgement gets nothing: what follows is the closing line.
    assert conversation.quick_reaction("Okay") == ""
    assert conversation.quick_reaction("thanks") == ""
    assert conversation.quick_reaction("alright") == ""
    # And the closing itself is no longer allowed to invent an update.
    from app import call_mind
    assert "thanks for the" in call_mind._PERSONA and "did not give" in call_mind._PERSONA
    assert "ANSWER it" in call_mind._PERSONA


def test_a_statement_still_gets_a_reaction_so_nobody_waits_in_silence():
    """The fillers exist because the brain takes 3-4 s. Silencing the wrong ones
    must not silence the right ones."""
    from app import conversation
    assert conversation.quick_reaction("the build is red") == "Got it."
    assert conversation.quick_reaction("No, I didn't get a chance") == "No worries."
    assert conversation.quick_reaction("yes that works for me") == "Great."
