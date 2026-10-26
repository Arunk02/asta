"""26 Oct, #419: a revised plan started implementing before he approved it.

He pushed back on the first plan; the revision was sized "1 class/file" and went
ahead as a small change — while it still said "waiting on Arun" and offered
(a)/(b)/(c) for him to pick."""
from app import go, store

SMALL = "STRUCTURE\n  A.java  rename the field"
REVISED = ("Revised plan per Arun's pushback: add the LD check as its own queued activity "
           "in application.yml. Still waiting on Arun for the exhausted-retries behavior: "
           "(a) silent false, (b) propagate and fail the workflow, or (c) catch locally. "
           "Recommended (c).")


def _task():
    return store.create_task("Move LD one-click check into an activity", "code", "x", "booking")


def test_a_plan_he_pushed_back_on_comes_back_to_him(monkeypatch):
    monkeypatch.setattr(go, "APPROVAL_FROM_TIER", 2)
    t = _task()
    store.add_task_event(t["id"], "gate", "answer: can't you do inside the rfp first activity")
    assert go.no_ask(t["id"], SMALL) == ""


def test_a_plan_that_says_it_waits_on_him_waits(monkeypatch):
    monkeypatch.setattr(go, "APPROVAL_FROM_TIER", 4)
    assert go.no_ask(_task()["id"], REVISED) == ""


def test_lettered_options_are_a_question_for_him(monkeypatch):
    monkeypatch.setattr(go, "APPROVAL_FROM_TIER", 4)
    plan = "STRUCTURE\n  A.java\nOn failure: (a) return false, (b) fail the workflow."
    assert go.no_ask(_task()["id"], plan) == ""


def test_a_small_plan_he_never_answered_still_goes_ahead(monkeypatch):
    monkeypatch.setattr(go, "APPROVAL_FROM_TIER", 2)
    assert go.no_ask(_task()["id"], SMALL).startswith("small change")
