"""Asta must not mistake its own voice for Arun's backlog.

Every complaint in the 26 Aug screenshots reduced to one fault: `notify.notify`
filed EVERY outbound push in the attention ledger as something he owed a reply
to. None of the fifty-odd call sites named a source, so the source became the
literal string "notify" and the key a hash of Asta's own sentence.

The chase loop then re-raised them — and since a chase is itself a push, each
chase was filed and chased in turn. From his live database, verbatim:

    '⏳ Still waiting on you (2):  • ⏳ Still waiting on you (3): …'
    '⏳ Still waiting on you (3):  • ⏳ Still waiting on you (13): …'
    '⏳ Still waiting on you (13): • <a colleague> (Jira): …'

One real item, wrapped in three generations of Asta talking to itself. These
tests reproduce that growth and pin it shut, and — just as important — prove the
fix did not silence the inbound chase this whole subsystem exists for.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

import pytest

from app import asking, attention, delivery, health, main, notify, store


@pytest.fixture(autouse=True)
def _ledger_on(monkeypatch):
    # Deliberately NOT ASTA_DELIVERY: coalescing would buffer the second push of
    # a pair and these tests would be asserting the batching window, not the
    # ledger. `chase_due` does not consult the flag, so nothing here needs it.
    monkeypatch.setenv("ASTA_ATTENTION", "1")


@pytest.fixture(autouse=True)
def _no_phone(monkeypatch):
    """Nothing here reaches a real phone; record what would have been sent."""
    sent: list[str] = []

    async def _wa(text):
        sent.append(text)
        return True

    async def _tg(text):
        return True

    monkeypatch.setattr(notify, "wa_send", _wa)
    monkeypatch.setattr(notify.telegram, "send", _tg)
    return sent


def _arrival(key: str, what: str, source: str = "outlook") -> None:
    """Something that genuinely came in and wants an answer."""
    attention.consider(source, key, what=what, priority=attention.P_TODAY)


#: A fixed evening, past ASTA_EOD_HOUR (18:00 by default), so "is this overdue?"
#: is answered by an argument rather than by what time the suite happens to run.
#:
#: These three tests read `time.time()` at first and passed all evening on Arun's
#: laptop, then failed on CI at 17:37 UTC — chase_due derives end-of-day from the
#: clock it is given, and before 18:00 nothing is ever due. Same family as the
#: quiet-hours lesson in conftest: a suite that is green after six and red before
#: it teaches people to re-run rather than to read.
def _evening() -> float:
    return dt.datetime(2026, 6, 4, 20, 0).timestamp()


# --- the recursion ------------------------------------------------------------

def test_a_chase_is_never_itself_chased(_no_phone):
    """The exact growth from his screenshots: run the loop twice, and the second
    pass must not find the first pass's own message."""
    _arrival("INC4471", "someone: booking service is down")
    late = _evening()
    due = delivery.chase_due(now=late)
    assert [r["key"] for r in due] == ["INC4471"], "the real item must be chased"
    delivery.mark_chased(due, now=late)

    asyncio.run(notify.notify(delivery.render_chase(due), "attention",
                              urgency="direct", priority=attention.P_TODAY))

    again = delivery.chase_due(now=late + 3600)
    assert again == [], "Asta's own chase must never become a thing he owes"


def test_three_hours_of_chasing_never_nests(_no_phone):
    """Left running, the old code grew one layer an hour. Three passes is enough
    to show growth; this asserts flat."""
    _arrival("ACME-1234", "someone: revenue line item should be non-editable")
    now = _evening()
    chased_at_least_once = False
    for hour in range(3):
        due = delivery.chase_due(now=now + hour * 3600)
        if not due:
            continue
        chased_at_least_once = True
        delivery.mark_chased(due, now=now + hour * 3600)
        text = delivery.render_chase(due)
        assert "Still waiting on you" not in text.split(":", 1)[1], \
            "a chase quoting a chase is the nesting bug"
        asyncio.run(notify.notify(text, "attention", urgency="direct",
                                  priority=attention.P_TODAY))
    assert chased_at_least_once, "the loop never chased, so it asserted nothing"
    rows = store.attention_open(limit=50, max_priority=attention.P_FYI)
    chases = [r for r in rows if "Still waiting" in (r.get("what") or "")]
    assert all(attention.self_originated(r) for r in chases)


def test_asta_own_pushes_are_filed_as_its_own(_no_phone):
    """Health reports, finished tasks, meeting reminders — all Asta announcing."""
    for text in ("🩺 Health check — issues found:\n• claude-key: refused",
                 "✅ DONE — #70 PR 1409: Fast CT addition",
                 "📅 In 32 min — Backend CoP tech-byte"):
        asyncio.run(notify.notify(text, "info", urgency="direct"))
    rows = store.attention_open(limit=50, max_priority=attention.P_FYI)
    assert rows, "they are still recorded — the audit trail is worth keeping"
    assert all(attention.self_originated(r) for r in rows)
    assert delivery.chase_due(now=_evening()) == []


