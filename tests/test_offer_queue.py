"""His yes must land on the question he was shown.

From a real WhatsApp transcript, 2026-08-26. Asta staged a Teams call to Vinish,
Arun said "Go ahead", and nothing rang. He said it twice more. The brain then
concluded that approval must happen "through a separate confirmation channel" —
a reasonable inference from what it could see, and wrong.

What actually happened: `offer()` wrote to one global slot and every new offer
overwrote it. Four background daemons stage offers (refresh, ci_watch, and two in
meetings), so a staleness proposal took the slot between the call being staged and
him answering. His "yes" reached a question he had never read.
"""

from __future__ import annotations

import pytest

from app import offers, store


@pytest.fixture(autouse=True)
def _clean():
    store.init()
    offers.drop_all()
    yield
    offers.drop_all()


def _call():
    return offers.staged_write("teams_call", {"who": "Vinish Kumar"},
                               "📞 Call Vinish Kumar", "Teams call to Vinish Kumar.",
                               "Ring Vinish on Teams?", kind="teams_write")


def _daemon_offer():
    return offers.propose("🗂 booking context is stale", "14 days since last enrichment",
                          "Want me to bring the context up to date?", action="refresh")


def test_a_background_offer_cannot_steal_the_one_he_is_answering():
    """The bug, exactly. His call must still be the open question."""
    call = _call()
    _daemon_offer()

    head = offers.pending()
    assert head is not None
    assert head.id == call.id, "a daemon replaced the question he was shown"
    assert head.op.get("name") == "teams_call"


def test_his_yes_runs_the_call_he_read_not_the_daemons_proposal():
    call = _call()
    _daemon_offer()

    accepted = offers.accept()
    assert accepted is not None and accepted.id == call.id
    assert accepted.op["args"]["who"] == "Vinish Kumar", \
        "the approved arguments must be the ones he was shown"


def test_the_queued_offer_is_not_lost():
    """Protecting the head must not mean dropping everything behind it —
    that would trade a wrong action for a silently missing one."""
    _call()
    stale = _daemon_offer()

    assert [o.id for o in offers.waiting()] == [stale.id]
    offers.accept()
    head = offers.pending()
    assert head is not None and head.id == stale.id, \
        "the queued offer never surfaced after the first was answered"


def test_declining_also_surfaces_the_next_one():
    _call()
    stale = _daemon_offer()
    offers.decline()
    assert offers.pending().id == stale.id


def test_a_queued_offer_does_not_invite_a_yes():
    """Producers push render() the moment they stage.

    If a queued offer rendered as a question, he would be invited to say yes to
    something his yes does not answer — the ambiguity the single slot was
    protecting against, reintroduced by fixing the clobbering.
    """
    _call()
    stale = _daemon_offer()

    asked = offers.pending().render()
    assert "reply “yes” to go ahead" in asked

    queued = stale.render()
    assert "reply “yes” to go ahead" not in queued, "a queued offer asked for a yes"
    assert "Queued" in queued
    assert "Call Vinish" in queued, "it must say what it is waiting behind"


def test_moving_on_drops_everything_not_just_the_head():
    """A promoted question he never read must not be armed by a later yes."""
    _call()
    _daemon_offer()
    offers.drop_all()
    assert offers.pending() is None
    assert offers.waiting() == []


def test_an_expired_head_lets_the_next_through(monkeypatch):
    """A stale question must not block the queue behind it for ever."""
    _call()
    stale = _daemon_offer()
    monkeypatch.setattr(offers, "ttl_seconds", lambda: 1)
    head = offers.pending()
    # Age the head past its TTL without touching the queued one's clock.
    import json
    from dataclasses import asdict
    raw = json.loads(store.kv_get(offers.KEY))
    raw["created"] = 0.0
    store.kv_set(offers.KEY, json.dumps(raw))

    promoted = offers.pending()
    assert promoted is not None, "the queue was stranded behind an expired offer"
    assert promoted.id == stale.id


def test_the_queue_is_bounded():
    """A backlog he will never work through is a way to lose the recent ones."""
    _call()
    made = [_daemon_offer() for _ in range(offers.QUEUE_MAX + 3)]
    waiting = offers.waiting()
    assert len(waiting) <= offers.QUEUE_MAX
    assert waiting[-1].id == made[-1].id, "the newest proposal was dropped"


def test_double_yes_cannot_run_the_same_work_twice():
    """The property the original slot had, which the queue must not lose."""
    call = _call()
    first = offers.accept()
    second = offers.accept()
    assert first is not None and first.id == call.id
    assert second is None or second.id != call.id


# --- the same question is one question ---------------------------------------

def test_restaging_the_open_question_is_a_no_op():
    """The loop from the transcript.

    His yes was not reaching the staged call, so the brain staged it again every
    turn. Each re-stage must not become another queue entry — that fills the queue
    with copies of the very thing he is being asked, and changes the id he was
    shown underneath him.
    """
    first = _call()
    for _ in range(5):
        _call()
    assert offers.pending().id == first.id, "the head changed under him"
    assert offers.waiting() == [], "re-proposals piled up behind the same question"


def test_a_different_target_is_a_different_question():
    """Dedup must not swallow a genuinely new act."""
    _call()
    offers.staged_write("teams_call", {"who": "Priya"}, "📞 Call Priya",
                        "Teams call to Priya.", "Ring Priya?", kind="teams_write")
    assert [o.subject for o in offers.waiting()] == ["📞 Call Priya"]


def test_a_watcher_repeating_itself_does_not_evict_real_offers():
    """Arun's point: one refresh already proposed does not need proposing again.

    Watchers re-detect the same state every pass. Without collapsing them the
    bounded queue fills with restatements of one thing and drops the offers that
    actually differ — the exact failure a bounded queue exists to prevent,
    arriving by another route.
    """
    _call()
    for days in (14, 18, 21, 25, 30, 34):
        offers.propose("🗂 booking context is stale", f"{days} days since last enrichment",
                       "Want me to bring the context up to date?", action="refresh")
    ci = offers.offer("analyse", "🔴 CI failed: booking", "", "Analyse it?")

    waiting = offers.waiting()
    subjects = [o.subject for o in waiting]
    assert subjects.count("🗂 booking context is stale") == 1, \
        f"the same news queued more than once: {subjects}"
    assert ci.subject in subjects, "a real offer was evicted by repeats of one thing"


def test_the_surviving_duplicate_carries_the_freshest_context():
    """Replace rather than skip — "21 days" is more useful than "14"."""
    _call()
    for days in (14, 21):
        offers.propose("🗂 booking context is stale", f"{days} days since last enrichment",
                       "Refresh it?", action="refresh")
    stale = [o for o in offers.waiting() if "stale" in o.subject]
    assert len(stale) == 1
    assert "21 days" in stale[0].context, "kept the older, staler description"
