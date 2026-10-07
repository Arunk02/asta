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
and REPLY, for them, in his voice. Unless Arun explicitly granted automatic
Teams replies to that exact 1:1, the reply is staged in his phone conversation
through the same "can I send this?" path every other outward message uses.

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
import hashlib
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
it; nothing they did not ask about. If you could not find it, or a term or
reference is unclear ("topic refresh", "the prod PR"), the REPLY is the one short
question to {who} that gets you what you need — ALWAYS write a REPLY; never end
by asking Arun whether to ask them. Never promise work Arun has not agreed to.

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
                  note: str = "", lead: str = "", source_text: str = "",
                  ask_kind: str = "") -> bool:
    """Stage the reply and put the decision in front of him. False if it cannot."""
    from . import loop, notify, threads
    source = {"to_group": group, "to": chat, "who": who,
              "source_text": source_text, "ask_kind": ask_kind}
    if not origin_allowed(source):
        _record_rejected_origin(source, task_id)
        return True  # Handled: the task worker must not send a fallback notification.
    cid = phone_conversation()
    if not cid or not (reply or "").strip() or not chat:
        return False
    origin = _review_origin(task_id)
    if origin and not await review_is_current(origin):
        store.record_outcome("answer", "stale review", subject=str(task_id or ""),
                             detail=f"{origin['ref']} changed before staging")
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
    from . import chat_watch, writing
    # He answered them himself while this was being worked out — Shabda's
    # "please review the email service issue" got his "will check and update
    # and approve" at 14:51, and a reply for him to send arrived at 14:53
    # anyway (1 Oct). The finding still reaches him; a second reply does not.
    if await chat_watch.he_replied_since(chat, chat_watch.their_last(chat)):
        first = (who or "them").split()[0]
        await notify.notify(
            f"💬 You've already answered {first} yourself — what I found, in case "
            f"it helps:\n\n{(analysis or reply).strip()[:1500]}",
            "answer", urgency="ambient", considered=True)
        if thread:
            threads.update(thread, status="awaiting_arun")
        store.record_outcome("answer", "he answered", subject=str(task_id or ""),
                             detail=f"{who}: {need}"[:200])
        return True
    reply = writing.as_him(reply)
    intent = {"kind": "send", "what": reply.strip(), "to": chat, "channel": "teams",
              "to_group": bool(group), "task_id": task_id, "thread": thread,
              "who": who, "need": need, "analysis": analysis, "note": note,
              "lead": lead, "source_text": source_text, "ask_kind": ask_kind,
              "_at": time.time()}
    if origin:
        intent["review_origin"] = origin
        intent["review_auto_ok"] = not more
    if thread:
        threads.update(thread, status="awaiting_arun")
    if await _deliver_approved_review(intent):
        return True
    if not more and await _deliver_automatic_reply(intent):
        return True
    if _blocked(cid):
        _enqueue({"type": "answer", **intent})
        return True
    await _show(cid, intent)
    return True


async def _deliver_automatic_reply(intent: dict) -> bool:
    from . import authority, chat_watch, notify
    target = intent.get("to", "")
    if not authority.auto_reply_to(target, group=bool(intent.get("to_group"))):
        return False
    if await chat_watch.he_replied_since(target, chat_watch.their_last(target)):
        return False
    try:
        line = await authority.send_reply(target, intent["what"])
    except Exception as exc:
        store.record_outcome("answer", "auto reply failed",
                             subject=str(intent.get("task_id") or ""),
                             detail=f"{target}: {type(exc).__name__}: {exc}"[:200])
        intent["note"] = "Automatic send failed; delivery was not confirmed. Do not resend blindly."
        return False
    sent({**intent, "review_origin": None})
    analysis = (intent.get("analysis") or "").strip()[:1200]
    await notify.notify(f"{line}" + (f"\n\n{analysis}" if analysis else "")
                        + "\n\n> " + intent["what"].replace("\n", "\n> "),
                        "answer", urgency="ambient", considered=True)
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
                     words: str = "", group: bool = False, source_text: str = "") -> bool:
    """A colleague wants code changed: ask him whether to plan it — now, or
    after whatever he is already deciding. True if shown now.

    `words` is what they actually wrote, quotes included. The plan is briefed
    from it, not from a one-line summary: "add the field Sonal sent, to whatever
    he's replying on" named neither the field nor the PR (1 Oct)."""
    from . import threads
    item = {"type": "plan", "who": who, "chat": chat, "need": need,
            "summary": summary, "thread": thread, "words": (words or "")[:2500],
            "group": group, "source_text": source_text}
    if not origin_allowed({"to_group": group, "to": chat, "who": who,
                           "source_text": source_text}):
        _record_rejected_origin(item)
        return False
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
    if not origin_allowed({"to_group": item.get("group"), "to": item.get("chat"),
                           "who": item.get("who"), "source_text": item.get("source_text")}):
        _record_rejected_origin(item)
        return
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
    if not origin_allowed(intent):
        _record_rejected_origin(intent)
        return
    intent = {**intent, "type": "answer", "_shown": time.time(),
              "_showings": int(intent.get("_showings") or 0) + 1}
    loop.stage(cid, intent)
    waiting = len(_load_queue())
    note = intent.get("note", "")
    if intent["_showings"] > 1:
        note = (note + "\n" if note else "") + "(Asking again — this one is still unanswered.)"
    from . import senior
    if not intent.get("to_group") and senior.is_senior(intent.get("to") or ""):
        note = (note + "\n" if note else "") + \
            "🔒 Manager and above — this goes only on your *send*. Check every word."
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


