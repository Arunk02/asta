"""A call nobody answers should leave something behind.

Ringing a colleague and hanging up in silence makes the call pointless: they see
a missed call from Arun and have no idea what it was about. "if they not picked
up send voice note and cut the call".

Teams has no API for this — a voice message is recorded from the SYSTEM
microphone, which is the same trick `say_in_call` already uses, pointed at the
composer instead of at a call. That makes it blind UI-driving against a live
composer, and the thing it gets wrong lands in a colleague's chat rather than in
a log. So: every failure is loud and total, never a partial send.
"""

from __future__ import annotations

import asyncio

import pytest

from app import call_audio, meetings, teams_bridge


class _Page:
    """A composer that has whichever controls the test says it has."""

    def __init__(self, has=("mic", "stop", "send")):
        self.has, self.clicked, self.keys = has, [], []
        self.keyboard = self

    async def wait_for_selector(self, sel, timeout=0):
        for name, group in (("mic", teams_bridge._MIC_BUTTON),
                            ("stop", teams_bridge._MIC_STOP),
                            ("send", teams_bridge._MIC_SEND)):
            if sel in group:
                if name not in self.has:
                    raise RuntimeError("no such control")
                return _Btn(name, self.clicked)
        raise RuntimeError("no such control")

    async def press(self, key):
        self.keys.append(key)


class _Btn:
    def __init__(self, name, log): self.name, self.log = name, log
    async def click(self): self.log.append(self.name)


@pytest.fixture
def wired(monkeypatch):
    """Speech, mic and the chat all stubbed; records what happened."""
    seen = {"played": [], "mic": []}
    from app import voice

    async def speak(text, **kw):
        seen["said"] = text
        return b"RIFF....fake wav"
    monkeypatch.setattr(voice, "speak", speak)
    monkeypatch.setattr(voice, "play_to_device",
                        lambda wav, dev="": seen["played"].append(dev) or 1.0)

    async def set_mic(page=None, device: str = ""):
        seen["mic"].append(device)
        return True
    monkeypatch.setattr(call_audio, "set_call_mic", set_mic)

    async def restore(page):
        seen["mic"].append("RESTORED")
    monkeypatch.setattr(call_audio, "_restore_mic", restore)

    async def find(page, chat, allow_group=False):
        return chat
    monkeypatch.setattr(teams_bridge, "_find_chat", find)
    return seen


def _run(page, wired, monkeypatch, who="Alex Kumar", text="hi"):
    import contextlib

    @contextlib.asynccontextmanager
    async def fake_page():
        yield page
    monkeypatch.setattr(teams_bridge, "teams_page", fake_page)
    return asyncio.run(teams_bridge.send_voice_note(who, text))


# --- the happy path ----------------------------------------------------------

def test_it_records_stops_and_sends_in_that_order(wired, monkeypatch):
    page = _Page()
    landed = _run(page, wired, monkeypatch)
    assert page.clicked == ["mic", "stop", "send"]
    assert landed.startswith("Alex Kumar")


def test_the_mic_is_borrowed_and_given_back(wired, monkeypatch):
    """Recording holds the system input. Leaving it on the virtual device would
    mute Arun's own next call — the failure this codebase keeps relearning."""
    _run(_Page(), wired, monkeypatch)
    assert wired["mic"] == [call_audio.AUDIO_DEVICE, "RESTORED"]


def test_the_speech_is_played_into_the_call_device(wired, monkeypatch):
    _run(_Page(), wired, monkeypatch)
    assert wired["played"] == [call_audio.AUDIO_DEVICE]


# --- and every way it can fail -----------------------------------------------

def test_no_record_button_is_a_hard_failure(wired, monkeypatch):
    """A voice note reported as sent and never recorded is worse than none."""
    page = _Page(has=())
    with pytest.raises(RuntimeError, match="no voice-message button"):
        _run(page, wired, monkeypatch)
    assert page.clicked == []


def test_a_mic_it_cannot_borrow_abandons_the_recording(wired, monkeypatch):
    """Otherwise it records silence and sends it."""
    async def refuse(page=None, device: str = ""):
        return False
    monkeypatch.setattr(call_audio, "set_call_mic", refuse)
    page = _Page()
    with pytest.raises(RuntimeError, match="would have been silence"):
        _run(page, wired, monkeypatch)
    assert "Escape" in page.keys, "the open recording must be abandoned"


def test_a_missing_send_control_still_gives_the_mic_back(wired, monkeypatch):
    """The `finally` matters most on the paths that raise."""
    page = _Page(has=("mic", "stop"))
    with pytest.raises(RuntimeError, match="no send control"):
        _run(page, wired, monkeypatch)
    assert "RESTORED" in wired["mic"]


def test_silence_is_never_sent(wired, monkeypatch):
    from app import voice

    async def nothing(text, **kw):
        return b""
    monkeypatch.setattr(voice, "speak", nothing)
    with pytest.raises(RuntimeError, match="synthesis produced nothing"):
        _run(_Page(), wired, monkeypatch)


# --- wired into the unanswered call ------------------------------------------

def test_it_is_off_until_asked_for(monkeypatch):
    """It drives a live composer blind; the default cannot be on."""
    monkeypatch.delenv("ASTA_VOICE_NOTE", raising=False)
    assert meetings.voice_notes_enabled() is False


def test_a_failed_note_never_turns_into_an_error_report(monkeypatch):
    """It is the consolation prize for an unanswered call. Failing to leave one
    must not also break the "they didn't pick up" message."""
    monkeypatch.setenv("ASTA_VOICE_NOTE", "1")

    async def boom(chat, text, allow_group=False):
        raise RuntimeError("composer changed")
    monkeypatch.setattr(teams_bridge, "send_voice_note", boom)

    out = asyncio.run(meetings._leave_voice_note("Alex Kumar", "the 3 PRs"))
    assert "Could not leave a voice note" in out and "composer changed" in out


def test_the_note_says_who_it_is_and_what_it_was_about(monkeypatch):
    monkeypatch.setenv("ASTA_VOICE_NOTE", "1")
    said = {}

    async def capture(chat, text, allow_group=False):
        said["text"], said["to"] = text, chat
        return chat
    monkeypatch.setattr(teams_bridge, "send_voice_note", capture)

    asyncio.run(meetings._leave_voice_note("Alex Kumar", "the 3 hot-priority PRs"))
    assert "Arun's assistant" in said["text"]
    assert "the 3 hot-priority PRs" in said["text"]
    assert said["to"] == "Alex Kumar"


def test_nothing_is_left_when_the_feature_is_off(monkeypatch):
    monkeypatch.setenv("ASTA_VOICE_NOTE", "0")
    assert asyncio.run(meetings._leave_voice_note("Alex Kumar", "x")) == ""


def test_the_call_is_hung_up_before_the_note_is_recorded():
    """Recording holds the system input and a live call is already holding it —
    so the order is not cosmetic."""
    import inspect
    src = inspect.getsource(meetings.call_watch)
    assert src.index("await leave()") < src.index("_leave_voice_note")
