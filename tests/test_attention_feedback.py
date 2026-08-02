"""Did the interruption earn its place? — the measurement that was missing.

`quality.py` scored plans, tasks, drafts, verification and relevance, and not the
one thing that actually buzzes his pocket. So "it pushes too much" could only be
argued about, never checked.

The labels are free because Arun already produces them: he deals with a thing or
he does not. Nothing here CHANGES what Asta pushes — measure first, act on the
data later, the same order the relevance gate was built in. These tests hold that
line: they assert the numbers are recorded, and that no suppression happens.
"""

from __future__ import annotations

import asyncio

import pytest

from app import attention, memory, outlook, quality, store, teams_bridge


@pytest.fixture(autouse=True)
def _quiet_local_model(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")


def _outcomes(kind: str = "attention") -> list[dict]:
    return [r for r in store.recent_outcomes(100) if r["kind"] == kind]


# --- the labels ------------------------------------------------------------------

def test_dealing_with_something_scores_the_interruption(on):
    attention.consider("outlook", "k1", what="approve?", priority=attention.P_TODAY)
    attention.mark_acted("k1")
    rows = _outcomes()
    assert len(rows) == 1 and rows[0]["outcome"] == "acted"
    assert "p1" in rows[0]["detail"]


def test_reading_it_elsewhere_counts_as_engagement(on):
    """He handled it on his phone. The thing mattered; only who handled it differs."""
    attention.consider("outlook", "k1", what="approve?")
    attention.note_read("k1")
    assert _outcomes()[0]["outcome"] == "read_elsewhere"


def test_something_never_dealt_with_is_labelled_noise(on):
    attention.consider("outlook", "k1", what="newsletter-ish thing")
    store.attention_set("k1", notified_at=0)          # announced long ago
    assert attention.settle_stale(days=7, now=10 * 86400) == 1
    assert _outcomes()[0]["outcome"] == "ignored"


def test_the_stale_window_is_generous_enough_to_survive_a_holiday(on):
    """Six days of silence is 'he was busy', not 'Asta was wrong'."""
    attention.consider("outlook", "k1", what="approve?")
    store.attention_set("k1", notified_at=5 * 86400)
    assert attention.settle_stale(days=7, now=10 * 86400) == 0
    assert _outcomes() == []


def test_a_thing_settled_before_it_was_ever_announced_scores_nothing(on):
    """Only a telling can be judged. Something muted or dropped before it went out
    says nothing about whether the filter was right."""
    store.attention_upsert("k1", "outlook", what="held back", priority=attention.P_MUTE)
    attention.mark_acted("k1")
    assert _outcomes() == []


def test_a_recovered_alert_is_not_counted_against_the_filter(on):
    """It was reported correctly and then the world moved on. Scoring that as
    noise would punish the filter for doing its job."""
    attention.consider("outlook", "k1", what="disk 90%", priority=attention.P_NOW)
    attention.mark_dropped("k1")
    assert _outcomes() == []


def test_an_explicit_mute_is_recorded_rather_than_silently_obeyed(on):
    """'Why didn't you tell me' needs an answer a silent drop cannot give."""
    attention.consider("outlook", "k1", what="daily digest")
    attention.mute("k1")
    assert _outcomes()[0]["outcome"] == "muted"
    assert store.attention_get("k1")["priority"] == attention.P_MUTE


def test_a_label_is_written_once_not_on_every_later_poll(on):
    attention.consider("outlook", "k1", what="approve?")
    attention.mark_acted("k1")
    attention.mark_acted("k1")
    attention.note_read("k1")
    assert len(_outcomes()) == 1


# --- reading the numbers ----------------------------------------------------------

def test_precision_is_reported_per_tier_not_as_one_blurred_number(on):
    """One number cannot separate 'P0 is miscalibrated' from 'lots of FYI', and
    those want opposite fixes."""
    for i in range(4):
        attention.consider("outlook", f"now{i}", what="broken", priority=attention.P_NOW)
        attention.mark_acted(f"now{i}")
    for i in range(3):
        attention.consider("outlook", f"fyi{i}", what="chatter", priority=attention.P_FYI)
        store.attention_set(f"fyi{i}", notified_at=0)
    attention.settle_stale(days=7, now=10 * 86400)

    p = attention.precision()
    assert p["now"]["rate"] == 1.0 and p["now"]["total"] == 4
    assert p["FYI"]["rate"] == 0.0 and p["FYI"]["ignored"] == 3


def test_precision_is_empty_before_anything_has_been_measured(on):
    assert attention.precision() == {}


def test_the_quality_report_carries_the_interruption_numbers(on):
    attention.consider("outlook", "k1", what="approve?", priority=attention.P_TODAY)
    attention.mark_acted("k1")
    text = quality.report()
    assert "Interruptions" in text
    assert "today: 100% engaged (1/1)" in text


def test_the_quality_report_is_unchanged_when_nothing_was_measured():
    text = quality.report()
    assert "Interruptions" not in text


# --- measuring must not start suppressing ------------------------------------------

def test_being_ignored_before_does_not_silence_the_next_one(on):
    """The line this whole stage is built to hold: labels are recorded, and they
    change NOTHING yet. Acting on them comes after the numbers prove they can be
    trusted — the relevance gate's order, deliberately repeated."""
    for i in range(5):
        attention.consider("outlook", f"noise{i}", who="Chatty Colleague", what="hi")
        store.attention_set(f"noise{i}", notified_at=0)
    attention.settle_stale(days=7, now=10 * 86400)
    assert len(_outcomes()) == 5

    assert attention.consider("outlook", "new", who="Chatty Colleague", what="hi again") is True


# --- wired into the live watchers ---------------------------------------------------

class _Notify:
    def __init__(self):
        self.sent = []

    async def notify(self, text, level="info", urgency="direct", priority=None):
        self.sent.append((text, urgency))
        return {"bell": True}


def _mail(sender, subject, unread=True):
    return {"unread": unread, "important": False, "sender": sender,
            "subject": subject, "when": "9:00 AM", "preview": ""}


def test_a_mail_going_un_bold_is_picked_up_as_engagement(on, monkeypatch):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "please review the plan")]))
    key = attention.key_for("Sam", "please review the plan")
    assert store.attention_get(key)["state"] == "notified"

    # The next poll sees the same mail, now read. That signal was already being
    # scraped and thrown away.
    read_mail = [_mail("Sam", "please review the plan", unread=False)]
    monkeypatch.setattr(outlook, "read_mail", lambda n=20: _async(read_mail))
    monkeypatch.setattr(outlook, "needs_attention", lambda mails: [])
    asyncio.run(_one_outlook_poll())

    assert store.attention_get(key)["state"] == "acted"
    assert _outcomes()[0]["outcome"] == "read_elsewhere"


