"""He said go — once is enough.

30 Sep, task #178 (MX as a one-click country). He typed, in order: "enable MX
… in both develop as well as latest release branch 3.1.6", "no do the change
and create PR", "approve 178", "push and raise PR", "approval". Five messages
for one instruction, and at the end nothing was pushed: two of them were read
as feedback on a colleague's draft that happened to be waiting, the chat brain
twice answered that it had "no tool that can approve", and the task held the
push because his relayed words "arrived as an injected block". His words:
"I already said do it, then what is this — I keep on asking approval."

So his go-ahead is handled here, from the task table, by no brain at all:

  * a message that is entirely "do it / approval / push / raise the PR" acts on
    the task it can only mean — approves the gate it is waiting at, and pushes
    and opens the PR the moment it is done (or now, if it already is);
  * an ask that itself says "…and raise the PR" carries that go-ahead from the
    start: the plan still comes to his phone, as something to read rather than
    something to answer.

The plan gate stays for every task he has NOT said go on.
"""
from __future__ import annotations

import json
import os
import re
import time

from . import store

#: "raise the PR", "create PR", "push", "ship it" — the end state he grants.
_PR = re.compile(
    r"\b(?:raise|create|open|make|put\s+up|send)\s+(?:a\s+|an\s+|the\s+|both\s+)?"
    r"(?:pr|prs|pull\s+requests?)\b|\bpush(?:ed)?\b(?!\s*back)|\bship\b|\bpr\s+raise\b|"
    r"\bupdate\s+(?:it\s+)?(?:in\s+|to\s+)?(?:the\s+)?(?:same\s+)?(?:pr|pull\s+request)\b", re.I)

#: "…and inform Vinish" — the part of a go-ahead that is a message, not a command.
_AND_TELL = re.compile(
    r"\s*(?:,|and|then|&)\s*(?:then\s+)?((?:inform|tell|ping|message|msg|notify|let)\b.*)$",
    re.I | re.S)


def split_tell(text: str) -> tuple[str, str]:
    """("commit and update in same PR", "inform Vinish") — or (text, "")."""
    m = _AND_TELL.search(text or "")
    if not m:
        return text, ""
    head = (text or "")[:m.start()].strip()
    return (head, m.group(1).strip()) if head else (text, "")
#: "don't push", "no PR yet", "before raising the PR" — the opposite.
_NOT = re.compile(r"\b(?:don'?t|do\s+not|not|never|without|before|hold|no\s+need\s+to|"
                  r"stop\s+after)\b[^.\n!?]{0,28}$", re.I)

_PHRASE = (r"(?:approval|approved?|go\s+ahead|do\s+(?:it|that|the\s+changes?)|just\s+do\s+it|"
           r"update\s+(?:it\s+)?in\s+helm|commit(?:\s+it)?|"
           r"update\s+(?:it\s+)?(?:in\s+|to\s+)?(?:the\s+)?(?:same\s+)?(?:pr|pull\s+request)(?:\s+#?\d+)?|"
           r"push\s+(?:it\s+)?(?:to|in)\s+(?:the\s+)?(?:same\s+)?(?:pr|branch)(?:\s+#?\d+)?|"
           r"push(?:\s+(?:it|this|that|the\s+(?:changes?|branch|code)))?|"
           r"(?:raise|create|open)\s+(?:a\s+|the\s+)?(?:pr|prs|pull\s+requests?)|"
           r"ship(?:\s+(?:it|this|that))?)")
_FILL = r"(?:now\s+itself|now|fast|please|pls|asap|quickly|directly|right\s+away)"
#: A message that is ENTIRELY a go-ahead. Anchored at both ends, like the
#: approve command: "push the ETA fix after CI is green" is an ordinary message.
_COMMAND = re.compile(
    r"^\s*(?:(?:no|ok(?:ay)?|yes|pls|please|just|then|so)[\s,.:—-]+)*"
    rf"{_PHRASE}(?:(?:\s*(?:and|then|,|&|\+)\s*(?:then\s+)?|\s+){_PHRASE})*"
    r"(?:\s+(?:for|on|of)?\s*(?:the\s+)?(?:task\s*)?#?\s*(\d{1,5}))?"
    rf"(?:\s+{_FILL})*\s*[.!]*\s*$", re.I)

RECENT_SECONDS = float(os.environ.get("ASTA_GO_RECENT_HOURS", "24")) * 3600


def enabled() -> bool:
    return os.environ.get("ASTA_GO_MEANS_GO", "1").strip().lower() not in ("0", "false", "off", "no")


def wants_pr(text: str) -> bool:
    """Did he ask for the push / PR — and not ask for it to be held?"""
    for m in _PR.finditer(text or ""):
        if not _NOT.search((text or "")[:m.start()]):
            return True
    return False


def command(text: str) -> tuple[bool, int | None]:
    """(is this whole message a go-ahead?, the task number he named)."""
    m = _COMMAND.match(text or "")
    if not m:
        return False, None
    low = (text or "").lower()
    # "go ahead" / "do it" on their own answer whatever he was last asked — a
    # draft, an offer. Only with the PR words, or the word approval itself, do
    # they belong to a code task.
    if not (wants_pr(text) or "approv" in low):
        return False, None
    return True, (int(m.group(1)) if m.group(1) else None)


_RERUN = re.compile(
    r"^\s*(?:(?:yes|ok(?:ay)?|pls|please)[\s,]+)*(?:re-?run|reran|retry|run\s+again)\s*(?:the\s+)?"
    r"(?:ci|pipeline|build|checks?|failed(?:\s+(?:one|ones|jobs?|run))?|it)?"
    r"(?:\s+(?:for|on|of)?\s*(?:the\s+)?(?:task\s*)?#?\s*(\d{1,5}))?\s*[.!]*\s*$", re.I)


