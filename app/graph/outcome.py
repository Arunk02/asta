"""A leg says what state it left the work in — as data, not as a sentinel.

The bug this retires: Asta decided whether a run had produced a PLAN or a
RESULT by searching its last lines for "PLAN APPROVED", then "PLAN READY", then
eight more phrases, each added after a brain phrased it some other way. Task
#121 ended "…requires your go-ahead", matched none of them, and was pushed to
Arun as "✅ DONE" with eighteen files it had not written.

So a leg now ends by calling `report_outcome` (an MCP tool every task leg has),
or — for a brain that cannot call tools — with a fenced ```outcome block. Only
when neither is present is the kind INFERRED from the old markers, and the
inference is conservative in the one direction that matters: a planning leg
that says nothing is a plan, never a result.
"""

from __future__ import annotations

import json
import re
import time

from app import store

KINDS = ("plan_ready", "needs_input", "escalate", "done", "blocked", "failed")

_FENCED = re.compile(r"```outcome\s*(\{.*?\})\s*```", re.S | re.I)


def _key(task_id: int) -> str:
    return f"task_outcome:{task_id}"


def record(task_id: int, kind: str, summary: str = "", repos: list[str] | None = None,
           questions: list[str] | None = None, notes: list[str] | None = None) -> str:
    """Store what a leg reported. Returns a line the brain can read back."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)} — got {kind!r}")
    if not store.get_task(int(task_id)):
        raise ValueError(f"no task #{task_id}")
    row = {"kind": kind, "summary": (summary or "")[:600],
           "repos": [r for r in (repos or []) if isinstance(r, str) and r.strip()][:8],
           "questions": [q for q in (questions or []) if isinstance(q, str)][:6],
           "at": time.time()}
    store.kv_set(_key(int(task_id)), json.dumps(row))
    if notes:
        from . import notes as notes_mod
        for n in notes[:6]:
            notes_mod.add(int(task_id), n)
    return f"Recorded: task #{task_id} is {kind}."


def reported(task_id: int, since: float) -> dict | None:
    """What the brain reported during THIS leg (after `since`), if anything."""
    try:
        row = json.loads(store.kv_get(_key(task_id)) or "null")
    except ValueError:
        return None
    if not isinstance(row, dict) or float(row.get("at") or 0) < since:
        return None
    return row


def fenced(text: str) -> dict | None:
    """The ```outcome block a tool-less brain ends with, if it wrote one."""
    for m in reversed(list(_FENCED.finditer(text or ""))):
        try:
            row = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("kind", "")).lower() in KINDS:
            row["kind"] = row["kind"].lower()
            return row
    return None


def read(task_id: int, text: str, since: float, stage: str) -> dict:
    """The outcome of the leg that just ran: reported > fenced > inferred.

    Inference is the fallback for a brain that did neither, and it keeps the
    rule that made the plan gate hold: on the PLAN stage, anything that is not
    a question or an escalation is a plan. Only an approved stage may infer done.
    """
    for source, row in (("reported", reported(task_id, since)), ("fenced", fenced(text))):
        if row:
            return {**row, "source": source}
    tail = (text or "")[-2500:]
    if "CONTEXT CHECK:" in tail:
        return {"kind": "needs_input", "source": "inferred"}
    if "ESCALATE:" in tail:
        reason = tail.split("ESCALATE:", 1)[1].strip().split("\n")[0][:200]
        return {"kind": "escalate", "summary": reason, "source": "inferred"}
    return {"kind": "plan_ready" if stage == "plan" else "done", "source": "inferred"}


def rider(task_id: int, fresh: bool = True) -> str:
    """How a leg is told to end. The full form once per session, a reminder after."""
    if not fresh:
        return (f"\n\n[End this run as before: call report_outcome with task_id={task_id}, "
                "or end with a fenced ```outcome block.]")
    return (
        f"\n\n[How this run ends] When you stop, call the `report_outcome` tool with "
        f"task_id={task_id} and a kind: plan_ready (a plan is waiting for Arun), "
        "needs_input (you need an answer first — put the questions in `questions`), "
        "escalate (bigger than this pipeline allows — say why in `summary`), done "
        "(implemented and checked), blocked (cannot continue — say why) or failed. "
        "On a plan, list every repo the change touches in `repos`. Decisions worth "
        "keeping across a restart go in `notes`. If you have no report_outcome tool, "
        "end your reply with a fenced block instead:\n"
        "```outcome\n{\"kind\": \"plan_ready\", \"repos\": [\"…\"], \"summary\": \"…\"}\n```")
