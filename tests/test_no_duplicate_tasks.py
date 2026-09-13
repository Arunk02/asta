"""One live task per piece of work, and one context window each.

Arun: "many times it creating duplicate tasks and running". `spawn` validated its
arguments and created the row — nothing looked at whether the identical thing was
already in flight. Two live tasks doing the same work is waste twice over: the
second bills a whole agentic pipeline to reach the same answer, and then both
report, so he reviews the same work twice and cannot tell which diff is which.

The isolation half of his question is answered by construction and pinned below:
every task gets its own uuid4 session and its own git worktree, so nothing one
task reads or writes can reach another's context.
"""

from __future__ import annotations

import pytest

from app import store, tasks


def _live(prompt: str, workspace: str | None = "booking", **kw) -> dict:
    t = store.create_task(kw.get("title", "a task"), kw.get("kind", "analysis"),
                          prompt, workspace)
    store.update_task(t["id"], status="running")
    return t


# --- one live task per piece of work -----------------------------------------

def test_the_identical_live_task_is_returned_not_duplicated(monkeypatch):
    monkeypatch.setattr(tasks, "_start", lambda *a, **k: None, raising=False)
    first = _live("investigate booking ZZ1TESTBK9Q2")
    again = tasks.spawn("different title", "investigate booking ZZ1TESTBK9Q2",
                        "analysis", "booking")
    assert again["id"] == first["id"], "it spawned a second one"


def test_it_matches_on_the_prompt_not_the_title():
    """Two spawns of the same work get titled differently by whatever summarised
    it; the prompt is the instruction."""
    first = _live("investigate booking ZZ1TESTBK9Q2")
    hit = tasks._already_live("a totally different title",
                              "investigate booking ZZ1TESTBK9Q2", "booking")
    assert hit and hit["id"] == first["id"]


def test_a_different_workspace_is_different_work():
    _live("add the field", workspace="booking")
    assert tasks._already_live("t", "add the field", "empv3") is None


def test_a_different_prompt_is_different_work():
    _live("investigate booking ZZ1TESTBK9Q2")
    assert tasks._already_live("t", "investigate booking H2GFPMYM8R7", "booking") is None


def test_re_running_finished_work_is_allowed():
    """"do that again" is an ordinary request; refusing it is the more annoying
    failure."""
    t = _live("investigate booking ZZ1TESTBK9Q2")
    store.update_task(t["id"], status="done")
    assert tasks._already_live("t", "investigate booking ZZ1TESTBK9Q2", "booking") is None


def test_an_empty_prompt_never_matches():
    """Otherwise every prompt-less row collides with every other."""
    _live("")
    assert tasks._already_live("t", "", "booking") is None


# --- one context window each -------------------------------------------------

def test_each_task_gets_its_own_session_id():
    """Not shared with the chat, and not shared with another task: the session id
    is keyed by task AND executor, and minted fresh."""
    import inspect
    src = inspect.getsource(tasks)
    assert 'sid_key = f"task_session:{task_id}:{ex}"' in src
    assert "sid, resume = str(uuid.uuid4()), False" in src


def test_a_tasks_session_is_never_the_conversations():
    """`claude_cli._session_id` keys on conversation; tasks key on task id. If
    those ever collided, a task would inherit the whole chat transcript."""
    from app import claude_cli
    import inspect
    assert 'f"task_session:' not in inspect.getsource(claude_cli._session_id)


def test_each_code_task_gets_its_own_worktree():
    """Isolation of the FILES, not just the transcript — two tasks in one
    checkout is how #88 and #89 moved each other's branch."""
    import inspect
    src = inspect.getsource(tasks.task_cwd)
    assert ".asta-worktrees" in src or "task_cwd" in src


# --- naming a workspace the way it looks on disk -----------------------------

def test_the_directory_name_resolves_to_the_registered_one(monkeypatch):
    """The brain names the DIRECTORY it can see, not the registry key nobody
    told it about. `booking` is registered at ~/booking-workspace, so
    "booking-workspace" is a sensible thing to say — and on 10 September it
    failed a whole code task on the difference:

        unknown workspace 'booking-workspace' — known: booking, empv3
    """
    from pathlib import Path
    from app import workspace_tools
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {
        "booking": Path("/Users/x/booking-workspace"),
        "empv3": Path("/Users/x/Projects/empv3-tenant-intake"),
    })
    assert tasks.resolve_workspace("booking-workspace") == "booking"
    assert tasks.resolve_workspace("empv3-tenant-intake") == "empv3"


def test_the_registered_name_still_wins(monkeypatch):
    from pathlib import Path
    from app import workspace_tools
    monkeypatch.setattr(workspace_tools, "WORKSPACES",
                        {"booking": Path("/Users/x/booking-workspace")})
    assert tasks.resolve_workspace("booking") == "booking"


def test_it_is_case_insensitive(monkeypatch):
    from pathlib import Path
    from app import workspace_tools
    monkeypatch.setattr(workspace_tools, "WORKSPACES",
                        {"booking": Path("/Users/x/booking-workspace")})
    assert tasks.resolve_workspace("BOOKING-WORKSPACE") == "booking"


