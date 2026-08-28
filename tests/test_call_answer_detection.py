"""Noticing that somebody picked up.

Written after the worst outcome this code can produce: Asta rang Vinish, Vinish
answered and started asking questions, and Asta said nothing at all for forty
seconds and then hung up. The call was connected the whole time; `call_state`
reported "unknown", because every piece of positive evidence it had was broken.

  `_CONNECTED`  shipped with a comment admitting it was "UNVERIFIED against a
                live connected call — nobody was rung to find out". Rung now: it
                does not match.
  `_TIMER_JS`   matched some other m:ss on screen during one call (reporting
                "connected" before the phone had even rung) and nothing at all
                during the next. Wrong in both directions.
  captions      the most trusted signal, and it depends on captions starting —
                which was itself broken by a selector-ordering bug.

So the answer uses the TRANSITION instead: it was ringing, it is no longer
ringing, and it has not ended. That is what being answered looks like from the
outside, and there is no element for Teams to rename.
"""

from __future__ import annotations

import pytest

from app import meetings


def _states(seq):
    """A call_state that walks a scripted sequence, holding on the last value."""
    box = {"i": 0}

    async def state(_page):
        i = min(box["i"], len(seq) - 1)
        box["i"] += 1
        return seq[i]

    return state


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(meetings, "RING_SECONDS", 6.0)


@pytest.mark.asyncio
async def test_ringing_then_quiet_means_they_answered(monkeypatch):
    monkeypatch.setattr(meetings, "call_state",
                        _states(["ringing", "ringing", "unknown"]))
    assert await meetings.wait_for_answer(object()) == "connected"


@pytest.mark.asyncio
async def test_positive_evidence_is_still_honoured_when_it_works(monkeypatch):
    monkeypatch.setattr(meetings, "call_state", _states(["ringing", "connected"]))
    assert await meetings.wait_for_answer(object()) == "connected"


@pytest.mark.asyncio
async def test_ringing_all_the_way_to_the_deadline_is_no_answer(monkeypatch):
    monkeypatch.setattr(meetings, "call_state", _states(["ringing"]))
    assert await meetings.wait_for_answer(object()) == "no answer"


@pytest.mark.asyncio
async def test_declined_is_ended_not_answered(monkeypatch):
    """Pressing decline stops the ringing too — the difference is that the call
    screen says so, and `ended` is checked before the transition."""
    monkeypatch.setattr(meetings, "call_state", _states(["ringing", "ended"]))
    assert await meetings.wait_for_answer(object()) == "ended"


@pytest.mark.asyncio
async def test_never_ringing_does_not_invent_an_answer(monkeypatch):
    """The guard on the transition. If `_RINGING` stops matching entirely — a
    reworded call screen, another locale — an unreadable call must stay unknown
    rather than being declared connected and talked into."""
    monkeypatch.setattr(meetings, "call_state", _states(["unknown"]))
    assert await meetings.wait_for_answer(object()) == "unknown"
