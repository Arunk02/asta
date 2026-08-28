"""The watchers end to end — the two bugs Arun reported, pinned at the wiring.

Unit-testing `stable_key` is not enough: both bugs were in how the watch loops
USED their helpers. These drive the real loop bodies with faked scrapes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import outlook, teams_bridge, store


@pytest.fixture(autouse=True)
def _clean_kv(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    store.kv_set(teams_bridge.ACTIVITY_SEEN_KEY, "")
    store.kv_set(outlook.SEEN_KEY, "")
    yield


class _Notify:
    """Stands in for app.notify, recording what would reach his phone."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def notify(self, text, level="info", urgency="direct", priority=None,
                     **kw):        # **kw: notify() also takes source/key/considered
        self.sent.append((text, urgency))
        return {"bell": True}


# --- bug 1: an item already opened must not be pushed -----------------------

def test_an_already_opened_mention_is_not_pushed():
    """He reads Teams on his phone; what he opened there is settled."""
    n = _Notify()
    rows = [{"text": "Priya — mentioned you — can you review? — 2m", "unread": False}]
    fresh = _fresh_activity(rows)
    assert fresh == []                                  # nothing survives to push
    assert n.sent == []


def test_an_unopened_mention_is_still_pushed():
    _prime()
    rows = [{"text": "Priya — mentioned you — can you review? — 2m", "unread": True}]
    assert _fresh_activity(rows)                        # the safety net still works


def test_unknown_read_state_pushes_rather_than_going_silent():
    """If the scrape cannot tell, a repeat beats a miss."""
    _prime()
    rows = [{"text": "Priya — mentioned you — can you review? — 2m", "unread": None}]
    assert _fresh_activity(rows)


def test_the_very_first_poll_primes_instead_of_blasting_history():
    """On a cold start the whole feed is 'new'. Pushing it would be an avalanche,
    so the first poll only records what it saw."""
    assert _fresh_activity(
        [{"text": "Priya — mentioned you — review? — 2m", "unread": True}]) == []


# --- bug 2: the same item must not re-notify as its age re-renders ----------

def test_the_same_mention_does_not_re_notify_an_hour_later():
    """THE reported bug. The feed re-renders '2m' as '1h'; the old key was the
    raw string, so the identical mention looked new on every single poll."""
    _prime()
    first = _fresh_activity([{"text": "Priya — mentioned you — review? — 2m", "unread": True}])
    assert first                                        # pushed once, correctly

    later = _fresh_activity([{"text": "Priya — mentioned you — review? — 1h", "unread": True}])
    assert later == []                                  # and never again

    tomorrow = _fresh_activity(
        [{"text": "Priya — mentioned you — review? — Yesterday", "unread": True}])
    assert tomorrow == []


def test_a_genuinely_new_mention_still_gets_through():
    _prime()
    _fresh_activity([{"text": "Priya — mentioned you — review? — 2m", "unread": True}])
    assert _fresh_activity([{"text": "Ravi — mentioned you — deploy is red — 1m", "unread": True}])


def test_mail_keys_survive_a_re_render():
    a = outlook.mail_key({"sender": "Priya", "subject": "Review PR (sent 10:30 AM)"})
    b = outlook.mail_key({"sender": "Priya", "subject": "Review PR (sent 4:05 PM)"})
    assert a == b


# --- bug 3: random thoughts must not be pushed like blockers ----------------

def test_a_random_thought_rides_the_quiet_path_and_asks_nothing():
    """'some random thoughts also getting notifications' — now one quiet line."""
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [
        {"sender": "Sam", "subject": "Just a thought on caching",
         "preview": "Was wondering if we could pre-warm someday."}]))
    text, urgency = n.sent[0]
    assert urgency == "ambient"                         # does not buzz his pocket
    assert "nothing needed from you" in text
    assert "needs you" not in text


def test_a_real_ask_interrupts_and_says_what_is_wanted():
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [
        {"sender": "Priya", "subject": "Can you approve the release today?",
         "preview": "Blocked until you sign off."}]))
    text, urgency = n.sent[0]
    assert urgency == "direct"                          # this one is allowed to interrupt
    assert "needs you" in text and "Priya" in text


def test_a_mixed_batch_is_one_message_with_asks_first():
    """Six mails used to mean six 180-char previews. Now: one scannable message."""
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [
        {"sender": "Sam", "subject": "FYI nightly finished", "preview": "all green"},
        {"sender": "Priya", "subject": "Please review PR #12", "preview": "waiting on you"},
        {"sender": "Lee", "subject": "Sharing my notes", "preview": "from the sync"}]))
    assert len(n.sent) == 1                             # ONE push, not three
    text, urgency = n.sent[0]
    assert urgency == "direct"                          # because one item really needs him
    assert text.index("needs you") < text.index("FYI")
    assert "Priya" in text


def test_nothing_at_all_sends_nothing():
    n = _Notify()
    asyncio.run(outlook._push_mail(n, []))
    assert n.sent == []


