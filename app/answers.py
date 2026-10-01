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

BUT A QUESTION HE DOES NOT ANSWER MUST NOT STOP THE LINE. 29 Sep: a draft for
Harika was shown at 17:40 and never answered, and behind it Vinish's booking
check (done 19:07) and Harika's own follow-up sat unseen all evening while the
colleagues waited. A decision left unanswered for PARK_SECONDS is parked — it
goes to the back of the queue and the next one is shown; one that is still
unanswered after being shown MAX_SHOWINGS times, or that is older than
STALE_SECONDS, is retired and he is told, in one line, what was dropped.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import store

_QUEUE = "answers_queue"


def _minutes(raw: str | None, default: float) -> float:
    try:
        return float(raw or default) * 60
    except ValueError:
        return default * 60


#: Unanswered this long, a decision steps aside for the next one.
PARK_SECONDS = _minutes(os.environ.get("ASTA_DECISION_PARK_MINUTES"), 15)
#: A reply to a colleague older than this is no longer worth sending.
STALE_SECONDS = _minutes(os.environ.get("ASTA_DECISION_STALE_MINUTES"), 180)
#: Shown this many times without an answer, it is retired.
MAX_SHOWINGS = 2

#: Appended to an investigation's brief when a colleague is waiting for the answer.
REPLY_FORMAT = """

When you are done, end with exactly these two sections and nothing after them:

ANALYSIS:
For Arun, at most five short lines: each of {who}'s questions answered directly
(yes/no/what, then the evidence: file:line, the log line, the workflow, the PR
state, the document and heading). At most one "Also noticed:" line, only if it
bears on their question.

REPLY:
The message to send to {who}, in Arun's voice: short, plain and polite, no
greeting ceremony, no sign-off. Answer what they asked, in the order they asked
it; nothing they did not ask about. If you could not find it,
say briefly what you checked and ask them for the one thing you need. Never
promise work Arun has not agreed to.

If what they asked for is Arun himself — a call, a discussion — the ANALYSIS
prepares him for it (where the topic stands, what changed, what is still open)
and the REPLY answers the request, without committing him to a time."""


def brief_rider(who: str) -> str:
    """The reply section of the brief, with how he actually writes to them."""
    from . import style
    voice = ""
    try:
        voice = style.rider(who)
    except Exception:                                          # noqa: BLE001
        pass
    return REPLY_FORMAT.format(who=who or "the colleague") + (f"\n\n{voice}" if voice else "")


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


def render(who: str, need: str, analysis: str, reply: str, note: str = "",
           lead: str = "") -> str:
    """Exactly what he needs to decide, said the way a colleague would say it.

    Was a form — "🧑‍💻 X asked: … 🔎 … ✉️ Reply to X: ——— … ——— Send it?" — the
    same five boxes for every ask. His words, 29 Sep: talk to me, don't make
    the format constant. So: what they wanted, what was found, the reply as a
    quote, one question."""
    first = (who or "Someone").split()[0]
    need = (need or "").strip().rstrip(".")
    head = (f"*{who}* asked about {need[0].lower() + need[1:]}." if need
            else f"*{who}* asked me something.")
    # The reader's own sentence, when it wrote one ("Yogesh wants a quick call
    # about the event-history defect…"), says it better than the template.
    parts = [(lead or head) + (f" {note}" if note else "")]
    if analysis:
        parts.append(analysis)
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in reply.splitlines())
    parts.append(f"Here's what I'd send {first}:\n{quoted}")
    parts.append("Shall I send it? Or tell me what to change.")
    return "\n\n".join(parts)


async def present(*, who: str, need: str, chat: str, group: bool, analysis: str,
                  reply: str, task_id: int | None = None, thread: str = "",
                  note: str = "", lead: str = "") -> bool:
    """Stage the reply and put the decision in front of him. False if it cannot."""
    from . import loop, notify, threads
    cid = phone_conversation()
    if not cid or not (reply or "").strip() or not chat:
        return False
    if _already_put(chat, need, reply):
        # The same ask for the same person, already in front of him (or already
        # answered) today. "These we already discussed — why again?" (30 Sep,
        # Vinish's "can we connect", re-read from an old message.)
        store.record_outcome("answer", "duplicate", subject=str(task_id or ""),
                             detail=f"{who}: {need}"[:200])
        return False
    more = followups(task_id)
    if more:
        first = (who or "They").split()[0]
        said = "; ".join(f"“{m[:200]}”" for m in more[-3:])
        note = ((note + "\n") if note else "") + (
            f"⚠️ {first} added while I was on it: {said} — check the reply covers it.")
    intent = {"kind": "send", "what": reply.strip(), "to": chat, "channel": "teams",
              "to_group": bool(group), "task_id": task_id, "thread": thread,
              "who": who, "need": need, "analysis": analysis, "note": note,
              "lead": lead, "_at": time.time()}
    if thread:
        threads.update(thread, status="awaiting_arun")
    if _blocked(cid):
        _enqueue({"type": "answer", **intent})
        return True
    await _show(cid, intent)
    return True


