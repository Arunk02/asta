"""The code-change graph: one checkpointed thread per code task.

    plan ──► announce_plan ──► gate ⏸ ──► implement ──► verify ──► complete
     ▲  │                        │           │   ▲        │ ▲
     │  ├─► ask ──► wait ⏸ ──────┘           hop ┘        ▼ │
     │  └─► escalate (micro → full)                 fix / stronger
     └──────────── feedback at a gate                     │
                                                  park ──► wait_verify ⏸

⏸ is an `interrupt()`: the thread stops, its state is checkpointed, and it resumes
when Arun answers — minutes or days later, across restarts.

Two rules make that safe, both from how LangGraph resumes:

  A GATE NODE DOES NOTHING BUT WAIT. A resumed node re-runs from its start, so a
  push or a status change inside a gate node would repeat on every resume. The
  announcement is its own node, before the gate.

  A LEG KNOWS WHEN IT IS BEING RE-RUN. A restart or a usage limit can stop a leg
  halfway, and the node then runs again from the top. `_leg` marks the stage it
  is running; finding its own mark on entry means "continue the session you were
  in", not "start the stage again" — so a half-implemented change is picked up,
  not re-implemented beside itself (the #88/#89 failure, in a new costume).

Every decision the old engine made by reading prose is made here from the
leg's typed outcome (see outcome.py).
"""

from __future__ import annotations

import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app import store

from . import notes, outcome


class JobState(TypedDict, total=False):
    task_id: int
    text: str             # the last leg's output
    outcome: dict         # the last leg's typed outcome
    plan_outcome: dict    # the approved plan's outcome — its repos, its summary
    answer: dict          # what Arun said at the last gate
    verdict: str          # green | skip | red | plateau
    verify_tail: str
    verify_cmd: str
    hops: int


class Stopped(Exception):
    """The task was rejected or cancelled while the graph was between steps."""


RESUME_PROMPT = (
    "Resume: this run stopped partway — a restart or a usage limit — and this is the "
    "same task continuing, not a new one. Check `git log --oneline -5` and `git status` "
    "first so nothing already done is redone, then carry on from the next unfinished step.")


def _tasks():
    from app import tasks
    return tasks


def _task(tid: int) -> dict:
    tasks = _tasks()
    t = store.get_task(tid)
    if not t or t["status"] in tasks.FINAL:
        raise Stopped(tid)
    return t


def _has_session(tid: int) -> bool:
    ex = _tasks()._resolve_executor(tid)
    return bool(store.kv_get(f"task_session:{tid}:{ex}"))


def _overrides(tid: int) -> str:
    tasks = _tasks()
    return tasks.CODE_OVERRIDES if tasks._pipeline_for(tid) == "full" else ""


async def _leg(tid: int, stage: str, fresh_prompt: str, *, effort: str,
               resume_prompt: str | None = None) -> tuple[str, dict]:
    """One brain leg for `stage` — fresh, continued, or picked up after a stop."""
    tasks = _tasks()
    t = _task(tid)
    mark = f"graph_leg:{tid}"
    session = _has_session(tid)
    if session and store.kv_get(mark) == stage:
        prompt = RESUME_PROMPT + outcome.rider(tid, fresh=False)
    elif session:
        prompt = (resume_prompt if resume_prompt is not None else fresh_prompt) \
            + outcome.rider(tid, fresh=False)
    else:
        from app import context_pack
        prompt = (fresh_prompt + context_pack.build(tid, "plan" if stage == "plan" else "implement")
                  + outcome.rider(tid, fresh=True))
    store.kv_set(mark, stage)
    since = time.time()
    async with tasks._ws_lock(t["workspace"]):
        text = await tasks._run_code_leg(tid, prompt, tasks.task_cwd(tid, t["workspace"]),
                                         resume=session, effort=effort,
                                         workspace=t["workspace"])
    store.kv_del(mark)
    _task(tid)                       # rejected while the leg ran: stop here
    return text, outcome.read(tid, text, since, "plan" if stage == "plan" else "implement")


# --- entry ------------------------------------------------------------------

def entry(state: JobState) -> str:
    """A new run plans; follow-up on approved work (a refine) goes straight to building."""
    ans = state.get("answer") or {}
    return "implement" if ans.get("approved") and _tasks().plan_approved(state["task_id"]) \
        else "plan"


# --- planning ----------------------------------------------------------------

