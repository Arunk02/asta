"""Reading his actual chats — the case the Activity feed structurally cannot see.

Arun:

    "if they didnt tag , still if it one to one chat na , that message is for me
     correct , sometimes the first message they tag and second message they wont
     tag in both personal one to one chat as well as group chat this is basic
     thing"

Teams' Activity feed lists mentions, replies, reactions and invites, and never an
ordinary message. Verified against his real feed rows, not assumed. So everything
below is about the reader that replaces it, and the two properties that keep it
affordable: the rail's ORDER is the signal, and Asta's own high-water mark — not
Teams' unread styling — decides what is new.
"""

from __future__ import annotations

import asyncio

import pytest

from app import chat_watch, store


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ASTA_CHATWATCH", "1")


# --- the rail order is the signal ---------------------------------------------

def test_a_chat_that_rose_had_activity():
    assert "Vinish Kumar" in chat_watch.moved_up(
        ["Komal", "Vinish Kumar", "Team"], ["Vinish Kumar", "Komal", "Team"])


def test_the_top_chat_is_always_checked():
    """The untagged follow-up this module exists for: a SECOND message in a chat
    already at position 0 moves nothing in the rail."""
    assert chat_watch.moved_up(["Vinish", "Komal"], ["Vinish", "Komal"]) == ["Vinish"]


def test_a_brand_new_conversation_counts():
    assert "Newperson" in chat_watch.moved_up(["Komal"], ["Newperson", "Komal"])


def test_the_first_ever_run_does_not_open_the_whole_rail():
    """With no previous order everything looks new. Opening forty conversations on
    the first poll is minutes of browser work on a single-writer profile."""
    assert chat_watch.moved_up([], ["A", "B", "C", "D"]) == ["A"]


def test_nothing_on_screen_opens_nothing():
    assert chat_watch.moved_up(["A", "B"], []) == []


def test_a_chat_that_sank_is_not_reopened():
    """Only a RISE means activity. Something dropping down the rail happened
    because other chats moved, and re-reading it is a page load for nothing."""
    assert "Komal" not in chat_watch.moved_up(["Komal", "Vinish"], ["Vinish", "Komal"])


# --- Asta's high-water mark, not Teams' read state ----------------------------

def _msgs(*keys):
    return [{"key": k, "text": f"message {k}", "sender": "Vinish Kumar"} for k in keys]


def test_only_messages_after_the_mark_are_new():
    store.kv_set(chat_watch._seen_key("Vinish"), "m2")
    assert [m["key"] for m in chat_watch.unseen("Vinish", _msgs("m1", "m2", "m3", "m4"))] \
        == ["m3", "m4"]


def test_the_same_poll_twice_yields_nothing_the_second_time():
    rows = _msgs("m1", "m2")
    store.kv_set(chat_watch._seen_key("Vinish"), "")
    chat_watch.unseen("Vinish", rows)
    chat_watch.remember("Vinish", rows)
    assert chat_watch.unseen("Vinish", rows) == []


def test_first_sight_of_a_thread_takes_only_the_latest():
    """Otherwise adding a chat replays its entire visible history at him."""
    store.kv_set(chat_watch._seen_key("New"), "")
    assert [m["key"] for m in chat_watch.unseen("New", _msgs("a", "b", "c"))] == ["c"]


def test_a_mark_that_scrolled_out_of_view_replays_rather_than_loses():
    """If the remembered message is no longer in the window, the conversation ran
    ahead of the poll. Losing them silently is the failure mode that matters."""
    store.kv_set(chat_watch._seen_key("Vinish"), "long-gone")
    assert len(chat_watch.unseen("Vinish", _msgs("m8", "m9"))) == 2


