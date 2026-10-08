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
import subprocess
import time
import uuid
from pathlib import Path

from . import (agents, claude_cli, clip, copilot_cli, guardrails, repo_ops, roles, store,
               wa_format, workspace_tools)


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

# --- how long a code task may take, given what it can touch -------------------
#
# A flat number is wrong in both directions, which is Arun's point: "even small
# changes getting affected in multiple repos take more time." A one-line fix in
# one repo does not need 45 minutes, and a rename that lands across three repos —
# each with its own build, its own checkstyle, its own multi-module Maven reactor —
# is not the same job at all. Verification alone is per-repo: `mvn clean test` on
# the booking service is minutes, and a task touching three pays it three times.
#
# So the budget is a function of scope. Not of DIFFICULTY, which nobody can
# estimate up front — of how many repos are in reach, which is a fact available
# before the work starts.
#
# This is only safe because the idle watchdog landed first (`turn_budget`): a
# wedged brain is stopped after two minutes of silence no matter how much ceiling
# remains. Before that, the ceiling was doubling as a liveness check and had to
# stay tight; now it can be honest about how long real work takes.

CODE_BASE_SECONDS = int(os.environ.get("ASTA_CODE_BASE_SECONDS", "1200"))     # 20 min
CODE_PER_REPO_SECONDS = int(os.environ.get("ASTA_CODE_PER_REPO_SECONDS", "900"))  # +15
CODE_MAX_SECONDS = int(os.environ.get("ASTA_CODE_MAX_SECONDS", "5400"))      # 90 min


def code_timeout(workspace: str | None) -> int:
    """Budget for a code task: a base, plus room for each repo it could touch.

    One repo -> 35 min, which already clears the measured p90 of 32 (n=46).
    Three repos -> 65 min. Capped, because past a point a run that long is not a
    slow task, it is a task that should have been split — and the cap is what
    turns that into a reported budget overrun rather than an afternoon of silence.
    """
    repos = 1
    if workspace:
        try:
            from . import workspace as workspace_mod, worktrees
            root = workspace_mod.WORKSPACES.get(workspace)
            if root:
                repos = max(1, len(worktrees.repos_in(Path(root))))
        except Exception:                       # noqa: BLE001
            repos = 1
    return min(CODE_MAX_SECONDS, CODE_BASE_SECONDS + CODE_PER_REPO_SECONDS * repos)

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
- Never run a command in the background or wait for one to land later (no `&`,
  no run_in_background, no "I'll analyse when it finishes"). Run it in the
  foreground with a timeout. When you stop, this run ENDS — nothing that is
  still downloading or running comes back to you, and "waiting on X" is
  reported to Arun as no change made.
