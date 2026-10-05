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
import contextlib
import json
import os
import re

from . import store

#: How often to compare the rail. Cheaper than the activity poll: one DOM read,
#: and no chat is opened unless something moved.
#: How often to look. 180 put the modal message 1-3 minutes behind before the
#: sweep even started; 60 is the difference between "he told me" and "I saw it".
POLL_SECONDS = float(os.environ.get("ASTA_CHATWATCH_SECONDS", "60"))
#: The least rest between two sweeps, however long the last one took.
MIN_GAP_SECONDS = 5.0

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
MAX_OPENS = int(os.environ.get("ASTA_CHATWATCH_MAX_OPENS", "10"))

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
_RAIL_FURNITURE = ("telikos - all teams", "followed threads")

#: Rail names that search could not open — channels and communities sit on the
#: same rail as chats and are not reachable as one. Skipped for a day rather
#: than tried (and failed) on every sweep.
_UNOPENABLE_KEY = "chatwatch_unopenable:"
#: An hour, not a day: a chat that could not be opened at 12:14 was still
#: being written in at 13:00, unread (1 Oct). And a chat that moves on the rail
#: is tried again at once — see `candidates`.
UNOPENABLE_SECONDS = 3600


def unopenable(name: str) -> bool:
    import time as _time
    try:
        at = float(store.kv_get(_UNOPENABLE_KEY + name.strip().lower()[:80]) or 0)
    except ValueError:
        at = 0.0
    return _time.time() - at < UNOPENABLE_SECONDS


def note_unopenable(name: str) -> None:
    import time as _time
    store.kv_set(_UNOPENABLE_KEY + name.strip().lower()[:80], str(_time.time()))


def _he_had_the_last_word(chat: str, since: float) -> bool:
    """Did he write in this chat after `since` — read there, or sent by Asta for him?"""
    try:
        from . import teams_bridge
        if any(at > since and (sent or "").strip().lower() == (chat or "").strip().lower()
               for at, sent in teams_bridge.SENT):
            return True
    except Exception:                                          # noqa: BLE001
        pass
    try:
        rows = store.teams_messages(chat=chat, since=since, limit=50)
    except Exception:                                          # noqa: BLE001
        return False
    rows = [r for r in rows if r.get("sent_at") and float(r["sent_at"]) > since]
    return bool(rows) and is_from_him(max(rows, key=lambda r: float(r["sent_at"])).get("sender", ""))


def open_with_him(now: float | None = None, hours: float = 12) -> list[str]:
    """Today's conversations that are not closed, newest first, one line each —
    and any chat on his rail that could not be opened, said plainly."""
    import time as _time
    now = _time.time() if now is None else now
    lines: list[str] = []
    try:
        with store._connect() as c:
            rows = c.execute(
                "SELECT counterpart, status, need, summary, last_activity, chat FROM conv_threads "
                "WHERE closed_at IS NULL AND last_activity > ? ORDER BY last_activity DESC LIMIT 12",
                (now - hours * 3600,)).fetchall()
    except Exception:                                          # noqa: BLE001
        rows = []
    for r in rows:
        who, status, need, summary, at = r[0], r[1], (r[2] or "").strip(), (r[3] or "").strip(), r[4]
        if _he_had_the_last_word(r[5] or who, float(at or 0)):
            # He (or Asta in his name) answered after it — 2 Oct: "Vinish suggested
            # tomorrow 10-11" was still in the briefing an hour after "bro I'm going
            # out tmrw" went to Vinish.
            continue
        when = _time.strftime("%H:%M", _time.localtime(float(at or now)))
        what = (f"waiting on you: {need}" if status == "awaiting_arun" and need
                else need or summary[:160] or "open")
        lines.append(f"• {who} ({when}) — {what}")
    try:
        rail = json.loads(store.kv_get("chatwatch_rail") or "[]")[:8]
    except ValueError:
        rail = []
    blind = [n for n in rail if isinstance(n, str) and unopenable(n) and not is_furniture(n)]
    if blind:
        lines.append("⚠️ Active on your rail but I could not open: " + ", ".join(blind)
                     + " — check those yourself.")
    return lines


def is_furniture(name: str) -> bool:
    low = (name or "").strip().lower()
    return (not low) or low.endswith(_SELF_MARK) or low in _RAIL_FURNITURE


def enabled() -> bool:
    return os.environ.get("ASTA_CHATWATCH", "1").strip() not in ("", "0", "false", "no")


def _seen_key(chat: str) -> str:
    return f"chatwatch_seen:{chat.strip().lower()[:60]}"


def pick(current: list[str], cursor: int,
         previous: list[str] | None = None) -> tuple[list[str], int]:
    """Which conversations to open this sweep, and where the rotation got to.

    Three tiers, in this order: anything that MOVED UP since last sweep, then the
    head of the list, then a moving window through the tail so nothing is
    permanently unread.

    The first tier is the fix for how late he heard about things. `moved_up` has
    always existed — a chat with a new message jumps toward the top, and a rise
    is the signal — and `pick` never asked it. So a message in a thread below the
    head waited for the rotation to come round: with ROTATE=2 over twenty
    threads, up to nine sweeps. Measured over a week of real traffic, 433 of
    2,743 messages arrived more than fifteen minutes late and 24% took more than
    five. Opening what just changed is what that number was waiting for.

    Still bounded by MAX_OPENS, and the head still always gets a look, so a burst
    of activity in one room cannot starve the rest.
    """
    if not current:
        return [], cursor
    active = [c for c in moved_up(previous or [], current) if c in current]
    top = current[:ALWAYS_TOP]
    tail = [c for c in current[ALWAYS_TOP:] if c not in active]
    if not tail:
        return list(dict.fromkeys(active + top))[:MAX_OPENS], 0
    start = cursor % len(tail)
    window = [tail[(start + i) % len(tail)] for i in range(min(ROTATE, len(tail)))]
    return list(dict.fromkeys(active + top + window))[:MAX_OPENS], start + len(window)


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
    if mark in keys:
        return rows[keys.index(mark) + 1:]
    # The mark is not among what was read. A message's key includes its text,
    # so a reaction added since ("1 Like reaction.") changes it — and treating
    # the whole thread as new then re-read the same Team Booking messages every
    # few minutes all evening, each time through the model (30 Sep). Newer than
    # the newest message already processed is what "new" means.
    try:
        upto = float(store.kv_get(_seen_at_key(chat)) or 0)
    except ValueError:
        upto = 0.0
    if upto:
        return [r for r in rows if float(r.get("sent_at") or 0) > upto]
    return rows                     # no record at all: as before


def _identity(row: dict) -> tuple:
    """Who, when and what — without the reaction counts Teams appends."""
    text = _REACTION_TAIL.sub("", (row.get("text") or "")).strip()
    text = re.sub(r"\s*\d+\s+[^\n]{1,40}?\breactions?\.?\s*$", "", text, flags=re.I)
    when = row.get("sent_at")
    return ((row.get("sender") or "").strip().lower(),
            int(float(when)) // 60 if when else None,
            " ".join(text.split()).lower()[:300])


def _seen_at_key(chat: str) -> str:
    return f"chatwatch_seen_at:{chat.strip().lower()[:60]}"


def _done_key(chat: str) -> str:
    return f"chatwatch_done:{chat.strip().lower()[:60]}"


#: How many processed messages a chat remembers — far more than one read returns.
DONE_MAX = 400


def _ident_hash(row: dict) -> str:
    import hashlib
    return hashlib.sha1(repr(_identity(row)).encode()).hexdigest()[:16]


def processed(chat: str) -> set[str]:
    """Messages the SWEEP has handled in this chat — never what some other reader
    merely stored. A chat with no record yet counts what is older than its
    high-water time, which only the sweep advances."""
    try:
        got = json.loads(store.kv_get(_done_key(chat)) or "[]")
        if got:
            return set(got)
    except (ValueError, TypeError):
        pass
    try:
        upto = float(store.kv_get(_seen_at_key(chat)) or 0)
    except ValueError:
        upto = 0.0
    if not upto:
        return set()
    try:
        rows = store.teams_messages(chat=chat, since=upto - 2 * 86400, limit=3000)
    except Exception:                                          # noqa: BLE001
        return set()
    return {_ident_hash(r) for r in rows if float(r.get("sent_at") or 0) <= upto}


def mark_processed(chat: str, rows: list[dict]) -> None:
    try:
        old = json.loads(store.kv_get(_done_key(chat)) or "[]")
    except (ValueError, TypeError):
        old = []
    new = [h for h in (_ident_hash(r) for r in rows) if h not in set(old)]
    store.kv_set(_done_key(chat), json.dumps((old + new)[-DONE_MAX:]))


def remember(chat: str, rows: list[dict]) -> None:
    mark = _mark_of(rows)
    if mark:
        store.kv_set(_seen_key(chat), mark)
    times = [float(r.get("sent_at") or 0) for r in rows if r.get("sent_at")]
    if times:
        try:
            before = float(store.kv_get(_seen_at_key(chat)) or 0)
        except ValueError:
            before = 0.0
        store.kv_set(_seen_at_key(chat), str(max(before, max(times))))


def _engaged_key(chat: str) -> str:
    return f"chatwatch_tagged:{chat.strip().lower()[:60]}"


def mentions_him(text: str) -> bool:
    """Is his name in this message? The signal that a group thread became his."""
    from . import meetings
    low = (text or "").lower()
    return any(n and n in low for n in meetings.HIS_NAMES)


