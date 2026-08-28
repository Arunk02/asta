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
import re

from . import store

#: How often to compare the rail. Cheaper than the activity poll: one DOM read,
#: and no chat is opened unless something moved.
POLL_SECONDS = float(os.environ.get("ASTA_CHATWATCH_SECONDS", "180"))

#: Conversations at the head of the list, read on EVERY sweep. These are the ones
#: with recent activity, so this is where a new message almost always is.
ALWAYS_TOP = int(os.environ.get("ASTA_CHATWATCH_TOP", "3"))

#: Plus this many from the tail, rotating, so every conversation is eventually
#: read even if Teams never reorders it.
#:
#: The first version read only what MOVED UP the rail, on the theory that a new
#: message pushes its chat toward the top. Arun called it: "why u watching via
#: notification , can't use playwright and open teams and fetch from there via
#: direct visible from there". He was right. Inferring activity from ordering
#: means anything the ordering does not reflect is never read at all — fourteen
#: hours, two conversations, and nothing anywhere saying so. Opening the chats and
#: reading them is the obvious correct thing; the only real question was cost, and
#: cost is answered by a bounded window rather than by a clever signal.
ROTATE = int(os.environ.get("ASTA_CHATWATCH_ROTATE", "2"))

#: Chats opened per sweep, at most. Each one is a real browser navigation on a
#: profile that tolerates a single writer.
MAX_OPENS = int(os.environ.get("ASTA_CHATWATCH_MAX_OPENS", "5"))

_CURSOR_KEY = "chatwatch_cursor"

#: Messages pulled per chat. Enough to cover a burst since the last poll without
#: paying for scrollback.
READ_LIMIT = int(os.environ.get("ASTA_CHATWATCH_READ", "12"))

_RAIL_KEY = "chatwatch_rail"

#: How long a group conversation stays "his" after he is tagged in it.
#:
#: "need my attentation for group chats that is valid my name tagged at first,
#: follow up convo with or without tagging as well.. but it should aware and
#: follow up post the first tag message as well".
#:
#: A tag in a group opens a thread that belongs to him; the replies that follow it
#: do not get tagged again, and they are the substance. So the tag starts a window
#: rather than marking one message. A 1:1 needs none of this — every message there
#: is his by construction.
#: Shortened from 12h after it fired live. A tag at breakfast should not make the
#: room his until the evening; a conversation that resumes tomorrow gets tagged
#: again, because that is what people do.
ENGAGED_HOURS = float(os.environ.get("ASTA_GROUP_FOLLOW_HOURS", "2"))

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


def pick(current: list[str], cursor: int) -> tuple[list[str], int]:
    """Which conversations to open this sweep, and where the rotation got to.

    The head of the list every time — that is where a new message lands — plus a
    moving window through the tail so nothing is permanently unread. No dependence
    on Teams reordering anything, on unread styling, or on the Activity feed.
    """
    if not current:
        return [], cursor
    top = current[:ALWAYS_TOP]
    tail = current[ALWAYS_TOP:]
    if not tail:
        return top[:MAX_OPENS], 0
    start = cursor % len(tail)
    window = [tail[(start + i) % len(tail)] for i in range(min(ROTATE, len(tail)))]
    return list(dict.fromkeys(top + window))[:MAX_OPENS], start + len(window)


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


def _engaged_key(chat: str) -> str:
    return f"chatwatch_tagged:{chat.strip().lower()[:60]}"


def mentions_him(text: str) -> bool:
    """Is his name in this message? The signal that a group thread became his."""
    from . import meetings
    low = (text or "").lower()
    return any(n and n in low for n in meetings.HIS_NAMES)


def note_tagged(chat: str, now: float | None = None, by: str = "") -> None:
    """Remember WHEN he was pulled in, and by WHOM.

    Who matters as much as when. Vinish tagged him in a release channel and, for
    the next twelve hours, every message in the room reached his phone — Navya's
    schema question, Roshan's "22nd September ko release hai", and "Vinish Kumar
    what do you say", which is addressed to Vinish. Being pulled into a thread is
    not being subscribed to a room.
    """
    import time
    when = now if now is not None else time.time()
    store.kv_set(_engaged_key(chat), f"{when}|{(by or '').strip()}")


def engaged(chat: str, now: float | None = None) -> tuple[bool, str]:
    """(is this still a conversation he was pulled into, who pulled him in)."""
    import time
    raw = (store.kv_get(_engaged_key(chat)) or "").strip()
    if not raw:
        return False, ""
    stamp, _, by = raw.partition("|")
    try:
        when = float(stamp)
    except ValueError:
        return False, ""
    now = time.time() if now is None else now
    return ((now - when) < ENGAGED_HOURS * 3600), by


