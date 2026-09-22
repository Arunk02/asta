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


_RULES = re.compile(_LEAD + r"(?:(?:what\s+are\s+)?my\s+(?:standing\s+)?rules|"
                    r"(?:list|show)\s+(?:my\s+)?(?:standing\s+)?rules|standing\s+rules)" + _END, re.I)
_DROP_RULE = re.compile(_LEAD + r"(?:drop|forget|remove|delete|lift)\s+rule\s*#?(\d{1,4})" + _END, re.I)


_EVOLUTIONS = re.compile(_LEAD + r"(?:what\s+have\s+you\s+(?:changed|tuned)|"
                         r"(?:your\s+)?(?:own\s+)?changes|what\s+did\s+you\s+tune|"
                         r"(?:my\s+)?settings)" + _END, re.I)
_ROLLBACK = re.compile(_LEAD + r"roll\s*back\s*#?(\d{1,4})" + _END, re.I)
_PERMISSIONS = re.compile(_LEAD + r"(?:(?:what\s+)?(?:are\s+)?my\s+permissions|permissions|"
                          r"what\s+can\s+you\s+do\s+(?:alone|without\s+asking))" + _END, re.I)
_REVOKE = re.compile(_LEAD + r"revoke\s+(?:permission\s*)?#?(\d{1,4})" + _END, re.I)
_DIGEST_ASK = re.compile(_LEAD + r"(?:what(?:'s| is)?\s+in\s+the\s+digest|the\s+digest|digest)" + _END, re.I)
#: "push IT Service Desk" / "digest that channel" — only ever for a name Asta has
#: actually moved, so "push the branch" stays a perfectly ordinary message.
_FORCE = re.compile(_LEAD + r"(push|unmute|digest|mute)\s+(?P<name>[\w .&'-]{2,40}?)"
                    r"(?:\s+again)?" + _END, re.I)


#: His answer to a deadline warning. Short and unmistakable on purpose: anything
#: longer is a sentence for the brain, not a gate answer.
_PROMISE_REPLY = re.compile(
    r"^\s*(chase (them|him|her|it)|leave it|drop it|stop chasing|"
    r"i'?ll do it( myself)?)\s*[.!]?\s*$", re.I)


def _promises_at_a_gate() -> list[int]:
    """Follow-ups whose thread is parked at the deadline gate, from state."""
    from . import followup
    from .graph import engine
    out = []
    for row in followup.list_open():
        if (store.kv_get(engine.waiting_key("promise", row["id"])) or "") == "deadline":
            out.append(int(row["id"]))
    return out


def answer_from_state(text: str) -> str:
    """The reply, when the message is a question the task table answers. '' otherwise."""
    t = (text or "").strip()
    if not t or len(t) > 120:
        return ""
    from . import policy
    if _RULES.match(t):
        return policy.summary()
    if _EVOLUTIONS.match(t):
        from . import evolve, settings
        return evolve.summary() + "\n\n" + settings.summary()
    m = _ROLLBACK.match(t)
    if m:
        from . import evolve
        out = evolve.rollback(int(m.group(1)), "you asked")
        return (f"Rolled back: {out}" if out else
                f"There's nothing promoted as {m.group(1)}. Say “what have you changed”.")
    # A promise that warned him is a thread PARKED at a gate, and his one-word
    # answer is what it is waiting for. Without this the warning is a broadcast:
    # it asks "chase them or leave it?" and then has no way to hear the answer,
    # and the thread sits at its gate until a restart cleans it up. Rules only —
    # a brain is never asked which promise he meant.
    m = _PROMISE_REPLY.match(t)
    if m:
        from .graph import engine
        if engine.enabled():
            open_ones = [f for f in _promises_at_a_gate()]
            if len(open_ones) == 1:
                # Recorded, not driven. This function is synchronous and runs
                # inside the server's loop; the next tick applies it, and a
                # restart in between loses nothing.
                engine.record_answer("promise", open_ones[0], {"text": t})
                leave = t.strip().lower().startswith(("leave", "drop", "stop", "i'"))
                return ("Left it with you." if leave else
                        "Right — I'll keep chasing and tell you what moves.")
            if len(open_ones) > 1:
                return ("Which one — " + ", ".join(f"#{f}" for f in open_ones)
                        + "? Say “leave #N” or “chase #N”.")
    if _PERMISSIONS.match(t):
        from . import authority
        return authority.summary()
    m = _REVOKE.match(t)
    if m:
        from . import authority
        gid = int(m.group(1))
        return (f"Permission {gid} revoked — that goes back to asking you first."
                if authority.revoke(gid) else
                f"There's no permission {gid}. Say “my permissions” to list them.")
    if _DIGEST_ASK.match(t):
        from . import digest
        rows = digest.pending()
        return digest.render(rows) if rows else "Nothing in the digest right now."
    m = _FORCE.match(t)
    if m:
        from . import attention
        how = "push" if m.group(1).lower() in ("push", "unmute") else "digest"
        name = m.group("name").strip()
        # Only a source Asta has actually moved, or one he has steered before.
        if store.kv_get(f"attention_demoted:{name.lower()}") or attention.forced(name):
            return attention.set_force(name, how)
    m = _DROP_RULE.match(t)
    if m:
        rid = int(m.group(1))
        return (f"Rule {rid} dropped — it no longer applies." if policy.drop(rid)
                else f"There's no standing rule {rid}. Say “my rules” to list them.")
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


