"""Teams triage + call recap — the SAFE 'calls & meetings' pieces: draft & summarize,
never join a live call, never send without a yes.
"""

from __future__ import annotations

import asyncio

import pytest

from app import agent


# --- meeting_recap ----------------------------------------------------------

def test_recap_summarizes_a_transcript(monkeypatch):
    from app import memory, notify
    monkeypatch.setattr(memory, "local_llm_complete",
                        lambda *a, **k: "**TL;DR**\nShipped v2.\n**Decisions**\n- go\n"
                                        "**Action items**\n- Bob writes docs\n**Open questions**\n- none")
    monkeypatch.setattr(notify, "notify", _no_notify)
    out = asyncio.run(agent.meeting_recap("A" * 100, "Release sync"))
    assert "Recap" in out and "Release sync" in out and "Shipped v2" in out


def test_recap_pings_arun_when_an_item_is_his(monkeypatch):
    from app import memory, notify
    pinged = {}

    async def fake_notify(text, *a, **k):
        pinged["text"] = text

    monkeypatch.setattr(memory, "local_llm_complete",
                        lambda *a, **k: "**Action items**\n- ARUN: sign off the release")
    monkeypatch.setattr(notify, "notify", fake_notify)
    asyncio.run(agent.meeting_recap("B" * 100))
    assert "needs you" in pinged.get("text", "")            # he was pinged


def test_recap_does_not_ping_when_nothing_is_his(monkeypatch):
    from app import memory, notify
    pinged = {}

    async def fake_notify(text, *a, **k):
        pinged["text"] = text

    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "**Decisions**\n- ship it")
    monkeypatch.setattr(notify, "notify", fake_notify)
    asyncio.run(agent.meeting_recap("C" * 100))
    assert "text" not in pinged


def test_recap_asks_for_the_transcript_when_too_short(monkeypatch):
    out = asyncio.run(agent.meeting_recap("too short"))
    assert "transcript" in out.lower()


# --- draft_teams_reply ------------------------------------------------------

def test_draft_reply_uses_thread_context_and_never_sends(monkeypatch):
    from app import memory, teams_bridge
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)

    async def fake_read(chat, limit):
        return ["Priya: can you review the PR today?"]

    def boom(*a, **k):
        raise AssertionError("draft must NEVER send — only prepare_to_send does, on a yes")

    monkeypatch.setattr(teams_bridge, "read_chat", fake_read)
    monkeypatch.setattr(teams_bridge, "send_message", boom)
    monkeypatch.setattr(memory, "recall_block", lambda q: "PR #8 is ready for review")
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "Yes — reviewing #8 now.")
    out = asyncio.run(agent.draft_teams_reply("Priya"))
    assert "Draft reply to Priya" in out and "reviewing #8" in out
    assert "send" in out.lower()                            # tells Arun how to confirm


def test_draft_reply_reports_when_it_lacks_the_question(monkeypatch):
    from app import teams_bridge
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(teams_bridge, "read_chat", _empty_thread)
    out = asyncio.run(agent.draft_teams_reply("Sam"))
    assert "No question" in out


def test_recap_needs_arun_marker():
    assert agent._recap_needs_arun("- ARUN: do X")
    assert not agent._recap_needs_arun("- Bob: do X")


async def _no_notify(*a, **k):
    return {}


async def _empty_thread(chat, limit):
    return []
