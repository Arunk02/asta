"""A ticket in his own queue must reach him.

Arun: "from outlook it is not sending any notification, one incident was assigned
to L2 group where there i am but it was not send to me."

The ledger had them all along — nine rows of "Incident INC… has been assigned to
group OH - TELIKOS - L2", every one at priority 3, which is P_MUTE: recorded,
never pushed. Meanwhile "[FOR YOUR ACTION] - Incident Task" sat at p1, notified.

Three layers had to agree to lose it, and each was individually reasonable:

  triage         "assigned to group" is not "assigned to you", so no ask → P_FYI
  contacts       IT Service Desk sends mostly noise, so its act-rate is low
  adjust         low-rate sender + already P_FYI → P_MUTE

`outlook.needs_attention` deliberately exempts ServiceNow from the bulk filter and
says so in a comment. The exemption simply did not survive to the ranking layer —
the same per-layer drift that lost `critical` on the Teams path.
"""

from __future__ import annotations

import pytest

from app import attention

MINE = "IT Service Desk: Incident INC9605669 has been assigned to group OH - TELIKOS - L2."
THEIRS = "IT Service Desk: Change CHG0584320 has been assigned to group SOME - OTHER - TEAM"


@pytest.fixture(autouse=True)
def _groups(monkeypatch):
    monkeypatch.setattr(attention, "MY_GROUPS", ("oh - telikos - l2",))


def test_a_ticket_in_his_queue_is_never_muted():
    pri, why, _ = attention.rank(False, MINE, key="inc1", who="IT Service Desk")
    assert pri <= attention.P_TODAY, f"ranked p{pri} — it would never be pushed"
    assert "your group" in why


def test_another_teams_queue_stays_quiet():
    """The other half. If everything from that sender now reaches him, the fix has
    just replaced a missed incident with a flood, and he stops reading the channel."""
    pri, _, _ = attention.rank(False, THEIRS, key="chg1", who="IT Service Desk")
    assert pri >= attention.P_FYI


def test_the_sender_statistic_cannot_overrule_his_own_queue(monkeypatch):
    """The exact mechanism that lost it: a low act-rate on the sender dropped an
    already-FYI item to P_MUTE. His queue outranks the statistic."""
    from app import contacts
    monkeypatch.setattr(contacts, "adjust",
                        lambda p, who: (attention.P_MUTE, "you never act on them"))
    pri, _, _ = attention.rank(False, MINE, key="inc2", who="IT Service Desk")
    assert pri <= attention.P_TODAY


def test_it_is_pushed_rather_than_recorded_and_forgotten():
    """P_MUTE is not a quieter notification, it is no notification — `should_push`
    refuses anything at or above it. This asserts the end of the pipe, not the
    middle."""
    pri, _, _ = attention.rank(False, MINE, key="inc3", who="IT Service Desk")
    assert attention.should_push({"priority": pri, "state": "new"})


def test_no_groups_configured_changes_nothing(monkeypatch):
    """Nothing can infer which rotas somebody is on. With none configured this
    must behave exactly as it did before, rather than guessing."""
    monkeypatch.setattr(attention, "MY_GROUPS", ())
    assert not attention.assigned_to_him(MINE)


def test_the_group_is_matched_however_it_is_cased_or_spaced():
    assert attention.assigned_to_him("assigned to group oh - telikos - l2. priority 3")
    assert attention.assigned_to_him("ASSIGNED TO GROUP OH - TELIKOS - L2")