def origin_allowed(intent: dict) -> bool:
    """A bot-generated group answer must still come from an addressed message."""
    if not intent.get("to_group"):
        return True
    from . import chat_watch, policy
    source_text = intent.get("source_text")
    if not isinstance(source_text, str) or not chat_watch.mentions_him(source_text):
        return False
    kind = intent.get("ask_kind")
    return not kind or policy.check("investigate", kind).ok


def _record_rejected_origin(intent: dict, task_id: int | None = None) -> None:
    store.record_outcome("answer", "origin rejected",
                         subject=str(task_id or intent.get("task_id") or ""),
                         detail=f"{intent.get('who', '')}: {intent.get('to', intent.get('chat', ''))}"[:200])


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
    queue = _load_queue()
    for it in queue:
        if it.get("type") == "answer" and not origin_allowed(it):
            _record_rejected_origin(it)
            continue
        old = now - float(it.get("_at") or 0) > STALE_SECONDS
        ignored = int(it.get("_showings") or 0) >= MAX_SHOWINGS
        (gone if old or ignored else keep).append(it)
    if keep != queue:
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
    while q := _load_queue():
        item = q.pop(0)
        store.kv_set(_QUEUE, json.dumps(q))
        if item.get("type") == "plan":
            if not origin_allowed({"to_group": item.get("group"), "to": item.get("chat"),
                                   "who": item.get("who"),
                                   "source_text": item.get("source_text")}):
                _record_rejected_origin(item)
                continue
            await _show_plan(item)
            return True
        if not origin_allowed(item):
            _record_rejected_origin(item)
            continue
        origin = item.get("review_origin")
        if origin and not await review_is_current(origin):
            store.record_outcome("answer", "stale review", subject=str(item.get("task_id") or ""),
                                 detail=origin.get("ref", ""))
            await notify.notify(f"🔄 {origin['ref']} changed. I dropped the queued old "
                                "reply; a new review is needed.",
                                "answer", urgency="direct", considered=True)
            continue
        if await _deliver_approved_review(item):
            continue
        await _show(cid, item)
        return True
    return False


def sent(staged: dict) -> None:
    """The reply went out: the conversation now has Asta's words in it."""
    from . import threads
    origin = (staged or {}).get("review_origin")
    if origin and staged.get("type") == "answer" and not staged.get("to_group"):
        store.kv_set(_approved_review_key(origin), json.dumps({
            "what": staged.get("what", ""), "chat": staged.get("to", ""),
            "who": staged.get("who", ""), "at": time.time()}))
    tid = (staged or {}).get("thread") or ""
    if tid and threads.get(tid):
        threads.update(tid, asta_spoke=1, status="answered")
        threads._record(tid, "answered", (staged.get("what") or "")[:160])


def _approved_review_key(origin: dict) -> str:
    return "approved_review_reply:" + hashlib.sha256(origin["revision"].encode()).hexdigest()[:24]