async def _async(value):
    return value


async def _one_outlook_poll():
    """One turn of watch_loop's body, without the sleep or the browser."""
    from app import attention as att
    mails = await outlook.read_mail(20)
    att.note_scrape("outlook")
    for m in mails:
        if not m.get("unread"):
            att.note_read(att.key_for(m.get("sender", ""), m.get("subject", "")))


def test_an_opened_teams_mention_is_picked_up_as_engagement(on):
    n = _Notify()
    item = "Priya — mentioned you: can you approve the release?"
    asyncio.run(teams_bridge._push_activity(n, [item]))
    key = attention.key_for(item)
    assert store.attention_get(key)["state"] == "notified"

    attention.note_read(key)                     # what the loop does for unread=False
    assert store.attention_get(key)["state"] == "acted"
    assert _outcomes()[0]["outcome"] == "read_elsewhere"


def test_the_sweeper_labels_and_purges_without_touching_live_work(on):
    attention.consider("outlook", "stale", what="a")
    attention.consider("outlook", "live", what="b")
    store.attention_set("stale", notified_at=0)
    assert attention.settle_stale(days=7, now=30 * 86400) == 1
    store.attention_set("stale", last_seen=0)
    assert attention.purge(days=14, now=30 * 86400) == 1
    assert [i["key"] for i in attention.open_items()] == ["live"]
