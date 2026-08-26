"""Every flag on at once — the configuration nobody tested until now.

Each stage was built and proved in isolation, which is exactly how a system
passes six test files and still misbehaves the day someone enables all six. The
stages compose: ranking reads the ledger, the sender prior re-ranks what ranking
decided, delivery reads the rank, and the feedback loop scores what delivery did.
Every one of those is an interaction, and an interaction is not covered by either
side's own tests.

Also here: the two things that only break on a machine with history — a schema
migration, and the flags being turned off again after a week of use.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3

import pytest

from app import attention, contacts, delivery, notify, outlook, store, teams_bridge


@pytest.fixture(autouse=True)
def _quiet_local_model(monkeypatch):
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)


@pytest.fixture
def everything(monkeypatch):
    for flag in ("ASTA_ATTENTION", "ASTA_CONTACTS", "ASTA_DELIVERY", "ASTA_MEET2"):
        monkeypatch.setenv(flag, "1")
    monkeypatch.setenv("ASTA_QUIET_HOURS", "22:00-07:00")


@pytest.fixture
def nothing(monkeypatch):
    for flag in ("ASTA_ATTENTION", "ASTA_CONTACTS", "ASTA_DELIVERY", "ASTA_MEET2",
                 "ASTA_QUIET_HOURS"):
        monkeypatch.delenv(flag, raising=False)


@pytest.fixture(autouse=True)
def _no_channels(monkeypatch):
    sent: list[str] = []

    async def _wa(text):
        sent.append(text)
        return True

    async def _tg(text):
        return True

    monkeypatch.setattr(notify, "wa_send", _wa)
    monkeypatch.setattr(notify.telegram, "send", _tg)
    return sent


def _mail(sender, subject, preview=""):
    return {"unread": True, "important": False, "sender": sender,
            "subject": subject, "when": "9:00 AM", "preview": preview}


def _push(mails):
    asyncio.run(outlook._push_mail(notify, mails))


# --- the whole pipeline, everything on ------------------------------------------------

def test_a_days_traffic_sorts_itself_with_every_stage_live(everything, monkeypatch,
                                                           _no_channels):
    """The end-to-end claim, exercised through the real push path: one outage,
    one deadline ask, one nudge and one newsletter arrive together and come out
    ranked rather than as four identical lines."""
    monkeypatch.setattr(delivery, "should_batch", lambda *a, **k: False)
    monkeypatch.setattr(delivery, "quiet_now", lambda now=None: False)
    monkeypatch.setattr(delivery, "hold_for_quiet", lambda *a, **k: False)

    _push([
        _mail("monitoring", "Error Reporting :: Booking", "connection refused, pods are down"),
        _mail("Sam", "release sign-off", "can you approve this? we ship tonight, asap"),
        _mail("Priya", "runbook", "any update when you get a sec?"),
        _mail("news@vendor.com", "This week in widgets", "our latest blog"),
    ])
    text = _no_channels[0]
    assert "needs you NOW" in text
    urgent = text.split("🔴")[0]
    assert "monitoring" in urgent and "Sam" in urgent
    assert "Priya" in text.split("🔴")[1]


def test_the_stages_compose_rather_than_fight(everything, monkeypatch, _no_channels):
    """Ranking says FYI, the sender prior lifts it, delivery sees the lifted rank.
    Each stage's own tests pass with the others absent; this is the seam."""
    monkeypatch.setattr(delivery, "should_batch", lambda *a, **k: False)
    monkeypatch.setattr(delivery, "quiet_now", lambda now=None: False)
    monkeypatch.setattr(delivery, "hold_for_quiet", lambda *a, **k: False)
    for _ in range(10):
        store.contact_bump("boss@x.com", "engaged")

    _push([_mail("boss@x.com", "thoughts on the roadmap")])
    assert "needs you" in _no_channels[0]


def test_an_outage_still_wakes_him_with_every_guard_engaged(everything, monkeypatch,
                                                            _no_channels):
    """Quiet hours, batching and the sender prior all switched on and all pointing
    the wrong way — breakage must still get through every one of them."""
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    delivery.note_sent()                       # something went out a moment ago
    for _ in range(30):
        store.contact_bump("monitoring", "ignored")   # a sender he never reads

    _push([_mail("monitoring", "prod alert", "pods are down, connection refused")])
    assert _no_channels and "NOW" in _no_channels[0]


def test_an_l2_ticket_at_2am_waits_for_morning(everything, monkeypatch, _no_channels):
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    _push([_mail("ServiceNow", "INC9001 assigned to your group",
                 "please review this incident")])
    assert _no_channels == []
    assert notify._held_items()                 # not lost — waiting


def test_what_waited_overnight_arrives_in_the_morning(everything, monkeypatch,
                                                      _no_channels):
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    _push([_mail("ServiceNow", "INC9001 assigned to your group", "please review")])
    assert _no_channels == []

    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: False)
    asyncio.run(notify.flush_held())
    assert _no_channels and "INC9001" in _no_channels[0]


