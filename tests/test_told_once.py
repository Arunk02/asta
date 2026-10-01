"""A conversation tells him something once.

29 Sep, 16:02–17:33: "Rajendra Kumar in SCP Deployment to PP and UAT: Team is
coordinating SCP deployment…" reached his phone eight times, each copy the same
summary with the latest raw message glued on. Three causes, each fixed here:

  * the model step fell back to rules on every live sweep, and said only
    "1/1 by rules" — so nobody could see why;
  * the rules floor appended the raw message to the summary and cut it at 400
    characters, so once full, every sweep produced the SAME line;
  * nothing remembered what a conversation had already told him.

And the same afternoon: every meeting invite twice (Teams feed + Outlook mail),
and Chrome relaunched twelve times in ten minutes over rail entries that are not
chats ("Followed threads", community channels).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import chat_watch, store, teams_bridge, understand


def _last_understand() -> str:
    return next(r["detail"] for r in store.recent_outcomes(50) if r["kind"] == "understand")


def test_the_rules_floor_keeps_the_summary_it_has():
    item = {"id": "t", "new": ["Rajendra Kumar: Hi Vinish/ Arunkumar,\n\nPlease check the build"],
            "so_far": "Team is coordinating SCP deployment to PP and UAT."}
    assert understand.rules(item)["summary"] == "Team is coordinating SCP deployment to PP and UAT."


def test_the_rules_floor_starts_a_summary_from_the_message_when_there_is_none():
    item = {"id": "t", "new": ["Can you check booking 88271?"], "so_far": ""}
    assert understand.rules(item)["summary"] == "Can you check booking 88271?"


def test_the_same_line_is_told_once():
    now = time.time()
    line = "Team is coordinating SCP deployment to PP and UAT."
    assert chat_watch._worth_telling("teams:SCP", line, fyi=True, group=True, now=now)
    for later in (now + 150, now + 3600, now + 5 * 3600):
        assert not chat_watch._worth_telling("teams:SCP", line, fyi=True, group=True, now=later)


def test_a_busy_group_says_fyi_at_most_every_half_hour():
    now = time.time()
    assert chat_watch._worth_telling("teams:SCP", "PP is up", fyi=True, group=True, now=now)
    assert not chat_watch._worth_telling("teams:SCP", "UAT in 15 mins", fyi=True, group=True,
                                         now=now + 600)
    assert chat_watch._worth_telling("teams:SCP", "UAT is up", fyi=True, group=True,
                                     now=now + chat_watch.GROUP_FYI_SECONDS + 1)


def test_a_new_ask_is_never_held_back_by_an_fyi():
    now = time.time()
    assert chat_watch._worth_telling("teams:SCP", "PP is up", fyi=True, group=True, now=now)
    assert chat_watch._worth_telling("teams:SCP", "bookings stuck in TMS, please check",
                                     fyi=False, group=True, now=now + 60)


def test_conversations_do_not_share_what_they_told():
    now = time.time()
    assert chat_watch._worth_telling("teams:A", "same words", fyi=False, group=False, now=now)
    assert chat_watch._worth_telling("teams:B", "same words", fyi=False, group=False, now=now)


def test_an_invite_is_told_by_the_mail_reader_alone(monkeypatch):
    row = "GSC Safety and Resilience invited you — BRAD2026 Event - Session on People Resilience"
    store.kv_set("attention_scrape:outlook", str(time.time()))
    assert teams_bridge.duplicates_chat_watch(row)
    store.kv_set("attention_scrape:outlook", str(time.time() - 7200))
    monkeypatch.setenv("ASTA_CHATWATCH", "0")
    assert not teams_bridge.duplicates_chat_watch(row), "mail not being read: the feed keeps it"


def test_a_rail_entry_search_cannot_open_is_skipped_for_a_day():
    assert chat_watch.is_furniture("Followed threads")
    assert not chat_watch.unopenable("Architecture Forums")
    chat_watch.note_unopenable("Architecture Forums")
    assert chat_watch.unopenable("Architecture Forums")


def test_a_chat_not_found_does_not_relaunch_the_browser(monkeypatch):
    class Page:
        went = []

        async def goto(self, url, **_):
            self.went.append(url)

    page = Page()
    discarded = []

    async def pooled():
        return page

    async def discard(why=""):
        discarded.append(why)

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge, "_discard_pool", discard)

    async def run(exc):
        with pytest.raises(type(exc)):
            async with teams_bridge.teams_page():
                raise exc

    asyncio.run(run(teams_bridge.NotFound("no person match for 'Followed threads'")))
    assert discarded == [] and page.went == [teams_bridge.TEAMS_URL]
    asyncio.run(run(TimeoutError("Page.wait_for_selector: Timeout 20000ms exceeded.")))
    assert len(discarded) == 1, "an unknown failure still throws the page away"


def test_a_fallback_to_rules_says_why(monkeypatch):
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "haiku")

    async def broken(_):
        raise RuntimeError("claude exited 1: Invalid API key")

    monkeypatch.setattr(understand, "_call", broken)
    asyncio.run(understand.read([{"id": "teams:x", "new": ["hi"], "so_far": ""}]))
    assert "Invalid API key" in _last_understand()


def test_an_unreadable_reply_is_named_too(monkeypatch):
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "haiku")

    async def chatty(_):
        return "I can't help with that."

    monkeypatch.setattr(understand, "_call", chatty)
    asyncio.run(understand.read([{"id": "teams:x", "new": ["hi"], "so_far": ""}]))
    detail = _last_understand()
    assert "unreadable reply" in detail and "can't help" in detail
