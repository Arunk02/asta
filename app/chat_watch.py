"""Reading his actual chats, not just the mentions feed.

Arun, on why the Activity feed was always the wrong reader:

    "if they didnt tag , still if it one to one chat na , that message is for me
     correct , sometimes the first message they tag and second message they wont
     tag in both personal one to one chat as well as group chat this is basic
     thing"

He is right and it is basic. Teams' Activity feed lists mentions, replies,
reactions and invites. It never lists an ordinary message. So a 1:1 — where every
message is addressed to him by definition — was invisible unless somebody
@mentioned him inside his own DM, and the second message of any conversation was
invisible because nobody tags twice.

**Asta's high-water mark, not his read state.** What matters is whether ASTA has
processed a message, not whether Teams believes Arun has seen it. Keying off
Teams' unread styling would also mean anything he glanced at on his phone became
invisible here — the opposite of "irrespective im present or not". And in this
Teams build the rail carries no unread marker at all: `role="treeitem"` rows have
an empty aria-label, an empty data-tid, and hashed Fluent class names. Checked
against his live rail rather than assumed.

**The rail's ORDER is the cheap signal.** A chat with a new message jumps to the
top. Opening every conversation on every poll would be minutes of browser work on
a single-writer profile, competing with everything else for it; comparing the
order costs one DOM read. So only chats that moved up — plus whichever is
currently on top, where a second message in an already-top chat lands — are
opened at all. On a quiet poll nothing is opened.
"""

from __future__ import annotations

import asyncio
import json
import os

from . import store

#: How often to compare the rail. Cheaper than the activity poll: one DOM read,
#: and no chat is opened unless something moved.
POLL_SECONDS = float(os.environ.get("ASTA_CHATWATCH_SECONDS", "180"))

#: Chats opened per sweep, at most. Each one is a real browser navigation; ten
#: people pinging at once must not become ten page loads on a profile that
#: tolerates a single writer.
MAX_OPENS = int(os.environ.get("ASTA_CHATWATCH_MAX_OPENS", "3"))

#: Messages pulled per chat. Enough to cover a burst since the last poll without
#: paying for scrollback.
READ_LIMIT = int(os.environ.get("ASTA_CHATWATCH_READ", "12"))

_RAIL_KEY = "chatwatch_rail"

#: Rail rows that are not somebody talking to him.
#:
#: His self-chat renders as "Arunkumar K (You)" and is PINNED to the top, so it
#: was permanently the "always check the top row" candidate — spending one of the
#: three opens every sweep on a thread only he writes in, and hiding whatever
#: conversation was really newest behind it. Found live: the first sweep against
#: his real rail reported "nothing new" and was right for the wrong reason.
_SELF_MARK = "(you)"
_RAIL_FURNITURE = ("telikos - all teams",)


def is_furniture(name: str) -> bool:
    low = (name or "").strip().lower()
    return (not low) or low.endswith(_SELF_MARK) or low in _RAIL_FURNITURE


def enabled() -> bool:
    return os.environ.get("ASTA_CHATWATCH", "").strip() not in ("", "0", "false", "no")


def _seen_key(chat: str) -> str:
    return f"chatwatch_seen:{chat.strip().lower()[:60]}"


def moved_up(previous: list[str], current: list[str]) -> list[str]:
    """Chats that rose in the rail since last time — i.e. that had activity.

    A chat with a new message jumps toward the top, so a rise is the signal. The
    row currently on top always counts: a SECOND message in a chat already at
    position 0 moves nothing, and that is exactly the untagged follow-up this
    module exists for.
    """
    if not current:
        return []
    if not previous:
        return current[:1]          # first run: only the top, not a whole backlog
    was = {name: i for i, name in enumerate(previous)}
    risen = [name for i, name in enumerate(current)
             if name not in was or i < was[name]]
    top = current[0]
    return list(dict.fromkeys([top, *risen]))


def _mark_of(rows: list[dict]) -> str:
    """The high-water mark for a thread: the last message's stable key."""
    return (rows[-1].get("key") or "") if rows else ""


def unseen(chat: str, rows: list[dict]) -> list[dict]:
    """Messages in `rows` that Asta has not processed before.

    Keyed on the message key rather than a timestamp: `_msg_key` is stable and
    `sent_at` is legitimately None when Teams renders no machine-readable time,
    and a null timestamp must not silently mean "new every poll".
    """
    mark = store.kv_get(_seen_key(chat)) or ""
    if not mark:
        return rows[-1:]            # first sight of a thread: the latest only
    keys = [r.get("key") or "" for r in rows]
    if mark not in keys:
        return rows                 # the mark scrolled out of view — treat all as new
    return rows[keys.index(mark) + 1:]


