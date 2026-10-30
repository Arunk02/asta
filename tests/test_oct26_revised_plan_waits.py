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


def test_lettered_findings_do_not_revoke_conditional_fix_authority():
    import json
    from app import reproduction
    t = _task()
    reproduction.request(t['id'], 'Reproduce first; if confirmed fix it')
    saved = reproduction.state(t['id'])
    store.kv_set(f"reproduction:{t['id']}", json.dumps({**saved, 'state': 'confirmed'}))
    plan = 'Reproduced: (A) line 355 accepts skipped checks; (B) promotion changes content. Awaiting your approval.'
    assert 'authorized a fix' in go.no_ask(t['id'], plan)
    assert go.no_ask(t['id'], 'Open questions: choose (A) return false, (B) fail the workflow.') == ''
    assert go.no_ask(t['id'], '(a) return false, (b) fail the workflow.') == ''
    assert go.no_ask(t['id'], 'Reproduced; choose (A) return false, (B) fail.') == ''
    go.note_his_words(t['id'], 'Get approval from Vinish before coding')
    assert go.no_ask(t['id'], plan) == ''


def test_conditional_authority_is_explicit_in_the_plan_prompt():
    import json
    from app import reproduction, tasks
    t = _task()
    reproduction.request(t['id'], 'Reproduce first; if confirmed fix it')
    saved = reproduction.state(t['id'])
    store.kv_set(f"reproduction:{t['id']}", json.dumps({**saved, 'state': 'confirmed'}))
    prompt = tasks.first_code_prompt(t['id'], t)
    assert 'do not request another approval for this same fix' in prompt
