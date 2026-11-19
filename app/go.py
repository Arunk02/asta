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
    # "send him the PR", "send <name> the PR" (11 Nov: read as no PR at all)
    r"\bsend\s+(?:him|her|them|me|us|[a-z]{3,15})\s+(?:the\s+|a\s+)?(?:pr|prs|pull\s+requests?)\b|"
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
           r"ship(?:\s+(?:it|this|that))?|merge(?:\s+(?:it|this|that))?)")
_FILL = r"(?:now\s+itself|now|fast|please|pls|asap|quickly|directly|right\s+away)"
#: A message that is ENTIRELY a go-ahead. Anchored at both ends, like the
#: approve command: "push the ETA fix after CI is green" is an ordinary message.
_COMMAND = re.compile(
    r"^\s*(?:(?:no|ok(?:ay)?|yes|pls|please|just|then|so)[\s,.:—-]+)*"
    rf"{_PHRASE}(?:(?:\s*(?:and|then|,|&|\+)\s*(?:then\s+)?|\s+){_PHRASE})*"
    r"(?:\s+(?:for|on|of)?\s*(?:the\s+)?(?:task\s*)?#?\s*(\d{1,5}))?"
    rf"(?:\s+{_FILL})*\s*[.!]*\s*$", re.I)

RECENT_SECONDS = 24 * 3600
_STOP_MERGE = re.compile(r"^\s*(?:don'?t|do\s+not|stop|hold)\s+(?:the\s+)?merg(?:e|ing)"
                         r"(?:\s+(?:for\s+)?(?:task\s*)?#?\s*(\d{1,5}))?\s*[.!]*$", re.I)


def enabled() -> bool:
    """Always: his go-ahead is a command. (A switch for it was one more flag
    with only one right answer — removed 1 Oct.)"""
    return True


#: The sentence asks something, or says he will do it himself — not a go.
_ASKING = re.compile(r"^\s*(?:should|shall|can|could|do|did|will|would|may|must|is|are|why|when|how)\b", re.I)
_HIMSELF = re.compile(r"\b(?:i'?ll|i\s+will|i'?m\s+going\s+to|i\s+can|let\s+me)\b[^.\n!?]{0,20}$|\bmyself\b",
                      re.I)


def _sentence_at(text: str, pos: int) -> tuple[str, str]:
    """(the sentence up to pos, the whole sentence) around a match."""
    start = max(text.rfind(c, 0, pos) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, pos) for c in ".!?\n") if i >= 0]
    end = min(ends) + 1 if ends else len(text)
    return text[start:pos], text[start:end]


def wants_pr(text: str) -> bool:
    """Did he ask for the push / PR — and not ask for it to be held?

    16 Nov: "should I raise the PR?" and "fix it, I'll raise the PR myself" read
    as a request to raise it — and a request to raise it is a go-ahead to build."""
    text = text or ""
    for m in _PR.finditer(text):
        before, sentence = _sentence_at(text, m.start())
        if _NOT.search(text[:m.start()]):
            continue
        if sentence.rstrip().endswith("?") or _ASKING.match(sentence):
            continue
        if _HIMSELF.search(before) or re.search(r"\bmyself\b", sentence, re.I):
            continue
        return True
    return False


#: His own words that skip the plan gate without a PR: "just do it", "no need to
#: show me the plan", "go ahead and fix it", "don't wait for my approval".
_GO_AHEAD = re.compile(
    r"\bjust\s+do\s+it\b"
    r"|\bno\s+need\s+(?:to\s+)?(?:show|ask|wait|check\s+with|approv\w*|for\s+(?:my\s+)?(?:approval|plan))\b"
    r"|\b(?:don'?t|do\s+not)\s+wait\s+for\s+(?:my\s+)?(?:approval|yes|go|ok)\b"
    # "go ahead and create PR, don't need my approval" (19 Nov)
    r"|\b(?:don'?t|do\s+not|doesn'?t)\s+need\s+(?:my\s+|any\s+)?(?:approval|yes|go-?ahead|ok)\b"
    r"|\bwithout\s+(?:waiting\s+for\s+)?(?:my\s+)?approval\b"
    r"|\bno\s+approval\s+(?:needed|required)\b"
    r"|\bskip\s+(?:the\s+)?(?:plan\s+)?approval\b"
    r"|\bgo\s+ahead\s+and\s+(?:fix|implement|do|change|make|add|update|build)\b", re.I)


