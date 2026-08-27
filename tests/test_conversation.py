"""Holding a real two-way call — the four ways it went wrong in front of Vinish.

Each test here is a thing he reported, in his words or Arun's:

    "it called vinish , nothing was spoken"
    "he not able to hear u , what he speaking i can able to hear"
    "he keep on asking questions, nothing was spoken"
    "if persons not takes and it reaches till the end , it is not cutting the call"

The loop is cheap to get subtly wrong and expensive to test live — every attempt
costs a colleague's afternoon — so the rules are pinned here instead.
"""

from __future__ import annotations

import asyncio

import pytest

from app import conversation


class _Meetings:
    """A call that never touches a browser."""

    def __init__(self, answer="answered", heard=None, speak_raises=False):
        self._answer = answer
        self._heard = list(heard or [])
        self._speak_raises = speak_raises
        self.said: list[str] = []
        self.left = False
        self._CALL = {"page": object()}

    async def call_person(self, who, video=False):
        return f"{who} Kumar"

    async def wait_for_answer(self, page, seconds=0):
        return self._answer

    async def start_captions(self, page):
        return True

    async def poll_captions(self, page, lines):
        if self._heard:
            lines.append({"speaker": "Vinish Kumar", "text": self._heard.pop(0)})

    def speaker_is_arun(self, speaker):
        return "arun" in (speaker or "").lower()

    def transcript_text(self, lines):
        return "\n".join(f"{ln['speaker']}: {ln['text']}" for ln in lines)

    async def say_in_call(self, text, voice_name=""):
        if self._speak_raises:
            raise RuntimeError("no virtual microphone configured")
        self.said.append(text)
        return "said"

    async def leave(self):
        self.left = True
        return "hung up"


@pytest.fixture
def fake(monkeypatch):
    """Install a stub `meetings` and make the loop run instantly."""
    made = {}

    def _install(**kw):
        m = _Meetings(**kw)
        # Substituting the module ATTRIBUTE, not the sys.modules entry: `from .
        # import meetings` resolves the package attribute, so a setitem stub does
        # not take and the real call path runs. That mistake switched Arun's system
        # input to BlackHole and opened a browser before this test timed out.
        monkeypatch.setattr(conversation, "meetings", m)
        monkeypatch.setattr(conversation, "HEAR_SECONDS", 0.05)
        monkeypatch.setattr(conversation, "CONVERSE_SECONDS", 5)
        made["m"] = m
        return m

    yield _install


@pytest.fixture(autouse=True)
def _instant_brain(monkeypatch):
    monkeypatch.setattr(conversation, "answer_from_knowledge",
                        lambda *a, **k: _done("that's a fair point, I'll note it"))
    monkeypatch.setattr(conversation, "spoken_form", lambda t: t)


def _done(value):
    async def _c():
        return value
    return _c()


def test_nobody_answered_means_nothing_is_spoken(fake):
    """"if persons not takes and it reaches till the end, it is not cutting the
    call" — and worse, it used to talk to the voicemail."""
    m = fake(answer="no answer")
    out = asyncio.run(conversation.converse("Vinish", "the 1409 review"))
    assert m.said == [], "spoke into a call nobody answered"
    assert "no answer" in out


def test_an_unknown_state_is_not_treated_as_no_answer(fake):
    """The bug that cut Vinish off mid-sentence: "unknown" means the detector could
    not tell, not that the line is dead. Hanging up on it is the destructive read."""
    m = fake(answer="unknown", heard=["yeah I am here, what is it"])
    asyncio.run(conversation.converse("Vinish", "the 1409 review"))
    assert m.said, "hung up on a live call because the state was unreadable"


def test_it_opens_the_conversation_itself(fake):
    m = fake(answer="answered", heard=["sure, go ahead"])
    asyncio.run(conversation.converse("Vinish", "the 1409 review comments"))
    assert m.said
    assert "Arun" in m.said[0]
    assert "the 1409 review comments" in m.said[0]