# --- the half that must NOT be silenced ---------------------------------------

def test_a_real_arrival_is_still_chased(_no_phone):
    """The fix removes Asta's own voice, not the feature."""
    _arrival("INC9001", "ServiceNow: L2 queue assignment")
    due = delivery.chase_due(now=_evening())
    assert [r["key"] for r in due] == ["INC9001"]


def test_a_real_source_joining_makes_it_owed_again():
    """A key Asta happened to create first, that a genuine arrival later lands
    on, is owed — the judgement follows the accumulated sources, not the first."""
    key = "shared-key"
    attention.consider(attention.SELF_SOURCE, key, what="Asta said something",
                       priority=attention.P_TODAY)
    assert attention.self_originated(store.attention_get(key))
    attention.consider("teams", key, what="a colleague asked about it",
                       priority=attention.P_TODAY)
    row = store.attention_get(key)
    assert not attention.self_originated(row)
    assert row["state"] == "notified"
    assert [r["key"] for r in delivery.chase_due(now=_evening())] == [key]


def test_self_rows_are_not_scored_as_interruptions_he_ignored():
    """`settle_stale` labels a week-old unanswered item "ignored", which is the
    signal the ranking is measured against. Counting Asta's own announcements
    there would teach the filter that its own voice is noise."""
    attention.consider(attention.SELF_SOURCE, "mine", what="✅ DONE — #70",
                       priority=attention.P_TODAY)
    _arrival("theirs", "someone: can you review this")
    old = time.time() - 30 * 86400
    for key in ("mine", "theirs"):
        store.attention_set(key, state="notified", notified_at=old)
    assert attention.settle_stale(days=7) == 1
    assert store.attention_get("mine")["state"] == "notified"
    assert store.attention_get("theirs")["state"] == "dropped"


def test_whats_on_my_plate_excludes_asta_talking():
    attention.consider(attention.SELF_SOURCE, "mine", what="📅 In 32 min — standup",
                       priority=attention.P_TODAY)
    _arrival("theirs", "someone: revenue line item")
    assert [r["key"] for r in attention.open_items()] == ["theirs"]


# --- "I already told you to ignore that" --------------------------------------

@pytest.fixture
def _no_batching(monkeypatch):
    """Coalescing is on in Arun's .env and absent on CI, so a health test that
    did not say which it wanted would assert the batching window on his laptop
    and the mute on CI. These are about the mute."""
    monkeypatch.delenv("ASTA_DELIVERY", raising=False)


def test_a_muted_problem_stops_being_announced(_no_phone, _no_batching, monkeypatch):
    async def _checks():
        return {"claude-key": "the API key is set but the provider REFUSED it",
                "disk": "only 2.1 GB free"}

    monkeypatch.setattr(health, "checks", _checks)
    asyncio.run(health.run_check())
    _no_phone.clear()

    health.mute("claude-key", "refused")
    # Both faults read as NEW on the next pass, so the only thing keeping
    # claude-key quiet is the mute — not the transition tracking that was
    # already there.
    store.kv_set("health_problems", json.dumps([]))
    asyncio.run(health.run_check())
    assert not any("claude-key" in t for t in _no_phone), "he said ignore it"
    assert any("disk" in t for t in _no_phone), "the other fault still reports"


def test_a_muted_problem_is_still_visible_when_he_asks():
    """Muting is never a black hole — "why didn't you tell me" must have an
    answer, so the report still lists it and says how to undo it."""
    health.mute("claude-key", "refused")
    text = health.report_text({"claude-key": "the provider REFUSED it"})
    assert "claude-key" in text and "muted" in text and "unmute claude-key" in text


def test_a_mute_is_forgotten_once_the_fault_clears(monkeypatch):
    """The safety property: the silence lasts exactly as long as the thing he
    silenced, so the same key breaking next month is news again."""
    health.mute("claude-key", "refused")

    async def _checks():
        return {}

    monkeypatch.setattr(health, "checks", _checks)
    asyncio.run(health.run_check(notify_transitions=False))
    assert health.muted() == {}


def test_only_a_muted_fault_left_is_not_all_healthy(_no_phone, _no_batching, monkeypatch):
    """The reassuring lie this module exists to prevent."""
    async def _checks():
        return {"claude-key": "refused"}

    monkeypatch.setattr(health, "checks", _checks)
    health.mute("claude-key", "refused")
    store.kv_set("health_problems", json.dumps(["claude-key", "disk"]))
    asyncio.run(health.run_check())
    assert not any("healthy again" in t for t in _no_phone)


