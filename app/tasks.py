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
#: Whether a finished code task reads its own diff before handing it over.
#: On by default: the reviewer already exists, and Arun reading every diff by
#: hand is what makes him the bottleneck on the work this exists to take off him.
REVIEW_OWN_DIFF = os.environ.get("ASTA_REVIEW_OWN_DIFF", "1").strip().lower() \
    not in ("0", "false", "no", "off")

#: Per-kind ceilings. `code` was 1800s — and the measured baseline for real code
#: tasks is median 7.7 min with a **p90 of 32 min** (n=46), so the ceiling sat
#: BELOW the p90 and killed roughly the slowest tenth of tasks with their own
#: budget. A limit that fires on work which was going to succeed is not a safety
#: net, it is a source of repeated work: the task is re-run from nothing and pays
#: the whole cost again.
#:
#: 45 min clears the measured p90 with room, and the idle watchdog
#: (`turn_budget`) is what actually catches a wedged brain now — which is the job
#: this number was doing badly. A stuck turn is stopped after two minutes of
#: silence regardless of how much ceiling is left.
TASK_TIMEOUT = {"analysis": 900, "code": 2700, "teams_draft": 300}

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
- THE PLAN GATE IS UNCONDITIONAL. Every code task stops for Arun's approval
  before a single line is implemented — a one-line constant change included.
  There is no size below which you may proceed on your own, and no "obviously
  trivial" exemption. Print the plan and END the response.
  This replaces an earlier rule that let small changes auto-proceed. He removed
  it deliberately: the cost of stopping is a few minutes, and the cost of not
  stopping is discovering at the end that the whole thing was built on a
  misread of what he wanted. He would rather correct the plan than the diff.
  The plan he approves is the definition of done — implement THAT, not a better
  idea you have afterwards. If implementing reveals the plan was wrong, stop
  and say so; do not quietly build something else.
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
  AI/assistant name, or a "Generated with …" line.
- Branch: Asta has already put every repo on the task's feature branch, cut
  fresh from develop. Do NOT create another branch, do not switch branches, and
  do not rebase onto anything. If `git status` shows an unexpected branch, stop
  and say so rather than fixing it yourself.

[How Arun expects the code to read — he reviews every diff]
- Small, named, single-purpose functions. A function that needs a comment to
  explain WHAT it does should be two functions with better names. He has to
  debug this at 11pm; a forty-line branch-heavy method is where that goes wrong.
- Prefer a functional shape: take arguments, return a value, no hidden state.
  Pure helpers where the logic is real (mapping, filtering, deciding), effects
  pushed to the edges. Avoid mutating a parameter to communicate a result.
- Name things after the domain, not the mechanism: `cancelledBookingsSkipTms`,
  not `processFlag2`. Method names say what is true after they run.
- Comments explain WHY — the constraint, the bug that forced it, the thing the
  next reader would otherwise undo. Never restate the code in English.
- SIMPLIFY. The smallest change that fully solves it wins. No layer, interface,
  factory, config switch or generalisation that this task does not need — do not
  build for a second caller that does not exist. If you find yourself adding
  indirection "for later", stop: he would rather change simple code twice.
- Delete what you replace. A dead branch left behind is a future bug.
- Guard clauses over nesting; early return over an else-tree three deep.
- If the same logic already exists in the repo, call it. Do not write a second
  copy with a different name — that is how the two drift apart.
