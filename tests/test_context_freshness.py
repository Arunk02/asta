"""Stale context is a thing that must be impossible to lose track of.

The detector was never broken. On this machine it ran, it was right, and it
reported — five identical warnings inside eighty minutes on 2026-08-03, then a
weekly repeat for another eight days. The context still rotted 286 commits, because
the alert was a MESSAGE ending in "say 'rebuild the stale context'": an exact phrase
to remember, retype, and be at a keyboard for, while every other outward act in Asta
is a one-tap yes.

So what is pinned here is not detection. It is that the same rot is reported once
rather than five times, that ignoring it makes it louder rather than quieter, that
saying yes is cheap, and that nothing claims to have enriched a mini-skill it never
read.
"""

from __future__ import annotations

import asyncio

import pytest

from app import attention, health, offers, refresh, store


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    offers.clear()
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    yield
    offers.clear()


DETAIL = ("DRIFT telikos-booking-service: da20d26..6ee7d83\n"
          "  stale mini-skills: runtime/rfp-flow.md, integrations/sap-tms.md\n")
OTHER = ("DRIFT telikos-email-service: a7d1d87..e1b2569\n"
         "  stale mini-skills: integrations/sendgrid.md\n")


# --- drift gets an identity --------------------------------------------------

def test_the_same_rot_reported_twice_is_the_same_thing():
    """The five-in-eighty-minutes case. Two polls of an unchanged workspace must
    produce one key, or the ledger cannot tell a repeat from a new problem."""
    assert refresh.drift_key("booking", DETAIL) == refresh.drift_key("booking", DETAIL)


def test_the_order_the_detector_lists_them_in_does_not_matter():
    shuffled = DETAIL.replace(
        "runtime/rfp-flow.md, integrations/sap-tms.md",
        "integrations/sap-tms.md, runtime/rfp-flow.md")
    assert refresh.drift_key("booking", shuffled) == refresh.drift_key("booking", DETAIL)


def test_new_rot_is_a_new_thing():
    """Otherwise a second repo going stale hides behind the first one's row."""
    assert refresh.drift_key("booking", DETAIL) != refresh.drift_key("booking", OTHER)


def test_two_workspaces_never_share_a_row():
    assert refresh.drift_key("booking", DETAIL) != refresh.drift_key("iom", DETAIL)


# --- how long it has been rotten ---------------------------------------------

def test_a_workspace_nobody_has_ever_enriched_says_never():
    assert refresh.stale_days("fresh-ws") == -1.0


def test_enriching_is_recorded_separately_from_merely_checking():
    """`last_refresh` means "we looked"; this means "we fixed it". Conflating them
    is how a workspace gets checked every ten minutes for six weeks and is still
    six weeks out of date."""
    now = 1_000_000.0
    refresh.note_enriched("booking", now=now)
    assert refresh.stale_days("booking", now=now + 3 * 86400) == pytest.approx(3.0)
    assert store.kv_get("last_refresh:booking") is None


def test_neglect_makes_it_louder_not_quieter():
    now = 1_000_000.0
    refresh.note_enriched("booking", now=now)
    fresh = refresh._staleness_priority("booking", now=now + 86400)
    old = refresh._staleness_priority("booking", now=now + 30 * 86400)
    assert fresh == attention.P_FYI
    assert old == attention.P_TODAY
    assert old < fresh, "older drift must outrank newer drift"


@pytest.mark.parametrize("days,expected", [
    (1, attention.P_FYI),
    (6.9, attention.P_FYI),
    (7.1, attention.P_TODAY),
    (60, attention.P_TODAY),
])
def test_the_threshold_is_where_it_says_it_is(days, expected):
    """Pins the actual boundary. Without this the constant can be deleted and
    every test still passes, which is how a threshold silently becomes decorative."""
    now = 1_000_000.0
    refresh.note_enriched("edge", now=now)
    assert refresh._staleness_priority("edge", now=now + days * 86400) == expected


def test_stale_context_never_reaches_the_breakage_tier():
    """A month-old mini-skill produces worse answers; it does not take production
    down. Sharing a tier with "prod is on fire" is how the top tier stops meaning
    anything."""
    now = 1_000_000.0
    refresh.note_enriched("ancient", now=now)
    assert refresh._staleness_priority("ancient", now=now + 400 * 86400) > attention.P_NOW