def note_tagged(chat: str, now: float | None = None, by: str = "") -> None:
    """Remember WHEN he was pulled in, and by WHOM.

    Who matters as much as when. Alex tagged him in a release channel and, for
    the next twelve hours, every message in the room reached his phone — Peyton's
    schema question, Hayden's "22nd September ko release hai", and "Alex Kumar
    what do you say", which is addressed to Alex. Being pulled into a thread is
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
    # theirs: "Alex Kumar what do you say" is a question for Alex.
    if names_someone_else(text, sender):
        return False
    return (sender or "").strip().lower() == (by or "").strip().lower()


# --- what he actually reads ---------------------------------------------------
#
# "what is this message what i will get know from these ? nothing proper"
#
# He was sent this, verbatim:
#
#     · Stone Blake Rivers — Stone Blake Rivers: Arunkumar K
#     28/08/2026 18:38
#     lets analyse on some idea and see how it going on weekend sunday and Monday
#     · Alex Kumar — Alex Kumar: https://maersk.service-now.com/now/platform-…
#
# Three faults in one line. The 1:1 names the person twice, because the chat and
# the sender are the same thing and both were printed. The body opens with
# "Arunkumar K / 28/08/2026 18:38", which is the quoted header Teams renders
# inside a REPLY — scraped along with the text, so the actual sentence starts on
# line three. And a bare URL says nothing at all about what it is.

#: The quote block Teams renders at the top of a REPLY, captured verbatim from a
#: real stored message:
#:
#:     Oakley Kumar                 <- who is being quoted
#:     20/07/2026 11:14             <- when they said it
#:     Reese - What is IP...    <- THEIR words
#:                                  <- blank line
#:     IP is specific integration…  <- what the sender actually typed
#:
#: The first version stripped only the name and the timestamp, which left the
#: QUOTED text as the message — so Arun was shown his own sentence under Blake's
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

        Harini S              <- who is quoted
        28/08/2026 12:39          <- when
        Brooks in alex and urs team..     <- HER words
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

    "Alex Kumar: https://github.com/example-dev/incident-copilot" told him
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


def as_read(text: str, known: set[str] | None = None) -> str:
    """A message as the reader should see it: what the sender typed, with a
    quoted earlier message taken out and noted. "Arunkumar K 25/09/2026 15:50
    Hi Sankalp could you please confirm… yes it is" is a reply ("yes it is"),
    not the question it quotes — read whole, it looked like a new ask."""
    raw = (text or "").strip()
    if not _REPLY_HEADER.search(raw):
        return raw
    body = clean_message(raw, known)
    if not body:
        return raw
    # What they quoted is kept, LABELLED — it is often the whole point. 1 Oct:
    # Vinish's "can you please add this field also?" quoted Sonal's code (the
    # field itself), and "This PR, bro" quoted Arun's PR 1429 link. With the
    # quotes dropped, Asta knew neither the field nor the PR, guessed PR 1252,
    # and went round in circles for twenty minutes.
    quoted = " ".join(raw.replace(body, " ").split()) if body in raw else ""
    line = f"{body} (replying to an earlier message)"
    return f"{line}\n  ↳ quoting: {quoted[:500]}" if quoted else line


def summarise(text: str, limit: int = 160, known: set[str] | None = None) -> str:
    """One readable line for a message — links named rather than pasted raw."""
    body = clean_message(without_image_paths(text), known)
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
    from . import attention
    mark = attention.marker(priority)
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
    if m:
        named = m.group(1).strip().lower()
        return named != (sender or "").strip().lower()
    # A SINGLE capitalised word, but only when it is a person Asta has actually
    # seen him talk to. Grammar alone is far too loose here — "Activity getting
    # missed in prod" opens with a capitalised noun and is addressed to nobody —
    # so this asks the evidence instead: is that a colleague's name?
    #
    # "Priya Nair: Rahul lets take the billtoparty change tomorrow" reached his
    # chase list because the two-word rule could not see a one-word address, and
    # Rahul is unmistakably who that sentence is for.
    one = re.match(r"^([A-Z][a-z]{2,20})\b[,:]?\s+\w", lead)
    if not one:
        return False
    first = one.group(1).strip().lower()
    if first == (sender or "").strip().lower().split(" ")[0]:
        return False
    return any(first == n.strip().lower().split(" ")[0]
               for n in _people_he_talks_to() if n)


def _people_he_talks_to() -> list[str]:
    """Every person Asta has seen — thread names AND senders inside them.

    Senders matter as much as threads: "Rahul Verma" has never been a 1:1 in
    his rail, he only ever speaks inside group chats. Thread names alone could
    not see him, so "Rahul lets take the billtoparty change tomorrow" read as a
    sentence rather than as a message for Rahul.
    """
    from . import contacts, store as _store
    names: list[str] = []
    try:
        names += contacts.known_threads()
    except Exception:                                          # noqa: BLE001
        pass
    try:
        names += [r["sender"] for r in _store.teams_senders_known()]
    except Exception:                                          # noqa: BLE001
        pass
    return names


#: The count Teams renders under a message that somebody reacted to — "1 Saluting
#: face reaction.", "2 Like reactions." — at the END of the scraped text. Anchored
#: there, so "what was the customer reaction?" is a sentence and not a reaction.
_REACTION_TAIL = re.compile(r"\n\s*\d+\s+[^\n]{1,40}?\breactions?\.?\s*$", re.I)


def answered_by_him(chat: str, message: dict) -> bool:
    """Has Arun himself dealt with this message?

    Read from what Asta has already stored, so it costs nothing. Anything without
    a timestamp is ignored rather than guessed at: an untimed row cannot be
    honestly claimed to come after anything.

    A REACTION is an answer too, in a 1:1. Navya's "Thank you" arrived with his
    salute already on it and was pushed to him in red, because only a text reply
    counted. A 1:1 has two people in it, so a reaction on her message is his. In a
    group anyone could have reacted, and it proves nothing.
    """
    if (message.get("sender") or chat or "").strip().lower() == (chat or "").strip().lower() \
            and _REACTION_TAIL.search(message.get("text") or ""):
        return True
    when = message.get("sent_at")
    if not when:
        return False
    try:
        # WINDOWED, not "the first 200". `teams_messages` orders oldest-first and
        # then applies the limit, so an unwindowed call on a long thread returns
        # the oldest 200 messages — and his reply, which is by definition the
        # newest thing in it, is never in the window. The Alex thread has 200+
        # stored messages, so this returned False for every question in it no
        # matter how promptly he answered, and the ledger kept chasing him about
        # conversations he had finished.
        rows = store.teams_messages(chat=chat, since=when, limit=200)
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
    is not a hypothetical: the stored rail had "Blake" and "Stone Blake
    Rivers" at the top, which were results of a resolve call, and the watcher
    compared THAT against the previous order. Fourteen hours, two chats processed.

    The furniture at the head of the real list is the tell — search results never
    contain Copilot, Mentions or Discover.
    """
    head = " ".join(r.strip().lower() for r in rows[:8])
    return any(m in head for m in _LIST_MARKERS)


async def _rail_rows(page) -> list[dict]:
    """The rail as {name, text}: the name and what Teams previews under it."""
    from . import teams_bridge
    try:
        full = await page.evaluate(teams_bridge._CHAT_ROWS_FULL)
        return [{"name": (r.get("name") or "").strip(), "text": (r.get("text") or "").strip()}
                for r in full or [] if (r.get("name") or "").strip()]
    except Exception:                                          # noqa: BLE001
        rows = await page.evaluate(teams_bridge._CHAT_ROWS)
        return [{"name": r.strip(), "text": ""} for r in rows or [] if r.strip()]


async def candidates() -> list[str]:
    """Rail names worth opening this sweep — every chat with something new,
    the most important first — then the head of the list and the rotation."""
    from . import teams_bridge
    async with teams_bridge.teams_page() as page:
        await teams_bridge.wait_for_rail(page)
        try:
            rail = await _rail_rows(page)
        except Exception:
            return []
        if not looks_like_the_chat_list([r["name"] for r in rail]):
            # Someone left a search on the shared page. Go back to the chat list
            # rather than comparing an order that means nothing.
            try:
                await page.goto(teams_bridge.TEAMS_URL, wait_until="domcontentloaded",
                                timeout=60000)
                await teams_bridge.wait_for_rail(page)
                rail = await _rail_rows(page)
            except Exception:
                return []
            if not looks_like_the_chat_list([r["name"] for r in rail]):
                return []          # still not the list — report nothing, change nothing
    # Chats only. Teams now lists every team and channel under the chats —
    # "See more", then "General" ten times, "See all channels" — and the sweep
    # was opening those one by one while Rajendra's 1:1 waited (1 Oct). The
    # chat list ends at its "See more"; channels reach him through Activity.
    for i, r in enumerate(rail):
        if r["name"].strip().lower().startswith(("see more", "see all")):
            rail = rail[:i]
            break
    rail = [r for r in rail if re.search(r"\w", r["name"] or "")
            and r["name"].lower() not in teams_bridge._NOT_A_CHAT and not is_furniture(r["name"])]
    current = [r["name"] for r in rail]
    # Read BEFORE it is overwritten: the comparison is the whole activity signal.
    try:
        previous = json.loads(store.kv_get(_RAIL_KEY) or "[]")
    except (ValueError, TypeError):
        previous = []
    store.kv_set(_RAIL_KEY, json.dumps(current[:60]))
    try:
        cursor = int(store.kv_get(_CURSOR_KEY) or "0")
    except ValueError:
        cursor = 0
    # A chat that just moved has something new in it — try it, even if it
    # could not be opened earlier.
    moved = set(touched(previous, current))
    readable = [r for r in current if r in moved or not unopenable(r)]
    first = changed_first(rail, readable, previous)
    rest, cursor = pick(readable, cursor, previous)
    store.kv_set(_CURSOR_KEY, str(cursor))
    chosen = list(dict.fromkeys(first + rest))[:MAX_OPENS]
    # What did not fit this minute is owed to the next — never left to the
    # rotation, which could take twenty minutes to come back round.
    store.kv_set(_OWED_KEY, json.dumps([c for c in first if c not in chosen][:40]))
    return chosen


