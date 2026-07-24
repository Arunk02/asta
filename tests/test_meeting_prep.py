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


def test_prep_falls_back_to_a_skeleton_when_local_down(monkeypatch):
    _wire(monkeypatch, _meetings(), None)       # local model offline
    out = asyncio.run(agent.meeting_prep("standup"))
    assert "Talking points" in out and "blank checklist" in out


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
