"""The front desk: what a message needs, decided before any brain is woken.

Most of what he sends from his phone is short — a status check, a yes, a
correction, a new ask. None of that needs an agent re-reading a session of
hundreds of thousands of tokens, and yet "status" with nothing running went to
one, and waited a minute for it.

Three rules live here:

  ANSWER FROM STATE. Status, "what's pending", "status of 117", "any PR
  blocked" are facts in the task table, the event log and the PR rows. They are
  read, not generated: no brain, no tokens, well under five seconds.

  BIND STRICTLY. A message goes to a job only when it names the job, or when a
  RULE recognises it as an addition or a redirect. The brain's guess no longer
  binds anything: its "augment" is how a new request once became part of a
  different task in a different repo.

  RECORD EVERY ROUTE. Each message's path — answered from state, a command, a
  job, new work, a brain turn — is one outcome row, so the scorecard can say
  how much of his day never needed a model.

On unless ASTA_FRONTDESK=0.
"""

from __future__ import annotations

import os
import re
import time

from . import activity, store

#: Routes that never wake a brain.
NO_BRAIN = ("state", "command", "job", "work", "fresh", "meta", "trivial")


def enabled() -> bool:
    return os.environ.get("ASTA_FRONTDESK", "1").strip().lower() not in ("0", "false", "no", "off")


def record(route: str, detail: str = "") -> None:
    store.record_outcome("frontdesk", route, detail=detail[:200])


# --- what can be answered from state ---------------------------------------------------

_LEAD = r"^\s*(?:ok(?:ay)?|k|hey|hi|so|and|pls|please)?[\s,.]*"
_END = r"\s*[?.!]*\s*$"
_TASK_STATUS = re.compile(
    _LEAD + r"(?:(?:what(?:'s| is|s)?|how(?:'s| is)?|where(?:'s| is)?)\s+(?:the\s+)?"
    r"(?:status|state|progress|update)?\s*(?:of|on|with|about)?\s*|status\s+(?:of|on)\s+|"
    r"any\s+update\s+on\s+)(?:task\s*)?#?(\d{1,5})(?:\s+(?:going|doing|at|status|now))?" + _END
    + r"|" + _LEAD + r"(?:task\s*)?#?(\d{1,5})\s+(?:status|update|progress)" + _END, re.I)
_PENDING = re.compile(
    _LEAD + r"(?:what(?:'s| is|s)?\s+(?:pending|waiting(?:\s+(?:on|for)\s+me)?|"
    r"left(?:\s+for\s+me)?|on\s+my\s+plate)|anything\s+(?:pending|waiting(?:\s+(?:on|for)\s+me)?"
    r"|for\s+me)|pending(?:\s+approvals?)?|what\s+do\s+i\s+(?:need|have)\s+to\s+(?:approve|answer|do)"
    r"|what\s+needs\s+me)" + _END, re.I)
_TASKS = re.compile(
    _LEAD + r"(?:what(?:'s| is|s| are)?\s+(?:running|in\s+progress|you\s+working\s+on)|"
    r"(?:list|show)(?:\s+(?:me\s+)?(?:my|the|all))?\s+tasks|my\s+tasks|tasks|running\s+tasks)" + _END,
    re.I)
_PRS = re.compile(
    _LEAD + r"(?:any\s+prs?\s+(?:blocked|stuck|red|failing|pending)|"
    r"(?:what(?:'s| is|s)?\s+the\s+)?pr\s+status|prs?\s+status|status\s+of\s+(?:my\s+)?prs|"
    r"(?:which|what)\s+prs?\s+(?:are\s+)?(?:open|blocked|pending)|open\s+prs)" + _END, re.I)


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    m = (time.time() - float(ts)) / 60
    return f"{m:.0f}m" if m < 90 else f"{m / 60:.1f}h"


def task_card(task_id: int) -> str:
    """One task, as it stands: status, how long, and the last few things that happened."""
    t = store.get_task(task_id)
    if not t:
        return f"There's no task #{task_id}."
    since = t.get("finished_at") or t.get("started_at") or t.get("created_at")
    gate = store.kv_get(f"task_gate:{task_id}") if t["status"] == "awaiting_approval" else ""
    waiting = {"plan": " — waiting for you to approve the plan",
               "context": " — waiting for your answer to its question",
               "verify": " — its check is red; waiting for a hint or approve"}.get(gate or "", "")
    lines = [f"#{task_id} {t['title'][:60]}",
             f"{t['status'].replace('_', ' ')}{waiting} · {_ago(since)}"]
    if t.get("pr_urls"):
        lines.append(f"PR: {t.get('pr_state') or 'open'} · " + t["pr_urls"].splitlines()[0])
    events = [e for e in store.task_events(task_id, 8) if e["kind"] != "created"][-4:]
    for e in events:
        lines.append(f"  {time.strftime('%H:%M', time.localtime(e['created_at']))} "
                     f"{e['kind']}: {e['detail'][:70]}")
    return "\n".join(lines)


def pending_for_him() -> str:
    """Everything waiting on him: gates, open questions."""
    from . import asking
    rows = [t for t in store.list_tasks(limit=100) if t["status"] == "awaiting_approval"]
    out = []
    for t in rows[:8]:
        gate = store.kv_get(f"task_gate:{t['id']}") or ("draft" if t["kind"] == "teams_draft" else "plan")
        out.append(f"• #{t['id']} {t['title'][:50]} — {gate}")
    for q in asking.open_questions()[:5]:
        out.append(f"• question {q['id']}: {q['text'][:60]}")
    if not out:
        return "Nothing is waiting on you."
    return "Waiting on you:\n" + "\n".join(out)


def running_now() -> str:
    live = [t for t in store.list_tasks(limit=100)
            if t["status"] in ("running", "awaiting_approval", "paused")]
    if not live:
        return "No tasks running or waiting."
    return "\n".join(f"• #{t['id']} {t['title'][:50]} — {t['status'].replace('_', ' ')}"
                     for t in live[:10])


def prs_now() -> str:
    from . import agent
    return agent.task_pr_status(0)


def answer_from_state(text: str) -> str:
    """The reply, when the message is a question the task table answers. '' otherwise."""
    t = (text or "").strip()
    if not t or len(t) > 120:
        return ""
    m = _TASK_STATUS.match(t)
    if m:
        return task_card(int(m.group(1) or m.group(2)))
    if _PENDING.match(t):
        return pending_for_him()
    if _PRS.match(t):
        return prs_now()
    if _TASKS.match(t):
        return running_now()
    if activity.is_status_ask(t):
        # "status" with nothing named: what is running, and what waits on him.
        body = activity.summary()
        waiting = pending_for_him()
        if not waiting.startswith("Nothing"):
            body += "\n\n" + waiting
        return body
    return ""


# --- binding a message to a job ----------------------------------------------------------

def interjection(text: str, named: bool) -> str:
    """How a message relates to a live job — decided by RULES, never by a brain.

    Unnamed and unclear stays 'ambiguous': the message is answered on its own,
    never folded in on a guess. Named and unclear goes to the job he named.
    """
    verdict = activity.classify_interjection(text)
    if verdict == "ambiguous" and named:
        return "augment"
    return verdict
