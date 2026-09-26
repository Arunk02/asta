"""The experience ledger — Round 3, P12.

"Approvals, edits, rejections and corrections are logged but never learned from,
and confidence is never calibrated."

Every day already produces the only signal that matters: he approved it as it
was, he changed it before it went, he said no, or he never answered. That was
scattered across a kv counter, an outcome row and nothing at all, in shapes
nothing could learn from.

One table, one row per judgement, with the features of the thing he judged — so
a learner can ask "of the drafts I claimed 0.9 for, how many went out
untouched?" and get a number instead of an opinion.

What it is NOT: a place to keep what he said. The edit is kept as the SHAPE of
the change — shorter, greeting removed, a name corrected — never the words,
because this repo is public and his colleagues' messages are not.
"""

from __future__ import annotations

import pytest

from app import ledger, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    yield


# --- one row per judgement ---------------------------------------------------------------

def test_a_judgement_is_recorded_with_what_it_was_about():
    ledger.record("send", "Alex Kumar", "as_is", confidence=0.9)
    rows = ledger.recent()
    assert len(rows) == 1
    assert rows[0]["kind"] == "send" and rows[0]["target"] == "Alex Kumar"
    assert rows[0]["verdict"] == "as_is" and rows[0]["confidence"] == 0.9


@pytest.mark.parametrize("verdict", ["as_is", "amended", "rejected", "ignored"])
def test_the_four_things_he_can_do_are_all_recordable(verdict):
    ledger.record("send", "Alex", verdict)
    assert ledger.recent()[0]["verdict"] == verdict


def test_an_unknown_verdict_is_refused_rather_than_stored():
    """A fifth verdict nobody defined would be counted as neither approval nor
    rejection by every learner, silently."""
    with pytest.raises(ValueError):
        ledger.record("send", "Alex", "sort of approved")


def test_no_confidence_is_recorded_as_no_confidence_not_as_zero():
    """Zero means "certain it is wrong". Most judgements have no claim at all,
    and calibration must not read those as a failed prediction."""
    ledger.record("push", "teams", "ignored")
    assert ledger.recent()[0]["confidence"] is None


# --- what is kept, and what is not -------------------------------------------------------

def test_the_shape_of_an_edit_is_kept_and_never_the_words():
    """This repo is public. His colleagues' messages are not."""
    ledger.record("send", "Alex", "amended",
                  before="Hi Alex, could you possibly take a look at PR 1409 when you get a chance?",
                  after="Alex — PR 1409 when you can?")
    row = ledger.recent()[0]
    assert "PR 1409" not in str(row), "the message itself was stored"
    assert row["edit"] and "shorter" in row["edit"]


def test_a_removed_greeting_is_noticed_as_a_shape():
    ledger.record("send", "Alex", "amended",
                  before="Hi Alex, the build passed.", after="The build passed.")
    assert "greeting removed" in ledger.recent()[0]["edit"]


def test_an_edit_that_only_grew_is_described_as_longer():
    ledger.record("send", "Alex", "amended",
                  before="Done.", after="Done — I also reran the failing test, it passes now.")
    assert "longer" in ledger.recent()[0]["edit"]


# --- what a learner asks it --------------------------------------------------------------

def test_the_as_is_rate_is_per_kind_and_per_person():
    for _ in range(8):
        ledger.record("send", "Alex", "as_is")
    ledger.record("send", "Alex", "amended")
    ledger.record("send", "Harika", "rejected")
    assert ledger.rate("send", "Alex") == pytest.approx(8 / 9)
    assert ledger.rate("send", "Harika") == 0.0
    assert ledger.rate("send") == pytest.approx(8 / 10)


def test_a_rate_with_too_little_evidence_says_so_rather_than_guessing():
    """Three approvals is not a 100% approval rate, it is three approvals."""
    ledger.record("send", "Alex", "as_is")
    assert ledger.rate("send", "Alex", least=10) is None


def test_calibration_compares_what_it_claimed_with_what_happened():
    """The point of the whole phase: does 0.9 mean anything?"""
    for _ in range(9):
        ledger.record("send", "Alex", "as_is", confidence=0.95)
    ledger.record("send", "Alex", "rejected", confidence=0.95)
    for _ in range(5):
        ledger.record("send", "Bo", "rejected", confidence=0.6)
    out = ledger.calibration()
    high = next(b for b in out if b["claimed"] == 0.9)
    assert high["n"] == 10 and high["actual"] == pytest.approx(0.9)
    low = next(b for b in out if b["claimed"] == 0.6)
    assert low["n"] == 5 and low["actual"] == 0.0


def test_judgements_with_no_claim_are_left_out_of_calibration():
    for _ in range(5):
        ledger.record("push", "teams", "ignored")
    assert ledger.calibration() == []


def test_ignored_is_not_counted_as_a_rejection_of_the_words():
    """He never looked. That says something about the interruption, not about
    whether the draft was right — and a learner that conflates them stops
    sending things he would have approved."""
    for _ in range(10):
        ledger.record("send", "Alex", "ignored", confidence=0.9)
    assert ledger.rate("send", "Alex") is None, "ignored counted as a verdict on the words"