def addressed_to_him(chat: str, sender: str, text: str,
                     now: float | None = None) -> bool:
    """Does this message want something from Arun?

    Three rules, in his words:
      * a 1:1 always counts — "there no point whether they mention or not the
        message is for me only";
      * a group counts once his name is in it;
      * and thereafter, for a while, so does the conversation that follows —
        "follow up convo with or without tagging as well".
    """
    if (sender or "").strip().lower() == (chat or "").strip().lower():
        return True                         # a 1:1: the chat IS the person
    if mentions_him(text):
        note_tagged(chat, now, by=sender)
        return True
    open_window, by = engaged(chat, now)
    if not open_window:
        return False
    # Inside the window, the follow-up is what the person who pulled him in says
    # next — not everything the room says. Anything naming somebody ELSE is
    # theirs: "Vinish Kumar what do you say" is a question for Vinish.
    if names_someone_else(text, sender):
        return False
    return (sender or "").strip().lower() == (by or "").strip().lower()


# --- what he actually reads ---------------------------------------------------
#
# "what is this message what i will get know from these ? nothing proper"
#
# He was sent this, verbatim:
#
#     · Palikala Divya Maheswari — Palikala Divya Maheswari: Arunkumar K
#     28/08/2026 18:38
#     lets analyse on some idea and see how it going on weekend sunday and Monday
#     · Vinish Kumar — Vinish Kumar: https://maersk.service-now.com/now/platform-…
#
# Three faults in one line. The 1:1 names the person twice, because the chat and
# the sender are the same thing and both were printed. The body opens with
# "Arunkumar K / 28/08/2026 18:38", which is the quoted header Teams renders
# inside a REPLY — scraped along with the text, so the actual sentence starts on
# line three. And a bare URL says nothing at all about what it is.

#: The quote block Teams renders at the top of a REPLY, captured verbatim from a
#: real stored message:
#:
#:     Ashwin Kumar                 <- who is being quoted
#:     20/07/2026 11:14             <- when they said it
#:     Ayashkant - What is IP...    <- THEIR words
#:                                  <- blank line
#:     IP is specific integration…  <- what the sender actually typed
#:
#: The first version stripped only the name and the timestamp, which left the
#: QUOTED text as the message — so Arun was shown his own sentence under Divya's
#: name. Attributing one colleague's words to another is worse than the raw noise
#: it replaced, and he caught it immediately.
#: Measured against 200 real captured messages rather than guessed. 22 of them
#: carry a quote block; 21 open with it and 1 has it after the reply. The
#: timestamp is 12-hour with AM/PM far more often than 24-hour — the first
#: version matched only "18:38", so 21 of the 22 slipped straight through and
#: kept misattributing.
_REPLY_HEADER = re.compile(
    r"[^\n]{1,60}\n[ \t]*\d{1,2}/\d{1,2}/\d{2,4}[ ,]+\d{1,2}:\d{2}(?:\s*[AP]M)?[ \t]*\n",
    re.I)

#: Teams appends these to the scraped body; they are not what anyone said.
_TRAILING_NOISE = re.compile(
    r"\n+\s*\d*\s*(like|heart|laugh|surprised|sad|angry)\s+reactions?[^\n]*\Z", re.I)

_URL = re.compile(r"https?://\S+")


