"""His standup: the tickets assigned to him in the current sprint, and nothing else.

His words, 17 Sep: "Don't include PR review or some file opening anything related
in standup, there u should mention only related to the tickets which gets assigned
to u .. remember this always" — "That also in the current sprint".

That was said to Asta, a brain agreed and "saved" it into its own notes, and the
standup — which is assembled by code from commits, Asta's finished tasks and every
Jira update — never read those notes. So the rule is here, in the code that builds
the standup, where it cannot be forgotten by the next session.
"""

from __future__ import annotations

import asyncio

import pytest

from app import briefing, jira, store

SPRINT = [
    {"key": "BK-101", "summary": "Point booking service at new Kafka topics",
     "status": "In Progress", "updated": "2099-01-01T09:00:00.000+0000"},
    {"key": "BK-102", "summary": "Fix BookingEquipment equals/hashCode",
     "status": "To Do", "updated": "2000-01-01T09:00:00.000+0000"},
]


@pytest.fixture
def _brain(monkeypatch):
    seen: list = []

    async def one_shot(prompt, **kw):
        seen.append(prompt)
        return "STANDUP"

    monkeypatch.setattr(briefing.copilot_cli, "one_shot", one_shot)
    return seen


def test_only_sprint_tickets_reach_the_standup(monkeypatch, _brain):
    """No PR reviews, no Asta tasks, no commits — the three things the old
    standup was built from."""
    async def sprint(limit=30):
        return SPRINT

    async def commits(since):
        raise AssertionError("commits must not be read for the standup")

    monkeypatch.setattr(jira, "current_sprint", sprint)
    monkeypatch.setattr(jira, "configured", lambda: True)
    monkeypatch.setattr(briefing, "_recent_commits", commits)
    t = store.create_task("Komal Jayswal's review on PR #1440: is it right?",
                          "analysis", "p", None)
    store.update_task(t["id"], status="done")

    out = asyncio.run(briefing.standup_draft())
    assert out == "STANDUP"
    prompt = _brain[0]
    assert "BK-101" in prompt and "BK-102" in prompt
    assert "PR #1440" not in prompt and "review" not in prompt.split("TICKETS")[-1].lower()


def test_the_brain_is_told_the_rule_not_just_given_the_data(monkeypatch, _brain):
    async def sprint(limit=30):
        return SPRINT

    monkeypatch.setattr(jira, "current_sprint", sprint)
    monkeypatch.setattr(jira, "configured", lambda: True)
    asyncio.run(briefing.standup_draft())
    prompt = _brain[0].lower()
    assert "only" in prompt and "pr review" in prompt


def test_a_rejected_jira_token_is_said_not_passed_off_as_a_quiet_sprint(monkeypatch, _brain):
    """With the token rejected every search came back empty — which reads exactly
    like "nothing assigned to you this sprint". The standup says which it is."""
    async def sprint(limit=30):
        raise jira.JiraAuthError("Jira rejected Asta's API token")

    monkeypatch.setattr(jira, "current_sprint", sprint)
    monkeypatch.setattr(jira, "configured", lambda: True)
    out = asyncio.run(briefing.standup_draft())
    assert "can't read Jira" in out and "token" in out and _brain == []


def test_an_empty_sprint_is_said_plainly(monkeypatch, _brain):
    async def sprint(limit=30):
        return []

    monkeypatch.setattr(jira, "current_sprint", sprint)
    monkeypatch.setattr(jira, "configured", lambda: True)
    out = asyncio.run(briefing.standup_draft())
    assert "nothing assigned to you in the current sprint" in out.lower() and _brain == []


def test_finished_tickets_from_long_ago_are_not_standup_material(monkeypatch, _brain):
    """Live, 17 Sep, first run with a working token: 21 tickets, 18 of them Done
    or Closed months ago in a project whose sprint is still technically open.
    A standup is what is moving — in progress, to do, or finished yesterday."""
    tickets = SPRINT + [
        {"key": "OHM-1", "summary": "Old finished thing", "status": "Done",
         "updated": "2000-01-01T09:00:00.000+0000"},
        {"key": "OHM-2", "summary": "Closed ages ago", "status": "Closed",
         "updated": "2000-01-01T09:00:00.000+0000"},
        {"key": "BK-103", "summary": "Finished yesterday", "status": "Done",
         "updated": "2099-01-01T09:00:00.000+0000"},
    ]

    async def sprint(limit=30):
        return tickets

    monkeypatch.setattr(jira, "current_sprint", sprint)
    monkeypatch.setattr(jira, "configured", lambda: True)
    asyncio.run(briefing.standup_draft())
    prompt = _brain[0]
    assert "OHM-1" not in prompt and "OHM-2" not in prompt
    assert "BK-101" in prompt and "BK-102" in prompt and "BK-103" in prompt


def test_the_standup_is_phrased_by_whichever_brain_still_has_quota(monkeypatch):
    """Live, 17 Sep: Copilot's quota was spent, and the standup — calling Copilot
    by name — fell back to a raw ticket dump while Claude sat there available.
    Every other path already fails over; this one does now too."""
    from app import agent, claude_cli, copilot_cli

    async def sprint(limit=30):
        return SPRINT

    async def exhausted(prompt, **kw):
        raise RuntimeError("quota exhausted")

    async def claude(prompt, **kw):
        return "PHRASED BY CLAUDE"

    monkeypatch.setattr(jira, "current_sprint", sprint)
    monkeypatch.setattr(jira, "configured", lambda: True)
    monkeypatch.setattr(copilot_cli, "one_shot", exhausted)
    monkeypatch.setattr(claude_cli, "one_shot", claude)
    monkeypatch.setattr(agent, "quota_down", lambda name: False)
    monkeypatch.setattr(agent, "model_registry",
                        lambda: {n: {"available": True} for n in agent.EXECUTORS})
    assert asyncio.run(briefing.standup_draft()) == "PHRASED BY CLAUDE"


def test_the_standup_never_arrives_inside_a_code_block(monkeypatch):
    """His guardrails: no backticks or code blocks on WhatsApp. The brain wrapped
    the first live standup in a ``` fence anyway."""
    async def sprint(limit=30):
        return SPRINT

    async def fenced(prompt, **kw):
        return "```\nYesterday: none\nToday: BK-101\nBlockers: none\n```"

    from app import agent
    monkeypatch.setattr(jira, "current_sprint", sprint)
    monkeypatch.setattr(jira, "configured", lambda: True)
    monkeypatch.setattr(agent, "one_shot_any", fenced)
    out = asyncio.run(briefing.standup_draft())
    assert "```" not in out and out.startswith("Yesterday")