def test_it_answers_what_they_actually_said(fake, monkeypatch):
    """Not a script. "he keep on asking questions, nothing was spoken" was a loop
    that talked past him."""
    seen = {}

    async def _brain(prompt, ws=""):
        seen["prompt"] = prompt
        return "understood, I'll pass that to Arun"

    m = fake(answer="answered", heard=["the ETA check returns true even when nothing applied"])
    monkeypatch.setattr(conversation, "answer_from_knowledge", _brain)
    asyncio.run(conversation.converse("Vinish", "PR 1409"))
    assert "returns true even when nothing applied" in seen["prompt"]
    assert "understood, I'll pass that to Arun" in m.said


def test_silence_from_the_brain_is_still_spoken_aloud(fake, monkeypatch):
    """A colleague talking to forty seconds of nothing is the failure Vinish hit.
    If the brain dies, SAY so — do not just hold the line."""
    async def _boom(prompt, ws=""):
        raise RuntimeError("brain down")

    m = fake(answer="answered", heard=["so what do you want to do about it"])
    monkeypatch.setattr(conversation, "answer_from_knowledge", _boom)
    asyncio.run(conversation.converse("Vinish", "PR 1409"))
    assert any("check with Arun" in s for s in m.said), m.said


def test_the_call_is_always_hung_up(fake, monkeypatch):
    """A call left open holds the microphone and leaves `_CALL` set, and every later
    call is refused as "already in a call" until the server restarts."""
    async def _boom(prompt, ws=""):
        raise KeyboardInterrupt

    m = fake(answer="answered", heard=["hello"])
    monkeypatch.setattr(conversation, "answer_from_knowledge", _boom)
    with pytest.raises(BaseException):
        asyncio.run(conversation.converse("Vinish", "PR 1409"))
    assert m.left, "the call was left open"


def test_a_mute_call_is_reported_as_a_call_not_a_discussion(fake):
    """"i donr see he able to hear u , even i cant hear u , so whole convo was mute."
    Reporting that as a successful discussion is the lie that cost four attempts."""
    m = fake(answer="answered", heard=[])
    out = asyncio.run(conversation.converse("Vinish", "PR 1409"))
    assert "captured nothing back" in out
    assert "not a discussion" in out


def test_it_never_commits_arun_to_anything(fake, monkeypatch):
    """The instruction that keeps a spoken answer safe: an unknown becomes "I'll
    check with Arun", never a decision made on his behalf."""
    seen = {}

    async def _brain(prompt, ws=""):
        seen["prompt"] = prompt
        return "ok"

    fake(answer="answered", heard=["can arun get this merged today"])
    monkeypatch.setattr(conversation, "answer_from_knowledge", _brain)
    asyncio.run(conversation.converse("Vinish", "PR 1409"))
    assert "never invent a fact about Arun's intentions" in seen["prompt"].lower() \
        or "Never invent a fact about Arun's intentions" in seen["prompt"]


def test_a_call_that_never_connected_says_so(fake, monkeypatch):
    m = fake()

    async def _refuse(who, video=False):
        raise RuntimeError("already in a call — leave that one first")

    m.call_person = _refuse
    out = asyncio.run(conversation.converse("Vinish", "PR 1409"))
    assert "Nothing rang" in out
    assert "already in a call" in out


# --- the guard that should have existed before any of this --------------------

def test_the_suite_cannot_open_a_browser():
    """The tripwire has to be armed, not just written. Without it a stub that
    silently fails to take runs the real call path — which is how Arun's system
    input ended up on BlackHole twice in one day."""
    import asyncio as _a

    from app import teams_bridge
    with pytest.raises(AssertionError, match="opens a browser"):
        _a.run(teams_bridge._launch())


def test_the_suite_cannot_move_his_microphone():
    from app import meetings
    assert not __import__("os").path.exists(meetings.SWITCH_AUDIO), \
        "tests can still reach the real audio switcher"


def test_the_sandbox_names_the_hardware_calls_too():
    """`app.sandbox` seals the bench. It listed the calls that reach a PERSON but
    not the one that reaches his hardware, which is just as unrecoverable."""
    from app import sandbox
    covered = sandbox.covers()
    assert "meetings.set_call_mic" in covered
    assert "voice.play_to_device" in covered