def clean_message(text: str, known: set[str] | None = None) -> str:
    """What the SENDER typed — never the message they were replying to.

    A quote appears in two places, both in his real data: at the START when
    replying, and AFTER the text when forwarding. Position decides which side to
    keep.

    The hard case is a leading quote with NO blank line before the reply, which is
    the common shape:

        Nakka Harika              <- who is quoted
        28/08/2026 12:39          <- when
        Swamy in vinish and urs team..     <- HER words
        Arrey I'm in multiple teams for Background support   <- his reply

    Nothing in the text marks where one ends and the other begins. But the quoted
    line is, by definition, a message that already exists in the thread — so
    `known` (the texts Asta has already stored for this chat) identifies it
    exactly. Where that is unavailable the last paragraph is used, because the
    reply is always last; that truncates a multi-paragraph reply, which is why the
    lookup is preferred and the fallback is only a fallback.
    """
    body = _TRAILING_NOISE.sub("", (text or "").strip())
    m = _REPLY_HEADER.search(body)
    if not m:
        return re.sub(r"\n{2,}", "\n", body).strip()
    if m.start() > 0:
        return re.sub(r"\n{2,}", "\n", body[:m.start()]).strip()   # a forward

    rest = body[m.end():]
    head, _, tail = rest.partition("\n\n")
    if tail.strip():
        return re.sub(r"\n{2,}", "\n", tail).strip()      # blank line separated them
    lines = [ln for ln in rest.split("\n") if ln.strip()]
    if known:
        # Drop the leading lines that are already messages in this thread — those
        # are the quote, whatever the spacing looks like.
        seen = {_norm(k) for k in known}
        while lines and _norm(lines[0]) in seen:
            lines.pop(0)
        if lines:
            return "\n".join(lines).strip()
        return ""
    return lines[-1].strip() if len(lines) > 1 else ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def describe_link(url: str) -> str:
    """What a bare link IS, in the words he would use to decide whether to open it.

    "Vinish Kumar: https://github.com/VinishKumar1/incident-copilot" told him
    nothing he could act on. The host and the path do.
    """
    from urllib.parse import urlparse
    u = urlparse(url)
    host = (u.netloc or "").replace("www.", "")
    tail = [p for p in (u.path or "").split("/") if p][:3]
    known = {"github.com": "GitHub", "maersk.service-now.com": "ServiceNow",
             "maersk-tools.atlassian.net": "Jira",
             "grafana-mcp.westeurope.azure.mop.maersk.io": "Grafana"}
    label = known.get(host, host)
    return f"{label}: {'/'.join(tail)}" if tail else label


def summarise(text: str, limit: int = 160, known: set[str] | None = None) -> str:
    """One readable line for a message — links named rather than pasted raw."""
    body = clean_message(text, known)
    if not body:
        # A reply whose own body did not survive the capture. Saying so is honest;
        # showing the quoted text would put someone else's words in their mouth.
        return "replied (couldn't read their text)"
    links = _URL.findall(body)
    without = _URL.sub("", body).strip(" -–—:\n\t")
    if links and not without:
        return "shared " + "; ".join(describe_link(u) for u in links[:2])
    if links:
        return f"{without[:limit]} · {describe_link(links[0])}"[:limit + 60]
    return body[:limit] + ("…" if len(body) > limit else "")


def render(chat: str, who: str, text: str, priority: int | None = None,
           known: set[str] | None = None) -> str:
    """The line on his phone. Names the person once."""
    mark = "🔴" if (priority is not None and priority <= 1) else "·"
    where = "" if (chat or "").strip().lower() == (who or "").strip().lower() \
        else f" in {chat}"
    return f"{mark} {who}{where}: {summarise(text, known=known)}"


