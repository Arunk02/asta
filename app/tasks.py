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
import json as _json
import os
import re
import time
import uuid
from pathlib import Path

from . import claude_cli, copilot_cli, store, workspace_tools

ROOT = Path(__file__).resolve().parent.parent
TASK_TIMEOUT = {"analysis": 900, "code": 1800, "teams_draft": 300}

# Workspace tasks run the workspace's own .github/agents pipelines (per-workspace
# by design — nothing at global level). The solo agent already does everything
# Asta used to prompt by hand, but with contmark discipline: resolver instead of
# grepping, reads only matched lines, plans behind a human gate, learns into
# lessons.md via its Stage 5 evolution loop. Asta's job shrinks to launching it,
# relaying its gates to Arun, and shipping the PR afterwards. Analysis goes
# through the SAME agent (its Stage 0 inquiry mode) — the lighter explore agent
# was dropped 2026-07-21: no Boot 0, no lessons, not enough context to be useful.
CODE_AGENT = "contmark.solo.copilot"

# Small ad-hoc changes skip the whole solo ceremony (Boot 0 artifacts, stages,
# reviews) — for a 7-line edit the ritual was 85% of the bill. The micro agent
# is ~25 turns end-to-end and ESCALATEs back here the moment scope grows.
MICRO_AGENT = "contmark.micro"
_JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")

