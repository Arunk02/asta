"""The self-learning loop must be fed by what he actually decides.

29 Sep: the experience ledger (P12, built 26 Sep) had 0 rows. It had exactly one
write site — an approved mechanical send — while the learners read rows of kind
"push" (did he engage with what Asta interrupted him with?) and "send" (was the
draft taken as written, edited, or rejected?) that NOTHING wrote. In the same
week the attention ledger saw 279 replies and 68 ignores; none reached the
learners. A loop that reads from a table nobody fills cannot learn.

Every decision he makes is now recorded where he makes it.
"""

from __future__ import annotations

import pytest

from app import attention, ledger, store


def _rows(kind: str = "") -> list[dict]:
    return ledger.recent(100, kind)


def _notified(key="k1", who="Navya R", source="teams-chat"):
    store.attention_upsert(key, source, who=who, what="x", priority=1, why="")
    store.attention_set(key, state="notified", notified_at=1.0)


@pytest.mark.parametrize("outcome,verdict", [
    ("he replied", "as_is"), ("acted", "as_is"), ("read_elsewhere", "as_is"),
    ("ignored", "ignored"), ("muted", "rejected"),
])
def test_every_push_he_reacts_to_teaches_the_learner(outcome, verdict):
    _notified()
    attention._label(store.attention_get("k1"), outcome)
    rows = _rows("push")
    assert rows and rows[0]["verdict"] == verdict and rows[0]["target"] == "teams-chat"


def test_a_draft_he_rejects_is_recorded():
    from app import main
    main._judge_staged("conv", {"to": "Navya R", "what": "draft", "channel": "teams"}, "no")
    assert _rows("send")[0]["verdict"] == "rejected"


def test_a_draft_he_edits_is_recorded_and_completed_by_the_revision():
    from app import main
    main._judge_staged("conv", {"to": "Navya R", "what": "Hi Navya, merged it now, thanks!",
                                "channel": "teams"}, "make it shorter, no greeting")
    row = _rows("send")[0]
    assert row["verdict"] == "amended"
    main._revision_staged("conv", {"to": "Navya R", "what": "Merged.", "channel": "teams"})
    row = _rows("send")[0]
    assert "shorter" in row["edit"] and "greeting removed" in row["edit"]


def test_an_offer_he_takes_or_turns_down_is_recorded():
    from app import main
    main._judge_offer("plan_code", accepted=True)
    main._judge_offer("investigate", accepted=False)
    got = {(r["target"], r["verdict"]) for r in _rows("offer")}
    assert ("plan_code", "as_is") in got and ("investigate", "rejected") in got


def test_history_already_recorded_is_not_wasted():
    """A week of his reactions sits in `outcomes`. The learners start from it
    rather than from zero — once."""
    for outcome in ("he replied", "he replied", "ignored"):
        store.record_outcome("attention", outcome, subject="k", detail="p1 source=teams-chat seen=1")
    assert ledger.backfill_pushes() == 3
    assert ledger.backfill_pushes() == 0, "only ever once"
    verdicts = sorted(r["verdict"] for r in _rows("push"))
    assert verdicts == ["as_is", "as_is", "ignored"]


def test_the_learner_can_now_see_something(monkeypatch):
    from app import learners
    for _ in range(12):
        ledger.record("push", "teams-chat", "ignored")
    for _ in range(1):
        ledger.record("push", "teams-chat", "as_is")
    assert any(c.knob == "ASTA_ATTENTION_IGNORE_SHARE" for c in learners.propose())