def test_never_enriched_is_treated_as_seriously_as_long_neglect():
    """No context at all is not a mild version of slightly-stale context — every
    answer about that workspace is guesswork."""
    assert refresh._staleness_priority("never-touched") == attention.P_TODAY


# --- the alert becomes something he can act on -------------------------------

def _report(monkeypatch, workspace="booking", detail=DETAIL):
    sent = []

    async def fake_notify(text, kind, urgency="direct", priority=None, **kw):
        sent.append((text, priority))

    monkeypatch.setattr("app.notify.notify", fake_notify)
    out = asyncio.run(refresh._report_stale(workspace, None, detail, ["head"], "test"))
    return out, sent


def test_stale_context_arrives_as_a_yes_no_question(monkeypatch):
    """Not a message telling him to remember a phrase."""
    _report(monkeypatch)
    o = offers.pending()
    assert o is not None
    assert "Want me to bring the context up to date?" in o.prompt
    assert o.payload["workspace"] == "booking"


def test_the_approved_work_carries_the_quality_bar(monkeypatch):
    """His yes must not spawn a writer that captures "added a null check"."""
    _report(monkeypatch)
    action = offers.pending().action
    assert "null check" in action
    assert "DRIFTED mini-skills only" in action
    assert "source: path:line" in action
    assert "verified_against" in action


def test_the_same_rot_is_not_pushed_twice(monkeypatch):
    """The actual defect: five identical alerts in eighty minutes."""
    _report(monkeypatch)
    offers.clear()
    out, sent = _report(monkeypatch)
    assert sent == [], "the second poll re-notified identical drift"
    assert "already reported" in out
    assert offers.pending() is None


def test_different_rot_still_gets_through(monkeypatch):
    """Suppression must not become silence — new staleness is new news."""
    _report(monkeypatch)
    offers.clear()
    _, sent = _report(monkeypatch, detail=OTHER)
    assert len(sent) == 1


def test_how_long_it_has_been_rotten_is_in_the_message(monkeypatch):
    """"stale" is a shrug; "41 days since last enrichment" is a decision."""
    out, _ = _report(monkeypatch)
    assert "never enriched" in out
    refresh.note_enriched("booking2")
    out2, _ = _report(monkeypatch, workspace="booking2")
    assert "days since last enrichment" in out2


def test_the_push_carries_the_rank_so_delivery_can_hold_it(monkeypatch):
    """Without a rank it rides the unranked path and can arrive at 2am."""
    _, sent = _report(monkeypatch)
    assert sent[0][1] is not None


# --- it is visible without being asked ---------------------------------------

def test_a_long_neglected_workspace_shows_up_in_health(monkeypatch):
    monkeypatch.setattr(health, "CONTEXT_STALE_DAYS", 14)
    monkeypatch.setattr("app.workspace.available_workspaces", lambda: {"booking": {}})
    monkeypatch.setattr("app.workspace.get",
                        lambda n: type("W", (), {"exists": lambda self: True})())
    now = 1_000_000.0
    refresh.note_enriched("booking", now=now)
    assert health.stale_contexts(now=now + 40 * 86400) == {"booking": pytest.approx(40.0)}


def test_a_workspace_enriched_last_week_is_not_nagged_about(monkeypatch):
    """Code moves faster than the notes about it. Saying so daily is the noise
    this whole thing exists to replace."""
    monkeypatch.setattr("app.workspace.available_workspaces", lambda: {"booking": {}})
    monkeypatch.setattr("app.workspace.get",
                        lambda n: type("W", (), {"exists": lambda self: True})())
    now = 1_000_000.0
    refresh.note_enriched("booking", now=now)
    assert health.stale_contexts(now=now + 3 * 86400) == {}


def test_a_workspace_that_was_never_set_up_is_not_called_broken(monkeypatch):
    """Not bootstrapped is not the same as rotten, and reporting it as a problem
    every six hours is how a health check gets ignored."""
    monkeypatch.setattr("app.workspace.available_workspaces", lambda: {"ghost": {}})
    monkeypatch.setattr("app.workspace.get", lambda n: None)
    assert health.stale_contexts() == {}
