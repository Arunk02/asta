"""Somebody is calling him right now.

    "tell their name who calling it is one to one or group call like that if i
     said accept the call and talk for incoming calls"

Everything Asta had was outbound. `call_state` reads the screen of a call ASTA
placed, out of `_CALL`, and there is no `_CALL` when the phone simply rings — so
an incoming call was invisible until it appeared in the Activity feed afterwards
as "Missed call from Vinish Kumar", by which point the only honest thing to say is
that it was missed.
"""

from __future__ import annotations

import asyncio

import pytest

from app import incoming, store

ONE_TO_ONE = "Vinish Kumar is calling you\nAccept  Decline"
GROUP = "Incoming call from Komal, Vinish +3"
ORDINARY = "Chat  Teams  Calendar\nVinish Kumar\nOkay I will check"


@pytest.fixture(autouse=True)
def _fresh():
    incoming.clear()


# --- noticing the ring --------------------------------------------------------

def test_a_ringing_call_is_noticed():
    assert incoming.looks_incoming(ONE_TO_ONE)
    assert incoming.looks_incoming(GROUP)


def test_an_ordinary_teams_screen_is_not_a_call():
    """A false positive offers to answer a phone that is not ringing, and the
    watcher polls every eight seconds."""
    assert not incoming.looks_incoming(ORDINARY)


def test_it_says_who_is_calling():
    """He asked for the name first: "tell their name who calling"."""
    assert incoming.who_is_calling(ONE_TO_ONE) == "Vinish Kumar"
    assert "Komal" in incoming.who_is_calling(GROUP)


def test_it_says_whether_it_is_one_to_one_or_group():
    """The second thing he asked for, and the two want different answers."""
    assert not incoming.is_group(incoming.who_is_calling(ONE_TO_ONE), ONE_TO_ONE)
    assert incoming.is_group(incoming.who_is_calling(GROUP), GROUP)


def test_the_line_he_reads_names_both():
    line = incoming.describe({"who": "Vinish Kumar", "group": False})
    assert "Vinish Kumar" in line and "1:1" in line
    assert "group" in incoming.describe({"who": "A, B", "group": True})


def test_detection_does_not_rest_on_a_toast_selector():
    """Matched on visible text, like `meetings._RINGING`, and for the same reason:
    a data-tid guess looks right in review and fails silently on the one call that
    mattered. The selectors are only used to CLICK something already identified."""
    import inspect
    src = inspect.getsource(incoming.looks_incoming) + inspect.getsource(incoming.look)
    assert "data-tid" not in src


# --- one offer per ring -------------------------------------------------------

def test_the_same_ring_is_only_offered_once():
    """Polling every eight seconds through a thirty-second ring must not ask him
    four times."""
    call = {"who": "Vinish Kumar", "group": False}
    assert not incoming.already_offered(call)
    incoming.note_offered(call)
    assert incoming.already_offered(call)


def test_a_later_call_from_the_same_person_is_a_new_call():
    call = {"who": "Vinish Kumar", "group": False}
    incoming.note_offered(call)
    incoming.clear()                      # the ring stopped
    assert not incoming.already_offered(call)


# --- answering ----------------------------------------------------------------

class _Page:
    def __init__(self, clicks=True):
        self.clicks = clicks
        self.clicked = []


@pytest.fixture
def clickable(monkeypatch):
    from app import meetings
    page = _Page()

    async def _click(p, selectors, timeout=3000):
        page.clicked.append(selectors[0])
        return page.clicks

    async def _captions(p):
        return True

    monkeypatch.setattr(meetings, "_click_first", _click)
    monkeypatch.setattr(meetings, "start_captions", _captions)
    monkeypatch.setattr(meetings, "_CALL", {})
    return page


def test_answering_silently_does_not_arm_speech(clickable, monkeypatch):
    from app import meetings
    out = asyncio.run(incoming.answer(clickable, speak=False))
    assert "listening only" in out
    assert meetings._CALL["speaks"] is False


def test_answering_to_talk_arms_speech(clickable, monkeypatch):
    from app import meetings, voice
    monkeypatch.setattr(voice, "can_speak", lambda: True)
    monkeypatch.setattr(voice, "ensure_unmuted", lambda p: _done(True))
    out = asyncio.run(incoming.answer(clickable, speak=True))
    assert "talking" in out
    assert meetings._CALL["speaks"] is True


def _done(v):
    async def _c():
        return v
    return _c()


def test_it_refuses_to_talk_with_no_virtual_microphone(clickable, monkeypatch):
    """Picking up unmuted without BlackHole would broadcast his real microphone to
    whoever called. Refusing is the only safe answer."""
    from app import voice
    monkeypatch.setattr(voice, "can_speak", lambda: False)
    out = asyncio.run(incoming.answer(clickable, speak=True))
    assert "Didn't answer" in out
    assert not clickable.clicked, "clicked Accept anyway"


def test_a_missing_accept_button_is_reported_not_assumed(clickable):
    """Telling him "answered" when nothing was answered leaves a colleague talking
    to a phone that never picked up."""
    clickable.clicks = False
    out = asyncio.run(incoming.answer(clickable, speak=False))
    assert "not answered" in out


# --- the op his yes runs ------------------------------------------------------

