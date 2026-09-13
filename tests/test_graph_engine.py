"""The LangGraph task engine (app/graph) — P2 of the Astra-class plan.

Everything real except the brain: the graph, its SQLite checkpointer, the task
table, `_run_code_leg` with its plan-only gate and pinned sessions. The CLI a leg
would call is replaced by a script, and each test reads what reached Arun.

What these pin down is the list of incidents the engine exists to end:

  #121  a plan announced as "✅ DONE" because its wording matched no sentinel
  #117  a task still "running" a day after the process that ran it had died
  #88/89 one change implemented twice, because a re-run leg started over
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import claude_cli, store, tasks
from app.graph import code_graph, notes, outcome, runner

WS = "test-workspace"


# --- the brain, scripted ----------------------------------------------------------

class Brain:
    """Answers each leg from a script; records the prompt and the flags it got."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        reply = self.replies.pop(0) if self.replies else "Implemented. Tests: 3 passed."
        if callable(reply):
            reply = await reply(prompt, kw) if asyncio.iscoroutinefunction(reply) \
                else reply(prompt, kw)
        return reply


@pytest.fixture
def world(monkeypatch, tmp_path):
    pushed: list[str] = []

    async def notify(msg, kind="task", **k):
        pushed.append(msg)

    async def nothing(*a, **k):
        return ""

    async def no_branches(tid, t):
        return []

    from app import notify as notify_mod, verify
    monkeypatch.setenv("ASTA_GRAPH", "1")
    monkeypatch.setattr(notify_mod, "notify", notify)
    monkeypatch.setattr(tasks, "_prepare_branches", no_branches)
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(tmp_path))
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "_resolve_executor", lambda tid: "claude")
    monkeypatch.setattr(tasks, "committed_so_far", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "_audit_note", lambda tid: "")
    monkeypatch.setattr(tasks, "_learn_from", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_self_review", nothing)
    monkeypatch.setattr(verify, "enabled", lambda: False)
    tasks._running.clear()
    return pushed


def _use(monkeypatch, brain: Brain) -> Brain:
    monkeypatch.setattr(claude_cli, "one_shot", brain)
    return brain


async def _settle(tid: int) -> None:
    """Wait for whatever the engine is doing on this task to stop."""
    for _ in range(3):
        job = tasks._running.get(tid)
        if job:
            await job
        await asyncio.sleep(0)


def _spawn(title="bump the retention", prompt="bump it", pipeline="full"):
    return tasks.spawn(title, prompt, kind="code", workspace=WS, pipeline=pipeline)


def _status(tid):
    return store.get_task(tid)["status"]


# --- the whole road: plan, gate, approve, build, done --------------------------------

def test_a_code_task_plans_stops_at_the_gate_then_builds_on_approval(world, monkeypatch):
    brain = _use(monkeypatch, Brain("STRUCTURE\n  Retention  30 → 90 days\n\nPLAN READY",
                                    "Changed one line. Tests: 3 passed."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        assert _status(t["id"]) == "awaiting_approval"
        assert await runner.waiting_at(t["id"]) == "plan"
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "done"
    assert runner.manages(tid)
    assert [c["plan_only"] for c in brain.calls] == [True, False]
    assert brain.calls[1]["resume"] is True                 # the same session, continued
    assert any("PLAN #" in p for p in world) and any("✅ DONE" in p for p in world)


def test_feedback_at_the_gate_replans_in_the_same_session(world, monkeypatch):
    brain = _use(monkeypatch, Brain("Plan A\n\nPLAN READY", "Plan B, PDF side left alone\n\nPLAN READY",
                                    "Implemented plan B."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "leave the PDF side alone")
        await _settle(t["id"])
        assert _status(t["id"]) == "awaiting_approval"
        assert await runner.waiting_at(t["id"]) == "plan"
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "done"
    assert "leave the PDF side alone" in brain.calls[1]["prompt"]
    assert brain.calls[1]["plan_only"] is True and brain.calls[1]["resume"] is True
    assert sum("PLAN #" in p for p in world) == 2


# --- #121: what the leg SAYS decides, not how it words it ----------------------------

def test_121_a_plan_worded_like_a_result_is_still_a_plan(world, monkeypatch):
    """Task #121 ended '…requires your go-ahead', matched no sentinel, and was
    announced as done with eighteen files it had not written."""
    _use(monkeypatch, Brain("I've mapped all 18 files. The change requires your go-ahead."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "awaiting_approval"
    assert not any("DONE" in p for p in world)


def test_a_reported_outcome_beats_the_wording(world, monkeypatch):
    def plan_but_say_done(prompt, kw):
        tid = int(prompt.split("task_id=")[1].split()[0].rstrip(".,"))
        outcome.record(tid, "plan_ready", "two repos", repos=["svc-a", "svc-b"])
        return "Done! All implemented and green."

    brain = _use(monkeypatch, Brain(plan_but_say_done))
    prepared: list[list[str]] = []

    async def prepare(tid, t, repos):
        prepared.append(list(repos))

    monkeypatch.setattr(code_graph, "_prepare_repos", prepare)

    async def go():
        t = _spawn()
        await _settle(t["id"])
        assert _status(t["id"]) == "awaiting_approval"
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "done"
    # Every repo the plan named got its checkout BEFORE the first edit.
    assert prepared == [["svc-a", "svc-b"]]
    assert "report_outcome" in brain.calls[0]["prompt"]


def test_a_fenced_outcome_works_for_a_brain_without_tools(world, monkeypatch):
    _use(monkeypatch, Brain('Which repo?\n```outcome\n{"kind": "needs_input", '
                            '"questions": ["svc-a or svc-b?"]}\n```'))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        return t["id"], await runner.waiting_at(t["id"])

    tid, gate = asyncio.run(go())
    assert gate == "context"
    assert any("❓" in p for p in world)


# --- the early gate, and micro growing into full -------------------------------------

def test_a_context_check_waits_for_the_answer_then_plans_with_it(world, monkeypatch):
    brain = _use(monkeypatch, Brain("CONTEXT CHECK: the booking side or the AP side?",
                                    "Plan for the AP side\n\nPLAN READY"))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        assert await runner.waiting_at(t["id"]) == "context"
        tasks.reply(t["id"], "the AP side")
        await _settle(t["id"])
        return t["id"], await runner.waiting_at(t["id"])

    tid, gate = asyncio.run(go())
    assert gate == "plan"
    assert "the AP side" in brain.calls[1]["prompt"]
    assert any("Arun at the gate: the AP side" in n for n in notes.all_notes(tid))


def test_micro_that_proves_bigger_reruns_on_the_full_pipeline(world, monkeypatch):
    brain = _use(monkeypatch, Brain("ESCALATE: touches three services",
                                    "Full plan\n\nPLAN READY"))

    async def go():
        t = _spawn(pipeline="micro")
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert store.kv_get(f"task_pipeline:{tid}") == "full"
    assert brain.calls[1]["resume"] is False                 # fresh session on full
    assert tasks.CODE_OVERRIDES.strip()[:40] in brain.calls[1]["prompt"]
    assert _status(tid) == "awaiting_approval"


# --- #117 and #88/89: a stopped job resumes, and does not start over -----------------

def test_a_usage_limit_mid_build_pauses_and_resumes_the_same_leg(world, monkeypatch):
    def limited(prompt, kw):
        raise tasks._LimitPaused("claude", None, "You've hit your session limit")

    brain = _use(monkeypatch, Brain("Plan\n\nPLAN READY", limited, "Finished the change."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        assert _status(t["id"]) == "paused"
        await tasks.resume_task(t["id"])
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "done"
    assert len(brain.calls) == 3                              # the plan leg ran ONCE
    assert brain.calls[2]["prompt"].startswith(code_graph.RESUME_PROMPT)
    assert brain.calls[2]["resume"] is True


def test_a_job_killed_mid_run_resumes_from_its_checkpoint_after_a_restart(world, monkeypatch):
    """The process dies while the implementation leg is running. The next process
    owns no workers; the row still says running. Recovery must pick it up at the
    leg that was cut short — and tell that leg it is continuing, not starting."""
    started = asyncio.Event()

    async def dies(prompt, kw):
        started.set()
        await asyncio.sleep(3600)

    brain = _use(monkeypatch, Brain("Plan\n\nPLAN READY", dies, "Finished the change."))
    monkeypatch.setattr(tasks, "RESTART_RESUME_SECONDS", 0)

    async def first_life():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "PLAN APPROVED")
        await started.wait()
        tasks._running[t["id"]].cancel()                      # the process dies
        await asyncio.sleep(0)
        return t["id"]

    tid = asyncio.run(first_life())
    tasks._running.clear()
    assert _status(tid) == "running"

    async def second_life():
        assert await tasks.recover_orphans() == [tid]
        await tasks._resume_due()
        await _settle(tid)

    asyncio.run(second_life())
    assert _status(tid) == "done"
    assert len(brain.calls) == 3
    assert brain.calls[2]["prompt"].startswith(code_graph.RESUME_PROMPT)


def test_an_approval_that_arrives_just_before_a_restart_is_not_lost(world, monkeypatch):
    """Found by the bench: the process died after 'approve' reached the task but
    before the graph applied it. The thread woke still waiting at the gate, the
    row said running, and nothing would ever move it."""
    brain = _use(monkeypatch, Brain("Plan\n\nPLAN READY", "Implemented."))
    monkeypatch.setattr(tasks, "RESTART_RESUME_SECONDS", 0)

    async def first_life():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "PLAN APPROVED")
        tasks._running[t["id"]].cancel()                      # dies before it runs
        await asyncio.sleep(0)
        return t["id"]

    tid = asyncio.run(first_life())
    tasks._running.clear()

    async def second_life():
        await tasks.recover_orphans()
        await tasks._resume_due()
        await _settle(tid)

    asyncio.run(second_life())
    assert _status(tid) == "done"
    assert [c["plan_only"] for c in brain.calls if "plan_only" in c] == [True, False]


def test_a_resume_that_finds_its_thread_at_a_gate_says_so(world, monkeypatch):
    """No answer to apply: the row must read 'awaiting approval', not 'running'."""
    _use(monkeypatch, Brain("Plan\n\nPLAN READY"))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        store.update_task(t["id"], status="paused")
        await tasks.resume_task(t["id"])
        await _settle(t["id"])
        return t["id"]

    assert _status(asyncio.run(go())) == "awaiting_approval"


# --- the check: fix, escalate, park -------------------------------------------------

def _check(monkeypatch, *results):
    from app import verify
    outs = list(results)

    async def run(cwd, cmd, _retried=False):
        ok = outs.pop(0)
        return verify.VerifyResult(ran=True, ok=ok, command=cmd, code=0 if ok else 1,
                                   tail="" if ok else "FAILED test_retention - 30 != 90")

    monkeypatch.setattr(verify, "enabled", lambda: True)
    monkeypatch.setattr(verify, "resolve_command", lambda cwd, ws=None: "pytest -q")
    monkeypatch.setattr(verify, "run", run)


def test_a_red_check_loops_to_fix_then_completes(world, monkeypatch):
    _check(monkeypatch, False, True)
    brain = _use(monkeypatch, Brain("Plan\n\nPLAN READY", "Implemented.", "Fixed the assertion."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "done"
    assert "30 != 90" in brain.calls[2]["prompt"]
    assert any("🔴" in p and "fixing" in p for p in world)


def test_the_same_failure_twice_parks_and_his_approval_accepts_it(world, monkeypatch):
    _check(monkeypatch, False, False)
    monkeypatch.setattr(tasks, "_stronger_executor", lambda tid: "")
    _use(monkeypatch, Brain("Plan\n\nPLAN READY", "Implemented.", "Tried again."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        assert _status(t["id"]) == "awaiting_approval"
        assert await runner.waiting_at(t["id"]) == "verify"
        await tasks.approve(t["id"])
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "done"
    assert any("stuck on the same failure" in p for p in world)


# --- stopping, refining, and the engine a task belongs to ----------------------------

def test_rejecting_at_the_gate_ends_it_quietly(world, monkeypatch):
    brain = _use(monkeypatch, Brain("Plan\n\nPLAN READY"))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        await tasks.reject(t["id"], "not now")
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "rejected"
    # One task leg. (Rejecting also asks what went wrong — a lesson, not a leg.)
    assert sum("plan_only" in c for c in brain.calls) == 1


def test_feedback_on_finished_work_goes_straight_back_to_building(world, monkeypatch):
    brain = _use(monkeypatch, Brain("Plan\n\nPLAN READY", "Implemented.", "Covered amend too."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        assert _status(t["id"]) == "done"
        await tasks.refine(t["id"], "also cover the amend path")
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert _status(tid) == "done"
    assert len(brain.calls) == 3                              # no second plan leg
    assert "also cover the amend path" in brain.calls[2]["prompt"]
    assert brain.calls[2]["plan_only"] is False


def test_a_task_finishes_on_the_engine_it_started_on(world, monkeypatch):
    _use(monkeypatch, Brain("Plan\n\nPLAN READY", "Implemented."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        monkeypatch.delenv("ASTA_GRAPH")                      # flag flipped mid-task
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])
        return t["id"]

    assert _status(asyncio.run(go())) == "done"


def test_with_the_flag_off_code_tasks_stay_on_the_old_engine(world, monkeypatch):
    monkeypatch.delenv("ASTA_GRAPH")
    _use(monkeypatch, Brain("Plan\n\nPLAN READY"))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        return t["id"]

    tid = asyncio.run(go())
    assert not runner.manages(tid)
    assert _status(tid) == "awaiting_approval"


def test_the_checkpoints_live_beside_the_isolated_database():
    assert runner.db_path().parent == store.DB_PATH.parent


# --- the pieces ----------------------------------------------------------------------

def test_outcome_precedence_is_reported_then_fenced_then_inferred():
    t = store.create_task("x", "code", "p", None)
    fenced = '```outcome\n{"kind": "blocked", "summary": "no creds"}\n```'
    assert outcome.read(t["id"], fenced, 0, "implement")["kind"] == "blocked"
    outcome.record(t["id"], "plan_ready")
    assert outcome.read(t["id"], fenced, 0, "implement")["source"] == "reported"
    # A report from an EARLIER leg does not speak for this one.
    later = json.loads(store.kv_get(f"task_outcome:{t['id']}"))["at"] + 1
    assert outcome.read(t["id"], "all good", later, "implement")["kind"] == "done"
    assert outcome.read(t["id"], "all good", later, "plan")["kind"] == "plan_ready"


def test_an_unknown_kind_is_refused():
    t = store.create_task("x", "code", "p", None)
    with pytest.raises(ValueError):
        outcome.record(t["id"], "finished")


def test_the_long_rider_rides_once_per_session():
    assert len(outcome.rider(1, fresh=False)) < len(outcome.rider(1, fresh=True)) / 3


def test_notes_are_capped_and_deduped():
    t = store.create_task("x", "code", "p", None)
    for i in range(30):
        notes.add(t["id"], f"fact {i}")
    notes.add(t["id"], "fact 29")
    rows = notes.all_notes(t["id"])
    assert len(rows) == notes.MAX_NOTES
    assert sum(r.endswith("fact 29") for r in rows) == 1
    assert len(notes.block(t["id"])) < notes.MAX_CHARS + 200


def test_report_outcome_is_a_capability_with_an_endpoint():
    from app import capabilities, main
    cap = capabilities.get("report_outcome")
    assert cap and "/outcome" in cap.http
    assert any(getattr(r, "path", "") == "/api/tasks/{task_id}/outcome" for r in main.app.routes)


def test_gate_nodes_do_nothing_but_wait():
    """A resumed node re-runs from its start: a push inside a gate would repeat."""
    import inspect
    for fn in (code_graph.gate, code_graph.wait_answer, code_graph.wait_verify):
        body = inspect.getsource(fn)
        assert "interrupt(" in body and "notify" not in body and "update_task" not in body


# --- a second brain reads the diff -------------------------------------------------

def test_the_diff_is_reviewed_by_the_brain_that_did_not_write_it(monkeypatch):
    monkeypatch.setattr(tasks, "_resolve_executor", lambda tid: "copilot")
    monkeypatch.setattr(tasks, "_can_take_over", lambda b: True)
    assert tasks._second_reviewer(1) == "claude"
    monkeypatch.setenv("ASTA_CROSS_REVIEW", "0")
    assert tasks._second_reviewer(1) == ""


def test_the_second_brain_reviews_read_only_and_a_failure_falls_back(monkeypatch):
    from app import memory, review
    seen: dict = {}

    async def second(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return "- the amend path is not handled"

    async def cheap(prompt, *a, **k):
        return "- fallback note"

    monkeypatch.setattr(claude_cli, "one_shot", second)
    monkeypatch.setattr(memory, "cheap_complete", cheap)
    diff = "diff --git a/x.py b/x.py\n+retention = 90\n" * 3
    got = asyncio.run(review.review_own_diff(diff, reviewer="claude", cwd="/tmp"))
    assert got == "- the amend path is not handled"
    assert seen["plan_only"] is True and "colleague's AI wrote" in seen["prompt"]

    async def broken(prompt, **kw):
        raise RuntimeError("quota")

    monkeypatch.setattr(claude_cli, "one_shot", broken)
    assert asyncio.run(review.review_own_diff(diff, reviewer="claude")) == "- fallback note"


# --- the check runs where the work is --------------------------------------------------

def _two_trees(monkeypatch, tmp_path):
    """A shared checkout and the task's own worktree, as on his machine — the
    sandbox points both at one folder, which is how this went unseen."""
    shared, own = tmp_path / "shared", tmp_path / "own"
    shared.mkdir(), own.mkdir()
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(shared))
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(own))
    from app import verify
    seen: list[str] = []

    async def run(cwd, cmd, _retried=False):
        seen.append(cwd)
        return verify.VerifyResult(ran=True, ok=True, command=cmd, code=0, tail="")

    monkeypatch.setattr(verify, "enabled", lambda: True)
    monkeypatch.setattr(verify, "resolve_command", lambda cwd, ws=None: "pytest -q")
    monkeypatch.setattr(verify, "run", run)
    return str(own), seen


def test_the_graph_checks_the_tasks_own_worktree(world, monkeypatch, tmp_path):
    own, seen = _two_trees(monkeypatch, tmp_path)
    _use(monkeypatch, Brain("Plan\n\nPLAN READY", "Implemented."))

    async def go():
        t = _spawn()
        await _settle(t["id"])
        tasks.reply(t["id"], "PLAN APPROVED")
        await _settle(t["id"])

    asyncio.run(go())
    assert seen == [own]


def test_the_old_engine_checks_the_tasks_own_worktree(world, monkeypatch, tmp_path):
    own, seen = _two_trees(monkeypatch, tmp_path)
    t = store.create_task("x", "code", "p", WS)
    tasks.mark_approved(t["id"])
    asyncio.run(tasks._verify_gate(t["id"], t, "done", hops=0))
    assert seen == [own]