def remember(chat: str, rows: list[dict]) -> None:
    mark = _mark_of(rows)
    if mark:
        store.kv_set(_seen_key(chat), mark)


def is_from_him(sender: str) -> bool:
    """His own messages are not things he was asked."""
    from . import meetings
    return meetings.speaker_is_arun(sender or "")


async def candidates() -> list[str]:
    """Rail names worth opening this sweep, newest activity first."""
    from . import teams_bridge
    async with teams_bridge.teams_page() as page:
        await teams_bridge.wait_for_rail(page)
        try:
            rows = await page.evaluate(teams_bridge._CHAT_ROWS)
        except Exception:
            return []
    current = [r.strip() for r in (rows or [])
               if r.strip() and r.strip().lower() not in teams_bridge._NOT_A_CHAT
               and not is_furniture(r)]
    try:
        previous = json.loads(store.kv_get(_RAIL_KEY) or "[]")
    except Exception:                                          # noqa: BLE001
        previous = []
    store.kv_set(_RAIL_KEY, json.dumps(current[:60]))
    return moved_up(previous, current)[:MAX_OPENS]


async def new_in(chat: str, advance: bool = True) -> list[dict]:
    """Messages in one chat that Asta has not processed, excluding his own.

    `advance=False` looks without consuming: "what's unread" is a question he can
    ask twice, and a read tool that marks things processed would make the second
    answer empty and the first unrepeatable.
    """
    from . import teams_bridge
    rows = await teams_bridge.read_history(chat, limit=READ_LIMIT, max_scrolls=0)
    fresh = unseen(chat, rows or [])
    if advance:
        remember(chat, rows or [])
    return [r for r in fresh if (r.get("text") or "").strip()
            and not is_from_him(r.get("sender", ""))]


async def pending() -> list[dict]:
    """Everything Asta has not processed yet, without marking any of it processed.

    Backs the `teams_unread` tool. Deliberately not the rail's unread styling:
    this Teams build exposes none (empty aria-label, empty data-tid, hashed Fluent
    class names — checked live), and keying off his read state would hide anything
    he glanced at on his phone, which is the opposite of what he asked for.
    """
    out: list[dict] = []
    for chat in await candidates():
        try:
            for m in await new_in(chat, advance=False):
                out.append({"chat": chat, "who": m.get("sender") or chat,
                            "text": (m.get("text") or "").strip()})
        except Exception:                                      # noqa: BLE001
            continue
    return out


async def sweep(notify=None) -> list[dict]:
    """One pass: what moved, what is new in it, judged and acted on.

    Returns the messages it handled, so a test can assert on the decision rather
    than on a notification having been sent.
    """
    from . import attention, responder, triage
    handled: list[dict] = []
    lines: list[str] = []
    started: list[str] = []
    for chat in await candidates():
        try:
            fresh = await new_in(chat)
        except Exception:                                      # noqa: BLE001
            continue                # one unreadable thread must not end the sweep
        for m in fresh:
            who = (m.get("sender") or chat).strip()
            text = (m.get("text") or "").strip()
            # A 1:1 message is addressed to him by the fact of being a 1:1. That
            # is the whole point: no tag required, and none expected on the
            # second message of any conversation.
            direct = who.lower() == chat.strip().lower()
            key = attention.key_for(f"{chat}:{text}")
            v = triage.classify(who, text, addressed=direct)
            pri, why, due = attention.rank(v.action, text, addressed=direct,
                                           key=key, who=who)
            if not attention.consider("teams-chat", key, who=who, what=v.one_line,
                                      why=why, priority=pri, due_at=due):
                continue
            handled.append({"chat": chat, "who": who, "text": text, "priority": pri})
            lines.append(f"{'🔴' if v.action else '·'} {chat} — {who}: {text[:120]}")
            task = responder.respond("teams-chat", who, text, priority=pri, key=key)
            if task:
                started.append(responder.line_for(task, who,
                                                  responder.what_it_asks(text)))
    if notify and (lines or started):
        body = "\n".join(lines + ([""] if lines and started else []) + started)
        await notify("💬 Teams\n" + body, "teams",
                     urgency="direct" if started or any(
                         h["priority"] is not None and h["priority"] <= attention.P_TODAY
                         for h in handled) else "ambient",
                     considered=True)
    return handled


async def watch_loop() -> None:
    """Poll the rail forever. Quiet when nothing moved."""
    from . import notify, teams_bridge, wake
    while True:
        await wake.sleep(POLL_SECONDS)
        if not (enabled() and teams_bridge.enabled() and teams_bridge.logged_in_once()
                and store.kv_get("teams_session_ok") != "0"):
            continue
        try:
            await sweep(notify.notify)
        except Exception as exc:                               # noqa: BLE001
            from . import quiet
            quiet.note("chatwatch.sweep", exc)
        await asyncio.sleep(0)