# The solo pipeline ends in a PR; Arun's flow ends at a reviewed diff. This
# rider turns the interactive gates into pause points and cuts the pipeline
# off before it can publish anything.
CODE_OVERRIDES = """

[Asta runtime overrides — obey exactly]
- Boot 0 efficiency: run `sh .contmark/boot.sh "<key nouns>"` as ONE terminal
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
        from . import token_audit
        rep = token_audit.audit_task(task_id)
        if not rep:
            return ""
        if rep["waste_ratio"] >= 0.12:
            return (f"\n\n⚠️ token audit: {rep['waste_ratio']:.0%} avoidable "
                    f"(~{rep['avoidable_tokens']:,} tok), biggest = {rep['top_fix']}. "
                    f"Grade {rep['grade']}.")
        return f"\n\n📉 token audit: {rep['waste_ratio']:.0%} waste, grade {rep['grade']}."
    except Exception:
        return ""


def _agent_for(t: dict) -> str:
    """Workspace tasks run the workspace's contmark solo agent (code = full
    pipeline, analysis = its inquiry mode); tasks without a workspace (e.g. on
    asta itself) have no .contmark, and the solo agent hard-stops without
    one — those keep the plain prompt path."""
    if not t["workspace"] or t["kind"] not in ("code", "analysis"):
        return ""
    return CODE_AGENT


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


_QUOTA_DOWN_KEY = "copilot_quota_down"     # timestamp of last quota refusal
_QUOTA_DOWN_TTL = 48 * 3600                # self-heals after the monthly reset


def _copilot_quota_down() -> bool:
    ts = store.kv_get(_QUOTA_DOWN_KEY)
    try:
        return bool(ts) and time.time() - float(ts) < _QUOTA_DOWN_TTL
    except ValueError:
        return False


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
    return MICRO_AGENT if _pipeline_for(task_id) == "micro" else CODE_AGENT


def _claude_agent_file(workspace: str | None, kind: str, pipeline: str = "full") -> str:
    """The contmark pipeline file Claude runs — same agents, claude flavour
    (analysis uses the solo agent's inquiry mode; micro is executor-neutral)."""
    name = ("contmark.micro" if kind == "code" and pipeline == "micro"
            else "contmark.solo.claude")
    f = Path(_cwd(workspace)) / ".github" / "agents" / f"{name}.agent.md"
    return str(f) if f.is_file() else ""


def _is_quota_err(exc: Exception) -> bool:
    return "quota" in str(exc).lower()


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
    sid_key = f"task_session:{task_id}:{ex}"
    sid = store.kv_get(sid_key)
    if not sid:
        sid, resume = str(uuid.uuid4()), False
        store.kv_set(sid_key, sid)
    watcher = _progress_watcher(task_id, (store.get_task(task_id) or {}).get("title", ""))
    if ex == "claude":
        return await claude_cli.one_shot(
            prompt, cwd=cwd, timeout=TASK_TIMEOUT["code"],
            agent_file=_claude_agent_file(workspace, "code", _pipeline_for(task_id)),
            effort=effort, session_id=sid, resume=resume, on_progress=watcher)
    try:
        return await copilot_cli.one_shot(
            prompt, cwd=cwd, timeout=TASK_TIMEOUT["code"], agent=_code_agent(task_id),
            effort=effort, session_id=sid, resume=resume, on_progress=watcher)
    except RuntimeError as exc:
        if not _is_quota_err(exc):
            raise
        store.kv_set(_QUOTA_DOWN_KEY, str(time.time()))
        if resume or not claude_cli.available():
            raise RuntimeError(
                "Copilot quota exhausted mid-task — reject this task and respawn "
                "it; new tasks run on claude automatically.") from exc
        store.kv_set(f"task_executor:{task_id}", "claude")
        store.kv_del(sid_key)
        return await _run_code_leg(task_id, prompt, cwd, resume=False,
                                   effort=effort, workspace=workspace)


async def _run_simple(task_id: int, t: dict, prompt: str) -> str:
    """Non-pipeline kinds (analysis, teams_draft, agent-less code): one leg,
    executor-aware, with transparent claude failover when copilot's quota dies."""
    ex = _resolve_executor(task_id)
    cwd, tout = _cwd(t["workspace"]), TASK_TIMEOUT[t["kind"]]
    agent = _agent_for(t)
    eff = _effort_for(t["kind"], ex)
    agent_file = _claude_agent_file(t["workspace"], t["kind"]) if agent else ""
    if ex == "claude":
        return await claude_cli.one_shot(prompt, cwd=cwd, timeout=tout,
                                         agent_file=agent_file, effort=eff)
    try:
        return await copilot_cli.one_shot(prompt, cwd=cwd, timeout=tout,
                                          agent=agent, effort=eff)
    except RuntimeError as exc:
        if not _is_quota_err(exc) or not claude_cli.available():
            raise
        store.kv_set(_QUOTA_DOWN_KEY, str(time.time()))
        store.kv_set(f"task_executor:{task_id}", "claude")
        return await claude_cli.one_shot(prompt, cwd=cwd, timeout=tout,
                                         agent_file=agent_file, effort=eff)


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
            "Resume from .contmark/todos.md + handoff.md — continue with the next "
            "repo." + CODE_OVERRIDES,
            _cwd(t["workspace"]), resume=False,
            effort=_impl_effort(_resolve_executor(task_id)),
            workspace=t["workspace"])
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _finish_code(task_id, t, result2, hops + 1)
        return
    store.update_task(task_id, status="done", result=result, finished_at=time.time())
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
            from .missions import playbook_block
            prompt += playbook_block(Path(_cwd(t["workspace"])))
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
            snippet = result[:400] + ("…" if len(result) > 400 else "")
            waste = _audit_note(task_id)
            await notify.notify(
                f"✅ Task #{task_id} done — {t['title']}\n\n{snippet}\n\n"
                f"(full result: ask 'task {task_id} result' or see the Missions tab)"
                f"{waste}", "task")
    except asyncio.CancelledError:
        # cancel() owns the status and the notification; just stop cleanly.
        raise
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
    try:
        async with _ws_lock(t["workspace"]):
            result = await _run_code_leg(task_id, text + CODE_OVERRIDES,
                                         _cwd(t["workspace"]), resume=True,
                                         effort=effort, workspace=t["workspace"])
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _finish_code(task_id, t, result, hops=0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        store.update_task(task_id, status="failed", error=str(exc)[:500],
                          finished_at=time.time())
        await notify.notify(f"❌ Task #{task_id} failed — {t['title']}: {str(exc)[:200]}", "task")


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
    await notify.notify(f"📨 Task #{task_id}: message sent to Teams chat '{t['teams_chat']}'.", "task")
    return f"Sent to '{t['teams_chat']}'."


_BASE_BRANCHES = ("main", "master", "develop")


async def ship(task_id: int) -> str:
    """Push the pipeline's committed feature branch(es) and open PRs — one per
    repo the task touched. Only ever triggered by Arun after reviewing the diff."""
    from . import notify
    from .missions import _git
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
        rc, cur = await _git(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
        cur = cur.strip()
        if rc != 0 or cur in _BASE_BRANCHES:
            continue
        rc, dirty = await _git(repo, "git", "status", "--porcelain")
        if dirty.strip():
            # Stage 4 test files sometimes stay uncommitted — fold them in.
            await _git(repo, "git", "add", "-A")
            rc, out = await _git(repo, "git", "commit", "-m", t["title"])
            if rc != 0:
                raise RuntimeError(f"{repo.name}: commit failed: {out[:200]}")
        rc, ahead = await _git(repo, "git", "log", "--oneline", f"origin/{cur}..HEAD")
        if rc == 0 and not ahead.strip():
            continue   # branch exists remotely and has nothing new
        rc, out = await _git(repo, "git", "push", "-u", "origin", cur, timeout=300)
        if rc != 0:
            raise RuntimeError(f"{repo.name}: push failed: {out[:300]}")
        rc, out = await _git(repo, "gh", "pr", "create", "--fill", "--head", cur, timeout=300)
        if rc != 0 and "already exists" not in out:
            raise RuntimeError(f"{repo.name}: gh pr create failed: {out[:300]}")
        import re as _re
        mu = _re.search(r"https://github\.com/\S+/pull/\d+", out)
        if not mu:
            rc2, out2 = await _git(repo, "gh", "pr", "view", "--json", "url", "--jq", ".url")
            urls.append(f"{repo.name}: {out2.strip() if rc2 == 0 else '(PR url unavailable)'}")
        else:
            urls.append(f"{repo.name}: {mu.group(0)}")
    if not urls:
        raise RuntimeError("no unpushed feature branch found — nothing to ship")
    msg = f"🔀 Task #{task_id} shipped:\n" + "\n".join("• " + u for u in urls)
    await notify.notify(msg, "action", urgency="direct")
    return msg


async def reject(task_id: int) -> str:
    """Reject a task AND stop it. Rejecting used to be cosmetic — the worker kept
    running, kept spending, and finished by marking itself done."""
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    killed = await cancel(task_id, status="rejected")
    if not killed:
        store.update_task(task_id, status="rejected")
    return (f"Task #{task_id} rejected and its worker killed."
            if killed else f"Task #{task_id} rejected (it had already finished).")
