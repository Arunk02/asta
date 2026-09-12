"""Which model, at what effort, for each leg of a task — chosen by the work.

Until this, a task went to Copilot, or to Claude when Copilot was out, at the
stage's fixed effort. The brain was chosen by availability and the effort by
the stage, never by what the work needed: a one-line fix and a three-repo
schema change got the same model at the same effort.

    T1 · fast      one repo, no ticket, ≤ 2 files, no schema or config words
    T2 · standard  a ticket, 3–10 files, logs to read            (plans run here)
    T3 · max       ≥ 2 repos, a schema/contract/migration change, "prod is down",
                   or the same failure twice

Two-stage. Before the plan, only cheap signals exist, so every plan runs at T2.
The plan then says how big the change really is — which repos, which files, a
schema change — and the implementation runs at THAT tier. A plan that names two
repos implements at T3 without anyone asking.

Brains stay sticky once a session exists (a session cannot move between them),
so a tier changes the model and effort of the brain the task is on. He can
override any of it in the message: "use claude", "cheap", "max".

Off unless ASTA_ROUTING=1. Every decision is recorded (the task's event log and
an outcome row), so the scorecard can say what tier the work ran at.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import store

T1, T2, T3 = 1, 2, 3
NAMES = {T1: "T1", T2: "T2", T3: "T3"}


def enabled() -> bool:
    return os.environ.get("ASTA_ROUTING", "").strip().lower() in ("1", "true", "yes", "on")


# --- what the words say ------------------------------------------------------------

_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,15}-\d{1,6}\b")
#: A change to a contract other code depends on: always worth the best model.
_CONTRACT = re.compile(
    r"\b(schema|migration|migrate|avro|dto|api contract|contract|protobuf|proto|"
    r"liquibase|flyway|ddl|alter table|breaking change|kafka topic|topic schema)\b", re.I)
_PROD_DOWN = re.compile(r"\b(prod(uction)? (is )?(down|broken|on fire)|outage|sev ?[12]|p1 incident)\b",
                        re.I)
_SMALL = re.compile(r"\b(typo|rename|one[- ]line|single line|log line|comment|bump|"
                    r"version bump|tiny|small fix|quick fix|wording|constant)\b", re.I)
_FILE = re.compile(r"\b[\w./-]+\.(java|kt|py|ts|tsx|js|go|rb|cs|sql|yaml|yml|json|xml|md|gradle|properties)\b")

#: "use claude", "on copilot", "with codex" — which brain he wants the job on.
_BRAIN_OVERRIDE = re.compile(r"\b(?:use|on|with|via)\s+(claude|copilot|codex)\b", re.I)
_CHEAP = re.compile(r"\b(cheap(ly)?|quick and cheap|low effort|don'?t overthink)\b", re.I)
#: Deliberately narrow: "raise max retries to 5" is not a request for T3.
_MAX = re.compile(r"\b(max(imum)? effort|go max|max tier|use (the )?best model|"
                  r"use opus|think hard)\b|[,;—-]\s*max\s*[.!]*$", re.I)


@dataclass(frozen=True)
class Choice:
    tier: int
    model: str          # '' = the brain's own default
    effort: str
    why: str


def overrides(text: str) -> dict:
    """What he asked for in so many words: a brain, and/or a tier."""
    t = text or ""
    out: dict = {}
    m = _BRAIN_OVERRIDE.search(t)
    if m:
        out["brain"] = m.group(1).lower()
    if _MAX.search(t):
        out["tier"] = T3
    elif _CHEAP.search(t):
        out["tier"] = T1
    return out


def pre_tier(title: str, prompt: str) -> tuple[int, str]:
    """The tier cheap signals suggest before any brain has looked."""
    text = f"{title}\n{prompt}"
    if _PROD_DOWN.search(text):
        return T3, "prod is down"
    if _CONTRACT.search(text):
        return T3, f"touches a contract ({_CONTRACT.search(text).group(0)})"
    files = set(_FILE.findall(text))
    if _TICKET.search(text) or len(files) >= 3:
        return T2, "a ticket" if _TICKET.search(text) else f"{len(files)} files named"
    if _SMALL.search(text) or len(text) < 160:
        return T1, "a small, single-place change"
    return T2, "standard"


def _structure_entries(plan: str) -> int:
    """How many classes/files the plan's STRUCTURE block lists (its sub-lines —
    `└─ Service.method()` — belong to the entry above them)."""
    from .tasks import _STRUCTURE_HEAD, _block_span
    lines = (plan or "").splitlines()
    start, end = _block_span(lines, _STRUCTURE_HEAD)
    if start < 0:
        return 0
    return sum(1 for ln in lines[start + 1:end]
               if ln.strip() and not ln.strip().startswith(("└", "├", "│")))


def plan_tier(plan: str, outcome: dict | None = None) -> tuple[int, str]:
    """The tier the approved plan calls for — the true size of the change."""
    outcome = outcome or {}
    repos = [r for r in outcome.get("repos") or [] if r]
    if len(repos) >= 2:
        return T3, f"{len(repos)} repos"
    text = plan or ""
    if _CONTRACT.search(text):
        return T3, f"changes a contract ({_CONTRACT.search(text).group(0)})"
    if re.search(r"\brisk\s*[:=]\s*high\b", text, re.I):
        return T3, "the plan rates its risk high"
    n = _structure_entries(text) or len(set(_FILE.findall(text)))
    if n >= 3:
        return T2, f"{n} classes/files"
    if n:
        return T1, f"{n} class(es)/file(s), one repo"
    return T2, "size not stated"


# --- what each tier means on each brain ------------------------------------------------

#: (model, effort) per brain per tier and stage. Claude CLI takes model aliases;
#: Copilot's model list differs by account, so it keeps its own and moves on
#: effort. Override any cell with ASTA_ROUTE_<BRAIN>_<T#>="model:effort".
_TABLE = {
    "claude": {T1: ("sonnet", "low"), T2: ("sonnet", "high"), T3: ("opus", "xhigh")},
    "copilot": {T1: ("", "low"), T2: ("", "high"), T3: ("", "xhigh")},
}
#: Every plan runs at T2, at planning effort — a plan is read, not built.
_PLAN = {"claude": ("sonnet", "medium"), "copilot": ("", "medium")}


def _cell(brain: str, tier: int) -> tuple[str, str]:
    raw = os.environ.get(f"ASTA_ROUTE_{brain.upper()}_{NAMES[tier]}", "").strip()
    if raw:
        model, _, effort = raw.partition(":")
        return model.strip(), effort.strip()
    return _TABLE.get(brain, {}).get(tier, ("", ""))


def _key(task_id: int) -> str:
    return f"task_tier:{task_id}"


def tier_of(task_id: int) -> int:
    try:
        return int(json.loads(store.kv_get(_key(task_id)) or "{}").get("tier") or 0)
    except (ValueError, AttributeError):
        return 0


def set_tier(task_id: int, tier: int, why: str, source: str) -> None:
    tier = max(T1, min(T3, tier))
    store.kv_set(_key(task_id), json.dumps({"tier": tier, "why": why, "source": source}))
    store.add_task_event(task_id, "tier", f"{NAMES[tier]} — {why} ({source})")


def on_spawn(task_id: int, title: str, prompt: str) -> str:
    """Record the starting tier, and honour a brain he named. Returns the brain
    he asked for, or ''."""
    ov = overrides(f"{title}\n{prompt}")
    if "tier" in ov:
        set_tier(task_id, ov["tier"], "you asked for it", "override")
        store.kv_set(f"task_tier_pinned:{task_id}", "1")
    else:
        tier, why = pre_tier(title, prompt)
        set_tier(task_id, tier, why, "signals")
    return ov.get("brain", "")


def on_plan(task_id: int, plan: str, outcome: dict | None = None) -> None:
    """Re-tier from the approved plan — unless he pinned a tier himself."""
    if store.kv_get(f"task_tier_pinned:{task_id}"):
        return
    tier, why = plan_tier(plan, outcome)
    set_tier(task_id, tier, why, "plan")


def escalate(task_id: int, why: str) -> int:
    """The same failure twice: one tier up, once."""
    tier = min(T3, (tier_of(task_id) or T2) + 1)
    set_tier(task_id, tier, why, "escalation")
    return tier


def choose(task_id: int, brain: str, stage: str) -> Choice:
    """The model and effort for this leg, on the brain the task is on."""
    brain = "claude" if brain.startswith("claude") else brain
    if stage == "plan":
        model, effort = _PLAN.get(brain, ("", "medium"))
        pinned = store.kv_get(f"task_tier_pinned:{task_id}") and tier_of(task_id) == T3
        if pinned:
            model, effort = _cell(brain, T3)
            return Choice(T3, model, effort, "planning at T3 — you asked for max")
        return Choice(T2, model, effort, "every plan runs at T2")
    tier = tier_of(task_id) or T2
    model, effort = _cell(brain, tier)
    return Choice(tier, model, effort, f"{NAMES[tier]} on {brain}")


def record(task_id: int, stage: str, brain: str, c: Choice) -> None:
    detail = f"{stage}: {brain} {c.model or 'default'} @ {c.effort or 'default'} — {c.why}"
    store.add_task_event(task_id, "route", detail)
    store.record_outcome("route", NAMES[c.tier], subject=str(task_id), detail=detail)