def go_ahead(text: str) -> bool:
    """His words say to build without showing him the plan first."""
    text = text or ""
    for m in _GO_AHEAD.finditer(text):
        _, sentence = _sentence_at(text, m.start())
        if sentence.rstrip().endswith("?") or _ASKING.match(sentence):
            continue
        return True
    return False


#: "567 approve raise PR", "task 567: approve and raise PR" — the task named first.
_LEAD = re.compile(r"^\s*(?:task\s*)?#?(\d{1,5})\b[\s,:—-]*", re.I)
#: A short go-ahead said as a sentence: "once u done plan go ahead and implement
#: and raise PR" (11 Nov: not a go-ahead, so the task stopped at "ready").
_GO_WORD = re.compile(r"\b(?:go\s+ahead|approve[ds]?|approval|proceed|implement|do\s+it|"
                      r"go\s+for\s+it)\b", re.I)
_GO_START = re.compile(r"^\s*(?:once|after|when|ok(?:ay)?|yes|then|pls|please|go|approve[ds]?|"
                       r"implement|proceed|so|and|just|fine|done|task|#?\d{1,5})\b", re.I)
_HOLD = re.compile(r"\b(?:don'?t|do\s+not|not\s+yet|hold|wait|stop|without|never)\b", re.I)
_TASK_REF = re.compile(r"\btask\s*#?(\d{1,5})\b|^\s*#?(\d{1,5})\b", re.I)


def _go_sentence(text: str) -> tuple[bool, int | None]:
    """A short sentence that is only his go-ahead to approve, build and raise the PR."""
    t = (text or "").strip()
    if not t or len(t) > 160 or "?" in t or _HOLD.search(t):
        return False, None
    if not (_GO_START.match(t) and _GO_WORD.search(t) and wants_pr(t)):
        return False, None
    ref = _TASK_REF.search(t)
    return True, (int(ref.group(1) or ref.group(2)) if ref else None)


#: "approve task 567, don't push or raise PR", "567 approve, no PR" — an approval
#: that keeps the change local. 12 Nov: read as plan feedback, it re-planned and
#: asked for the same yes again.
_APPROVE_LOCAL = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|yes|pls|please|then|so)[\s,]+)*(?:(?:task\s*)?#?(\d{1,5})[\s,:—-]+)?"
    r"(?:approve[ds]?|approval|go\s+ahead|implement|proceed)(?:\s+(?:the\s+)?plan)?"
    r"(?:\s+(?:for\s+|on\s+)?(?:task\s*)?#?(\d{1,5}))?[\s,.;—-]*(?:but\s+|and\s+|just\s+)?"
    r"(?:don'?t|do\s+not|no|without|never|not)\s+(?:to\s+)?"
    r"(?:push|raise|open|create|pr\b|prs\b|a\s+pr\b|the\s+pr\b)"
    r"[\w\s,/'-]{0,40}?[.!]*\s*$", re.I)
_NO_PR = re.compile(r"\b(?:don'?t|do\s+not|no|without|never|not)\s+(?:to\s+)?"
                    r"(?:push|raise|open|create|(?:a\s+|the\s+)?prs?\b)", re.I)


def approves_locally(text: str) -> tuple[bool, int | None]:
    """An approval with the push/PR held back — (is it?, the task he named)."""
    m = _APPROVE_LOCAL.match(text or "")
    if not m or wants_merge(text or ""):
        return False, None
    n = m.group(1) or m.group(2)
    return True, (int(n) if n else None)


