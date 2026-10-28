"""A call or meeting is recorded only as what was observed: ringing is not
answered, the lobby is not the meeting, permission to speak is not speech."""
from __future__ import annotations

import pytest

from app import call_screen, call_states, meetings
from tests.test_call_lifecycle import CONNECTED, RINGING, FakePage, call, pushed, spoke  # noqa: F401

LOBBY = "Weekly sync\nSomeone in the meeting should let you in soon\nLeave"


@pytest.mark.asyncio
@pytest.mark.parametrize("body, marker, state", [
    (LOBBY, False, "lobby"),
    ("Hi there, when the meeting starts we'll let people know you're waiting", False, "lobby"),
    (RINGING, False, "ringing"),
    (CONNECTED, True, "connected"),
])
async def test_the_screen_tells_the_lobby_apart(body, marker, state):
    assert await call_screen.call_state(FakePage(body, connected_marker=marker)) == state


def test_a_repeat_observation_is_not_a_new_transition():
    c = {}
    call_states.mark(c, "ringing", now=1)
    call_states.mark(c, "ringing", now=2)
    call_states.mark(c, "answered", "timer", now=3)
    assert [s["state"] for s in c["states"]] == ["ringing", "answered"]
    with pytest.raises(ValueError):
        call_states.mark(c, "heard")


@pytest.mark.asyncio
async def test_permission_to_speak_is_not_reported_as_having_spoken(call):
    assert call["speaks"] is True
    await meetings.leave()
    last = meetings.last_call()
    assert last["spoke"] is False and last["answered"] is False
    assert [s["state"] for s in last["states"]] == ["ended"]


@pytest.mark.asyncio
async def test_a_delivered_line_is_recorded_with_its_evidence(call, spoke):
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    await meetings.say_in_call("the build passed")
    await meetings.leave()
    last = meetings.last_call()
    states = [s["state"] for s in last["states"]]
    assert states == ["answered", "speech_delivered", "ended"]
    assert last["spoke"] is True and "unmuted call" in last["states"][1]["evidence"]


@pytest.mark.asyncio
async def test_a_failed_line_is_not_recorded_as_delivered(call, spoke):
    with pytest.raises(RuntimeError):
        await meetings.say_in_call("the build passed")       # still ringing
    assert not call_states.reached(call, "speech_delivered")


class Screens(FakePage):
    """A page whose text moves through the given screens, one per read."""

    def __init__(self, *bodies):
        super().__init__(bodies[0])
        self.bodies = list(bodies)

    async def evaluate(self, js, *args):
        if "innerText" in js and "document.body" in js:
            return self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return False


def _in_meeting(page, joined_at):
    meetings._CALL.update(page=page, captions=[], answered_at=joined_at, speaks=False,
                          who="", joined_at=joined_at, in_lobby=True)
    call_states.mark(meetings._CALL, "lobby", "Teams lobby text after Join")


@pytest.mark.asyncio
async def test_a_meeting_never_let_in_is_left_and_not_reported_as_attended(monkeypatch):
    monkeypatch.setattr(meetings, "CAPTION_POLL_SECONDS", 0.001)
    _in_meeting(FakePage(LOBBY), meetings._now() - call_states.LOBBY_MINUTES * 60 - 5)
    try:
        why = await meetings.watch(poll_seconds=0.001)
    finally:
        meetings._CALL.clear()
    assert "never admitted from the lobby" in why
    last = meetings.last_call()
    assert last["admitted"] is False
    assert [s["state"] for s in last["states"]] == ["lobby", "ended"]


@pytest.mark.asyncio
async def test_being_let_in_is_recorded_as_joining(monkeypatch):
    monkeypatch.setattr(meetings, "CAPTION_POLL_SECONDS", 0.001)

    async def no_captions(page, lines):
        return None

    monkeypatch.setattr(meetings, "poll_captions", no_captions)
    # lobby → in the meeting → the meeting ends
    page = Screens(LOBBY, LOBBY, "Weekly sync\n12:04", "Weekly sync\n12:04",
                   "Weekly sync\n12:05", "Meeting ended")
    _in_meeting(page, meetings._now())
    try:
        why = await meetings.watch(poll_seconds=0.001)
    finally:
        meetings._CALL.clear()
    assert why == "the call ended"
    last = meetings.last_call()
    assert last["admitted"] is True
    states = [s["state"] for s in last["states"]]
    assert states[:2] == ["lobby", "joined"] and states[-1] == "ended"
    assert last["states"][1]["evidence"] == "admitted from the lobby"