_PREVIEWS_KEY = "chatwatch_previews"
_OWED_KEY = "chatwatch_owed"


def touched(previous: list[str], current: list[str]) -> list[str]:
    """Every chat that had activity since `previous`, from the ORDER alone.

    Teams moves a chat with a new message to the top. So the chats that were
    NOT touched keep their old relative order, and they form the tail of the
    new list; everything before that tail was touched — including a 1:1 that
    five later group messages pushed down to sixth, which never "rose".

    Needed because his Teams renders the rail compact: no preview text under
    any row, so there is nothing to compare but order (checked live, 30 Sep)."""
    if not current:
        return []
    if not previous:
        return current[:1]
    pos = {n: i for i, n in enumerate(previous)}
    k, last = len(current), len(previous)
    for i in range(len(current) - 1, -1, -1):
        p = pos.get(current[i])
        if p is None or p >= last:
            break
        last, k = p, i
    return list(dict.fromkeys([current[0], *current[:k]]))


def changed_first(rail: list[dict], readable: list[str],
                  previous: list[str] | None = None) -> list[str]:
    """Chats whose preview changed since the last look, plus any owed from the
    last sweep — 1:1s first, then groups that name him, then the rest.

    The position of a chat on the rail was the only activity signal, and it
    breaks under a burst: five messages landing after a 1:1 push the 1:1 DOWN
    the list, so it never "rose", missed the cap, and waited for the rotation.
    The preview Teams shows under every row changes when a message arrives,
    wherever the row ends up. A preview that is his own reply ("You: …") is
    something he has already dealt with: left alone, as he asked."""
    try:
        before = json.loads(store.kv_get(_PREVIEWS_KEY) or "{}")
    except (ValueError, TypeError):
        before = {}
    try:
        owed = [c for c in json.loads(store.kv_get(_OWED_KEY) or "[]") if c in readable]
    except (ValueError, TypeError):
        owed = []
    now = {r["name"]: _preview(r) for r in rail}
    store.kv_set(_PREVIEWS_KEY, json.dumps(now))
    if not before and any(now.values()):
        return owed                 # first look: nothing to compare with yet
    if any(now.values()):
        fresh = [r for r in rail if r["name"] in readable and now[r["name"]]
                 and before.get(r["name"]) != now[r["name"]] and not _his_own(now[r["name"]])]
    else:
        # A compact rail shows names only — read the order instead.
        moved = set(touched(previous or [], [r["name"] for r in rail]))
        fresh = [r for r in rail if r["name"] in readable and r["name"] in moved]
    rank = {r["name"]: (0 if _looks_one_to_one(r["name"]) else
                        1 if mentions_him(r["text"]) else 2) for r in fresh}
    ordered = sorted((r["name"] for r in fresh), key=lambda n: rank[n])
    return list(dict.fromkeys(owed + ordered))


def _preview(row: dict) -> str:
    """The part of a rail row that changes when a message arrives."""
    lines = [ln.strip() for ln in (row.get("text") or "").splitlines() if ln.strip()]
    # The time label moves on its own ("9:48 PM" becomes "Yesterday" at
    # midnight) — it is not a message, and must not look like one.
    return " | ".join(ln for ln in lines[1:] if not _TIME_LABEL.match(ln))[:300]


_TIME_LABEL = re.compile(
    r"^(?:\d{1,2}[:.]\d{2}(?:\s*[ap]\.?m\.?)?|yesterday|today|now|just now|"
    r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?|"
    r"\d+\s*(?:m|min|mins|h|hr|hrs|d)(?:\s+ago)?)$", re.I)


def _his_own(preview: str) -> bool:
    return bool(re.match(r"^\s*(?:\S+\s+)?you\s*:", preview or "", re.I))


def _looks_one_to_one(name: str) -> bool:
    """A person's name rather than a room's: no separators, a few words, and —
    when the history knows — someone who has written in a chat named after them."""
    n = (name or "").strip()
    if not n or re.search(r"[:,|&@/#()\[\]]|\+\d|\band\b", n, re.I) or len(n.split()) > 4:
        return False
    try:
        return any((m.get("sender") or "").strip().lower() == n.lower()
                   for m in store.teams_messages(chat=n, limit=20))
    except Exception:                                          # noqa: BLE001
        return len(n.split()) <= 3


async def new_in(chat: str, advance: bool = True) -> list[dict]:
    """Messages in one chat that Asta has not processed, excluding his own.

    `advance=False` looks without consuming: "what's unread" is a question he can
    ask twice, and a read tool that marks things processed would make the second
    answer empty and the first unrepeatable.
    """
    from . import teams_bridge
    rows = await teams_bridge.read_history(chat, limit=READ_LIMIT, max_scrolls=0)
    fresh = unseen(chat, rows or [])
    if fresh:
        news_in(chat)
    # A message already processed is never new again, whatever happened to its
    # key. A reaction changes the text a key is made from; Team Booking's "Hi
    # Everyone — can we connect please" came back as "Vinish wants to connect"
    # at 23:11, long after it was dealt with (30 Sep). Same sender, same time,
    # same words with the reactions taken off: the same message.
    #
    # PROCESSED, not stored. This used to be "anything already in the database",
    # and every other reader stores what it reads — a brain reading the chat for
    # context, "what's unread", the check before a reply. Komal's "u would have
    # called and cleared your doubts … typical Arun" (1 Oct, 14:56) was stored by
    # one of those first, and the sweep then took it for handled: it never
    # reached him.
    done = processed(chat)
    fresh = [r for r in fresh if _ident_hash(r) not in done]
    if advance:
        remember(chat, rows or [])
        mark_processed(chat, rows or [])
    import time as _time
    old = _time.time() - 2 * 3600
    return [r for r in fresh if (r.get("text") or "").strip()
            and not is_from_him(r.get("sender", ""))
            # An image-only message Teams used to drop: not news once it is old.
            and not (_only_images(r.get("text") or "") and float(r.get("sent_at") or 0) < old)]


_IMAGE_MARK = re.compile(r"\[image: [^\]]+\]")


def _only_images(text: str) -> bool:
    return bool(_IMAGE_MARK.search(text)) and not _IMAGE_MARK.sub("", text).strip()


def image_paths(text: str) -> list[str]:
    return [m.group(0)[8:-1].strip() for m in _IMAGE_MARK.finditer(text or "")]


def without_image_paths(text: str) -> str:
    """For his phone: a screenshot is "📷 screenshot", never a file path."""
    n = len(image_paths(text))
    body = _IMAGE_MARK.sub("", text or "").strip()
    tag = "📷 screenshot" if n == 1 else f"📷 {n} screenshots"
    return (f"{body} ({tag})" if body else tag) if n else (text or "")


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


#: At most this many clarifying questions in one conversation, counting the
#: ask-back. A colleague is asked, not interrogated.
MAX_QUESTIONS = 2

#: At or above this, a closing conversation is simply closed. Below it — probably
#: done, not certainly — and only if Asta itself has been helping, it asks once.
CLOSE_SURE = 0.85


def checkin_line() -> str:
    """The one casual question before a conversation Asta was helping with closes.

    A question, and nothing in it promises anything: the same fence as the
    ask-back in `steward`, for the same reason — it goes out in his name.
    """
    return os.environ.get("ASTA_THREAD_CHECKIN_LINE",
                          "Is there anything else you need from my side?")


def _checkin_enabled() -> bool:
    return os.environ.get("ASTA_THREAD_CHECKIN", "1").strip().lower() in ("1", "true", "yes", "on")


def group_silent() -> bool:
    """ASTA_GROUP_SILENT — Asta says nothing in a group chat, not even a question.

    His rule, 29 Sep, "just for now until we make sure one to one chat is
    working fine": in a group, people put what they need up front, so Asta
    analyses it and brings him the result; only his yes posts anything there."""
    return os.environ.get("ASTA_GROUP_SILENT", "1").strip().lower() not in ("0", "false", "no")


async def he_replied_since(chat: str, since: float | None) -> bool:
    """A fresh look at the chat, right now: has Arun written in it since `since`?

    What the sweep read can be minutes old by the time a line is ready — the
    model reads first. He opened Shabda's chat and answered, and Asta then sent
    "Hi Shabda, what's the issue?" on top of his reply (1 Oct). The look costs a
    few seconds; a second voice answering the same message costs his name."""
    if not since:
        return False
    from . import teams_bridge as _bridge
    if not _bridge.enabled():
        return False
    try:
        rows = await _bridge.read_history(chat, limit=8, max_scrolls=0)
    except Exception:                                          # noqa: BLE001
        return False
    return any(float(r.get("sent_at") or 0) > float(since)
               and is_from_him(r.get("sender", ""))
               and not store.is_automatic_teams_message(r.get("key", ""), chat)
               for r in rows)


async def _say(chat: str, line: str, *, group: bool = False,
               since: float | None = None) -> bool:
    """One of the two unapproved lines Asta may send: the ask-back and the check-in.

    Never in a group while ASTA_GROUP_SILENT is on — False, and the caller
    carries on as if the line could not be sent. Never once he has answered
    the message (`since`) himself."""
    if group and group_silent():
        store.record_outcome("thread", "held back", subject=chat[:80],
                             detail=f"group — not said: {line}"[:200])
        return False
    if await he_replied_since(chat, since):
        store.record_outcome("thread", "held back", subject=chat[:80],
                             detail=f"he answered himself — not said: {line}"[:200])
        return False
    from . import senior
    if senior.is_senior(chat):
        # Manager and above: not even "checking, will update you" goes on its
        # own. The line is his to read first — staged, like any reply.
        from . import answers
        store.record_outcome("thread", "held back", subject=chat[:80],
                             detail=f"manager and above — staged for his yes: {line}"[:200])
        await answers.present(who=chat, need="wrote to you", chat=chat, group=False,
                              analysis="🔒 Manager and above — I did not reply on my own.",
                              reply=line)
        return False
    from . import teams_bridge as _bridge
    try:
        await _bridge.send_automatic(chat, line)
    except Exception as exc:                                   # noqa: BLE001
        from . import quiet
        quiet.note("chatwatch.say", exc)
        return False
    store.record_outcome("thread", "said", subject=chat[:80], detail=line[:200])
    if not group:
        # He sees Asta answered someone: the 1:1 is marked unread for him, as
        # if they had just written (his ask, 2 Oct). Never in the way of the ack.
        try:
            if await _bridge.give_back_unread(chat):
                note_restored(chat)
        except Exception as exc:                               # noqa: BLE001
            from . import quiet
            quiet.note("chatwatch.unread_after_ack", exc)
    return True