async def plan(state: JobState) -> dict:
    tasks = _tasks()
    tid = state["task_id"]
    t = _task(tid)
    if not store.kv_get(f"graph_prepared:{tid}"):
        store.update_task(tid, started_at=time.time())
        async with tasks._ws_lock(t["workspace"]):
            await tasks._prepare_branches(tid, t)
        store.kv_set(f"graph_prepared:{tid}", "1")
    said = ((state.get("answer") or {}).get("text") or "").strip()
    fresh = tasks.first_code_prompt(tid, t)
    if said:
        fresh += f"\n\nArun's answer at the last gate:\n{said}"
        notes.add(tid, f"Arun at the gate: {said[:200]}")
    ex = tasks._resolve_executor(tid)
    text, out = await _leg(tid, "plan", fresh, effort=tasks._effort_for("code", ex),
                           resume_prompt=said or None)
    return {"text": text, "outcome": out, "plan_outcome": out, "answer": {}}


def after_plan(state: JobState) -> str:
    kind = (state.get("outcome") or {}).get("kind")
    if kind == "needs_input":
        return "ask"
    if kind == "escalate" and _tasks()._pipeline_for(state["task_id"]) == "micro":
        return "escalate"
    return "announce_plan"


async def escalate(state: JobState) -> dict:
    from app import notify
    tasks = _tasks()
    tid = state["task_id"]
    _task(tid)
    store.kv_set(f"task_pipeline:{tid}", "full")
    store.kv_set(f"task_escalated:{tid}", "1")
    for ex in tasks._executor_names():
        store.kv_del(f"task_session:{tid}:{ex}")
    reason = (state.get("outcome") or {}).get("summary", "")
    notes.add(tid, f"Escalated from micro to the full pipeline: {reason}")
    await notify.notify(f"⤴️ Task #{tid}: bigger than micro ({reason}) — rerunning "
                        f"through the full pipeline with plan gate.", "task")
    return {"answer": {}}


async def ask(state: JobState) -> dict:
    tid = state["task_id"]
    await _tasks().announce_context_check(tid, _task(tid), state.get("text", ""))
    return {}


async def announce_plan(state: JobState) -> dict:
    tid = state["task_id"]
    await _tasks().announce_plan(tid, _task(tid), state.get("text", ""))
    return {}


# --- the gates: nothing but waiting --------------------------------------------

def gate(state: JobState) -> dict:
    return {"answer": interrupt({"gate": "plan", "task_id": state["task_id"]})}


def wait_answer(state: JobState) -> dict:
    return {"answer": interrupt({"gate": "context", "task_id": state["task_id"]})}


def wait_verify(state: JobState) -> dict:
    return {"answer": interrupt({"gate": "verify", "task_id": state["task_id"]})}


def after_gate(state: JobState) -> str:
    ans = state.get("answer") or {}
    if ans.get("rejected"):
        return "end"
    return "implement" if ans.get("approved") else "plan"


def after_answer(state: JobState) -> str:
    return "end" if (state.get("answer") or {}).get("rejected") else "plan"


def after_wait_verify(state: JobState) -> str:
    ans = state.get("answer") or {}
    if ans.get("rejected"):
        return "end"
    return "complete" if ans.get("approved") else "fix"


# --- building -------------------------------------------------------------------

async def implement(state: JobState) -> dict:
    from app import task_spec
    tasks = _tasks()
    tid = state["task_id"]
    t = _task(tid)
    # Repos known up front: every repo the plan named gets a checkout BEFORE the
    # first edit, so the second half of a cross-repo change has somewhere to go.
    repos = (state.get("plan_outcome") or {}).get("repos") or []
    if repos:
        await _prepare_repos(tid, t, repos)
    said = ((state.get("answer") or {}).get("text") or "PLAN APPROVED").strip()
    body = task_spec.preamble(tid) + said
    ex = tasks._resolve_executor(tid)
    text, out = await _leg(
        tid, "implement",
        tasks.first_code_prompt(tid, t) + "\n\nArun approved the plan. Implement it now.\n"
        + said, effort=tasks._impl_effort(ex), resume_prompt=body)
    return {"text": text, "outcome": out, "answer": {}, "hops": 0}


async def _prepare_repos(tid: int, t: dict, repos: list[str]) -> None:
    import contextlib
    from pathlib import Path
    tasks = _tasks()
    from app import worktrees
    branch = store.kv_get(f"task_branch:{tid}") or tasks.task_branch(t, tid)
    with contextlib.suppress(Exception):
        await worktrees.create(Path(tasks.code_cwd(t["workspace"])), tid, branch, *repos)


#: Outcomes that stop the build and hand it to Arun instead of checking it.
_STOPPING = ("blocked", "needs_input", "failed")


def after_implement(state: JobState) -> str:
    tasks = _tasks()
    tid = state["task_id"]
    t = store.get_task(tid) or {}
    if (state.get("outcome") or {}).get("kind") in _STOPPING:
        return "park"
    unfinished = tasks._repos_still_needed(tid, t, (state.get("text") or "")[-2500:])
    if unfinished and state.get("hops", 0) < tasks._MAX_REPO_HOPS:
        return "hop"
    return "verify"