def command(text: str) -> tuple[bool, int | None]:
    """(is this whole message a go-ahead?, the task number he named)."""
    local, number = approves_locally(text)
    if local:
        return True, number
    stop = _STOP_MERGE.match(text or '')
    if stop:
        return True, int(stop.group(1)) if stop.group(1) else None
    from . import task_inform
    quiet, number = task_inform.stop_command(text)
    if quiet:
        return True, number
    lead = _LEAD.match(text or "")
    if lead and not wants_merge(text):
        # The number first, then the go-ahead: the same command, said the other way.
        rest = (text or "")[lead.end():]
        m = _COMMAND.match(rest)
        if m and (wants_pr(rest) or "approv" in rest.lower()) and \
                (not m.group(1) or m.group(1) == lead.group(1)):
            return True, int(lead.group(1))
    m = _COMMAND.match(text or "")
    if not m:
        return _go_sentence(text)
    if wants_merge(text) and m.group(1) and not re.search(r'\btask\s*#?\s*' + m.group(1) + r'\b', text, re.I):
        return False, None  # a bare merge number may identify a PR, not a task
    low = (text or "").lower()
    # "go ahead" / "do it" on their own answer whatever he was last asked — a
    # draft, an offer. Only with the PR words, or the word approval itself, do
    # they belong to a code task.
    if not (wants_pr(text) or wants_merge(text) or "approv" in low):
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
    merge = bool(prior.get('merge'))
    if re.search(r"\b(?:don'?t|do\s+not|never|stop|hold)\s+(?:the\s+)?merg", words, re.I):
        merge = False
    elif wants_merge(words):
        merge = True
    # "…don't push / no PR" said now outranks a ship granted earlier.
    keep_local = bool(_NO_PR.search(words or "")) and not ship
    local = keep_local or (bool(prior.get("local")) and not ship)
    store.kv_set(f"task_go:{task_id}", json.dumps(
        {"ship": False if local else bool(ship or prior.get("ship")), "local": local,
         "words": (words or "")[:300],
         "merge": merge, "merge_words": words if wants_merge(words) else prior.get('merge_words', ''),
         "at": time.time()}))
    store.add_task_event(task_id, "go", f"he said go{' + PR' if ship else ''}: {(words or '')[:160]}")


def his_plan_yes(task_id: int, words: str) -> None:
    """His own yes to this task's plan — the one approval it needs (his rule,
    19 Nov): the PR follows without another ask, unless he kept it local.
    Called only where HIS words arrive (main), never from Asta's own approvals."""
    grant(task_id, words, ship=not approves_locally(words)[0])


def granted(task_id: int) -> dict | None:
    try:
        d = json.loads(store.kv_get(f"task_go:{task_id}") or "null")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def ships(task_id: int) -> bool:
    """Does the finished work go up as a PR without another ask?

    His rule, 19 Nov: one approval per code task. Said up front ("…and raise
    the PR", "just do it"), or given at the plan gate — either way the PR follows
    without a second yes (`tasks.record_answer` grants it where his yes lands).
    Only his "don't push / no PR" keeps it local."""
    g = granted(task_id) or {}
    return bool(g.get("ship")) and not g.get("local")


def wants_merge(text: str) -> bool:
    """A positive merge instruction, not a mention of resolving merge conflicts."""
    if re.search(r"\b(?:how\s+to|whether\s+to|should\s+(?:i|we))\s+merge\b", text, re.I):
        return False
    if re.search(r"\b(?:explain|describe|simulate|dry.run)\b[^.;\n]{0,80}\bmerge\b", text, re.I):
        return False
    for m in re.finditer(r"\bmerge\b(?!\s+(?:conflicts?|strategy|base|approval|branch\s+into)\b)",
                         text or '', re.I):
        before = text[:m.start()]
        if not _NOT.search(before) and re.search(
                r"(?:^|[,.;\n]|\b(?:and|then|please|pls|you))\s*$", before, re.I):
            return True
    return False


def merges(task_id: int) -> bool:
    return bool((granted(task_id) or {}).get('merge'))


def words(task_id: int) -> str:
    return (granted(task_id) or {}).get("words", "")


#: The pause between showing a plan he is not asked about and starting it: long
#: enough for the gate to be in place, short enough not to be a wait.
#: Seconds between showing a plan that is not waiting for him and starting it.
AFTER_PLAN_SECONDS = 3.0
#: The smallest change whose plan waits for his approval: tier 2 = anything past
#: one or two files in one repo; contracts and high-risk plans always wait.
APPROVAL_FROM_TIER = 2


def after_plan_seconds() -> float:
    return AFTER_PLAN_SECONDS