def their_last(chat: str, hours: float = 12) -> float | None:
    """When the other side last wrote in this chat, from what has been read."""
    import time as _t
    try:
        rows = store.teams_messages(chat=chat, since=_t.time() - hours * 3600, limit=400)
    except Exception:                                          # noqa: BLE001
        return None
    times = [float(r["sent_at"]) for r in rows
             if r.get("sent_at") and not is_from_him(r.get("sender", ""))]
    return max(times) if times else None


def his_last_words(chat: str, before: float | None, hours: float = 24) -> str:
    """What he last said in this chat before `before` — what is being answered."""
    import time as _t
    try:
        rows = store.teams_messages(chat=chat, since=_t.time() - hours * 3600, limit=400)
    except Exception:                                          # noqa: BLE001
        return ""
    mine = [r for r in rows if is_from_him(r.get("sender", "")) and r.get("sent_at")
            and (before is None or float(r["sent_at"]) < float(before))]
    return " ".join((max(mine, key=lambda r: float(r["sent_at"]))["text"] or "").split()) if mine else ""


def answering_him(chat: str, sent_at: float | None, hours: float = 24) -> bool:
    """Was the last message before theirs his? Then what they sent is a reply."""
    if not sent_at:
        return False
    try:
        rows = store.teams_messages(chat=chat, since=float(sent_at) - hours * 3600, limit=400)
    except Exception:                                          # noqa: BLE001
        return False
    before = [r for r in rows if r.get("sent_at") and float(r["sent_at"]) < float(sent_at)]
    if not before:
        return False
    last = max(before, key=lambda r: float(r["sent_at"]))
    return is_from_him(last.get("sender", ""))


