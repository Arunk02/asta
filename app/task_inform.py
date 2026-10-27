"""His "…and inform Vinish", carried by the coding task until it is true.

"Fix it, raise the PR, merge after CI and inform Vinish" was one instruction.
The task kept the PR and merge halves (go.py) and dropped the last one: the
"inform Vinish" part reached a brain, which staged a draft — and he was asked
again for the very message he had just told Asta to send.

So the named person is kept with the task, read from HIS words only (the turn
that spawned it, or his go-ahead on it — never a brief, a PR or a colleague's
message), and told once when the milestone he named is reached: the merge if he
asked for one, otherwise the PR. One exact 1:1 contact, never a group.

Before anything is typed: sends held, his standing rules, a senior recipient and
the chat Teams would actually open are all checked, and the send claims a
receipt. A refusal stages the same words for his yes instead; an uncertain send
is reported and never repeated. Revoked, cancelled or rejected tasks tell nobody.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from . import store

PREFIX = "task_inform:"
RECEIPT = "task_inform_receipt:"
STAGES = ("pr", "merged")
#: The verb, then one name — a surname only when he capitalised it.
_VERB = re.compile(r"\b(?:inform|tell|ping|message|msg|notify|update|let)\s+"
                   r"([A-Za-z][\w'.-]*(?-i:\s+[A-Z][\w'.-]*)?)", re.I)
#: Not a person: himself, or more than one person.
_NOT_A_PERSON = {"me", "him", "her", "them", "us", "everyone", "everybody", "all",
                 "team", "the", "channel", "group", "it", "know", "people"}
_GROUPISH = re.compile(r"\b(?:team|channel|group|everyone|everybody|all\s+of)\b", re.I)
_STOP = re.compile(r"^\s*(?:don'?t|do\s+not|no\s+need\s+to|stop|never)\s+"
                   r"(?:inform|tell|ping|message|msg|notify|update)\b\s*([\w .'-]*?)"
                   r"(?:\s+(?:for|on|of)?\s*(?:task\s*)?#?\s*(\d{1,5}))?\s*[.!]*\s*$", re.I)
_DEAD = ("cancelled", "rejected", "failed", "pr_closed")


def parse(words: str) -> tuple[str, str] | None:
    """(person, stage) when his words ask to tell exactly one named person."""
    from . import consent, go
    for m in _VERB.finditer(words or ""):
        if consent._negated(words, m.start()):
            continue
        name = re.sub(r"\s+(?:know|about|once|when|after|that|on|in)\b.*$", "",
                      m.group(1).strip(), flags=re.I).strip(" .,'")
        if not name or name.split()[0].lower() in _NOT_A_PERSON:
            continue
        clause = (words or "")[m.start():m.start() + 80]
        after = (words or "")[m.start(1) + len(name):]
        if _GROUPISH.search(clause) or re.match(r"\s*(?:,|&|\band\b|\bor\b)\s*\w", after, re.I):
            return None       # more than one person: his yes, as before
        return name, ("merged" if go.wants_merge(words) else "pr")
    return None


def record(task_id: int, words: str) -> dict | None:
    task = store.get_task(task_id)
    found = parse(words)
    if not task or task.get("kind") != "code" or not found:
        return None
    person, stage = found
    rec = {"version": 1, "person": person, "stage": stage, "words": (words or "")[:300],
           "state": "pending", "at": time.time()}
    store.kv_set(PREFIX + str(task_id), json.dumps(rec))
    store.add_task_event(task_id, "inform", f"tell {person} once the {stage} happens")
    return rec


def get(task_id) -> dict | None:
    try:
        rec = json.loads(store.kv_get(PREFIX + str(task_id)) or "null")
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("state") == "pending" else None


def _end(task_id, rec: dict, state: str) -> None:
    store.kv_set(PREFIX + str(task_id), json.dumps({**rec, "state": state, "ended_at": time.time()}))


def stop_command(text: str) -> tuple[bool, int | None]:
    m = _STOP.match(text or "")
    return (True, int(m.group(2)) if m and m.group(2) else None) if m else (False, None)


def revoke(task_id: int | None = None, person: str = "") -> int:
    ended = 0
    for key in store.kv_like(PREFIX + "%"):
        tid = key[len(PREFIX):]
        rec = get(tid) if tid.isdigit() else None
        if not rec or (task_id is not None and int(tid) != task_id):
            continue
        if person and person.lower() not in rec["person"].lower():
            continue
        _end(tid, rec, "revoked")
        store.add_task_event(int(tid), "inform", f"revoked: not telling {rec['person']}")
        ended += 1
    return ended


def message(task: dict, stage: str, person: str) -> str:
    from . import prname, writing
    links = [u for u in (task.get("pr_urls") or "").split() if u]
    named = ", ".join(prname.name_links(u) for u in links) or "the change"
    what = (f"{task.get('title', '')[:80]} is merged — {named}" if stage == "merged"
            else f"PR is up for {task.get('title', '')[:80]} — {named}")
    return writing.fit_address(f"Hi {person.split()[0]}, {what}", person)


def _opened_is(person: str, opened: str) -> bool:
    words = [w.lower() for w in person.split()]
    have = (opened or "").lower()
    return bool(words) and all(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", have) for w in words)


async def reached(task_id: int, stage: str) -> str | None:
    """The milestone happened. Tell the person once, or stage it. The line he reads."""
    rec = get(task_id)
    task = store.get_task(task_id) or {}
    if not rec or STAGES.index(stage) < STAGES.index(rec["stage"]):
        return None
    if task.get("status") in _DEAD:
        _end(task_id, rec, "dropped")
        return None
    person, text = rec["person"], message(task, stage, rec["person"])
    name = f"{task_id}:{rec['stage']}"
    with store._connect() as conn:
        if conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)",
                        (RECEIPT + name, json.dumps({"state": "checking", "to": person,
                                                     "at": time.time()}))).rowcount != 1:
            return None
    why = await _refusal(person)
    if why:
        from . import loop
        loop.set_task_send(task_id, text, person, "teams")
        store.kv_set(RECEIPT + name, json.dumps({"state": "staged", "to": person, "why": why}))
        _end(task_id, rec, "staged")
        from . import answers
        await answers.present_task_draft(task_id, task)
        return f"Not sent to {person} on my own — {why}. The message is waiting for your yes."
    from . import chat_watch, ops, teams_bridge
    store.kv_set(RECEIPT + name, json.dumps({"state": "sending", "to": person, "at": time.time()}))
    token = teams_bridge._automatic_send.set(person)
    try:
        line = await ops.REGISTRY["teams_send"]["run"](to=person, text=text, to_group=False)
    except (Exception, asyncio.CancelledError) as exc:       # noqa: BLE001
        store.kv_set(RECEIPT + name, json.dumps({"state": "uncertain", "to": person,
                                                 "why": f"{type(exc).__name__}: {exc}"[:200]}))
        _end(task_id, rec, "uncertain")
        if isinstance(exc, asyncio.CancelledError):
            raise
        return (f"⚠ Telling {person} about #{task_id} was not confirmed — {exc}. "
                f"Check the chat before sending again; I won't resend it.")
    finally:
        teams_bridge._automatic_send.reset(token)
    ok = line.startswith("✅ Sent to ")
    store.kv_set(RECEIPT + name, json.dumps({"state": "sent" if ok else "uncertain",
                                             "to": person, "line": line[:200], "at": time.time()}))
    _end(task_id, rec, "sent" if ok else "uncertain")
    if ok:
        chat_watch.note_asta_said(person, text)
        store.record_outcome("inform", "sent", subject=str(task_id), detail=f"{person}: {text}"[:200])
        return f"📨 Told {person}, as you asked:\n{text}"
    return f"⚠ Telling {person} about #{task_id} was not confirmed — {line[:120]}. Not resent."


async def _refusal(person: str) -> str:
    """Why this must wait for his yes after all, or ''."""
    from . import policy, senior, steward, teams_bridge
    if steward.sends_held():
        return "sends are on hold"
    rule = policy.check("send", person, asked=True)
    if not rule.ok:
        return rule.why
    if senior.is_senior(person):
        return f"{person} is senior, and those always wait for your yes"
    try:
        opened = (await teams_bridge.resolve_target(person))["opened"]
    except Exception as exc:                                    # noqa: BLE001
        return f"could not open their chat ({type(exc).__name__})"
    if not _opened_is(person, opened):
        return f"Teams opens “{opened}” for “{person}”, which may not be them"
    return ""