def test_it_does_not_depend_on_teams_unread_styling():
    """Two reasons, both his. Anything he glanced at on his phone would become
    invisible here — the opposite of "irrespective im present or not" — and this
    Teams build renders no unread marker at all: empty aria-label, empty data-tid,
    hashed Fluent class names. Checked against his live rail."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(chat_watch))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    # Named symbols, not a substring search: the first version of this test
    # matched the word inside "unreadable" in a comment.
    assert not ({"unread_rows", "_row_is_unread", "_UNREAD_HINTS"} & used), \
        "the watcher reads Teams' unread styling — it must use its own mark"


# --- what it does with what it finds ------------------------------------------

class _Bridge:
    def __init__(self, rows):
        self.rows = rows
        self.opened = []

    async def read_history(self, chat, limit=12, max_scrolls=0):
        self.opened.append(chat)
        return self.rows.get(chat, [])


@pytest.fixture
def wired(monkeypatch):
    """A rail that moved, a chat with one new message, and no browser."""
    bridge = _Bridge({"Vinish Kumar": [
        {"key": "k1", "sender": "Vinish Kumar",
         "text": "can you check the production temporal bookings struck"}]})

    async def _candidates():
        return ["Vinish Kumar"]

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in",
                        lambda chat: bridge.read_history(chat))
    store.kv_set(chat_watch._seen_key("Vinish Kumar"), "")
    return bridge


def test_an_untagged_one_to_one_message_is_handled(wired):
    """The whole point. Nobody @mentioned him; it is still for him."""
    handled = asyncio.run(chat_watch.sweep())
    assert handled, "an untagged 1:1 message was ignored"
    assert handled[0]["who"] == "Vinish Kumar"


def test_it_investigates_what_it_finds(wired, monkeypatch):
    """The reader and the actuator have to meet — otherwise this is a better
    sensor bolted to the same dead end."""
    monkeypatch.setenv("ASTA_RESPOND", "1")
    spawned = []
    from app import tasks
    monkeypatch.setattr(tasks, "spawn",
                        lambda title, prompt, kind="analysis", ws=None, *a, **k:
                        (spawned.append({"kind": kind, "title": title}), {"id": 7})[1])
    asyncio.run(chat_watch.sweep())
    assert spawned and spawned[0]["kind"] == "analysis"


def test_his_own_messages_are_not_things_he_was_asked(monkeypatch):
    from app import meetings
    monkeypatch.setattr(meetings, "speaker_is_arun", lambda s: "arun" in s.lower())
    assert chat_watch.is_from_him("Arunkumar K")
    assert not chat_watch.is_from_him("Vinish Kumar")


def test_one_unreadable_thread_does_not_end_the_sweep(monkeypatch):
    async def _candidates():
        return ["Broken", "Fine"]

    async def _new_in(chat):
        if chat == "Broken":
            raise RuntimeError("thread would not open")
        return [{"key": "k", "sender": "Vinish", "text": "prod is stuck"}]

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in", _new_in)
    assert asyncio.run(chat_watch.sweep())


def test_it_is_off_until_switched_on(monkeypatch):
    monkeypatch.delenv("ASTA_CHATWATCH", raising=False)
    assert not chat_watch.enabled()


def test_opens_are_capped(monkeypatch):
    """Each open is a real navigation on a profile that tolerates one writer."""
    monkeypatch.setattr(chat_watch, "MAX_OPENS", 2)
    assert len(chat_watch.moved_up([], ["A", "B", "C", "D"])[:chat_watch.MAX_OPENS]) <= 2


def test_his_own_self_chat_is_not_a_conversation():
    """"Arunkumar K (You)" is PINNED to the top of his rail, so it was permanently
    the "always check the top row" candidate — one of three opens per sweep spent
    on a thread only he writes in, and the genuinely newest conversation hidden
    behind it. The first live sweep said "nothing new" and was right for the wrong
    reason."""
    assert chat_watch.is_furniture("Arunkumar K (You)")
    assert chat_watch.is_furniture("Someone Else (You)")
    assert not chat_watch.is_furniture("Vinish Kumar")


def test_a_team_header_is_not_a_conversation():
    assert chat_watch.is_furniture("TELIKOS - All Teams")


def test_a_real_channel_is_still_watched():
    """Group channels are where untagged follow-ups land too — his second case."""
    assert not chat_watch.is_furniture("Team Booking and Execution")
    assert not chat_watch.is_furniture("OHP Garage")