def names_someone_else(text: str, sender: str) -> bool:
    """Does this message open by addressing a different person?

    A cheap, deliberately narrow test: a name at the very start is how people
    direct a message in a busy channel. Anything subtler is left alone, because
    over-filtering loses him a message and that is the failure that matters.
    """
    lead = (text or "").strip()[:40]
    if not lead or mentions_him(lead):
        return False
    # Two capitalised words at least — a FULL name, which is how people address
    # somebody in a busy channel. One word is far too loose: "Activity getting
    # missed in prod" opens with a capitalised noun and is not addressed to
    # anybody, and reading it as a name would have silenced a real report.
    m = re.match(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*[,:]?\s+\w", lead)
    if not m:
        return False
    named = m.group(1).strip().lower()
    return named != (sender or "").strip().lower()


def answered_by_him(chat: str, message: dict) -> bool:
    """Has Arun himself said something in this thread since this message?

    Read from what Asta has already stored, so it costs nothing. Anything without
    a timestamp is ignored rather than guessed at: an untimed row cannot be
    honestly claimed to come after anything.
    """
    when = message.get("sent_at")
    if not when:
        return False
    try:
        rows = store.teams_messages(chat=chat, limit=200)
    except Exception:                                          # noqa: BLE001
        return False
    return any(r.get("sent_at") and r["sent_at"] > when
               and is_from_him(r.get("sender", "")) for r in rows)


def is_from_him(sender: str) -> bool:
    """His own messages are not things he was asked."""
    from . import meetings
    return meetings.speaker_is_arun(sender or "")


#: Rows that only ever appear on the real chat list. Their presence is how we
#: know we are looking at the rail and not at something else.
_LIST_MARKERS = ("copilot", "mentions", "discover", "drafts", "saved")


def looks_like_the_chat_list(rows: list[str]) -> bool:
    """Is this the chat rail, or whatever the last operation left on screen?

    The Teams page is POOLED and shared with every other loop. `_find_chat` runs a
    search to open a thread, and a search replaces the rail with its results — so
    `[role="treeitem"]` then returns matches for whatever was last searched. That
    is not a hypothetical: the stored rail had "Divya" and "Palikala Divya
    Maheswari" at the top, which were results of a resolve call, and the watcher
    compared THAT against the previous order. Fourteen hours, two chats processed.

    The furniture at the head of the real list is the tell — search results never
    contain Copilot, Mentions or Discover.
    """
    head = " ".join(r.strip().lower() for r in rows[:8])
    return any(m in head for m in _LIST_MARKERS)


async def candidates() -> list[str]:
    """Rail names worth opening this sweep, newest activity first."""
    from . import teams_bridge
    async with teams_bridge.teams_page() as page:
        await teams_bridge.wait_for_rail(page)
        try:
            rows = await page.evaluate(teams_bridge._CHAT_ROWS)
        except Exception:
            return []
        if not looks_like_the_chat_list(rows or []):
            # Someone left a search on the shared page. Go back to the chat list
            # rather than comparing an order that means nothing.
            try:
                await page.goto(teams_bridge.TEAMS_URL, wait_until="domcontentloaded",
                                timeout=60000)
                await teams_bridge.wait_for_rail(page)
                rows = await page.evaluate(teams_bridge._CHAT_ROWS)
            except Exception:
                return []
            if not looks_like_the_chat_list(rows or []):
                return []          # still not the list — report nothing, change nothing
    current = [r.strip() for r in (rows or [])
               if r.strip() and r.strip().lower() not in teams_bridge._NOT_A_CHAT
               and not is_furniture(r)]
    store.kv_set(_RAIL_KEY, json.dumps(current[:60]))
    try:
        cursor = int(store.kv_get(_CURSOR_KEY) or "0")
    except ValueError:
        cursor = 0
    chosen, cursor = pick(current, cursor)
    store.kv_set(_CURSOR_KEY, str(cursor))
    return chosen


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
    opened = failed = 0
    for chat in await candidates():
        opened += 1
        # What has already been said in this thread. A quoted line is by
        # definition one of these, which is how the quote is told from the reply
        # when Teams puts no blank line between them.
        try:
            known = {(r.get("text") or "") for r in store.teams_messages(chat=chat, limit=300)}
        except Exception:                                      # noqa: BLE001
            known = set()
        try:
            fresh = await new_in(chat)
        except Exception:                                      # noqa: BLE001
            failed += 1
            continue                # one unreadable thread must not end the sweep
        for m in fresh:
            who = (m.get("sender") or chat).strip()
            text = (m.get("text") or "").strip()
            # 1:1 always; a group once he is tagged, and for a window after —
            # the replies that follow a tag are never tagged again.
            direct = addressed_to_him(chat, who, text)
            key = attention.key_for(f"{chat}:{text}")
            v = triage.classify(who, text, addressed=direct)
            pri, why, due = attention.rank(v.action, text, addressed=direct,
                                           key=key, who=who)
            if not attention.consider("teams-chat", key, who=who, what=v.one_line,
                                      why=why, priority=pri, due_at=due):
                continue
            # Recorded above, pushed only if it is HIS. Reading every conversation
            # is right; forwarding every conversation is not. Without this gate a
            # release-triage channel sent him "Shall we join here now?" and "Hi
            # Sumith just wanted to check what we have concluded" — a standing
            # group discussion between other people, none of it his, delivered to
            # his phone. `direct` was computed here and then never used.
            if not direct:
                continue
            handled.append({"chat": chat, "who": who, "text": text, "priority": pri})
            lines.append(render(chat, who, text, pri, known=known))
            # He has already dealt with it. "i have already shared na the
            # analysis then why again it doing" — Vinish's list of production
            # issues was investigated by a background task while Arun's own answer
            # was already sitting in the thread above it. A reply of his, later
            # than the ask, is the clearest possible signal that it is handled.
            if answered_by_him(chat, m):
                continue
            task = responder.respond("teams-chat", who, text, priority=pri, key=key,
                                     sent_at=m.get("sent_at"))
            if task:
                started.append(responder.line_for(task, who,
                                                  responder.what_it_asks(text)))
    # Every thread failing looks exactly like a quiet morning, and that is how
    # this ran for fourteen hours having read two chats while saying nothing. The
    # sweep now records its own health so `attention.stale_sources` can notice.
    if opened and failed >= opened:
        attention.note_scrape_error(
            "teams-chat", RuntimeError(f"all {opened} chat(s) failed to open"))
    elif opened:
        attention.note_scrape("teams-chat")
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