@pytest.mark.parametrize("said,expected", [
    ("claude-key", "claude-key"),
    ("claude key", "claude-key"),
    ("CLAUDE-KEY", "claude-key"),
    ("disk", "disk"),
    ("nonsense", ""),
])
def test_resolve_key_is_loose_about_wording(said, expected):
    assert health.resolve_key(said, ["claude-key", "disk", "telegram"]) == expected


def test_an_ambiguous_name_mutes_nothing():
    """Two keys match "context" — silencing the wrong workspace would be
    invisible until an answer was already wrong."""
    assert health.resolve_key("context", ["context_booking", "context_iom"]) == ""


@pytest.mark.parametrize("text", [
    "ignore that message",
    "ignore him",
    "mute the call",
    "stop telling me lies",
])
def test_ordinary_sentences_are_not_mute_commands(text):
    """Loose about how he says it, strict about what it names — a hit requires a
    real health key, or "ignore that message" silences something at random."""
    store.kv_set("health_problems", json.dumps(["claude-key", "disk"]))
    assert main._health_mute_reply(text) == ""


def test_mute_and_unmute_round_trip():
    store.kv_set("health_problems", json.dumps(["claude-key"]))
    assert "Muted claude-key" in main._health_mute_reply("ignore claude-key")
    assert "claude-key" in health.muted()
    assert "already muted" in main._health_mute_reply("ignore claude-key")
    assert "Reporting claude-key again" in main._health_mute_reply("unmute claude-key")
    assert health.muted() == {}


def test_unmute_still_resolves_after_the_fault_stops_being_listed():
    """He mutes it, the health set moves on, and he changes his mind. The keys
    a command may name include the muted ones for exactly this."""
    health.mute("claude-key", "refused")
    store.kv_set("health_problems", json.dumps([]))
    assert "Reporting claude-key again" in main._health_mute_reply("unmute claude-key")


# --- the same question, twice --------------------------------------------------

def test_a_question_he_already_answered_is_not_asked_again(_no_phone):
    q = "Compare ETA against PORT_GATE_IN/EARLIEST or LATEST?"
    asked = store.create_question(q, "chat")
    store.close_question(asked["id"], "LATEST")
    # An explicit short timeout, not the 15-minute default: reuse must return
    # WITHOUT waiting, and a test that hangs for a quarter of an hour when the
    # behaviour regresses is a test nobody will ever let run to completion.
    assert asyncio.run(asking.ask(q, timeout=0.05)) == "LATEST"
    assert _no_phone == [], "asking again is what he called the worst of the lot"


def test_wording_that_differs_only_in_spacing_is_the_same_question():
    q = "Which repo applies?"
    asked = store.create_question(q, "chat")
    store.close_question(asked["id"], "booking-service")
    assert asyncio.run(asking.ask("  which   repo   APPLIES?  ", timeout=0.05)) == "booking-service"


def test_a_different_question_is_still_asked(_no_phone):
    asked = store.create_question("Which repo applies?", "chat")
    store.close_question(asked["id"], "booking-service")

    async def _run():
        return await asking.ask("Which branch should I cut from?", timeout=0.05)

    assert asyncio.run(_run()) == asking.NO_ANSWER
    assert any("Which branch" in t for t in _no_phone)


def test_an_unanswered_question_is_not_treated_as_answered(_no_phone):
    """A timed-out question has status 'timeout' and no answer. Reusing that as
    if he had spoken would put words in his mouth."""
    asked = store.create_question("Which repo applies?", "chat")
    store.close_question(asked["id"], "", status="timeout")

    async def _run():
        return await asking.ask("Which repo applies?", timeout=0.05)

    assert asyncio.run(_run()) == asking.NO_ANSWER
    assert _no_phone, "it must actually ask him"


def test_an_old_answer_stops_standing(_no_phone, monkeypatch):
    """Yesterday's answer is not today's answer."""
    asked = store.create_question("Which repo applies?", "chat")
    store.close_question(asked["id"], "booking-service")
    store.kv_set("_", "_")
    monkeypatch.setattr(asking, "REPEAT_WINDOW", 0.0)

    async def _run():
        return await asking.ask("Which repo applies?", timeout=0.05)

    assert asyncio.run(_run()) == asking.NO_ANSWER
    assert _no_phone


def test_the_same_question_in_flight_buzzes_him_once(_no_phone):
    """Two workers wanting the same answer is one question, not two."""
    async def _run():
        first = asyncio.ensure_future(asking.ask("Which repo applies?", timeout=5))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(asking.ask("Which repo applies?", timeout=5))
        await asyncio.sleep(0.05)
        qid = store.open_questions()[0]["id"]
        asking.answer(qid, "booking-service")
        return await asyncio.gather(first, second)

    assert asyncio.run(_run()) == ["booking-service", "booking-service"]
    assert len([t for t in _no_phone if "Which repo" in t]) == 1