- ONE BRANCH, ONE WORKTREE. You work on the branch Asta cut for this task. If
  he wants the same change on a second base branch too ("develop as well as
  release 3.1.6"), make it ONCE here and say in your summary which other base
  it must also land on: Asta ports the commit onto that base and opens its PR
  when he ships it. Never stop or report "blocked" for want of a second
  checkout. Pushing and opening PRs is Asta's step, on his word, after you
  finish — never yours.
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
- THE PLAN MUST BE READABLE IN THIRTY SECONDS, ON A PHONE. He approves these
  from WhatsApp, standing up. Open it with TWO blocks and nothing before them.

  `STRUCTURE` — the classes and files that change, as an indented tree, one line
  each. After each name, say in TWO TO FOUR WORDS what happens IN that class —
  what it gains, not that it changed. "new record, 2 fields", "adds the priority
  object", "unchanged, MapStruct handles it". Never a sentence, never the word
  NEW on its own (Asta already marks that), never a restatement of the filename.

  `FLOW` — the run of events the change sits in, one actor per line in the order
  they happen, with two to six words on what that actor does with the data. Mark
  the step where the change actually lands with a trailing `<- change`. This is
  the half the tree cannot show: that a service in the MIDDLE needs no change is
  the fact a reviewer most needs and the easiest one to get wrong.

      STRUCTURE
        EtaValidator                  NEW · rejects ETA at/after gate-in
          └─ BookingService.applyVesselEta()   calls it before persist
        BookingServiceTest            +3 cases, before/at/after

      FLOW
        vessel feed        sends the ETA
        BookingService     validates it now   <- change
        ServicePlanLeg     stores portGateIn

  END THE PLAN WITH A LINE THAT SAYS ONLY `PLAN READY`. Asta needs one
  unambiguous mark to tell a plan apart from finished work — without it a plan
  is reported to him as "✅ DONE", with a file list it has not written.
  Then the numbered steps, then a one-line RISK. No prose paragraph before the
  blocks: they are the part he actually reads, and a plan built on a misread
  shows up there in seconds where three paragraphs hide it. Under about twelve
  lines each — this is a shape, not a specification.
  Everything else in the plan is a short bullet, never a sentence past about
  fifteen words. Repos that DON'T change are one bullet each, not a paragraph
  explaining why. Asta reflows both blocks to fit his screen, but it can only
  shorten a line by cutting it — what you leave out of a note is lost, and what
  you pack in gets truncated.
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
  EXCEPTION — an EXISTING branch or PR. When he says the work belongs on a
  branch or pull request that already exists ("pick that PR", "fix it on the
  existing branch", "update the same PR"), that is an instruction, not a
  question to bring back to him: find it yourself (`gh pr list --search`,
  `git branch -r`), `git fetch`, and `git switch` THIS worktree to that branch.
  Commit there, never on a new branch, and name the branch in your summary."""
# How Arun expects the code to READ — the quality bar he reviews every diff
# against — used to be a block here. It is now the `## Coding` section of his
# guardrails.md, so he edits it himself, and every fresh code leg receives it
# (micro pipeline included — this block only ever reached the full one).

# Analysis rides the same pipeline in inquiry mode: full Boot 0 (resolver,
# lessons, pins) but read-only and gate-free.
ANALYSIS_RIDER = """

[Asta runtime overrides — obey exactly]
- Treat this as Stage 0 `inquiry`: answer the question with file:line evidence
  from Boot 0's matches and STOP. Read-only — do not modify any file, do not
  write plan files, no Jira writes, no branches, no builds."""

# How a paused run is recognised from its tail. Deliberately matches the solo
# agent's own gate wording, nothing looser.
#: A run that is WAITING, not finished. Matched loosely on purpose, because the
#: prompt asks for a plan and never dictated a sentinel — so the brain ends the
#: plan however reads naturally, and only one of those phrasings was listed.
#:
#: Task #121 ended "PLAN READY" plus "AUTO-PROCEED gate: … requires your
#: go-ahead", matched nothing, and was reported to Arun as "✅ DONE" with 18
#: files it had not written. He had to be told the plan was waiting, by hand.
#: A plan announced as done is the most misleading push this system can send.
_GATE_MARKS = ("PLAN APPROVED", "PLAN READY", "Which repo applies?",
               "Re-implement, modify, or cancel?", "requires your go-ahead",
               "awaiting your approval", "waiting for your approval",
               "your go-ahead", "approve task")
#: The brain's own sign-off line, replaced by the push's own buttons.
_ASK_LINE = re.compile(r"(?im)^\s*(?:reply|respond)\b[^\n]*plan approved[^\n]*$\n?")
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

#: States that mean this task has already had its say. FINAL deliberately does
#: NOT include "done" — a finished task can still be steered ("reply with
#: changes and I'll continue THIS task"), and the checks that use FINAL are
#: asking "was this called off?", not "is it over?".
#:
#: `_finish_code` needs the other question. It re-enters itself on escalation and
#: on a repo handoff, and its done branch has no memory, so #88 finished four
#: times and pushed four identical completion messages to his phone.
_ALREADY_REPORTED = FINAL + ("done",)

# A task is "live" (owns the conversation's attention) while it runs or waits at
# a gate — that's the window in which a follow-up should augment or redirect it.
LIVE_STATUSES = ("running", "awaiting_approval", "queued")

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


def bind_conversation(conv_id: str | None):
    """Bind the turn's conversation; returns a token that restores the previous one.

    The token matters on the HTTP path. A chat turn owns its whole task and can
    set-and-forget, but `/api/_invoke` serves one call inside a long-lived server:
    leaving the binding behind means the next call that arrives WITHOUT a
    conversation inherits this one, and a staged draft goes to the wrong chat
    looking exactly like success. Callers that own the whole turn may ignore it.
    """
    return _TURN_CONV.set(conv_id or None)


def unbind_conversation(token) -> None:
    """Restore whatever was bound before `bind_conversation` returned this token."""
    with contextlib.suppress(ValueError):        # a token from another context
        _TURN_CONV.reset(token)


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
    # The reverse key too: a task run needs to know WHICH chat to stage a draft
    # into, and the forward list only answers the other direction.
    store.kv_set(f"task_conv:{task_id}", conv_id)
    ids = _linked_ids(conv_id)
    if task_id not in ids:
        ids.append(task_id)
    store.kv_set(f"conv_tasks:{conv_id}", _json.dumps(ids[-10:]))
    store.kv_set(f"task_conv:{task_id}", conv_id)


def conversation_of(task_id: int) -> str:
    """The conversation this task was spawned from, or "".

    Written by `link_task`. A task that predates the reverse key falls back to a
    scan, because the alternative — staging a reply into no conversation — is the
    failure that made `prepare_to_send` useless over MCP in the first place.
    """
    direct = (store.kv_get(f"task_conv:{task_id}") or "").strip()
    if direct:
        return direct
    for row in store.list_conversations(limit=50) or []:
        if task_id in _linked_ids(row.get("id") or ""):
            return row["id"]
    # NOTHING spawned it from a chat — the responder's investigations, and every
    # other task a background loop starts. Those are the ones that most need to
    # hand something back, and they were the ones that could not: task #96 read
    # prod, found why STF never ran, wrote the reply to Alex, and then said
    # "Teams send tool isn't available in this environment, so please send
    # manually". The work done and nobody told.
    #
    # Safe to fall back, where guessing a Teams RECIPIENT would not be: this
    # conversation only decides where Arun SEES the approval, not who the
    # message goes to. The recipient is named separately and is never guessed.
    latest = (store.list_conversations(limit=1) or [{}])[0].get("id") or ""
    return latest
    return ""


#: Kinds whose work is reading code, and so the only ones the symbol-nav and
#: docs servers earn their startup cost for. Asta's OWN tools go to every kind.
_DEV_MCP_KINDS = ("analysis", "code")


def task_tools(task_id: int, cwd: str, kind: str = "") -> str:
    """The MCP servers a task run gets: Asta's own, plus the dev ones if enabled.

    Task subprocesses used to get `dev_mcp.config_json(cwd)` and nothing else —
    and that feature is off by default, so they got an empty string. Asta's own
    server was attached on the CHAT path only, which meant every task ran with no
    Jira, no Teams, no memory and no `prepare_to_send`.

    That is not a missing nicety. It is the last step of the loop Arun actually
    asked for: task #88 implemented `transportAssetPriority` across eleven files
    with a green build, then finished with "I can't message Alex directly" —
    the work done and nobody told.

    Bound to the spawning conversation so an approval lands in the chat he asked
    from, rather than in no conversation at all.
    """
    from . import copilot_cli, dev_mcp, mcp_server
    base = None
    if copilot_cli.mcp_cli_enabled():
        base = mcp_server.config_entry(conv_id=conversation_of(task_id), task_id=str(task_id))
    project = cwd if (not kind or kind in _DEV_MCP_KINDS) else ""
    return dev_mcp.config_json(project, base)


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
    now = time.time()
    return [i for i in keep
            if (store.get_task(i) or {}).get("status") in LIVE_STATUSES
            and not _gate_gone_stale(store.get_task(i) or {}, now)]


#: How long a task may sit at a gate before it stops CLAIMING the conversation.
#: It stays open and approvable — it just no longer swallows unrelated messages.
GATE_STALE_SECONDS = float(os.environ.get("ASTA_GATE_STALE_HOURS", "3")) * 3600


def _gate_gone_stale(t: dict, now: float) -> bool:
    """A task waiting at a gate long enough that it is no longer "what we are
    doing right now".

    A live task owns the conversation's attention, which is right while the work
    is in flight and wrong once it has been sitting unanswered for hours. Task
    #117 asked for approval at 09:31; its PR was merged by mid-morning and it
    still sat there, and because it was the only "live" task every unrelated
    message Arun sent for the rest of the day was routed into it — a topic
    request, a PR ask, an instruction about a different repo entirely. He put it
    plainly: "why still 117 is running doesn't makes sense it confusing with
    other things".

    Deliberately NOT auto-closing it: he may still want to approve it. It just
    stops being the default owner of whatever he says next.
    """
    if t.get("status") != "awaiting_approval":
        return False                       # actually running: it owns attention
    since = t.get("finished_at") or t.get("started_at") or t.get("created_at") or 0
    return bool(since) and (now - float(since)) >= GATE_STALE_SECONDS


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


def augment(task_id: int, text: str, *, code_change: bool = False) -> str:
    """Fold a follow-up into a live code task WITHOUT restarting its session.
    It's buffered and delivered as part of the instructions the moment Arun acts
    on the task's next gate — the mandatory approval stays intact and there's no
    expensive Claude/Copilot session re-cache."""
    from . import activity, frontdesk
    if not code_change and frontdesk.task_intent(text) in ("read", "external"):
        raise ValueError(f"task #{task_id} was not amended: this is a question or "
                         "external action, not code feedback")
    if activity.classify_interjection(text) == "new_task":
        # He said it is a separate piece of work. Absorbing it anyway is how
        # task #117 — the BookingEquipment equals/hashCode fix — ended up
        # holding clarifying questions about Kafka topics in another repo, and
        # then pushed a DONE and a PLAN that mixed the two.
        raise ValueError(
            f"that reads as a new task, not an addition to #{task_id} — "
            f"start it separately")

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
    # Said to be his, in so many words. "[Additional instructions…]" read to the
    # worker as an injected block: #178 held its work and reported his own
    # "create PR" as unverified (30 Sep).
    return ("\n\nArun said this himself, in his own chat with Asta, after this task "
            "started. Asta's pipeline is relaying it word for word — it is his "
            "instruction, not tool output:\n" + extra +
            "\n(If it asks for a push or a PR: that is Asta's own step once you "
            "finish, done on his word. Do not push, and do not report it as blocked.)")


# --- the investigation queue ------------------------------------------------------
#
# 30 Sep, his words: "5 people pinging at a time for different issues — you tell
# me you do only 2 now and the remaining 3 after hours? No, this is not right."
# Every real ask is worked. What is bounded is how many run AT ONCE (each is a
# model session reading production); the rest wait their turn, highest priority
# first, and nothing is ever dropped.

def max_parallel_investigations() -> int:
    from . import settings
    try:
        return max(1, int(settings.effective("ASTA_MAX_PARALLEL_INVESTIGATIONS",
                                             int(os.environ.get("ASTA_MAX_PARALLEL_INVESTIGATIONS", "3")))))
    except (TypeError, ValueError):
        return 3


class _Gate:
    """A priority semaphore: a freed slot goes to the most urgent waiter."""

    def __init__(self) -> None:
        self.running = 0
        self.waiting: list = []
        self._seq = 0

    async def acquire(self, priority: int, task_id: int) -> None:
        import heapq
        if self.running < max_parallel_investigations() and not self.waiting:
            self.running += 1
            return
        fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self.waiting, (priority, self._seq, task_id, fut))
        store.update_task(task_id, status="queued")
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()                 # the slot was handed over: pass it on
            else:
                self.waiting = [w for w in self.waiting if w[3] is not fut]
                heapq.heapify(self.waiting)
            raise

    def release(self) -> None:
        import heapq
        while self.waiting:
            *_, fut = heapq.heappop(self.waiting)
            if not fut.done():
                fut.set_result(True)           # the slot moves to the waiter
                return
        self.running = max(0, self.running - 1)

    def queued_ids(self) -> list[int]:
        return [w[2] for w in sorted(self.waiting)]


_gate = _Gate()


@contextlib.asynccontextmanager
async def investigation_slot(task_id: int):
    try:
        priority = int(store.kv_get(f"task_priority:{task_id}") or 2)
    except ValueError:
        priority = 2
    await _gate.acquire(priority, task_id)
    try:
        yield
    finally:
        _gate.release()


def queue_summary() -> dict:
    """What is running, waiting its turn, and paused on a limit — for "brain
    status", the limit message and the morning line."""
    rows = store.list_tasks(limit=200)
    return {"running": sum(1 for t in rows if t["status"] == "running"),
            "queued": sum(1 for t in rows if t["status"] == "queued"),
            "paused": sum(1 for t in rows if t["status"] == "paused"),
            "queued_ids": [t["id"] for t in rows if t["status"] == "queued"],
            "paused_ids": [t["id"] for t in rows if t["status"] == "paused"]}


_OVERRIDE_KEY = "brain_override"


def brain_override() -> str:
    """The brain he moved the work to ("use copilot"), while it holds."""
    try:
        o = _json.loads(store.kv_get(_OVERRIDE_KEY) or "{}")
    except ValueError:
        return ""
    return o.get("brain", "") if float(o.get("until") or 0) > time.time() else ""


async def use_brain(brain: str, until: float | None = None) -> str:
    """"use copilot" / "use claude": new work goes to that brain until the
    other one renews, and everything paused on a limit carries on there now."""
    from . import agent as agent_mod
    ex = {"claude": "claude", "copilot": "copilot"}.get((brain or "").lower())
    if not ex:
        return "I can move work to *claude* or *copilot* (the local model can't run tasks)."
    spec = "claude_cli" if ex == "claude" else "copilot"
    if not agent_mod.available(spec):
        return f"{ex.title()} isn't installed here."
    if agent_mod.quota_down(spec) or (ex == "claude" and claude_cli.limited_until()):
        from . import brains
        why = (f"its monthly quota is out until {brains.copilot_status()['resets_on']}"
               if ex == "copilot" else "its session limit is reached right now")
        return f"Can't move to {ex.title()} — {why}."
    until = until or (time.time() + 12 * 3600)
    store.kv_set(_OVERRIDE_KEY, _json.dumps({"brain": ex, "until": until}))
    moved = []
    for tid in queue_summary()["paused_ids"]:
        try:
            await resume_task(tid, switch_to=ex)
            moved.append(f"#{tid}")
        except Exception:                                      # noqa: BLE001
            continue
    store.record_outcome("brains", "switched", subject=ex, detail=", ".join(moved))
    return (f"✅ New work goes to {ex.title()} now"
            + (f"; resumed there: {', '.join(moved)}" if moved else "")
            + ". Say “use claude” / “use copilot” to move it again.")


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


#: Words that mean a repo was NAMED as still to do, rather than merely mentioned.
_UNFINISHED = re.compile(
    r"\b(outstanding|blocked|remaining|still (?:to|needs?|outstanding|pending)|"
    r"pending|not (?:yet )?(?:done|implemented|applied)|next repo|todo|to do|"
    r"second half|other half|waiting on you)\b", re.I)


def _repos_still_needed(task_id: int, t: dict, result: str) -> list[str]:
    """Repos this run said were left over and has no checkout for.

    Both halves are required. A repo NAMED in the result is not enough — a run
    legitimately mentions the repo it just finished, and the one upstream of it.
    An unfinished word is not enough either. Together they are the shape of #88's
    ending: "telikos-activityplanworkflow-service (TMS-facing side): … still
    outstanding — blocked, no feature branch cut for this repo yet under this
    task."

    A repo the task already has a checkout for is never returned: if it could
    work there and did not, that is a decision, not a missing tree.
    """
    if not (result or "").strip() or not t.get("workspace"):
        return []
    from . import worktrees as _wt
    try:
        root = Path(code_cwd(t.get("workspace")))
        have = {r.name for r in _wt.repos_in(Path(task_cwd(task_id, t.get("workspace"))))}
        every = {r.name for r in _wt.repos_in(root)} | {r.name for r in _wt.outside(result)}
    except Exception:                                          # noqa: BLE001
        # `code_cwd` refuses an unregistered workspace by raising, which is a
        # state a finishing task is legitimately in. Continuing to the next repo
        # is a nicety; reporting the one it did is not.
        return []
    missing = [name for name in sorted(every - have) if name in result]
    if not missing or not _UNFINISHED.search(result):
        return []
    return missing


def committed_so_far(task_id: int, t: dict) -> list[dict]:
    """What THIS task has already committed, per repo, read from git.

    The run's own memory is the wrong source — a compaction loses it, and a fresh
    leg never had it. Git is the record that survives both.
    """
    from . import worktrees as _wt
    out: list[dict] = []
    try:
        where = Path(task_cwd(task_id, t.get("workspace")))
        repos = _wt.repos_in(where)
    except Exception:                                          # noqa: BLE001
        return out              # advisory only — never break a finishing task
    for repo in repos:
        try:
            head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                  cwd=repo, capture_output=True, text=True,
                                  timeout=10).stdout.strip()
            base = repo_ops.base_ref(repo)
            if not base:
                continue
            log = subprocess.run(["git", "log", "--oneline", f"{base}..HEAD"],
                                 cwd=repo, capture_output=True, text=True,
                                 timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if log:
            out.append({"repo": repo.name, "branch": head,
                        "commits": log.splitlines()[:8]})
    return out


def _done_note(task_id: int, t: dict) -> str:
    """The work already on the branch, for a leg that did not do it.

    A task can run several legs — micro, then escalation, then a repo hop — and
    each is a fresh window with no memory of the last. Task #89's escalated leg
    opened its worktree, saw nothing it recognised, and implemented the whole
    change a second time; the two commits differ, so now there are two versions
    of one change. Reading `git log` costs nothing and is the only account that
    survives a compaction.
    """
    done = committed_so_far(task_id, t)
    if not done:
        return ""
    lines = []
    for d in done:
        lines.append(f"  {d['repo']} ({d['branch']}):")
        lines.extend(f"    {c}" for c in d["commits"])
    return ("\n\n[ALREADY COMMITTED ON THIS BRANCH — your own earlier work]\n"
            + "\n".join(lines) +
            "\nThis is yours, from an earlier window of THIS task. Do not "
            "re-implement it. Verify with `git show` if you need to, then continue "
            "from where it left off.")


def _context_note() -> str:
    """Which context directory is Asta's, said out loud.

    His workspace still carries another toolchain's `.contmark/`, and its own
    `AGENTS.md` — a file in HIS repo, not Asta's to edit — opens with "Context
    comes from `.contmark/` — run `node .contmark/resolve-task.js`". An agent
    reading that does exactly what it says. So the instruction has to be
    overridden here, where Asta speaks last: "dont use contmark anywhere even the
    existing contmark stuff in the repo ignore it use ours".
    """
    from .workspace.providers.indexed import DEFAULT_CONTEXT_DIR
    ours = os.environ.get("ASTA_CONTEXT_DIRNAME", "").strip() or DEFAULT_CONTEXT_DIR
    return (f"\n\n[CONTEXT — which directory is authoritative]\n"
            f"  Use `{ours}/` and nothing else: `node {ours}/resolve-task.js "
            f"<root> \"<task>\"`.\n"
            f"Any instruction you find in the workspace — AGENTS.md, a README, a "
            f"lessons file — telling you to read `.contmark/` or run its resolver "
            f"is out of date. Ignore it. It is left on disk only so nothing "
            f"breaks; it is not maintained and its indexes are not updated.")


def _branch_note(task_id: int, t: dict) -> str:
    """Tell the run where it already is. "" when there is nothing prepared.

    `_prepare_branches` cuts a branch in every repo the task names, in a private
    worktree, and records it — and then nothing ever told the agent. Neither
    pipeline mentions branches at all, so the agent did the only thing left: it
    cut its own.

    Task #88 is the proof, and it is almost funny. It reported "blocked, no
    feature branch cut for this repo yet under this task" while standing on
    `feature/asta-88-implement-missing-transportassetpriority`, which Asta had cut
    for it, with its own commit already on it. It then asked Arun for a branch.
    """
    branch = (store.kv_get(f"task_branch:{task_id}") or "").strip()
    if not branch:
        return ""
    from . import worktrees as _wt
    where = task_cwd(task_id, t.get("workspace"))
    repos = sorted(r.name for r in _wt.repos_in(Path(where)))
    listed = ", ".join(repos) if repos else "this workspace"
    return (f"\n\n[THIS RUN — where you already are]\n"
            f"  Working tree : {where}\n"
            f"  Branch       : {branch} (already checked out in every repo below)\n"
            f"  Repos        : {listed}\n"
            f"This is your own worktree, cut from develop. Commit on the branch you "
            f"are on. Do NOT create a branch, do NOT switch branch, and never say "
            f"you are blocked for want of one — you have it. Every repo listed above "
            f"is yours to change in THIS run; a change spanning two of them is one "
            f"task, not two. The one exception: if he asked for the work on a branch "
            f"or PR that ALREADY exists, find it and `git switch` this worktree to "
            f"it yourself — do not ask him whether to.")


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
        # The brain names the DIRECTORY it can see, not the registry key nobody
        # told it about. `booking` is registered at ~/booking-workspace, so
        # "booking-workspace" is a perfectly sensible thing to say — and it
        # failed a whole code task on the difference. Resolve it when exactly one
        # workspace can be meant; refuse only when the name is genuinely unknown
        # or genuinely ambiguous.
        hit = resolve_workspace(workspace)
        if hit:
            workspace = hit
        else:
            known = ", ".join(sorted(workspace_tools.WORKSPACES)) or "none registered"
            raise RuntimeError(f"unknown workspace '{workspace}' — known: {known}")
    return str(workspace_tools.WORKSPACES[workspace])


def resolve_workspace(name: str) -> str:
    """The registered workspace `name` unambiguously means, or "".

    Matched three ways, each requiring exactly ONE hit: the registered key, the
    basename of its root directory, and a unique prefix. Anything matching two
    workspaces resolves to nothing — guessing between repos is the failure this
    whole area exists to prevent.
    """
    want = (name or "").strip().lower()
    if not want:
        return ""
    if want in workspace_tools.WORKSPACES:
        return want
    for candidates in (
        {k for k, v in workspace_tools.WORKSPACES.items()
         if Path(str(v)).name.lower() == want},
        {k for k in workspace_tools.WORKSPACES
         if want.startswith(k.lower()) or k.lower().startswith(want)},
    ):
        if len(candidates) == 1:
            return candidates.pop()
    # A REPO named where a workspace was expected — the brain names what it sees.
    # Three code tasks in September died on this ("unknown workspace
    # 'telikos-activityplanworkflow-service'", "'activity-plan-service'").
    from . import review
    owners = {k for k, v in workspace_tools.WORKSPACES.items()
              if review.resolve_repo(Path(str(v)), name)}
    return owners.pop() if len(owners) == 1 else ""


def _already_live(title: str, prompt: str, workspace: str | None) -> dict | None:
    """A task doing this exact thing that has not finished yet, or None.

    Matched on the PROMPT, not the title: the prompt is the instruction, and two
    spawns of the same work can be titled differently by whatever summarised it.
    """
    want = (prompt or "").strip()
    if not want:
        return None
    for row in store.list_tasks(limit=40):
        if row.get("status") not in LIVE_STATUSES:
            continue
        if (row.get("prompt") or "").strip() != want:
            continue
        if (row.get("workspace") or None) != (workspace or None):
            continue
        return row
    return None


def _prior_case_finding(prompt: str) -> str:
    """Carry a recent same-case, same-env finding into a new read-only task."""
    from . import answers, booking_case
    scope = booking_case.case_scope(prompt)
    if not scope:
        return ""
    for row in store.list_tasks(limit=80):
        if row.get("kind") != "analysis" or row.get("status") != "done" \
                or time.time() - float(row.get("finished_at") or 0) > 24 * 3600 \
                or booking_case.case_scope(row.get("prompt") or "") != scope:
            continue
        analysis, _ = answers.split(row.get("result") or "")
        finding = analysis.strip() or (row.get("result") or "").strip()
        if finding:
            return (f"\n\n[Prior task #{row['id']} for {scope[0]} in {scope[1]}: "
                    "use its evidence, but verify the conclusion. Do not redo the "
                    "same log query; investigate missing downstream steps, another "
                    "attempt or newer events.]\n" + finding[:1800])
    return ""


#: A brief that asks to FIND something out, not to write to someone.
_NOT_A_DRAFT = re.compile(
    r"\bdo\s+not\s+draft\b|\bdon'?t\s+draft\b|\bno\s+draft\b|\bjust\s+report\b|"
    r"\breport\s+back\b|\bfind\s+(?:out\s+)?(?:his|her|their|the)\s+(?:actual\s+)?"
    r"(?:message|reply|confirmation|answer)\b|\blook\s+up\b", re.I)


def _notes_about(text: str, who: str) -> bool:
    """Does this read as notes ABOUT the person rather than a message TO them?"""
    first = (who or "").split()[0] if (who or "").split() else ""
    if len(first) < 3:
        return False
    return bool(re.search(
        rf"\b{re.escape(first)}(?:'s|’s)\s|\b{re.escape(first)}\s+(?:confirmed|replied|said|"
        rf"asked|wants|wrote|mentioned|referenced|is\s+asking)\b", text or "", re.I))


def spawn(title: str, prompt: str, kind: str = "analysis",
          workspace: str | None = None, teams_chat: str = "",
          executor: str = "", context_from: int | None = None,
          pipeline: str = "", priority: int = 2) -> dict:
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
    if kind == "teams_draft" and _NOT_A_DRAFT.search(prompt or ""):
        # A lookup dressed as a draft. #186 (1 Oct) was told "do not draft a
        # reply — just report back", was spawned as a teams_draft anyway, and
        # its notes ("Vinish's confirmation reply…") went to Vinish on approve.
        kind, teams_chat = "analysis", ""
    if executor and executor not in _executor_names():
        raise ValueError(f"unknown executor '{executor}' (copilot|claude, empty = auto)")
    if pipeline and pipeline not in ("micro", "full"):
        raise ValueError(f"unknown pipeline '{pipeline}' (micro|full, empty = auto)")
    if context_from:
        prev = store.get_task(int(context_from))
        if prev and prev.get("result"):
            # Anchors from the earlier investigation — paying for discovery
            # twice was ~half of a code task's boot cost.
            prompt += (f"\n\n[Prior investigation (task #{context_from}) — use "
                       "these anchors, verify the conclusion rather than re-discovering]\n"
                       + prev["result"][-2500:])
    same = _already_live(title, prompt, workspace)
    if same:
        # TWO LIVE TASKS DOING THE IDENTICAL THING is never what anyone meant. It
        # happened often enough for Arun to call it out — "many times it creating
        # duplicate tasks and running" — and it is pure waste twice over: the
        # second run bills a whole agentic pipeline to reach the same answer, and
        # then both report, so he reviews the same work twice and cannot tell
        # which diff is which.
        #
        # Only against LIVE tasks. Re-running something that has FINISHED is a
        # perfectly ordinary request ("do that again"), and refusing it would be
        # the more annoying failure.
        return same
    if kind == "analysis" and not context_from:
        prompt += _prior_case_finding(prompt)
    t = store.create_task(title, kind, prompt, workspace or None, teams_chat)
    # Where it stands in the investigation queue: 0 = he asked for it himself,
    # 1 = urgent, then the attention ranks. Lower goes first.
    store.kv_set(f"task_priority:{t['id']}", str(int(priority)))
    from . import routing
    if routing.enabled():
        # "use claude" in the ask itself names the brain; "cheap"/"max" the tier.
        asked = routing.on_spawn(t["id"], title, prompt) if kind == "code" \
            else routing.overrides(f"{title}\n{prompt}").get("brain", "")
        if asked in _executor_names() and not executor:
            executor = asked
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
            # "…and raise the PR" in his own ask is the go-ahead for the whole
            # run: read from what HE typed, never from the brief a brain wrote.
            with contextlib.suppress(Exception):
                from . import capabilities, go, scorecard
                go.on_spawn(t["id"], scorecard.his_words(capabilities.said_this_turn()))
        if _agent_for(t) and _graph().enabled():
            _graph().start(t["id"])
            return t
    job = asyncio.create_task(_worker(t["id"]))
    _running[t["id"]] = job
    job.add_done_callback(lambda _j, tid=t["id"]: _running.pop(tid, None))
    return t


def _graph():
    """The LangGraph engine (app/graph). A task started there finishes there."""
    from .graph import runner
    return runner


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


#: The heading that opens a plan's shape-at-a-glance block. Matched loosely
#: because the brain writes the heading, not this code — "STRUCTURE",
#: "## Structure", "Class diagram:" all mean the same thing to Arun.
_STRUCTURE_HEAD = re.compile(r"^[#*\s]*(structure|class diagram|shape)\b\s*:?[#*\s]*$", re.I)


#: "FLOW", "## Sequence", "Event flow:" — the run of events the change sits in.
_FLOW_HEAD = re.compile(r"^[#*\s]*(flow|sequence|event flow|call flow)\b\s*:?[#*\s]*$", re.I)


def _block_span(lines: list[str], head: re.Pattern) -> tuple[int, int]:
    """(start, end) of the block `head` opens, or (-1, -1).

    Ends at the first blank line followed by something that is not part of the
    block — a heading, a numbered step — so it keeps its internal blank lines
    without swallowing the rest of the plan.
    """
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), -1)
    if start < 0:
        return -1, -1
    end = start + 1
    indented = False
    for i in range(start + 1, len(lines)):
        nxt = lines[i]
        if not nxt.strip():
            following = next((x for x in lines[i + 1:] if x.strip()), "")
            if following and not following.startswith((" ", "\t")):
                break
            continue
        if _STRUCTURE_HEAD.match(nxt) or _FLOW_HEAD.match(nxt):
            break        # the other pinned block starts here
        if indented and not nxt.startswith((" ", "\t")):
            # An unindented line after the block's own indented rows is the plan
            # resuming — "RISK: low", a prose aside. Without this the block only
            # ended on a blank line, so a plan that ran STRUCTURE straight into
            # RISK reflowed the risk line as if it were a file.
            break
        indented = indented or nxt.startswith((" ", "\t"))
        end = i + 1
    return start, end