async def hop(state: JobState) -> dict:
    """The change reaches a repo with no checkout yet: prepare it, carry on here."""
    from app import notify
    tasks = _tasks()
    tid = state["task_id"]
    t = _task(tid)
    unfinished = tasks._repos_still_needed(tid, t, (state.get("text") or "")[-2500:])
    await _prepare_repos(tid, t, unfinished)
    ex = store.kv_get(f"task_executor:{tid}") or "copilot"
    store.kv_del(f"task_session:{tid}:{ex}")
    hops = state.get("hops", 0) + 1
    notes.add(tid, f"Change reaches {', '.join(unfinished)} — checkout prepared")
    await notify.notify(f"🔁 Task #{tid}: same change reaches {', '.join(unfinished)} — "
                        f"checkout prepared, continuing in this task (window "
                        f"{hops}/{tasks._MAX_REPO_HOPS}).", "task")
    text, out = await _leg(
        tid, f"hop-{hops}",
        "Continue THIS change in the repo(s) you said were still outstanding: "
        + ", ".join(unfinished) + ". A checkout is now prepared for them on your branch. "
        "Do NOT redo anything already committed; check `git log` first."
        + _overrides(tid) + tasks._branch_note(tid, t) + tasks._done_note(tid, t),
        effort=tasks._impl_effort(tasks._resolve_executor(tid)))
    return {"text": text, "outcome": out, "hops": hops}


# --- checking -------------------------------------------------------------------

async def verify_node(state: JobState) -> dict:
    """Run the repo's own check. Zero model tokens; the only un-fakeable judge."""
    from app import verify
    tasks = _tasks()
    tid = state["task_id"]
    t = _task(tid)
    if not verify.enabled():
        return {"verdict": "skip"}
    # Where the change is — the task's own tree — never the shared checkout.
    res = await verify.check_tree(tasks.task_cwd(tid, t["workspace"]), t["workspace"],
                                  tasks._cwd(t["workspace"]))
    if not res.ran:
        return {"verdict": "skip"}
    cmd = res.command
    if res.ok:
        store.record_outcome("verify", "passed", subject=str(tid),
                             detail=f"fix_rounds={tasks._verify_rounds(tid)} cmd={cmd[:140]}")
        return {"verdict": "green"}
    sig = verify.signature(res.tail)
    prev = store.kv_get(f"task_verify_sig:{tid}")
    store.kv_set(f"task_verify_sig:{tid}", sig)
    store.record_outcome("verify_round", "failed", subject=str(tid),
                         detail=f"round {tasks._verify_rounds(tid) + 1}: {cmd[:160]}")
    last = (res.tail.strip().splitlines() or [""])[-1][:160]
    notes.add(tid, f"Check failed: {last}")
    return {"verdict": "plateau" if prev and prev == sig else "red",
            "verify_tail": res.tail, "verify_cmd": cmd}


def after_verify(state: JobState) -> str:
    from app import verify
    tasks = _tasks()
    tid = state["task_id"]
    v = state.get("verdict")
    if v in ("green", "skip"):
        return "complete"
    if v == "plateau":
        if tasks._stronger_executor(tid) and store.kv_get(f"task_verify_escbrain:{tid}") != "1":
            return "stronger"
        return "park"
    return "fix" if tasks._verify_rounds(tid) < verify.max_rounds() else "park"


def after_fix(state: JobState) -> str:
    return "park" if (state.get("outcome") or {}).get("kind") in _STOPPING else "verify"


def _failure(state: JobState):
    from app import verify
    return verify.VerifyResult(ran=True, ok=False, command=state.get("verify_cmd", ""),
                               code=1, tail=state.get("verify_tail", ""))


async def fix(state: JobState) -> dict:
    from app import notify, verify
    tasks = _tasks()
    tid = state["task_id"]
    t = _task(tid)
    rounds = tasks._verify_rounds(tid) + 1
    store.kv_set(f"task_verify_rounds:{tid}", str(rounds))
    store.kv_set(f"task_escalated:{tid}", "1")
    hint = ((state.get("answer") or {}).get("text") or "").strip()
    feedback = (f"Arun's hint: {hint}\n\n" if hint else "") + verify.failure_feedback(_failure(state))
    await notify.notify(f"🔴 #{tid} {t['title']} — its own check failed, fixing "
                        f"(round {rounds}/{verify.max_rounds()})…", "task")
    text, out = await _leg(tid, f"fix-{rounds}",
                           tasks.first_code_prompt(tid, t) + feedback,
                           effort=tasks._impl_effort(tasks._resolve_executor(tid)),
                           resume_prompt=feedback)
    return {"text": text, "outcome": out, "answer": {}}


