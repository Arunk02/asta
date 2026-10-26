"""26 Oct, task #419: what went wrong around one task, each pinned.

A design question got the status card; "workflow" meant CI; his note was refused
while it built; "push and raise PR" ran a build leg; a follow-up that opened its
PR read as "no change made" and was never linked; the session grew to 330k."""
import asyncio
import json

import pytest

from app import frontdesk, go, store, tasks, token_audit
from app import main as main_mod
from app.graph import code_graph

Q = "task 419 : one question can't we keep that inside the existing activity process?"


def _task(status="done"):
    t = store.create_task("Move LD one-click check into an activity", "code", "x", "booking")
    store.update_task(t["id"], status=status)
    return t["id"]


# --- a question about a task is answered, not given the card ------------------------------

def test_a_design_question_naming_a_task_goes_to_the_brain():
    assert main_mod._task_inquiry_target("c", Q) is None


@pytest.mark.parametrize("text", ["task 419 status?", "what's the progress on task 419?",
                                  "is task 419 done?"])
def test_a_status_question_still_gets_the_card(text):
    assert main_mod._task_inquiry_target("c", text) == 419


def test_a_temporal_workflow_is_not_ci():
    assert frontdesk.ci_inquiry("never fail the workflow on a LaunchDarkly timeout?")[0] is False
    assert frontdesk.ci_inquiry("why did the github actions run fail?")[0] is True
    assert frontdesk.ci_inquiry("check the CI and PR checks") == (True, False)


# --- his note to a running task is kept ---------------------------------------------------

def test_approve_and_raise_pr_with_an_instruction_is_kept_for_a_running_task():
    tid = _task("running")
    out = asyncio.run(tasks.refine(tid, "task 419 approve and raise PR, make it keep on retry "
                                        "until it get response"))
    assert "noted" in out
    assert "keep on retry" in tasks.note_waiting(tid)


def test_an_external_ask_on_a_finished_task_is_still_refused():
    with pytest.raises(ValueError):
        asyncio.run(tasks.refine(_task("done"), "send this PR to Vinish"))


# --- push / raise PR is a ship, not a build -----------------------------------------------

@pytest.mark.parametrize("note", ["Task 419 : push and raise PR",
                                  "Go ahead, push and raise the PR now — Arun approved."])
def test_a_ship_only_note_is_recognised(note):
    assert tasks.only_ship(note)


def test_a_note_with_an_instruction_is_not_ship_only():
    assert not tasks.only_ship("raise PR, and make the retries unlimited")


def test_a_ship_only_note_grants_the_ship_instead_of_a_build(monkeypatch):
    tid = _task("running")
    store.kv_set(f"task_addenda:{tid}", "Task 419 : push and raise PR")
    sent = []

    async def _notify(text, *a, **k):
        sent.append(text)
    monkeypatch.setattr("app.notify.notify", _notify)
    assert asyncio.run(tasks._note_arrived_while_building(tid, store.get_task(tid), "built")) is False
    assert go.ships(tid) and not tasks.note_waiting(tid) and not sent


# --- the follow-up card says what happened ------------------------------------------------

def test_already_in_place_is_not_a_failure():
    assert tasks._ALREADY_IN.search("Already in place from the last fix — nothing more to change.")
    assert tasks._ALREADY_IN.search("PR #1505 is already up and open — nothing further needed")
    assert not tasks._ALREADY_IN.search("Waiting on both gh run view --log-failed fetches")


# --- the session does not grow without end ------------------------------------------------

def test_context_size_is_read_from_the_last_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(token_audit, "CLAUDE_PROJECTS", tmp_path)
    cwd = "/x/task-1"
    d = tmp_path / token_audit._encode_project(cwd)
    d.mkdir()
    rows = [{"type": "assistant", "message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": n,
                                                         "cache_creation_input_tokens": 9}}}
            for n in (1000, 330000)]
    (d / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert token_audit.context_tokens(cwd, "s") == 330010
    assert token_audit.context_tokens(cwd, "missing") == 0


def test_a_build_leg_does_not_resume_an_oversized_session(monkeypatch):
    tid = _task("running")
    monkeypatch.setattr(tasks, "_resolve_executor", lambda _tid: "claude")
    monkeypatch.setattr(tasks, "task_cwd", lambda _tid, _ws: "/x/task")
    store.kv_set(f"task_session:{tid}:claude", "sid")
    monkeypatch.setattr(token_audit, "context_tokens", lambda cwd, sid: 330000)
    assert code_graph._too_large(tid, store.get_task(tid)) == 330000
    monkeypatch.setattr(token_audit, "context_tokens", lambda cwd, sid: 90000)
    assert code_graph._too_large(tid, store.get_task(tid)) == 0


def test_the_tool_that_steers_a_task_is_always_reachable():
    """His feedback on a delivered task must reach it on any turn, whatever the
    ranker thought the message was about — or the brain offers a new task."""
    from app import capabilities
    assert "refine_task" in capabilities.ALWAYS
    assert "refine_task" in capabilities.registry()
