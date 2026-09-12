"""Standing instructions, and who counts as "somebody handing something over".

Two failures with one bill attached. Between 9 and 10 September Asta spawned six
agentic analyses off "IT Service Desk" mails — "Incident INC… has been assigned
to group OH - TELIKOS" — and every one of them failed on his Claude session
limit. They cost the quota and returned nothing, and nobody had asked for any of
them.

* `handed_over` (added the day before) treats a ticket number as permission to
  investigate: somebody saying "here, this one". That is true of a colleague and
  false of a ticket feed, and nothing checked WHO said it.
* He had already said not to look into incidents. There was nowhere to record
  that, so the only thing his instruction changed was that he had to repeat it.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from app import responder, store


_INC = "Incident INC9000001 has been assigned to group OH - TELIKOS"
_BOOKING = ("tools.maersk-digital.net: inland/booking/ZZ1TESTBK9Q2\n"
            "Can you check why STF is not done for this one?")


# --- who handed it over ------------------------------------------------------

@pytest.mark.parametrize("who", [
    "IT Service Desk", "Service Desk", "helpdesk", "ITSM Notifications",
    "Incident Management", "servicenow", "no-reply",
])
def test_a_ticket_feed_is_machine_traffic(who):
    """However human the display name reads."""
    assert responder.is_broadcast(who, _INC) is True


@pytest.mark.parametrize("who", ["Alex Kumar", "Ravi Menon", "Anita Rao"])
def test_a_colleague_is_not(who):
    assert responder.is_broadcast(who, _BOOKING) is False


@pytest.fixture
def decided(monkeypatch):
    """What `respond` chose: spawned, asked first, or nothing."""
    out: dict = {}
    monkeypatch.setattr(responder, "should_respond", lambda *a, **k: "")
    monkeypatch.setattr(responder, "_note_started", lambda *a: None)
    monkeypatch.setattr(responder, "_note_handled", lambda *a: None)
    from app import offers, tasks
    monkeypatch.setattr(tasks, "spawn",
                        lambda *a, **k: out.setdefault("spawned", True) and {"id": 1} or {"id": 1})
    monkeypatch.setattr(offers, "propose", lambda **k: out.setdefault("asked", True))
    return out


def test_a_ticket_number_from_a_machine_is_not_permission(decided):
    """Six investigations nobody asked for came from exactly this."""
    responder.respond("outlook", "IT Service Desk", _INC)
    assert "spawned" not in decided


def test_the_same_handle_from_a_colleague_still_is(decided):
    """The fix must not undo yesterday's — this is the case he asked for."""
    responder.respond("teams-chat", "Alex Kumar",
                      "Can you check why STF is not done for this one?", context=_BOOKING)
    assert decided.get("spawned") is True


# --- a standing instruction --------------------------------------------------

def test_muting_a_kind_survives_being_said_once():
    """"i asked to not to look into incidents still it looking into"."""
    store.kv_set(responder._MUTE_KEY, "")
    responder.mute("incident")
    assert responder.muted("incident") is True
    assert responder.muted_kinds() == {"incident"}


def test_a_muted_kind_is_refused_with_the_reason_he_gave(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")     # conftest neutralises it
    store.kv_set(responder._MUTE_KEY, "")
    responder.mute("incident")
    why = responder.should_respond("incident", 1, "k", now=time.time(),
                                   broadcast=False, sent_at=time.time())
    assert "asked me not to" in why


def test_muting_one_kind_leaves_the_others_alone(monkeypatch):
    """He said incidents, not everything."""
    monkeypatch.setenv("ASTA_RESPOND", "1")
    store.kv_set(responder._MUTE_KEY, "")
    responder.mute("incident")
    assert responder.should_respond("debug", 1, "k2", now=time.time(),
                                    broadcast=False, sent_at=time.time()) == ""


def test_it_can_be_undone():
    store.kv_set(responder._MUTE_KEY, "")
    responder.mute("incident")
    responder.unmute("incident")
    assert responder.muted("incident") is False


def test_the_brain_has_a_tool_to_record_it():
    """Agreeing in chat and leaving the behaviour running is the bug."""
    from app import capabilities
    cap = capabilities.get("stop_investigating")
    assert cap is not None and cap.fn is not None and cap.write
    assert "standing instruction" in (cap.note or "").lower()


def test_the_mute_is_persisted_not_held_in_memory():
    """A restart must not undo what he told it."""
    store.kv_set(responder._MUTE_KEY, "")
    responder.mute("incident")
    assert "incident" in (store.kv_get(responder._MUTE_KEY) or "")
