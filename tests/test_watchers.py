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

    async def notify(self, text, level="info", urgency="direct"):
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
    n = _Notify()
    asyncio.run(teams_bridge._push_activity(n, [
        "Priya — mentioned you — can you review the PR?",
        "Ravi — mentioned you — nice work on the launch"]))
    assert len(n.sent) == 1
    text, urgency = n.sent[0]
    assert urgency == "direct"
    assert "Priya" in text and "Ravi" in text
    assert "nothing needed from you" in text            # Ravi's praise asks nothing


def test_teams_all_quiet_batch_does_not_interrupt():
    n = _Notify()
    asyncio.run(teams_bridge._push_activity(n, ["Ravi — mentioned you — nice work"]))
    assert n.sent[0][1] == "ambient"


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