async def _deliver_approved_review(intent: dict) -> bool:
    """Reuse only the exact wording he previously approved for this PR revision."""
    origin = intent.get("review_origin")
    if not origin or not intent.get("review_auto_ok") or intent.get("to_group"):
        return False
    from . import senior
    if senior.is_senior(intent.get("to", "")):
        return False
    try:
        approved = json.loads(store.kv_get(_approved_review_key(origin)) or "{}")
    except ValueError:
        return False
    reply = intent.get("what", "")
    first = (approved.get("who") or "").split()[:1]
    if (not approved or approved.get("chat") == intent.get("to")
            or approved.get("what") != reply or "?" in reply
            or (first and re.search(rf"\b{re.escape(first[0])}\b", reply, re.I))):
        return False
    if not await review_is_current(origin):
        return False
    from . import chat_watch, ops, notify
    if await chat_watch.he_replied_since(intent["to"], chat_watch.their_last(intent["to"])):
        return False
    try:
        line = await ops.run({"name": "teams_send",
                              "args": {"to": intent["to"], "text": reply, "to_group": False}})
    except Exception as exc:
        store.record_outcome("answer", "approved reuse failed",
                             subject=str(intent.get("task_id") or ""),
                             detail=f"{intent['to']}: {type(exc).__name__}: {exc}"[:200])
        return False
    if line.startswith("⛔ Not done"):
        store.record_outcome("answer", "approved reuse blocked",
                             subject=str(intent.get("task_id") or ""), detail=line[:200])
        return False
    sent({**intent, "review_origin": None})
    store.record_outcome("answer", "approved review reused",
                         subject=str(intent.get("task_id") or ""), detail=intent["to"][:200])
    await notify.notify(f"{line} (same approved review of {origin['ref']}).",
                        "answer", urgency="ambient", considered=True)
    return True


async def present_task(task_id: int, t: dict, result: str) -> bool:
    """A finished investigation that a colleague is waiting on. False: not one."""
    try:
        meta = json.loads(store.kv_get(f"answer_meta:{task_id}") or "{}")
    except ValueError:
        meta = {}
    if not meta:
        return False
    recipients = [meta, *_review_waiters(task_id)]
    eligible = []
    for recipient in recipients:
        source = {"to_group": recipient.get("group"),
                  "to": recipient.get("chat") or t.get("teams_chat", ""),
                  "who": recipient.get("who", ""),
                  "source_text": recipient.get("source_text"),
                  "ask_kind": recipient.get("ask_kind")}
        if origin_allowed(source):
            eligible.append(recipient)
        else:
            _record_rejected_origin(source, task_id)
    if not eligible:
        return True  # No generic task-complete notification for an unaddressed group.
    origin = _review_origin(task_id)
    if origin and not await review_is_current(origin):
        from . import notify
        await notify.notify(f"🔄 {origin['ref']} changed while I was reviewing it. "
                            "I won't present or send the old findings; a new review is needed.",
                            "answer", urgency="direct", considered=True)
        store.record_outcome("answer", "stale review", subject=str(task_id),
                             detail=origin["ref"])
        return True
    analysis, reply = split(result)
    if not reply:
        return False
    # A question back to them goes straight to them — clarifying is his rule
    # (29 Sep: "talk to them directly, get what they want"); he hears what was
    # asked. Answers still wait for his "send".
    chat = meta.get("chat") or t.get("teams_chat", "")
    if meta in eligible and not meta.get("group") and chat and _is_clarifying(reply, meta.get("who", "")):
        from . import chat_watch, notify
        if await chat_watch._say(chat, reply.strip(), group=False,
                                 since=chat_watch.their_last(chat)):
            first = (meta.get("who") or "them").split()[0]
            await notify.notify(f"❓ Asked {first}: “{reply.strip()}”\n\n{analysis}".strip(),
                                "answer", urgency="direct", considered=True)
            return True
    shown = False
    for recipient in eligible:
        shown = await present(who=recipient.get("who", ""), need=recipient.get("need", ""),
                              chat=recipient.get("chat") or t.get("teams_chat", ""),
                              group=bool(recipient.get("group")), analysis=analysis, reply=reply,
                              task_id=task_id, thread=recipient.get("thread", ""),
                              source_text=recipient.get("source_text") or "",
                              ask_kind=recipient.get("ask_kind") or "") or shown
    return shown


#: How long after an answer is finished a further message still belongs to it.
FOLLOWUP_SECONDS = 45 * 60


_CORRECTION = re.compile(
    r"^\s*(?:no[,!.\s]+)?(?:(?:that's|that is|this is|you're|you are|your answer is|"
    r"your reply is|asta[,!.\s]+)\s+)?(?:not correct|incorrect|wrong|not right|"
    r"mistaken)\b|^\s*(?:no[,!.\s]+)?(?:not that one|you (?:checked|picked|"
    r"looked at) the wrong\b)", re.I)