def ask_from_tier() -> int:
    """The smallest change whose plan waits for his approval (1 = every plan,
    2 = anything past one class or file, 4 = never)."""
    return APPROVAL_FROM_TIER


#: He wants someone else to approve before any code: "share the plan to vinish,
#: get an approval and implement" (9 Oct, #272 — waved through as small).
_OUTSIDE_APPROVAL = re.compile(
    r"\b(?:get|take|ask\s+for|need|wait\s+for|after)\s+(?:an?\s+|his\s+|her\s+|their\s+)?"
    r"(?:approval|go-?ahead|sign-?off)\b"
    r"|\bapprov\w*\s+(?:from|by)\s+[a-z]+"
    r"|\b(?:share|send|show)\b[^.\n]{0,40}\bplan\b[^.\n]{0,60}"
    r"\b(?:approv\w*|review|go-?ahead|sign-?off|ok)\b", re.I)

#: The plan itself still asks him something — it is not ready to run.
_PLAN_ASKS = re.compile(
    r"\bawaiting\s+(?:your|arun'?s|his)\s+(?:approval|confirmation|answer|decision|go)"
    r"|\bopen\s+questions?\b"
    r"|\bI'?ll\s+implement\s+(?:as\s+soon\s+as|once|when)\s+you\b"
    r"|\bneeds?\s+(?:your|arun'?s|his)\s+(?:answer|decision|confirmation|input)\b"
    r"|\bplease\s+confirm\b"
    # 26 Oct, #419: "Still waiting on Arun for the exhausted-retries behavior …
    # (a) … (b) … (c) … Recommended (c)" went ahead as a small change.
    r"|\bwaiting\s+(?:on|for)\s+(?:you|arun|him)\b"
    r"|\(a\)[^\n]{0,400}?\(b\)", re.I)


def _asks_outside_approval(text: str) -> str:
    """The words asking for someone else's approval before code, or ""."""
    m = _OUTSIDE_APPROVAL.search(text or "")
    if m:
        return m.group(0)
    # "plan on requirements which i gave then send to Vinish i told for approval"
    for sentence in re.split(r"(?<=[.!?\n])\s+", text or ""):
        if (re.search(r"\bplan\b", sentence, re.I)
                and re.search(r"\b(?:approv\w*|go-?ahead|sign-?off)\b", sentence, re.I)
                and re.search(r"\b(?:send|share|show|get|take|ask|wait)\b", sentence, re.I)):
            return sentence.strip()[:300]
    return ""


def note_his_words(task_id: int, text: str) -> None:
    """Keep what he said about who must approve, in full. Gate answers are
    recorded clipped to 200 characters, and #272's "get an approval" came after."""
    if _asks_outside_approval(text):
        store.kv_set(f"task_outside_approval:{task_id}", (text or "")[:600])


def waits_for_someone(task_id: int) -> str:
    """His words asking for someone else's approval before code, or ""."""
    t = store.get_task(task_id) or {}
    said = store.kv_get(f"task_outside_approval:{task_id}") or ""
    if said:
        return said
    return _asks_outside_approval(f"{t.get('prompt') or ''}\n{words(task_id)}")


def reviewed_by_him(task_id: int) -> bool:
    """He answered an earlier plan on this task with changes, not a yes.

    26 Oct, #419: he pushed back on the first plan ("can't you do inside the rfp
    first activity…"); the revision was sized one file and went ahead on its own.
    A plan he is in the middle of reviewing comes back to him, whatever its size."""
    return any(e.get("kind") == "gate" and str(e.get("detail") or "").startswith("answer:")
               for e in store.task_events(task_id))