def _blocked(cid: str, now: float | None = None) -> bool:
    """Is he already being asked something his next "yes" would answer?

    Something he has left unanswered for PARK_SECONDS no longer counts: a draft
    of ours is parked at the back of the queue, and an old offer simply stops
    holding the line (it stays open — a staged draft is read before an offer, so
    his next "yes" still goes to what he was shown last)."""
    from . import loop, offers
    now = time.time() if now is None else now
    staged = loop.awaiting(cid)
    if staged:
        if staged.get("type") != "answer" or now - float(staged.get("_shown") or 0) < PARK_SECONDS:
            return True
        if not _load_queue():
            # Nothing else is waiting: parking would only re-show the SAME
            # question 15 minutes later ("Asking again…", 30 Sep) — a nag, not
            # a way through. It stays where he left it.
            return True
        loop.clear_awaiting(cid)
        _park(staged, now)
    o = offers.pending()
    return o is not None and now - float(o.created or 0) < PARK_SECONDS


def _park(item: dict, now: float) -> None:
    q = _load_queue()
    q.append(item)
    store.kv_set(_QUEUE, json.dumps(q))
    store.record_outcome("answer", "parked", subject=str(item.get("task_id") or ""),
                         detail=f"{item.get('who')}: {item.get('need', '')}"[:200])


def _enqueue(item: dict) -> None:
    item.setdefault("_at", time.time())
    q = _load_queue()
    q.append(item)
    store.kv_set(_QUEUE, json.dumps(q))
    store.record_outcome("answer", "queued", subject=str(item.get("task_id") or ""),
                         detail=f"{item.get('type')}: {item.get('who')}: {item.get('need', '')}"[:200])


async def offer_plan(*, who: str, chat: str, need: str, summary: str, thread: str,
                     words: str = "") -> bool:
    """A colleague wants code changed: ask him whether to plan it — now, or
    after whatever he is already deciding. True if shown now.

    `words` is what they actually wrote, quotes included. The plan is briefed
    from it, not from a one-line summary: "add the field Sonal sent, to whatever
    he's replying on" named neither the field nor the PR (1 Oct)."""
    from . import threads
    item = {"type": "plan", "who": who, "chat": chat, "need": need,
            "summary": summary, "thread": thread, "words": (words or "")[:2500]}
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
                f"({item.get('summary', '')})."
                + (f"\n\nWhat {who} actually wrote — quotes included; the field, the PR "
                   f"and the repo are in here, so brief the task from THIS and do not ask "
                   f"Arun for what it already says:\n{item['words']}" if item.get("words") else "")
                + "\n\nIts plan gate brings the plan back to Arun; nothing big is "
                  "written before he approves it, and nothing ships before he says ship."),
        kind="plan_code", payload={"who": who, "chat": item.get("chat"),
                                   "thread": item.get("thread")})
    await notify.notify(f"🛠 *{who}* asks for a code change: {need}\n\n"
                        f"Reply *yes* and I'll plan it — the plan comes to you before any code.",
                        "answer", urgency="direct", considered=True)


async def _show(cid: str, intent: dict) -> None:
    from . import loop, notify
    intent = {**intent, "type": "answer", "_shown": time.time(),
              "_showings": int(intent.get("_showings") or 0) + 1}
    loop.stage(cid, intent)
    waiting = len(_load_queue())
    note = intent.get("note", "")
    if intent["_showings"] > 1:
        note = (note + "\n" if note else "") + "(Asking again — this one is still unanswered.)"
    text = render(intent["who"], intent.get("need", ""), intent.get("analysis", ""),
                  intent["what"], note, lead=intent.get("lead", ""))
    if waiting:
        text += f"\n\n({waiting} more waiting after this one.)"
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


async def announce_offer() -> bool:
    """Ask the offer that moved up the queue, once — as the queue promised.

    A queued offer says "I'll ask when that one is answered". On 29 Sep an
    offer moved up without being asked, and a finished answer for a colleague
    waited behind a question he had never seen.
    """
    from . import notify, offers
    o = offers.pending()
    if o is None or not store.kv_get(f"offer_unasked:{o.id}"):
        return False
    store.kv_set(f"offer_unasked:{o.id}", "")
    await notify.notify(o.render(), "offer", urgency="direct", considered=True)
    return True


def _retire(now: float) -> list[dict]:
    """Take out what is too old to send, or has been ignored enough times."""
    keep, gone = [], []
    for it in _load_queue():
        old = now - float(it.get("_at") or 0) > STALE_SECONDS
        ignored = int(it.get("_showings") or 0) >= MAX_SHOWINGS
        (gone if old or ignored else keep).append(it)
    if gone:
        store.kv_set(_QUEUE, json.dumps(keep))
        for it in gone:
            store.record_outcome("answer", "retired", subject=str(it.get("task_id") or ""),
                                 detail=f"{it.get('who')}: {it.get('need', '')}"[:200])
    return gone