def test_a_ring_that_stopped_is_not_answered_anyway(monkeypatch):
    """His yes can arrive after the ring ended. Clicking Accept on a toast that is
    gone either does nothing or hits whatever replaced it."""
    from app import ops, teams_bridge

    class _Ctx:
        async def __aenter__(self): return _Page()
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(teams_bridge, "teams_page", lambda: _Ctx())
    monkeypatch.setattr(incoming, "look", lambda page: _done(None))
    out = asyncio.run(ops.run({"name": "call_answer", "args": {"speak": False}}))
    assert "stopped ringing" in out


def test_answering_is_a_recorded_op_not_a_brain_instruction():
    """The words he approved are the ones that run. A brain re-reading "answer the
    call" could answer a different one, or decide the tool call was optional."""
    from app import ops
    assert "call_answer" in ops.REGISTRY
    assert "listen only" in ops.REGISTRY["call_answer"]["describe"]({})
    assert "and talk" in ops.REGISTRY["call_answer"]["describe"]({"speak": True})


def test_nothing_rings_by_default(monkeypatch):
    monkeypatch.delenv("ASTA_INCOMING", raising=False)
    assert not incoming.enabled()


def test_a_call_already_in_progress_is_not_ours_to_answer(monkeypatch):
    from app import meetings
    monkeypatch.setattr(meetings, "_CALL", {"page": object()})
    assert incoming.meetings_busy()


# --- can Asta actually be heard? ----------------------------------------------
# The check that was missing when five calls were placed and every one transmitted
# silence. `say_in_call` measured audio PLAYED and reported success; the far end
# heard nothing, because macOS denies the microphone by handing the app a valid,
# correctly-labelled track full of zeroes.

def test_the_check_measures_what_ARRIVES_not_what_was_played():
    """The distinction the whole failure turned on."""
    import inspect
    from app import voice
    src = inspect.getsource(voice.self_test)
    assert "browser_mic_delivers" in src
    assert "play_to_device" in src


def test_it_listens_before_it_plays():
    """Starting playback first measures nothing — a browser that is not yet
    listening cannot report a level. That cost a whole cycle the first time."""
    import inspect
    from app import voice
    src = inspect.getsource(voice.self_test)
    assert src.index("browser_mic_delivers") < src.index("play_to_device")


def test_his_microphone_is_restored_whatever_happens():
    """Twice in one day a test left his system input on BlackHole and his own Teams
    calls had no working microphone."""
    import inspect
    from app import voice
    src = inspect.getsource(voice.self_test)
    assert "finally:" in src
    tail = src[src.index("finally:"):]
    assert "set_call_mic(device=was)" in tail


def test_with_no_virtual_microphone_it_says_so_rather_than_testing_nothing(monkeypatch):
    import asyncio as _a

    from app import voice
    monkeypatch.setattr(voice, "CALL_DEVICE", "")
    out = _a.run(voice.self_test())
    assert "no virtual microphone" in out["error"]
    assert out["restored"] is False        # nothing was changed, nothing to restore


def test_silence_is_reported_as_silence_not_as_success(monkeypatch):
    """A peak of zero with a perfect device label is exactly what a denied
    microphone looks like, and it must never read as working."""
    import asyncio as _a

    from app import agent, voice
    monkeypatch.setattr(voice, "self_test",
                        lambda: _done({"peak": 0.0, "label": "BlackHole 2ch",
                                       "heard": False, "device": "BlackHole 2ch"}))
    out = _a.run(agent.voice_check())
    assert "SILENT" in out
    assert "not be transmitted" in out


# --- the selectors are a guess, so do not depend on them ----------------------
# Nobody has rung this laptop, so the data-tid list for Accept was written without
# anything to check it against. That is exactly how the mute button, the captions
# menu and the microphone all went wrong this week.

def test_accept_falls_back_to_what_the_button_says(clickable, monkeypatch):
    """When the data-tid guesses miss, find the control by its label — the same
    approach `meetings._RINGING` uses, and for the same reason."""
    from app import meetings, voice

    async def _never(p, selectors, timeout=3000):
        return False

    monkeypatch.setattr(meetings, "_click_first", _never)
    monkeypatch.setattr(voice, "can_speak", lambda: True)

    class _P(_Page):
        async def evaluate(self, js, *a):
            self.clicked.append("by-text")
            return "Accept"

        async def click(self, sel, timeout=0):
            self.clicked.append(sel)

    page = _P()
    out = asyncio.run(incoming.answer(page, speak=False))
    assert "Answered" in out
    assert '[data-asta-accept="1"]' in page.clicked


def test_the_text_search_never_picks_decline_or_video():
    """Answering a call by clicking Decline, or picking up on VIDEO when he asked
    for audio, are both unrecoverable in front of a colleague."""
    assert "decline|reject|ignore|video" in incoming._ACCEPT_BY_TEXT


def test_the_first_real_ring_records_what_teams_rendered():
    """Rather than leaving the guess as a permanent unknown, the first genuine call
    writes the actual markup so the selectors can be replaced with the truth."""
    import inspect
    assert "incoming-toast.html" in inspect.getsource(incoming.capture_toast)
    assert "if out.exists():" in inspect.getsource(incoming.capture_toast)