def no_ask(task_id: int, plan: str) -> str:
    """Why this plan goes ahead without waiting for him — "" when it waits.

    His rule, 16 Nov: every code change waits for his approval — a one-line fix
    included, because a plan built on a misread is cheaper to correct than a
    diff — UNLESS his own words for this task said to go all the way ("…and
    raise the PR", "just do it", "no need to show me the plan"). Size never
    decides any more (it did from 30 Sep). Even with his go, the plan stops for
    him when it asks something, is big or risky, or is about something other
    than what he named (stop_reason says which)."""
    store.kv_del(_stop_key(task_id))
    if not granted(task_id):
        return ""
    reason = _stop(task_id, plan)
    if reason:
        store.kv_set(_stop_key(task_id), reason)
        store.add_task_event(task_id, "go", f"waits despite his go-ahead: {reason}")
        return ""
    # His rule, 19 Nov: approval given up front means no stop at the plan gate.
    # Size, risk words, open questions and scope used to stop it anyway — a helm
    # list named "…_TO_BILLING" was "high impact (billing)" and waited, twice.
    # What would have stopped it rides along as a heads-up he can act on.
    note = _heads_up(task_id, plan)
    if note:
        store.add_task_event(task_id, "go", f"going ahead as he said; heads-up: {note}")
        return f"as you said — heads-up: {note}"
    return "as you said"


def _stop_key(task_id: int) -> str:
    return f"task_go_stopped:{task_id}"


def stop_reason(task_id: int) -> str:
    """Why a plan he said go on still waits for him, or ""."""
    return store.kv_get(_stop_key(task_id)) or ""


def _stop(task_id: int, plan: str) -> str:
    """What still stops a plan he said go on: only what HE asked for — someone
    else's approval first, or the bug reproduced first. Nothing Asta infers."""
    who = waits_for_someone(task_id)
    if who:
        return "you asked for someone else's approval first"
    from . import reproduction
    st = reproduction.state(task_id).get("state")
    if st and st != "confirmed":
        return "the bug was not reproduced"
    return ""


def _heads_up(task_id: int, plan: str) -> str:
    """What used to stop a go-ahead plan, now said without stopping it."""
    # A model repeating the approval ceremony is not a question.
    asks = re.sub(r"\bawaiting\s+(?:your|arun'?s|his)\s+approval\b", "", plan or "", flags=re.I)
    if _PLAN_ASKS.search(asks):
        return "the plan has an open question — it goes ahead on its own reading"
    from . import routing
    from .graph import outcome as _outcome
    reported = _outcome.reported(task_id, 0)
    try:
        tier, why = routing.plan_tier(plan, reported)
    except Exception:                                       # noqa: BLE001
        tier, why = 0, ""
    if tier >= routing.T3:
        return f"a big or risky change — {why}"
    off = _off_scope(task_id, plan, reported or {})
    if off:
        return off
    return ""


def _off_scope(task_id: int, plan: str, reported: dict) -> str:
    """The plan is about another ticket or repo than the one he named."""
    from . import routing
    t = store.get_task(task_id) or {}
    his = f"{t.get('title') or ''}\n{t.get('prompt') or ''}\n{words(task_id)}"
    his_tickets = {k.upper() for k in routing._TICKET.findall(his)}
    plan_tickets = {k.upper() for k in routing._TICKET.findall(plan or "")}
    if his_tickets and plan_tickets and not (his_tickets & plan_tickets):
        return f"the plan is about {', '.join(sorted(plan_tickets))}, not {', '.join(sorted(his_tickets))}"
    repos = [r for r in reported.get("repos") or [] if r]
    if repos:
        try:
            from .workspace import registry
            ws = registry.get(t.get("workspace") or "")
            known = list(getattr(ws, "repos", None) or [])
        except Exception:                                   # noqa: BLE001
            known = []
        named = {r for r in known if re.search(rf"\b{re.escape(r)}\b", his, re.I)}
        if named and not (named & set(repos)):
            return f"the plan changes {', '.join(repos)}, not {', '.join(sorted(named))}"
    return ""