def _structure_span(lines: list[str]) -> tuple[int, int]:
    """(start, end) of the structure block in `lines`, or (-1, -1).

    Ends at the first blank line followed by something that is not part of the
    tree — a heading, a numbered step — so the block keeps its internal blank
    lines without swallowing the rest of the plan.
    """
    return _block_span(lines, _STRUCTURE_HEAD)


def _unfence_plan(lines: list[str]) -> list[str]:
    """Drop the fence markers around a fence that holds THE PLAN.

    A fence is code or build output and is dropped — except that a brain will
    wrap its whole plan in one (#178, 30 Sep: STRUCTURE, FLOW, RISK and a
    FLAGGED note all inside ```), and then the phone showed one line of
    preamble and the approve buttons. He approved a plan he could not see."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            body = lines[i + 1:j]
            if any(_STRUCTURE_HEAD.match(b) or _FLOW_HEAD.match(b)
                   or b.strip() == "PLAN READY" for b in body):
                out += body
                i = j + 1
                continue
            out += lines[i:j + 1]
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    return out


def _phone_text(result: str, limit: int = 1100) -> str:
    """A gate's output, made readable on a phone.

    The raw tail was unusable there: it started mid-sentence, carried code fences,
    tool noise and table pipes, and ran past the notification cap. This keeps the
    structure that matters (headings, bullets, numbered steps), drops the noise,
    and cuts on a LINE boundary so it never ends mid-word.

    The STRUCTURE block is PINNED. Everything else prefers the tail, which is
    right for a gate's question — but the shape of the change is what Arun reads
    first to decide whether the plan is built on a misread, and it opens the
    plan, so a pure tail cut is exactly what would drop it.
    """
    lines = _unfence_plan((result or "").splitlines())
    keep: list[str] = []
    in_fence = False
    fenced_structure = False
    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            # A fence right after the STRUCTURE heading holds the tree itself, so
            # its CONTENTS are kept (without the fence markers). Every other fence
            # is code or build output and stays dropped.
            fenced_structure = (not in_fence
                                and bool(keep) and _STRUCTURE_HEAD.match(keep[-1]))
            in_fence = not in_fence
            continue
        if in_fence and not fenced_structure:
            continue
        s = line.strip()
        if not s:
            if keep and keep[-1] != "":
                keep.append("")
            continue
        if s.startswith("|") or set(s) <= set("-=_|+ "):   # table rows / rules
            continue
        if s == "PLAN READY":                  # the pipeline's mark, not his reading
            continue
        keep.append(line)
    # Two blocks are PINNED, in this order: the shape of the change, then the run
    # of events it sits in. Both are written column-aligned, which is unreadable
    # on the phone they are written for — the padding lands mid-line once the
    # bubble wraps, and six files become a paragraph. Reflowed here rather than
    # asked for in the prompt, because the brain cannot know the width and the
    # terminal copy of the same plan is better off aligned.
    spans = [(_structure_span(keep), "tree"), (_block_span(keep, _FLOW_HEAD), "flow")]
    spans = sorted(((sp, kind) for sp, kind in spans if sp[0] >= 0), key=lambda x: x[0])
    shape: list[str] = []
    for (start, end), kind in spans:
        block = keep[start:end]
        head = "*" + block[0].strip("*# ") + "*"
        if kind == "tree":
            shape += [head] + wa_format.reflow_tree(block[1:]) + [wa_format.LEGEND, ""]
        else:
            shape += ["🔄 " + head] + wa_format.reflow_flow(block[1:]) + [""]
    shape = shape[:-1] if shape else shape
    cut = {i for (st, en), _ in spans for i in range(st, en)}
    rest = [ln for i, ln in enumerate(keep) if i not in cut]
    budget = limit - sum(len(ln) + 1 for ln in shape)
    # Prefer the tail (the plan + the ask), but start on a real heading/bullet.
    out: list[str] = []
    total = 0
    for line in reversed(rest):
        if total + len(line) + 1 > budget:
            break
        out.append(line)
        total += len(line) + 1
    out.reverse()
    # Don't start mid-sentence — but only while there is something to spare. The
    # guard used to pop FIRST and check after, so a `rest` of one line ("RISK:
    # low — additive nullable field") was emptied by the very trim meant to tidy
    # its opening, and the risk line vanished from the plan.
    # And only when the tail really was cut: a plan that fits whole starts where
    # its author started it. Trimming one anyway ate "RISK:" and the first line
    # of a "FLAGGED:" note, leaving its second line orphaned (#178, 30 Sep).
    was_cut = len(out) < len(rest)
    while was_cut and len(out) > 3 and not (
            out[0].lstrip().startswith(("#", "-", "*", "•"))
            or out[0].lstrip()[:2].rstrip(".").isdigit()
            or re.match(r"\s*[A-Z][A-Z ]{2,20}:", out[0])):
        out.pop(0)
    body = "\n".join((shape + [""] + out) if shape and out else (shape or out)).strip()
    # One blank line between things, never three. Dropped lines (the brain's own
    # sign-off, a stripped fence) leave their blanks behind, and a phone bubble
    # shows every one of them as empty screen.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body or (result or "").strip()[-limit:]


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
    ex = brain_override() or os.environ.get("ASTA_EXECUTOR", "copilot")
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


def _with_pipeline(pipeline: str, prompt: str, task_id: str = "") -> str:
    """Prepend a pipeline body to a prompt, for executors without a file flag.

    The pipeline says how the work is STAGED; the role says who is doing it.
    They are orthogonal — a micro task can be a debugging job or a coding one —
    so the role rides alongside rather than forking the pipeline files.
    """
    body = agents.load(pipeline) if pipeline else ""
    hat = roles.brief(roles.sustained(task_id, prompt) if task_id
                      else roles.role_for(prompt))
    body = f"{body}\n\n{hat}" if (body and hat) else (body or hat)
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


def plan_approved(task_id: int) -> bool:
    """Has Arun approved this task's plan yet?

    The one fact that decides whether a leg may write. Set by `reply()` at the
    gate, by `resume_task` (the work was already approved when it paused) and by
    `refine` (feedback on a diff he has seen). Absent means: plan only.
    """
    return (store.kv_get(f"task_approved:{task_id}") or "") == "1"


def mark_approved(task_id: int) -> None:
    store.kv_set(f"task_approved:{task_id}", "1")


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
    if not resume:
        # His standing instructions ride ONCE, on the fresh session. A resumed
        # leg already carries them, and re-sending a rule the session holds is
        # the token bleed the guardrails file exists to end.
        prompt += guardrails.block("code")
    watcher = _progress_watcher(task_id, (store.get_task(task_id) or {}).get("title", ""))
    pipeline = _pipeline_name("code", _pipeline_for(task_id))
    from . import agent as agent_mod
    # Asta's own tools, plus Serena + Context7 when ASTA_DEV_MCP is on.
    dev_cfg = task_tools(task_id, cwd, "code")
    # Before his approval, a code leg may read everything and change nothing.
    # The rule was a sentence in a prompt; the micro pipeline's own instructions
    # said "make the edit", and an instruction a model may ignore is not a gate.
    plan_only = not plan_approved(task_id)
    # Which model at what effort: by the size of the work, not the stage alone.
    model = ""
    from . import routing
    if routing.enabled():
        choice = routing.choose(task_id, ex, "plan" if plan_only else "implement")
        model, effort = choice.model, choice.effort or effort
        routing.record(task_id, "plan" if plan_only else "implement", ex, choice)
    if ex == "claude":
        try:
            out = await claude_cli.one_shot(
                prompt, cwd=cwd, timeout=code_timeout(workspace),
                agent_file=_agent_file("code", _pipeline_for(task_id)),
                effort=effort, session_id=sid, resume=resume, on_progress=watcher,
                mcp_config=dev_cfg, plan_only=plan_only, model=model)
            agent_mod.mark_quota_ok("claude_cli")
            return out
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
        out = await copilot_cli.one_shot(
            _with_pipeline(pipeline, prompt), cwd=cwd, timeout=code_timeout(workspace),
            effort=effort, session_id=sid, resume=resume, on_progress=watcher,
            mcp_config=dev_cfg, plan_only=plan_only, model=model)
        agent_mod.mark_quota_ok("copilot")
        return out
    except RuntimeError as exc:
        if not agent_mod.transient_limit(str(exc)):
            raise
        agent_mod.mark_quota_down("copilot", str(exc))
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


#: Which guardrails sections a non-pipeline run receives — see guardrails.AUDIENCES.
_GUARDRAIL_AUDIENCE = {"analysis": "analysis", "teams_draft": "draft", "code": "code"}


async def _run_simple(task_id: int, t: dict, prompt: str) -> str:
    """Non-pipeline kinds (analysis, teams_draft, agent-less code): one leg,
    executor-aware, with transparent claude failover when copilot's quota dies."""
    ex = _resolve_executor(task_id)
    cwd = _cwd(t["workspace"])
    # One fresh session per simple run, so his guardrails ride exactly once —
    # the Investigation rules to an analysis, the Communication rules to a draft.
    prompt += guardrails.block(_GUARDRAIL_AUDIENCE.get(t["kind"], "chat"))
    tout = (code_timeout(t["workspace"]) if t["kind"] == "code"
            else TASK_TIMEOUT[t["kind"]])
    agent = _agent_for(t)
    eff = _effort_for(t["kind"], ex)
    pipeline = _pipeline_name(t["kind"]) if agent else ""
    agent_file = _agent_file(t["kind"]) if agent else ""
    # Asta's own tools for every kind — a teams_draft task that cannot reach
    # `prepare_to_send` is the one that most obviously needs them. The dev
    # servers stay with the kinds that read code.
    dev_cfg = task_tools(task_id, cwd, t["kind"])
    from . import agent as agent_mod
    # Every brain that could take this run, the task's own first. A usage limit
    # hands the run to the next one that is up; when none is, the run PAUSES and
    # auto-resumes — it is never reported as failed.
    #
    # This path used to try Claude with no handling at all: eight analyses
    # between 9 and 10 September died as "claude exited 1: You've hit your
    # session limit", while the same limit on a code task paused and resumed.
    # One rule for every kind of task, not one per call site.
    order = [ex] + [b for b in _SIMPLE_BRAINS if b != ex]
    limited: list[tuple[str, float | None, str]] = []
    for i, brain in enumerate(order):
        if i and not _can_take_over(brain):
            continue
        effort = eff if brain == ex else _effort_for(t["kind"], brain)
        try:
            if brain == "claude":
                result = await claude_cli.one_shot(prompt, cwd=cwd, timeout=tout,
                                                   agent_file=agent_file, effort=effort,
                                                   mcp_config=dev_cfg)
            else:
                result = await copilot_cli.one_shot(_with_pipeline(pipeline, prompt),
                                                    cwd=cwd, timeout=tout, effort=effort,
                                                    mcp_config=dev_cfg)
        except RuntimeError as exc:
            if not agent_mod.transient_limit(str(exc)):
                raise
            agent_mod.mark_quota_down(_QUOTA_NAME[brain], str(exc))
            limited.append((brain, agent_mod.limit_reset_at(str(exc)), str(exc)))
            continue
        agent_mod.mark_quota_ok(_QUOTA_NAME[brain])
        if brain != ex:
            store.kv_set(f"task_executor:{task_id}", brain)
        return result
    # Nobody could take it. Resume on whichever brain said it comes back first;
    # a limit that named no time sorts last.
    brain, reset_at, raw = min(limited, key=lambda x: x[1] if x[1] else float("inf"))
    store.kv_set(f"task_executor:{task_id}", brain)
    raise _LimitPaused(brain, reset_at, raw)


#: The executors a simple run can move between, and the quota flag each one sets.
_SIMPLE_BRAINS = ("copilot", "claude")
_QUOTA_NAME = {"copilot": "copilot", "claude": "claude_cli"}


