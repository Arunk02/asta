"""Meeting / 1:1 prep — pick the right meeting, synthesize locally, degrade gracefully.
The 'calls & meetings' piece that IS doable now (drafting), separate from telephony.
"""

from __future__ import annotations

import asyncio

import pytest

from app import agent


def _meetings():
    return [
        {"title": "Team standup", "start": "09:15", "minutes": 555, "organizer": ""},
        {"title": "1:1 with Priya", "start": "15:00", "minutes": 900, "organizer": "Priya"},
        {"title": "Lunch", "start": "12:30", "minutes": 750, "organizer": ""},
    ]


# --- picking the meeting ----------------------------------------------------

def test_pick_by_title_substring():
    m = agent._pick_meeting(_meetings(), "priya")
    assert m and m["title"] == "1:1 with Priya"


def test_pick_defaults_to_next_speaking_meeting():
    # empty title → the first meeting Arun has to speak in (standup), not Lunch.
    assert agent._pick_meeting(_meetings(), "")["title"] == "Team standup"


def test_pick_returns_none_when_title_absent():
    assert agent._pick_meeting(_meetings(), "board review") is None


# --- meeting_prep end to end ------------------------------------------------

def _wire(monkeypatch, meetings, local_out):
    from app import briefing, memory

    async def fake_meetings():
        return meetings

    async def fake_jira():
        return "- ABC-1 fix the thing"

    monkeypatch.setattr(briefing, "_cached_meetings", fake_meetings)
    monkeypatch.setattr(agent, "jira_my_issues", fake_jira)
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: local_out)


def test_prep_uses_the_local_model_when_up(monkeypatch):
    _wire(monkeypatch, _meetings(),
          "**Talking points**\n- ship the fix\n**Questions to ask**\n- when to deploy")
    out = asyncio.run(agent.meeting_prep("priya"))
    assert "1:1 with Priya" in out and "with Priya" in out
    assert "ship the fix" in out                # the synthesized body came through


def test_prep_says_nothing_rather_than_handing_back_an_empty_form(monkeypatch):
    """It used to push three empty bullets under "(local model offline)" half an
    hour before a meeting. That is not prep — it is the assistant handing the work
    back with extra steps, and it still costs a read on his phone."""
    _wire(monkeypatch, _meetings(), None)       # nothing can answer
    assert asyncio.run(agent.meeting_prep("standup")) == agent.NO_PREP


def test_prep_tries_a_paid_brain_before_giving_up(monkeypatch):
    """Local-model-FIRST was right; local-model-ONLY was an accident of it. He
    walks into this meeting in half an hour — a short turn on a subscription he
    already pays for is worth more than an apology."""
    _wire(monkeypatch, _meetings(), None)

    async def one_shot(prompt, **kw):
        return "**Talking points**\n- ship the fix"

    monkeypatch.setattr(agent, "best_model_name", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(agent, "EXECUTORS", ("copilot",))
    monkeypatch.setattr(agent, "available", lambda n: True)
    monkeypatch.setattr(agent, "runner", lambda n: type("M", (), {"one_shot": staticmethod(one_shot)}))
    out = asyncio.run(agent.meeting_prep("standup"))
    assert "ship the fix" in out


def test_prep_when_nothing_on_the_calendar(monkeypatch):
    _wire(monkeypatch, [], "x")
    assert "No meetings" in asyncio.run(agent.meeting_prep(""))


def test_prep_reports_when_the_named_meeting_is_absent(monkeypatch):
    _wire(monkeypatch, _meetings(), "x")
    out = asyncio.run(agent.meeting_prep("quarterly board review"))
    assert "No meeting matching" in out and "Team standup" in out   # lists what IS on


def test_prep_survives_a_jira_failure(monkeypatch):
    from app import briefing, memory

    async def fake_meetings():
        return _meetings()

    async def boom():
        raise RuntimeError("jira down")

    monkeypatch.setattr(briefing, "_cached_meetings", fake_meetings)
    monkeypatch.setattr(agent, "jira_my_issues", boom)
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "**Talking points**\n- x")
    out = asyncio.run(agent.meeting_prep("priya"))   # jira blew up, prep still lands
    assert "1:1 with Priya" in out


# --- the shared completion path ----------------------------------------------
#
# From a real notification, 29 minutes before a code review:
#
#     📝 Draft for it:
#     📝 Prep — Code Review at 11:15 AM (with Vinish Kumar):
#     (local model offline — blank checklist)
#     *Talking points*
#     -
#
# LM Studio was closed, and nine background features called local_llm_complete
# directly and treated None as "emit whatever you have". "Free-first" was right;
# "free-only" was an accident of it that made the background half of Asta depend
# on one optional process nobody is told to keep running.

def test_the_free_brain_is_still_tried_first(monkeypatch):
    """Cost discipline is the whole reason the local model exists. If it answers,
    nothing paid may be reached."""
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda p, n=400: "local said so")
    monkeypatch.setattr(agent, "best_model_name",
                        lambda: pytest.fail("a paid brain was reached unnecessarily"))
    out = asyncio.run(memory.cheap_complete("anything", paid_ok=True))
    assert out == "local said so"


def test_background_housekeeping_never_escalates_to_a_paid_brain(monkeypatch):
    """Re-digesting yesterday's chat at 2am is worth skipping a night. Prep for a
    meeting he walks into is not — that is what the flag distinguishes."""
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda p, n=400: None)
    monkeypatch.setattr(agent, "best_model_name",
                        lambda: pytest.fail("housekeeping must not spend money"))
    assert asyncio.run(memory.cheap_complete("digest this", paid_ok=False)) is None


def test_nothing_available_returns_none_rather_than_a_placeholder(monkeypatch):
    """Callers must be able to tell "no answer" from "here is an empty answer" —
    the whole bug was a caller that could not."""
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda p, n=400: None)
    monkeypatch.setattr(agent, "EXECUTORS", ())
    assert asyncio.run(memory.cheap_complete("x", paid_ok=True)) is None


def test_a_blank_answer_counts_as_no_answer(monkeypatch):
    """A model that returns whitespace has not answered, and treating that as
    content is what put three empty bullets on his phone."""
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda p, n=400: "   \n  ")
    monkeypatch.setattr(agent, "EXECUTORS", ())
    monkeypatch.setattr(agent, "best_model_name",
                        lambda: (_ for _ in ()).throw(RuntimeError()))
    assert asyncio.run(memory.cheap_complete("x", paid_ok=True)) is None
