"""Ranking, not a flag — what is urgent vs what merely reads as urgent.

The old system compressed every judgement into `action: bool`, so "approve by
EOD, we ship tonight" and "any update?" rendered identically. These prove the
three signals that separate them, in the order of how objective each one is:
something provably broken, then a real clock deadline, then wording. And the
fourth signal that no single message can carry — that this is the third time
someone has asked.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

from app import attention, memory, outlook, store, teams_bridge, triage


@pytest.fixture(autouse=True)
def _quiet_local_model(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")


def _at(hour: int, minute: int = 0, day: int = 4) -> float:
    """A fixed Thursday, so weekday arithmetic is checkable rather than 'today'."""
    return dt.datetime(2026, 6, 1 + day, hour, minute).timestamp()


# --- reading a deadline out of the words -----------------------------------------

def test_no_deadline_words_means_no_deadline():
    assert attention.deadline("can you review this when you get a chance?") is None
    assert attention.deadline("") is None


@pytest.mark.parametrize("text", ["needed by EOD", "please confirm by end of day",
                                  "sign off by COB", "by close of business please"])
def test_end_of_day_lands_on_the_configured_hour(text):
    now = _at(9)
    due = attention.deadline(text, now)
    assert dt.datetime.fromtimestamp(due).hour == 18
    assert dt.datetime.fromtimestamp(due).date() == dt.datetime.fromtimestamp(now).date()


def test_the_end_of_day_hour_is_configurable(monkeypatch):
    monkeypatch.setenv("ASTA_EOD_HOUR", "17")
    assert dt.datetime.fromtimestamp(attention.deadline("by EOD", _at(9))).hour == 17


def test_asap_means_right_now():
    now = _at(9)
    assert attention.deadline("need this ASAP", now) == now
    assert attention.deadline("urgent — please look", now) == now


def test_an_explicit_clock_time_is_parsed():
    due = attention.deadline("can you get back by 3pm", _at(9))
    assert dt.datetime.fromtimestamp(due).hour == 15


def test_a_deadline_already_past_stays_past_rather_than_rolling_to_tomorrow():
    """Read at 4pm, 'by 3pm' means he is LATE. Quietly reinterpreting it as
    tomorrow would hide the single most urgent state there is."""
    now = _at(16)
    due = attention.deadline("by 3pm please", now)
    assert due < now
    assert dt.datetime.fromtimestamp(due).date() == dt.datetime.fromtimestamp(now).date()


def test_tomorrow_is_tomorrow():
    due = attention.deadline("first thing tomorrow", _at(9))
    assert dt.datetime.fromtimestamp(due).day == dt.datetime.fromtimestamp(_at(9)).day + 1


def test_by_a_weekday_finds_the_next_one():
    thursday = _at(9, day=4)          # 2026-06-05 is a Friday; day=4 → Thursday
    due = attention.deadline("by Monday please", thursday)
    assert dt.datetime.fromtimestamp(due).weekday() == 0
    assert due > thursday


def test_the_earliest_deadline_in_the_message_wins():
    """'by Friday' with an 'asap' in the same breath is an asap."""
    now = _at(9)
    assert attention.deadline("need it asap, definitely by Friday", now) == now


# --- ranking --------------------------------------------------------------------

def test_something_broken_outranks_everything_including_wording():
    pri, why, _ = attention.score(False, "pods are down in booking", critical=True)
    assert pri == attention.P_NOW and "broken" in why


def test_an_ask_due_within_hours_is_an_interrupt():
    now = _at(15)
    pri, why, due = attention.score(True, "please approve by EOD", now=now)
    assert pri == attention.P_NOW
    assert "due within hours" in why and due is not None


def test_the_same_ask_due_much_later_is_todays_work_not_an_interrupt():
    now = _at(9)
    pri, _, _ = attention.score(True, "please approve by Friday", now=now)
    assert pri == attention.P_TODAY


def test_the_urgent_window_is_configurable(monkeypatch):
    now = _at(9)
    assert attention.score(True, "approve by EOD", now=now)[0] == attention.P_TODAY
    monkeypatch.setenv("ASTA_URGENT_HOURS", "12")
    assert attention.score(True, "approve by EOD", now=now)[0] == attention.P_NOW


def test_a_plain_ask_is_todays_work():
    assert attention.score(True, "can you review the deploy plan?")[0] == attention.P_TODAY


def test_a_deadline_with_no_ask_does_not_manufacture_urgency():
    """A newsletter saying 'offer ends today' is not someone waiting on him."""
    pri, _, due = attention.score(False, "sale ends today!", now=_at(9))
    assert pri == attention.P_FYI
    assert due is not None          # the time is still recorded, just not acted on


def test_pure_fyi_stays_fyi():
    assert attention.score(False, "notes from the sync")[0] == attention.P_FYI


# --- the signal a single message cannot carry -------------------------------------

def test_a_third_chase_escalates_what_a_first_ask_could_not(on):
    """Someone asking a third time outranks anything in the wording of the first,
    and it is invisible without the ledger — that is why ranking needed one."""
    key = "k1"
    for _ in range(2):
        store.attention_upsert(key, "outlook", what="any update?", priority=attention.P_TODAY)
    pri, note = attention.escalate_for_chase(attention.P_TODAY, key)
    assert pri == attention.P_NOW and "chased 3" in note


def test_a_first_and_second_ask_do_not_escalate(on):
    store.attention_upsert("k1", "outlook", what="any update?")
    pri, note = attention.escalate_for_chase(attention.P_TODAY, "k1")
    assert pri == attention.P_TODAY and note == ""


def test_the_chase_threshold_is_configurable(on, monkeypatch):
    store.attention_upsert("k1", "outlook", what="any update?")
    monkeypatch.setenv("ASTA_CHASE_AT", "2")
    assert attention.escalate_for_chase(attention.P_TODAY, "k1")[0] == attention.P_NOW


def test_something_he_already_dealt_with_is_not_a_chase(on):
    """Repeats after he acted are a thank-you thread, not someone waiting."""
    for _ in range(5):
        store.attention_upsert("k1", "outlook", what="thanks!")
    attention.mark_acted("k1")
    assert attention.escalate_for_chase(attention.P_TODAY, "k1")[0] == attention.P_TODAY


def test_escalation_cannot_climb_past_the_top(on):
    for _ in range(9):
        store.attention_upsert("k1", "outlook", what="still down")
    assert attention.escalate_for_chase(attention.P_NOW, "k1")[0] == attention.P_NOW


# --- rendering: the new tier appears only when something earned it -----------------

def test_an_unranked_batch_renders_exactly_as_it_always_did():
    """The off-switch has to be real: unranked verdicts must produce the old text
    byte for byte, or turning the ledger off would still change what he reads."""
    verdicts = [triage.Verdict(True, "asks", "Sam: approve?"),
                triage.Verdict(False, "fyi", "Priya: notes")]
    text, needs = triage.summarize(verdicts, "📧 Outlook")
    assert "needs you NOW" not in text
    assert "🔴 📧 Outlook — needs you (1):" in text
    assert needs is True


def test_a_ranked_urgent_item_gets_its_own_tier():
    verdicts = [triage.Verdict(True, "asks", "Sam: approve?").ranked(attention.P_NOW),
                triage.Verdict(True, "asks", "Priya: review?").ranked(attention.P_TODAY)]
    text, needs = triage.summarize(verdicts, "📧 Outlook")
    assert "🚨 📧 Outlook — needs you NOW (1):" in text
    assert "Sam: approve?" in text.split("🔴")[0]      # urgent listed above the rest
    assert "🔴 📧 Outlook — needs you (1):" in text
    assert needs is True


def test_a_batch_of_nothing_but_urgent_still_counts_as_needing_him():
    """The bug this catches: splitting the urgent tier out of `acts` made a
    P0-only batch report 'nothing needed' and ride the quiet ambient path."""
    verdicts = [triage.Verdict(True, "asks", "Sam: prod is down").ranked(attention.P_NOW)]
    text, needs = triage.summarize(verdicts, "📧 Outlook")
    assert needs is True
    assert "NOW" in text


def test_something_broken_reaches_the_urgent_tier_though_nobody_asked_anything():
    """The bug this catches: the tiers were split on `action` before priority, and
    nobody ASKS anything when prod falls over — so a critical alert (action=False)
    was filed under 'FYI, nothing needed from you'. Rank has to win over wording."""
    v = triage.Verdict(False, "no ask detected", "monitoring: pods are down")
    text, needs = triage.summarize([v.ranked(attention.P_NOW, "something is broken")], "📧 Outlook")
    assert "needs you NOW" in text
    assert "nothing needed from you" not in text
    assert needs is True


def test_a_promoted_item_leaves_the_fyi_pile_it_was_promoted_out_of():
    """The mirror of the bug above, one tier down: the ask/FYI split was still
    made on `action`, so something ranked up by the sender prior stayed filed
    under FYI — the exact pile the promotion existed to lift it out of."""
    v = triage.Verdict(False, "no ask detected", "boss: thoughts on the roadmap")
    text, needs = triage.summarize([v.ranked(attention.P_TODAY, "you usually act")], "📧 Outlook")
    assert "🔴 📧 Outlook — needs you (1):" in text
    assert "nothing needed from you" not in text
    assert needs is True


def test_render_marks_an_urgent_line_distinctly():
    assert triage.Verdict(True, "w", "x").ranked(attention.P_NOW).render().startswith("🚨")
    assert triage.Verdict(True, "w", "x").render().startswith("🔴")
    assert triage.Verdict(False, "w", "x").render().startswith("·")


def test_ranking_returns_a_copy_and_never_mutates(monkeypatch):
    v = triage.Verdict(True, "asks", "Sam: approve?")
    r = v.ranked(attention.P_NOW, "due within hours", 123.0)
    assert v.priority is None and v.why == "asks"      # original untouched
    assert r.priority == attention.P_NOW and r.due_at == 123.0


# --- end to end through the real watchers ------------------------------------------

class _Notify:
    def __init__(self):
        self.sent = []

    async def notify(self, text, level="info", urgency="direct", priority=None):
        self.sent.append((text, urgency))
        return {"bell": True}


def _mail(sender, subject, preview=""):
    return {"unread": True, "important": False, "sender": sender,
            "subject": subject, "when": "9:00 AM", "preview": preview}


def test_a_deadline_ask_and_a_vague_nudge_no_longer_look_the_same(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [
        _mail("Sam", "please approve the release", "we ship tonight, need it asap"),
        _mail("Priya", "quick one", "any update on the runbook when you get a sec?"),
    ]))
    text = n.sent[0][0]
    assert "needs you NOW" in text and "Sam" in text.split("🔴")[0]
    assert "Priya" in text.split("🔴")[1]


def test_a_broken_service_is_ranked_now_even_with_calm_wording(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [
        _mail("monitoring", "Error Reporting :: Booking", "connection refused, pods are down")]))
    assert "NOW" in n.sent[0][0]
    assert store.attention_get(
        attention.key_for("monitoring", "Error Reporting :: Booking"))["priority"] == 0


def test_the_ledger_stores_the_deadline_it_parsed(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "approve by 3pm please")]))
    row = store.attention_get(attention.key_for("Sam", "approve by 3pm please"))
    assert row["due_at"] is not None
    assert dt.datetime.fromtimestamp(row["due_at"]).hour == 15


def test_with_the_ledger_off_nothing_is_ranked_and_the_message_is_the_old_one(monkeypatch):
    monkeypatch.delenv("ASTA_ATTENTION", raising=False)
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [
        _mail("Sam", "please approve the release", "we ship tonight, need it asap")]))
    assert "NOW" not in n.sent[0][0]
    assert "🔴 📧 Outlook — needs you (1):" in n.sent[0][0]


def test_a_teams_mention_with_a_deadline_ranks_urgent(on):
    n = _Notify()
    asyncio.run(teams_bridge._push_activity(
        n, ["Priya — mentioned you: can you approve the release asap? blocked on you"]))
    assert "NOW" in n.sent[0][0]