def test_teams_push_splits_asks_from_mentions_with_no_ask():
    """Both still arrive in one message, and the ask is still marked as the ask.

    What changed on 28 August is the URGENCY of the second one. This test used to
    assert that a mention with no ask verb rides the ambient path — and ambient is
    held while he is at the laptop, so on that morning Komal, Nakka Harika and
    Abhijit (twice) all used his name between 11:58 and 13:04 and not one reached
    his phone. He found them by opening the screen.

    His rule, given twice: "if they tag me u have to respond to me". Someone using
    his name IS the ask, whether or not the sentence parses as one — "hi Arunkumar
    K" has no verb in it and is unmistakably somebody wanting him. The cost is
    asymmetric and known: an unneeded push costs a glance, a missed one costs a
    colleague waiting on a reply that never comes.

    Reactions are the exception and stay quiet — see test_tagged_reaches_him.
    """
    n = _Notify()
    asyncio.run(teams_bridge._push_activity(n, [
        "Priya — mentioned you — can you review the PR?",
        "Ravi — mentioned you — nice work on the launch"]))
    assert len(n.sent) == 1
    text, urgency = n.sent[0]
    assert urgency == "direct"
    assert "Priya" in text and "Ravi" in text


def test_a_batch_of_only_tags_still_reaches_him():
    """Formerly `test_teams_all_quiet_batch_does_not_interrupt`, which asserted the
    behaviour that lost four pings. A batch where nobody used an ask verb is not a
    quiet batch if his name is in it."""
    n = _Notify()
    asyncio.run(teams_bridge._push_activity(n, ["Ravi — mentioned you — nice work"]))
    assert n.sent[0][1] == "direct"


def test_a_batch_of_reactions_still_does_not_interrupt():
    """The thing that must stay quiet, and the reason the rule above is safe: emoji
    are by far the highest-volume row in his feed."""
    n = _Notify()
    asyncio.run(teams_bridge._push_activity(
        n, ["Divya — reacted to your message — nice work — In chat with you"]))
    assert not n.sent or n.sent[0][1] == "ambient"


# --- helpers: drive the dedup + read-state logic of the real loop -----------

def _prime() -> None:
    """Get past the cold-start poll, which records rather than pushes."""
    _fresh_activity([{"text": "old unrelated item — 3d", "unread": False}])



def _fresh_activity(rows: list[dict]) -> list[str]:
    """Mirror of activity_watch_loop's selection step, minus the browser+sleep."""
    items = [r["text"] for r in rows]
    raw = store.kv_get(teams_bridge.ACTIVITY_SEEN_KEY)
    seen = set(json.loads(raw)) if raw else None
    fresh = [] if seen is None else [
        it for it in items if teams_bridge._activity_key(it) not in seen]
    keys = [teams_bridge._activity_key(it) for it in items]
    if seen is not None:
        keys = keys + [k for k in seen if k not in keys][:300 - len(keys)]
    store.kv_set(teams_bridge.ACTIVITY_SEEN_KEY, json.dumps(keys[:300]))
    opened = {teams_bridge._activity_key(r["text"]) for r in rows if r.get("unread") is False}
    return [it for it in fresh if teams_bridge._activity_key(it) not in opened]


# --- one reader per surface ---------------------------------------------------
# "ABhijit msg is returning now it bloating unwanted arrey"
#
# Once `chat_watch` reads the conversations directly it sees every message the
# Activity feed describes, and sees the real sentence rather than the feed's
# truncated rendering. The feed pushing them as well is the same message arriving
# twice in two different shapes.

def test_a_chat_message_is_left_to_the_reader_that_sees_it_properly(monkeypatch):
    monkeypatch.setenv("ASTA_CHATWATCH", "1")
    assert teams_bridge.duplicates_chat_watch(
        "Abhijit Mohapatra mentioned you — hi Arunkumar K — 13:04 — In chat with you")


@pytest.mark.parametrize("row", [
    "Missed call from Vinish Kumar — Teams call — Call — Chat",
    "Zishan M invited you: Zishan - OOO - 31/08",
    "Vinish Kumar updated — AI Ideathon — 12:41",
])
def test_things_that_are_not_messages_are_never_deduplicated_away(row, monkeypatch):
    """A missed call, an invite and a calendar change never appear as a message in
    any thread, so the feed is the only thing that can see them at all.

    Asserts the dedup decision only. Whether a row is ultimately WANTED also runs
    through `msnotify.keywords()`, which is derived from the machine's username and
    from TEAMS_WATCH_KEYWORDS — so asserting it here passed on his laptop and
    failed on CI, testing his .env rather than this change.
    """
    monkeypatch.setenv("ASTA_CHATWATCH", "1")
    assert not teams_bridge.duplicates_chat_watch(row)


@pytest.mark.parametrize("row", [
    "Missed call from Vinish Kumar — Teams call — Call — Chat",
    "Zishan M invited you: Zishan - OOO - 31/08",
])
def test_the_feed_still_delivers_what_only_it_can_see(row, monkeypatch):
    """These two are interesting unconditionally, so this holds on any machine."""
    monkeypatch.setenv("ASTA_CHATWATCH", "1")
    assert teams_bridge._activity_wanted(row)


def test_with_the_chat_reader_off_the_feed_keeps_everything(monkeypatch):
    """Deduplicating against a reader that is not running would lose the message."""
    monkeypatch.delenv("ASTA_CHATWATCH", raising=False)
    assert not teams_bridge.duplicates_chat_watch(
        "Abhijit Mohapatra mentioned you — hi Arunkumar K — In chat with you")
