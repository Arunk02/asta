"""Who matters, learned rather than listed.

`_BULK_SENDER` works and does not scale: every new newsletter is a code change
and every important human is anonymous to it. This is the prior that regex should
have been — and the tests that matter most here are the ones proving what it
REFUSES to do, because a learned filter that can silence the wrong thing is worse
than no filter at all.
"""

from __future__ import annotations

import asyncio

import pytest

from app import attention, contacts, memory, outlook, store


@pytest.fixture(autouse=True)
def _quiet_local_model(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    monkeypatch.setenv("ASTA_CONTACTS", "1")


def _history(who: str, engaged: int = 0, ignored: int = 0, met: int = 0):
    for _ in range(engaged):
        store.contact_bump(contacts.normalise(who), "engaged")
    for _ in range(ignored):
        store.contact_bump(contacts.normalise(who), "ignored")
    for _ in range(met):
        store.contact_bump(contacts.normalise(who), "met")


# --- one person, one row -----------------------------------------------------------

def test_a_display_name_and_an_address_are_the_same_person():
    """Split history is history that never reaches the evidence bar."""
    assert contacts.normalise("Sam Patel <Sam@X.com>") == "sam@x.com"
    assert contacts.normalise("  sam@x.com ") == "sam@x.com"
    assert contacts.normalise('"Sam Patel"') == "sam patel"
    assert contacts.normalise("") == ""


def test_an_unknown_person_has_no_rate():
    assert contacts.signal_rate("nobody@x.com") is None


# --- the evidence bar ---------------------------------------------------------------

def test_a_rate_is_withheld_until_there_is_enough_to_say(on):
    _history("sam@x.com", engaged=1, ignored=1)
    assert contacts.signal_rate("sam@x.com") is None
    _history("sam@x.com", engaged=2, ignored=1)
    assert contacts.signal_rate("sam@x.com") == pytest.approx(3 / 5)


def test_one_unlucky_ignored_mail_cannot_mute_somebody(on):
    _history("sam@x.com", ignored=1)
    assert contacts.adjust(attention.P_FYI, "sam@x.com") == (attention.P_FYI, "")


def test_the_evidence_bar_is_configurable(on, monkeypatch):
    _history("sam@x.com", ignored=2)
    monkeypatch.setenv("ASTA_CONTACT_MIN_EVIDENCE", "2")
    assert contacts.signal_rate("sam@x.com") == 0.0


# --- what it does -------------------------------------------------------------------

def test_somebody_he_always_answers_gets_promoted(on):
    _history("boss@x.com", engaged=9, ignored=1)
    pri, why = contacts.adjust(attention.P_FYI, "boss@x.com")
    assert pri == attention.P_TODAY and "usually act" in why


def test_somebody_he_never_answers_gets_quieted(on):
    _history("blast@x.com", ignored=10)
    pri, why = contacts.adjust(attention.P_FYI, "blast@x.com")
    assert pri == attention.P_MUTE and "never act" in why


def test_a_nudge_is_capped_at_one_tier(on):
    """A prior is evidence, not a verdict. Two tiers would be a statistic quietly
    overruling the message itself."""
    _history("boss@x.com", engaged=10)
    assert contacts.adjust(attention.P_TODAY, "boss@x.com")[0] == attention.P_NOW
    assert contacts.adjust(attention.P_FYI, "boss@x.com")[0] == attention.P_TODAY


# --- what it REFUSES to do (the reason it is safe to switch on) ----------------------

def test_it_can_quiet_noise_but_never_silence_a_question(on):
    """Someone actually ASKING him something is never muted by a statistic,
    however bad their history."""
    _history("chatty@x.com", ignored=20)
    assert contacts.adjust(attention.P_TODAY, "chatty@x.com") == (attention.P_TODAY, "")


def test_breakage_is_never_re_ranked_by_who_reported_it(on):
    """'This sender is usually noise' is not an argument about whether prod is down."""
    _history("monitoring@x.com", ignored=50)
    assert contacts.adjust(attention.P_NOW, "monitoring@x.com") == (attention.P_NOW, "")


def test_somebody_he_sits_in_meetings_with_is_never_auto_muted(on):
    """The objective seed doing its job: a colleague is not bulk mail, whatever a
    thin stretch of ignored mail says."""
    _history("colleague@x.com", ignored=20, met=3)
    assert contacts.adjust(attention.P_FYI, "colleague@x.com") == (attention.P_FYI, "")
    assert contacts.known_human("colleague@x.com") is True


def test_disabled_it_changes_nothing(monkeypatch):
    monkeypatch.delenv("ASTA_CONTACTS", raising=False)
    _history("blast@x.com", ignored=50)
    assert contacts.adjust(attention.P_FYI, "blast@x.com") == (attention.P_FYI, "")


# --- learning from the labels the ledger already writes -------------------------------

def test_the_attention_labels_build_the_reputation(on):
    attention.consider("outlook", "k1", who="Sam <sam@x.com>", what="approve?")
    attention.mark_acted("k1")
    assert store.contact_get("sam@x.com")["engaged"] == 1


def test_reputation_is_learned_even_while_the_prior_is_switched_off(monkeypatch):
    """So flipping ASTA_CONTACTS on later starts with real history rather than an
    empty table that has to earn its evidence from zero."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    monkeypatch.delenv("ASTA_CONTACTS", raising=False)
    attention.consider("outlook", "k1", who="sam@x.com", what="approve?")
    attention.mark_acted("k1")
    assert store.contact_get("sam@x.com")["engaged"] == 1


def test_being_ignored_and_being_muted_are_both_recorded(on):
    attention.consider("outlook", "k1", who="blast@x.com", what="digest")
    store.attention_set("k1", notified_at=0)
    attention.settle_stale(days=7, now=10 * 86400)
    attention.consider("outlook", "k2", who="blast@x.com", what="digest 2")
    attention.mute("k2")
    row = store.contact_get("blast@x.com")
    assert row["ignored"] == 1 and row["muted"] == 1


def test_the_calendar_seeds_who_he_actually_meets():
    events = [{"organizer": "Priya <priya@x.com>"}, {"organizer": "Priya <priya@x.com>"},
              {"organizer": ""}]
    assert contacts.seed_from_meetings(events) == 2
    assert store.contact_get("priya@x.com")["met"] == 2


def test_the_scoreboard_shows_the_evidence_before_it_is_trusted(on):
    _history("sam@x.com", engaged=4, ignored=1)
    _history("new@x.com", engaged=1)
    board = {r["who"]: r for r in contacts.scoreboard()}
    assert board["sam@x.com"]["rate"] == 0.8 and board["sam@x.com"]["acting"] is True
    assert board["new@x.com"]["acting"] is False      # visible, not yet acted on


# --- end to end through the real watcher ---------------------------------------------

class _Notify:
    def __init__(self):
        self.sent = []

    async def notify(self, text, level="info", urgency="direct", priority=None,
                     **kw):        # **kw: notify() also takes source/key/considered
        self.sent.append((text, urgency))
        return {"bell": True}


def _mail(sender, subject, preview=""):
    return {"unread": True, "important": False, "sender": sender,
            "subject": subject, "when": "9:00 AM", "preview": preview}


def test_a_sender_he_never_reads_stops_reaching_his_phone(on):
    _history("blast@x.com", ignored=12)
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("blast@x.com", "this week in widgets")]))
    assert n.sent == []
    assert store.attention_get(
        attention.key_for("blast@x.com", "this week in widgets"))["priority"] == attention.P_MUTE


def test_the_same_sender_asking_a_real_question_still_gets_through(on):
    """The safety rule, end to end: history quiets chatter, it does not silence
    somebody who is actually waiting on him."""
    _history("blast@x.com", ignored=12)
    n = _Notify()
    asyncio.run(outlook._push_mail(
        n, [_mail("blast@x.com", "can you approve the invoice?", "we need your sign-off")]))
    assert n.sent and "needs you" in n.sent[0][0]


def test_someone_he_always_answers_is_lifted_out_of_the_fyi_pile(on):
    _history("boss@x.com", engaged=10)
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("boss@x.com", "thoughts on the roadmap")]))
    assert n.sent and "needs you" in n.sent[0][0]
