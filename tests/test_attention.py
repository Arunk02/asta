"""One ledger, one decision — and a heartbeat so silence can be trusted.

Three guarantees, in the order they matter:

  1. Off by default it changes NOTHING. Every watcher behaves exactly as it did
     before the module existed, because `consider` waves everything through.
  2. On, the same thing wanting Arun is announced ONCE, whichever channel carried
     it — the cross-source collision that `goes_to_hold` had to fix by hand.
  3. The freshness heartbeat is live regardless of the flag, because a watcher
     that stopped reading looks exactly like a quiet week.
"""

from __future__ import annotations

import asyncio

import pytest

from app import attention, memory, outlook, store, teams_bridge, triage


@pytest.fixture(autouse=True)
def _quiet_local_model(monkeypatch):
    """triage.refine must never reach LM Studio in a test — and must be instant."""
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")


# --- the no-op contract ---------------------------------------------------------

def test_disabled_pushes_everything_and_records_nothing(monkeypatch):
    monkeypatch.delenv("ASTA_ATTENTION", raising=False)
    assert attention.consider("outlook", "k1", who="Sam", what="ping") is True
    assert attention.consider("outlook", "k1", who="Sam", what="ping") is True
    assert store.attention_get("k1") is None       # nothing written at all


def test_an_empty_key_is_waved_through_rather_than_recorded(on):
    """A source that cannot identify its item must not create an unkeyed row that
    would then dedup against every OTHER unkeyed item."""
    assert attention.consider("outlook", "", what="no identity") is True
    assert attention.open_items() == []


# --- announce once, whatever carried it ------------------------------------------

def test_a_new_thing_is_pushed_and_recorded(on):
    assert attention.consider("outlook", "k1", who="Sam", what="Sam: approve?") is True
    row = store.attention_get("k1")
    assert row["state"] == "notified" and row["who"] == "Sam"


def test_the_same_thing_is_not_pushed_twice(on):
    attention.consider("outlook", "k1", what="approve?")
    assert attention.consider("outlook", "k1", what="approve?") is False


def test_two_sources_carrying_one_incident_announce_it_once(on):
    """The collision `goes_to_hold` was written to patch, solved generally."""
    assert attention.consider("outlook", "INC12345", what="pods down") is True
    assert attention.consider("teams", "INC12345", what="pods down") is False
    assert attention.consider("ci", "INC12345", what="pods down") is False
    assert store.attention_get("INC12345")["sources"] == "outlook,teams,ci"


def test_re_sighting_counts_the_chase_without_resetting_the_lifecycle(on):
    """seen_count climbing while state stays `notified` IS the chase signal —
    overwriting the row every poll is what would erase it."""
    attention.consider("outlook", "k1", what="any update?")
    for _ in range(3):
        attention.consider("outlook", "k1", what="any update?")
    row = store.attention_get("k1")
    assert row["seen_count"] == 4
    assert row["state"] == "notified"
    assert row["source"] == "outlook"          # first source kept


def test_a_thing_that_got_worse_is_re_ranked_up(on):
    """A warning that became an outage must not stay ranked as a warning."""
    attention.consider("outlook", "k1", what="latency high", priority=attention.P_FYI)
    attention.consider("outlook", "k1", what="pods down", priority=attention.P_NOW)
    row = store.attention_get("k1")
    assert row["priority"] == attention.P_NOW
    assert row["what"] == "pods down"           # the better description wins too


def test_a_thing_never_gets_re_ranked_down(on):
    attention.consider("outlook", "k1", what="pods down", priority=attention.P_NOW)
    attention.consider("teams", "k1", what="chatter", priority=attention.P_FYI)
    assert store.attention_get("k1")["priority"] == attention.P_NOW


# --- settled means settled --------------------------------------------------------

def test_something_he_acted_on_is_never_raised_again(on):
    attention.consider("outlook", "k1", what="approve?")
    attention.mark_acted("k1")
    assert attention.consider("teams", "k1", what="approve?") is False
    assert store.attention_get("k1")["state"] == "acted"


