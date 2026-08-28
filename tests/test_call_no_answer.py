"""A call nobody answers must stop ringing.

Arun watched one do it and said so: "if persons not takes and it reaches till the
end, it is not cutting the call."

He was right, and the number is worse than it sounds. `watch()` exited on exactly
two conditions — the call ENDED, or it OVERRAN — and a ringing call is neither.
`MAX_CALL_MINUTES` is 90. So a colleague who happened to be away from their desk
had their phone rung by Asta for an hour and a half.

`wait_for_answer` already knew how to detect this. Nothing was asking it.
"""

from __future__ import annotations

import pytest

from app import meetings


class _Page:
    """A call screen that reports whatever state the test wants, in sequence."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def next_state(self):
        self.calls += 1
        return self.states[min(self.calls - 1, len(self.states) - 1)]


@pytest.fixture
def wired(monkeypatch):
    """A placed-but-unanswered call, with leave() recorded rather than performed."""
    left: list[str] = []

    async def fake_leave():
        meetings._CALL.clear()
        left.append("left")
        return "left the call"

    monkeypatch.setattr(meetings, "leave", fake_leave)
    monkeypatch.setattr(meetings, "RING_SECONDS", 0.2)
    return left


@pytest.mark.asyncio
async def test_a_call_nobody_answers_is_hung_up(monkeypatch, wired):
    page = _Page(["ringing"])

    async def state(_page):
        return page.next_state()

    monkeypatch.setattr(meetings, "call_state", state)
    # answered_at = 0.0 is what `call_person` sets: placed, not yet picked up.
    meetings._CALL.update(page=page, answered_at=0.0, captions=[], who="Someone")
    why = await meetings.watch()
    assert "no answer" in why
    assert wired == ["left"], "it kept ringing"


@pytest.mark.asyncio
async def test_an_answered_call_is_not_hung_up(monkeypatch, wired):
    """The direction that matters more. Dropping a live call because the screen
    was briefly unreadable is far worse than staying on a dead one."""
    page = _Page(["connected", "connected"])

    async def state(_page):
        return page.next_state()

    async def ended(_page):
        return True          # end it immediately so the loop terminates

    monkeypatch.setattr(meetings, "call_state", state)
    monkeypatch.setattr(meetings, "call_ended", ended)
    meetings._CALL.update(page=page, answered_at=0.0, captions=[], who="Someone")
    why = await meetings.watch()
    assert "no answer" not in why


@pytest.mark.asyncio
async def test_an_unreadable_call_screen_is_left_alone(monkeypatch, wired):
    """'unknown' is not 'no answer'. A call that cannot be read might be live,
    and hanging up on a live call in front of somebody cannot be undone."""
    page = _Page(["unknown"])

    async def state(_page):
        return page.next_state()

    async def ended(_page):
        return True

    monkeypatch.setattr(meetings, "call_state", state)
    monkeypatch.setattr(meetings, "call_ended", ended)
    meetings._CALL.update(page=page, answered_at=0.0, captions=[], who="Someone")
    why = await meetings.watch()
    assert "no answer" not in why


@pytest.mark.asyncio
async def test_a_call_already_answered_skips_the_ring_check(monkeypatch, wired):
    """A joined meeting sets answered_at, so the ring wait must not run at all —
    it would burn RING_SECONDS before every meeting Asta sits in on."""
    asked = {"n": 0}

    async def state(_page):
        asked["n"] += 1
        return "connected"

    async def ended(_page):
        return True

    monkeypatch.setattr(meetings, "call_state", state)
    monkeypatch.setattr(meetings, "call_ended", ended)
    meetings._CALL.update(page=_Page(["connected"]), answered_at=123.0,
                          captions=[], who="Someone")
    await meetings.watch()
    assert asked["n"] == 0, "it waited for an answer on a call already answered"
