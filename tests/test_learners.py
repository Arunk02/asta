"""Four bounded learners — Round 3, P12.

The ledger records what he decided. These turn that into one bounded change
each, and hand it to the fence P6 already built: `evolve` proves a candidate
against the version running now, promotes it only on a measured gain, checks the
constitution, and can roll it back. Nothing here re-implements any of that — the
learners are evidence and a proposal, not a second promotion path.

What each one refuses to do is the interesting part:

  NOTHING MOVES ON THIN EVIDENCE. Every learner has a minimum, and below it
  proposes nothing at all. A learner that acts on five judgements is a random
  number generator with a changelog.

  NOTHING MOVES FAR. One step per run, inside the knob's own bounds. The worst a
  wrong learner can do is be slightly wrong, once, reversibly.

  NOTHING LEARNS FROM SILENCE. "ignored" means he never looked; it is evidence
  about the interruption, not about the words.
"""

from __future__ import annotations

import pytest

from app import learners, ledger, settings, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    yield


def _judge(n: int, verdict: str, kind: str = "send", target: str = "Alex",
           confidence: float | None = None) -> None:
    for _ in range(n):
        ledger.record(kind, target, verdict, confidence=confidence)


# --- nothing on thin evidence ------------------------------------------------------------

def test_an_empty_ledger_proposes_nothing():
    assert learners.propose() == []


@pytest.mark.parametrize("n", [1, 5, 9])
def test_a_handful_of_judgements_is_not_evidence(n):
    """A learner acting on five judgements is a random number generator with a
    changelog."""
    _judge(n, "rejected", confidence=0.9)
    assert learners.propose() == []


# --- 1 · confidence calibration -----------------------------------------------------------

def test_confidence_that_overstates_itself_is_pulled_down():
    """It claimed 0.9 and he took half of them as is. The claim is the thing
    that is wrong, and every gate downstream reads it."""
    _judge(10, "as_is", confidence=0.9)
    _judge(10, "rejected", confidence=0.9)
    found = [c for c in learners.propose() if c.knob == "ASTA_CONFIDENCE_SHIFT"]
    assert found, "an overconfident claim went uncorrected"
    assert found[0].after < 0, "confidence was not pulled DOWN"
    assert "0.9" in found[0].why and "50%" in found[0].why


def test_confidence_that_understates_itself_is_allowed_up():
    _judge(20, "as_is", confidence=0.7)
    found = [c for c in learners.propose() if c.knob == "ASTA_CONFIDENCE_SHIFT"]
    assert found and found[0].after > 0


def test_a_claim_that_matches_reality_is_left_alone():
    """The commonest outcome must be no change at all."""
    _judge(18, "as_is", confidence=0.9)
    _judge(2, "rejected", confidence=0.9)
    assert [c for c in learners.propose() if c.knob == "ASTA_CONFIDENCE_SHIFT"] == []


# --- 2 · the bar for sending without asking -----------------------------------------------

def test_a_poor_record_raises_the_bar_for_sending_unasked():
    _judge(6, "as_is", kind="send")
    _judge(14, "amended", kind="send")
    found = [c for c in learners.propose() if c.knob == "ASTA_SEND_MIN_CONFIDENCE"]
    assert found and found[0].after > settings.value("ASTA_SEND_MIN_CONFIDENCE")


def test_a_strong_record_lowers_the_bar_but_never_past_its_floor():
    _judge(40, "as_is", kind="send")
    found = [c for c in learners.propose() if c.knob == "ASTA_SEND_MIN_CONFIDENCE"]
    assert found and found[0].after >= settings.KNOBS["ASTA_SEND_MIN_CONFIDENCE"][1]


# --- 3 · how readily a feed is demoted ----------------------------------------------------

def test_pushes_he_never_looks_at_move_a_feed_to_the_digest_sooner():
    _judge(25, "ignored", kind="push", target="teams")
    found = [c for c in learners.propose() if c.knob == "ASTA_ATTENTION_IGNORE_SHARE"]
    assert found and found[0].after < settings.value("ASTA_ATTENTION_IGNORE_SHARE")


def test_pushes_he_acts_on_are_not_treated_as_noise():
    _judge(25, "as_is", kind="push", target="teams")
    assert [c for c in learners.propose() if c.knob == "ASTA_ATTENTION_IGNORE_SHARE"] == []


# --- 4 · what he keeps rewriting ----------------------------------------------------------

def test_a_habit_he_repeats_becomes_a_drafting_rule():
    """He removed the greeting nine times. That is a rule Asta could have
    followed before he had to."""
    for _ in range(12):
        ledger.record("send", "Alex", "amended",
                      before="Hi Alex, the build passed.", after="The build passed.")
    said = learners.drafting_rules()
    assert any("greeting" in r for r in said)


def test_one_rewrite_is_not_a_habit():
    ledger.record("send", "Alex", "amended",
                  before="Hi Alex, the build passed.", after="The build passed.")
    assert learners.drafting_rules() == []


# --- the fence stays P6's ----------------------------------------------------------------

def test_every_proposal_is_inside_its_knobs_bounds():
    _judge(30, "rejected", confidence=0.9)
    _judge(30, "amended", kind="send")
    _judge(30, "ignored", kind="push", target="teams")
    for c in learners.propose():
        assert settings.within_bounds(c.knob, c.after), f"{c.knob} -> {c.after} is out of bounds"


def test_a_proposal_is_an_evolve_candidate_and_not_a_second_promotion_path():
    """Promotion, proof, the constitution check and rollback all live in
    `evolve`. A learner that promoted its own change would be a way around the
    fence rather than a user of it."""
    import inspect
    from app import evolve
    _judge(30, "rejected", confidence=0.9)
    assert all(isinstance(c, evolve.Candidate) for c in learners.propose())
    src = inspect.getsource(learners)
    for forbidden in ("settings.pin(", "promote(", "record("):
        assert forbidden not in src, f"learners can {forbidden}"