- Tests are part of the change, not a follow-up: cover the new behaviour AND the
  case that used to work and must still work. A test that cannot fail is worse
  than no test."""

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
_ws_locks: dict[str, asyncio.Semaphore] = {}

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

# Statuses a task can still be WORKED ON from, as opposed to still running.
#
# The bug this exists for: a code task went to "done" the moment the diff was
# written, and "done" was the end of it. Feedback arriving afterwards found no
# live task, so it was handled as a brand new request — a fresh worker, a fresh
# session, none of the context of the thing it was feedback ABOUT, and the work
# re-derived from nothing. Every one of these can instead resume the task's own
# session, which is where all of that context still is.
REFINABLE = ("done", "shipped", "failed", "pr_changes_requested", "pr_ci_failed")

# The PR is open and its fate is not yet decided. A task sitting here is not
# finished — CI can still go red, review can still ask for changes, and both of
# those belong to the task that produced the branch.
SHIPPED_STATUSES = ("shipped", "pr_ci_failed", "pr_changes_requested")

# Actually over. Merged is the only success; the rest are ways of stopping.
CLOSED_STATUSES = ("merged", "pr_closed", "rejected", "cancelled")

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
    """Every task from this conversation that is still live, newest last.

    The link is NOT dropped when a task finishes. It used to be — this function
    rewrote `conv_tasks` to the live subset on every call — and that quietly
    removed the only trail from a conversation back to the work it had just
    produced. Feedback a minute later found nothing to attach to and became a
    new task, which is the reprocessing Arun kept hitting. Only genuinely
    closed tasks are forgotten now; finished-but-refinable ones stay reachable.
    """
    ids = _linked_ids(conv_id)
    keep = [i for i in ids
            if (store.get_task(i) or {}).get("status") not in CLOSED_STATUSES]
    if keep != ids:
        store.kv_set(f"conv_tasks:{conv_id}", _json.dumps(keep))
    return [i for i in keep
            if (store.get_task(i) or {}).get("status") in LIVE_STATUSES]


def refinable_for(conv_id: str, now: float | None = None) -> list[int]:
    """Recently-finished tasks from this conversation that feedback can continue.

    Newest last, and windowed: a correction arriving four days later is much
    more likely to be new work than a comment on the old change.
    """
    now = time.time() if now is None else now
    out = []
    for i in _linked_ids(conv_id):
        t = store.get_task(i) or {}
        if t.get("kind") != "code" or t.get("status") not in REFINABLE:
            continue
        ended = t.get("finished_at") or t.get("created_at") or 0
        if ended and now - ended <= REFINE_WINDOW_SECONDS:
            out.append(i)
    return out


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


def _ws_lock(workspace: str | None) -> asyncio.Semaphore:
    """How many code tasks may run in this workspace at once.

    This was an `asyncio.Lock` held for up to thirty minutes, so two tickets in
    the same repo ran strictly one after the other. It existed because tasks
    shared one checkout and would fight over the branch, the index and the
    working tree — and with `worktrees` they no longer share anything.

    A semaphore rather than nothing at all: each task is a full checkout plus a
    CLI subprocess plus, at the gate, a Maven build. The limit is the machine, not
    the git model, and Arun is working on this laptop while they run.
    """
    from . import worktrees
    key = workspace or "_root"
    if key not in _ws_locks:
        _ws_locks[key] = asyncio.Semaphore(max(1, worktrees.MAX_PARALLEL))
    return _ws_locks[key]


def _cwd(workspace: str | None) -> str:
    """Where a task runs. Falls back to Asta's own root for workspace-less work.

    That fallback is fine for an analysis task, which only reads — and was very
    much not fine for a code task. A code task with no workspace once ran real
    git commands in Asta's own repository and moved a branch carrying five
    unpushed commits. `code_cwd` below is the version code tasks must use.
    """
    if workspace and workspace in workspace_tools.WORKSPACES:
        return str(workspace_tools.WORKSPACES[workspace])
    return str(ROOT)


def task_cwd(task_id: int, workspace: str | None) -> str:
    """Where THIS task works: its own checkout when it has one.

    A task with a worktree never touches the shared checkout, which is what lets
    several run at once — and what stops Arun's editor moving underneath him.
    Falls back to the workspace itself for tasks created before worktrees existed
    and for repos where one could not be made.
    """
    from . import worktrees
    root = Path(code_cwd(workspace))
    if worktrees.exists(root, task_id):
        return str(worktrees.root_for(root, task_id))
    return str(root)


def code_cwd(workspace: str | None) -> str:
    """Where a CODE task runs — or a refusal.

    A code task with nowhere to work is a bug in whoever created it, not a task
    to run somewhere convenient. The old behaviour silently chose Asta's own
    repository, which is how a branch with five unpushed commits got moved by a
    task that had nothing to do with this project.

    A guard was added at the time to the one path that caused it. This is the
    mechanism instead of the symptom: every code path that resolves a working
    directory for a code task comes through here.
    """
    if not workspace:
        # One registered workspace and no ambiguity about which: use it rather
        # than refuse. Refusing outright would be correct and useless — the only
        # place the task could possibly mean is the only place there is.
        known = sorted(workspace_tools.WORKSPACES)
        if len(known) == 1:
            return str(workspace_tools.WORKSPACES[known[0]])
        raise RuntimeError(
            "this code task has no workspace, and there is more than one to choose "
            "from — refusing to guess, and refusing to fall back to Asta's own "
            f"repository. Name one of: {', '.join(known) or 'none registered'}."
            if known else
            "this code task has no workspace and none is registered — refusing to "
            "run it against Asta's own repository. Register the workspace first.")
    if workspace not in workspace_tools.WORKSPACES:
        known = ", ".join(sorted(workspace_tools.WORKSPACES)) or "none registered"
        raise RuntimeError(f"unknown workspace '{workspace}' — known: {known}")
    return str(workspace_tools.WORKSPACES[workspace])


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


async def cancel(task_id: int, status: str = "cancelled", why: str = "") -> bool:
    """Stop a running worker and kill its copilot process. True if one was killed."""
    job = _running.get(task_id)
    if not job or job.done():
        return False
    t = store.get_task(task_id) or {}
    job.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await job
    store.update_task(task_id, status=status, finished_at=time.time())
    learn_from_stop(task_id, t, status, why)
    return True


def learn_from_stop(task_id: int, t: dict, status: str, why: str = "") -> None:
    """Learn from a run Arun stopped.

    Nothing used to. `should_extract` required a status of done or sent, so 39%
    of code tasks — the largest category after success — taught nothing, while a
    run that merely needed two attempts taught something. Exactly backwards: a
    task he kills is him saying "you misread what I wanted", within minutes of it
    happening, which is the most informative thing that occurs all day.

    Fire-and-forget, like every other extraction: he has already moved on, and a
    slow distillation must not hold up the cancel he just asked for.
    """
    from . import learn
    if not learn.should_extract(_rounds(task_id), _escalated(task_id), status):
        return
    asked = (t.get("prompt") or "")[:2000]
    did = (t.get("result") or "")[-2000:]
    transcript = (f"WHAT ARUN ASKED FOR:\n{asked}\n\n"
                  f"WHAT ASTA HAD DONE WHEN HE STOPPED IT:\n{did or '(nothing recorded yet)'}"
                  + (f"\n\nWHY HE STOPPED IT, IN HIS OWN WORDS:\n{why[:1000]}" if why else
                     "\n\n(He gave no reason — infer it from the gap above, and say so if "
                     "you cannot.)"))
    with contextlib.suppress(RuntimeError):        # no loop in a sync caller
        asyncio.get_running_loop().create_task(
            learn.extract(t.get("title") or f"task #{task_id}", transcript,
                          outcome=status, escalated=_escalated(task_id)))


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
        # Reported against this brain's OWN history rather than as a bare
        # percentage with a fixed letter beside it. The old line said "7% waste,
        # grade B (ok)" on a run that was among the worst ever measured — the
        # bands were set before any data existed and no real run has ever left
        # A or B, so the letter carried no information and the word "ok" was
        # actively misleading.
        verdict = rep.get("verdict") or f"{rep['waste_ratio']:.0%} avoidable"
        worse = "WORSE" in verdict
        icon = "⚠️" if worse or rep["waste_ratio"] >= 0.12 else "📉"
        note = f"\n\n{icon} token audit: {verdict}"
        if worse or rep["waste_ratio"] >= 0.12:
            note += (f" · ~{rep['avoidable_tokens']:,} tok, "
                     f"biggest = {rep['top_fix']}")
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


# --- what a task did to git, and how to undo it -------------------------------

def _repos_under(root: Path) -> list[Path]:
    """Every git repo a task could touch: the workspace, or the repos inside it."""
    if (root / ".git").is_dir():
        return [root]
    try:
        return sorted(p for p in root.iterdir() if (p / ".git").is_dir())
    except OSError:
        return []


async def mark_rollback_point(task_id: int, workspace: str | None) -> dict:
    """Record where every repo stood before the task touched it.

    A task commits, branches and moves HEAD, and nothing recorded what it did in
    a form that could be reversed — recovery from the branch incident was manual
    reflog archaeology. Ten lines here turn a bad run from an incident into an
    inconvenience, which is the difference between a tool he supervises and one
    he can let run.
    """
    try:
        root = Path(code_cwd(workspace))
    except RuntimeError:
        return {}
    marks: dict = {}
    for repo in _repos_under(root):
        rc_b, branch = await repo_ops.git(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
        rc_s, sha = await repo_ops.git(repo, "git", "rev-parse", "HEAD")
        if rc_b or rc_s:
            continue
        marks[repo.name] = {"branch": branch.strip(), "sha": sha.strip(),
                            "path": str(repo)}
    if marks:
        store.kv_set(f"task_rollback:{task_id}", _json.dumps(marks))
    return marks


def rollback_point(task_id: int) -> dict:
    raw = store.kv_get(f"task_rollback:{task_id}")
    try:
        return _json.loads(raw) if raw else {}
    except ValueError:
        return {}


async def rollback(task_id: int) -> str:
    """Put every repo back where it stood before this task ran.

    Deliberately does NOT delete the task's branch — the work is still there to
    look at, it simply stops being checked out. Undo should be reversible too.
    """
    # A task with its own checkout never moved the shared one, so undoing it is
    # removing a directory rather than resetting a branch Arun may be standing
    # on. Strictly the safer operation, and the common case now.
    from . import worktrees
    t = store.get_task(task_id) or {}
    try:
        ws_root = Path(code_cwd(t.get("workspace")))
    except RuntimeError:
        ws_root = None
    if ws_root is not None and worktrees.exists(ws_root, task_id):
        notes = await worktrees.remove(ws_root, task_id)
        kept = [n for n in notes if "uncommitted" in n]
        if kept:
            return ("Kept, because the work is only there: " + "; ".join(kept)
                    + " — commit or discard it, then ask again.")
        return f"Removed task #{task_id}'s checkout. Your own tree never moved. " + "; ".join(notes)

    marks = rollback_point(task_id)
    if not marks:
        return f"No rollback point for task #{task_id} — nothing to undo."
    done, failed = [], []
    for name, mark in marks.items():
        repo = Path(mark["path"])
        rc_d, dirty = await repo_ops.git(repo, "git", "status", "--porcelain")
        if rc_d == 0 and dirty.strip():
            failed.append(f"{name}: uncommitted changes — left alone")
            continue
        rc, out = await repo_ops.git(repo, "git", "checkout", mark["branch"])
        if rc != 0:
            failed.append(f"{name}: {out.strip()[:80]}")
            continue
        rc, out = await repo_ops.git(repo, "git", "reset", "--hard", mark["sha"])
        if rc != 0:
            failed.append(f"{name}: {out.strip()[:80]}")
            continue
        done.append(f"{name} → {mark['branch']} @ {mark['sha'][:8]}")
    parts = []
    if done:
        parts.append("Restored: " + "; ".join(done))
    if failed:
        parts.append("Could not restore: " + "; ".join(failed))
    return " · ".join(parts) or "Nothing to restore."


# --- reading its own work before handing it over ------------------------------

async def _self_review(task_id: int, t: dict, result: str) -> str:
    """Read the diff this task just produced, the way it reads anyone else's PR.

    `review.py` gathers a diff, its checks and the project conventions and
    produces real reviewer notes — and was only ever pointed at OTHER people's
    pull requests. The code Asta itself wrote went to a PR unread by Asta, with
    Arun's own eyes as the only safety net. That does not scale: it makes him the
    bottleneck on exactly the work this exists to take off him.

    Returns a short note to append to the completion message, or "" when there is
    nothing to say. Never raises and never blocks completion — a review that
    fails is worth less than the diff it was reviewing.
    """
    from . import review
    if not REVIEW_OWN_DIFF:
        return ""
    try:
        root = Path(code_cwd(t.get("workspace")))
    except RuntimeError:
        return ""
    diffs = []
    for repo in _repos_under(root):
        mark = rollback_point(task_id).get(repo.name)
        base = mark["sha"] if mark else "HEAD~1"
        rc, out = await repo_ops.git(repo, "git", "diff", "--stat", base, "HEAD")
        if rc == 0 and out.strip():
            rc2, full = await repo_ops.git(repo, "git", "diff", base, "HEAD")
            if rc2 == 0 and full.strip():
                diffs.append((repo.name, out.strip(), full))
    if not diffs:
        return ""
    try:
        notes = await review.review_own_diff(
            "\n\n".join(f"### {name}\n{full[:20000]}" for name, _stat, full in diffs),
            t.get("workspace") or "")
    except Exception:
        return ""
    if not notes:
        return ""
    stat = " · ".join(f"{name}: {s.splitlines()[-1].strip()}" for name, s, _ in diffs)
    return f"\n\n🔍 I read my own diff ({stat}):\n{notes[:900]}"


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
            task_id, t["prompt"] + CODE_OVERRIDES, task_cwd(task_id, t["workspace"]),
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
            task_cwd(task_id, t["workspace"]), resume=False,
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
    own = await _self_review(task_id, t, result)
    await notify.notify(
        f"✅ DONE — #{task_id} {t['title']}\n\n{_phone_text(result, 700)}{own}\n\n"
        f"Diff is local only — nothing pushed. Say 'ship' when you're happy, "
        f"or just reply with changes and I'll continue THIS task rather than "
        f"starting a new one."
        f"{waste}", "task")


def task_branch(t: dict, task_id: int) -> str:
    """The branch this task's work belongs on.

    Named after the ticket when there is one — `feature/BEPTELIKOS-10159` reads
    the same in the branch list, the PR title and Jira, so the three can be
    lined up without asking anyone. The key is taken from the title or the
    prompt, since he raises tasks by pasting a ticket id into either.
    """
    key = _JIRA_KEY.search(f"{t.get('title', '')} {t.get('prompt', '')}")
    return repo_ops.branch_name(key.group(0) if key else "",
                                t.get("title", ""), task_id)


async def _prepare_branches(task_id: int, t: dict) -> list[dict]:
    """Put every repo in the workspace on a fresh feature branch off develop.

    Arun's rule: coding always starts from develop, never from wherever the
    working tree happened to be left. Without this a task inherits the previous
    task's feature branch, and its PR then carries the previous task's commits.

    Reported, never raised: one repo that cannot be prepared must not kill a
    multi-repo run, and he is told which one and why.
    """
    # Where everything stood before this task touched git. Taken here because
    # this is the first thing in a code task that moves a branch — after this
    # point "put it back" needs a record, and there was none.
    await mark_rollback_point(task_id, t.get("workspace"))
    from . import notify
    # A task with no workspace is NOT a task with no repo — `_cwd(None)` falls
    # back to ROOT, which is Asta's own checkout. Branching there means the
    # running process rewrites the branch it is executing from, mid-session.
    #
    # That is not hypothetical: it happened. A task titled "fix bug" with no
    # workspace cut `feature/asta-1-fix-bug`, and the reflog shows the whole
    # sequence — `checkout: moving from feature/agentic-loop to main`,
    # `pull --ff-only`, `checkout: moving from main to feature/asta-1-fix-bug`
    # — while five commits of unpushed work sat on the branch it left. Nothing
    # was lost, because git does not lose commits, but the working tree moved
    # underneath an editor and a test run at the same time.
    #
    # So: no workspace, no branching. And never this repo, even if a workspace
    # somehow resolves to it.
    if not t.get("workspace"):
        return []
    root = Path(_cwd(t.get("workspace")))
    if not root.exists() or root.resolve() == ROOT.resolve():
        return []
    repos = [root] if (root / ".git").is_dir() else \
        sorted(p for p in root.iterdir() if (p / ".git").is_dir())
    repos = [r for r in repos if r.resolve() != ROOT.resolve()]
    if not repos:
        return []
    branch = task_branch(t, task_id)

    # A private checkout per repo rather than moving the shared one. Two tasks
    # can then run at the same time — which is the whole point — and the checkout
    # Arun has open in his editor is never touched. `start_branch` remains for
    # anything that genuinely needs the shared tree.
    from . import worktrees
    try:
        results = await worktrees.create(root, task_id, branch)
    except Exception as exc:                          # noqa: BLE001
        results = [{"repo": r.name, "branch": branch, "ok": False, "dirty": False,
                    "note": f"{type(exc).__name__}: {exc}"} for r in repos]

    store.kv_set(f"task_branch:{task_id}", branch)
    # Only speak when there is something he would want to know: a repo that
    # could not be prepared, a base that was not develop, or uncommitted work
    # that is now riding along on the new branch.
    notable = [r for r in results if not r["ok"] or r.get("note") or r.get("dirty")]
    if notable:
        lines = []
        for r in notable:
            bits = [r["note"]] if r.get("note") else []
            if r.get("dirty"):
                bits.append("uncommitted changes were already in the tree")
            lines.append(f"• {r['repo']}: {'; '.join(bits) or 'could not prepare'}")
        await notify.notify(
            f"🌿 Task #{task_id} — branch `{branch}`:\n" + "\n".join(lines),
            "task", urgency="ambient")
    return results


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
                # Cut the branch before the executor touches anything. Held
                # inside the workspace lock so two tasks cannot race over the
                # same working tree.
                await _prepare_branches(task_id, t)
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
                                         task_cwd(task_id, t["workspace"]), resume=True,
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
    # The task does NOT end here. Raising the PR is the middle of the job, not
    # the end of it: CI has not run, nobody has reviewed it, and it has not
    # merged. Keeping the task open through all three is what makes those
    # events belong to something instead of arriving as orphaned noise.
    store.update_task(task_id, status="shipped", pr_urls="\n".join(urls),
                      pr_state="OPEN", pr_checked_at=0.0)
    msg = (f"🔀 Task #{task_id} shipped:\n" + "\n".join("• " + u for u in urls)
           + "\n\nStaying on it — I'll tell you if CI goes red, if review asks "
             "for changes, or when it merges.")
    await notify.notify(msg, "action", urgency="direct")
    return msg


# --- after the PR is open -----------------------------------------------------

#: How often to ask GitHub what happened to the open PRs. A PR is a slow object:
#: CI takes minutes and review takes hours, so polling harder buys nothing and
#: spends rate limit.
PR_POLL_SECONDS = int(os.environ.get("ASTA_PR_POLL", "300"))

_PR_FIELDS = "state,mergedAt,statusCheckRollup,reviewDecision,url,title,reviews,comments"

#: Review noise that is not a request for a change. Approvals with no body and
#: bot chatter would otherwise arrive as "someone wants something from you".
_BOT_AUTHORS = ("github-actions", "sonarqubecloud", "sonarcloud", "codecov",
                "dependabot", "copilot-pull-request-reviewer")


def _review_notes(pr: dict, me: str = "") -> list[str]:
    """What humans actually asked for on this PR, newest last.

    `reviewDecision` alone says CHANGES_REQUESTED without saying what to change,
    which is not enough to act on — the whole point of following a PR to merge
    is answering the comments, and that needs their text.
    """
    out = []
    for item in list(pr.get("reviews") or []) + list(pr.get("comments") or []):
        author = ((item.get("author") or {}).get("login") or "").lower()
        body = (item.get("body") or "").strip()
        if not body or author in _BOT_AUTHORS or author.endswith("[bot]"):
            continue
        if me and author == me.lower():
            continue                    # his own replies are not asks of him
        state = (item.get("state") or "").upper()
        if state == "APPROVED" and len(body) < 20:
            continue                    # "lgtm" is not a change request
        out.append(f"{author}: {body[:400]}")
    return out


def _new_review_notes(task_id: int, notes: list[str]) -> list[str]:
    """Only the comments not already reported for this task.

    Re-reporting every comment on every poll is how a useful watcher becomes one
    he mutes, and a muted watcher tells him nothing when it matters.
    """
    import hashlib
    seen_raw = store.kv_get(f"task_pr_seen:{task_id}") or "[]"
    try:
        seen = set(_json.loads(seen_raw))
    except (ValueError, TypeError):
        seen = set()
    fresh, keys = [], []
    for n in notes:
        key = hashlib.sha1(n.encode("utf-8", "replace")).hexdigest()[:16]
        keys.append(key)
        if key not in seen:
            fresh.append(n)
    store.kv_set(f"task_pr_seen:{task_id}", _json.dumps((keys + sorted(seen))[:200]))
    return fresh


def _pr_links(t: dict) -> list[str]:
    """The PR urls recorded on a task, without the 'repo: ' label ship() adds."""
    out = []
    for line in (t.get("pr_urls") or "").splitlines():
        _, _, url = line.rpartition(" ")
        url = url.strip()
        if url.startswith("http"):
            out.append(url)
    return out


async def _pr_state(url: str) -> dict:
    """Ask GitHub about one PR. {} when it cannot be read — never a guess."""
    import json as _json
    rc, out = await repo_ops.git(ROOT, "gh", "pr", "view", url,
                                 "--json", _PR_FIELDS, timeout=60)
    if rc != 0:
        return {}
    try:
        return _json.loads(out)
    except (ValueError, TypeError):
        return {}


def _checks_verdict(pr: dict) -> str:
    """'red' | 'green' | 'pending' from the check rollup.

    Anything not explicitly a failure or explicitly finished is pending — a run
    still in flight must not be reported as a pass.
    """
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "pending"
    states = []
    for c in rollup:
        s = (c.get("conclusion") or c.get("state") or "").upper()
        states.append(s)
    if any(s in ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR", "ACTION_REQUIRED")
           for s in states):
        return "red"
    if all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states):
        return "green"
    return "pending"


async def check_pr(task_id: int) -> str | None:
    """One poll of one shipped task. Returns a line to tell him, or None.

    Transitions only. A PR that was red an hour ago and is still red is not
    news, and saying so every five minutes is how a useful watcher gets muted.
    """
    t = store.get_task(task_id)
    if not t or t["status"] not in SHIPPED_STATUSES:
        return None
    links = _pr_links(t)
    if not links:
        return None

    was = t.get("pr_state") or "OPEN"
    title = t["title"][:60]
    for url in links:
        pr = await _pr_state(url)
        if not pr:
            continue
        store.update_task(task_id, pr_checked_at=time.time())

        if pr.get("state") == "MERGED" or pr.get("mergedAt"):
            store.update_task(task_id, status="merged", pr_state="MERGED",
                              finished_at=time.time())
            store.record_outcome("ship", "merged", subject=str(task_id), detail=url)
            return f"🎉 Merged — #{task_id} {title}\n{url}"
        if pr.get("state") == "CLOSED":
            store.update_task(task_id, status="pr_closed", pr_state="CLOSED",
                              finished_at=time.time())
            store.record_outcome("ship", "closed_unmerged", subject=str(task_id), detail=url)
            return (f"🚫 PR closed without merging — #{task_id} {title}\n{url}\n"
                    f"Reply with what should change and I'll pick the task back up.")

        decision = (pr.get("reviewDecision") or "").upper()
        checks = _checks_verdict(pr)
        # One state string carries both signals, so a change in EITHER is a
        # transition worth reporting and a repeat of both is silence.
        now_state = f"{checks}/{decision or 'NONE'}"
        if now_state == was:
            # Nothing moved — but someone may still have left a plain comment,
            # which changes no field on the PR and is exactly the kind of ask
            # that gets missed until somebody follows up in Teams.
            asks = _new_review_notes(task_id, _review_notes(pr))
            if asks:
                return (f"💬 New comment on the PR for #{task_id} {title}\n{url}\n\n"
                        + "\n".join(f"• {a}" for a in asks[:4])
                        + f"\n\nSay 'fix #{task_id}' to address it in the same task.")
            continue
        store.update_task(task_id, pr_state=now_state)

        if checks == "red":
            store.update_task(task_id, status="pr_ci_failed")
            return (f"🔴 CI red on the PR for #{task_id} {title}\n{url}\n"
                    f"Reply 'fix #{task_id}' and I'll pick the task back up with "
                    f"everything it already knows.")
        if decision == "CHANGES_REQUESTED":
            store.update_task(task_id, status="pr_changes_requested")
            # Carry what they actually said. "Changes requested" on its own is
            # not something he can act on from his phone.
            asks = _new_review_notes(task_id, _review_notes(pr))
            detail = ("\n\n" + "\n".join(f"• {a}" for a in asks[:4])) if asks else ""
            return (f"📝 Changes requested on #{task_id} {title}\n{url}{detail}\n\n"
                    f"Say 'fix #{task_id}' and I'll address these in the same task.")
        if decision == "APPROVED" and checks == "green":
            store.update_task(task_id, status="shipped")
            return f"✅ Approved and green — #{task_id} {title}\n{url}\nReady to merge."
    return None


def open_prs() -> list[int]:
    """Task ids whose PR is open and still being watched."""
    return [t["id"] for t in store.list_tasks(limit=200)
            if t["status"] in SHIPPED_STATUSES]


async def pr_watch_loop() -> None:
    """Follow every shipped task until its PR merges or closes.

    Supervised by daemon.start, so this cannot quietly stop being true.
    """
    from . import notify, wake
    while True:
        await wake.sleep(PR_POLL_SECONDS)
        for task_id in open_prs():
            try:
                note = await check_pr(task_id)
            except Exception:
                continue      # a transient gh failure is not worth a report
            if not note:
                continue
            # Red CI and a change request are both "you are blocked" — those
            # interrupt. A merge is good news, and good news can wait.
            good = note.startswith(("🎉", "✅"))
            await notify.notify(note, "task",
                                urgency="ambient" if good else "direct")


#: How long after a task finishes its work is still "the thing we were just
#: doing". Beyond this a similar-sounding request is much more likely to be a
#: genuinely new piece of work on the same area of code.
REFINE_WINDOW_SECONDS = int(os.environ.get("ASTA_REFINE_WINDOW", str(6 * 3600)))

#: Words that carry no signal about WHAT a task was about. Without stripping
#: these, "fix the booking service" and "fix the email service" look similar
#: because they share "fix" and "service".
_STOPWORDS = frozenset("""
a an and are as at be by for from has have in into is it its of on or that the
to with add fix update change make use also should would could need needs
please can task issue bug ticket pr code test tests service
""".split())


def _terms(*parts: str) -> set[str]:
    """The words that actually identify a piece of work."""
    words = re.findall(r"[a-z0-9]+", " ".join(p or "" for p in parts).lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def refinable_match(title: str, prompt: str, workspace: str = "",
                    now: float | None = None) -> dict | None:
    """A recently-finished task this request is really feedback on, or None.

    Deliberately conservative. A false positive blocks real new work and makes
    Arun argue with his own assistant, so the bar is a strong overlap with a
    task that finished recently in the same workspace — not a vague family
    resemblance.
    """
    now = time.time() if now is None else now
    wanted = _terms(title, prompt)
    if len(wanted) < 2:
        return None
    best, best_score = None, 0.0
    for t in store.list_tasks(limit=40):
        if t["kind"] != "code" or t["status"] not in REFINABLE:
            continue
        if workspace and t.get("workspace") and t["workspace"] != workspace:
            continue
        ended = t.get("finished_at") or t.get("created_at") or 0
        if not ended or now - ended > REFINE_WINDOW_SECONDS:
            continue
        have = _terms(t["title"])
        if not have:
            continue
        # Against the OLD task's terms: a long new prompt should not be able to
        # dilute its way under the threshold by mentioning many other things.
        score = len(wanted & have) / len(have)
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= 0.6 else None


async def refine(task_id: int, feedback: str) -> str:
    """Continue a finished task with feedback, in the session it already has.

    This is the whole point of REFINABLE. The alternative — and what used to
    happen — is a new task with a new session, which starts by re-deriving
    everything the original one already worked out, and answers feedback about
    a change by writing a different change.
    """
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    if t["kind"] != "code":
        raise ValueError(f"task #{task_id} is not a code task (kind={t['kind']})")
    if t["status"] in LIVE_STATUSES:
        # Still running: augment() is the right door, and it needs no restart.
        return augment(task_id, feedback)
    if t["status"] not in REFINABLE:
        raise ValueError(f"task #{task_id} cannot be continued (status={t['status']})")

    store.record_outcome("task", "refined", subject=str(task_id))
    was_shipped = t["status"] in SHIPPED_STATUSES
    store.update_task(task_id, status="running")
    prompt = (
        f"FOLLOW-UP ON YOUR OWN COMPLETED WORK — this is not a new task.\n"
        f"You already implemented this; the diff is in the working tree"
        + (" and a PR is open for it.\n" if was_shipped else ".\n")
        + f"Arun's feedback:\n{feedback}\n\n"
        f"Apply it to the EXISTING change. Do not start over, do not re-plan "
        f"from scratch, and do not revert what is already correct."
        + ("\nThe branch is already pushed — commit on top of it so the open PR "
           "picks the change up.\n" if was_shipped else "")
    )
    job = asyncio.create_task(_resume_worker(task_id, prompt, approved=True))
    _running[task_id] = job
    job.add_done_callback(lambda _j, tid=task_id: _running.pop(tid, None))
    where = "the open PR" if was_shipped else "the existing diff"
    return f"Task #{task_id}: continuing {where} with your feedback (same session, full context)."


async def reject(task_id: int, why: str = "") -> str:
    """Reject a task AND stop it. Rejecting used to be cosmetic — the worker kept
    running, kept spending, and finished by marking itself done.

    `why` is whatever Arun said when he stopped it, and it is the single most
    valuable sentence in the whole run: it is him naming the gap between what he
    asked for and what Asta started doing, minutes after it happened.
    """
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    store.record_outcome("task", "rejected", subject=str(task_id), detail=why[:300])
    killed = await cancel(task_id, status="rejected", why=why)
    if not killed:
        store.update_task(task_id, status="rejected")
        learn_from_stop(task_id, t, "rejected", why)
    return (f"Task #{task_id} rejected and its worker killed."
            if killed else f"Task #{task_id} rejected (it had already finished).")