def _his_last_minutes(chat: str, now: float, hours: float = 6) -> int | None:
    """Minutes since he last wrote in this chat — None if not in the window.
    A small number means he is in the exchange right now."""
    try:
        rows = store.teams_messages(chat=chat, since=now - hours * 3600, limit=400)
    except Exception:                                          # noqa: BLE001
        return None
    his = [float(r.get("sent_at") or 0) for r in rows if is_from_him(r.get("sender", ""))]
    return int((now - max(his)) // 60) if his else None


def _transcript(chat: str, now: float, hours: float = 6, at_most: int = 14) -> list[str]:
    """The last few messages of this chat, both sides, oldest first, labelled."""
    try:
        rows = store.teams_messages(chat=chat, since=now - hours * 3600, limit=400)
    except Exception:                                          # noqa: BLE001
        return []
    out = []
    earlier = {(r.get("text") or "") for r in rows}
    for r in rows[-at_most:]:
        text = " ".join(as_read(r.get("text") or "", earlier).split())[:400]
        if text:
            out.append(f"{'Arun' if is_from_him(r.get('sender', '')) else r.get('sender', '?')}: {text}")
    return out


#: A colleague whose 1:1 ask is being worked on is told so at once. Tests may set it.
ACKNOWLEDGE = True
#: How long one acknowledgement covers a conversation.
ACK_SECONDS = 3600


async def _acknowledge(tid: str, c: dict) -> bool:
    """"checking bro, will update you" — once per thread per ACK_SECONDS."""
    import time as _t
    from . import steward
    key = f"chatwatch_acked:{tid}"
    try:
        last = float(store.kv_get(key) or 0)
    except ValueError:
        last = 0.0
    if _t.time() - last < ACK_SECONDS:
        return False
    line = steward.ack_line(c["chat"])
    if await _say(c["chat"], line, group=False, since=c.get("sent_at")):
        store.kv_set(key, str(_t.time()))
        return True
    return False


def _concrete(text: str) -> bool:
    """Is there something in it a worker can go and check?"""
    from . import responder, understand
    if understand._HAS_A_HANDLE.search(text or ""):
        return True
    return responder.what_it_asks(text or "") in ("incident", "debug", "pr_review", "review_request")


# --- half an ask: wait for the other half ------------------------------------
#
# 2 Oct, Arun: "if they split and send in as two messages — first message and
# then bookingid — wait and understand, ask them and then respond". The question
# ("does manual customs reach billing for this booking") was answered from the
# documents before the id arrived, and the id alone was then "nothing to check".
# A message that points at a booking it does not include, or an id with no
# question yet, waits — at most HOLD_SECONDS — and is read with what follows.

#: How long half an ask waits for its other half. Typing an id takes seconds.
HOLD_SECONDS = float(os.environ.get("ASTA_SPLIT_HOLD_SECONDS", "60"))
_HELD_KEY = "chatwatch_held"


def _held_all() -> dict:
    try:
        return json.loads(store.kv_get(_HELD_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def held(chat: str) -> dict:
    """{'since', 'why', 'msgs'} for a chat whose half-ask is waiting, or {}."""
    return _held_all().get(chat) or {}


def hold(chat: str, msgs: list[dict], why: str, since: float) -> None:
    allh = _held_all()
    allh[chat] = {"since": since, "why": why,
                  "msgs": [{k: m.get(k) for k in ("sender", "text", "sent_at")} for m in msgs]}
    store.kv_set(_HELD_KEY, json.dumps(allh))


def release(chat: str) -> list[dict]:
    """The held messages, taken back out to be read with the rest."""
    allh = _held_all()
    got = allh.pop(chat, None) or {}
    if got:
        store.kv_set(_HELD_KEY, json.dumps(allh))
    return list(got.get("msgs") or [])


def _held_due(now: float) -> list[str]:
    return [c for c, h in _held_all().items() if now - float(h.get("since") or 0) >= HOLD_SECONDS]


def _next_held(now: float) -> float | None:
    waits = [max(0.0, HOLD_SECONDS - (now - float(h.get("since") or 0)))
             for h in _held_all().values()]
    return min(waits) if waits else None


def _wait_reason(c: dict, now: float) -> str:
    """Why this conversation should wait for the next message, or ''."""
    from . import booking_case
    if c["handled_by_him"] or not c["raw"]:
        return ""
    h = held(c["chat"])
    if h and now - float(h.get("since") or 0) >= HOLD_SECONDS:
        return ""                                   # waited long enough: read it now
    texts = [(m.get("text") or "") for m in c["raw"]]
    before = "\n".join([c.get("open_need") or "", *texts[:-1]])
    return booking_case.waiting_for_more(texts[-1], before)


def _with_the_case(ask_text: str, need: str, convo: list[str]) -> str:
    """The ask with the booking and environment it is about, when those came in
    different messages: "MH65W8JZNVNT" alone, or "preprod" answering "which
    environment?", is the second half of a question asked before."""
    from . import booking_case, responder
    around = "\n".join(convo[-8:])
    if not (booking_case.ids(ask_text) or booking_case.env_of(ask_text)
            or booking_case.points_at_one(ask_text)):
        return ask_text
    out = ask_text
    if not responder.what_it_asks(out) and need:
        out = f"{need}\n{out}"
    found = [i for i in booking_case.ids(around) if i not in booking_case.ids(out)]
    if found and not booking_case.ids(out):
        out += f"\n(booking: {', '.join(found[:3])})"
    env = booking_case.env_of(around)
    if env and not booking_case.env_of(out):
        out += f"\n(environment: {env})"
    return out


async def _nudged(tid: str, c: dict, who: str, now: float) -> str:
    """They pinged again while something of theirs is open. The line for him."""
    from . import answers, loop, responder, threads
    need = c["open_need"]
    first = (who or "They").split()[0]
    cid = answers.phone_conversation()
    staged = loop.awaiting(cid) if cid else None
    waiting_draft = (staged and staged.get("thread") == tid) or any(
        q.get("thread") == tid for q in answers._load_queue())
    if waiting_draft:
        line = f"🔴 {first} pinged again — the reply for them is waiting on your *send*."
    else:
        task = responder.respond(
            "teams-chat", who, need + "\n" + "\n".join(c.get("conversation", [])[-8:]),
            priority=1, key=c["keys"][-1], sent_at=c["sent_at"],
            context=f"So far in this conversation: {c['so_far']}" if c["so_far"] else "",
            reply_to=c["chat"], group=False, need=need, thread=tid, questions=[need])
        if task and task.get("reused"):
            analysis, reply = answers.split(task.get("result") or "")
            if reply and await answers.present(
                    who=who, need=need, chat=c["chat"], group=False, analysis=analysis,
                    reply=reply, task_id=task["id"], thread=tid,
                    note=f"{first} pinged again. Already looked into this (task #{task['id']})."):
                return ""
        if task:
            threads.update(tid, status="working")
            line = (f"🔴 {first} pinged again — still waiting on: {need}. "
                    f"I'm checking it now (task #{task['id']}); the answer comes to you.")
        else:
            threads.update(tid, status="awaiting_arun")
            line = f"🔴 {first} pinged again — still waiting on you for: {need}."
    return line if _worth_telling(tid, line, fyi=False, group=False, now=now) else ""


#: What each conversation last told him, so it never tells him the same thing twice.
_TOLD_KEY = "thread_told:"
#: A line identical to the last one is not news for this long.
SAME_LINE_SECONDS = 6 * 3600
#: A busy group's "nothing needed from you" line, at most this often. On 29 Sep
#: the SCP deployment group produced one per sweep — eight in ninety minutes.
GROUP_FYI_SECONDS = 30 * 60


def _worth_telling(tid: str, line: str, *, fyi: bool, group: bool, now: float) -> bool:
    """Has this conversation got something to tell him that it has not already?

    Records the line when the answer is yes. Any line is held back if it is word
    for word what was told last; a status line ("nothing needed from you") in a
    group also waits out GROUP_FYI_SECONDS after the previous status line. An ask
    is never held back by an FYI, and an FYI never by an ask."""
    try:
        last = json.loads(store.kv_get(_TOLD_KEY + tid) or "{}")
    except (ValueError, TypeError):
        last = {}
    said = " ".join((line or "").lower().split())
    since = now - float(last.get("at") or 0)
    if said and last.get("line") == said and since < SAME_LINE_SECONDS:
        return False
    if fyi and group and last.get("fyi") and since < GROUP_FYI_SECONDS:
        return False
    store.kv_set(_TOLD_KEY + tid, json.dumps({"line": said, "at": now, "fyi": fyi}))
    return True


async def _sweep_threads(notify=None, only: list[str] | None = None) -> list[dict]:
    """One pass, by CONVERSATION: read, group, understand once, act per thread.

    The design of 29 Sep. The old sweep judged every message on its own, which is
    how a thank-you after his own reaction, three status lines about one defect,
    and "please ping when free" were all red pushes on the same morning. Here the
    new messages are grouped into their conversation, the conversation is read
    as a whole by `understand` — in one model call for the whole sweep — and one
    decision is taken per conversation:

      * he already replied or reacted  → closed, silently, whatever the words
      * closing, and sure              → closed, silently
      * closing, unsure, Asta helped   → one casual check-in, then it closes
      * an opener                      → asked, politely, what it is about; once
      * a status update                → one quiet line, never red
      * an ask                         → one line naming what they need, and an
                                         investigation — or the one already done
      * urgent                         → the same, red, now

    Nothing reaches his phone for the first three, and the last two carry the
    conversation, not the message. The attention ledger still records every
    message, and a source he keeps ignoring still cannot turn red.
    """
    import time as _time
    from . import attention, referents, responder, steward, threads, triage, understand
    now = _time.time()
    opened = failed = 0
    convs: dict[str, dict] = {}
    chats = list(only) if only is not None else await candidates()
    # A held half-ask is read on the next pass whatever the rail says.
    chats += [c for c in _held_all() if c not in chats and only is None]
    read_ok: set[str] = set()
    for chat in chats:
        opened += 1
        try:
            known = {(r.get("text") or "") for r in store.teams_messages(chat=chat, limit=300)}
        except Exception:                                      # noqa: BLE001
            known = set()
        try:
            fresh = await new_in(chat)
        except Exception as exc:                               # noqa: BLE001
            from . import teams_bridge
            if isinstance(exc, teams_bridge.NotFound):
                note_unopenable(chat)
            failed += 1
            continue                # one unreadable thread must not end the sweep
        read_ok.add(chat)
        # Half an ask held back last time is read again, before what is new.
        waiting = held(chat)
        if waiting:
            fresh = [*(waiting.get("msgs") or []), *fresh]
        for m in fresh:
            who = (m.get("sender") or chat).strip()
            text = (m.get("text") or "").strip()
            if not text:
                continue
            direct = addressed_to_him(chat, who, text)
            key = attention.key_for(f"{chat}:{text}")
            v = triage.classify(who, text, addressed=direct)
            pri, why, due = attention.rank(v.action, text, addressed=direct, key=key, who=who)
            wanted = attention.consider("teams-chat", key, who=who, what=v.one_line,
                                        why=why, priority=pri, due_at=due)
            if not direct:
                attention.mark_dropped(key)     # read, recorded, not his to answer
                continue
            one_to_one = who.lower() == chat.lower()
            counterpart = who if one_to_one else chat
            tid = threads.tid("teams", counterpart)
            c = convs.setdefault(tid, {
                "id": tid, "who": who, "chat": chat, "counterpart": counterpart,
                "one_to_one": one_to_one, "new": [], "keys": [], "known": known,
                "handled_by_him": False, "wanted": False, "pri": pri,
                "last": "", "sent_at": None, "first_at": m.get("sent_at"), "raw": []})
            c["raw"].append(m)
            seen = as_read(text, known)
            c["new"].append(seen if one_to_one else f"{who}: {seen}")
            c["keys"].append(key)
            c["wanted"] = c["wanted"] or bool(wanted)
            if pri is not None and (c["pri"] is None or pri < c["pri"]):
                c["pri"] = pri
            c["who"], c["last"], c["sent_at"] = who, text, m.get("sent_at")
            # Whether he has dealt with the LATEST message — not any of them. On
            # 29 Sep Navya's "Thank you" carried his salute and was followed by
            # "can you please merge this": his reaction answered the thanks, not
            # the merge. Latest wins, so a follow-up is never swallowed.
            c["handled_by_him"] = answered_by_him(chat, m)
    if opened and failed >= opened:
        attention.note_scrape_error(
            "teams-chat", RuntimeError(f"all {opened} chat(s) failed to open"))
    elif opened:
        attention.note_scrape("teams-chat")
    # A hold whose chat was read but came to nothing this pass (not his to
    # answer after all) is let go, not carried for ever.
    for chat in read_ok:
        if held(chat) and not any(c["chat"] == chat for c in convs.values()) \
                and now - float(held(chat).get("since") or 0) >= HOLD_SECONDS:
            release(chat)
    if not convs:
        return []

    # What each conversation already knows, and — for one just opened — the
    # earlier conversations with this person that it may be picking back up.
    for c in convs.values():
        # BOTH sides, in order. `new_in` leaves his own messages out, rightly —
        # they are not things to act on — but they are the half that says who is
        # handling it. The first live conversation, 29 Sep, was read as "a code
        # change, plan it?" because the model never saw his "u create group with
        # karthik" before "Ok Arun".
        c["conversation"] = _transcript(c["chat"], now)
        c["arun_minutes_ago"] = _his_last_minutes(c["chat"], now)
        t = threads.open("teams", c["counterpart"], chat=c["chat"], now=now)
        c["status"] = t.get("status", "open")
        c["so_far"] = t.get("summary", "")
        # What they were already waiting on BEFORE this sweep — read from the
        # thread, not from the reader, which fills a need in from old chat.
        c["open_need"] = (t.get("need") or "").strip()
        c["asta_spoke"] = bool(t.get("asta_spoke"))
        c["asked_back"] = int(t.get("asked_back") or 0)     # questions asked so far
        c["checked_in"] = bool(t.get("checked_in"))
        c["entities"] = sorted(set(t.get("entities") or [])
                               | set(threads.entities_in(" ".join(c["new"]))))
        c["past"] = []
        if threads.is_new(t, now):
            for r in threads.past(c["counterpart"], " ".join(c["new"]), c["entities"], now=now):
                day = _time.strftime("%d %b", _time.localtime(float(r["closed_at"])))
                c["past"].append(f"#{r['id']} ({day}): {r['need']} — {r['summary']}".strip())

    from . import booking_case
    for tid in list(convs):
        c = convs[tid]
        why_wait = _wait_reason(c, now)
        if why_wait:
            since = float(held(c["chat"]).get("since") or now)
            hold(c["chat"], c["raw"], why_wait, since)
            store.record_outcome("chatwatch", "held", subject=c["chat"][:80],
                                 detail=f"{why_wait} — {(c['last'] or '')[:100]}")
            del convs[tid]
            continue
        if held(c["chat"]):
            release(c["chat"])
    if not convs:
        return []

    decisions = await understand.read(list(convs.values()))

    handled: list[dict] = []
    red: list[str] = []
    quiet_lines: list[str] = []
    started: list[str] = []
    for tid, c in convs.items():
        d = decisions.get(tid) or understand.rules(c)
        state, conf = d["state"], float(d.get("closing_confidence") or 0.0)
        fields = {"need": d.get("need") or "", "summary": d.get("summary") or c["so_far"],
                  "entities": sorted(set(c["entities"]) | set(d.get("entities") or [])),
                  "last_activity": now}
        if d.get("continues"):
            fields["continues"] = d["continues"]
        ping = understand.is_ping(c["new"])
        if ping:
            # A ping changes nothing about what the conversation is about.
            fields["need"] = c.get("open_need", "")
        threads.update(tid, **fields)
        threads._record(tid, f"understood:{state}",
                        f"{d.get('source')} conf={conf:.2f} need={fields['need'][:80]}")
        who = c["who"]
        where = "Teams 1:1" if c["one_to_one"] else f"Teams · {c['chat']}"
        name = who if c["one_to_one"] else f"{who} in {c['chat']}"
        # In the model's own words when it gave them: "Yogesh wants a quick call
        # about the event-history defect — want me to set it up?" reads like a
        # colleague; "🔴 Yogesh Kumar Ravichandran: Discuss and understand the
        # fix" reads like a log line. The template is the floor, not the format.
        tell = (d.get("tell") or "") if d.get("source") == "model" else ""
        # A question about one booking with the booking or its environment still
        # missing is asked for — never answered from the documents, never
        # searched in prod by assumption (2 Oct).
        theirs = "\n".join((m.get("text") or "") for m in c["raw"])
        around = "\n".join([c.get("open_need") or "", *c.get("conversation", [])[-8:]])
        case_q = booking_case.ask_line(theirs, around)
        if c["status"] == "clarifying" and c.get("open_need") and state != "ask" \
                and (booking_case.ids(theirs) or booking_case.env_of(theirs)):
            # The booking or environment Asta asked for. It reads like an answer
            # ("status") — it is the rest of their question, and it is worked.
            state = "ask"


        if c["handled_by_him"]:
            for k in c["keys"]:
                attention.mark_acted(k, why="he replied or reacted")
            attention.settle_with(who)
            threads.close(tid, why="he replied or reacted", now=now)
            continue

        if state == "closing":
            for k in c["keys"]:
                attention.mark_dropped(k)
            if conf < CLOSE_SURE and c["asta_spoke"] and not c["checked_in"] \
                    and _checkin_enabled() \
                    and await _say(c["chat"], checkin_line(), group=not c["one_to_one"],
                                   since=c.get("sent_at")):
                threads.update(tid, checked_in=1, status="checked_in", asta_spoke=1)
                continue
            threads.close(tid, why=f"closing ({d.get('source')}, {conf:.2f})", now=now)
            continue

        if state == "opener":
            for k in c["keys"]:
                attention.mark_dropped(k)
            # The model's question, which uses what it knows ("is this about
            # the event-history defect?"), before the fixed line that knows
            # nothing. Yogesh, 29 Sep: "Call ?" after a defect discussion got
            # "Could you tell me a bit more about what you need help with?"
            if ping and c["one_to_one"] and c.get("open_need") \
                    and c["status"] in ("clarifying", "working", "awaiting_arun"):
                # Not an opening: they are WAITING. Asking "is this about …?"
                # again is the blind reply he called out (Vinish, 30 Sep: an
                # hour after his finding, "Bro" got a second question). Work
                # what is open, and he hears that they are waiting — once.
                line = await _nudged(tid, c, who, now)
                if line:
                    red.append(line)
                continue
            if c["one_to_one"] and answering_him(c["chat"], c.get("first_at") or c.get("sent_at")):
                # They are answering HIM — his message was the last word before
                # theirs. "hi Arunkumar, sorry saw the ping now" was Shabda
                # replying to Arun's own message (1 Oct) and got "Hi Shabda,
                # what's the issue?" back. Nothing to ask: his conversation.
                line = f"💬 {who.split()[0]} replied to your message: " \
                       f"{summarise(' / '.join(c['new']), limit=140)}"
                if _worth_telling(tid, line, fyi=False, group=False, now=now):
                    red.append(line)
                continue
            # The model's question when it knows what this is about and asks it
            # politely ("is this about the event-history defect?"); otherwise
            # "hi Shabda, yes tell me", in his words. Never a curt "what's the
            # issue?" (1 Oct) — safe_question refuses those, and a colleague
            # saying hello has no issue yet.
            if ping:
                ask = steward.ping_back(c["chat"])
            elif case_q:
                ask = case_q
            else:
                ask = understand.safe_question(d.get("question") or "", who) \
                    or steward.opener_line(c["chat"], who)
            if not c["asked_back"] and steward.ask_back_enabled() \
                    and await _say(c["chat"], ask, group=not c["one_to_one"],
                                   since=c.get("sent_at")):
                steward.note_asked_back(who)
                threads.update(tid, asked_back=c["asked_back"] + 1, status="clarifying",
                               asta_spoke=1)
            elif not c["one_to_one"] and tell \
                    and _worth_telling(tid, tell, fyi=True, group=True, now=now):
                quiet_lines.append(tell)      # a group Asta may not speak in
            continue

        # A status line is the model's reading of the conversation — or, when the
        # rules read it, simply the newest message: the rules cannot summarise,
        # and the summary they carry forward is the OLD one, not the news.
        said = (fields["summary"] if d.get("source") == "model" else "") \
            if state == "status" else (fields["need"] or "")
        said = said or summarise(c["last"], known=c["known"])
        referents.note(who, said, source=where)
        handled.append({"chat": c["chat"], "who": who, "text": "\n".join(c["new"]),
                        "priority": c["pri"], "key": c["keys"][-1], "state": state})
        # More from someone whose answer is being worked out, or is waiting for
        # his "send": it belongs with that answer, whatever else happens to it.
        with contextlib.suppress(Exception):
            from . import answers as _answers
            await _answers.note_followup(tid, who, "\n".join(as_read(x) for x in c["new"]))

        if state == "status" and c["one_to_one"] \
                and answering_him(c["chat"], c.get("first_at") or c.get("sent_at")):
            # An answer to HIS message is never a line for later. 2 Oct: Vinish's
            # "Bro, tomorrow morning, 10 to 11 Am" — his answer to "bro when ru
            # free for the call" — was read in 33 s and filed as status: no word
            # to him. He hears it now; Asta does not answer it for him (a time
            # agreed is his to agree).
            for k in c["keys"]:
                attention.mark_dropped(k)
            line = f"💬 {who.split()[0]} replied to you: " \
                   f"{summarise(' / '.join(c['new']), limit=200)}"
            asked = his_last_words(c["chat"], before=c.get("first_at") or c.get("sent_at"))
            if asked:
                line += f"\n(to your: “{asked[:120]}”)"
            if _worth_telling(tid, line, fyi=False, group=False, now=now):
                red.append(line)
            continue

        if state == "status":
            # Nothing is needed from him: a line to read later, never a buzz.
            for k in c["keys"]:
                attention.mark_dropped(k)
            if _worth_telling(tid, said, fyi=True, group=not c["one_to_one"], now=now):
                quiet_lines.append(tell or f"· {name}: {said}")
            continue

        # From here Asta works the ask the way he would, and he hears once, at
        # the end: "this person asked this, this is the final analysis, can I
        # send?" Only three things reach him before that — something urgent, a
        # code change (which needs his yes before anyone plans it), and an ask
        # Asta could not take on.
        # 1. CLARIFY, directly with them — no approval needed for a question.
        # When the subject is unclear (earlier chat is context, not proof), or
        # the ask is too vague to act on, ask before planning or investigating
        # anything. His rule, 29 Sep: talk to them directly, get what they want;
        # only the final analysis comes to him.
        q = understand.safe_question(d.get("question") or "", who)
        if q and d.get("subject") != "unclear" and _concrete("\n".join(c["new"])):
            # They named the thing — a booking, a container, a PR, an error.
            # That is checked, not asked about: Vinish's container finding
            # (30 Sep) got a question back where it should have got the logs.
            q = ""
        if not q and d.get("subject") == "unclear" and state == "ask":
            q = steward.ASK_BACK
        if case_q and state == "ask":
            q = case_q
        if state == "ask" and q and c["asked_back"] < MAX_QUESTIONS \
                and steward.ask_back_enabled() \
                and await _say(c["chat"], q, group=not c["one_to_one"], since=c.get("sent_at")):
            threads.update(tid, asked_back=c["asked_back"] + 1, status="clarifying",
                           asta_spoke=1)
            continue

        # 2. A code change needs his yes before anyone plans it.
        # …and a PR to REVIEW is not a code change to plan. Komal's "please
        # review …/pull/1459" reached him as "asks for a code change — reply yes
        # and I'll plan it" (30 Sep). It is read, and the review comes to him.
        review = responder.what_it_asks(f"{fields['need']}\n" + "\n".join(c["new"])) \
            in ("review_request", "pr_review")
        if d.get("work") == "code" and state != "urgent" and not review:
            from . import answers
            await answers.offer_plan(who=who, chat=c["chat"], need=said,
                                     summary=fields["summary"], thread=tid,
                                     words="\n".join(c["new"]))
            continue

        # 3. Everything else is worked — a call request too: if there is
        # something to check (the defect, the PR), it is checked first, and the
        # final message carries where it stands and the reply for them.
        context = "\n".join(x for x in [
            f"So far in this conversation: {c['so_far']}" if c["so_far"] else "",
            *[f"Earlier with {who}: {p}" for p in c["past"]]] if x)
        from . import offers
        before = offers.pending()
        ask_text = "\n".join(c["new"])
        ask_text = _with_the_case(ask_text, c.get("open_need") or said,
                                  c.get("conversation", []))
        if review and not responder.what_it_asks(ask_text):
            # The message that reached him was only the mention ("Arunkumar,
            # Vinish"); the PR itself is a few lines up in the same chat.
            ask_text = f"{said}\n" + "\n".join(c.get("conversation", [])[-6:])
        review_revision = ""
        if responder.what_it_asks(ask_text) == "review_request":
            from . import review as pr_review
            refs = {f"{owner}/{repo}#{number}"
                    for owner, repo, number, alt_owner, alt_repo, alt_number
                    in pr_review._PR_LINK.findall(f"{ask_text}\n{context}")
                    for owner, repo, number in
                    [(owner or alt_owner, repo or alt_repo, number or alt_number)]}
            if len(refs) == 1:
                try:
                    verified, reusable = await pr_review.revision(next(iter(refs)))
                    if reusable:
                        review_revision = verified
                except (RuntimeError, ValueError) as exc:
                    store.record_outcome("review", "verification failed",
                                         subject=next(iter(refs))[:80], detail=str(exc)[:200])
        task = responder.respond("teams-chat", who, ask_text, priority=c["pri"],
                                 key=c["keys"][-1], sent_at=c["sent_at"], context=context,
                                 reply_to=c["chat"], group=not c["one_to_one"], need=said,
                                 thread=tid, questions=d.get("questions") or [],
                                 review_revision=review_revision)
        if task and task.get("reused"):
            from . import answers
            analysis, reply = answers.split(task.get("result") or "")
            if reply and await answers.present(
                    who=who, need=said, chat=c["chat"], group=not c["one_to_one"],
                    analysis=analysis, reply=reply, task_id=task["id"], thread=tid,
                    note=f"Already looked into this recently (task #{task['id']}) — not run again."):
                continue
        if task and not task.get("reused"):
            threads.update(tid, status="working")
            # They hear back within the minute — "checking", in his words — and
            # the answer follows. 1 Oct: Vinish asked at 13:36 and heard nothing
            # while Asta worked; "earlier we acknowledged them and got them what
            # they wanted". 1:1 only, once per thread an hour, and never while he
            # himself is in the conversation.
            if ACKNOWLEDGE and c["one_to_one"] and (c.get("arun_minutes_ago") is None
                                    or c["arun_minutes_ago"] > 5):
                with contextlib.suppress(Exception):
                    await _acknowledge(tid, c)
            if task.get("joined"):
                continue
            if state == "urgent" and _worth_telling(tid, said, fyi=False,
                                                    group=not c["one_to_one"], now=now):
                red.append(f"🚨 {tell}" if tell else f"🚨 {name}: {said}")
                started.append(responder.line_for(task, who, responder.what_it_asks(c["last"])))
            # Otherwise silent until the answer is ready for his "send".
            continue
        # Nothing to check, and they want HIM: the final message now — who, what
        # about, and the reply in his voice, so one word answers them.
        if d.get("work") == "talk" and state == "ask" and not task:
            from . import answers
            if tell and (d.get("reply") or "").strip() \
                    and _worth_telling(tid, said, fyi=False, group=not c["one_to_one"], now=now) \
                    and await answers.present(who=who, need=said, chat=c["chat"],
                                              group=not c["one_to_one"], analysis="",
                                              reply=d["reply"], thread=tid, lead=tell):
                continue
        # Not taken on — he needs to know, and to know what would move it.
        after = offers.pending()
        offered = after is not None and (before is None or after.id != before.id)
        line = f"{name}: {said}" + (" — reply *yes* and I'll look into it." if offered else "")
        loud = c["wanted"] or state == "urgent"
        # Whether HE is interrupted is the ledger's call — a source he keeps
        # ignoring stays quiet. Whether the colleague is helped never was.
        if offered or _worth_telling(tid, said, fyi=False,
                                     group=not c["one_to_one"], now=now):
            if tell:
                line = tell + (" Reply *yes* and I'll look into it." if offered else "")
                (red if loud else quiet_lines).append(line)
            else:
                (red if loud else quiet_lines).append(f"{'🔴' if loud else '·'} {line}")
        if task:
            started.append(responder.line_for(task, who, responder.what_it_asks(c["last"])))

    if notify and (red or quiet_lines or started):
        lines = red + quiet_lines
        # Sentences get room; a list of one-liners stays a list under its label.
        spoken = any(not x.startswith(("·", "🔴", "🚨")) for x in lines)
        body = ("\n\n" if spoken else "\n").join(lines)
        if started:
            body += ("\n\n" if body else "") + "\n".join(started)
        await notify(body if spoken else "💬 Teams\n" + body, "teams",
                     urgency="direct" if red or started else "ambient",
                     considered=True, keys=tuple(h["key"] for h in handled))
    return handled


async def sweep(notify=None, only: list[str] | None = None) -> list[dict]:
    """One pass: what moved, what is new in it, judged and acted on.

    Returns the messages it handled, so a test can assert on the decision rather
    than on a notification having been sent.

    With ASTA_THREADS on, the pass is by conversation — see `_sweep_threads`. The
    per-message path below is kept whole, as the rollback.
    """
    from . import threads
    if threads.enabled():
        return await _sweep_threads(notify, only)
    from . import attention, responder, triage
    handled: list[dict] = []
    lines: list[str] = []
    started: list[str] = []
    opened = failed = 0
    for chat in (only if only is not None else await candidates()):
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
        except Exception as exc:                               # noqa: BLE001
            from . import teams_bridge
            if isinstance(exc, teams_bridge.NotFound):
                note_unopenable(chat)
            failed += 1
            continue                # one unreadable thread must not end the sweep
        for i, m in enumerate(fresh):
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
            # Kendall just wanted to check what we have concluded" — a standing
            # group discussion between other people, none of it his, delivered to
            # his phone. `direct` was computed here and then never used.
            if not direct:
                # Recorded, and explicitly NOT owed by him. `consider` has already
                # stamped this row `notified` — that is what it does when it
                # decides to push — so leaving it there made it chaseable, and the
                # hourly chase has no idea this second gate exists. That is how
                # "Still waiting on you (5)" came to list a deployment
                # announcement and two people talking to each other: "im not even
                # in the contest but it still asking it waiting for me".
                #
                # Dropped without a label: he neither engaged nor ignored it, and
                # scoring it either way would teach the filter from a message it
                # was right not to show him.
                attention.mark_dropped(key)
                continue
            # He has already dealt with it. "i have already shared na the
            # analysis then why again it doing" — Alex's list of production
            # issues was investigated by a background task while Arun's own answer
            # was already sitting in the thread above it. A reply of his, later
            # than the ask, is the clearest possible signal that it is handled.
            #
            # This used to sit two lines lower, which suppressed the INVESTIGATION
            # and forwarded the message anyway — so an answered question still
            # reached his phone, and still sat in "still waiting on you" after
            # that. Settled and silent is the whole point.
            if answered_by_him(chat, m):
                attention.mark_acted(key, why="he replied")
                attention.settle_with(who)
                continue
            # A greeting with nothing in it is the START of a conversation, not
            # news. Held here rather than pushed, and the message that explains
            # it — arriving seconds later — is what reaches him, once, with the
            # greeting behind it. See app/steward.py.
            from . import steward
            opening = steward.consider(who, text)
            if opening["hold"]:
                attention.mark_dropped(key)
                # …and ask them what for, so the hold ends with an answer rather
                # than with him driving it by hand. The ONE outward act that does
                # not wait for his yes, and only ever this one question — the
                # answer it produces is staged for approval like everything else.
                # See steward.ask_back_line for why the exception is this narrow.
                back = steward.ask_back_line(who, text)
                if back:
                    from . import teams_bridge as _bridge
                    try:
                        await _bridge.send_message(chat, back)
                    except Exception as exc:                   # noqa: BLE001
                        from . import quiet
                        quiet.note("chatwatch.ask_back", exc)
                    else:
                        steward.note_asked_back(who)
                continue
            if opening["opened_with"]:
                text = f"{opening['opened_with']}\n{text}"
            handled.append({"chat": chat, "who": who, "text": text, "priority": pri, "key": key})
            lines.append(render(chat, who, text, pri, known=known))
            # So "ask her what it is" a minute later knows who "her" is.
            from . import referents
            referents.note(who, text, source="Teams 1:1" if who.lower() == chat.lower()
                           else f"Teams · {chat}")
            # The few lines BEFORE this one, from the same person. People paste
            # the link and ask about it in the next breath — Alex's booking id
            # was one message above "can you check why STF is not done?", so
            # the responder got a question with nothing to check it against and
            # asked Arun for permission instead of just looking.
            before = "\n".join(
                (x.get("text") or "").strip() for x in fresh[max(0, i - 3):i]
                if (x.get("sender") or chat).strip() == who)
            task = responder.respond("teams-chat", who, text, priority=pri, key=key,
                                     sent_at=m.get("sent_at"), context=before)
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
                     considered=True, keys=tuple(h["key"] for h in handled))
    return handled


def in_a_call() -> bool:
    """Is a call live? One definition, in the bridge that owns the browser."""
    from . import teams_bridge
    return teams_bridge.in_a_call()


def stand_down() -> bool:
    """Should this poll be skipped? Both reasons are "somebody else owns it".

    A call owns the browser (see watch_loop). The BENCH owns something worse: it
    runs inside this process and replaces `notify`, `store.DB_PATH` and the chat
    rail itself with recorders, so a sweep that lands mid-run reads a temporary
    database and posts his real Teams messages into a scenario's list. That is
    how a twenty-two-hour silence started — and it looked exactly like a quiet
    morning from the outside.
    """
    if in_a_call():
        return True
    from .workworld import world as bench_world
    return bench_world.installed()


async def watch_loop() -> None:
    """Read Teams the moment a chat changes; everything, every few minutes.

    The page itself reports a chat turning unread or moving up (see
    attach_rail), and only that chat is opened, seconds later. A full sweep
    still runs every FULL_SWEEP_SECONDS as the net — and every POLL_SECONDS,
    as before, whenever the page has stopped reporting. The first sweep runs
    straight away: after a restart or a night offline, what came in meanwhile
    is read now, not a cycle later."""
    from . import notify, teams_bridge, wake
    import time as _time
    last_full = 0.0
    first = True
    while True:
        if first:
            first = False
            await asyncio.sleep(STARTUP_SECONDS)
            why = "full"
        else:
            every = FULL_SWEEP_SECONDS if rail_alive() else POLL_SECONDS
            due = max(MIN_GAP_SECONDS, every - (_time.monotonic() - last_full))
            why = await _wait_for_work(due)
        hot = [] if why == "full" else take_hot()
        if why in ("hot", "idle") and not hot:
            continue                    # nothing worth opening after all
        # A call OWNS the browser. Chromium tolerates one writer per profile, so
        # a sweep during a call is not a slow read — it is a second instance
        # contending for the tree the call is holding, and with real Chrome the
        # loser hands off and exits silently rather than erroring.
        #
        # That is what ended three calls in a row: dialled, then eleven seconds
        # later "TargetClosedError: Keyboard.press" while still searching for the
        # person's name. `incoming.watch_loop` already stands down for exactly
        # this reason; this loop never learned to.
        if stand_down():
            continue
        if not (enabled() and teams_bridge.enabled() and teams_bridge.logged_in_once()
                and store.kv_get("teams_session_ok") != "0"):
            continue
        began = _time.monotonic()
        try:
            if hot:
                found = await sweep(notify.notify, only=hot)
            else:
                last_full = _time.monotonic()
                take_hot()                  # a full sweep reads them all anyway
                found = await sweep(notify.notify)
            # One line per read: "is it reading?" is answered from the log, not
            # guessed (2 Oct: hours of failed reads looked like a quiet day).
            store.record_outcome("chatwatch", "read", detail=(
                f"{'hot' if hot else 'full'} · {len(found or [])} new · "
                f"{_time.monotonic() - began:.0f}s · {', '.join(hot)}")[:200])
        except Exception as exc:                               # noqa: BLE001
            from . import quiet
            quiet.note("chatwatch.sweep", exc)
            store.record_outcome("chatwatch", "read_failed", detail=(
                f"{'hot' if hot else 'full'} · {type(exc).__name__}: {exc}")[:200])
        # Conversations that are over give their context back — "once the convo
        # resolved automatically dissolve the thread context and make it free".
        from . import threads
        if threads.enabled():
            try:
                threads.dissolve_due()
                # A decision that waited behind another comes up once he is free.
                from . import answers
                await answers.announce_offer()
                await answers.next_after()
            except Exception as exc:                           # noqa: BLE001
                from . import quiet
                quiet.note("chatwatch.dissolve", exc)
        await asyncio.sleep(0)


# --- the page reports; nothing polls ------------------------------------------------

#: The full sweep, with the rail watcher alive. The net, not the way in.
FULL_SWEEP_SECONDS = 300.0
#: Before the first sweep after a start — enough for Teams to come up.
STARTUP_SECONDS = 20.0
#: A report is gathered this long, so a burst of messages is one read.
HOT_DEBOUNCE_SECONDS = 2.0
#: The watcher reports at least once a minute; quiet longer than this, it is down.
RAIL_SILENT_SECONDS = 180.0
#: A chat Asta gave back to him unread can only be re-read to see more news in
#: it: a new message in an already-bold chat changes nothing about its bold.
#: But it MOVES the chat to the top, which the watcher sees — so only a chat
#: that cannot move up (already among the top few, or pinned there) is re-read,
#: at 1, 2 and 4 minutes; after that the full sweep, which always reads the top
#: of the list, covers it. 2 Oct: re-read every minute while he left it unread —
#: 28 opens of one group in an hour, ~2,900 over two days, all "0 new".
RESTORED_RECHECK_SECONDS = 60.0
RESTORED_RECHECK_STEPS = 3
#: The chats at the head of the list: a new message cannot move them up.
RESTORED_TOP = 3

_RAIL: dict = {"order": [], "unread": set(), "at": 0.0}
_HOT: dict[str, float] = {}
_RESTORED: dict[str, float] = {}
_CHECKED: dict[str, float] = {}
#: How many re-reads each restored chat has had since its last news.
_BACKOFF: dict[str, int] = {}
_EVENT: dict = {"ev": None}


def _hot_event() -> asyncio.Event:
    if _EVENT["ev"] is None:
        _EVENT["ev"] = asyncio.Event()
    return _EVENT["ev"]


def rail_alive(now: float | None = None) -> bool:
    import time as _t
    now = _t.time() if now is None else now
    return bool(_RAIL["at"]) and now - _RAIL["at"] < RAIL_SILENT_SECONDS


def note_restored(chat: str) -> None:
    """Asta read this chat and put it back to unread for him."""
    import time as _t
    _RESTORED[chat] = _t.time()


def on_rail(rows: list[str], now: float | None = None) -> list[str]:
    """What the page reported, turned into the chats to read now."""
    import time as _t
    from . import teams_bridge
    now = _t.time() if now is None else now
    marks = [r for r in rows or [] if r.lstrip("*").startswith("!")]
    rows = [r for r in rows or [] if r not in marks and re.search(r"\w", r.lstrip("*"))]
    if _RAIL["order"] and len(rows) < len(_RAIL["order"]) // 2:
        # Not the chat list: mid-navigation Teams paints its app bar's icon
        # glyphs ("\uebe2"…) as the tree. Taken as the list, the real one coming
        # back looked like six chats moving — six needless reads after every
        # sweep (2 Oct). The picture is ignored.
        return []
    # "Mentions" bold: a channel @mention — the Activity feed reads it now.
    if any(m.startswith("*") for m in marks) and not _RAIL.get("mentioned"):
        with contextlib.suppress(Exception):
            teams_bridge.mentioned().set()
    _RAIL["mentioned"] = any(m.startswith("*") for m in marks)
    order = [r[1:] if r.startswith("*") else r for r in rows]
    unread = {r[1:] for r in rows if r.startswith("*")}
    prev_order, prev_unread = _RAIL["order"], _RAIL["unread"]
    _RAIL.update(order=order, unread=unread, at=now)
    with contextlib.suppress(Exception):
        store.kv_set("rail_last", json.dumps({"at": now, "rows": list(rows or [])[:40]}))
    if not prev_order:
        hot = set(unread)                       # first look: catch up on all of it
        with contextlib.suppress(Exception):
            store.record_outcome("rail", "reporting",
                                 detail=f"{len(order)} chats, {len(unread)} unread")
    else:
        hot = unread - prev_unread
        if order != prev_order:
            hot |= set(touched(prev_order, order))
    # Our own "Mark as unread" is not news.
    hot = {c for c in hot if not (c in _RESTORED and now - _RESTORED[c] < 30
                                  and c not in touched(prev_order, order)[1:])}
    for c in list(_RESTORED):
        if c not in unread and now - _RESTORED[c] > 30:
            _RESTORED.pop(c, None)              # he has read it himself
            _BACKOFF.pop(c, None)
    # A row with no letters is not a chat: while Teams loads, its list is
    # placeholder rows with invisible names, and ten of them were "read" every
    # five minutes (81 s each time, 2 Oct).
    hot = {c for c in hot if re.search(r"\w", c or "") and c.lower() not in teams_bridge._NOT_A_CHAT
           and not is_furniture(c) and not c.lower().endswith("(you)")}
    for c in hot:
        _HOT.setdefault(c, now)
    if hot:
        _hot_event().set()
    return sorted(hot)


def _at_the_top(chat: str) -> bool:
    """Among the first few real chats: a new message cannot move it up."""
    from . import teams_bridge
    real = [c for c in _RAIL["order"] if re.search(r"\w", c or "") and not is_furniture(c)
            and c.lower() not in teams_bridge._NOT_A_CHAT and not c.lower().endswith("(you)")]
    return chat in real[:RESTORED_TOP]


def _restored_wait(chat: str) -> float | None:
    """How long after its last read this restored chat is re-read — None: not
    again (it can announce news by moving, or the sweep covers it)."""
    if chat not in _RAIL["unread"] or not _at_the_top(chat):
        return None
    n = _BACKOFF.get(chat, 0)
    return None if n >= RESTORED_RECHECK_STEPS else RESTORED_RECHECK_SECONDS * (2 ** n)


def _restored_due(now: float) -> list[str]:
    due = []
    for c, at in _RESTORED.items():
        wait = _restored_wait(c)
        if wait is not None and now - _CHECKED.get(c, at) >= wait:
            due.append(c)
    return due


def _next_restored(now: float) -> float | None:
    """Seconds until the next restored chat is due, if any is."""
    waits = []
    for c, at in _RESTORED.items():
        wait = _restored_wait(c)
        if wait is not None:
            waits.append(max(0.0, wait - (now - _CHECKED.get(c, at))))
    return min(waits) if waits else None


def news_in(chat: str) -> None:
    """New messages were found in this chat: people are talking there, so a
    restored chat goes back to being re-read soon."""
    _BACKOFF.pop(chat, None)


def take_hot() -> list[str]:
    """The chats to read now, oldest report first; the queue is emptied."""
    import time as _t
    now = _t.time()
    due = _restored_due(now)
    for c in due:
        _BACKOFF[c] = _BACKOFF.get(c, 0) + 1
    chats = sorted(_HOT, key=_HOT.get) + [c for c in due if c not in _HOT]
    # Half an ask that has waited its time is read now, with or without the rest.
    chats += [c for c in _held_due(now) if c not in chats]
    _HOT.clear()
    _hot_event().clear()
    for c in chats:
        _CHECKED[c] = now
    return chats[:MAX_OPENS]


async def _wait_for_work(timeout: float) -> str:
    """"hot" when the page reported something, "woke" after sleep, else "full"."""
    import time as _t
    from . import wake
    ev = _hot_event()
    full = timeout
    for nxt in (_next_restored(_t.time()), _next_held(_t.time())):
        if nxt is not None:
            timeout = min(timeout, max(1.0, nxt))
    sleeper = asyncio.ensure_future(wake.sleep(timeout))
    waiter = asyncio.ensure_future(ev.wait())
    try:
        done, _ = await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for f in (sleeper, waiter):
            if not f.done():
                f.cancel()
    if waiter in done:
        await asyncio.sleep(HOT_DEBOUNCE_SECONDS)
        return "hot"
    if sleeper.result():
        return "full"                           # the Mac woke: read everything
    if _restored_due(_t.time()) or _held_due(_t.time()):
        return "hot"
    # Woken early for a re-read that is not due after all: NOT a full sweep.
    # (A full sweep every minute was the old fall-through.)
    return "idle" if timeout < full else "full"


async def attach_rail(ctx, page) -> None:
    """Install the rail watcher on the pooled Teams browser. Never raises: the
    sweep falls back to once a minute if this does not take."""
    if not enabled():
        return
    from . import teams_bridge

    async def _reported(source, rows):
        with contextlib.suppress(Exception):
            on_rail(list(rows or []))

    with contextlib.suppress(Exception):
        await ctx.expose_binding("astaRail", _reported)
    with contextlib.suppress(Exception):
        await ctx.add_init_script(teams_bridge.RAIL_WATCH_JS)
    with contextlib.suppress(Exception):
        await page.evaluate(teams_bridge.RAIL_WATCH_JS)
