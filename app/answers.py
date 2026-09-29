"""The finished answer, put in front of him once: who asked, what was found, the reply — send?

His words, 29 Sep:

    only final analysis report comes to [me] before ask: this person asked this,
    this is the final analysis, can i send — irrespective of debugging or any
    code task they gave

Before this, a colleague's ask produced a red push when it arrived, a "✅ Task
#N done" with a raw result minutes later, and then he wrote the reply himself.
Three interruptions, and the last step — the one that actually answers the
colleague — was his.

Now the investigation is briefed to end with two sections: ANALYSIS, for him,
and REPLY, for them, in his voice. When it finishes, the reply is staged in his
phone conversation through the same "can I send this?" path every other
outward message uses, so a plain "send" delivers it — as a recorded Teams call,
never re-typed by a model — and anything else he says revises it.

ONE DECISION IN FRONT OF HIM AT A TIME. A staged draft and an open offer both
read his next "yes" — so an answer that finishes while he is deciding on
something else waits in a queue, and so does a "want me to plan this code
change?". Two open questions sharing one word is how the wrong thing goes out.
The queue is drained after each sweep and after each send.
"""

from __future__ import annotations

import json
import re

from . import store

_QUEUE = "answers_queue"

#: Appended to an investigation's brief when a colleague is waiting for the answer.
REPLY_FORMAT = """

When you are done, end with exactly these two sections and nothing after them:

ANALYSIS:
For Arun, at most five short lines: what {who} asked, what you found, and the
evidence (file:line, the log line, the PR state, the document and heading).

REPLY:
The message to send to {who}, in Arun's voice: short, plain and polite, no
greeting ceremony, no sign-off. Give them the answer. If you could not find it,
say briefly what you checked and ask them for the one thing you need. Never
promise work Arun has not agreed to."""


def brief_rider(who: str) -> str:
    return REPLY_FORMAT.format(who=who or "the colleague")


_SECTION = re.compile(r"^\W*(ANALYSIS|REPLY)\W*:?\W*$", re.I | re.M)


def split(result: str) -> tuple[str, str]:
    """(analysis, reply) from a finished investigation. ('', '') if it did not
    use the format — the caller then falls back to the ordinary task report."""
    text = (result or "").strip()
    marks = list(_SECTION.finditer(text))
    found: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        found[m.group(1).upper()] = text[m.end():end].strip().strip("—-").strip()
    reply = found.get("REPLY", "").strip().strip('"“”').strip()
    return found.get("ANALYSIS", "").strip(), reply


def phone_conversation() -> str:
    """Where he answers from: his WhatsApp conversation, else Telegram's."""
    return (store.kv_get("wa_conversation") or store.kv_get("telegram_conversation") or "").strip()


def render(who: str, need: str, analysis: str, reply: str, note: str = "") -> str:
    """Exactly what he needs to decide, and nothing else."""
    head = f"🧑‍💻 *{who}* asked: {need}" if need else f"🧑‍💻 *{who}* asked something"
    parts = [head]
    if note:
        parts.append(note)
    if analysis:
        parts.append(f"🔎 {analysis}")
    parts.append(f"✉️ Reply to {who}:\n———\n{reply}\n———")
    parts.append("Send it? Reply *send*, or tell me what to change.")
    return "\n\n".join(parts)


async def present(*, who: str, need: str, chat: str, group: bool, analysis: str,
                  reply: str, task_id: int | None = None, thread: str = "",
                  note: str = "") -> bool:
    """Stage the reply and put the decision in front of him. False if it cannot."""
    from . import loop, notify, threads
    cid = phone_conversation()
    if not cid or not (reply or "").strip() or not chat:
        return False
    intent = {"kind": "send", "what": reply.strip(), "to": chat, "channel": "teams",
              "to_group": bool(group), "task_id": task_id, "thread": thread,
              "who": who, "need": need, "analysis": analysis, "note": note}
    if thread:
        threads.update(thread, status="awaiting_arun")
    if _blocked(cid):
        _enqueue({"type": "answer", **intent})
        return True
    await _show(cid, intent)
    return True


def _blocked(cid: str) -> bool:
    """Is he already being asked something his next "yes" would answer?"""
    from . import loop, offers
    return bool(loop.awaiting(cid)) or offers.pending() is not None