def test_something_dropped_is_never_raised_again(on):
    """An alert that recovered stopped mattering without him lifting a finger."""
    attention.consider("outlook", "k1", what="disk 90%")
    attention.mark_dropped("k1")
    assert attention.consider("outlook", "k1", what="disk 90%") is False


def test_a_muted_priority_is_recorded_but_never_pushed(on):
    """Suppression still leaves an audit trail — 'why didn't you tell me' has an
    answer, which a silent drop cannot give him."""
    assert attention.consider("outlook", "k1", what="newsletter",
                              priority=attention.P_MUTE) is False
    assert store.attention_get("k1") is not None


# --- what's on my plate -----------------------------------------------------------

def test_open_items_rank_urgent_first_then_oldest(on):
    attention.consider("outlook", "old-fyi", what="c", priority=attention.P_FYI, now=100)
    attention.consider("outlook", "new-now", what="a", priority=attention.P_NOW, now=300)
    attention.consider("outlook", "old-today", what="b", priority=attention.P_TODAY, now=100)
    attention.consider("outlook", "new-today", what="d", priority=attention.P_TODAY, now=400)
    assert [i["key"] for i in attention.open_items()] == [
        "new-now", "old-today", "new-today", "old-fyi"]


def test_open_items_leaves_out_what_is_settled(on):
    attention.consider("outlook", "done", what="a")
    attention.consider("outlook", "live", what="b")
    attention.mark_acted("done")
    assert [i["key"] for i in attention.open_items()] == ["live"]


def test_open_items_can_be_limited_to_the_things_that_want_something(on):
    attention.consider("outlook", "ask", what="a", priority=attention.P_TODAY)
    attention.consider("outlook", "fyi", what="b", priority=attention.P_FYI)
    keys = [i["key"] for i in attention.open_items(max_priority=attention.P_TODAY)]
    assert keys == ["ask"]


def test_purge_clears_settled_history_but_never_live_work(on):
    attention.consider("outlook", "old", what="a", now=0)
    attention.consider("outlook", "live", what="b", now=0)
    attention.mark_acted("old")
    store.attention_set("old", last_seen=0)
    store.attention_set("live", last_seen=0)
    assert attention.purge(days=14, now=100 * 86400) == 1
    assert [i["key"] for i in attention.open_items()] == ["live"]


# --- the heartbeat: silence must be explainable ------------------------------------

def test_a_source_that_never_ran_is_off_not_broken(monkeypatch):
    """Alarming about a Teams bridge he never enabled is crying wolf on day one."""
    monkeypatch.delenv("ASTA_ATTENTION", raising=False)
    assert attention.stale_sources(now=10**9) == {}


def test_a_source_that_just_read_is_healthy():
    attention.note_scrape("outlook", now=1000)
    assert attention.stale_sources(("outlook",), now=1000 + 60) == {}


def test_a_source_that_worked_and_went_quiet_is_reported(monkeypatch):
    monkeypatch.delenv("ASTA_ATTENTION", raising=False)   # heartbeat is NOT flagged
    attention.note_scrape("outlook", now=1000)
    stale = attention.stale_sources(("outlook",), now=1000 + 91 * 60)
    assert stale == {"outlook": 91}


def test_the_staleness_window_is_configurable_and_zero_disables_it(monkeypatch):
    attention.note_scrape("outlook", now=1000)
    monkeypatch.setenv("ASTA_STALE_AFTER_MINUTES", "10")
    assert attention.stale_sources(("outlook",), now=1000 + 11 * 60) == {"outlook": 11}
    monkeypatch.setenv("ASTA_STALE_AFTER_MINUTES", "0")
    assert attention.stale_sources(("outlook",), now=1000 + 999 * 60) == {}


