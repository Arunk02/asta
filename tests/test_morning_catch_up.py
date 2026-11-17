"""Nights off: the morning brief waits for the overnight read, then says who
wrote while he was away and is still waiting.

17 Nov: the laptop slept from 22:34; the brief went at 08:59 saying "Teams read
577 min ago", written from the view of the night before."""
from __future__ import annotations

import time

from app import attention, briefing, store


def _msg(key, chat, sender, text, ago):
    store.save_teams_messages([{"key": key, "chat": chat, "sender": sender, "text": text,
                                "sent_at": time.time() - ago, "stamp": ""}])


def test_the_brief_waits_until_the_watchers_have_read_after_waking():
    now = time.time()
    attention.note_scrape("teams-chat", now - 9 * 3600)
    assert not briefing.sources_caught_up(now)
    attention.note_scrape("teams-chat", now - 120)
    assert briefing.sources_caught_up(now)


def test_overnight_one_to_ones_still_waiting_are_listed():
    since = time.time() - 10 * 3600
    _msg("a1", "Colleague One", "Colleague One", "can you check the SIT deploy?", 5 * 3600)
    _msg("b1", "Colleague Two", "Colleague Two", "PR is up for review", 4 * 3600)
    _msg("b2", "Colleague Two", "Arunkumar K", "looking now", 3 * 3600)
    _msg("g1", "Some Group", "Colleague Three", "build is green", 2 * 3600)   # not for him
    waiting, answered = briefing.overnight_waiting(since)
    assert len(waiting) == 1 and waiting[0].startswith("• Colleague One (1, last")
    assert "SIT deploy" in waiting[0]
    assert answered == 1