def test_one_incident_on_two_channels_is_one_interruption(everything, monkeypatch,
                                                          _no_channels):
    monkeypatch.setattr(delivery, "should_batch", lambda *a, **k: False)
    monkeypatch.setattr(delivery, "quiet_now", lambda now=None: False)
    monkeypatch.setattr(delivery, "hold_for_quiet", lambda *a, **k: False)
    _push([_mail("ServiceNow", "INC4471 booking service down")])
    asyncio.run(teams_bridge._push_activity(
        notify, ["Priya — mentioned you: INC4471 still down, can you look?"]))
    assert len(_no_channels) == 1


def test_the_loop_closes_engagement_becomes_reputation(everything, monkeypatch,
                                                       _no_channels):
    """Stage D's label feeding Stage C's prior — the loop the whole design rests
    on, driven through the real push path rather than by calling record()."""
    monkeypatch.setattr(delivery, "should_batch", lambda *a, **k: False)
    monkeypatch.setattr(delivery, "quiet_now", lambda now=None: False)
    monkeypatch.setattr(delivery, "hold_for_quiet", lambda *a, **k: False)
    for i in range(5):
        _push([_mail("sam@x.com", f"question {i}", "can you take a look?")])
        attention.mark_acted(attention.key_for("sam@x.com", f"question {i}"))
    assert contacts.signal_rate("sam@x.com") == 1.0
    assert contacts.adjust(attention.P_FYI, "sam@x.com")[0] == attention.P_TODAY


# --- the guards that only matter when everything is on ---------------------------------

def test_a_chase_does_not_arrive_at_two_in_the_morning(everything, monkeypatch):
    """The chase loop runs hourly past end of day, so an unranked direct push
    would fire at 2am about something that had already waited eight hours."""
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    assert delivery.hold_for_quiet("direct", attention.P_TODAY) is True


def test_the_batch_flush_respects_the_night(everything, monkeypatch):
    """`deliver` sits below the policy, so the flush loop has to apply the guard
    itself or a buffered item escapes quiet hours through the back door."""
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    assert delivery.quiet_now() is True


def test_the_night_guard_is_inert_when_delivery_is_switched_off(monkeypatch):
    """ASTA_QUIET_HOURS set without ASTA_DELIVERY must not make flush_held refuse
    to run — notify would have already decided to deliver, and the held batch
    would be reported as sent while going nowhere."""
    monkeypatch.delenv("ASTA_DELIVERY", raising=False)
    monkeypatch.setenv("ASTA_QUIET_HOURS", "22:00-07:00")
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    assert delivery.quiet_now() is False


# --- off again, and on a machine with history --------------------------------------------

def test_turning_everything_off_restores_the_old_behaviour_exactly(nothing, _no_channels):
    """A week of use then a change of mind. The ledger keeps its rows, and the
    watchers must go straight back to how they behaved before any of this."""
    m = _mail("Sam", "please review the deploy plan")
    _push([m])
    _push([m])
    assert len(_no_channels) == 2                       # no dedup
    assert "🔴 📧 Outlook — needs you (1):" in _no_channels[0]
    assert "NOW" not in _no_channels[0]
    assert store.attention_get(attention.key_for("Sam", "please review the deploy plan")) is None


def test_history_survives_the_flags_going_off_and_on(monkeypatch, _no_channels):
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    attention.consider("outlook", "k1", who="sam@x.com", what="approve?")
    attention.mark_acted("k1")
    monkeypatch.delenv("ASTA_ATTENTION", raising=False)
    assert store.contact_get("sam@x.com")["engaged"] == 1      # learning kept
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    assert store.attention_get("k1")["state"] == "acted"       # ledger kept


def test_a_database_predating_the_chase_column_is_migrated(tmp_path, monkeypatch):
    """The one machine with real history is the one CREATE TABLE IF NOT EXISTS
    leaves behind — it already has the table, so a plain schema edit reaches
    every install except his."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE attention (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '',
            who TEXT NOT NULL DEFAULT '', what TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 2, why TEXT NOT NULL DEFAULT '',
            due_at REAL, state TEXT NOT NULL DEFAULT 'new',
            seen_count INTEGER NOT NULL DEFAULT 1, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL, notified_at REAL, acted_at REAL);
        INSERT INTO attention (key, source, first_seen, last_seen)
            VALUES ('legacy', 'outlook', 1, 1);
    """)
    con.commit()
    con.close()

    monkeypatch.setattr(store, "DB_PATH", db)
    store.init()
    assert "chased_at" in {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(attention)")}
    assert store.attention_get("legacy") is not None           # the row survived
    store.attention_set("legacy", chased_at=123.0)
    assert store.attention_get("legacy")["chased_at"] == 123.0


def test_the_new_tables_are_created_on_a_fresh_database(tmp_path, monkeypatch):
    db = tmp_path / "fresh.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init()
    tables = {r[0] for r in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"attention", "contacts"} <= tables
