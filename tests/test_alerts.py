"""Held alerts, and telling him what actually broke.

The complaint that produced this file, verbatim: a notification that read

    🚨 Still broken after 20 min:
    • Error Reporting :: Booking side work

    "how i know what breaks?"

He can't, and that is the bug. A monitoring subject names the alert CHANNEL, not
the fault — so the escalation repeated a line he had no way to act on, and the only
thing it could prompt was going and opening Outlook, which is the interruption the
hold window exists to avoid.

The evidence was already in hand: `read_mail` pulls a body preview off the row's
aria-label. The hold ledger threw it away and kept `subject[:120]`. So these tests
are mostly about one property — what gets parked is the evidence, not the label.
"""

from __future__ import annotations

import json
import time

import pytest

from app import outlook, store


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    store.kv_set(outlook._HOLD_KEY, "")
    monkeypatch.setattr(outlook, "HOLD_MINUTES", 20)
    yield


def _mail(subject, sender="Grafana Alerting", preview="", when="09:14"):
    return {"unread": True, "important": False, "sender": sender,
            "subject": subject, "when": when, "preview": preview}


THE_ALERT = "Error Reporting :: Booking side work"
THE_BODY = ("Hi team, an alert has fired. Booking service 5xx rate 12.4% over 5m, "
            "threshold 2%. View it in Grafana.")


# --- the actual complaint ----------------------------------------------------

def test_a_released_alert_says_what_broke_not_just_where_it_was_reported():
    """The regression this file exists for."""
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, preview=THE_BODY)], now=now)
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    assert len(release) == 1
    text = outlook.fmt_alert(release[0])
    assert "5xx rate 12.4%" in text
    assert "threshold 2%" in text
    assert THE_ALERT in text                 # the label is still there, just not alone


def test_the_quoted_line_is_the_diagnostic_one_not_the_greeting():
    """"Hi team, an alert has fired" is the first sentence and says nothing. The
    sentence with a rate and a threshold in it is the one worth his attention."""
    detail = outlook.alert_detail(THE_BODY)
    assert detail.startswith("Booking service")
    assert "Hi team" not in detail


@pytest.mark.parametrize("body,want", [
    ("Something happened. Connection refused to booking-db:5432.", "Connection refused"),
    ("Alert fired | HTTP 503 from /api/bookings | View in Grafana", "503"),
    ("Service degraded. Latency p99 4200ms exceeded threshold.", "4200ms"),
    ("NullPointerException in BookingMapper.map", "NullPointerException"),
])
def test_evidence_beats_position(body, want):
    assert want in outlook.alert_detail(body)


def test_a_footer_is_never_quoted_as_the_finding():
    assert outlook.alert_detail("View it in Grafana. Unsubscribe here.") \
        in ("", "View it in Grafana.")
    assert "Unsubscribe" not in outlook.alert_detail(
        "Booking 5xx at 9%. Unsubscribe here.")


def test_an_alert_with_no_usable_body_says_so_rather_than_looking_thin():
    """Silence here reads as "there is nothing more to know". Saying the mail
    itself is empty tells him the next step is Outlook, and why."""
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, preview="")], now=now)
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    text = outlook.fmt_alert(release[0])
    assert "no detail beyond the subject" in text


def test_the_sender_and_the_age_are_stated():
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, sender="Grafana Alerting",
                                 preview=THE_BODY)], now=now)
    release, _ = outlook.triage_alerts([], now=now + 25 * 60)
    text = outlook.fmt_alert(release[0])
    assert "Grafana Alerting" in text
    assert "25 min ago" in text


# --- how often it fired ------------------------------------------------------

def test_repeats_are_counted_because_one_and_fifteen_are_different_problems():
    now = time.time()
    for i in range(4):
        outlook.triage_alerts(
            [_mail(THE_ALERT, preview=THE_BODY, when=f"09:1{i}")], now=now + i * 60)
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    assert release[0]["count"] == 4
    assert "4 mails" in outlook.fmt_alert(release[0])


def test_the_same_mail_seen_on_every_poll_is_counted_once():
    """The watcher re-reads the same inbox every 15 minutes. Counting per poll
    would turn one alert into "20 mails" and invent a flap that never happened."""
    now = time.time()
    m = _mail(THE_ALERT, preview=THE_BODY)
    for i in range(5):
        outlook.triage_alerts([m], now=now + i * 60)
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    assert release[0]["count"] == 1
    assert "1 mail" in outlook.fmt_alert(release[0])


def test_a_later_mail_supplies_detail_the_first_one_lacked():
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, preview="")], now=now)
    outlook.triage_alerts([_mail(THE_ALERT, preview="Now at 40% error rate.",
                                 when="09:20")], now=now + 300)
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    assert "40% error rate" in release[0]["detail"]


# --- the hold window still behaves -------------------------------------------

def test_a_self_healing_alert_never_reaches_him():
    """The whole reason the window exists — by the time he looks there is nothing
    to do, so he should never have been told."""
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, preview=THE_BODY)], now=now)
    _, cancelled = outlook.triage_alerts(
        [_mail(f"RESOLVED: {THE_ALERT}", preview="Back to normal.")], now=now + 300)
    assert cancelled
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    assert release == []