async def rerun(text: str) -> str:
    """"rerun it", "rerun ci 180" — re-run the failed jobs on the PR it can only
    mean. "" when there is no such PR."""
    from . import tasks
    m = _RERUN.match(text or "")
    if not m:
        return ""
    if m.group(1):
        tid = int(m.group(1))
    else:
        red = [t["id"] for t in store.list_tasks(limit=80) if t["status"] == "pr_ci_failed"]
        if len(red) != 1:
            return ""
        tid = red[0]
    try:
        return await tasks.rerun_ci(tid)
    except (ValueError, RuntimeError) as exc:
        return f"#{tid}: {exc}"


def grant(task_id: int, words: str = "", *, ship: bool = True) -> None:
    prior = granted(task_id) or {}
    store.kv_set(f"task_go:{task_id}", json.dumps(
        {"ship": bool(ship or prior.get("ship")), "words": (words or "")[:300],
         "at": time.time()}))
    store.add_task_event(task_id, "go", f"he said go{' + PR' if ship else ''}: {(words or '')[:160]}")


def granted(task_id: int) -> dict | None:
    try:
        d = json.loads(store.kv_get(f"task_go:{task_id}") or "null")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def ships(task_id: int) -> bool:
    return bool((granted(task_id) or {}).get("ship"))


def words(task_id: int) -> str:
    return (granted(task_id) or {}).get("words", "")


#: The pause between showing a plan he is not asked about and starting it: long
#: enough for the gate to be in place, short enough not to be a wait.
def after_plan_seconds() -> float:
    try:
        return float(os.environ.get("ASTA_GO_AFTER_PLAN_SECONDS", "3"))
    except ValueError:
        return 3.0


def ask_from_tier() -> int:
    """The smallest change whose plan waits for his approval (1 = every plan,
    2 = anything past one class or file, 4 = never)."""
    try:
        return int(os.environ.get("ASTA_PLAN_APPROVAL_FROM_TIER", "2"))
    except ValueError:
        return 2


def no_ask(task_id: int, plan: str) -> str:
    """Why this plan goes ahead without waiting for him — "" when it must wait.

    His rule (30 Sep): "for a big coding task, plan and get approval is fine,
    but getting too much approval is drag." So a plan waits for him only when
    the change is big, and never when he has already said go on this task."""
    if not enabled():
        return ""
    if granted(task_id):
        return "as you said"
    from . import routing
    try:
        # What the brain REPORTED counts too: a plan that lists two classes and
        # reports two repos is a two-repo change, and that one waits.
        from .graph import outcome as _outcome
        tier, why = routing.plan_tier(plan, _outcome.reported(task_id, 0))
    except Exception:                                       # noqa: BLE001
        return ""                       # cannot size it: it waits, as before
    if tier < ask_from_tier():
        return f"small change — {why}"
    return ""


def on_spawn(task_id: int, said: str) -> bool:
    """His own ask said "…and raise the PR": the go-ahead rides with the task."""
    if not enabled() or not wants_pr(said):
        return False
    grant(task_id, said, ship=True)
    return True


def _candidates(now: float) -> list[dict]:
    return [t for t in store.list_tasks(limit=60)
            if t.get("kind") == "code" and now - float(t.get("created_at") or 0) < RECENT_SECONDS]


def target(wanted: int | None, now: float | None = None) -> tuple[dict | None, str]:
    """The task a go-ahead means: the one named, else the only one it can be —
    waiting on him first, then running, then finished and not yet pushed."""
    now = time.time() if now is None else now
    if wanted is not None:
        t = store.get_task(wanted)
        return (t, "") if t else (None, f"There's no task #{wanted}.")
    rows = _candidates(now)
    for statuses in (("awaiting_approval",), ("running", "queued", "paused"), ("done",)):
        here = [t for t in rows if t["status"] in statuses and not t.get("pr_urls")]
        if len(here) == 1 or (here and statuses == ("done",)):
            return max(here, key=lambda t: t["id"]), ""
        if len(here) > 1:
            listing = "\n".join(f"  #{t['id']} {t['title'][:45]}" for t in here)
            return None, f"Which one do you mean?\n{listing}\n\nSay “raise PR {here[0]['id']}”."
    return None, ""


async def act(text: str, wanted: int | None) -> str:
    """Carry out his go-ahead. "" when there is nothing it can mean."""
    from . import tasks
    t, problem = target(wanted)
    if problem:
        return problem
    if not t or t.get("kind") != "code":
        return ""
    tid, pr = t["id"], wants_pr(text)
    title = (t.get("title") or "")[:50]
    store.record_outcome("command", "go", subject=str(tid), detail=(text or "")[:120])
    status = t["status"]
    if status == "awaiting_approval":
        grant(tid, text, ship=pr)
        try:
            await tasks.approve(tid)
        except ValueError as exc:
            return f"Task #{tid}: {exc}"
        return (f"👍 #{tid} {title} — going ahead now"
                + (", and I'll push it and raise the PR as soon as it's done. "
                   "No more asks on this one." if ships(tid) else "."))
    if status in ("running", "queued", "paused"):
        grant(tid, text, ship=pr)
        return (f"👍 #{tid} {title} — it goes straight through"
                + (", and I raise the PR the moment it's done. No more asks on this one."
                   if ships(tid) else "; I won't stop it to ask."))
    if status == "done":
        if not pr:
            return (f"#{tid} {title} is finished and committed locally — "
                    f"say *raise PR* and I'll push it.")
        try:
            return await tasks.ship(tid)
        except (ValueError, RuntimeError) as exc:
            return f"❌ #{tid}: couldn't raise the PR — {exc}"
    if t.get("pr_urls"):
        return f"#{tid} is already up:\n{t['pr_urls']}"
    return ""
