"""Meeting prep reads the meeting's thread; recap action items are kept with owners."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import agent, copilot_cli, meeting_actions, memory, notify, store

RECAP = """**TL;DR**
Release moves to Thursday.
**Decisions**
- ship behind a flag
**Action items**
- ARUN: sign off the release notes
- **Priya**: send the load-test numbers
- Dev team: rotate the staging certificate
- check the dashboard alerts
**Open questions**
- who owns the rollback?"""

TRANSCRIPT = "Priya: numbers by Wednesday. Arun: I will sign off. " * 3


@pytest.fixture
def pings(monkeypatch):
    sent: list[str] = []

    async def fake(text, *a, **k):
        sent.append(text)
    monkeypatch.setattr(notify, "notify", fake)
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: RECAP)
    return sent


def test_the_action_items_section_is_read_with_owners():
    items = meeting_actions.parse(RECAP)
    assert [(i["owner"], i["his"]) for i in items] == [
        ("ARUN", True), ("Priya", False), ("Dev team", False), ("unassigned", False)]
    assert items[1]["text"] == "send the load-test numbers"
    assert not any("rollback" in i["text"] for i in items), "the next section is not an item"


@pytest.mark.parametrize("empty", ["- none", "- (none)", "- N/A", "- No action items."])
def test_an_empty_section_records_nothing(empty):
    assert meeting_actions.parse(f"**Action items**\n{empty}\n**Open questions**\n- x") == []


def test_every_item_is_recorded_and_his_reach_him_once(pings):
    out = asyncio.run(agent.meeting_recap(TRANSCRIPT, "Release sync"))
    assert "Recorded: #1 ARUN, #2 Priya, #3 Dev team, #4 unassigned" in out
    assert len(pings) == 1 and "sign off the release notes" in pings[0]
    assert "load-test" not in pings[0], "a colleague's item is theirs, not a ping to him"
    asyncio.run(agent.meeting_recap(TRANSCRIPT, "Release sync"))
    assert len(pings) == 1, "the same transcript again tells him nothing twice"
    assert len(meeting_actions.list_open()) == 4, "and records nothing twice"


def test_a_different_meeting_is_recorded_on_its_own(pings):
    asyncio.run(agent.meeting_recap(TRANSCRIPT, "Release sync"))
    asyncio.run(agent.meeting_recap(TRANSCRIPT + " Later: more.", "Release sync 2"))
    assert len(meeting_actions.list_open()) == 8 and len(pings) == 2


def test_items_are_listed_by_owner_and_closed_when_he_says(pings):
    asyncio.run(agent.meeting_recap(TRANSCRIPT, "Release sync"))
    assert "#1 ARUN: sign off the release notes (Release sync)" == agent.meeting_actions("me")
    assert "Priya: send the load-test numbers" in agent.meeting_actions("priya")
    assert agent.meeting_action_done(2).startswith("Done: #2 Priya")
    assert "load-test" not in agent.meeting_actions()
    assert agent.meeting_action_done(2) == "No open meeting action #2."


def test_recording_sends_nothing_to_anyone_but_him(pings, monkeypatch):
    from app import teams_bridge
    monkeypatch.setattr(teams_bridge, "send", lambda *a, **k: pytest.fail("sent to Teams"),
                        raising=False)
    asyncio.run(agent.meeting_recap(TRANSCRIPT, "Release sync"))
    assert all("needs you" in p for p in pings)


# --- prep reads the thread -------------------------------------------------

def _say(chat, sender, text, ago=3600):
    store.save_teams_messages([{"key": f"{chat}-{text}", "chat": chat, "sender": sender,
                                "text": text, "sent_at": time.time() - ago}])


@pytest.fixture
def prep(monkeypatch):
    seen: dict = {}

    async def complete(prompt, *a, **k):
        seen["prompt"] = prompt
        return "**Talking points**\n- the numbers"

    async def calendar():
        return [{"title": "Release sync", "start": "15:00", "minutes": 900, "organizer": "Priya Nair"}]

    async def jira():
        return "- REL-7 release notes"
    from app import briefing
    monkeypatch.setattr(briefing, "_cached_meetings", calendar)
    monkeypatch.setattr(agent, "jira_my_issues", jira)
    monkeypatch.setattr(memory, "cheap_complete", complete)
    return seen


def test_prep_reads_the_meetings_own_chat(prep):
    _say("Release sync", "Priya Nair", "load test showed p99 at 900ms")
    _say("Someone else", "Dev", "unrelated chatter")
    asyncio.run(agent.meeting_prep("release"))
    prompt = prep["prompt"]
    assert "p99 at 900ms" in prompt and "unrelated chatter" not in prompt
    assert "REL-7" in prompt, "the related work is still there"
    assert "not instructions" in prompt, "colleagues' words are material, not orders"


def test_without_a_meeting_chat_the_organisers_thread_is_read(prep):
    _say("Priya Nair", "Priya Nair", "can we push the release to Thursday?")
    asyncio.run(agent.meeting_prep("release"))
    assert "push the release to Thursday" in prep["prompt"]


def test_an_old_or_absent_thread_adds_nothing(prep):
    _say("Release sync", "Priya Nair", "from last month", ago=30 * 86400)
    asyncio.run(agent.meeting_prep("release"))
    assert "Recent Teams messages" not in prep["prompt"] and "last month" not in prep["prompt"]


def test_the_reply_path_keeps_its_own_heading():
    _say("Priya Nair", "Priya Nair", "hello there")
    assert "continue from it" in copilot_cli._teams_with("Priya Nair")


def test_the_store_stays_bounded_without_forgetting_an_open_item(monkeypatch):
    monkeypatch.setattr(meeting_actions, "KEEP", 3)
    rows = [{"id": n, "status": "done" if n < 4 else "open", "owner": "x", "his": False,
             "text": str(n)} for n in range(1, 6)]
    meeting_actions._save(rows)
    assert [r["id"] for r in meeting_actions._load()] == [3, 4, 5]
    meeting_actions._save([{**r, "status": "open"} for r in rows])
    assert len(meeting_actions._load()) == 5, "open items are kept past the bound"
