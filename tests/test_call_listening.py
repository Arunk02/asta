"""Borrowing the microphone, and noticing what the other person asked.

Two behaviours that only ever go wrong in front of somebody else, which is what
makes them worth testing hard:

  - Teams listens to ONE input. Asta has to point it at the virtual mic to be
    heard and point it back afterwards. Forget the second half and Arun is
    silently muted for the rest of the call — he finds out when someone asks why
    he went quiet.
  - While listening, Asta may offer to look something up. That offer goes to his
    phone. If it ever came out of his mouth in the call instead, he would be
    hearing his own assistant negotiate with him in front of a colleague.
"""

from __future__ import annotations

import io
import wave

import pytest

from app import meetings, store, voice


def _wav(seconds: float = 0.1, rate: int = 24000) -> bytes:
    import math
    frames = bytearray()
    for i in range(int(rate * seconds)):
        frames += int(9000 * math.sin(2 * math.pi * 220 * i / rate)).to_bytes(
            2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class FakePage:
    """Stands in for the Teams tab. Records nothing but what matters."""


@pytest.fixture
def in_call(monkeypatch):
    store.kv_set("teams_in_call", "1")
    monkeypatch.setattr(meetings, "can_speak", lambda: True)
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "BlackHole 2ch")
    monkeypatch.setattr(meetings, "HIS_MIC", "MacBook Pro Microphone")
    meetings._CALL["page"] = FakePage()
    yield
    store.kv_set("teams_in_call", "")
    meetings._CALL.clear()
    meetings.clear_noticed()


@pytest.fixture
def mic(monkeypatch):
    """Track every device the mic was pointed at, in order."""
    switches: list[str] = []

    async def set_mic(page, device):
        switches.append(device)
        return True

    monkeypatch.setattr(meetings, "set_call_mic", set_mic)
    return switches


@pytest.fixture
def speaks(monkeypatch):
    async def gen(text, profile="", engine="", voice=""):
        return _wav()
    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": 0.1)


# --- borrowing the mic --------------------------------------------------------

@pytest.mark.asyncio
async def test_the_mic_is_borrowed_and_given_back(in_call, mic, speaks):
    await meetings.say_in_call("tell him the build passed")
    assert mic == ["BlackHole 2ch", "MacBook Pro Microphone"]


@pytest.mark.asyncio
async def test_the_mic_is_given_back_even_when_speaking_fails(in_call, mic, monkeypatch):
    """THE test. An exception mid-utterance must not leave him muted."""
    async def gen(text, profile="", engine="", voice=""):
        return _wav()

    def boom(wav, device=""):
        raise RuntimeError("device disappeared")

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", boom)

    with pytest.raises(RuntimeError):
        await meetings.say_in_call("tell him")

    assert mic[-1] == "MacBook Pro Microphone", "he was left on the virtual mic"


@pytest.mark.asyncio
async def test_it_does_not_speak_if_the_mic_will_not_switch(in_call, monkeypatch):
    """Speaking into a mic Teams is not listening to is the original bug again."""
    played = []

    async def refuse(page, device):
        return False

    async def gen(text, profile="", engine="", voice=""):
        return _wav()

    monkeypatch.setattr(meetings, "set_call_mic", refuse)
    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": played.append(1))

    with pytest.raises(RuntimeError, match="could not point Teams"):
        await meetings.say_in_call("tell him")
    assert played == [], "it played into a mic nobody was listening to"


@pytest.mark.asyncio
async def test_a_failed_restore_shouts(in_call, monkeypatch):
    """Being silently muted is the worst outcome, so it must never be silent."""
    calls = []

    async def switch(page, device):
        calls.append(device)
        return device == "BlackHole 2ch"      # borrow works, restore fails

    told = {}

    async def spy(text, level="info", urgency="direct", priority=None):
        told["text"] = text

    async def gen(text, profile="", engine="", voice=""):
        return _wav()

    from app import notify
    monkeypatch.setattr(meetings, "set_call_mic", switch)
    monkeypatch.setattr(notify, "notify", spy)
    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": 0.1)

    await meetings.say_in_call("tell him")
    assert "may be muted" in told.get("text", "")


@pytest.mark.asyncio
async def test_audio_is_generated_before_the_mic_is_borrowed(in_call, monkeypatch):
    """Chatterbox takes ~11s. Holding his mic for that would mute him mid-sentence."""
    order = []

    async def gen(text, profile="", engine="", voice=""):
        order.append("generate")
        return _wav()

    async def switch(page, device):
        order.append(f"mic:{device}")
        return True

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(meetings, "set_call_mic", switch)
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": 0.1)

    await meetings.say_in_call("tell him")
    assert order[0] == "generate", f"mic borrowed before synthesis: {order}"


