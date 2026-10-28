"""A follow-up reads the chased person's side: "please stop" ends it, an answer
counts as movement. Both paths — the plain loop and the promise graph."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import followup, store
from tests.test_followup import _A, _pr, prs  # noqa: F401


@pytest.fixture(params=["0", "1"], ids=["loop", "graph"])
def engine(request, monkeypatch):
    monkeypatch.setenv("ASTA_GRAPHS", request.param)
    return request.param


def stalled(prs):
    prs[_A] = _pr(checks="red")
    now = time.time()
    row = followup.track("get the retry PR merged", [_A], "Alex Doe")
    rows = followup._load()
    for r in rows:
        r["created_at"] = r["last_moved_at"] = now - followup.STALL_SECONDS - 600
    followup._save(rows)
    return row["id"], now


def said(text, *, by="Alex Doe", ago=60):
    store.save_teams_messages([{"key": f"m-{text}-{by}", "chat": "Alex Doe", "sender": by,
                                "text": text, "sent_at": time.time() - ago}])


def drafts():
    return [t for t in store.list_tasks(limit=20) if t["kind"] == "teams_draft"]


def test_without_a_word_from_them_the_chase_goes_ahead(prs, engine):
    fid, _ = stalled(prs)
    asyncio.run(followup.check_all())
    assert len(drafts()) == 1


@pytest.mark.parametrize("text", ["please stop pinging me, I'm on it", "don't remind me again",
                                  "no need to follow up, will merge today"])
def test_their_ask_to_stop_ends_the_follow_up(prs, engine, text):
    fid, _ = stalled(prs)
    said(text)
    out = asyncio.run(followup.check_all())
    row = next(r for r in followup._load() if r["id"] == fid)
    assert row["status"] == "stopped" and drafts() == []
    assert any("Stopped chasing Alex Doe" in o for o in out)
    asyncio.run(followup.check_all())
    assert drafts() == [], "a stopped follow-up stays stopped"


def test_an_answer_counts_as_movement_and_holds_the_next_nudge(prs, engine):
    fid, _ = stalled(prs)
    said("on it, will check today")
    asyncio.run(followup.check_all())
    row = next(r for r in followup._load() if r["id"] == fid)
    assert row["status"] == "open" and drafts() == []
    assert row["their_reply"] == "on it, will check today"


def test_his_own_lines_and_old_messages_are_not_their_answer(prs, engine, monkeypatch):
    from app import chat_watch
    fid, _ = stalled(prs)
    monkeypatch.setattr(chat_watch, "is_from_him", lambda sender: sender == "Arun K")
    said("please stop pinging me", by="Arun K")                       # his (or Asta's) line
    said("please stop pinging me", ago=followup.STALL_SECONDS + 3600)  # before tracking began
    asyncio.run(followup.check_all())
    assert next(r for r in followup._load() if r["id"] == fid)["status"] == "open"
    assert len(drafts()) == 1