def _enqueue(item: dict) -> None:
    q = _load_queue()
    q.append(item)
    store.kv_set(_QUEUE, json.dumps(q))
    store.record_outcome("answer", "queued", subject=str(item.get("task_id") or ""),
                         detail=f"{item.get('type')}: {item.get('who')}: {item.get('need', '')}"[:200])


async def offer_plan(*, who: str, chat: str, need: str, summary: str, thread: str) -> bool:
    """A colleague wants code changed: ask him whether to plan it — now, or
    after whatever he is already deciding. True if shown now."""
    from . import threads
    item = {"type": "plan", "who": who, "chat": chat, "need": need,
            "summary": summary, "thread": thread}
    if thread:
        threads.update(thread, status="awaiting_arun")
    cid = phone_conversation()
    if cid and _blocked(cid):
        _enqueue(item)
        return False
    await _show_plan(item)
    return True


async def _show_plan(item: dict) -> None:
    from . import notify, offers
    who, need = item["who"], item["need"]
    offers.propose(
        subject=f"🛠 {who} asks for a code change",
        context=f"{who}: {need}\n{item.get('summary', '')}",
        question="Want me to plan it? The plan comes to you before any code.",
        action=(f"Delegate a CODE task for: {need} — asked by {who} on Teams "
                f"({item.get('summary', '')}). Its plan gate brings the plan back to "
                f"Arun; nothing is written before he approves it, and nothing ships "
                f"before he says ship."),
        kind="plan_code", payload={"who": who, "chat": item.get("chat"),
                                   "thread": item.get("thread")})
    await notify.notify(f"🛠 *{who}* asks for a code change: {need}\n\n"
                        f"Reply *yes* and I'll plan it — the plan comes to you before any code.",
                        "answer", urgency="direct", considered=True)


async def _show(cid: str, intent: dict) -> None:
    from . import loop, notify
    loop.stage(cid, intent)
    text = render(intent["who"], intent.get("need", ""), intent.get("analysis", ""),
                  intent["what"], intent.get("note", ""))
    # In the conversation too, so "change the second line" has something to refer to.
    store.add_ui_message(cid, "assistant", text, {"via": "answer", "channel": "whatsapp"})
    store.record_outcome("answer", "presented", subject=str(intent.get("task_id") or ""),
                         detail=f"{intent['who']}: {intent.get('need', '')}"[:200])
    await notify.notify(text, "answer", urgency="direct", considered=True)


def _load_queue() -> list[dict]:
    try:
        q = json.loads(store.kv_get(_QUEUE) or "[]")
    except ValueError:
        return []
    return [x for x in q if isinstance(x, dict)]


async def next_after(cid: str = "") -> bool:
    """Nothing is in front of him: show the next waiting decision, if any."""
    cid = cid or phone_conversation()
    if not cid or _blocked(cid):
        return False
    q = _load_queue()
    if not q:
        return False
    item = q.pop(0)
    store.kv_set(_QUEUE, json.dumps(q))
    if item.get("type") == "plan":
        await _show_plan(item)
    else:
        await _show(cid, item)
    return True


def sent(staged: dict) -> None:
    """The reply went out: the conversation now has Asta's words in it."""
    from . import threads
    tid = (staged or {}).get("thread") or ""
    if tid and threads.get(tid):
        threads.update(tid, asta_spoke=1, status="answered")
        threads._record(tid, "answered", (staged.get("what") or "")[:160])


async def present_task(task_id: int, t: dict, result: str) -> bool:
    """A finished investigation that a colleague is waiting on. False: not one."""
    try:
        meta = json.loads(store.kv_get(f"answer_meta:{task_id}") or "{}")
    except ValueError:
        meta = {}
    if not meta:
        return False
    analysis, reply = split(result)
    if not reply:
        return False
    return await present(who=meta.get("who", ""), need=meta.get("need", ""),
                         chat=meta.get("chat") or t.get("teams_chat", ""),
                         group=bool(meta.get("group")), analysis=analysis, reply=reply,
                         task_id=task_id, thread=meta.get("thread", ""))


def remember_meta(task_id: int, *, who: str, need: str, chat: str, group: bool,
                  thread: str) -> None:
    store.kv_set(f"answer_meta:{task_id}", json.dumps(
        {"who": who, "need": need, "chat": chat, "group": group, "thread": thread}))
