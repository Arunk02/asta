"""Background tasks — the orchestrator's spawn engine.

Asta (any brain: Copilot CLI, Claude, …) can delegate slow work here so the chat
stays responsive: each task runs as its own headless `copilot -p` process with its
own context, and Arun gets a WhatsApp/Telegram/UI notification when it finishes.

Kinds:
  analysis    read-only investigation/summarization — tasks run in PARALLEL
  code        edits code in a workspace — serialized per workspace (git safety)
  teams_draft drafts a Teams reply — result waits for Arun's APPROVAL, never
              auto-sent; approving sends it via teams_bridge

Main-chat context is never touched: workers are separate processes with
self-contained prompts.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as _dt
import json as _json
import os
import re
import time
import uuid
from pathlib import Path

from . import agents, claude_cli, copilot_cli, repo_ops, store, workspace_tools


class _LimitPaused(Exception):
    """A leg stopped because its brain hit a transient usage limit (session
    window, monthly quota, rate limit) — a pause to resume, not a failure to
    report. Carries who stopped and, if the message said so, when it lifts."""

    def __init__(self, brain: str, reset_at: float | None, raw: str):
        super().__init__(raw)
        self.brain = brain
        self.reset_at = reset_at
        self.raw = raw


# Wake a touch AFTER the stated reset, never exactly on it — a clock a minute
# fast would otherwise resume into the same wall and re-pause.
_RESUME_BUFFER = 120

ROOT = Path(__file__).resolve().parent.parent
TASK_TIMEOUT = {"analysis": 900, "code": 1800, "teams_draft": 300}

# Pipelines are Asta's own (agents/), not the workspace's. One definition for
# both executors and every workspace, so improving it improves every run — which
# is the point: the agents are iterated on to cut token waste, and that only
# works if there is a single thing to measure. The workspace still supplies the
# FACTS (resolver, lessons, build commands) via its ContextProvider.
CODE_PIPELINE = "solo"    # staged delivery, human gates
MICRO_PIPELINE = "micro"  # small change, ~25 turns, escalates
ANALYSIS_PIPELINE = "explore"
_JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")

# The solo pipeline ends in a PR; Arun's flow ends at a reviewed diff. This
# rider turns the interactive gates into pause points and cuts the pipeline
# off before it can publish anything.
CODE_OVERRIDES = """

[Asta runtime overrides — obey exactly]
- Boot 0 efficiency: run `sh .asta-context/boot.sh "<key nouns>"` as ONE terminal
  call to get root + resolver + lessons + pins together — do NOT cat
  workspace.yml, lessons.md, _pins.yml, navigation or integrations as separate
  calls (each is a billed turn). Do NOT run check-drift.js — drift is Asta's
  refresh job, not this task's. Read mini-skill/integration docs ONLY if the
  resolver's matches point at them, and only the cited sections.
