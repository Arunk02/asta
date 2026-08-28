""""Meeting starting — want me to join?"

    "notify me meeting starting and someone trying to pull me, u want me to join
     and take forward , if yes go ahead and do, no simple ignore let the call
     automatically end"

The prep ping already existed and fires 15-30 minutes out, which is the wrong
moment to ask this: he cannot answer it yet, and by the time he can the offer is
stale. This is a second, later ping whose only question is whether Asta should go.

Staged rather than asked in prose, so his "yes" runs the recorded join. The
calendar can move between the ping and the answer, and joining the wrong meeting
puts him in a room in front of people who watch him arrive.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import briefing, offers, ops, store

NOW = dt.datetime(2026, 8, 28, 17, 0)


def _ev(title="AI Ideathon", start="5:00 PM", join="https://teams.microsoft.com/l/meetup-join/x"):
    return {"title": title, "start": start, "minutes": 1020, "ends": 1080,
            "organizer": "Vinish Kumar", "join_url": join, "status": "Busy"}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def _n(*a, **k):
        return {}
    monkeypatch.setattr("app.notify.notify", _n)
    offers.drop_all()


def test_a_meeting_starting_asks_whether_to_join():
    asyncio.run(briefing._offer_to_join(_ev(), NOW))
    o = offers.pending()
    assert o is not None, "nothing was offered"
    assert "AI Ideathon" in o.subject
    assert "join" in o.render().lower()


def test_the_yes_runs_a_recorded_join_not_a_brain_instruction():
    """A brain re-reading "join the meeting" could join a different one, or decide
    the tool call was optional. The recorded args cannot drift."""
    asyncio.run(briefing._offer_to_join(_ev(), NOW))
    o = offers.pending()
    assert o.mechanical(), "the offer would be handed back to a brain as prose"
    assert o.op["name"] == "meeting_join"
    assert o.op["args"]["join_url"].startswith("https://teams.microsoft.com")


def test_it_defaults_to_listening():
    """A room full of people did not ask for an assistant's opinion, and he may
    well be in it himself."""
    asyncio.run(briefing._offer_to_join(_ev(), NOW))
    assert offers.pending().op["args"]["speak"] is False


def test_it_says_how_to_ask_for_the_other_mode():
    """"sometimes i will ask u to join. be silent , and sometimes will ask you to
    tak accordingly" — an offer he can only answer yes/no hides half of that."""
    asyncio.run(briefing._offer_to_join(_ev(), NOW))
    assert "join and talk" in offers.pending().render()


def test_the_same_meeting_is_only_offered_once():
    """The loop ticks every 60s across a two-minute window."""
    asyncio.run(briefing._offer_to_join(_ev(), NOW))
    asyncio.run(briefing._offer_to_join(_ev(), NOW))
    # Compared by CONTENT, not identity: `pending()` deserialises a fresh object
    # every call, so `is` would pass whatever the code did.
    assert len(offers.waiting()) <= 1, "queued a second offer for one meeting"


def test_a_meeting_with_no_link_is_not_offered():
    """There is nothing to join. The prep ping already covered the meeting itself,
    and an offer that cannot be honoured is worse than none."""
    asyncio.run(briefing._offer_to_join(_ev(join=""), NOW))
    assert offers.pending() is None


def test_declining_leaves_the_meeting_alone():
    """"no simple ignore let the call automatically end"."""
    asyncio.run(briefing._offer_to_join(_ev(), NOW))
    assert offers.decline() is not None
    assert offers.pending() is None


def test_the_op_prefers_the_captured_link_over_the_title():
    """Between the ping and his yes the calendar can move on."""
    import inspect
    src = inspect.getsource(ops._meeting_join)
    assert "if join_url:" in src
    assert "join_by_phrase" in src          # the fallback still exists


def test_the_op_describes_both_modes():
    assert "listen only" in ops.REGISTRY["meeting_join"]["describe"]({"title": "x"})
    assert "take part" in ops.REGISTRY["meeting_join"]["describe"](
        {"title": "x", "speak": True})