async def stronger(state: JobState) -> dict:
    """The same failure twice: one fresh attempt on a stronger brain."""
    from app import notify, verify
    tasks = _tasks()
    tid = state["task_id"]
    t = _task(tid)
    better = tasks._stronger_executor(tid)
    store.kv_set(f"task_verify_escbrain:{tid}", "1")
    store.kv_set(f"task_executor:{tid}", better)
    from app import routing
    if routing.enabled():
        routing.escalate(tid, "the same failure twice")
    store.kv_set(f"task_escalated:{tid}", "1")
    store.kv_set(f"task_verify_rounds:{tid}", str(tasks._verify_rounds(tid) + 1))
    for ex in tasks._executor_names():
        store.kv_del(f"task_session:{tid}:{ex}")
    store.record_outcome("verify_round", "escalated", subject=str(tid), detail=better)
    notes.add(tid, f"Stuck on the same failure — moved to {better} for a fresh attempt")
    await notify.notify(f"⤴️ #{tid} {t['title']} — stuck on the same failure, escalating "
                        f"to {better} for a fresh attempt…", "task")
    text, out = await _leg(tid, "stronger",
                           t["prompt"] + verify.failure_feedback(_failure(state)) + _overrides(tid),
                           effort=tasks._impl_effort(better))
    return {"text": text, "outcome": out}


async def park(state: JobState) -> dict:
    """Stop and ask — never ship red, never loop for ever."""
    from app import notify, verify
    tasks = _tasks()
    tid = state["task_id"]
    t = _task(tid)
    out = state.get("outcome") or {}
    if out.get("kind") in _STOPPING:
        asks = "\n".join(f"• {q}" for q in out.get("questions") or [])
        reason = (f"{out['kind'].replace('_', ' ')} — "
                  f"{out.get('summary') or 'it could not continue'}")
        tail = asks or out.get("summary", "")
    elif state.get("verdict") == "plateau":
        reason = f"stuck on the same failure after {tasks._verify_rounds(tid)} attempt(s)"
        tail = state.get("verify_tail", "")
    else:
        reason = f"still failing after {verify.max_rounds()} fix attempts"
        tail = state.get("verify_tail", "")
    store.kv_set(f"task_gate:{tid}", "verify")
    store.record_outcome("verify", "unresolved", subject=str(tid),
                         detail=f"{reason}: {state.get('verify_cmd', '')[:140]}")
    store.update_task(tid, status="awaiting_approval",
                      result=(state.get("text") or "") + "\n\n--- still not done ---\n" + tail)
    await notify.notify(
        f"🔴 #{tid} {t['title']} — {reason}:\n\n{tasks._phone_text(tail, 700)}\n\n"
        f"Reply with a hint, 'approve task {tid}' to accept as-is, or "
        f"'reject task {tid}'.", "task")
    return {}


async def complete(state: JobState) -> dict:
    tid = state["task_id"]
    await _tasks().complete(tid, _task(tid), state.get("text", ""))
    return {}


# --- the shape ------------------------------------------------------------------

def build() -> StateGraph:
    g = StateGraph(JobState)
    for name, fn in (("plan", plan), ("escalate", escalate), ("ask", ask),
                     ("wait_answer", wait_answer), ("announce_plan", announce_plan),
                     ("gate", gate), ("implement", implement), ("hop", hop),
                     ("verify", verify_node), ("fix", fix), ("stronger", stronger),
                     ("park", park), ("wait_verify", wait_verify), ("complete", complete)):
        g.add_node(name, fn)
    g.add_conditional_edges(START, entry, {"plan": "plan", "implement": "implement"})
    g.add_conditional_edges("plan", after_plan,
                            {"ask": "ask", "escalate": "escalate",
                             "announce_plan": "announce_plan"})
    g.add_edge("escalate", "plan")
    g.add_edge("ask", "wait_answer")
    g.add_conditional_edges("wait_answer", after_answer, {"plan": "plan", "end": END})
    g.add_edge("announce_plan", "gate")
    g.add_conditional_edges("gate", after_gate,
                            {"implement": "implement", "plan": "plan", "end": END})
    building = {"hop": "hop", "verify": "verify", "park": "park"}
    g.add_conditional_edges("implement", after_implement, building)
    g.add_conditional_edges("hop", after_implement, building)
    g.add_conditional_edges("verify", after_verify,
                            {"complete": "complete", "fix": "fix",
                             "stronger": "stronger", "park": "park"})
    checking = {"verify": "verify", "park": "park"}
    g.add_conditional_edges("fix", after_fix, checking)
    g.add_conditional_edges("stronger", after_fix, checking)
    g.add_edge("park", "wait_verify")
    g.add_conditional_edges("wait_verify", after_wait_verify,
                            {"complete": "complete", "fix": "fix", "end": END})
    g.add_edge("complete", END)
    return g