def on_spawn(task_id: int, said: str) -> bool:
    """His own ask said to go all the way: the go-ahead rides with the task.
    "…and raise the PR" also ships it; "just do it" builds and stays local."""
    if not enabled():
        return False
    if wants_pr(said) or wants_merge(said):
        grant(task_id, said, ship=True)
        return True
    if go_ahead(said):
        grant(task_id, said, ship=False)
        return True
    return False


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
    # Every task the word could be for, at once. Choosing by a fixed order was
    # a guess: 7 Oct 22:31, "ship" (for #257, finished on its open PR) approved
    # #263's plan instead, because a task with a PR was not even a candidate.
    could = [t for t in rows if t["status"] in ("awaiting_approval", "running", "queued", "paused")
             and not t.get("pr_urls")]
    could += [t for t in rows if t["status"] == "done"]
    if len(could) == 1:
        return could[0], ""
    live = [t for t in could if t["status"] != "done"]
    if not live and could:
        done = [t for t in could if not t.get("pr_urls")]
        if len(done) == len(could):
            return max(could, key=lambda t: t["id"]), ""   # only unshipped work: the newest
    if len(could) > 1:
        listing = "\n".join(f"  #{t['id']} {t['title'][:45]} ({t['status']})" for t in could[:6])
        return None, (f"Which one do you mean?\n{listing}\n\n"
                      f"Say “{could[0]['id']} ship”.")
    return None, ""


def asked_with(task_id: int) -> str:
    """His words that started this task ("" when not started from his turn)."""
    try:
        return (json.loads(store.kv_get(f"task_asked:{task_id}") or "{}").get("his") or "").strip()
    except (ValueError, AttributeError):
        return ""


def one_ask(now: float | None = None) -> list[int]:
    """The live code tasks a go-ahead could mean, when they all came from ONE ask
    of his — [] otherwise. 19 Nov: one ask became three tasks, and "go ahead
    and create PR, don't need my approval" got "Which one do you mean?"."""
    now = time.time() if now is None else now
    live = [t for t in _candidates(now)
            if t["status"] in ("awaiting_approval", "running", "queued", "paused")
            and not t.get("pr_urls")]
    if len(live) < 2:
        return []
    said = {asked_with(t["id"]) for t in live}
    return sorted(t["id"] for t in live) if len(said) == 1 and "" not in said else []


async def act(text: str, wanted: int | None) -> str:
    """Carry out his go-ahead. "" when there is nothing it can mean."""
    from . import pr_authority, task_inform, tasks
    if wanted is None:
        siblings = one_ask()
        if siblings:
            notes = [await act(text, tid) for tid in siblings]
            return "\n".join(n for n in notes if n)
    if task_inform.stop_command(text)[0]:
        person = task_inform._STOP.match(text).group(1).strip()
        n = task_inform.revoke(wanted, person)
        return (f"OK — I won't tell {person or 'them'}; {n} saved update"
                f"{'s' if n != 1 else ''} cancelled.") if n else ""
    ended = pr_authority.revoke(wanted) if _STOP_MERGE.match(text or '') else 0
    stopped = (f"Stopped: {ended} saved PR instruction{'s' if ended != 1 else ''} revoked — "
               f"nothing more is approved or merged under {'them' if ended != 1 else 'it'}.")
    t, problem = target(wanted)
    if problem:
        return stopped if ended else problem
    if not t or t.get("kind") != "code":
        return stopped if ended else ""
    tid, pr = t["id"], wants_pr(text) or wants_merge(text)
    if _STOP_MERGE.match(text or ''):
        grant(tid, text, ship=False)
        return f"Task #{tid}: saved merge permission revoked; its other work can continue."
    title = (t.get("title") or "")[:50]
    store.record_outcome("command", "go", subject=str(tid), detail=(text or "")[:120])
    status = t["status"]
    if status == "awaiting_approval":
        if pr:
            grant(tid, text, ship=True)
        else:
            his_plan_yes(tid, text)
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
            if wanted is None:
                # A bare "go ahead and approve" is not about a finished task with
                # nothing to approve. 19 Nov: it answered "#<old task> … say raise PR"
                # while three PR approvals waited for exactly that word.
                return ""
            return (f"#{tid} {title} is finished and committed locally — "
                    f"say *raise PR* and I'll push it.")
        try:
            grant(tid, text, ship=True)
            return await tasks.ship(tid)
        except (ValueError, RuntimeError) as exc:
            return f"❌ #{tid}: couldn't raise the PR — {exc}"
    if t.get("pr_urls"):
        if wants_merge(text):
            grant(tid, text, ship=True)
            result = await tasks.check_pr(tid)
            return result or f"#{tid}: merge instruction saved; watching its checks and review requirements."
        return f"#{tid} is already up:\n{t['pr_urls']}"
    return ""