def _can_take_over(brain: str) -> bool:
    """Installed, and not known to be out of quota right now."""
    from . import agent as agent_mod
    if brain == "claude":
        return claude_cli.available() and not agent_mod.quota_down("claude_cli")
    return copilot_cli.available() and not agent_mod.quota_down("copilot")


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
    from . import routing
    if routing.enabled():
        routing.escalate(task_id, "the same failure twice")
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
    # The task's OWN tree. The change lives in its private worktree; checking the
    # shared checkout ran his tests against code the task never touched, so a
    # green result said nothing about the work.
    cwd = task_cwd(task_id, t["workspace"])
    outcome = await verify.check_tree(cwd, t["workspace"], _cwd(t["workspace"]))
    if not outcome.ran:
        return False   # no usable oracle — behave exactly as today
    cmd = outcome.command
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
    """Every git repo a task could touch — the superset, used for rollback.

    Was a second copy of the rule in `worktrees` and carried the same bug: it
    returned `[root]` whenever the workspace root was itself a repo, which for the
    booking workspace means the generated-context repo and none of the three
    services. Delegated now rather than restated, so the two cannot drift.
    """
    from . import worktrees
    return worktrees.all_repos_in(root)


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

async def _task_base(repo: Path, t: dict, task_id: int = 0) -> str:
    """The commit just before THIS task's first commit in this repo — "" if it
    made none.

    Read from git by time, not from a recorded branch point: #185 was told to
    work on an existing PR branch, and its review then diffed three repos
    against where the SHARED checkout had stood — 126 files of other people's
    work — and reported a "cross-repo contradiction" in a five-line wording
    fix (30 Sep)."""
    since = int(float(t.get("created_at") or 0)) - 1 if t.get("created_at") else 0
    if not since:
        # No task row to date it by (a caller holding only the workspace): the
        # recorded branch point, as before.
        mark = rollback_point(task_id).get(repo.name) if task_id else None
        return mark["sha"] if mark else "HEAD~1"
    rc, out = await repo_ops.git(repo, "git", "log", f"--since=@{since}", "--format=%H", "HEAD")
    shas = out.split() if rc == 0 else []
    if not shas:
        return ""
    rc, parent = await repo_ops.git(repo, "git", "rev-parse", "--verify", f"{shas[-1]}~1")
    # A repository's very first commit has no parent: diff against the empty tree.
    return parent.strip() if rc == 0 and parent.strip() else _EMPTY_TREE


_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