def corrected_task(thread: str, text: str, now: float | None = None) -> dict | None:
    """The recent finished answer a colleague explicitly says was wrong."""
    from . import chat_watch
    if not thread or not _CORRECTION.match(chat_watch.clean_message(text)):
        return None
    now = time.time() if now is None else now
    for task in store.list_tasks(limit=40):
        if task["status"] != "done" or not task.get("result"):
            continue
        if now - float(task.get("finished_at") or 0) > FOLLOWUP_SECONDS:
            continue
        if _meta(task["id"]).get("thread") == thread:
            return task
    return None


def invalidate_answer(task_id: int) -> bool:
    """A rejected draft must not remain sendable while its answer is revisited."""
    from . import loop
    cid = phone_conversation()
    staged = loop.awaiting(cid) if cid else None
    if staged and staged.get("task_id") == task_id:
        loop.clear_awaiting(cid)
        store.record_outcome("answer", "correction withdrew draft", subject=str(task_id))
    queue = _load_queue()
    keep = [item for item in queue if item.get("task_id") != task_id]
    if len(keep) != len(queue):
        store.kv_set(_QUEUE, json.dumps(keep))
        store.record_outcome("answer", "correction withdrew queued", subject=str(task_id))
    return bool(staged and staged.get("task_id") == task_id) or len(keep) != len(queue)


def _meta(task_id: int) -> dict:
    try:
        d = json.loads(store.kv_get(f"answer_meta:{task_id}") or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


#: How long the same answer for the same person is "already put to him".
REPEAT_SECONDS = 12 * 3600


def _gist(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())[:200]


def _already_put(chat: str, need: str, reply: str, now: float | None = None) -> bool:
    """Was this same answer — same person, same ask AND reply — already
    shown to him recently? Records it when not."""
    import hashlib
    now = time.time() if now is None else now
    key = f"answer_put:{(chat or '').strip().lower()[:60]}"
    try:
        seen = [x for x in json.loads(store.kv_get(key) or "[]")
                if now - float(x.get("at", 0)) < REPEAT_SECONDS]
    except (ValueError, TypeError, AttributeError):
        seen = []
    mark = hashlib.sha1(f"{_gist(need)}|{_gist(reply)}".encode()).hexdigest()[:16]
    if any(mark == x.get("h") for x in seen):
        return True
    seen.append({"h": mark, "at": now})
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


def _is_clarifying(reply: str, who: str) -> bool:
    """A short question that asks them for something, promising nothing."""
    from . import understand
    r = " ".join((reply or "").split())
    return bool(r) and r.count("?") >= 1 and len(r) <= 260 \
        and bool(understand.safe_question(r if r.endswith("?") else r.rsplit("?", 1)[0] + "?", who))


def remember_meta(task_id: int, *, who: str, need: str, chat: str, group: bool,
                  thread: str, source_text: str = "", kind: str = "") -> None:
    store.kv_set(f"answer_meta:{task_id}", json.dumps(
        {"who": who, "need": need, "chat": chat, "group": group, "thread": thread,
         "source_text": source_text, "ask_kind": kind}))


def remember_waiter(task_id: int, *, who: str, need: str, chat: str, group: bool,
                    thread: str, source_text: str = "", kind: str = "") -> None:
    key = f"answer_waiters:{task_id}"
    waiters = _review_waiters(task_id)
    if not any(r["chat"] == chat and r["need"] == need for r in waiters):
        waiters.append({"who": who, "need": need, "chat": chat,
                        "group": group, "thread": thread, "source_text": source_text,
                        "ask_kind": kind})
        store.kv_set(key, json.dumps(waiters))


def _review_waiters(task_id: int) -> list[dict]:
    try:
        waiters = json.loads(store.kv_get(f"answer_waiters:{task_id}") or "[]")
    except (ValueError, TypeError):
        return []
    return [w for w in waiters if isinstance(w, dict)] if isinstance(waiters, list) else []


def _review_origin(task_id: int | None) -> dict:
    if not task_id:
        return {}
    try:
        origin = json.loads(store.kv_get(f"review_origin:{task_id}") or "{}")
    except (ValueError, TypeError):
        return {}
    return origin if isinstance(origin, dict) else {}


async def review_is_current(origin: dict) -> bool:
    from . import review
    try:
        current, good = await review.revision(origin["ref"])
        return good and current == origin["revision"]
    except (RuntimeError, ValueError, KeyError) as exc:
        store.record_outcome("review", "verification failed",
                             subject=str(origin.get("ref", ""))[:80], detail=str(exc)[:200])
        return False