- Context-confidence gate FIRST (before any code discovery — this is the whole
  point: don't plan on a half-read task). Right after Boot 0, judge whether the
  GOAL and SCOPE are 100% clear from what you were given (ticket + comments +
  Arun's message):
  · Fully clear, one obvious interpretation → print `CONTEXT CLEAR` and proceed
    straight through discovery → plan → the plan gate. Do NOT ask anything you
    can determine yourself. No interruption.
  · NOT sure — vague scope, undefined term, conflicting or missing acceptance
    criteria, or two plausible readings of INTENT → print `CONTEXT CHECK:`
    followed by ONLY the intent/scope questions a human must answer, then END.
    This costs almost nothing and saves the discovery+planning tokens a wrong
    reading would waste.
  Ask each thing at most once: intent/scope ambiguity HERE (pre-discovery);
  code-grounded questions LATER at the plan gate. Never double-ask, never ask
  here anything you could learn by reading the code.
- Headless run: you cannot ask questions interactively. At any human gate
  (grill questions, "PLAN APPROVED", "Which repo applies?") print the plan and
  the questions, then END the response. You will be resumed with the answers.
- Gate policy by size (Arun's rule): if after Stage 0.5 the change is clearly
  SMALL — at most 2 files, roughly ≤30 changed lines, no schema/avro/migration/
  config/cross-repo/API-contract impact, and ZERO open grill questions — do NOT
  stop at the plan gate: print `AUTO-PROCEED (small change): <one-line plan>`
  and continue straight through implement + tests in this same run. Anything
  medium or larger, any ambiguity, or any grill question → present the plan and
  STOP as usual; Arun's approval is mandatory there.
- Skip Stage 1.5 and never run Stage 6: no push, no PR, and no Jira writes of
  any kind (no subtasks, no comments, no transitions). Stage 5 Evolution SHOULD
  still run — lessons and skill patches are wanted. After the Stage 4 (and 4b)
  gates and Stage 5, print the gate lines plus `git diff --stat`, write
  handoff.md if more repos remain, and STOP. Asta ships after Arun reviews.
- Build-output hygiene (context safety): NEVER let raw build/test output into
  the conversation — `<cmd> > /tmp/build.log 2>&1; tail -5 /tmp/build.log`, on
  failure `grep -E "ERROR|FAIL|Tests run" /tmp/build.log | tail -20`.
- Prefer SCOPED tests for the touched classes/modules (`-Dtest=<TouchedClasses>`
  / `--tests`) in EVERY mode, not just quick — the full regression suite is CI's
  job after the PR. Running the whole module suite locally dumps huge logs and
  minutes of wall time for no extra safety here.
- Batch discovery: put related greps/finds in ONE terminal call joined with
  `;` and cap each with `| head -40` — do NOT run one grep per turn (every turn
  re-reads the whole accumulated context from cache; fewer turns is the single
  biggest cost lever). Never dump 100+ match lines into context.
- Amnesia guard: before re-doing ANY work (especially after a context
  compaction), check `git log --oneline -3` + `git status`. A commit from
  today matching this task is your OWN finished work → report it done; never
  re-discover or re-implement it.
- Keep narration lean: gate lines, diff stat, and short findings — do not
  restate plans or echo file contents back.
- Anchored reads only: open files at the resolver's `source:line` anchors with
  a line range (±20), never whole files — a full read of a large class dumps
  thousands of tokens that re-cache on every later turn. Read each path ONCE;
  if you already opened it this session, reuse what you have — never re-open.
- Commits: plain `git commit -m "<msg>"` — never a Co-Authored-By trailer, an
  AI/assistant name, or a "Generated with …" line."""

# Analysis rides the same pipeline in inquiry mode: full Boot 0 (resolver,
# lessons, pins) but read-only and gate-free.
ANALYSIS_RIDER = """

[Asta runtime overrides — obey exactly]
- Treat this as Stage 0 `inquiry`: answer the question with file:line evidence
  from Boot 0's matches and STOP. Read-only — do not modify any file, do not
  write plan files, no Jira writes, no branches, no builds."""

# How a paused run is recognised from its tail. Deliberately matches the solo
# agent's own gate wording, nothing looser.
_GATE_MARKS = ("PLAN APPROVED", "Which repo applies?", "Re-implement, modify, or cancel?")
# The cheap early gate: intent/scope unclear, asked BEFORE code discovery.
_CONTEXT_MARK = "CONTEXT CHECK:"
_HANDOFF_MARKS = ("handoff.md", "re-run me for")
_MAX_REPO_HOPS = 3   # multi-repo runs: one fresh window per repo, bounded

# One code-editing worker per workspace at a time; analysis/drafts don't lock.
_ws_locks: dict[str, asyncio.Lock] = {}

# Live workers, so rejecting one can actually stop it. Without this a rejected
# task ran to completion anyway: it kept billing model turns, kept editing the
# repo, and finally overwrote its own "rejected" row with "done".
_running: dict[int, asyncio.Task] = {}

# Statuses that mean "Arun has already decided" — a finishing worker must never
# overwrite them with its own result.
FINAL = ("rejected", "cancelled")

# A task is "live" (owns the conversation's attention) while it runs or waits at
# a gate — that's the window in which a follow-up should augment or redirect it.
LIVE_STATUSES = ("running", "awaiting_approval")

# Set by the chat layer at the start of a turn (contextvar → propagated to the
# agent's tools by anyio), so a code task spawned during the turn links back to
# the conversation it came from and follow-ups can steer it.
_TURN_CONV: contextvars.ContextVar[str | None] = contextvars.ContextVar("turn_conv", default=None)


def bind_conversation(conv_id: str | None) -> None:
    _TURN_CONV.set(conv_id or None)


def current_conversation() -> str | None:
    """The conversation the running turn belongs to — how a tool called inside a
    turn (loop signals, task links) finds its conversation without it being an
    argument the model has to fill."""
    return _TURN_CONV.get()


def link_task(conv_id: str, task_id: int) -> None:
    """Attach a task to the conversation that spawned it.

    A LIST, not a slot. The old single `conv_task:` key was last-write-wins, so
    spawning a second task silently orphaned the first: on WhatsApp and Telegram
    — one permanent conversation each — every later "also do X" or "stop that"
    hit whichever task happened to be newest, with no way to reach the other.
    """
    ids = _linked_ids(conv_id)
    if task_id not in ids:
        ids.append(task_id)
    store.kv_set(f"conv_tasks:{conv_id}", _json.dumps(ids[-10:]))
    store.kv_set(f"task_conv:{task_id}", conv_id)


def _linked_ids(conv_id: str) -> list[int]:
    raw = (store.kv_get(f"conv_tasks:{conv_id}") or "").strip()
    ids: list[int] = []
    if raw:
        try:
            ids = [int(i) for i in _json.loads(raw)]
        except (ValueError, TypeError):
            ids = []
    if not ids:
        # Migrate a pre-existing single link rather than losing it.
        legacy = (store.kv_get(f"conv_task:{conv_id}") or "").strip().strip('"')
        if legacy.isdigit():
            ids = [int(legacy)]
    return ids


def live_tasks_for(conv_id: str) -> list[int]:
    """Every task from this conversation that is still live, newest last."""
    ids = _linked_ids(conv_id)
    alive = [i for i in ids
             if (store.get_task(i) or {}).get("status") in LIVE_STATUSES]
    if alive != ids:                                  # self-heal finished links
        store.kv_set(f"conv_tasks:{conv_id}", _json.dumps(alive))
    return alive


def live_task_for(conv_id: str) -> int | None:
    """The single live task, or None. None when SEVERAL are live — the caller
    must then ask which one rather than guessing (that guess was the bug)."""
    alive = live_tasks_for(conv_id)
    return alive[0] if len(alive) == 1 else None


def paused_tasks_for(conv_id: str) -> list[int]:
    """Tasks from this conversation parked on a usage limit, waiting to resume."""
    return [i for i in _linked_ids(conv_id)
            if (store.get_task(i) or {}).get("status") == "paused"]


def augment(task_id: int, text: str) -> str:
    """Fold a follow-up into a live code task WITHOUT restarting its session.
    It's buffered and delivered as part of the instructions the moment Arun acts
    on the task's next gate — the mandatory approval stays intact and there's no
    expensive Claude/Copilot session re-cache."""
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    key = f"task_addenda:{task_id}"
    prior = (store.kv_get(key) or "").strip('"').strip()
    store.kv_set(key, (prior + "\n" if prior else "") + text.strip())
    where = "plan" if t["status"] == "awaiting_approval" else "next checkpoint"
    return (f"✚ noted for task #{task_id} — I'll fold that in when you approve its {where} "
            f"(no restart, no wasted tokens).")


def _drain_addenda(task_id: int) -> str:
    """Pull and clear anything buffered by augment(), formatted for the pipeline."""
    key = f"task_addenda:{task_id}"
    extra = (store.kv_get(key) or "").strip('"').strip()
    if not extra:
        return ""
    store.kv_set(key, "")
    return ("\n\n[Additional instructions Arun added while this was running — "
            "apply these too]\n" + extra)


def _ws_lock(workspace: str | None) -> asyncio.Lock:
    key = workspace or "_root"
    if key not in _ws_locks:
        _ws_locks[key] = asyncio.Lock()
    return _ws_locks[key]


def _cwd(workspace: str | None) -> str:
    if workspace and workspace in workspace_tools.WORKSPACES:
        return str(workspace_tools.WORKSPACES[workspace])
    return str(ROOT)


def spawn(title: str, prompt: str, kind: str = "analysis",
          workspace: str | None = None, teams_chat: str = "",
          executor: str = "", context_from: int | None = None,
          pipeline: str = "") -> dict:
    """Create the task row and fire the worker; returns immediately.

    executor:     '' = auto (copilot, or claude while copilot's quota is down).
    context_from: id of a finished task whose result is injected as trusted
                  evidence — a code task after an inquiry skips re-discovery.
    pipeline:     '' = auto (jira-key tickets → full solo, ad-hoc → micro),
                  or an explicit 'micro' / 'full'."""
    if kind not in TASK_TIMEOUT:
        raise ValueError(f"unknown task kind '{kind}' (analysis|code|teams_draft)")
    if kind == "teams_draft" and not teams_chat:
        raise ValueError("teams_draft tasks need teams_chat (who the draft is for)")
    if executor and executor not in _executor_names():
        raise ValueError(f"unknown executor '{executor}' (copilot|claude, empty = auto)")
    if pipeline and pipeline not in ("micro", "full"):
        raise ValueError(f"unknown pipeline '{pipeline}' (micro|full, empty = auto)")
    if context_from:
        prev = store.get_task(int(context_from))
        if prev and prev.get("result"):
            # Anchors from the earlier investigation — paying for discovery
            # twice was ~half of a code task's boot cost.
            prompt += (f"\n\n[Prior investigation (task #{context_from}) — trust "
                       "these anchors, do NOT re-discover them]\n"
                       + prev["result"][-2500:])
    t = store.create_task(title, kind, prompt, workspace or None, teams_chat)
    if executor:
        store.kv_set(f"task_executor:{t['id']}", executor)
    if kind == "code":
        if not pipeline:
            # Jira tickets are stories — full pipeline with gates. Ad-hoc asks
            # start micro; the agent ESCALATEs if discovery proves it bigger.
            pipeline = "full" if _JIRA_KEY.search(title + " " + prompt) else "micro"
        store.kv_set(f"task_pipeline:{t['id']}", pipeline)
        cid = _TURN_CONV.get()
        if cid:
            # Link both ways so a follow-up in this chat can steer the task, and
            # so completion can clear the link.
            link_task(cid, t["id"])
    job = asyncio.create_task(_worker(t["id"]))
    _running[t["id"]] = job
    job.add_done_callback(lambda _j, tid=t["id"]: _running.pop(tid, None))
    return t


def is_running(task_id: int) -> bool:
    job = _running.get(task_id)
    return bool(job and not job.done())


async def cancel(task_id: int, status: str = "cancelled") -> bool:
    """Stop a running worker and kill its copilot process. True if one was killed."""
    job = _running.get(task_id)
    if not job or job.done():
        return False
    job.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await job
    store.update_task(task_id, status=status, finished_at=time.time())
    return True


def _phone_text(result: str, limit: int = 1100) -> str:
    """A gate's output, made readable on a phone.

    The raw tail was unusable there: it started mid-sentence, carried code fences,
    tool noise and table pipes, and ran past the notification cap. This keeps the
    structure that matters (headings, bullets, numbered steps), drops the noise,
    and cuts on a LINE boundary so it never ends mid-word.
    """
    lines = (result or "").splitlines()
    keep: list[str] = []
    in_fence = False
    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        s = line.strip()
        if not s:
            if keep and keep[-1] != "":
                keep.append("")
            continue
        if s.startswith("|") or set(s) <= set("-=_|+ "):   # table rows / rules
            continue
        keep.append(line)
    # Prefer the tail (the plan + the ask), but start on a real heading/bullet.
    out: list[str] = []
    total = 0
    for line in reversed(keep):
        if total + len(line) + 1 > limit:
            break
        out.append(line)
        total += len(line) + 1
    out.reverse()
    while out and not (out[0].lstrip().startswith(("#", "-", "*", "•"))
                       or out[0].lstrip()[:2].rstrip(".").isdigit()):
        out.pop(0)
        if len(out) <= 3:
            break
    return "\n".join(out).strip() or (result or "").strip()[-limit:]


def _audit_note(task_id: int) -> str:
    """Audit the finished worker's session for token waste — records the trend
    and, when a run was wasteful, appends a one-line flag to the notification so
    it's visible without asking. Never lets an audit failure break completion."""
    try:
        from . import token_audit, skill_evolution
        rep = token_audit.audit_task(task_id)
        if not rep:
            return ""
        if rep["waste_ratio"] >= 0.12:
            note = (f"\n\n⚠️ token audit: {rep['waste_ratio']:.0%} avoidable "
                    f"(~{rep['avoidable_tokens']:,} tok), biggest = {rep['top_fix']}. "
                    f"Grade {rep['grade']}.")
        else:
            note = f"\n\n📉 token audit: {rep['waste_ratio']:.0%} waste, grade {rep['grade']}."
        # Close the loop: a waste category that RECURS across runs becomes a durable
        # fix-skill, so the next worker avoids it instead of the meter just noting it.
        try:
            evolved = skill_evolution.evolve()
        except Exception:
            evolved = []
        if evolved:
            note += "\n🧬 learned a skill to stop it: " + ", ".join(e["skill"] for e in evolved) + "."
        return note
    except Exception:
        return ""


def _agent_for(t: dict) -> str:
    """Workspace tasks run the workspace's project context solo agent (code = full
    pipeline, analysis = its inquiry mode); tasks without a workspace (e.g. on
    asta itself) have no project context, and the staged pipeline hard-stops without
    one — those keep the plain prompt path."""
    if not t["workspace"] or t["kind"] not in ("code", "analysis"):
        return ""
    return _pipeline_name(t["kind"])


def _executor_names() -> tuple[str, ...]:
    """Executor strings tasks persist — derived from the model spec table, so
    adding a CLI model makes it usable as an executor with no change here."""
    from . import agent as agent_mod
    return agent_mod.EXECUTOR_NAMES


def _effort_for(kind: str, executor: str = "") -> str:
    """Reasoning effort for a stage, per executor.

    Effort follows the work: investigation is cheap, implementation is not
    (Arun's quality-first choice). Planning sits in the middle. The per-model
    cascade lives in agent.effort_for, so every executor is dialled the same way.
    """
    stage = {"analysis": "ANALYSIS", "code": "PLAN"}.get(kind)
    if not stage:
        return ""
    from . import agent as agent_mod
    ex = executor or os.environ.get("ASTA_EXECUTOR", "copilot")
    return agent_mod.effort_for(agent_mod.from_exec_name(ex), stage)


def _impl_effort(executor: str = "") -> str:
    """Implementation leg — the one stage worth paying for, on any executor."""
    from . import agent as agent_mod
    ex = executor or os.environ.get("ASTA_EXECUTOR", "copilot")
    return agent_mod.effort_for(agent_mod.from_exec_name(ex), "CODE")


def _copilot_quota_down() -> bool:
    """One quota table for the whole app. This used to keep its own key + 48h TTL
    while the model picker read agent.quota_down("copilot") with a different TTL —
    same fact, two answers. Now everyone reads the one policy."""
    from . import agent as agent_mod
    return agent_mod.quota_down("copilot")


def _resolve_executor(task_id: int) -> str:
    """Sticky per task: decided once at the first leg, so a pipeline never
    switches brains mid-session (the session context wouldn't follow)."""
    ex = store.kv_get(f"task_executor:{task_id}") or ""
    if ex in _executor_names():
        return ex
    ex = os.environ.get("ASTA_EXECUTOR", "copilot")
    if ex == "copilot" and _copilot_quota_down() and claude_cli.available():
        ex = "claude"
    store.kv_set(f"task_executor:{task_id}", ex)
    return ex


def _pipeline_for(task_id: int) -> str:
    return store.kv_get(f"task_pipeline:{task_id}") or "full"


def _code_agent(task_id: int) -> str:
    """Pipeline name for a code task (Asta's own, not the workspace's)."""
    return MICRO_PIPELINE if _pipeline_for(task_id) == "micro" else CODE_PIPELINE


def _pipeline_name(kind: str, pipeline: str = "full") -> str:
    """Which of Asta's pipelines this task runs. Executor-neutral — the same
    definition serves Claude and Copilot; only delivery differs."""
    if kind == "code":
        return MICRO_PIPELINE if pipeline == "micro" else CODE_PIPELINE
    return ANALYSIS_PIPELINE


def _with_pipeline(pipeline: str, prompt: str) -> str:
    """Prepend a pipeline body to a prompt, for executors without a file flag."""
    body = agents.load(pipeline) if pipeline else ""
    return f"{body}\n\n---\n\n{prompt}" if body else prompt


def _agent_file(kind: str, pipeline: str = "full") -> str:
    """Path to the pipeline body, for executors that take a file."""
    p = agents.path_for(_pipeline_name(kind, pipeline))
    return str(p) if p else ""


# Phone updates, not a live feed. Three thumbs-up pings across a whole code task
# — coding done, tests running, PR pushed — keyed off the pipeline's own stage
# checklist. Each fires at most once per task; everything else stays silent until
# a gate or the final result.
_MILESTONES = (
    ("code_done", ("Stage 3 —", "Stage 3 -", "[x] Stage 2", "Stage 2 complete"),
     "⚙️ coding done — reviewing now"),
    ("testing", ("Stage 4 —", "Stage 4 -", "[x] Stage 3", "Unit Test"),
     "🧪 testing going on"),
    ("pr", ("Stage 6 —", "Stage 5 —", "[x] Stage 4b", "pr-delivery", "gh pr create"),
     "🚀 PR committed & pushed"),
)


def _progress_watcher(task_id: int, title: str):
    """Streamed-output scanner → at most one short push per milestone per task."""
    from . import notify
    seen: set[str] = set()
    buf: list[str] = []

    async def on_progress(chunk: str) -> None:
        buf.append(chunk)
        window = "".join(buf[-40:])          # markers can straddle chunk edges
        for key, marks, message in _MILESTONES:
            if key in seen:
                continue
            if any(m in window for m in marks):
                seen.add(key)
                await notify.notify(f"{message} — #{task_id} {title[:50]}", "task")
    return on_progress


async def _run_code_leg(task_id: int, prompt: str, cwd: str, *,
                        resume: bool, effort: str, workspace: str | None = None) -> str:
    """One executor leg of a code task, pinned to the task's session so gates
    can pause/resume without losing the pipeline's context. Copilot quota
    dying on a FRESH leg fails over to claude transparently; mid-pipeline it
    surfaces an actionable error instead (claude can't adopt a copilot session)."""
    ex = _resolve_executor(task_id)
    # Rounds are the signal that a run taught something: a task that took one leg
    # ran a standard flow, one that took several hit something worth recording.
    store.kv_set(f"task_rounds:{task_id}", str(_rounds(task_id) + 1))
    sid_key = f"task_session:{task_id}:{ex}"
    sid = store.kv_get(sid_key)
    if not sid:
        sid, resume = str(uuid.uuid4()), False
        store.kv_set(sid_key, sid)
    watcher = _progress_watcher(task_id, (store.get_task(task_id) or {}).get("title", ""))
    pipeline = _pipeline_name("code", _pipeline_for(task_id))
    from . import agent as agent_mod, dev_mcp
    # Serena + Context7, when ASTA_DEV_MCP is on: symbol-level nav/edit and live
    # docs for this repo. Empty string when disabled or nothing's installed, so
    # the default command is unchanged.
    dev_cfg = dev_mcp.config_json(cwd)
    if ex == "claude":
        try:
            return await claude_cli.one_shot(
                prompt, cwd=cwd, timeout=TASK_TIMEOUT["code"],
                agent_file=_agent_file("code", _pipeline_for(task_id)),
                effort=effort, session_id=sid, resume=resume, on_progress=watcher,
                mcp_config=dev_cfg)
        except RuntimeError as exc:
            # Claude's session (the pinned --resume thread) can't move to another
            # brain, so a limit here is always a pause — the wait is cheap because
            # resuming re-attaches this same session and re-derives nothing.
            if agent_mod.transient_limit(str(exc)):
                raise _LimitPaused("claude", agent_mod.limit_reset_at(str(exc)),
                                   str(exc)) from exc
            raise
    try:
        # Copilot's --agent resolves a name from the workspace's own agent
        # directory. Asta owns the pipeline now, so the body rides in the
        # prompt instead and nothing is installed into the user's repo.
        return await copilot_cli.one_shot(
            _with_pipeline(pipeline, prompt), cwd=cwd, timeout=TASK_TIMEOUT["code"],
            effort=effort, session_id=sid, resume=resume, on_progress=watcher,
            mcp_config=dev_cfg)
    except RuntimeError as exc:
        if not agent_mod.transient_limit(str(exc)):
            raise
        agent_mod.mark_quota_down("copilot")
        # A FRESH leg with claude up can fail over transparently — no session
        # context exists yet to lose. Mid-pipeline (resume) or with nothing to
        # switch to, the run PAUSES and waits instead of dying: the old
        # "reject and respawn" threw away everything discovered so far.
        if not resume and claude_cli.available() and not agent_mod.quota_down("claude_cli"):
            store.kv_set(f"task_executor:{task_id}", "claude")
            store.kv_del(sid_key)
            return await _run_code_leg(task_id, prompt, cwd, resume=False,
                                       effort=effort, workspace=workspace)
        raise _LimitPaused("copilot", agent_mod.limit_reset_at(str(exc)),
                           str(exc)) from exc


async def _run_simple(task_id: int, t: dict, prompt: str) -> str:
    """Non-pipeline kinds (analysis, teams_draft, agent-less code): one leg,
    executor-aware, with transparent claude failover when copilot's quota dies."""
    ex = _resolve_executor(task_id)
    cwd, tout = _cwd(t["workspace"]), TASK_TIMEOUT[t["kind"]]
    agent = _agent_for(t)
    eff = _effort_for(t["kind"], ex)
    pipeline = _pipeline_name(t["kind"]) if agent else ""
    agent_file = _agent_file(t["kind"]) if agent else ""
    # Analysis walks the code read-only, so Serena's symbol nav pays off here too;
    # teams_draft and other non-code kinds get nothing. "" when disabled.
    from . import dev_mcp
    dev_cfg = dev_mcp.config_json(cwd) if t["kind"] in ("analysis", "code") else ""
    if ex == "claude":
        return await claude_cli.one_shot(prompt, cwd=cwd, timeout=tout,
                                         agent_file=agent_file, effort=eff,
                                         mcp_config=dev_cfg)
    try:
        return await copilot_cli.one_shot(_with_pipeline(pipeline, prompt),
                                          cwd=cwd, timeout=tout, effort=eff,
                                          mcp_config=dev_cfg)
    except RuntimeError as exc:
        from . import agent as agent_mod
        if not agent_mod.transient_limit(str(exc)) or not claude_cli.available():
            raise
        agent_mod.mark_quota_down("copilot")
        store.kv_set(f"task_executor:{task_id}", "claude")
        return await claude_cli.one_shot(prompt, cwd=cwd, timeout=tout,
                                         agent_file=agent_file, effort=eff,
                                         mcp_config=dev_cfg)


def _rounds(task_id: int) -> int:
    try:
        return int(store.kv_get(f"task_rounds:{task_id}") or 0)
    except ValueError:
        return 0


def _escalated(task_id: int) -> bool:
    return (store.kv_get(f"task_escalated:{task_id}") or "") == "1"


def _verify_rounds(task_id: int) -> int:
    """How many times this task has already looped to fix a failing check."""
    try:
        return int(store.kv_get(f"task_verify_rounds:{task_id}") or 0)
    except ValueError:
        return 0


def _learn_from(task_id: int, title: str, result: str, status: str = "done") -> None:
    """Distil this run into a skill, in the background.

    Fire-and-forget on purpose: the task is finished and Arun has been told. A
    slow or failing extraction must never delay the result or fail the work.
    """
    from . import learn
    # Recorded for every finished task, not just the ones worth distilling —
    # "did the work land" is the measurement, and it needs the boring runs too.
    store.record_outcome("task", status, subject=str(task_id),
                         detail=f"rounds={_rounds(task_id)} escalated={_escalated(task_id)}")
    # Whatever skills this run had loaded now have a result attached to them.
    # Being read is not evidence a procedure is right; being read on runs that
    # keep working is the closest thing to it available here.
    row = store.get_task(task_id) or {}
    started = float(row.get("started_at") or row.get("created_at") or 0)
    if started:
        learn.credit(status, since=started)
    if not learn.should_extract(_rounds(task_id), _escalated(task_id), status):
        return
    asyncio.create_task(learn.extract(title, result, outcome=status,
                                      escalated=_escalated(task_id)))


def _stronger_executor(task_id: int) -> str:
    """A higher-capability code brain than the task's current one, available and
    not quota-down — or '' when the current brain is already the best option.

    Mirrors the copilot→claude failover already in _run_code_leg (claude carries
    the higher rank/context in the traits table): when the cheap brain keeps
    reproducing the same failure, a stronger one gets one bounded shot before Arun
    is bothered."""
    from . import agent as agent_mod
    cur = _resolve_executor(task_id)
    if cur != "claude" and "claude" in _executor_names() \
            and claude_cli.available() and not agent_mod.quota_down("claude_cli"):
        return "claude"
    return ""


async def _escalate_brain_and_retry(task_id: int, t: dict, outcome, hops: int,
                                    cwd: str, stronger: str) -> bool:
    """Plateau escape: switch to a stronger brain and take ONE fresh attempt.

    The fresh session is deliberate — the stuck brain's context is exactly what
    plateaued, so it is dropped rather than resumed. Guarded to happen at most once
    per task (task_verify_escbrain), and the goal + failure seed the new run."""
    from . import notify, verify
    store.kv_set(f"task_verify_escbrain:{task_id}", "1")
    store.kv_set(f"task_executor:{task_id}", stronger)
    store.kv_set(f"task_escalated:{task_id}", "1")
    store.kv_set(f"task_verify_rounds:{task_id}", str(_verify_rounds(task_id) + 1))
    for ex in _executor_names():
        store.kv_del(f"task_session:{task_id}:{ex}")
    store.record_outcome("verify_round", "escalated", subject=str(task_id), detail=stronger)
    await notify.notify(
        f"⤴️ #{task_id} {t['title']} — stuck on the same failure, escalating to "
        f"{stronger} for a fresh attempt…", "task")
    result2 = await _run_code_leg(
        task_id, t["prompt"] + verify.failure_feedback(outcome) + CODE_OVERRIDES, cwd,
        resume=False, effort=_impl_effort(stronger), workspace=t["workspace"])
    if (store.get_task(task_id) or {}).get("status") in FINAL:
        return True
    await _finish_code(task_id, t, result2, hops)
    return True


async def _park_verify(task_id: int, t: dict, result: str, outcome, reason: str) -> bool:
    """Hand a failing check to Arun — bounded, never infinite, never a green-looking
    'done' over a red check. The single exit for every give-up path in the gate."""
    from . import notify
    store.kv_set(f"task_gate:{task_id}", "verify")
    store.record_outcome("verify", "unresolved", subject=str(task_id),
                         detail=f"{reason}: {outcome.command[:140]}")
    parked = result + "\n\n--- verification still failing ---\n" + outcome.tail
    store.update_task(task_id, status="awaiting_approval", result=parked)
    await notify.notify(
        f"🔴 #{task_id} {t['title']} — check {reason}:\n\n"
        f"{_phone_text(outcome.tail, 700)}\n\n"
        f"Reply with a hint, 'approve task {task_id}' to accept as-is, or "
        f"'reject task {task_id}'.", "task")
    return True


async def _verify_gate(task_id: int, t: dict, result: str, hops: int) -> bool:
    """The objective bar before a code task calls itself done.

    Every other stop signal in Asta is the model declaring "done" about itself; a
    model will do that while the tests are red, and the learner then learns from a
    self-declared win. This runs the repo's OWN check (zero model tokens) and, on
    failure, loops to fix — bounded — instead of shipping a green-looking "done"
    over a red suite. That is the whole of "resilient".

    Returns True when this gate finalized the task (looped then re-finished, or
    parked for Arun); False to let the normal done path run. A repo with no
    resolvable check, a broken check, or the gate disabled is a pure NO-OP: the
    task finishes exactly as it did before this existed — this can never make a
    task that used to complete stop completing.
    """
    from . import verify, notify
    if not verify.enabled():
        return False
    cwd = _cwd(t["workspace"])
    cmd = verify.resolve_command(cwd, t["workspace"])
    if not cmd:
        return False
    outcome = await verify.run(cwd, cmd)
    if not outcome.ran:
        return False   # no usable oracle — behave exactly as today
    if outcome.ok:
        # fix_rounds in the detail feeds the convergence metric (quality.verify_convergence):
        # a rate that climbs while this average falls is the loop genuinely learning.
        store.record_outcome("verify", "passed", subject=str(task_id),
                             detail=f"fix_rounds={_verify_rounds(task_id)} cmd={cmd[:140]}")
        return False   # green: the normal done path runs, and now learns from a VERIFIED win

    # Red. Decide between three moves: retry the same brain, escalate to a stronger
    # one, or park. The signature tells progress from a plateau.
    sig = verify.signature(outcome.tail)
    prev_sig = store.kv_get(f"task_verify_sig:{task_id}")
    store.kv_set(f"task_verify_sig:{task_id}", sig)
    plateaued = bool(prev_sig) and sig == prev_sig
    vr = _verify_rounds(task_id)
    # A fix round is telemetry (is the loop converging?), kept OUT of the "verify"
    # kind so it never dilutes the terminal pass-rate.
    store.record_outcome("verify_round", "failed", subject=str(task_id),
                         detail=f"round {vr + 1}: {cmd[:160]}")

    # Plateau: the fix reproduced the SAME failure. Retrying the same brain just
    # repeats it — escalate to a stronger brain once (cheap-first, then pay up), or
    # stop wasting rounds and hand it to Arun.
    if plateaued:
        stronger = _stronger_executor(task_id)
        if stronger and store.kv_get(f"task_verify_escbrain:{task_id}") != "1":
            return await _escalate_brain_and_retry(task_id, t, outcome, hops, cwd, stronger)
        return await _park_verify(task_id, t, result, outcome,
                                  reason=f"stuck on the same failure after {vr} attempt(s)")

    # Making progress (a different failure) and budget left — resume the same brain
    # with only the failure fed back.
    if vr < verify.max_rounds():
        store.kv_set(f"task_verify_rounds:{task_id}", str(vr + 1))
        store.kv_set(f"task_escalated:{task_id}", "1")
        await notify.notify(
            f"🔴 #{task_id} {t['title']} — its own check failed, fixing "
            f"(round {vr + 1}/{verify.max_rounds()})…", "task")
        result2 = await _run_code_leg(
            task_id, verify.failure_feedback(outcome) + CODE_OVERRIDES, cwd,
            resume=True, effort=_impl_effort(_resolve_executor(task_id)),
            workspace=t["workspace"])
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return True
        await _finish_code(task_id, t, result2, hops)
        return True

    return await _park_verify(task_id, t, result, outcome,
                              reason=f"still failing after {verify.max_rounds()} fix attempts")


async def _finish_code(task_id: int, t: dict, result: str, hops: int) -> None:
    """Route a finished code leg: paused at a gate → ask Arun; handoff → next
    repo in a fresh window; otherwise done with the diff."""
    from . import notify
    tail = result[-2500:]
    if "ESCALATE:" in tail and _pipeline_for(task_id) == "micro":
        # Discovery proved the change is bigger than micro — rerun through the
        # full solo pipeline (fresh session; micro's context is 1-2 turns, not
        # worth carrying). pipeline=full makes this a one-way door, no loops.
        store.kv_set(f"task_pipeline:{task_id}", "full")
        # The teacher half of the loop: whatever finishes now writes the skill,
        # so the micro tier gets through this alone next time.
        store.kv_set(f"task_escalated:{task_id}", "1")
        for ex in _executor_names():
            store.kv_del(f"task_session:{task_id}:{ex}")
        reason = tail.split("ESCALATE:", 1)[1].strip().split("\n")[0][:200]
        await notify.notify(
            f"⤴️ Task #{task_id}: bigger than micro ({reason}) — rerunning "
            f"through the full pipeline with plan gate.", "task")
        result2 = await _run_code_leg(
            task_id, t["prompt"] + CODE_OVERRIDES, _cwd(t["workspace"]),
            resume=False, effort=_effort_for("code", _resolve_executor(task_id)),
            workspace=t["workspace"])
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _finish_code(task_id, t, result2, hops)
        return
    if _CONTEXT_MARK in tail:
        # Cheap early gate — intent unclear, asked before any discovery spend.
        store.kv_set(f"task_gate:{task_id}", "context")
        store.update_task(task_id, status="awaiting_approval", result=result)
        await notify.notify(
            f"❓ #{task_id} {t['title']} — quick context check before I dig in:\n\n"
            f"{_phone_text(result, 900)}\n\n"
            f"Reply with the answer, or 'reject task {task_id}'.", "task")
        return
    if any(m in tail for m in _GATE_MARKS):
        store.kv_set(f"task_gate:{task_id}", "plan")
        store.update_task(task_id, status="awaiting_approval", result=result)
        await notify.notify(
            f"📋 PLAN — #{task_id} {t['title']}\n\n"
            f"{_phone_text(result, 1100)}\n\n"
            f"👍 'approve task {task_id}' to implement · 'reject task {task_id}' to drop "
            f"· or just reply with changes.", "task")
        return
    if any(m in tail for m in _HANDOFF_MARKS) and hops < _MAX_REPO_HOPS:
        # Multi-repo change: the agent finished one repo and asked for a fresh
        # window for the next (one repo = one window keeps the context small).
        ex = store.kv_get(f"task_executor:{task_id}") or "copilot"
        store.kv_del(f"task_session:{task_id}:{ex}")
        await notify.notify(
            f"🔁 Task #{task_id}: repo done, continuing with the next repo "
            f"(fresh window {hops + 1}/{_MAX_REPO_HOPS}).", "task")
        result2 = await _run_code_leg(
            task_id,
            "Resume from .asta-context/todos.md + handoff.md — continue with the next "
            "repo." + CODE_OVERRIDES,
            _cwd(t["workspace"]), resume=False,
            effort=_impl_effort(_resolve_executor(task_id)),
            workspace=t["workspace"])
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _finish_code(task_id, t, result2, hops + 1)
        return
    if await _verify_gate(task_id, t, result, hops):
        return
    store.update_task(task_id, status="done", result=result, finished_at=time.time())
    _learn_from(task_id, t["title"], result)
    waste = _audit_note(task_id)
    await notify.notify(
        f"✅ DONE — #{task_id} {t['title']}\n\n{_phone_text(result, 700)}\n\n"
        f"Diff is local only — nothing pushed. Say 'ship' when you're happy."
        f"{waste}", "task")


async def _worker(task_id: int) -> None:
    from . import notify
    t = store.get_task(task_id)
    if not t:
        return
    prompt = t["prompt"]
    agent = _agent_for(t)
    if t["kind"] == "teams_draft":
        prompt += ("\n\nOutput ONLY the final message text to send — no preamble, "
                   "no quotes, no explanations.")
    elif t["kind"] == "analysis":
        # No resolver injection: the solo agent's Boot 0 runs resolve-task.js
        # itself (plus lessons + pins) — injecting a second copy just costs tokens.
        if agent:
            prompt += ANALYSIS_RIDER
    elif t["kind"] == "code":
        if agent:
            # micro's agent file is self-contained; the rider is solo-only.
            if _pipeline_for(task_id) == "full":
                prompt += CODE_OVERRIDES
        else:
            prompt += repo_ops.playbook_block(Path(_cwd(t["workspace"])))
    try:
        if t["kind"] == "code":
            async with _ws_lock(t["workspace"]):
                # started_at marks actual execution, not time spent queued on the lock
                store.update_task(task_id, started_at=time.time())
                if agent:
                    result = await _run_code_leg(
                        task_id, prompt, _cwd(t["workspace"]),
                        resume=False, effort=_effort_for("code", _resolve_executor(task_id)),
                        workspace=t["workspace"])
                else:
                    result = await _run_simple(task_id, t, prompt)
        else:
            store.update_task(task_id, started_at=time.time())
            result = await _run_simple(task_id, t, prompt)
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return   # rejected while it ran — drop the result, stay quiet
        if t["kind"] == "teams_draft":
            store.update_task(task_id, status="awaiting_approval", result=result,
                              finished_at=time.time())
            await notify.notify(
                f"📝 Task #{task_id} ({t['title']}) — draft for Teams chat "
                f"'{t['teams_chat']}':\n\n{result[:600]}\n\n"
                f"Reply 'approve task {task_id}' to send it, or 'reject task {task_id}'.",
                "task")
        elif t["kind"] == "code" and agent:
            await _finish_code(task_id, t, result, hops=0)
        else:
            store.update_task(task_id, status="done", result=result,
                              finished_at=time.time())
            _learn_from(task_id, t["title"], result)
            snippet = result[:400] + ("…" if len(result) > 400 else "")
            waste = _audit_note(task_id)
            await notify.notify(
                f"✅ Task #{task_id} done — {t['title']}\n\n{snippet}\n\n"
                f"(full result: ask 'task {task_id} result' or see the Missions tab)"
                f"{waste}", "task")
    except asyncio.CancelledError:
        # cancel() owns the status and the notification; just stop cleanly.
        raise
    except _LimitPaused as exc:
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _pause_task(task_id, t, exc)
    except Exception as exc:
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        store.update_task(task_id, status="failed", error=str(exc)[:500],
                          finished_at=time.time())
        await notify.notify(f"❌ Task #{task_id} failed — {t['title']}: {str(exc)[:200]}", "task")


def reply(task_id: int, text: str) -> str:
    """Resume a code task paused at its plan gate — 'PLAN APPROVED' or feedback.
    The pipeline continues in its own session with full context."""
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    if t["kind"] != "code" or t["status"] != "awaiting_approval":
        raise ValueError(f"task #{task_id} is not a code task awaiting approval "
                         f"(kind={t['kind']}, status={t['status']})")
    approved = text.strip().upper() == "PLAN APPROVED"
    # Did the plan hold? The cheapest honest measure of planning quality: a plan
    # Arun approves as-is versus one he sends back.
    store.record_outcome("plan", "approved" if approved else "replanned", subject=str(task_id))
    # The plan he just approved is the definition of done — keep it (no-op unless
    # ASTA_TASK_SPEC is on) so a later compacted leg can re-anchor to it.
    if approved:
        from . import task_spec
        task_spec.capture(task_id, t.get("result") or "")
    # Anything buffered by augment() while the task ran rides in now, on the user's
    # gate action — so mid-flight additions land without a session restart.
    full_text = text + _drain_addenda(task_id)
    store.update_task(task_id, status="running")
    job = asyncio.create_task(_resume_worker(task_id, full_text, approved=approved))
    _running[task_id] = job
    job.add_done_callback(lambda _j, tid=task_id: _running.pop(tid, None))
    return (f"Task #{task_id}: plan approved — implementing now."
            if approved
            else f"Task #{task_id}: feedback sent to the pipeline — it will re-plan.")


async def _resume_worker(task_id: int, text: str, approved: bool = False) -> None:
    from . import notify
    t = store.get_task(task_id)
    if not t:
        return
    # Implementation is where quality matters — effort steps up from the
    # planning leg's medium to high only now, so grilling/planning stays cheap.
    _ex = _resolve_executor(task_id)
    effort = _impl_effort(_ex) if approved else _effort_for("code", _ex)
    # Re-anchor an approved implementation leg to the definition of done, so a
    # compacted or fresh session rebuilds against the plan Arun signed off rather
    # than re-deriving one. "" unless ASTA_TASK_SPEC is on and a spec was captured.
    from . import task_spec
    spec_preamble = task_spec.preamble(task_id) if approved else ""
    try:
        async with _ws_lock(t["workspace"]):
            result = await _run_code_leg(task_id, spec_preamble + text + CODE_OVERRIDES,
                                         _cwd(t["workspace"]), resume=True,
                                         effort=effort, workspace=t["workspace"])
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _finish_code(task_id, t, result, hops=0)
    except asyncio.CancelledError:
        raise
    except _LimitPaused as exc:
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _pause_task(task_id, t, exc)
    except Exception as exc:
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        store.update_task(task_id, status="failed", error=str(exc)[:500],
                          finished_at=time.time())
        await notify.notify(f"❌ Task #{task_id} failed — {t['title']}: {str(exc)[:200]}", "task")


def _switchable_brains(exclude: str) -> list[str]:
    """Other executors worth switching a paused task to right now — installed and
    not themselves limited. Empty means waiting is the only option."""
    from . import agent as agent_mod
    out = []
    for spec_name in agent_mod.EXECUTORS:
        ex = agent_mod.exec_name(spec_name)
        if ex == exclude or ex in out:
            continue
        if agent_mod.available(spec_name) and not agent_mod.quota_down(spec_name):
            out.append(ex)
    return out


def _resolve_switch(name: str) -> str:
    """Map a brain name Arun typed ('copilot', 'claude', 'gpt') to an executor
    Asta can actually run a task on, or '' if it names none."""
    n = (name or "").strip().lower()
    if n in _executor_names():
        return n
    from . import agent as agent_mod
    spec = agent_mod.normalize_model(n.replace(" ", "_")) if n else ""
    ex = agent_mod.exec_name(spec) if spec else ""
    return ex if ex in _executor_names() else ""


async def _pause_task(task_id: int, t: dict, exc: _LimitPaused) -> None:
    """A leg hit a transient usage limit. Keep the pinned session and everything
    already done, mark the task paused, and both (a) schedule a durable
    auto-resume for when the brain renews and (b) tell Arun, offering to switch
    brains instead of waiting. The auto-resume survives a restart because the due
    time is persisted, not held in memory — a pause at 3:40pm that a crash would
    otherwise strand still fires."""
    from . import notify
    reset_at = exc.reset_at
    store.kv_set(f"task_paused:{task_id}", _json.dumps(
        {"brain": exc.brain, "reset_at": reset_at, "raw": exc.raw[:300], "at": time.time()}))
    # Auto-resume ONLY when the limit said when it lifts. Claude's session window
    # does ("resets 3:40pm"); Copilot's monthly pool doesn't. Retrying blindly
    # against a pool that won't refill for weeks would just ping Arun every half
    # hour, so a limit with no stated reset waits for him to resume or switch.
    when = ""
    if reset_at:
        store.kv_set(f"task_resume_at:{task_id}", str(reset_at + _RESUME_BUFFER))
        with contextlib.suppress(Exception):
            when = _dt.datetime.fromtimestamp(reset_at).strftime("%-I:%M%p").lower()
    else:
        store.kv_del(f"task_resume_at:{task_id}")
    store.update_task(task_id, status="paused", error=exc.raw[:500])
    alts = _switchable_brains(exc.brain)
    switch_line = (f"\nOr reply “task {task_id} use {alts[0]}” to switch brains and carry "
                   f"on now (a fresh session on {alts[0]} — it rebuilds context from the "
                   f"repo)." if alts else "")
    lead = (f"Nothing is lost. I'll auto-resume on {exc.brain} at ~{when} and pick up "
            f"exactly where it stopped. Say “resume task {task_id}” to try sooner." if when
            else f"Nothing is lost — the pinned session is kept. Say “resume task {task_id}” "
                 f"when {exc.brain} is back and I'll pick up exactly where it stopped.")
    await notify.notify(
        f"⏸ Task #{task_id} paused — {exc.brain} hit its usage limit"
        + (f" (renews ~{when})" if when else "") + ".\n" + lead + switch_line, "task")


async def resume_task(task_id: int, switch_to: str = "") -> str:
    """Resume a paused (or transiently-failed) code task from where it stopped.

    Same brain → re-attaches the pinned CLI session with --resume, so nothing is
    re-discovered. switch_to → moves it to another brain first; that brain can't
    inherit the old session, so it starts fresh and rebuilds context from the
    repo (handoff.md / todos.md / git), which is still far better than dropping
    the task. Used by both the manual command and the auto-resume sweep."""
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    if t["kind"] != "code":
        raise ValueError(f"task #{task_id} isn't a resumable code task ({t['kind']})")
    if t["status"] not in ("paused", "failed"):
        raise ValueError(f"task #{task_id} is {t['status']} — nothing to resume")
    if is_running(task_id):
        return f"Task #{task_id} is already running."
    note = ""
    if switch_to:
        ex = _resolve_switch(switch_to)
        if not ex:
            raise ValueError(f"“{switch_to}” isn't a brain I can run tasks on")
        store.kv_set(f"task_executor:{task_id}", ex)   # a fresh session opens for it
        note = f" on {ex}"
    store.kv_del(f"task_resume_at:{task_id}")
    store.kv_del(f"task_paused:{task_id}")
    store.update_task(task_id, status="running", error="")
    prompt = ("Resume: your session was paused mid-task when the brain hit a usage "
              "limit — this is the same task continuing, not a new one. Check "
              "`git log --oneline -5` and `git status` first so you don't redo "
              "finished work, then carry on from the next unfinished step.")
    job = asyncio.create_task(_resume_worker(task_id, prompt, approved=True))
    _running[task_id] = job
    job.add_done_callback(lambda _j, tid=task_id: _running.pop(tid, None))
    return f"Task #{task_id}: resuming{note} from where it stopped."


async def _resume_due(now: float | None = None) -> list[int]:
    """Auto-resume every paused task whose brain-limit has now lifted; returns the
    ids kicked. Split from the loop so it can be driven directly in a test."""
    from . import notify
    now = time.time() if now is None else now
    kicked: list[int] = []
    for t in store.list_tasks(limit=50):
        if t["status"] != "paused" or is_running(t["id"]):
            continue
        raw = store.kv_get(f"task_resume_at:{t['id']}")
        try:
            due = float(raw) if raw else None
        except (TypeError, ValueError):
            due = None
        if due is None or now < due:
            continue
        kicked.append(t["id"])
        with contextlib.suppress(Exception):
            await notify.notify(
                f"▶️ Auto-resuming task #{t['id']} — {t['title'][:50]} (brain renewed).", "task")
        with contextlib.suppress(Exception):
            await resume_task(t["id"])
    return kicked


async def resume_paused_loop(interval: int = 60) -> None:
    """Background sweep: resume paused tasks once their brain is back. Durable —
    the due time lives in the store, so a task paused before a restart is still
    picked up after one."""
    while True:
        try:
            await asyncio.sleep(interval)
            await _resume_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def approve(task_id: int) -> str:
    """Approve a paused task: teams_draft → send it; code → continue the pipeline."""
    from . import notify, teams_bridge
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    if t["kind"] == "code":
        # 'approve' at the early context gate means "your reading is right, go" —
        # not a plan approval (no plan exists yet). At the plan gate it means
        # implement.
        if store.kv_get(f"task_gate:{task_id}") == "context":
            return reply(task_id, "Your understanding is correct — proceed to "
                                  "discovery and planning.")
        return reply(task_id, "PLAN APPROVED")
    if t["kind"] != "teams_draft" or t["status"] != "awaiting_approval":
        raise ValueError(f"task #{task_id} is not a draft awaiting approval "
                         f"(kind={t['kind']}, status={t['status']})")
    try:
        await teams_bridge.send_message(t["teams_chat"], t["result"])
    except RuntimeError as exc:
        msg = ("Teams session expired — run: python -m app.teams_bridge login"
               if "SESSION_EXPIRED" in str(exc) else str(exc)[:300])
        store.update_task(task_id, status="send_failed", error=msg)
        await notify.notify(f"❌ Task #{task_id}: sending to '{t['teams_chat']}' failed — {msg}", "task")
        return f"Send failed: {msg}"
    store.update_task(task_id, status="sent")
    store.record_outcome("draft", "sent_unedited", subject=str(task_id))
    await notify.notify(f"📨 Task #{task_id}: message sent to Teams chat '{t['teams_chat']}'.", "task")
    return f"Sent to '{t['teams_chat']}'."




async def ship(task_id: int) -> str:
    """Push the pipeline's committed feature branch(es) and open PRs — one per
    repo the task touched. Only ever triggered by Arun after reviewing the diff."""
    from . import notify
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    if t["kind"] != "code" or t["status"] != "done":
        raise ValueError(f"task #{task_id} is not a finished code task "
                         f"(kind={t['kind']}, status={t['status']})")
    root = Path(_cwd(t["workspace"]))
    repos = [root] if (root / ".git").is_dir() else \
        sorted(p for p in root.iterdir() if (p / ".git").is_dir())
    urls: list[str] = []
    for repo in repos:
        rc, cur = await repo_ops.git(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
        cur = cur.strip()
        if rc != 0 or cur in repo_ops.BASE_BRANCHES:
            continue
        rc, dirty = await repo_ops.git(repo, "git", "status", "--porcelain")
        if dirty.strip():
            # Stage 4 test files sometimes stay uncommitted — fold them in.
            await repo_ops.git(repo, "git", "add", "-A")
            rc, out = await repo_ops.git(repo, "git", "commit", "-m", t["title"])
            if rc != 0:
                raise RuntimeError(f"{repo.name}: commit failed: {out[:200]}")
        rc, ahead = await repo_ops.git(repo, "git", "log", "--oneline", f"origin/{cur}..HEAD")
        if rc == 0 and not ahead.strip():
            continue   # branch exists remotely and has nothing new
        rc, out = await repo_ops.git(repo, "git", "push", "-u", "origin", cur, timeout=300)
        if rc != 0:
            raise RuntimeError(f"{repo.name}: push failed: {out[:300]}")
        rc, out = await repo_ops.git(repo, "gh", "pr", "create", "--fill", "--head", cur, timeout=300)
        if rc != 0 and "already exists" not in out:
            raise RuntimeError(f"{repo.name}: gh pr create failed: {out[:300]}")
        import re as _re
        mu = _re.search(r"https://github\.com/\S+/pull/\d+", out)
        if not mu:
            rc2, out2 = await repo_ops.git(repo, "gh", "pr", "view", "--json", "url", "--jq", ".url")
            urls.append(f"{repo.name}: {out2.strip() if rc2 == 0 else '(PR url unavailable)'}")
        else:
            urls.append(f"{repo.name}: {mu.group(0)}")
    if not urls:
        raise RuntimeError("no unpushed feature branch found — nothing to ship")
    store.record_outcome("ship", "pr_opened", subject=str(task_id), detail="; ".join(urls))
    msg = f"🔀 Task #{task_id} shipped:\n" + "\n".join("• " + u for u in urls)
    await notify.notify(msg, "action", urgency="direct")
    return msg


async def reject(task_id: int) -> str:
    """Reject a task AND stop it. Rejecting used to be cosmetic — the worker kept
    running, kept spending, and finished by marking itself done."""
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    store.record_outcome("task", "rejected", subject=str(task_id))
    killed = await cancel(task_id, status="rejected")
    if not killed:
        store.update_task(task_id, status="rejected")
    return (f"Task #{task_id} rejected and its worker killed."
            if killed else f"Task #{task_id} rejected (it had already finished).")
