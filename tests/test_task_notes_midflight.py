"""A note to a running task is taken, and its plan is redone with it. 8 Oct, #268.

He wrote "task 268, dont revert the whole changes of him, only the changes related
to startup flag issue" while #268 planned a full revert. The rules read it as
ambiguous, the brain said the task "can't take mid-flight feedback", then looped
"still running — checking again" onto his phone and claimed a check-back it never
scheduled. And #268 was a "go" task: its plan would have gone ahead without him.
"""

from __future__ import annotations

import asyncio

import pytest

from app import agent, frontdesk, main, store, tasks
from app.graph import code_graph

SAID = ("task 268 , dont revert the whole chanegs of him , only the changes related "
        "to startup flag issue")


@pytest.fixture
def pushed(monkeypatch):
    said: list[str] = []

    async def notify(msg, *a, **k):
        said.append(msg)
    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    return said


@pytest.mark.parametrize("text", [
    SAID, "only fix the null check", "keep the retry config as it is",
    "don't touch the booking config",
])
def test_narrowing_a_task_is_feedback_for_it(text):
    assert frontdesk.interjection(text, True) == "augment"


def test_a_question_about_it_is_still_a_question():
    assert frontdesk.interjection("what is the status of 268", True) != "augment"


def test_the_tool_says_a_running_task_takes_feedback():
    doc = agent.refine_task.__doc__
    assert "RUNNING" in doc and "never wait" in doc


@pytest.mark.parametrize("step", [
    "Check if task #268 has finished; once done, call refine_task on it",
    "Task #268 is still running — I'll apply it once it finishes",
])
def test_waiting_on_a_task_is_not_a_step(step):
    assert main._WAITS_ON_A_TASK.search(step)


def _plan_with_note(monkeypatch):
    t = store.create_task("Revert event library change", "code", "p", None)
    from app import go
    go.grant(t["id"], "raise PR", ship=True)          # a plan that would go ahead
    tasks.augment(t["id"], SAID)
    monkeypatch.setattr(tasks._graph(), "manages", lambda tid: False)
    replied: list[tuple[int, str]] = []
    monkeypatch.setattr(tasks, "reply", lambda tid, text: replied.append((tid, text)) or "ok")

    async def approve(tid):
        raise AssertionError("a plan written before his note went ahead")
    monkeypatch.setattr(tasks, "approve", approve)
    return t, replied


def test_a_plan_that_predates_his_note_is_redone_not_waved_through(monkeypatch, pushed):
    t, replied = _plan_with_note(monkeypatch)

    async def run():
        await tasks.announce_plan(t["id"], t, "Revert all of PR #89.")
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert replied == [(t["id"], tasks.REPLAN_WITH_NOTE)]
    assert "re-planning with it" in pushed[0] and "startup flag" in pushed[0]
    assert "going ahead" not in pushed[0]


def test_the_graph_sends_the_plan_back_with_his_note(monkeypatch):
    t = store.create_task("Revert event library change", "code", "p", None)
    tasks.augment(t["id"], SAID)
    out = code_graph.go_on({"task_id": t["id"]})
    assert out["answer"]["approved"] is False
    assert "startup flag issue" in out["answer"]["text"]
    assert not tasks.note_waiting(t["id"]), "delivered once"
    assert code_graph.after_gate(out) == "plan"


# --- a repo outside the workspace ------------------------------------------------

BLOCKED = ("Plan is approved but still can't implement: telikos-event-router-library "
           "isn't checked out in this worktree yet. Blocked until Asta adds it.")


@pytest.fixture
def library(tmp_path, monkeypatch):
    import subprocess

    from app import worktrees
    projects, ws = tmp_path / "Projects", tmp_path / "booking-workspace"
    for repo in (projects / "telikos-event-router-library", projects / "other-lib",
                 ws / "telikos-email-service"):
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.setattr(worktrees, "REPO_DIRS", str(projects))
    monkeypatch.setattr(tasks, "code_cwd", lambda w: str(ws))
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, w: str(tmp_path / "task"))
    return projects / "telikos-event-router-library"


def test_a_repo_named_exactly_is_found_outside_the_workspace(library):
    from app import worktrees
    assert worktrees.outside(BLOCKED) == [library]
    assert worktrees.outside("the event router library") == [], "a full repo name, never a guess"


def test_a_run_stopped_by_a_missing_checkout_gets_the_checkout(library):
    t = store.create_task("Fix event library constructor", "code", "p", "booking")
    assert tasks._repos_still_needed(t["id"], t, BLOCKED) == ["telikos-event-router-library"]
    state = {"task_id": t["id"], "text": BLOCKED, "outcome": {"kind": "blocked"}}
    assert code_graph.after_implement(state) == "hop"
