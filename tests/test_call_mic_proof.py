"""Proof that Asta can be heard: taken once, before the dial, and then believed.

22 Sep: a colleague picked up Asta's call and was hung up on in silence. The
tone check before the dial had passed, and the voice self-test minutes later
measured a full-scale peak through the same browser. But `say_in_call` read the
microphone again with nothing playing, saw BlackHole's quiet as "macOS denied
the mic", and refused to speak. A quiet line is not a denied microphone.

Also here: a check that could not measure at all used to let the dial through.
"""

from __future__ import annotations

import asyncio
import io
import wave

import pytest

from app import call_audio, meetings, store, voice


def _wav(seconds: float = 0.2, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(rate * seconds))
    return buf.getvalue()


@pytest.fixture
def live_call(monkeypatch):
    """A connected call Asta placed: a page, somebody on the line."""
    store.kv_set("teams_in_call", "call:A colleague")
    monkeypatch.setattr(meetings, "can_speak", lambda: True)
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "BlackHole 2ch")

    async def connected(page):
        return "connected"

    async def gen(text, profile="", engine="", voice=""):
        return _wav()

    async def yes(*a, **k):
        return True

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(meetings, "call_state", connected)
    monkeypatch.setattr(meetings, "ensure_unmuted", yes)
    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(call_audio, "set_call_mic", yes)
    monkeypatch.setattr(call_audio, "_restore_mic", nothing)
    played = []
    monkeypatch.setattr(voice, "play_to_device", lambda wav, device="": played.append(device) or 0.2)
    meetings._CALL.clear()
    meetings._CALL.update(page=object(), answered_at=meetings._now())
    yield played
    meetings._CALL.clear()
    store.kv_set("teams_in_call", "")


def _mic_reads(monkeypatch, peak: float, error: str = ""):
    async def reads(page, ms: int = 900):
        return {"error": error} if error else {"peak": peak, "label": "BlackHole 2ch"}
    monkeypatch.setattr(voice, "browser_mic_delivers", reads)


def test_a_quiet_line_does_not_undo_the_proof_taken_before_the_dial(live_call, monkeypatch):
    """The 22 Sep hang-up: proven before the dial, silent when nothing plays."""
    meetings._CALL["mic_proven"] = 0.98
    _mic_reads(monkeypatch, 0.0)
    out = asyncio.run(meetings.say_in_call("Hi, this is Asta"))
    assert "said it in the call" in out
    assert live_call == ["BlackHole 2ch"], "the line was never played"


def test_without_proof_it_listens_while_it_speaks_and_keeps_the_proof(live_call, monkeypatch):
    """A meeting joined or a call answered has no dial-time proof: the line itself is the test."""
    _mic_reads(monkeypatch, 0.62)
    out = asyncio.run(meetings.say_in_call("Hi, this is Asta"))
    assert "said it in the call" in out
    assert meetings._CALL.get("mic_proven") == pytest.approx(0.62)


def test_speech_that_arrives_silent_is_reported_not_claimed(live_call, monkeypatch):
    _mic_reads(monkeypatch, 0.0)
    with pytest.raises(RuntimeError, match="NOT said"):
        asyncio.run(meetings.say_in_call("Hi, this is Asta"))
    assert not meetings._CALL.get("mic_proven")


@pytest.mark.parametrize("heard", [{}, {"error": "TargetClosedError: page closed"}])
def test_a_check_that_could_not_measure_does_not_dial(heard, monkeypatch):
    """No proof, no ring. The old gate only refused a measured silence."""
    from app import teams_bridge

    class Page:
        async def wait_for_selector(self, *a, **k):
            return True

    class Ctx:
        closed = False

        async def close(self):
            self.closed = True

    class Pw:
        async def stop(self):
            pass

    ctx = Ctx()

    async def launch(headless=True):
        return Pw(), ctx

    async def open_teams(c, timeout=75.0):
        return Page()

    async def hears(page):
        return heard

    rang = []

    async def click(page, selectors, timeout=3000):
        rang.append(True)
        return True

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "close_pool", nothing)
    monkeypatch.setattr(teams_bridge, "_launch", launch)
    monkeypatch.setattr(teams_bridge, "_open_teams", open_teams)
    monkeypatch.setattr(meetings, "_wait_for_chat_list", nothing)
    monkeypatch.setattr(meetings, "_click_first", click)
    monkeypatch.setattr(meetings, "warm_the_voice", lambda: None)
    monkeypatch.setattr(call_audio, "set_call_mic", nothing)
    monkeypatch.setattr(voice, "browser_hears_us", hears)
    meetings._CALL.clear()
    with pytest.raises(RuntimeError, match="Nobody was rung"):
        asyncio.run(meetings.call_person("A colleague"))
    assert not rang, "a call was placed with no proof Asta could be heard"
    assert ctx.closed and not meetings._CALL