def test_health_names_a_stale_watcher_as_a_problem(monkeypatch):
    from app import health
    attention.note_scrape("teams", now=1000)
    monkeypatch.setattr(attention, "stale_sources", lambda *a, **k: {"teams": 120})
    problems = asyncio.run(health.checks())
    assert "teams_watcher" in problems
    assert "120 min" in problems["teams_watcher"]


# --- wired into the real watchers --------------------------------------------------

class _Notify:
    """Stands in for the notify module: records what would have reached his phone."""

    def __init__(self):
        self.sent = []

    async def notify(self, text, level="info", urgency="direct", priority=None):
        self.sent.append((text, urgency))
        return {"bell": True}


def _mail(sender, subject, preview=""):
    return {"unread": True, "important": False, "sender": sender,
            "subject": subject, "when": "9:00 AM", "preview": preview}


def test_outlook_announces_one_mail_once_across_polls(on):
    n = _Notify()
    m = _mail("Sam", "please review the deploy plan")
    asyncio.run(outlook._push_mail(n, [m]))
    asyncio.run(outlook._push_mail(n, [m]))          # same mail, next poll
    assert len(n.sent) == 1


def test_outlook_with_the_ledger_off_behaves_exactly_as_before(monkeypatch):
    monkeypatch.delenv("ASTA_ATTENTION", raising=False)
    n = _Notify()
    m = _mail("Sam", "please review the deploy plan")
    asyncio.run(outlook._push_mail(n, [m]))
    asyncio.run(outlook._push_mail(n, [m]))
    assert len(n.sent) == 2                          # the old behaviour, untouched


def test_a_teams_mention_about_an_already_announced_incident_stays_quiet(on):
    """The cross-source win, end to end through BOTH real push paths.

    This is the test that caught the hole: mail keys on sender+subject and the
    Teams feed keys on its rendered row, so the same incident produced two keys
    and the unique column deduped nothing. The shared id join is what makes it
    real, and driving both live push functions is what proves it.
    """
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("ServiceNow", "INC4471 booking service down")]))
    assert len(n.sent) == 1

    asyncio.run(teams_bridge._push_activity(
        n, ["Priya — mentioned you in Platform: INC4471 is still down, can you look?"]))
    assert len(n.sent) == 1                            # still one — same incident
    assert store.attention_get("INC4471")["sources"] == "outlook,teams"


def test_things_that_merely_resemble_each_other_are_not_collapsed(on):
    """The other half of the contract: no id, no join. Two different people
    asking about 'the deploy' are two asks, and merging them would lose one."""
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "can you review the deploy plan?")]))
    asyncio.run(outlook._push_mail(n, [_mail("Priya", "can you review the deploy runbook?")]))
    assert len(n.sent) == 2


def test_a_jira_key_joins_a_mail_and_a_mention_too(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Jira", "PROJ-812 needs your sign-off")]))
    asyncio.run(teams_bridge._push_activity(n, ["Sam — mentioned you: PROJ-812 blocked on you"]))
    assert len(n.sent) == 1
    assert store.attention_get("PROJ-812") is not None


def test_key_for_prefers_a_real_id_over_the_wording_around_it():
    assert attention.key_for("ServiceNow", "INC4471 booking down") == "INC4471"
    assert attention.key_for("Sam — mentioned you: inc4471 is down") != "INC4471"  # case-real
    assert attention.key_for("Sam", "any update?") == triage.stable_key("Sam any update?")


def test_teams_announces_one_mention_once_across_polls(on):
    n = _Notify()
    item = "Priya — mentioned you in Platform: can you approve the release?"
    asyncio.run(teams_bridge._push_activity(n, [item]))
    asyncio.run(teams_bridge._push_activity(n, [item]))
    assert len(n.sent) == 1


def test_a_real_ask_still_reaches_him_directly(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "can you approve this by EOD?")]))
    assert n.sent and n.sent[0][1] == "direct"


def test_pure_fyi_still_rides_the_quiet_path(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "FYI — notes from the sync")]))
    assert n.sent and n.sent[0][1] == "ambient"
