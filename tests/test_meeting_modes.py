"""Joining silent, and joining to take part.

    "sometimes i will ask u to join. be silent , and sometimes will ask you to tak
     accordingly u will have that capabolity"

Both halves existed and only one was reachable. `may_speak()`, `handle_ask()` and
`_say_quietly()` are a complete in-meeting speech path — and `_CALL["speaks"]` was
assigned False in two places and True in none, so every one of them was dead code.
Asta could not have spoken in a joined meeting whatever it was told.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from app import meetings


def test_the_default_is_still_silence():
    """A room full of people did not ask for an assistant's opinion, and he may
    well be in it himself."""
    assert inspect.signature(meetings.join).parameters["speak"].default is False


def test_taking_part_is_reachable_at_all():
    """The bug this closes: the flag existed, the machinery existed, and nothing
    could ever set it."""
    src = inspect.getsource(meetings.join)
    assert "speaks=bool(speak)" in src


def test_it_refuses_to_take_part_with_no_virtual_microphone(monkeypatch):
    """An unmuted join is only safe because the system input points at BlackHole,
    so what goes out is synthesis rather than the room he is sitting in. Without
    one, unmuting broadcasts his real microphone to the whole meeting."""
    from app import voice
    monkeypatch.setattr(voice, "can_speak", lambda: False)
    monkeypatch.setattr(meetings, "_CALL", {})
    with pytest.raises(RuntimeError, match="virtual microphone"):
        asyncio.run(meetings.join("https://teams.microsoft.com/x", speak=True))


def test_it_does_not_mute_itself_when_asked_to_take_part():
    """Joining muted and then trying to speak is the silent-call failure Vinish sat
    through four times."""
    src = inspect.getsource(meetings.join)
    assert "if muted and not speak:" in src


def test_the_named_form_carries_the_mode_through():
    """"join the standup and answer for me" must not quietly become a silent join."""
    assert inspect.signature(meetings.join_by_phrase).parameters["speak"].default is False
    assert "speak=speak" in inspect.getsource(meetings.join_by_phrase)


def test_both_agent_tools_expose_it():
    from app import agent
    for fn in (agent.join_meeting, agent.join_meeting_by_name):
        assert "speak" in inspect.signature(fn).parameters, fn.__name__


def test_the_tools_say_when_not_to_use_it():
    """The rule has to travel with the capability, or a model reaches for the
    louder option because it is available."""
    import re

    from app import agent
    for fn in (agent.join_meeting, agent.join_meeting_by_name):
        # Whitespace-normalised: the rule wraps across lines in one of them, and a
        # raw substring test would pass or fail on where the line happened to break.
        doc = re.sub(r"\s+", " ", fn.__doc__ or "")
        assert "ONLY when he asked" in doc, fn.__name__