async def next_after(cid: str = "") -> bool:
    """Nothing is in front of him: show the next waiting decision, if any."""
    from . import notify
    cid = cid or phone_conversation()
    if not cid or _blocked(cid):
        return False
    gone = _retire(time.time())
    if gone:
        names = "; ".join(f"{g.get('who')} ({(g.get('need') or '')[:50]})" for g in gone)
        # The drafts themselves go into his conversation, so "send Vinish's one"
        # has something to find — the note would be an empty promise otherwise.
        drafts = "\n\n".join(f"Draft for {g.get('who')} ({g.get('to')}): {g.get('what')}"
                              for g in gone if g.get("what"))
        if drafts:
            store.add_ui_message(cid, "assistant", "Unsent drafts, retired:\n\n" + drafts,
                                 {"via": "answer", "channel": "whatsapp"})
        await notify.notify(f"🗂 Didn't send these — too old now, or left unanswered: {names}. "
                            "Tell me if you still want one to go out.",
                            "answer", urgency="ambient", considered=True)
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


#: How long after an answer is finished a further message still belongs to it.
FOLLOWUP_SECONDS = float(os.environ.get("ASTA_ANSWER_FOLLOWUP_MINUTES", "45")) * 60


def _meta(task_id: int) -> dict:
    try:
        d = json.loads(store.kv_get(f"answer_meta:{task_id}") or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


#: How long the same answer for the same person is "already put to him".
REPEAT_SECONDS = float(os.environ.get("ASTA_ANSWER_REPEAT_HOURS", "12")) * 3600


def _gist(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())[:200]


def _already_put(chat: str, need: str, reply: str, now: float | None = None) -> bool:
    """Was this same answer — same person, same ask or same reply — already
    shown to him recently? Records it when not."""
    import hashlib
    now = time.time() if now is None else now
    key = f"answer_put:{(chat or '').strip().lower()[:60]}"
    try:
        seen = [x for x in json.loads(store.kv_get(key) or "[]")
                if now - float(x.get("at", 0)) < REPEAT_SECONDS]
    except (ValueError, TypeError, AttributeError):
        seen = []
    marks = {hashlib.sha1(g.encode()).hexdigest()[:16] for g in (_gist(need), _gist(reply)) if g}
    if any(m in {x.get("h") for x in seen} for m in marks):
        return True
    seen += [{"h": m, "at": now} for m in marks]
    store.kv_set(key, json.dumps(seen[-40:]))
    return False


def followups(task_id: int | None) -> list[str]:
    if not task_id:
        return []
    try:
        d = json.loads(store.kv_get(f"answer_followups:{task_id}") or "[]")
    except ValueError:
        return []
    return [str(x) for x in d if str(x).strip()] if isinstance(d, list) else []


async def note_followup(thread: str, who: str, text: str, now: float | None = None) -> int | None:
    """They said more while their answer was being worked out, or was waiting
    for his "send". Kept with that answer, so it reaches him WITH the draft.

    30 Sep: Sankalp asked for Mexico as a one-click country, then added "enable
    in uat and pp both". The second message found nothing to check, went
    nowhere, and the draft and the code task that followed were built for prod.
    Returns the task it was kept with, or None."""
    from . import loop, notify
    text = " ".join((text or "").split())
    if not thread or not text:
        return None
    now = time.time() if now is None else now
    for t in store.list_tasks(limit=40):
        meta = _meta(t["id"])
        if meta.get("thread") != thread:
            continue
        live = t["status"] in ("running", "queued", "paused")
        recent = t["status"] == "done" and now - float(t.get("finished_at") or 0) < FOLLOWUP_SECONDS
        if not (live or recent):
            continue
        if now - float(t.get("created_at") or 0) < 20 or text[:80] in " ".join((t.get("prompt") or "").split()):
            return None                     # the message that started it, not a follow-up
        kept = followups(t["id"])
        if text in kept:
            return t["id"]
        store.kv_set(f"answer_followups:{t['id']}", json.dumps((kept + [text[:400]])[-5:]))
        store.record_outcome("answer", "followup", subject=str(t["id"]),
                             detail=f"{who}: {text}"[:200])
        cid = phone_conversation()
        staged = loop.awaiting(cid) if cid else None
        if staged and staged.get("task_id") == t["id"]:
            # The draft in front of him was written before this. Say so, once.
            first = (who or "They").split()[0]
            await notify.notify(f"↪︎ {first} added, after that draft was written: “{text[:300]}”. "
                                f"It isn't covered above — tell me what to change, or say send.",
                                "answer", urgency="direct", considered=True)
        return t["id"]
    return None


def remember_meta(task_id: int, *, who: str, need: str, chat: str, group: bool,
                  thread: str) -> None:
    store.kv_set(f"answer_meta:{task_id}", json.dumps(
        {"who": who, "need": need, "chat": chat, "group": group, "thread": thread}))