# --- standing instructions -----------------------------------------------------------------

#: An instruction, not a question or a remark: it opens with the imperative.
_IMPERATIVE = re.compile(
    r"^\s*(?:ok(?:ay)?[,\s]+|and\s+|also\s+|pls\s+|please\s+)*"
    r"(always|never|don'?t|dont|do not|from now on|going forward|stop|no more|remember|my\s+"
    r"(?:favou?rite|default|preferred))\b", re.I)


_BACK = re.compile(
    r"^\W*(?:ok(?:ay)?\W+)?(?:i'?m|i am|am) back\b"
    r"|\b(?:end|stop|cancel|lift|turn off)\s+(?:the\s+|my\s+)?(?:quiet|silence|silent mode|dnd|do not disturb)\b"
    r"|\bunmute\b|\b(?:you can|u can)\s+(?:notify|ping|message)\s+me\b"
    r"|\b(?:notifications?|pings?)\s+(?:back\s+)?on\b|\bresume\s+notifications?\b", re.I)


def ends_quiet(text: str) -> bool:
    """"I'm back" — only meaningful while his quiet time holds (the caller checks)."""
    return bool(_BACK.search((text or "").replace("’", "'")))


def standing_instruction(text: str):
    """The rule a standing instruction states — once per distinct instruction.

    Two ways in: a message that OPENS with the imperative ("don't…", "always…",
    "from now on…"), or one that states a default anywhere in it ("…and remember
    my favourite workspace is booking"). A question is never an instruction.
    """
    from . import instructions
    t = (text or "").replace("’", "'").strip()
    if not t or t.endswith("?"):
        return None
    cand = instructions.compile(t)
    if cand is None:
        return None
    # A typed rule (quiet time, a mute, a never, a default) is heard wherever it
    # sits in the message — his weekend rule began "And also…", and a detector
    # that only listened to messages opening with an imperative never heard it.
    # A free-text note still needs the imperative, or every remark with
    # "always" in it would become a proposal.
    if cand.kind == "note" and not (_IMPERATIVE.search(t) or instructions.states_a_default(t)):
        return None
    return cand if first_time(cand, t) else None


def first_time(cand, text: str) -> bool:
    """True the first time this rule is offered — from the front desk or from a
    brain's `remember` — so he is never asked the same thing twice."""
    key = f"rule_proposed:{cand.kind}:{cand.act}:{cand.target.lower()}:{cand.value}"
    if cand.kind == "note":
        key += ":" + re.sub(r"\W+", "", (text or "").lower())[:60]
    if store.kv_get(key):
        return False
    store.kv_set(key, "1")
    return True


#: Words that say an instruction is meant to last. A "note" rule is the compiler's
#: catch-all, so on its own it cannot tell an instruction from a request that
#: merely starts with "always" — but a message that says "remember this always"
#: or "from now on" is telling you which one it is.
_STANDING = re.compile(
    r"\b(remember (this|that)|always|from now on|going forward|every ?time|"
    r"never|in future|henceforth|by default)\b", re.I)


def instruction_only(text: str, cand) -> bool:
    """Was the message JUST the instruction? Then the proposal answers it. A longer
    message with a request in it still goes on to be handled.

    A note-kind rule used to be never "only", so it always fell through — and on
    17 Sep "don't include PR review in standup … remember this always" fell
    straight into the PR check that happened to be running, where a brain folded
    it in, "saved" it to its own notes before he said yes, and narrated its
    memory directory to his phone. A note that says it is standing is the whole
    message.
    """
    t = (text or "").strip()
    if "?" in t:
        return False
    if cand.kind != "note":
        return len(t) <= 200
    return len(t) <= 320 and bool(_STANDING.search(t))

