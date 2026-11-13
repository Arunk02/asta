"""13 Nov: `teams_unread` re-opened every chat in the one shared browser while the
watcher's own pass read the same chats; reads queued behind each other "timed
out" and the brain's turn hit its ceiling with no answer. It now answers from
what the watcher already stored. (Synthetic names.)"""
from __future__ import annotations

import asyncio
import time

from app import agent, chat_watch, store, teams_bridge


def _msgs(chat, *texts, sender=None, start=None):
    start = start or time.time() - 600
    rows = [{"key": f"{chat}:{i}:{t}", "chat": chat, "sender": sender or chat, "text": t,
             "sent_at": start + i} for i, t in enumerate(texts)]
    store.save_teams_messages(rows)
    return rows


def _no_browser(monkeypatch):
    async def refuse(*a, **k):
        raise AssertionError("teams_unread opened the browser")
    monkeypatch.setattr(teams_bridge, "read_history", refuse)
    monkeypatch.setattr(chat_watch, "candidates", refuse)


def test_unprocessed_messages_come_from_the_store(monkeypatch):
    _no_browser(monkeypatch)
    done = _msgs("Ravi Menon", "morning", "is the build green?")
    chat_watch.remember("Ravi Menon", done[:1])
    chat_watch.mark_processed("Ravi Menon", done[:1])
    got = asyncio.run(chat_watch.pending())
    assert [(g["chat"], g["text"]) for g in got] == [("Ravi Menon", "is the build green?")]


def test_what_the_watcher_handled_is_not_listed(monkeypatch):
    _no_browser(monkeypatch)
    rows = _msgs("Priya Nair", "thanks", "see you")
    chat_watch.remember("Priya Nair", rows)
    chat_watch.mark_processed("Priya Nair", rows)
    assert asyncio.run(chat_watch.pending()) == []


def test_his_own_messages_and_old_threads_are_left_out(monkeypatch):
    _no_browser(monkeypatch)
    monkeypatch.setattr(chat_watch, "is_from_him", lambda s: s == "Arun K")
    _msgs("Kiran Das", "on it", sender="Arun K")
    _msgs("Asha Rao", "from last week", start=time.time() - 8 * 86400)
    assert asyncio.run(chat_watch.pending()) == []


def test_a_similar_chat_name_does_not_borrow_its_messages(monkeypatch):
    """`teams_messages` matches a name loosely; "Ravi" must not read "Ravi Menon"."""
    _no_browser(monkeypatch)
    _msgs("Ravi Menon", "one")
    _msgs("Ravi", "two")
    got = asyncio.run(chat_watch.pending())
    assert sorted((g["chat"], g["text"]) for g in got) == [("Ravi", "two"), ("Ravi Menon", "one")]


def test_the_tool_answers_without_the_browser(monkeypatch):
    _no_browser(monkeypatch)
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(chat_watch, "open_with_him", lambda: [])
    _msgs("Ravi Menon", "is the build green?")
    out = asyncio.run(agent.teams_unread())
    assert "1 message(s)" in out and "Ravi Menon" in out