# --- what counts as worth noticing -------------------------------------------

@pytest.mark.parametrize("line", [
    "how does the ATA fallback pick the transport order",
    "where is the vessel schedule sync handled",
    "what does TmsServiceImpl do with cancelled bookings",
    "which topic carries the activity plan events",
    "why does the amend flow skip the TO reset",
])
def test_code_questions_are_offered(line):
    assert meetings.classify_line(line) == "answerable"


@pytest.mark.parametrize("line", [
    "can you review the PR today",
    "shall we merge it now",
    "when can you deploy it",
    "are you ok with that approach",
    "what do you think about the mapper change",
    "could you join the call at four",
])
def test_questions_about_him_are_never_auto_answered(line):
    assert meetings.classify_line(line) == "his"


@pytest.mark.parametrize("line", [
    "can you check what does TmsServiceImpl do here",
    "shall we merge it, how does the amend flow handle that",
    "are you ok with how does the resolver pick the repo",
])
def test_a_request_of_him_that_also_mentions_code_is_still_his(line):
    """A sentence matching BOTH patterns is HIS — it is a request of him that
    happens to mention code, and answering it commits him to having looked at
    something he never looked at.

    These three are chosen because they genuinely match both regexes. An earlier
    version of this test used "can you check how the ATA fallback works", which
    matches only the his-pattern — so it passed no matter which way precedence
    ran, and proved nothing. Verified by flipping the precedence in the source:
    every line here flips to "answerable".
    """
    assert meetings._ANSWERABLE.search(line), "fixture no longer matches both"
    assert meetings._HIS_TO_ANSWER.search(line), "fixture no longer matches both"
    assert meetings.classify_line(line) == "his"


@pytest.mark.parametrize("line", ["yeah", "ok", "mm hmm", "right", "sure bro"])
def test_small_talk_is_ignored(line):
    assert meetings.classify_line(line) == "chatter"


# --- asking him, once ---------------------------------------------------------

def test_the_same_question_is_only_raised_once():
    """Captions repeat the same line as they settle — he must not be asked twice."""
    line = "how does the ATA fallback pick the transport order"
    meetings.clear_noticed()
    assert len(meetings.notice_asks([line])) == 1
    assert meetings.notice_asks([line]) == []
    assert meetings.notice_asks([line + "?"]) == [], "punctuation defeated the dedupe"


def test_his_own_words_are_not_treated_as_asks():
    """Asta offering to look up a question Arun just asked out loud is noise."""
    meetings.clear_noticed()
    line = "how does the ATA fallback work"
    assert meetings.notice_asks([line], speaker_is_him=True) == []


def test_a_new_call_starts_with_a_clean_slate():
    line = "where is the vessel schedule handled"
    meetings.clear_noticed()
    meetings.notice_asks([line])
    meetings.clear_noticed()
    assert len(meetings.notice_asks([line])) == 1


def test_things_aimed_at_him_are_collected_for_afterwards():
    lines = ["can you review the PR", "how does the mapper work", "yeah"]
    assert meetings.pending_for_him(lines) == ["can you review the PR"]


@pytest.mark.asyncio
async def test_the_offer_goes_to_him_and_is_never_spoken(monkeypatch):
    """If this ever reached say_in_call, he would hear his assistant
    negotiating with him in front of Vinish."""
    spoke = []
    told = {}

    async def spy(text, level="info", urgency="direct", priority=None):
        told["text"] = text

    async def must_not_speak(*a, **k):
        spoke.append(1)

    from app import notify
    monkeypatch.setattr(notify, "notify", spy)
    monkeypatch.setattr(meetings, "say_in_call", must_not_speak)

    await meetings.offer_to_analyse(
        {"kind": "answerable", "line": "how does the ATA fallback work", "key": "k"})

    assert spoke == [], "the offer was spoken into the call"
    assert "Want me to look this up" in told["text"]


@pytest.mark.asyncio
async def test_no_offer_is_made_for_something_he_must_answer(monkeypatch):
    told = {}

    async def spy(text, level="info", urgency="direct", priority=None):
        told["text"] = text

    from app import notify
    monkeypatch.setattr(notify, "notify", spy)

    await meetings.offer_to_analyse(
        {"kind": "his", "line": "can you review the PR", "key": "k"})
    assert told == {}, "it offered to answer something only Arun can answer"