def test_nothing_is_released_before_the_window_passes():
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, preview=THE_BODY)], now=now)
    release, _ = outlook.triage_alerts([], now=now + 19 * 60)
    assert release == []


def test_an_alert_is_released_once_not_on_every_poll():
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, preview=THE_BODY)], now=now)
    first, _ = outlook.triage_alerts([], now=now + 21 * 60)
    second, _ = outlook.triage_alerts([], now=now + 40 * 60)
    assert len(first) == 1 and second == []


def test_a_recovery_for_something_never_held_stays_silent():
    now = time.time()
    _, cancelled = outlook.triage_alerts(
        [_mail(f"RESOLVED: {THE_ALERT}", preview="ok")], now=now)
    assert cancelled == []


def test_a_recovery_after_release_does_not_resurrect_it():
    """It was released, he saw it, then it healed. The recovery must not be
    reported as cancelling a firing he was never spared."""
    now = time.time()
    outlook.triage_alerts([_mail(THE_ALERT, preview=THE_BODY)], now=now)
    outlook.triage_alerts([], now=now + 21 * 60)
    _, cancelled = outlook.triage_alerts(
        [_mail(f"RESOLVED: {THE_ALERT}", preview="ok")], now=now + 30 * 60)
    assert cancelled == [outlook.alert_key(_mail(THE_ALERT))]


def test_the_ledger_does_not_grow_without_bound():
    now = time.time()
    for i in range(3):
        outlook.triage_alerts([_mail(f"{THE_ALERT} {i}", preview=THE_BODY)], now=now)
    outlook.triage_alerts([], now=now + 25 * 3600)
    assert json.loads(store.kv_get(outlook._HOLD_KEY) or "{}") == {}


def test_corrupt_ledger_state_never_crashes_the_watcher():
    store.kv_set(outlook._HOLD_KEY, "{not json")
    release, cancelled = outlook.triage_alerts([_mail(THE_ALERT, preview=THE_BODY)])
    assert release == [] and cancelled == []


def test_a_non_alert_mail_is_left_alone():
    now = time.time()
    outlook.triage_alerts([_mail("Lunch tomorrow?", sender="Priya")], now=now)
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    assert release == []


# --- one mail, one owner -----------------------------------------------------
#
# From the live ledger, which held `[…/telikos-booking-service] run failed` — a CI
# failure the CI watcher had already pushed with the run, the branch and the
# recovery. The hold window then re-announced it twenty minutes later as "still
# broken", with none of that. Two mechanisms, one event, and the worse report
# arrived second.

def test_a_ci_failure_mail_is_left_to_the_ci_watcher():
    m = _mail("[maersk-global/telikos-booking-service] Run failed: build",
              sender="GitHub", preview="The run failed on develop.")
    assert not outlook.goes_to_hold(m)
    now = time.time()
    outlook.triage_alerts([m], now=now)
    release, _ = outlook.triage_alerts([], now=now + 21 * 60)
    assert release == []


def test_a_servicenow_incident_is_left_to_the_attention_path():
    """It is assigned work, not a self-healing blip. needs_attention keeps it, so
    the hold window must not announce it a second time."""
    m = _mail("Incident INC9499483 assigned to your group",
              sender="ServiceNow", preview="Booking API returning 500s.")
    assert m in outlook.needs_attention([m])
    assert not outlook.goes_to_hold(m)


def test_the_two_paths_never_both_claim_a_mail():
    """The property, stated directly: nothing may be delivered twice."""
    mails = [
        _mail(THE_ALERT, preview=THE_BODY),
        _mail("Incident INC1 assigned to your group", sender="ServiceNow", preview="x"),
        _mail("[org/repo] Run failed: ci", sender="GitHub", preview="y"),
        _mail("Lunch tomorrow?", sender="Priya", preview="are you free"),
    ]
    attention = {m["subject"] for m in outlook.needs_attention(mails)}
    held = {m["subject"] for m in mails if outlook.goes_to_hold(m)}
    assert attention & held == set()


def test_a_platform_alert_still_goes_to_the_hold_window():
    """The routing fix must not empty the window it is protecting."""
    assert outlook.goes_to_hold(_mail(THE_ALERT, preview=THE_BODY))


def test_suppressing_servicenow_hands_it_back_to_the_hold_window(monkeypatch):
    monkeypatch.setattr(outlook, "SUPPRESS_SERVICENOW", True)
    m = _mail("Incident INC2 error on booking", sender="ServiceNow", preview="z")
    assert outlook.goes_to_hold(m)


def test_a_servicenow_incident_is_not_swallowed_by_the_bulk_sender_filter():
    """The exception was tested after the rule that eats it. `_BULK_SENDER`
    matches "servicenow", so the keep-it branch below was unreachable and every
    incident assigned to his group was dropped from the attention path — then
    re-surfaced twenty minutes later by the hold window as a bare subject."""
    m = _mail("Incident INC9499483 assigned to your group",
              sender="ServiceNow", preview="Booking API returning 500s.")
    assert outlook._BULK_SENDER.search("ServiceNow")      # the rule really does match
    assert m in outlook.needs_attention([m])              # and the exception still wins