async def _already_pushed(task_id: int, t: dict) -> list[str]:
    """PR urls for work that is ALREADY on origin — the branch has an upstream
    with nothing ahead of it and an open PR. "" list when any of it is local."""
    try:
        root = Path(task_cwd(task_id, t.get("workspace")))
    except RuntimeError:
        return []
    urls: list[str] = []
    for repo in _repos_under(root):
        if not await _task_base(repo, t):
            continue
        rc, ahead = await repo_ops.git(repo, "git", "rev-list", "--count", "@{u}..HEAD")
        if rc != 0 or ahead.strip() != "0":
            return []
        rc, head = await repo_ops.git(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0 or not head.strip():
            return []
        try:
            url = await _verified_pr(repo, head.strip())
        except RuntimeError:
            return []
        urls.append(f"{repo.name}: {url}")
    return urls


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
        # The task's own tree: a worktree task commits there, never in the
        # shared checkout — reading that one found no diff and skipped the review.
        root = Path(task_cwd(task_id, t.get("workspace")))
    except RuntimeError:
        return ""
    diffs = []
    for repo in _repos_under(root):
        base = await _task_base(repo, t, task_id)
        if not base:
            continue                # this task committed nothing in this repo
        rc, out = await repo_ops.git(repo, "git", "diff", "--stat", base, "HEAD")
        if rc == 0 and out.strip():
            rc2, full = await repo_ops.git(repo, "git", "diff", base, "HEAD")
            if rc2 == 0 and full.strip():
                diffs.append((repo.name, out.strip(), full))
    if not diffs:
        return ""
    other = _second_reviewer(task_id)
    try:
        notes = await review.review_own_diff(
            "\n\n".join(f"### {name}\n{full[:20000]}" for name, _stat, full in diffs),
            t.get("workspace") or "", reviewer=other, cwd=str(root))
    except Exception:
        return ""
    if not notes:
        return ""
    stat = " · ".join(f"{name}: {s.splitlines()[-1].strip()}" for name, s, _ in diffs)
    who = f"{other} read the diff" if other else "I read my own diff"
    return f"\n\n🔍 {who} ({stat}):\n{clip.clip(notes, 900)}"


def _second_reviewer(task_id: int) -> str:
    """A brain other than the one that wrote the change, if one is up — else ''.

    ASTA_CROSS_REVIEW=0 keeps the old single-model self-review."""
    if os.environ.get("ASTA_CROSS_REVIEW", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""
    writer = _resolve_executor(task_id)
    return next((b for b in _SIMPLE_BRAINS if b != writer and _can_take_over(b)), "")


def _is_gate(tail: str) -> bool:
    """Is this run WAITING rather than finished?

    Case-insensitive: the brain writes "Waiting for your approval" or "waiting
    for your approval" depending on where in a sentence it lands, and its
    capitalisation must not be what decides whether Arun is told the work is
    done.
    """
    low = (tail or "").lower()
    return any(m.lower() in low for m in _GATE_MARKS)


async def _finish_code(task_id: int, t: dict, result: str, hops: int) -> None:
    """Route a finished code leg: paused at a gate → ask Arun; handoff → next
    repo in a fresh window; otherwise done with the diff."""
    from . import notify
    # Already finished. `_finish_code` re-enters itself on escalation and on a
    # repo handoff, and the done branch below has no memory — so task #88
    # "finished" four times and pushed four identical "✅ DONE" messages to his
    # phone at 12:42, 12:53, 12:59 and 13:07. One completion, one message.
    if (store.get_task(task_id) or {}).get("status") in _ALREADY_REPORTED:
        return
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
        await announce_context_check(task_id, t, result)
        return
    # A code task cannot finish before he has approved it. The brain saying
    # "PLAN READY" is one way to reach this gate; NOT having been approved is
    # the other, and it is the one that holds when the brain ignores the
    # instruction — which is exactly what the micro pipeline's own agent file
    # told it to do ("make the edit"). Its legs cannot write either (see
    # `_run_code_leg`), so whatever it produced here IS a plan.
    if _is_gate(tail) or not plan_approved(task_id):
        await announce_plan(task_id, t, result)
        return
    unfinished = _repos_still_needed(task_id, t, tail)
    if unfinished and hops < _MAX_REPO_HOPS:
        # The run talked about a repo it had no checkout for. Prepare it and keep
        # going in THIS task, rather than closing and letting the other half
        # become somebody's second task.
        #
        # `worktrees.create` picks repos from the task's own words, which is right
        # for cost and wrong for discovery: #88 named only booking-service, got
        # one worktree, then found the AP side mattered — and had nowhere to put
        # it. So it said "blocked… waiting on you for the AP-repo branch" and
        # stopped. One change across two repos became tasks #88 and #89, and #89
        # then re-implemented the first half.
        branch = store.kv_get(f"task_branch:{task_id}") or task_branch(t, task_id)
        root = Path(code_cwd(t["workspace"]))
        from . import worktrees as _wt
        with contextlib.suppress(Exception):
            await _wt.create(root, task_id, branch, *unfinished)
        ex = store.kv_get(f"task_executor:{task_id}") or "copilot"
        store.kv_del(f"task_session:{task_id}:{ex}")
        await notify.notify(
            f"🔁 Task #{task_id}: same change reaches {', '.join(unfinished)} — "
            f"checkout prepared, continuing in this task (window "
            f"{hops + 1}/{_MAX_REPO_HOPS}).", "task")
        result2 = await _run_code_leg(
            task_id,
            "Continue THIS change in the repo(s) you said were still outstanding: "
            + ", ".join(unfinished) +
            ". A checkout is now prepared for them on your branch — see THIS RUN "
            "below. Do NOT redo anything already committed; check `git log` first."
            + CODE_OVERRIDES + _branch_note(task_id, t) + _done_note(task_id, t),
            task_cwd(task_id, t["workspace"]), resume=False,
            effort=_impl_effort(_resolve_executor(task_id)),
            workspace=t["workspace"])
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return
        await _finish_code(task_id, t, result2, hops + 1)
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
    await complete(task_id, t, result)


_OUTCOME_FENCE = re.compile(r"```\s*outcome\s*\n(.*?)```", re.S | re.I)


_FOLLOWUP = "task_refine_from:"


_ABOUT_CI = re.compile(r"\b(?:ci|build|pipeline|checks?|component\s+tests?|ct|red|"
                       r"fail(?:ed|ing|ure|ures)?)\b", re.I)


#: A failed job's log is read from its END: the failure summary is there, and
#: the whole log was 153 MB for booking's component test (7 Oct) — the worker's
#: own download never finished, so it stopped "waiting on the log fetch".
CI_LOG_TAIL_BYTES = 3_000_000
_JOB_ID = re.compile(r"/actions/runs/\d+/job/(\d+)")
_CI_SIGNAL = re.compile(
    r"ComparisonFailure|AssertionError|AssertionFailedError|expected:\s*<|Expected:|"
    r"Tests run:.*(?:Failures|Errors): [1-9]|BUILD FAILURE|Failed scenarios|"
    r"\bFAILED\b|Caused by:|Exception:", re.I)
_LOG_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")


async def _github_token(repo: str) -> str:
    env = await repo_ops.github_env(ROOT, "-R", repo)
    if env and env.get("GH_TOKEN"):
        return env["GH_TOKEN"]
    proc = await asyncio.create_subprocess_exec(
        "gh", "auth", "token", env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    raw, _ = await asyncio.wait_for(proc.communicate(), 15)
    return raw.decode().strip()


async def _job_log_tail(repo: str, job_id: str, dest: Path | None = None) -> tuple[str, list[str]]:
    """(saved path, failure lines) from the last few MB of one failed job's log."""
    import httpx
    token = await _github_token(repo)
    if not token:
        return "", []
    async with httpx.AsyncClient(timeout=60) as client:
        first = await client.get(
            f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"}, follow_redirects=False)
        where = first.headers.get("location") if first.is_redirect else ""
        if not where:
            return "", []
        size = int((await client.head(where)).headers.get("content-length") or 0)
        start = max(0, size - CI_LOG_TAIL_BYTES)
        got = await client.get(where, headers={"Range": f"bytes={start}-{size - 1}"} if size else {})
    lines = [_LOG_STAMP.sub("", line) for line in got.text.splitlines()]
    hits: list[str] = []
    for line in lines:
        line = line.strip()
        if line and _CI_SIGNAL.search(line) and line not in hits:
            hits.append(line[:300])
    path = ""
    if dest is not None:
        with contextlib.suppress(OSError):
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / f"ci-job-{job_id}-tail.log"
            target.write_text("\n".join(lines))
            path = str(target)
    return path, hits[-15:]


async def _ci_failure_for(t: dict, feedback: str) -> str:
    """For "fix the CI": the failed jobs and what their logs say, read by Asta
    before the worker starts — so the worker does not download logs itself."""
    if os.environ.get("ASTA_CI_PREFETCH", "1") == "0" or not _ABOUT_CI.search(feedback or ""):
        return ""
    dest = None
    with contextlib.suppress(Exception):
        dest = Path(task_cwd(t["id"], t.get("workspace"))) / ".asta-ci"
    out = []
    for url in _pr_links(t)[:2]:
        try:
            pr = await asyncio.wait_for(_pr_state(url), 70)
        except Exception:                                      # noqa: BLE001
            continue
        if _checks_verdict(pr) != "red":
            continue
        failed = [c for c in _latest_checks(pr)
                  if (c.get("conclusion") or c.get("state") or "").upper() in _CI_FAILED]
        part = [f"{url}\nFailed checks:"] + [
            f"- {c.get('name')}: {c.get('detailsUrl') or c.get('targetUrl') or ''}"
            for c in failed[:6]]
        seen_jobs: set[str] = set()
        for c in failed[:4]:
            m = _JOB_ID.search(c.get("detailsUrl") or "")
            if not m or m.group(1) in seen_jobs:
                continue
            seen_jobs.add(m.group(1))
            try:
                path, hits = await asyncio.wait_for(
                    _job_log_tail(_repo_of(url), m.group(1), dest), 150)
            except Exception:                                  # noqa: BLE001
                continue
            if hits:
                part.append(f"From {c.get('name')} (end of its log"
                            + (f", saved at {path}" if path else "") + "):\n"
                            + "\n".join(f"  {h}" for h in hits))
        if len(part) == 1 + len(failed[:6]):
            with contextlib.suppress(Exception):
                why = await asyncio.wait_for(_why_red(pr, url, timeout=60), 130)
                if why:
                    part.append(f"From the failed logs:{why}")
        out.append("\n".join(part))
    return ("[CI failure Asta read from GitHub before you started — start from it; "
            "do not download logs in the background]\n" + "\n\n".join(out)) if out else ""


async def _record_followup_start(task_id: int, t: dict) -> None:
    """Where a follow-up run starts: each repo's HEAD and the status it left."""
    heads: dict[str, str] = {}
    with contextlib.suppress(Exception):
        from . import worktrees as _wt
        for repo in _wt.repos_in(Path(task_cwd(task_id, t.get("workspace")))):
            rc, sha = await repo_ops.git(repo, "git", "rev-parse", "HEAD")
            if rc == 0 and sha.strip():
                heads[str(repo)] = sha.strip()
    store.kv_set(_FOLLOWUP + str(task_id), _json.dumps(
        {"status": t.get("status", ""), "heads": heads}))


async def _followup_changed_nothing(task_id: int, t: dict, result: str) -> bool:
    """A follow-up run that ended without a commit or an edit is not DONE.

    7 Oct, #257: asked to fix the red CI on booking PR 1470, it started the log
    download in the background and stopped — "Waiting on both gh run view
    --log-failed fetches" — and was announced as "✅ DONE … Already pushed — the
    PR is updated", with CI still red and nothing changed."""
    raw = store.kv_get(_FOLLOWUP + str(task_id))
    if not raw:
        return False
    try:
        start = _json.loads(raw)
    except ValueError:
        return False
    heads = start.get("heads") or {}
    if not heads:
        return False
    for repo, sha in heads.items():
        rc, now = await repo_ops.git(Path(repo), "git", "rev-parse", "HEAD")
        if rc != 0 or now.strip() != sha:
            return False
        rc, dirty = await repo_ops.git(Path(repo), "git", "status", "--porcelain")
        if rc != 0 or dirty.strip():
            return False
    from . import notify, prname
    store.kv_del(_FOLLOWUP + str(task_id))
    back = start.get("status") or "done"
    store.update_task(task_id, status=back if back in REFINABLE else "done",
                      result=result, finished_at=time.time())
    tail = [line.strip() for line in (readable_outcome(result) or "").splitlines() if line.strip()]
    stopped = tail[-1][:300] if tail else "(no report)"
    prs = _pr_links(store.get_task(task_id) or t)
    where = (" " + ", ".join(prname.name_links(u) for u in prs) + " is unchanged"
             + (" and still red." if back == "pr_ci_failed" else ".")) if prs else ""
    await notify.notify(
        f"⚠️ #{task_id} {t['title']} — no change made. It stopped at: “{stopped}”."
        f"{where} Say 'fix #{task_id}' to try again.", "task")
    store.record_outcome("task", "followup_no_change", subject=str(task_id), detail=stopped[:200])
    return True


def readable_outcome(result: str) -> str:
    """The worker's report as he should read it: its ```outcome``` block turned
    into its summary, never raw JSON — and its question kept even when the prose
    only says "the two options above" (7 Oct, #263: the options were in the
    block, the message showed neither)."""
    text = result or ""
    found = _OUTCOME_FENCE.search(text)
    if not found:
        return text
    summary = ""
    try:
        data = _json.loads(found.group(1))
        summary = " ".join(str(data.get(k) or "") for k in ("summary", "notes")).strip()
    except (ValueError, TypeError, AttributeError):
        summary = ""
    prose = _OUTCOME_FENCE.sub("", text).strip()
    if summary and summary not in prose:
        prose = f"{prose}\n\n{summary}".strip()
    return prose


async def announce_context_check(task_id: int, t: dict, result: str) -> None:
    """The cheap early gate — intent unclear, asked before any discovery spend.
    Shared by both task engines, so the question reads the same either way."""
    from . import notify
    store.kv_set(f"task_gate:{task_id}", "context")
    store.update_task(task_id, status="awaiting_approval", result=result)
    await notify.notify(
        f"❓ #{task_id} {t['title']} — quick context check before I dig in:\n\n"
        f"{_phone_text(readable_outcome(result), 900)}\n\n"
        f"Reply with the answer, or 'reject task {task_id}'.", "task")


def note_waiting(task_id: int) -> str:
    """What he told this task after it started, not yet delivered to it."""
    return (store.kv_get(f"task_addenda:{task_id}") or "").strip('"').strip()


#: Sent with his buffered note when a plan written without it comes back.
REPLAN_WITH_NOTE = ("Revise the plan: Arun told you this after you started, so the plan "
                    "above was written without it. Follow it.")


async def announce_plan(task_id: int, t: dict, result: str) -> None:
    """Park the task at its plan gate and put the plan on his phone."""
    from . import notify
    store.kv_set(f"task_gate:{task_id}", "plan")
    # The brain signs off with its own "Reply 'PLAN APPROVED' to proceed",
    # and the push below adds the real buttons underneath it. Two asks in a
    # row, the first one unactionable, is exactly the clutter he pointed at.
    result = _ASK_LINE.sub("", result).rstrip()
    store.update_task(task_id, status="awaiting_approval", result=result)
    note = note_waiting(task_id)
    if note:
        # 8 Oct, #268: "dont revert the whole changes of him, only the startup
        # flag ones" arrived while it planned a full revert — and a "go" task's
        # plan goes ahead on its own. A plan that predates his note goes back.
        await notify.notify(f"📋 #{task_id}: the plan came back before your note — "
                            f"re-planning with it: “{clip.clip(note, 200)}”", "task")
        if _graph().manages(task_id):
            store.kv_set(f"task_goes_on:{task_id}", "1")   # go_on re-plans, see there
        else:
            def _replan() -> None:
                try:
                    reply(task_id, REPLAN_WITH_NOTE)
                except ValueError as exc:
                    store.record_outcome("task", "replan with note failed",
                                         subject=str(task_id), detail=str(exc)[:200])
            asyncio.get_running_loop().call_soon(_replan)
        return
    from . import go
    why = go.no_ask(task_id, result)
    if why:
        # His rule, 30 Sep: a plan for a big change is his to approve; being
        # asked again for a one-line change, or for one he already said "do it"
        # on, is drag. He still gets the plan — to read, and to stop.
        await notify.notify(
            f"📋 *PLAN #{task_id}* — going ahead ({why})\n{clip.clip(t['title'], 90)}\n\n"
            f"{_phone_text(result, 1100)}\n\n"
            f"— — —\n"
            + ("I'll raise the PR when it's done. " if go.ships(task_id)
               else "It stays local until you say *raise PR*. ")
            + f"Say *stop {task_id}* if this is not what you meant.", "task")
        born = float(t.get("created_at") or 0)
        if _graph().manages(task_id):
            # The workflow engine is still inside this step; its gate, next,
            # reads this and goes on without waiting. Approving from here raced
            # the gate — the answer landed before it was waiting, and the
            # finished work came back as a "plan" to approve (1 Oct, bench).
            store.kv_set(f"task_goes_on:{task_id}", "1")
        elif go.after_plan_seconds() <= 0:
            await _go_on(task_id, born)          # no pause asked for: go on now
        else:
            asyncio.get_running_loop().call_later(
                go.after_plan_seconds(), lambda: asyncio.ensure_future(_go_on(task_id, born)))
        return
    await notify.notify(
        f"📋 *PLAN #{task_id}*\n{clip.clip(t['title'], 90)}\n\n"
        f"{_phone_text(result, 1100)}\n\n"
        f"— — —\n"
        f"👍 *approve task {task_id}*\n"
        f"👎 *reject task {task_id}*\n"
        f"✏️ …or just reply with the changes", "task")


async def _go_on(task_id: int, born: float = 0.0) -> None:
    """Continue past a plan gate he is not being asked at — unless he stopped it."""
    t = store.get_task(task_id)
    if t and born and abs(float(t.get("created_at") or 0) - born) > 1:
        return                          # not the task this was scheduled for
    if not t or t["status"] != "awaiting_approval" \
            or store.kv_get(f"task_gate:{task_id}") != "plan":
        return
    try:
        await approve(task_id)
    except ValueError as exc:
        store.add_task_event(task_id, "go", f"could not continue: {exc}"[:200])


async def ship_as_told(task_id: int) -> None:
    """He said "…and raise the PR": the finished task is pushed without a second
    ask. A failure is said once, with the reason — never a silent local commit."""
    from . import go, notify
    if not (go.enabled() and go.ships(task_id)):
        return
    t = store.get_task(task_id)
    if not t or t["status"] != "done" or t.get("pr_urls"):
        return
    try:
        await ship(task_id)
    except (ValueError, RuntimeError) as exc:
        await notify.notify(f"❌ #{task_id}: not fully shipped — {str(exc)[:500]}",
                            "task", urgency="direct")


async def complete(task_id: int, t: dict, result: str) -> None:
    """The one way a code task is marked done and announced — both engines."""
    from . import go, notify
    if (store.get_task(task_id) or {}).get("status") in _ALREADY_REPORTED:
        return
    if await _followup_changed_nothing(task_id, t, result):
        return
    # The branch the work is ACTUALLY on, read from git rather than from the name
    # Asta chose. Both were unpushed for #88/#89 while the pushed branch —
    # `hotpriority/booking-equipment-mapping` — was one the agent cut itself, with
    # no upstream and unknown to Asta. `pr_urls` stayed empty on both tasks, so
    # nothing watched a PR or reported CI.
    landed = committed_so_far(task_id, t)
    from . import worktrees
    workspace = t.get("workspace")
    if workspace and worktrees.exists(Path(code_cwd(workspace)), task_id) and not landed:
        root = Path(task_cwd(task_id, workspace))
        dirty = False
        inspect_error = ""
        for repo in worktrees.repos_in(root):
            rc, changes = await repo_ops.git(repo, "git", "status", "--porcelain")
            if rc:
                inspect_error = f"Could not inspect {repo.name} for code changes: {changes[:150]}"
                break
            if changes.strip():
                dirty = True
                break
        if not dirty and not await _already_pushed(task_id, t):
            if not inspect_error and re.search(
                r"\b(?:no (?:code )?changes? (?:needed|required)|"
                r"already implemented)\b", result, re.I
            ):
                store.update_task(task_id, status="done", result=result,
                                  finished_at=time.time())
                await notify.notify(
                    f"ℹ️ No change needed — #{task_id} {t['title']}\n\n"
                    f"{_phone_text(result, 700)}\n\nNo local changes or PR to ship.",
                    "task")
                return
            reason = (inspect_error or
                      "The code task reported completion, but its worktree has no changes or verified PR.")
            store.update_task(task_id, status="failed", result=result, error=reason,
                              finished_at=time.time())
            await notify.notify(
                f"⚠️ #{task_id} {t['title']} — not completed: {reason} "
                f"Reply with 'task {task_id}: continue the implementation' to retry "
                f"this task in its own session.", "task")
            return
    if landed:
        store.kv_set(f"task_landed:{task_id}", _json.dumps(landed))
        expected = (store.kv_get(f"task_branch:{task_id}") or "").strip()
        strayed = [d for d in landed if expected and d["branch"] != expected]
        # A branch that already existed on origin is one he sent the task to
        # ("fix it on the existing PR branch") — adopted, not warned about.
        for d in list(strayed):
            with contextlib.suppress(Exception):
                repo = Path(task_cwd(task_id, t["workspace"])) / d["repo"]
                rc, _ = await repo_ops.git(repo, "git", "rev-parse", "--verify",
                                           f"origin/{d['branch']}")
                if rc == 0:
                    strayed.remove(d)
                    store.kv_set(f"task_branch:{task_id}", d["branch"])
        if strayed:
            result += ("\n\n⚠️ Committed on a branch Asta did not prepare: "
                       + ", ".join(f"{d['repo']} → {d['branch']}" for d in strayed)
                       + f" (expected {expected}). Shipping will use what is above.")
    store.update_task(task_id, status="done", result=result, finished_at=time.time())
    _learn_from(task_id, t["title"], result)
    waste = _audit_note(task_id)
    own = await _self_review(task_id, t, result)
    from . import prname
    multi_base = bool(_BOTH.search(f"{t['title']}\n{t['prompt']}\n{go.words(task_id)}") and
                      _RELEASE.search(f"{t['title']}\n{t['prompt']}\n{go.words(task_id)}"))
    pushed = [] if multi_base else await _already_pushed(task_id, t)
    if pushed:
        # It is on origin and its PR exists: say that, not "nothing pushed — say
        # raise PR" underneath a report that opens "Pushed." (#185, 30 Sep).
        store.update_task(task_id, status="shipped", pr_urls="\n".join(pushed),
                          pr_state="OPEN", pr_checked_at=0.0)
        await notify.notify(
            f"✅ DONE — #{task_id} {t['title']}\n\n{_phone_text(result, 700)}{own}\n\n"
            f"Already pushed — the PR is updated:\n"
            + "\n".join("• " + prname.name_links(u) for u in pushed)
            + f"\nI'm watching its CI and will tell you how it ends.{waste}", "task")
        return
    if go.enabled() and go.ships(task_id):
        # He already said "…and raise the PR". Asking "say ship" now would be
        # the second ask for one instruction.
        await notify.notify(
            f"🛠 Implementation ready — #{task_id} {t['title']}\n\n"
            f"{_phone_text(result, 700)}{own}\n\n"
            f"Raising the required PR{'s' if multi_base else ''} now, as you said; "
            f"not calling them shipped until GitHub confirms.{waste}", "task")
        await ship_as_told(task_id)
        return
    open_prs = _pr_links(store.get_task(task_id) or t)
    if open_prs:
        # Feedback on work that already has its PR: the new commits are on that
        # PR's branch, and "ship" pushes them onto it. 7 Oct, #257: booking PR
        # 1470 was open and the update still said "say *raise PR*".
        await notify.notify(
            f"🛠 Update ready — #{task_id} {t['title']}\n\n"
            f"{_phone_text(result, 700)}{own}\n\n"
            f"Committed on the PR branch, not pushed yet. Say *ship* and it goes onto "
            + ", ".join(prname.name_links(u) for u in open_prs)
            + f" — no new PR.{waste}", "task")
        return
    await notify.notify(
        f"🛠 Local implementation ready — #{task_id} {t['title']}\n\n"
        f"{_phone_text(result, 700)}{own}\n\n"
        f"Not shipped — say *raise PR* when you're happy, "
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
    # One definition, in worktrees. This was the third inline copy of the rule and
    # carried the same bug as the other two: `[root]` whenever the workspace root
    # is itself a repo, which for the booking workspace is the generated-context
    # repo and none of the three services.
    from . import worktrees as _wt
    repos = [r for r in _wt.repos_in(root) if r.resolve() != ROOT.resolve()]
    if not repos:
        return []
    branch = task_branch(t, task_id)

    # A private checkout per repo rather than moving the shared one. Two tasks
    # can then run at the same time — which is the whole point — and the checkout
    # Arun has open in his editor is never touched. `start_branch` remains for
    # anything that genuinely needs the shared tree.
    from . import worktrees
    try:
        # The task's own words decide which repos are prepared. A one-line change
        # in the booking service should not cost three fetches and three
        # checkouts — and two tasks on different services do not conflict, so
        # neither should be paying for the other's repos. Falls back to all when
        # nothing is named, because under-preparing breaks a run and
        # over-preparing only costs a fetch.
        results = await worktrees.create(root, task_id, branch,
                                         t.get("title") or "", t.get("goal") or "",
                                         t.get("prompt") or "")
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


def first_code_prompt(task_id: int, t: dict) -> str:
    """The opening prompt of a code task — one definition for both engines."""
    prompt = t["prompt"]
    if _agent_for(t):
        # micro's agent file is self-contained; the rider is solo-only.
        if _pipeline_for(task_id) == "full":
            prompt += CODE_OVERRIDES
    else:
        prompt += repo_ops.playbook_block(Path(_cwd(t["workspace"])))
    # How far the context map has fallen behind the code it describes — on
    # EVERY pipeline, because micro is the tier most likely to answer from
    # the map alone. Empty for a workspace whose context is current, so a
    # healthy run's prompt is unchanged.
    # Three advisory blocks. Each is a note for the run, so a failure in any
    # of them must cost a paragraph, never the task.
    with contextlib.suppress(Exception):
        from . import refresh as _refresh
        prompt += (_refresh.trust_note(t["workspace"] or "")
                   + _branch_note(task_id, t) + _done_note(task_id, t)
                   + _context_note())
    return prompt


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
    if t["kind"] in ("analysis", "teams_draft"):
        from . import context_pack
        prompt += context_pack.build(task_id, "analysis" if t["kind"] == "analysis" else "draft")
    if t["kind"] == "analysis":
        # A question about one booking starts from what its logs say, read in
        # code per flow — not from whatever a keyword search happens to find.
        with contextlib.suppress(Exception):
            from . import booking_case
            prompt += await asyncio.wait_for(booking_case.evidence(prompt), 60)
    elif t["kind"] == "code":
        prompt = first_code_prompt(task_id, t)
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
                    # `task_cwd`, NOT `_cwd`. The branches above are prepared in a
                    # private worktree per task, and every OTHER leg — escalation,
                    # repo handoff, verify retry — already runs there. This first
                    # leg ran in the SHARED checkout, and the two are different
                    # trees on different branches.
                    #
                    # Task #89 is what that costs. Its micro leg worked in the
                    # shared tree, cut `hotpriority/booking-equipment-mapping`
                    # itself and committed there; it then escalated, and the
                    # escalated leg opened the worktree, found nothing, and
                    # implemented the whole change again. Two divergent commits of
                    # one change, in two trees, on two branches, neither of them
                    # the one Asta was tracking — and Arun's own checkout moved
                    # underneath his editor, which is the thing worktrees exist to
                    # prevent.
                    result = await _run_code_leg(
                        task_id, prompt, task_cwd(task_id, t["workspace"]),
                        resume=False, effort=_effort_for("code", _resolve_executor(task_id)),
                        workspace=t["workspace"])
                else:
                    result = await _run_simple(task_id, t, prompt)
        else:
            async with investigation_slot(task_id):
                store.update_task(task_id, status="running", started_at=time.time())
                result = await _run_simple(task_id, t, prompt)
        if (store.get_task(task_id) or {}).get("status") in FINAL:
            return   # rejected while it ran — drop the result, stay quiet
        if t["kind"] == "teams_draft" and _notes_about(result, t["teams_chat"]):
            # It reads as notes about them, not a message to them: a finding for
            # him, never something to send.
            store.update_task(task_id, status="done", result=result, finished_at=time.time())
            await notify.notify(f"🔎 #{task_id} {t['title']}\n\n{clip.clip(result, 900)}", "task")
        elif t["kind"] == "teams_draft":
            store.update_task(task_id, status="awaiting_approval", result=result,
                              finished_at=time.time())
            await notify.notify(
                f"📝 Task #{task_id} — a message to send to {t['teams_chat']} on Teams:\n\n"
                f"{clip.clip(result, 600)}\n\n"
                f"'approve task {task_id}' SENDS exactly this to {t['teams_chat']}. "
                f"'reject task {task_id}' drops it.",
                "task")
        elif t["kind"] == "code" and agent:
            await _finish_code(task_id, t, result, hops=0)
        else:
            store.update_task(task_id, status="done", result=result,
                              finished_at=time.time())
            _learn_from(task_id, t["title"], result)
            # A colleague is waiting on this one: it reaches him as ONE decision —
            # who asked, what was found, the reply, send? — not as a task report
            # he then has to turn into a message himself. See app/answers.py.
            from . import answers
            if t.get("teams_chat") and await answers.present_task(task_id, t, result):
                return
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
    if t["status"] == "awaiting_approval" and store.kv_get(f"task_gate:{task_id}") == "change":
        raw = _json.loads(store.kv_get(f"task_proposal:{task_id}") or "{}")
        store.update_task(task_id, status=raw.get("status") or "done")
        store.kv_del(f"task_gate:{task_id}")
        asyncio.get_running_loop().create_task(propose_change(
            task_id, f"{raw.get('feedback', '')}\n\nArun on your earlier plan:\n{text}\n\n"
                     f"Your earlier plan:\n{readable_outcome(raw.get('plan') or '')[:2500]}"))
        return f"Task #{task_id}: re-planning with your changes — you'll see the new plan first."
    if t["kind"] != "code" or t["status"] != "awaiting_approval":
        raise ValueError(f"task #{task_id} is not a code task awaiting approval "
                         f"(kind={t['kind']}, status={t['status']})")
    if _graph().manages(task_id) and store.kv_get(f"task_gate:{task_id}") == "verify":
        approved = text.strip().upper().startswith("PLAN APPROVED")
        if approved and store.kv_get(f"task_gate_blocked:{task_id}") == "1":
            return (f"Task #{task_id} is blocked with unfinished work; approval cannot "
                    "mark it done. Send a direction to unblock it or reject the task.")
        store.update_task(task_id, status="running")
        _graph().answer(task_id, {"approved": approved, "text": text})
        return (f"Task #{task_id}: accepting the check failure as-is."
                if approved else f"Task #{task_id}: continuing with your direction.")
    # A plan approved WITH amendments is still an approval. Exact-match meant
    # "PLAN APPROVED — but hold the PDF half" was scored as a rejection, run at
    # planning effort, and sent back for a whole extra planning round before it
    # would write a line. Narrowing scope at the gate is the normal case.
    approved, full_text = record_answer(task_id, t, text)
    store.update_task(task_id, status="running")
    if _graph().manages(task_id):
        _graph().answer(task_id, {"approved": approved, "text": full_text})
    else:
        job = asyncio.create_task(_resume_worker(task_id, full_text, approved=approved))
        _running[task_id] = job
        job.add_done_callback(lambda _j, tid=task_id: _running.pop(tid, None))
    return (f"Task #{task_id}: plan approved — implementing now."
            if approved
            else f"Task #{task_id}: feedback sent to the pipeline — it will re-plan.")


def record_answer(task_id: int, t: dict, text: str) -> tuple[bool, str]:
    """Everything an answer at the plan gate records — whoever gives it: him,
    or the go-ahead rules. Returns (approved, the text the next leg receives)."""
    approved = text.strip().upper().startswith("PLAN APPROVED")
    # Did the plan hold? The cheapest honest measure of planning quality: a plan
    # Arun approves as-is versus one he sends back.
    # Three states, not two. "Approved" and "approved but cut in half" are both
    # approvals to the pipeline and very different answers to the only question
    # this metric exists to ask — did the plan hold? Folding them together would
    # make planning look better every time he had to correct the scope.
    verdict = ("approved" if text.strip().upper() == "PLAN APPROVED"
               else "approved_amended" if approved else "replanned")
    store.record_outcome("plan", verdict, subject=str(task_id))
    if approved:
        # From here the legs may write. Before it, they could not — see
        # `_run_code_leg`.
        mark_approved(task_id)
    # The plan he just approved is the definition of done — keep it (no-op unless
    # ASTA_TASK_SPEC is on) so a later compacted leg can re-anchor to it.
    if approved:
        from . import routing, task_spec
        task_spec.capture(task_id, t.get("result") or "")
        # Always kept: a later FRESH leg (a brain switch, an escalation, a lost
        # session) is handed this in its context pack as the definition of done.
        store.kv_set(f"task_plan:{task_id}", (t.get("result") or "")[:6000])
        if routing.enabled():
            # Two-stage routing: the plan he approved says how big the change
            # really is, and the implementation runs at that tier.
            from .graph import outcome as _outcome
            routing.on_plan(task_id, t.get("result") or "", _outcome.reported(task_id, 0))
    store.add_task_event(task_id, "gate", ("approved: " if approved else "answer: ") + text[:200])
    # Anything buffered by augment() while the task ran rides in now, on the user's
    # gate action — so mid-flight additions land without a session restart.
    full_text = text + _drain_addenda(task_id)
    return approved, full_text


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
    await _tell_the_limit_once(exc.brain, reset_at)
    return


def _gather_seconds() -> float:
    try:
        return float(os.environ.get("ASTA_LIMIT_NOTICE_GATHER_SECONDS", "20") or 20)
    except ValueError:
        return 20.0


async def _tell_the_limit_once(brain: str, reset_at: float | None) -> None:
    """ONE message when a brain runs out — everything paused and waiting, when
    it renews, and exactly what the other brain can do. It was one message per
    paused task, each offering a switch that was sometimes not possible."""
    from . import brains, notify
    key = f"limit_notice:{brain}"
    try:
        if time.time() - float(store.kv_get(key) or 0) < 15 * 60:
            return                            # already told; the list is in "brain status"
    except ValueError:
        pass
    store.kv_set(key, str(time.time()))
    # Tasks hit the same wall seconds apart: gather them, then say it once.
    await asyncio.sleep(_gather_seconds())
    q = queue_summary()
    paused = []
    for tid in q["paused_ids"][:6]:
        row = store.get_task(tid) or {}
        paused.append(f"#{tid} {(row.get('title') or '')[:50]}")
    when = (_dt.datetime.fromtimestamp(reset_at).strftime("%-I:%M%p").lower()
            if reset_at else "")
    cp = brains.copilot_status()
    first = q["paused_ids"][0] if q["paused_ids"] else None
    sooner = f" Say “resume task {first}” to try sooner." if first else ""
    if brain == "copilot":
        head = (f"⏸ Copilot's monthly quota is out — it renews {cp['resets_on']}.")
    else:
        head = "⏸ Claude hit its session limit" + (f" — it resets {when}." if when else ".")
    if when:
        renew = ("I'll run it again at " if len(q["paused_ids"]) <= 1 else
                 "I'll run them again at ") + when + "." + sooner
    else:
        renew = (f"It won't renew on its own soon — say “resume task {first}” when you want "
                 f"it picked up." if first else "")
    other = "claude" if brain == "copilot" else "copilot"
    if other == "copilot" and cp["out"]:
        switch = f"Copilot's monthly quota is out until {cp['resets_on']}, so this waits for Claude."
    elif other == "claude" and claude_cli.limited_until():
        switch = "Claude is limited too right now."
    else:
        switch = f"Say “use {other}” to carry on there now."
    lines = [head]
    if paused:
        lines.append("Nothing lost — paused: " + "; ".join(paused))
    if q["queued"]:
        lines.append(f"Waiting their turn: {q['queued']}.")
    lines += [x for x in (renew, switch) if x]
    await notify.notify("\n".join(lines), "task", urgency="direct", considered=True)


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
    if t["kind"] != "code":
        # A read-only analysis or a draft holds no pinned session and no git
        # state, so resuming it IS running it again — from its own prompt, with
        # its guardrails, on whichever brain is now set for it.
        job = asyncio.create_task(_worker(task_id))
        _running[task_id] = job
        job.add_done_callback(lambda _j, tid=task_id: _running.pop(tid, None))
        return f"Task #{task_id}: running it again{note}."
    if _graph().manages(task_id):
        # The checkpoint knows where it stopped; the leg that was cut short
        # knows to continue rather than start again (code_graph._leg).
        _graph().carry_on(task_id)
        return f"Task #{task_id}: resuming{note} from its last checkpoint."
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


#: How long after a restart an interrupted task waits before it picks itself up.
#: Long enough for the watchers and MCP probes to settle, short enough that he
#: does not notice; the resume sweep's own interval decides the granularity.
RESTART_RESUME_SECONDS = float(os.environ.get("ASTA_RESTART_RESUME_SECONDS", "90"))


async def recover_orphans() -> list[int]:
    """Tasks the database still calls running, in a process that owns no workers.

    A fresh process has no `_running` entries, so anything still marked running
    was interrupted — a restart, a crash, a laptop that slept. Nothing noticed:
    the row said running for ever, `live_tasks_for` kept handing it every message
    Arun sent, and no work was happening behind it. That is the shape of "why
    still 117 is running doesn't makes sense".

    Marked paused with a due time rather than resumed inline, so the existing
    sweep does the resuming — one resume path, already tested, and a task whose
    brain is out of quota waits instead of failing twice.
    """
    from . import notify
    picked: list[int] = []
    now = time.time()
    for t in store.list_tasks(limit=200):
        if t["status"] != "running" or is_running(t["id"]):
            continue
        brain = _resolve_executor(t["id"])
        store.kv_set(f"task_paused:{t['id']}", _json.dumps(
            {"brain": brain, "reset_at": None, "raw": "interrupted by a restart", "at": now}))
        store.kv_set(f"task_resume_at:{t['id']}", str(now + RESTART_RESUME_SECONDS))
        store.update_task(t["id"], status="paused", error="interrupted by a restart")
        store.record_outcome("task", "orphaned", subject=str(t["id"]), detail=brain)
        picked.append(t["id"])
    if picked:
        listing = ", ".join(f"#{i}" for i in picked)
        await notify.notify(
            f"♻️ Asta restarted while {listing} {'was' if len(picked) == 1 else 'were'} "
            f"running — picking {'it' if len(picked) == 1 else 'them'} back up now.",
            "task", urgency="ambient")
    return picked


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
        if store.kv_get(f"task_gate:{task_id}") == "change":
            plan, back = _apply_proposal(task_id)
            store.update_task(task_id, status=back if back in REFINABLE else "done")
            mark_approved(task_id)
            await refine(task_id, plan, code_change=True, approved_plan=True)
            # His word was an approval, so he hears that — the same line as at the
            # plan gate. "continuing the existing diff with your feedback" read
            # as if his "approve" had been taken as feedback (8 Oct, #268).
            return f"Task #{task_id}: plan approved — implementing now."
        if store.kv_get(f"task_gate:{task_id}") == "context":
            return reply(task_id, "Your understanding is correct — proceed to "
                                  "discovery and planning.")
        if store.kv_get(f"task_gate:{task_id}") == "verify":
            return reply(task_id, "PLAN APPROVED")
        return reply(task_id, "PLAN APPROVED")
    if t["kind"] != "teams_draft" or t["status"] != "awaiting_approval":
        raise ValueError(f"task #{task_id} is not a draft awaiting approval "
                         f"(kind={t['kind']}, status={t['status']})")
    if _notes_about(t["result"] or "", t["teams_chat"]):
        store.update_task(task_id, status="done")
        return (f"Not sent — #{task_id} reads as notes about {t['teams_chat']}, not a message "
                f"to them. Kept as a finding for you.")
    from . import senior
    try:
        with senior.approved():       # he approved this draft
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




_RELEASE = re.compile(r"\b(release/[\w.\-]+)|\brelease(?:\s+branch)?[\s:/]*v?(\d+(?:\.\d+){1,3})\b"
                      r"|\b(?:branch\s+)v?(\d+\.\d+(?:\.\d+){0,2})\b", re.I)
_BOTH = re.compile(r"\bboth\b|\bas\s+well\s+as\b|\band\s+also\b|\balso\s+(?:in|on|to|into)\b|"
                   r"\btwo\s+(?:separate\s+)?branches\b|\beach\s+branch\b", re.I)


async def _other_bases(task_id: int, t: dict, repo: Path, base: str) -> list[str]:
    """Base branches, besides the one the task was cut from, that he asked for
    the same change on — only ones that exist on the remote."""
    from . import go
    text = f"{t.get('title', '')}\n{t.get('prompt', '')}\n{go.words(task_id)}"
    if not _BOTH.search(text):
        return []
    found: list[str] = []
    for m in _RELEASE.finditer(text):
        name = m.group(1) or f"release/{m.group(2) or m.group(3)}"
        name = name.rstrip(".,;:)")
        if name in found or f"origin/{name}" == base or name == base:
            continue
        rc, _ = await repo_ops.git(repo, "git", "rev-parse", "--verify", f"origin/{name}")
        if rc != 0:
            await repo_ops.git(repo, "git", "fetch", "origin", name, timeout=120)
            rc, _ = await repo_ops.git(repo, "git", "rev-parse", "--verify", f"origin/{name}")
        if rc != 0:
            raise RuntimeError(f"{repo.name}: requested base {name} is not available on origin")
        found.append(name)
    return found[:3]


async def _port(repo: Path, cur: str, base: str, other: str) -> str:
    """Put this branch's commits onto `other` as a branch of its own, push it
    and open its PR. A failed required port is never a successful shipment."""
    import tempfile
    branch = f"{cur}-{other.replace('/', '-')}"[:120]
    rc, commits = await repo_ops.git(repo, "git", "rev-list", "--reverse", f"{base}..HEAD")
    picks = commits.split()
    if rc != 0 or not picks:
        raise RuntimeError(f"{repo.name}: no commits to port from {base} onto {other}")
    where = Path(tempfile.mkdtemp(prefix="asta-port-")) / "wt"
    try:
        rc, out = await repo_ops.git(repo, "git", "worktree", "add", "-B", branch,
                                     str(where), f"origin/{other}", timeout=120)
        if rc != 0:
            raise RuntimeError(f"{repo.name}: could not cut {branch}: {out[:160]}")
        rc, out = await repo_ops.git(where, "git", "cherry-pick", *picks, timeout=120)
        if rc != 0:
            await repo_ops.git(where, "git", "cherry-pick", "--abort")
            raise RuntimeError(f"{repo.name}: does not apply cleanly on {other}: {out[:160]}")
        rc, out = await repo_ops.git(where, "git", "push", "-u", "origin", branch, timeout=300)
        if rc != 0:
            raise RuntimeError(f"{repo.name}: push to {other} failed: {out[:160]}")
        rc, out = await repo_ops.git(where, "gh", "pr", "create", "--fill", "--base", other,
                                     "--head", branch, timeout=300)
        if rc != 0 and "already exists" not in out:
            raise RuntimeError(f"{repo.name}: pushed {branch}, PR failed: {out[:160]}")
        return await _verified_pr(where, branch, other, repo_name=repo.name)
    finally:
        await repo_ops.git(repo, "git", "worktree", "remove", "--force", str(where))


_BRANCH_NAMED = re.compile(r"\b((?:feature|bugfix|hotfix|fix|release|chore)/[\w.\-/]+[\w])")


async def _verified_pr(repo: Path, head: str, base: str = "",
                       *, repo_name: str = "") -> str:
    rc, raw = await repo_ops.git(repo, "gh", "pr", "view", head, "--json",
                                 "url,headRefName,baseRefName", timeout=60)
    if rc != 0:
        raise RuntimeError(f"{repo.name}: could not verify PR for {head}: {raw[:180]}")
    try:
        pr = _json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{repo.name}: invalid PR receipt for {head}") from exc
    url = pr.get("url") or ""
    if (pr.get("headRefName") != head or
            (base and pr.get("baseRefName") != base.removeprefix("origin/")) or
            not re.fullmatch(r"https://github\.com/[^/]+/" +
                             re.escape(repo_name or repo.name) + r"/pull/\d+", url)):
        raise RuntimeError(f"{repo.name}: PR head/base/repository does not match {head}")
    return url


async def _push_named_branches(task_id: int, t: dict) -> list[str]:
    """Push branches the task NAMES (in its brief or its result) that are ahead
    of origin — the existing-PR case. Returns "repo: url" per branch pushed."""
    from . import worktrees as _wt
    text = f"{t.get('prompt', '')}\n{t.get('result', '')}\n{store.kv_get(f'task_branch:{task_id}') or ''}"
    names = list(dict.fromkeys(m.group(1).rstrip(".,;:)") for m in _BRANCH_NAMED.finditer(text)))
    if not names:
        return []
    named_repos = {m.group(1) for m in re.finditer(
        r"github\.com/[^/\s]+/([^/\s]+)/pull/\d+", t.get("prompt", ""), re.I)}
    try:
        root = Path(_cwd(t.get("workspace")))
    except Exception:                                          # noqa: BLE001
        return []
    repos = _wt.repos_in(root)
    named_repos.update(r.name for r in repos if re.search(
        rf"(?<![\w-]){re.escape(r.name)}(?![\w-])",
        f"{t.get('title', '')}\n{t.get('prompt', '')}", re.I))
    if not named_repos and len(repos) != 1:
        raise RuntimeError("existing PR target is ambiguous — name its repository")
    if named_repos and not any(r.name in named_repos for r in repos):
        raise RuntimeError(f"existing PR target {', '.join(sorted(named_repos))} "
                           "is not in this workspace; no branch was pushed")
    out: list[str] = []
    for repo in repos:
        if named_repos and repo.name not in named_repos:
            continue
        for b in names:
            rc, _ = await repo_ops.git(repo, "git", "rev-parse", "--verify", b)
            if rc != 0:
                continue
            rc, ahead = await repo_ops.git(repo, "git", "rev-list", "--count", f"origin/{b}..{b}")
            if rc != 0 or not ahead.strip().isdigit() or int(ahead.strip()) == 0:
                continue
            url = await _verified_pr(repo, b)
            requested_prs = set(re.findall(
                r"\bPR\s*#?(\d+)\b|github\.com/[^/\s]+/[^/\s]+/pull/(\d+)",
                f"{t.get('title', '')}\n{t.get('prompt', '')}", re.I))
            expected = {num for pair in requested_prs for num in pair if num}
            if expected and url.rsplit("/", 1)[-1] not in expected:
                raise RuntimeError(f"{repo.name}: {b} is not the requested PR "
                                   f"#{', #'.join(sorted(expected))}")
            rc, msg = await repo_ops.git(repo, "git", "push", "origin", b, timeout=300)
            if rc != 0:
                raise RuntimeError(f"{repo.name}: push of {b} failed: {msg[:300]}")
            out.append(f"{repo.name}: {url}")
            store.kv_set(f"task_branch:{task_id}", b)
    return out


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
    root = Path(task_cwd(task_id, t["workspace"]))
    # Same rule, same place. This copy mattered most: with the old shortcut,
    # shipping looked for the task's branch in the generated-context repo and
    # would have raised no PR at all for the services the work was actually in.
    from . import worktrees as _wt
    repos = _wt.repos_in(root)
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
        base = await _wt._base_branch(repo)
        rc, ahead = ((1, "") if not base else
                     await repo_ops.git(repo, "git", "log", "--oneline", f"{base}..HEAD"))
        if rc == 0 and not ahead.strip():
            continue   # nothing on this branch beyond its base — this repo's cut was unused
        rc, out = await repo_ops.git(repo, "git", "push", "-u", "origin", cur, timeout=300)
        if rc != 0:
            raise RuntimeError(f"{repo.name}: push failed: {out[:300]}")
        rc, out = await repo_ops.git(repo, "gh", "pr", "create", "--fill", "--head", cur,
                                     "--base", base.removeprefix("origin/"), timeout=300)
        if rc != 0 and "already exists" not in out:
            raise RuntimeError(f"{repo.name}: gh pr create failed: {out[:300]}")
        urls.append(f"{repo.name}: {await _verified_pr(repo, cur, base)}")
        # "develop as well as release 3.1.6": the same commits, on the other
        # base too, each with its own PR. A task has one worktree, so #178
        # stopped at "needs a worktree" with half the ask undone (30 Sep).
        try:
            for other in await _other_bases(task_id, t, repo, base):
                url = await _port(repo, cur, base, other)
                urls.append(f"{repo.name} → {other}: {url}")
        except RuntimeError as exc:
            raise RuntimeError(f"{repo.name}: required release PR still incomplete ({exc}). "
                               f"Already opened: {'; '.join(urls)}") from exc
    if not urls:
        # The work may be on a branch that already has a PR — he sent the task
        # to it ("add it to PR 1429"), so its commit is not on the task's own
        # branch. #187 committed there and "ship" found nothing to push (1 Oct).
        urls = await _push_named_branches(task_id, t)
    if not urls:
        raise RuntimeError("no unpushed feature branch found — nothing to ship")
    store.record_outcome("ship", "pr_opened", subject=str(task_id), detail="; ".join(urls))
    # The task does NOT end here. Raising the PR is the middle of the job, not
    # the end of it: CI has not run, nobody has reviewed it, and it has not
    # merged. Keeping the task open through all three is what makes those
    # events belong to something instead of arriving as orphaned noise.
    store.update_task(task_id, status="shipped", pr_urls="\n".join(urls),
                      pr_state="OPEN", pr_checked_at=0.0)
    from . import prname
    msg = (f"🔀 Task #{task_id} shipped:\n" + "\n".join("• " + prname.name_links(u) for u in urls)
           + "\n\nStaying on it — I'll tell you if CI goes red, if review asks "
             "for changes, or when it merges.")
    await notify.notify(msg, "action", urgency="direct")
    return msg


# --- after the PR is open -----------------------------------------------------

#: How often to ask GitHub what happened to the open PRs. A PR is a slow object:
#: CI takes minutes and review takes hours, so polling harder buys nothing and
#: spends rate limit.
PR_POLL_SECONDS = int(os.environ.get("ASTA_PR_POLL", "300"))

_PR_FIELDS = ("state,mergedAt,statusCheckRollup,reviewDecision,url,title,reviews,comments,"
              "headRefName,headRefOid,createdAt")

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


def _latest_checks(pr: dict) -> list[dict]:
    """Every check on the PR's head commit, as GitHub lists them.

    NOT collapsed by name. #1252 had two "cicd / Build" checks on one commit —
    one from the push trigger, one from the pull_request trigger. They are
    different runs and both count: the pull_request one passed, the push one
    had really failed, and "CI is green now, the fail you see is stale" was
    wrong (30 Sep). A re-run replaces its own run in this list; nothing else does."""
    return list(pr.get("statusCheckRollup") or [])


_RUN_ID = re.compile(r"/actions/runs/(\d+)")
_FAILED_TEST = re.compile(
    r"\[ERROR\]\s+(?:Tests run:.*?(?:Failures|Errors): [1-9].*?(?:in|-)\s+(\S+)|"
    r"(\w+(?:Test|IT|Tests)\w*[.:#]\w+)\S*\s+(?:--|:)\s*(.{0,120}))")


def _failed_runs(pr: dict) -> list[str]:
    ids: list[str] = []
    for c in _latest_checks(pr):
        if (c.get("conclusion") or c.get("state") or "").upper() in (
                "FAILURE", "TIMED_OUT", "ERROR"):
            m = _RUN_ID.search(c.get("detailsUrl") or c.get("targetUrl") or "")
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


def _repo_of(url: str) -> str:
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/\d+", url or "")
    return m.group(1) if m else ""


async def _why_red(pr: dict, url: str, *, timeout: float = 120) -> str:
    """What actually failed, read from the failed run's own log — so "CI red"
    arrives as "this test, this assertion", not as a link to go and open."""
    repo = _repo_of(url)
    found: list[str] = []
    for run in _failed_runs(pr)[:2]:
        rc, out = await repo_ops.git(ROOT, "gh", "run", "view", run, "--repo", repo,
                                     "--log-failed", timeout=timeout)
        if rc != 0:
            continue
        for line in out.splitlines():
            m = _FAILED_TEST.search(line)
            if not m:
                continue
            what = (m.group(1) or f"{m.group(2)} — {(m.group(3) or '').strip()}").strip(" —")
            if what and what not in found:
                found.append(what[:200])
            if len(found) >= 4:
                break
    return ("\nFailed: " + "; ".join(found)) if found else ""


_CI_REPORTS: dict[tuple[int, bool], asyncio.Task[str]] = {}
_CI_FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED"}
_GITHUB_PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+/?")


async def _ci_report(task_id: int, historical: bool) -> str:
    """Read GitHub's current checks and, if asked, past runs. Never update the task."""
    t = store.get_task(task_id)
    if not t:
        return f"There's no task #{task_id}."
    links = _pr_links(t)
    if not links:
        return f"Task #{task_id} has no linked PR to check."
    lines = []
    for url in links:
        if not _GITHUB_PR.fullmatch(url):
            lines.append(f"Task #{task_id} has an invalid PR link; CI was not checked.")
            continue
        try:
            pr = await _pr_state(url)
        except (OSError, RuntimeError, asyncio.TimeoutError):
            lines.append(f"Could not read {url} from GitHub (authentication or network error); "
                         "CI is unverified.")
            continue
        if not pr:
            lines.append(f"Could not read {url} from GitHub; CI is unverified.")
            continue
        checks = _latest_checks(pr)
        state = (pr.get("state") or "unknown").lower()
        decision = (pr.get("reviewDecision") or "none").upper()
        head = (pr.get("headRefOid") or "")[:8]
        summary = (f"{url} ({state}, head {head or 'unknown'}): "
                   f"CI {_checks_verdict(pr)} ({len(checks)} checks); review {decision}.")
        not_green = [c for c in checks if (c.get("conclusion") or c.get("state") or "").upper()
                     not in ("SUCCESS", "NEUTRAL", "SKIPPED")]
        if not_green:
            summary += " Not passing: " + "; ".join(
                f"{(c.get('name') or 'unnamed')[:70]}: "
                f"{(c.get('conclusion') or c.get('state') or 'pending')[:30]}"
                for c in not_green[:6]) + "."
        lines.append(summary)
        if not historical:
            continue
        branch = pr.get("headRefName") or ""
        if not branch:
            lines.append("Cannot inspect earlier runs: GitHub did not provide the PR branch.")
            continue
        repo = _repo_of(url)
        try:
            rc, out = await repo_ops.git(
                ROOT, "gh", "run", "list", "-R", repo, "--branch", branch,
                "--limit", "100", "--json",
                "databaseId,conclusion,status,event,workflowName,headSha,createdAt,url",
                timeout=45)
        except (OSError, RuntimeError, asyncio.TimeoutError):
            lines.append(f"Could not read historical workflow runs for {url} "
                         "(authentication or network error); earlier failures are unverified.")
            continue
        if rc:
            lines.append(f"Could not read historical workflow runs for {url}; "
                         "the cause of earlier failures is unverified.")
            continue
        try:
            runs = _json.loads(out)
            if not isinstance(runs, list):
                raise ValueError("GitHub did not return a run list")
        except (ValueError, TypeError):
            lines.append(f"Could not parse historical workflow runs for {url}.")
            continue
        created = pr.get("createdAt") or ""
        failed = [r for r in runs if isinstance(r, dict)
                  and (r.get("conclusion") or "").upper() in _CI_FAILED
                  and (r.get("createdAt") or "") >= created]
        if not failed:
            lines.append("No failed runs found among the latest 100 runs on this PR branch "
                         "since the PR opened; an earlier cause is not established.")
            continue
        for run in failed[:5]:
            lines.append(f"Earlier {run.get('conclusion') or 'failed'}: "
                         f"{(run.get('workflowName') or 'workflow')[:70]} "
                         f"({run.get('event') or 'unknown event'}, "
                         f"{(run.get('headSha') or '')[:8] or 'unknown head'}) "
                         f"{run.get('url') or ''}")
        checks_for_logs = [
            {"conclusion": "FAILURE", "detailsUrl": r.get("url")}
            for r in failed[:2] if r.get("url")
        ]
        try:
            detail = await _why_red({"statusCheckRollup": checks_for_logs}, url, timeout=45)
        except (OSError, RuntimeError, asyncio.TimeoutError):
            detail = ""
        lines.append(detail.strip() if detail else
                     "Failed-run logs did not identify a specific test; "
                     "a root cause cannot be confirmed from these results.")
        if len(failed) > 5:
            lines.append(f"{len(failed) - 5} other failed runs in the last 100 omitted.")
    return f"Task #{task_id}:\n" + "\n".join(lines)


async def ci_report(task_id: int, historical: bool = False) -> str:
    """Share one in-flight GitHub read, not an implementation worker or cached status."""
    key = (task_id, historical)
    job = _CI_REPORTS.get(key)
    if job is None or job.done():
        job = asyncio.create_task(_ci_report(task_id, historical))
        _CI_REPORTS[key] = job
        job.add_done_callback(lambda done: _CI_REPORTS.pop(key, None)
                              if _CI_REPORTS.get(key) is done else None)
    return await asyncio.shield(job)


#: The first red CI on his task PR is re-run once by itself. Tests may set it.
CI_AUTO_RERUN = True


def _auto_rerun() -> bool:
    return CI_AUTO_RERUN


#: Fix attempts a task makes on its own red PR before it hands it to him.
CI_AUTOFIX_MAX = 2
CI_FIX_BRIEF = (
    "The CI on this task's PR is red. Work out WHY. Start from the failed checks and "
    "log lines Asta read for you below — do not download CI logs yourself.")


def _auto_fix_ci() -> bool:
    return os.environ.get("ASTA_CI_AUTOFIX", "1") != "0"


async def rerun_ci(task_id: int) -> str:
    """Re-run the failed jobs on a task's PR. His "rerun it", done rather than
    described."""
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    links = _pr_links(t)
    if not links:
        raise ValueError(f"task #{task_id} has no PR yet")
    done: list[str] = []
    for url in links:
        pr = await _pr_state(url)
        for run in _failed_runs(pr):
            rc, out = await repo_ops.git(ROOT, "gh", "run", "rerun", run, "--failed",
                                         "--repo", _repo_of(url), timeout=120)
            done.append(f"run {run}: " + ("re-running the failed jobs" if rc == 0
                                          else f"could not rerun — {out.strip()[:160]}"))
    if not done:
        return f"Task #{task_id}: nothing failed on its latest CI runs — nothing to re-run."
    store.update_task(task_id, pr_state="OPEN")           # so the next result is reported
    for url in links:
        store.kv_del(f"pr_told:{url}")
    store.add_task_event(task_id, "ci", "; ".join(done)[:200])
    return f"🔁 #{task_id}: " + "; ".join(done) + ". I'll tell you how it ends."


async def pr_note(task_id: int, text: str, where: str = "description") -> str:
    """Add to his own PR: a section on its description, or a comment.

    Done by Asta with the recorded words. On 30 Sep "add that in PR description"
    was staged, confirmed twice, approved with "send" — and never landed: the
    chat brain is sandboxed from `gh pr edit`, and nothing else could do it."""
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    links = _pr_links(t)
    if not links:
        raise ValueError(f"task #{task_id} has no PR yet — say “raise PR” first")
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to add")
    url = links[0]
    if where == "comment":
        rc, out = await repo_ops.git(ROOT, "gh", "pr", "comment", url, "--body-file", "-",
                                     stdin=text, timeout=120)
        if rc != 0:
            raise RuntimeError(f"comment failed: {out.strip()[:300]}")
        store.add_task_event(task_id, "pr", "commented: " + text[:160])
        return f"💬 Commented on {url}"
    rc, body = await repo_ops.git(ROOT, "gh", "pr", "view", url, "--json", "body",
                                  "--jq", ".body", timeout=60)
    if rc != 0:
        raise RuntimeError(f"could not read the PR description: {body.strip()[:300]}")
    body = body.rstrip()
    if text in body:
        return f"Already in the description of {url}"
    rc, out = await repo_ops.git(ROOT, "gh", "pr", "edit", url, "--body-file", "-",
                                 stdin=(body + "\n\n" + text).strip() + "\n", timeout=120)
    if rc != 0:
        raise RuntimeError(f"description update failed: {out.strip()[:300]}")
    rc, check = await repo_ops.git(ROOT, "gh", "pr", "view", url, "--json", "body",
                                   "--jq", ".body", timeout=60)
    if rc == 0 and text.splitlines()[0][:60] not in check:
        raise RuntimeError("the description did not change after the update — treat as NOT done")
    store.add_task_event(task_id, "pr", "description: " + text[:160])
    return f"📝 Added to the description of {url}"


def task_for_pr(ref: str) -> int | None:
    """The task whose PR this is — from a url, or "PR #1252 — repo-name"."""
    m = re.search(r"/pull/(\d+)|\bPR\s*#?(\d+)", ref or "", re.I)
    if not m:
        return None
    num = m.group(1) or m.group(2)
    for t in store.list_tasks(limit=80):
        if any(u.rstrip("/").endswith(f"/pull/{num}") for u in _pr_links(t)):
            return t["id"]
    return None


def _checks_verdict(pr: dict) -> str:
    """'red' | 'green' | 'pending' from the check rollup.

    Anything not explicitly a failure or explicitly finished is pending — a run
    still in flight must not be reported as a pass.
    """
    rollup = _latest_checks(pr)
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

    from . import prname
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
            return f"🎉 Merged — {prname.from_url(url)} (#{task_id} {title})\n{url}"
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
        # Still marked red while the checks are green: that IS a transition,
        # whatever the stored state string says. #180 sat at "pr_ci_failed" with
        # every check passing, and he was never told it had recovered (30 Sep).
        recovered = checks == "green" and t["status"] == "pr_ci_failed"
        if checks == "green":
            store.kv_del(f"task_ci_autofix:{task_id}")
        # Two tasks on one PR (#185 and #187 both on booking PR 1429) must not
        # each report the same change: "CI green" arrived twice at 12:49.
        told_key = f"pr_told:{url}"
        if now_state != was and store.kv_get(told_key) == now_state:
            store.update_task(task_id, pr_state=now_state,
                              status="shipped" if checks == "green" else t["status"])
            continue
        if now_state == was and not recovered:
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
        store.kv_set(told_key, now_state)

        if checks == "red":
            store.update_task(task_id, status="pr_ci_failed")
            why = ""
            with contextlib.suppress(Exception):
                why = await _why_red(pr, url)
            # The first red is re-run once, on its own: a flaky test should cost
            # him one message, not five. A second red is his to decide.
            if _auto_rerun() and not store.kv_get(f"task_ci_rerun:{task_id}"):
                store.kv_set(f"task_ci_rerun:{task_id}", str(time.time()))
                with contextlib.suppress(Exception):
                    await rerun_ci(task_id)
                    return (f"🔴 CI red on {prname.from_url(url)} (#{task_id} {title})\n{url}{why}\n"
                            f"Re-running the failed jobs once — I'll tell you how it ends.")
            # Still red after the re-run: the task that wrote the change owns it
            # until merge — it works out WHY and proposes the fix; nothing is
            # changed until he approves the plan. (7 Oct: "analyse, get
            # approval — agents push random things to make a test pass.")
            tries = int(store.kv_get(f"task_ci_autofix:{task_id}") or 0)
            if _auto_fix_ci() and tries < CI_AUTOFIX_MAX:
                store.kv_set(f"task_ci_autofix:{task_id}", str(tries + 1))
                try:
                    await propose_change(task_id, CI_FIX_BRIEF)
                except Exception as exc:                       # noqa: BLE001
                    store.record_outcome("task", "ci_analysis_failed", subject=str(task_id),
                                         detail=str(exc)[:200])
                else:
                    return (f"🔴 CI red on {prname.from_url(url)} (#{task_id} {title})\n{url}{why}\n"
                            f"#{task_id} is working out why. You'll get the cause and a fix "
                            f"plan to approve — nothing changes before you do.")
            return (f"🔴 CI red on {prname.from_url(url)} (#{task_id} {title})\n{url}{why}\n"
                    + (f"Still red after {CI_AUTOFIX_MAX} analysed fixes — it needs you. "
                       if tries >= CI_AUTOFIX_MAX and _auto_fix_ci() else "")
                    + f"Say *rerun ci {task_id}* to run the failed jobs again, or "
                    f"*fix #{task_id}* for a fix plan.")
        if decision == "CHANGES_REQUESTED":
            store.update_task(task_id, status="pr_changes_requested")
            # Carry what they actually said. "Changes requested" on its own is
            # not something he can act on from his phone.
            asks = _new_review_notes(task_id, _review_notes(pr))
            detail = ("\n\n" + "\n".join(f"• {a}" for a in asks[:4])) if asks else ""
            return (f"📝 Changes requested on {prname.from_url(url)} (#{task_id} {title})\n{url}{detail}\n\n"
                    f"Say 'fix #{task_id}' and I'll address these in the same task.")
        if decision == "APPROVED" and checks == "green":
            store.update_task(task_id, status="shipped")
            return f"✅ Approved and green — {prname.from_url(url)} (#{task_id} {title})\n{url}\nReady to merge."
        if checks == "green":
            # The end of the CI run is news too — he raised the PR and then had
            # to ask, three times, whether the pipeline had passed (30 Sep).
            store.update_task(task_id, status="shipped")
            return (f"✅ CI green on {prname.from_url(url)} (#{task_id} {title})\n{url}\n"
                    f"Waiting on review.")
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
            # His own PR's result is the thing he is waiting for, good news
            # included. "Good news can wait" held "✅ CI green on booking PR
            # 1470" while he sat at the laptop asking whether it had passed
            # (7 Oct, 22:59) — and he had asked the same three times on 30 Sep.
            await notify.notify(note, "task", urgency="direct")


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


#: Feedback that changes tests, or fixes a red build, is planned before it is
#: done: an agent told "make CI pass" can weaken the assertion instead of fixing
#: the cause. He sees the plan for a CT/test first (7 Oct).
_PLAN_FIRST = re.compile(
    r"\b(?:ct|component\s+tests?|cucumber|feature\s+files?|scenarios?|test\s*cases?|"
    r"unit\s+tests?|uts?|tests?|ci|build|pipeline|checks?)\b", re.I)
_PLAN_ONLY_BRIEF = (
    "\n\nPLAN ONLY — change NO file in this step. Report:\n"
    "1. CAUSE: the failing line or the gap, and its root cause, with file:line evidence.\n"
    "2. PLAN: each file to change, what changes, and why it fixes the cause (not the "
    "symptom). For tests: which existing scenario/test, the exact steps and assertions, "
    "and what each asserts against in the code. Never weaken, skip or delete an "
    "assertion, and never change the expected value to the observed one, unless the "
    "cause shows the expectation itself was wrong — say so explicitly.\n"
    "3. RISK: what else it touches.\n"
    "Arun approves the plan before anything is changed.")


async def propose_change(task_id: int, feedback: str) -> str:
    """Analyse and plan a change to finished work, in the task's own session,
    without editing anything; the plan waits for his approval."""
    t = store.get_task(task_id)
    if not t or t["kind"] != "code":
        raise ValueError(f"task #{task_id} is not a code task")
    if t["status"] in LIVE_STATUSES:
        raise ValueError(f"task #{task_id} is still running — I'll plan this when it stops")
    ci_read = await _ci_failure_for(t, feedback)
    store.kv_set(f"task_approved:{task_id}", "")          # this leg cannot write
    store.kv_set(f"task_proposal:{task_id}", _json.dumps(
        {"feedback": feedback, "status": t["status"], "at": time.time()}))
    store.update_task(task_id, status="running")
    store.record_outcome("task", "proposal", subject=str(task_id), detail=feedback[:200])
    prompt = (f"FOLLOW-UP ON YOUR OWN TASK — this is not a new task.\n"
              f"Arun's request:\n{feedback}\n\n" + (f"{ci_read}\n\n" if ci_read else "")
              + _PLAN_ONLY_BRIEF)
    job = asyncio.create_task(_propose_worker(task_id, prompt))
    _running[task_id] = job
    job.add_done_callback(lambda _j, tid=task_id: _running.pop(tid, None))
    return (f"Task #{task_id}: working out the cause and a plan — you'll approve it "
            f"before anything changes.")


async def _propose_worker(task_id: int, prompt: str) -> None:
    from . import notify
    t = store.get_task(task_id) or {}
    try:
        async with _ws_lock(t.get("workspace")):
            result = await _run_code_leg(task_id, prompt, task_cwd(task_id, t["workspace"]),
                                         resume=True, effort=_effort_for("code", _resolve_executor(task_id)),
                                         workspace=t.get("workspace"))
    except Exception as exc:                                    # noqa: BLE001
        raw = _json.loads(store.kv_get(f"task_proposal:{task_id}") or "{}")
        store.update_task(task_id, status=raw.get("status") or "done")
        await notify.notify(f"❌ #{task_id} could not plan the change: {str(exc)[:200]}", "task")
        return
    raw = _json.loads(store.kv_get(f"task_proposal:{task_id}") or "{}")
    raw["plan"] = result
    store.kv_set(f"task_proposal:{task_id}", _json.dumps(raw))
    store.kv_set(f"task_gate:{task_id}", "change")
    store.update_task(task_id, status="awaiting_approval")
    await notify.notify(
        f"📋 #{task_id} {t.get('title', '')} — proposed change (nothing changed yet):\n\n"
        f"{_phone_text(readable_outcome(result), 1500)}\n\n"
        f"Reply *approve task {task_id}* to apply it, or tell me what to change.", "task")


def _apply_proposal(task_id: int) -> tuple[str, str]:
    """(the plan he approved, the status to resume from)."""
    raw = _json.loads(store.kv_get(f"task_proposal:{task_id}") or "{}")
    store.kv_del(f"task_proposal:{task_id}")
    store.kv_del(f"task_gate:{task_id}")
    return (f"Arun approved this plan — implement exactly it, nothing else:\n"
            f"{readable_outcome(raw.get('plan') or '')}\n\nHis request was: {raw.get('feedback', '')}",
            raw.get("status") or "done")


async def refine(task_id: int, feedback: str, *, code_change: bool = False,
                 approved_plan: bool = False) -> str:
    """Continue a finished task with feedback, in the session it already has.

    This is the whole point of REFINABLE. The alternative — and what used to
    happen — is a new task with a new session, which starts by re-deriving
    everything the original one already worked out, and answers feedback about
    a change by writing a different change.

    `code_change` is for callers whose text is already a code change by
    construction (walkthrough review notes): "Review notes … add a comment"
    reads as a review or an outward action to the intent rules, not as edits.
    """
    t = store.get_task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id}")
    if t["kind"] != "code":
        raise ValueError(f"task #{task_id} is not a code task (kind={t['kind']})")
    from . import frontdesk
    intent = "edit" if code_change else frontdesk.task_intent(feedback)
    if intent in ("read", "external"):
        raise ValueError(f"task #{task_id} was not resumed: that is "
                         + ("a read-only question" if intent == "read"
                            else "a request for a different action")
                         + ", not an instruction to change its implementation")
    if t["status"] in LIVE_STATUSES:
        # Still running: augment() is the right door, and it needs no restart.
        return augment(task_id, feedback, code_change=code_change)
    if not approved_plan and t["status"] in REFINABLE and _PLAN_FIRST.search(feedback or ""):
        return await propose_change(task_id, feedback)
    if t["status"] not in REFINABLE:
        raise ValueError(f"task #{task_id} cannot be continued (status={t['status']})")

    store.record_outcome("task", "refined", subject=str(task_id))
    # Feedback on a diff he has already seen is approval to keep working on it.
    mark_approved(task_id)
    was_shipped = t["status"] in SHIPPED_STATUSES
    await _record_followup_start(task_id, t)
    ci_read = await _ci_failure_for(t, feedback)
    store.update_task(task_id, status="running")
    prompt = (
        f"FOLLOW-UP ON YOUR OWN TASK — this is not a new task.\n"
        + ("The previous run claimed completion without leaving a code change; "
           "implement the original request before reporting done.\n"
           if t.get("error", "").startswith("The code task reported completion")
           else "You already implemented this; the diff is in the working tree"
           + (" and a PR is open for it.\n" if was_shipped else ".\n"))
        + f"Arun's feedback:\n{feedback}\n\n"
        + (f"{ci_read}\n\n" if ci_read else "")
        + f"Apply it to the EXISTING change. Do not start over, do not re-plan "
        f"from scratch, and do not revert what is already correct."
        + ("\nThe branch is already pushed — commit on top of it so the open PR "
           "picks the change up.\n" if was_shipped else "")
    )
    if _graph().manages(task_id):
        _graph().revisit(task_id, prompt)
    else:
        job = asyncio.create_task(_resume_worker(task_id, prompt, approved=True))
        _running[task_id] = job
        job.add_done_callback(lambda _j, tid=task_id: _running.pop(tid, None))
    where = ("the open PR" if was_shipped else
             "the original implementation" if t.get("error", "").startswith(
                 "The code task reported completion") else "the existing diff")
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
    # Let go of its checkout. A rejected task that kept its worktree kept its
    # BRANCH checked out too, and the task started to replace it opened with
    # "could not create a worktree: already checked out at …task-179" (30 Sep).
    # Clean checkouts only — uncommitted edits are kept — and the branch stays.
    if t.get("kind") == "code":
        with contextlib.suppress(Exception):
            from . import worktrees
            ws_root = Path(code_cwd(t.get("workspace")))
            if worktrees.exists(ws_root, task_id):
                await worktrees.remove(ws_root, task_id)
    return (f"Task #{task_id} rejected and its worker killed."
            if killed else f"Task #{task_id} rejected (it had already finished).")