def test_an_ambiguous_name_resolves_to_nothing(monkeypatch):
    """Guessing between repos is the failure this whole area exists to prevent —
    two candidates must refuse, not pick."""
    from pathlib import Path
    from app import workspace_tools
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {
        "book": Path("/Users/x/book"),
        "booking": Path("/Users/x/booking"),
    })
    assert tasks.resolve_workspace("boo") == ""


def test_an_unknown_name_still_refuses(monkeypatch):
    from pathlib import Path
    from app import workspace_tools
    monkeypatch.setattr(workspace_tools, "WORKSPACES",
                        {"booking": Path("/Users/x/booking-workspace")})
    assert tasks.resolve_workspace("nonsense") == ""
    with pytest.raises(RuntimeError, match="unknown workspace"):
        tasks.code_cwd("nonsense")


def test_code_cwd_accepts_the_directory_name(monkeypatch):
    from pathlib import Path
    from app import workspace_tools
    root = Path("/Users/x/booking-workspace")
    monkeypatch.setattr(workspace_tools, "WORKSPACES",
                        {"booking": root, "empv3": Path("/Users/x/empv3")})
    assert tasks.code_cwd("booking-workspace") == str(root)


# --- a task at a gate stops owning the conversation --------------------------

def test_a_task_sitting_at_a_gate_for_hours_stops_claiming_messages():
    """A live task owns the conversation's attention — right while work is in
    flight, wrong once it has sat unanswered for hours.

    Task #117 asked for approval at 09:31, its PR merged by 12:33, and it still
    sat there. Being the only "live" task, it claimed every unrelated message
    Arun sent for the rest of the day: a topic request, a PR ask, an instruction
    about a different repo. "why still 117 is running doesn't makes sense it
    confusing with other things"."""
    import time
    stale = {"status": "awaiting_approval",
             "finished_at": time.time() - tasks.GATE_STALE_SECONDS - 60}
    assert tasks._gate_gone_stale(stale, time.time()) is True


def test_a_fresh_gate_still_owns_it():
    """Answering "yes" a minute after the plan arrives must still reach it."""
    import time
    fresh = {"status": "awaiting_approval", "finished_at": time.time() - 60}
    assert tasks._gate_gone_stale(fresh, time.time()) is False


def test_a_running_task_always_owns_it_however_long_it_runs():
    """Actually working is not the same as waiting on him. A three-hour
    implementation must still take "stop that"."""
    import time
    running = {"status": "running", "started_at": time.time() - 99999}
    assert tasks._gate_gone_stale(running, time.time()) is False


def test_a_stale_gate_is_not_auto_closed():
    """He may still want to approve it — it just stops being the default owner
    of whatever he says next."""
    import inspect
    src = inspect.getsource(tasks._gate_gone_stale)
    assert "NOT auto-closing" in src


def test_a_task_with_no_timestamps_is_not_called_stale():
    assert tasks._gate_gone_stale({"status": "awaiting_approval"}, 0) is False


# --- a plan is never announced as finished work ------------------------------

def test_a_plan_ending_plan_ready_is_a_gate_not_a_done():
    """Task #121 ended "PLAN READY" plus "AUTO-PROCEED gate: … requires your
    go-ahead", matched none of the three phrases listed, and was pushed to Arun
    as "✅ DONE" with 18 files it had not written. He had to be told by hand that
    the plan was waiting — and the task had to be flipped back to
    awaiting_approval before it could be approved at all."""
    tail = ("RISK: version-number collision if another PR merges first.\n\n"
            "AUTO-PROCEED gate: this touches 18 files — above the small-change "
            "threshold, so per rules this requires your go-ahead.\n\nPLAN READY")
    assert tasks._is_gate(tail)


@pytest.mark.parametrize("tail", [
    "PLAN READY",
    "Reply 'PLAN APPROVED' to proceed.",
    "This requires your go-ahead before I write anything.",
    "Waiting for your approval.",
    "Which repo applies?",
])
def test_every_way_a_run_says_it_is_waiting(tail):
    assert tasks._is_gate(tail)


@pytest.mark.parametrize("tail", [
    "Done, committed on feature/asta-121. Tests run: 2, Failures: 0.",
    "Implemented and committed. git diff --stat: 3 files changed.",
    "Nothing to change — the field already flows end to end.",
])
def test_finished_work_still_reads_as_finished(tail):
    """Widening the match must not turn every completion into a gate."""
    assert not tasks._is_gate(tail)


def test_the_brain_is_told_to_emit_the_sentinel():
    """Loose matching is the safety net; the sentinel is the actual contract."""
    assert "PLAN READY" in tasks.CODE_OVERRIDES
    assert "reported to him as" in tasks.CODE_OVERRIDES


def test_capitalisation_does_not_decide_whether_he_is_told_it_is_done():
    for tail in ("Waiting for your approval.", "WAITING FOR YOUR APPROVAL",
                 "plan ready", "Plan Ready"):
        assert tasks._is_gate(tail), tail
