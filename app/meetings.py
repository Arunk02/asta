"""Calendar and calls: making invites, joining them, and what happens afterwards.

Three jobs that look unrelated and are not:

  invite   build a meeting or an all-day leave request and put it in front of him
  join     be in a call he cannot attend, or does not want to attend alone
  recap    afterwards, tell him the part that was actually about him

**Invites are BUILT, not typed.** Outlook's compose form is a moving target, and
automating it field by field means a broken selector silently produces an invite
with no attendees or the wrong day. So the invite is assembled as a compose
deeplink — a URL Outlook itself parses — which makes the whole construction step
deterministic, unit-testable, and impossible to half-complete. All the browser
does is open it and, once Arun says yes, press one button.

**Nothing is sent by this module on its own.** An invite going out books time in
other people's calendars; a leave request goes to his manager. Both are staged as
offers with their exact contents, and the send is a recorded op — the same rule
that governs Jira comments and PR approvals, for the same reason.

**Speaking in a call is gated, not faked.** Joining a meeting is a browser action
and works. Putting Asta's VOICE into that meeting requires the operating system to
have a virtual microphone that Teams can select, which is a machine setup step no
amount of code here can substitute for. When it is not configured this says so and
refuses; it does not join silently and let him believe something was said.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta

from . import quiet, store
from .call_brain import (  # noqa: F401  (re-exported: callers and tests use these)
    _ANSWERABLE, _ANSWER_PROMPT, _ASKED, _HIS_TO_ANSWER, _ask_key, _call_tools,
    answer_from_knowledge, classify_line, clear_noticed, confident, notice_asks,
    pending_for_him, spoken_form, CONFIRM_SPEECH, SPOKEN_ANSWER_WORDS)


COMPOSE_URL = "https://outlook.office.com/calendar/deeplink/compose"


def _now() -> float:
    """The clock everything in a call is measured against.

    `monotonic` rather than the event loop's clock, which was the original here.
    Loop time reads fine inside a coroutine and raises outside one, so anything
    that wanted to know how long a call had been running from ordinary code —
    a report, a status line, a test — depended on there happening to be a loop.
    It also cannot go backwards when the clock is adjusted, which `time.time()`
    can, and a call whose duration jumps is a call he cannot trust.
    """
    return time.monotonic()

#: The macOS virtual audio device Teams must be pointed at for Asta to be heard
#: (BlackHole, Loopback, or similar). Empty = speaking is not available, and
#: `say_in_call` says so rather than pretending.
AUDIO_DEVICE = os.environ.get("ASTA_CALL_AUDIO_DEVICE", "").strip()

#: A joined call is left after this long no matter what, so a meeting that
#: overruns — or a bug — cannot leave Asta sitting in someone's call all day.
MAX_CALL_MINUTES = int(os.environ.get("ASTA_MAX_CALL_MINUTES", "90"))
#: Captions scroll out of their window within seconds, so they are read far more
#: often than the call-ended check. A caption missed is gone; a call noticed as
#: ended a few seconds late costs nothing. Two seconds rather than four because a
#: caption is now the trigger for answering out loud, and every second spent not
#: having noticed the question is a second of silence on the line.
CAPTION_POLL_SECONDS = float(os.environ.get("ASTA_CAPTION_POLL", "2"))

#: How long a placed call is allowed to ring before Asta hangs up. Someone who
#: has not picked up in this long is not about to, and a call left ringing holds
#: the browser context — and therefore the next call — open indefinitely.
RING_SECONDS = float(os.environ.get("ASTA_RING_SECONDS", "45"))

#: How long an answer may take before it is too late to say out loud. Measured
#: rather than guessed: a warm kokoro line costs ~1.1s to synthesise and ~0.4s to
#: switch the microphone, so the budget is almost entirely thinking time. Past it
#: the moment has gone, and the answer goes to his phone instead of arriving in
#: the call forty seconds after anybody wanted it.
ANSWER_BUDGET_SECONDS = float(os.environ.get("ASTA_ANSWER_BUDGET", "25"))



# --- building an invite ------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def compose_url(subject: str, start: datetime, end: datetime,
                attendees: list[str] | None = None, body: str = "",
                all_day: bool = False, online: bool = True) -> str:
    """The Outlook deeplink that opens a fully pre-filled invite.

    Every field is placed by Outlook's own parser rather than by us clicking
    around its UI, so either the whole invite is right or the link is obviously
    wrong — there is no half-filled middle state to notice too late.
    """
    params = {
        "subject": subject,
        "startdt": _iso(start),
        "enddt": _iso(end),
        "body": body,
        "path": "/calendar/action/compose",
        "rru": "addevent",
    }
    if all_day:
        params["allday"] = "true"
    if online:
        params["online"] = "1"
    if attendees:
        params["to"] = ";".join(a.strip() for a in attendees if a.strip())
    return COMPOSE_URL + "?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in ("", None)})


def leave_invite(start_date: str, end_date: str = "", reason: str = "",
                 to: list[str] | None = None) -> dict:
    """An all-day leave/out-of-office invite. Returns the invite, unsent.

    End date is EXCLUSIVE in the calendar's own model, so a single day off runs
    to the next morning. Getting that wrong by one day is the classic off-by-one
    in leave booking, and the person who finds it is whoever needed him on the
    day he was actually there.
    """
    start = _parse_day(start_date)
    end = _parse_day(end_date) if end_date else start
    if end < start:
        raise RuntimeError(f"leave ends ({end_date}) before it starts ({start_date})")
    subject = "Leave — Arun" + (f" ({reason})" if reason else "")
    return {
        "subject": subject,
        "start": start,
        "end": end + timedelta(days=1),        # exclusive: the day itself is included
        "all_day": True,
        "attendees": to or [],
        "body": (reason or "Out of office."),
        "days": (end - start).days + 1,
        "url": compose_url(subject, start, end + timedelta(days=1),
                           to or [], reason or "Out of office.",
                           all_day=True, online=False),
    }


def meeting_invite(subject: str, when: str, minutes: int = 30,
                   attendees: list[str] | None = None, agenda: str = "") -> dict:
    """A normal meeting. Returns the invite, unsent."""
    start = _parse_when(when)
    end = start + timedelta(minutes=max(5, minutes))
    return {
        "subject": subject,
        "start": start,
        "end": end,
        "all_day": False,
        "attendees": attendees or [],
        "body": agenda,
        "url": compose_url(subject, start, end, attendees or [], agenda, online=True),
    }


_DAY = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_WHEN = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})\s*$")


def _parse_day(s: str) -> datetime:
    m = _DAY.match(s or "")
    if not m:
        raise RuntimeError(f"date must be YYYY-MM-DD, got '{s}'")
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        # Shaped right but impossible ("2026-13-01", "2026-02-30"). Raised as a
        # RuntimeError like every other rejection here, so the calling tool
        # answers with a sentence instead of dying on a ValueError it never
        # thought to catch.
        raise RuntimeError(f"'{s}' is not a real date — {exc}") from exc


def _parse_when(s: str) -> datetime:
    """Local wall-clock time, given explicitly.

    Deliberately NOT a natural-language parser. "Thursday at 3" resolved by a
    library that disagrees with him about which Thursday books a real meeting in
    other people's calendars on the wrong day — so whoever calls this resolves the
    words first and passes an unambiguous timestamp.
    """
    m = _WHEN.match(s or "")
    if not m:
        raise RuntimeError(f"time must be 'YYYY-MM-DD HH:MM' (local), got '{s}'")
    y, mo, d, h, mi = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError as exc:
        raise RuntimeError(f"'{s}' is not a real date/time — {exc}") from exc


def describe(invite: dict) -> str:
    """What he reads before approving — the facts, in the order he checks them."""
    if invite.get("all_day"):
        days = invite.get("days", 1)
        when = (f"{invite['start']:%a %d %b}" if days == 1
                else f"{invite['start']:%a %d %b} → {invite['end'] - timedelta(days=1):%a %d %b}"
                     f" ({days} days)")
        when += ", all day"
    else:
        when = f"{invite['start']:%a %d %b, %H:%M}–{invite['end']:%H:%M}"
    who = ", ".join(invite.get("attendees") or []) or "no attendees"
    return f"{invite['subject']}\n{when}\nTo: {who}"


# --- sending it (outward — only through an approved offer) -------------------

_SEND_BUTTONS = ('button[aria-label="Send"]', 'button[aria-label*="Send" i]',
                 '[data-tid="sendButton"]', 'button:has-text("Send")')


async def open_and_send(url: str, send: bool = False) -> str:
    """Open a pre-filled invite; press Send only when told to.

    Verified like every other outward act: if the compose window is still sitting
    there afterwards, this reports NOT sent rather than assuming the click landed.
    """
    from . import outlook, teams_bridge
    async with teams_bridge._lock:
        pw, ctx = await teams_bridge._launch()
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await outlook._open(page, url, '[role="main"], [aria-label*="Send" i]')
            await asyncio.sleep(3)
            if not send:
                return "opened (not sent)"
            for sel in _SEND_BUTTONS:
                try:
                    await (await page.wait_for_selector(sel, timeout=5000)).click()
                    await asyncio.sleep(3)
                    store.kv_set("teams_session_ok", "1")
                    return "sent"
                except Exception:
                    continue
            raise RuntimeError("couldn't find the Send button — treat as NOT sent")
        finally:
            await ctx.close()
            await pw.stop()


# --- joining a call ----------------------------------------------------------

_JOIN_BUTTONS = ('button[aria-label*="Join now" i]', 'button:has-text("Join now")',
                 '[data-tid="prejoin-join-button"]', 'button[aria-label*="Join" i]')
_MUTE_TOGGLES = ('[data-tid="toggle-mute"]', 'button[aria-label*="Mute" i]')
_CAMERA_TOGGLES = ('[data-tid="toggle-video"]', 'button[aria-label*="camera" i]')


def can_speak() -> bool:
    """Whether Asta can actually be heard in a call on this machine."""
    return bool(AUDIO_DEVICE)


def speaking_hint() -> str:
    return ("Speaking in calls needs a virtual microphone Teams can select "
            "(BlackHole or Loopback on macOS), then ASTA_CALL_AUDIO_DEVICE set to "
            "its name in .env. Until then I can join and listen, but I cannot say "
            "anything.")


#: The live call, if there is one. Held in the module rather than only in kv
#: because a browser context is not serialisable — and without the handle, nothing
#: can later hang up or notice the call ended. The kv row is the durable "am I in
#: a call" flag that survives a restart; this is what can act on it.
_CALL: dict = {}


async def join(join_url: str, muted: bool = True, camera: bool = False) -> str:
    """Join a meeting from its join link. Muted with the camera off by default.

    Those defaults are not a preference. An assistant that joins someone's call
    with an open mic broadcasts whatever his laptop can hear to everyone in it,
    and a camera-on join shows a room he did not agree to show.

    The browser context stays OPEN on success — closing it is what leaving a call
    means — and is handed to `watch`, which hangs up when the call ends or when
    the ceiling is reached, whichever comes first.
    """
    from . import teams_bridge
    if not (join_url or "").startswith("http"):
        raise RuntimeError("need the meeting's join link")
    if _CALL:
        raise RuntimeError("already in a call — leave that one first")
    warm_the_voice()                  # cold start lands here, not in the meeting
    await teams_bridge.close_pool()   # one writer per profile — see call_person
    pw, ctx = await teams_bridge._launch(headless=False)   # a call needs a real window
    joined = False
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(join_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)
        if muted:
            await _click_first(page, _MUTE_TOGGLES)
        if not camera:
            await _click_first(page, _CAMERA_TOGGLES)
        if not await _click_first(page, _JOIN_BUTTONS, timeout=6000):
            raise RuntimeError("couldn't find the Join button — did not join")
        joined = True
        # `speaks` is False for a JOINED meeting. Asta placing a call is Asta
        # having a conversation on his behalf; Asta sitting in on a meeting is a
        # room full of people who did not ask for an assistant's opinion, and he
        # may well be in it himself. Silence is the default, and `say_in_call`
        # with words he gave is how it gets broken.
        _CALL.update(pw=pw, ctx=ctx, page=page, url=join_url,
                     joined_at=_now(), captions=[],
                     answered_at=_now(), speaks=False,
                     who="")
        store.kv_set("teams_in_call", join_url)
        # Captions are what make a recap possible at all. Failing to turn them on
        # is not a reason to abandon a call that has already been joined, so it
        # is recorded and reported later rather than raised now.
        _CALL["captions_on"] = await start_captions(page)
        note = "" if _CALL["captions_on"] else " (no live captions — recap will be thin)"
        return ("joined (muted, camera off)" if muted else "joined") + note
    finally:
        if not joined:
            await ctx.close()
            await pw.stop()


#: The call controls in an open chat header.
_CALL_BUTTONS = {
    # Captured off the live chat header, not guessed. The previous set required a
    # <button> TAG — 'button[aria-label="Audio call"]' — and Teams renders this
    # as a div with role=button, so every selector missed, _click_first returned
    # False, and the call was reported as "clicked but never started". The
    # data-tid is the stable one; the tagless aria selectors are the fallbacks.
    "audio": ['[data-tid="default-chat-call-audio-button"]',
              '[aria-label="Audio call"]', '[aria-label*="Audio call" i]',
              '[data-tid="calling-audio-button"]'],
    # Same tag problem as audio above.
    "video": ['[data-tid="default-chat-call-video-button"]',
              '[aria-label="Video call"]', '[aria-label*="Video call" i]',
              '[data-tid="calling-video-button"]'],
}
#: Proof a call SCREEN is up, rather than a button having been clicked. Present
#: from the moment Teams starts dialling — so it proves the call was placed, and
#: says nothing at all about whether anybody picked up.
_CALL_PLACED = ('[data-tid="calling-hangup-button"], [aria-label*="Hang up" i], '
                '[data-tid="call-duration"], [data-tid="calling-screen"]')

#: Still ringing. Matched on visible text because that is the part of a calling
#: screen Teams has reworded least.
_RINGING = re.compile(r"\bringing\b|\bcalling\b|waiting for (others|them)", re.I)

#: Somebody answered. UNVERIFIED against a live connected call — nobody was rung
#: to find out — so it is deliberately not the only evidence `connected` accepts.
#: The reliable half is captions: a caption line existing means a human is talking,
#: which cannot happen before the call connects. If these selectors turn out to be
#: wrong the cost is a slower answer, not a wrong one.
_CONNECTED = '[data-tid="call-duration"], [data-tid="calling-timer"]'

#: A running m:ss timer with no children — Teams renders the call clock this way
#: whatever it names the element that day.
_TIMER_JS = """() => {
    for (const n of document.querySelectorAll('span,div')) {
        if (n.children.length) continue;
        if (/^\\d{1,2}:\\d{2}(:\\d{2})?$/.test((n.innerText || '').trim())) return true;
    }
    return false;
}"""


async def call_state(page) -> str:
    """Where the call is: 'ringing', 'connected', 'ended', or 'unknown'.

    Four states rather than a bool because the honest answer is sometimes "I
    cannot tell", and the two ways of being wrong are not equally bad. Reporting
    a ringing call as connected makes Asta talk to a phone nobody has picked up.
    Reporting a connected call as ringing costs a few seconds of silence. So
    'connected' is only ever returned on positive evidence, and everything else
    that is not clearly ringing or ended is admitted as 'unknown'.
    """
    if page is None:
        return "unknown"
    try:
        text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return "ended"                 # the page is gone; that counts as ended
    if _ENDED.search(text[:4000]):
        return "ended"
    # A caption cannot exist before somebody is talking, and nobody talks into a
    # phone that is still ringing. This is the one piece of connection evidence
    # that runs on code already proven against live Teams.
    if _CALL.get("captions"):
        return "connected"
    with contextlib.suppress(Exception):
        if await page.query_selector(_CONNECTED):
            return "connected"
    if _RINGING.search(text[:4000]):
        return "ringing"
    with contextlib.suppress(Exception):
        if await page.evaluate(_TIMER_JS):
            return "connected"
    return "unknown"


async def wait_for_answer(page, seconds: float = 0) -> str:
    """Wait for somebody to pick up. Returns the state it settled on.

    'no answer' is only returned when the call is still visibly RINGING at the
    deadline — an unreadable call screen returns 'unknown' and is left alone,
    because hanging up on a call that is actually connected is a far worse
    outcome than staying on one that is not.
    """
    deadline = _now() + (seconds or RING_SECONDS)
    state = "unknown"
    while _now() < deadline:
        state = await call_state(page)
        if state in ("connected", "ended"):
            return state
        await asyncio.sleep(1.0)
    return "no answer" if state == "ringing" else state


#: How long to let a headed Teams window finish painting its chat list.
_CHAT_LIST_ATTEMPTS = 15
_CHAT_LIST_POLL = 0.6


async def _wait_for_chat_list(page) -> bool:
    """Wait until the chat rail has real entries in it.

    The app shell renders before the chat list is populated, and Teams' search
    returns nothing while that is still true. Waiting on the CONDITION rather
    than on a duration is the difference between a call that works and one that
    reports "no person match" for somebody who is plainly there.
    """
    for _ in range(_CHAT_LIST_ATTEMPTS):
        try:
            if await page.evaluate(
                    """() => document.querySelectorAll('[role="treeitem"]').length > 3"""):
                return True
        except Exception:
            pass                       # navigation in flight — look again
        await asyncio.sleep(_CHAT_LIST_POLL)
    return False


#: How many times to re-search when the headed window opens the wrong chat.
_FIND_ATTEMPTS = int(os.environ.get("ASTA_CALL_FIND_ATTEMPTS", "4"))
_FIND_BACKOFF = 1.5


async def _find_chat_settled(page, who: str) -> str:
    """Find the chat, retrying while the headed window is still settling.

    Placing a real call to Vinish is what exposed this. Headless `resolve` finds
    "Vinish Kumar" every time; the HEADED window opened a chat called 'Author' and
    aborted — correctly, because opening the wrong conversation and dialling it
    would ring a stranger on Arun's behalf. But aborting on the FIRST mismatch
    made calling by name fail every time rather than merely be slow.

    `_wait_for_chat_list` waits for the rail to have entries, which is a proxy: it
    proves there are items, not that search has settled on the right one. Rather
    than tune that proxy — the next Teams build would move it again — the real
    condition is used directly. The title check already knows whether the right
    chat is open, so a mismatch simply means "too early", and too early is worth
    retrying.

    The refusal is preserved exactly: after the last attempt the mismatch is
    raised, and nothing is ever dialled on a chat whose title did not match.
    """
    from . import teams_bridge
    last: Exception | None = None
    for attempt in range(_FIND_ATTEMPTS):
        try:
            return await teams_bridge._find_chat(page, who, allow_group=False)
        except RuntimeError as exc:
            last = exc
            # Only a "wrong chat / not found" is a timing problem. A refusal on
            # policy — a group, an ambiguous name — must not be retried into
            # succeeding, because retrying does not make it any more what he meant.
            if "group" in str(exc).lower() or "ambiguous" in str(exc).lower():
                raise
            if attempt == _FIND_ATTEMPTS - 1:
                break
            await asyncio.sleep(_FIND_BACKOFF * (attempt + 1))
            with contextlib.suppress(Exception):
                await page.keyboard.press("Escape")   # drop a half-open search
    raise RuntimeError(
        f"{last} — still wrong after {_FIND_ATTEMPTS} attempts; nothing was dialled")


async def call_person(who: str, video: bool = False) -> str:
    """Ring a PERSON on Teams. Returns who it actually rang.

    Lives here rather than in the bridge because a call is not a message: the
    browser context has to STAY OPEN for the call to continue, and closing it is
    what hanging up means. So it follows `join` exactly — same profile, real
    window, registered in `_CALL` so `leave()` ends it.

    Groups are refused outright. Not as policy — "call the group" dials every
    member at once, and there is no reading of that which is what he meant unless
    he said so in those words.

    Verified like a send, because a clicked button is not a ringing phone. If no
    call UI appears this reports NOT placed: believing he rang someone he did not
    leaves a colleague waiting for a call that was never coming.
    """
    from . import teams_bridge
    if not teams_bridge.enabled():
        raise RuntimeError("Teams bridge is off (set TEAMS_BRIDGE=1 in .env)")
    if _CALL:
        raise RuntimeError("already in a call — leave that one first")
    kind = "video" if video else "audio"
    # Pay the model's cold start NOW, while the browser is still opening and the
    # phone has not even rung. Measured: the first utterance after Voicebox starts
    # takes 11.4 seconds, every later one takes 1.05 — so the cost is a one-time
    # model load, not synthesis, and the only question is whether it lands in
    # front of Vinish or in front of nobody. It was wired into join_by_phrase and
    # nowhere else, so every CALL paid it out loud.
    warm_the_voice()
    # A headed call cannot share the profile with the pooled headless browser.
    # Chromium tolerates exactly one writer per user-data-dir, and the pool
    # deliberately keeps its browser alive between operations — so by the time a
    # call is placed there is already one running, and the second launch either
    # fails or corrupts the store both are writing. That is precisely what left
    # Teams unable to boot for fourteen and a half hours on 26 August. Drop ours
    # first; the pool rebuilds itself on the next headless operation.
    await teams_bridge.close_pool()
    pw, ctx = await teams_bridge._launch(headless=False)   # a call needs a real window
    placed = False
    try:
        page = await teams_bridge._open_teams(ctx)
        # A HEADED window paints far slower than the headless one every other
        # code path uses: _open_teams returns as soon as the app shell exists,
        # and searching that early found nothing at all — "no person match for
        # 'Vinish' (saw: nothing)" on a name that resolves fine headless. Wait
        # for the chat rail to actually be populated, which is the condition
        # that makes search work, rather than sleeping a guessed number of
        # seconds and hoping.
        await _wait_for_chat_list(page)
        title = await _find_chat_settled(page, who)
        if not await _click_first(page, _CALL_BUTTONS[kind], timeout=5000):
            raise RuntimeError(
                f"no {kind} call button in the chat with '{title}' — either the Teams "
                f"UI changed or calling is not available for this account")
        try:
            await page.wait_for_selector(_CALL_PLACED, timeout=25000)
        except Exception as exc:
            raise RuntimeError(f"clicked {kind} call for '{title}' but no call ever "
                               f"started — treat as NOT called") from exc
        placed = True
        # `speaks` is True because a call Asta placed on his behalf is one he is
        # not on. The moment he is heard in it this flips off for good — see
        # `_note_speaker`. Ringing is not talking, so nothing is said until
        # `wait_for_answer` has seen somebody pick up.
        _CALL.update(pw=pw, ctx=ctx, page=page, url=f"teams-call:{title}",
                     joined_at=_now(), captions=[],
                     answered_at=0.0, speaks=True, who=title)
        store.kv_set("teams_in_call", f"call:{title}")
        _CALL["captions_on"] = await start_captions(page)
        warm_the_voice()
        return title
    finally:
        if not placed:
            await ctx.close()
            await pw.stop()


async def join_by_phrase(phrase: str, now_minutes: int | None = None) -> str:
    """Join the meeting he named — "join my 3pm", "join the standup".

    `join()` has always needed a URL and nothing ever produced one, so this
    capability was reachable only by pasting a link. Now the calendar answers it.

    Refuses on ambiguity rather than guessing. Joining the wrong call puts him in
    a room he did not mean to be in, in front of people who watch him arrive, and
    that is not an error anyone can quietly undo.
    """
    import datetime as _dt

    from . import agenda, outlook
    events = await outlook.todays_meetings(structured=True)
    if not events:
        raise RuntimeError("nothing on the calendar today")
    if now_minutes is None:
        now = _dt.datetime.now()
        now_minutes = now.hour * 60 + now.minute
    ev = agenda.pick(events, phrase, now_minutes)
    if not ev:
        listing = ", ".join(f"{e['start']} {e['title']}" for e in events[:6])
        raise RuntimeError(
            f"'{phrase}' doesn't pick out one meeting — today has: {listing}")
    if not ev.get("join_url"):
        raise RuntimeError(
            f"found '{ev['title']}' at {ev['start']} but the calendar row carries no "
            f"join link — open it in Outlook and send me the link")
    return f"{await join(ev['join_url'])} — {ev['title']} ({ev['start']})"


async def _click_first(page, selectors, timeout: float = 3000) -> bool:
    """Click the first selector that works.

    page.click() rather than wait_for_selector().click(): the handle form resolves
    an element and then clicks whatever that handle still points at, which is
    nothing at all once the surrounding UI has re-rendered. That exact pattern is
    what left the Teams activity watcher silently dead. Meeting join controls
    re-render constantly as the pre-join screen settles, so it is the same race.
    """
    for sel in selectors:
        try:
            await page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    return False


# --- borrowing the microphone -----------------------------------------------
#
# Teams listens to ONE input at a time. While it is pointed at the virtual mic
# Asta speaks through, it cannot hear Arun at all — so leaving it there after an
# utterance would silently mute him for the rest of the call, and he would find
# out when somebody asked why he had gone quiet.
#
# So the mic is BORROWED: switched immediately before speaking and given back in
# a finally, whatever happened in between. The restore is the part that matters;
# the switch merely fails to be heard, the missing restore fails to be heard FROM.

#: His real microphone — restored after every utterance.
HIS_MIC = os.environ.get("ASTA_HIS_MIC", "MacBook Pro Microphone")

#: The macOS input switcher (brew install switchaudio-osx). Teams must have its
#: microphone left on "Same as System" for this to reach it.
SWITCH_AUDIO = os.environ.get("ASTA_SWITCH_AUDIO", "/opt/homebrew/bin/SwitchAudioSource")


def can_switch_mic() -> bool:
    """Whether the input device can be changed at all."""
    from pathlib import Path as _P
    return _P(SWITCH_AUDIO).is_file()


async def set_call_mic(page=None, device: str = "") -> bool:
    """Point the microphone at `device`. False when it could not be done.

    This switches the SYSTEM default input rather than Teams' own setting.
    Driving the Teams UI was the first attempt and it does not survive contact:
    settings sit behind a React flyout off the "Settings and more" menu, the
    picker has no stable data-tid, and a pre-flight against live Teams returned
    False on every selector — meaning a call would have connected and then sat
    silent. Teams follows the system default when its device is left on "Same as
    System", so this is both simpler and one less thing to break when Teams
    ships a UI change.

    `page` is accepted and ignored so callers and tests keep the same shape.

    Returns a bool rather than raising: the caller must tell "could not switch,
    so do not speak" apart from "spoke and then could not restore", and those
    two want very different reactions.
    """
    if not device:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            SWITCH_AUDIO, "-t", "input", "-s", device,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=10)
        if proc.returncode != 0:
            return False
    except Exception:
        return False
    # Verified, not assumed: the switcher exits 0 for a name it did not apply.
    return (await current_mic()) == device


async def current_mic() -> str:
    """Whatever the system input is right now ('' if it cannot be read)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            SWITCH_AUDIO, "-c", "-t", "input",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return out.decode(errors="replace").strip()
    except Exception:
        return ""


async def _restore_mic(page) -> None:
    """Give the microphone back. Shouts if it could not — being silently muted
    for the rest of a call is the worst outcome this module can produce."""
    from . import notify
    if await set_call_mic(page, HIS_MIC):
        return
    await notify.notify(
        f"🎙️ Could not switch the Teams mic back to {HIS_MIC} — you may be muted "
        f"to the call. Set it manually in Teams → Settings → Devices.",
        "warn", urgency="direct")


def call_duration() -> float:
    """Seconds since somebody picked up. Zero if nobody did.

    Zero for an unanswered call is the honest number rather than a missing one —
    "rang Vinish for 45 seconds" is not a 45-second call, and reporting it as one
    would put a conversation in his day that never happened.
    """
    started = float(_CALL.get("answered_at") or 0)
    if not started:
        return 0.0
    return max(0.0, _now() - started)


def spoken_duration(seconds: float) -> str:
    """A duration the way he would say it, not the way a computer would."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m" if not secs else f"{minutes}m {secs}s"


def may_speak() -> bool:
    """Whether Asta is allowed to open its mouth in this call on its own.

    Separate from `can_speak()`, which is about whether the machine has a virtual
    microphone at all. This is about whether it is his conversation.
    """
    return bool(_CALL) and bool(_CALL.get("speaks"))


def _note_speaker(speaker: str) -> None:
    """Notice Arun talking, and shut up for the rest of the call.

    A one-way latch on purpose. He said it plainly: while he is speaking to
    people, Asta does not talk — it listens and sends him what he needs. Anything
    that could turn speech back on mid-call is a way for Asta to interrupt him,
    so there isn't one; the call ending is what clears it.
    """
    if not _CALL or not _CALL.get("speaks"):
        return
    if speaker_is_arun(speaker):
        _CALL["speaks"] = False
        _CALL["muted_because"] = "Arun is on the call"


#: Whatever Teams calls him in a caption. His display name varies by tenant, so
#: this is configurable rather than hard-coded to one spelling.
HIS_NAMES = tuple(n.strip().lower() for n in
                  os.environ.get("ASTA_HIS_TEAMS_NAMES", "arun,arun k,arun kumar").split(",")
                  if n.strip())


def speaker_is_arun(speaker: str) -> bool:
    name = (speaker or "").strip().lower()
    return bool(name) and any(n and (n == name or n in name) for n in HIS_NAMES)


# --- being quick enough to be worth saying -----------------------------------
#
# Measured on this machine rather than assumed, because the design changed once
# the numbers came in:
#
#   kokoro (assistant), cold ....... 8.9s for a 3.5s line
#   kokoro (assistant), warm ....... 1.1s for a 3.5s line
#   chatterbox (his clone) ......... 9-15s, warm or cold
#   microphone switch .............. 0.38s, restore 0.27s
#
# Two conclusions, both of which are now enforced below. The cold-start penalty is
# most of the latency and is pure waste — one throwaway synthesis at the start of
# a call buys back eight seconds on the first real line. And his cloned voice is
# an order of magnitude too slow to carry a spontaneous sentence, so autonomous
# speech uses the assistant voice; the clone is for words he composed in advance,
# where ten seconds costs nothing.

_VOICE_CACHE: dict[str, bytes] = {}

#: Said the moment somebody asks for something Asta cannot answer on the spot.
#: A fixed set precisely so they can be synthesised once and replayed instantly —
#: this is the line that keeps dead air off the call while the real answer is
#: still being worked out, so it has to be the fastest thing in the module.
HOLDING_LINES = {
    "review": "Sure, give me a few minutes, I'll check and come back on it.",
    "checking": "Let me check that and come back to you.",
    "his": "I'll get that to Arun and come back to you.",
}


def _cache_key(text: str, voice_name: str) -> str:
    return f"{voice_name}:{hashlib.sha1(text.encode()).hexdigest()[:16]}"


async def synth(text: str, voice_name: str = "") -> bytes:
    """Speech for `text`, from memory when it has been said before.

    The holding lines are said in most calls and never change, so synthesising
    them more than once is buying the same 1.1 seconds over and over.
    """
    from . import voice
    key = _cache_key(text, voice_name or voice.VOICE_ASSISTANT)
    cached = _VOICE_CACHE.get(key)
    if cached:
        return cached
    audio = await voice.speak(text, voice=voice_name or voice.VOICE_ASSISTANT)
    if audio:
        _VOICE_CACHE[key] = audio
    return audio


def warm_the_voice() -> None:
    """Pay the cold-start cost now, while the phone is still ringing.

    Fire-and-forget: nothing waits on it, and a failure here must never stop a
    call from being placed. Worst case the first line is slow, which is exactly
    what happens today.
    """
    async def _warm():
        from . import voice
        with contextlib.suppress(Exception):
            for line in HOLDING_LINES.values():
                # Warmed through the SAME transformation `say_in_call` applies.
                # It strips a trailing instruction and its punctuation, so warming
                # the raw line caches it under text that is never requested — the
                # cache stays full, every lookup misses, and the eight seconds this
                # exists to save get paid anyway.
                await synth(voice.strip_voice_instruction(line))
    with contextlib.suppress(RuntimeError):        # nothing to warm without a loop
        task = asyncio.get_running_loop().create_task(_warm())
        _REACTING.add(task)                        # asyncio holds only a weak ref
        task.add_done_callback(_REACTING.discard)


async def say_in_call(text: str, voice_name: str = "") -> str:
    """Say something out loud in the call Asta is in — only if he asked for it.

    This used to generate the audio and then DROP it: no playback, no device, and
    a cheerful "said it in the call" either way. Its own docstring warned about a
    milder version of the same thing. So the contract now is that the function
    returns only after the audio has finished playing into the virtual mic Teams
    is listening to, and raises on every other path — he must never be told a
    point was made in a call when nothing was said.

    `voice_name` is "mine" (his clone) or "assistant". Unrecognised → assistant,
    never his voice by accident.
    """
    from . import voice
    if not can_speak():
        raise RuntimeError("no virtual microphone configured — " + speaking_hint())
    if not store.kv_get("teams_in_call"):
        raise RuntimeError("not in a call")

    # Talking into a phone that is still ringing. The old code could not tell the
    # difference — the hangup button appears the instant Teams starts dialling —
    # so a call placed and spoken into straight away delivered a monologue to
    # nobody and reported it as said.
    page = _CALL.get("page")
    if page is not None:
        state = await call_state(page)
        # Checked on EVERY line, not just the first. A call can die under Asta's
        # feet — the far end hangs up, or the Mac sleeps and takes the browser
        # with it — and speaking into the corpse would play audio into a virtual
        # microphone nobody is listening to and report it as said.
        if state == "ended":
            raise RuntimeError("the call has ended — said nothing")
        if not _CALL.get("answered_at"):
            if state != "connected":
                raise RuntimeError(f"nobody has picked up yet ({state}) — said nothing")
            _CALL["answered_at"] = _now()

    words = voice.strip_voice_instruction(text)
    if not words:
        raise RuntimeError("nothing left to say once the instruction was removed")
    chosen = voice.pick_voice(text, voice_name or voice.VOICE_ASSISTANT)

    # Generated BEFORE the mic is borrowed: synthesis is the slow part, and
    # holding his microphone hostage for ten seconds of Chatterbox would mute him
    # mid-conversation for no reason.
    audio = await synth(words, chosen)
    if not audio:
        raise RuntimeError("speech generation produced nothing — said nothing")

    borrowed = False
    if page is not None:
        borrowed = await set_call_mic(page, AUDIO_DEVICE)
        if not borrowed:
            raise RuntimeError(
                f"could not point Teams at {AUDIO_DEVICE} — said nothing. "
                f"Set it manually in Teams → Settings → Devices.")
    try:
        # Blocking: it has to have FINISHED before this reports that it spoke, or
        # a second line starts over the top of the first.
        played = await asyncio.to_thread(voice.play_to_device, audio, AUDIO_DEVICE)
    finally:
        # Whatever happened above — including an exception — he gets his
        # microphone back. This is the line that keeps him from going silently
        # mute for the rest of the call.
        if borrowed:
            await _restore_mic(page)

    store.kv_set("teams_last_spoken", words[:500])
    return f"said it in the call in {chosen} voice ({played:.1f}s): {words[:120]}"


#: The transcript of the call that just finished. Kept OUTSIDE `_CALL` because
#: leaving clears that dict, and the recap is wanted precisely after the hang-up
#: — losing the transcript at the moment it becomes useful would be perfect.
_LAST_TRANSCRIPT: list[str] = []


def last_transcript() -> str:
    """What was said in the most recent call Asta sat in on."""
    return _LAST_TRANSCRIPT[0] if _LAST_TRANSCRIPT else ""


#: What the call that just ended was. Kept for the same reason the transcript is:
#: `leave()` clears `_CALL`, and how long he was on the phone is wanted precisely
#: after the hang-up.
_LAST_CALL: dict = {}


def last_call() -> dict:
    """Who the last call was with, whether it was answered, and how long it ran."""
    return dict(_LAST_CALL)


async def leave() -> str:
    """Hang up. Closing the browser context IS leaving the call."""
    store.kv_set("teams_in_call", "")
    call = dict(_CALL)
    text = transcript_text(call.get("captions") or [])
    _LAST_TRANSCRIPT[:] = [text] if text else []
    # Read while `_CALL` is still standing — a moment later there is nothing to
    # measure, and a call reported without its duration is a call he cannot judge.
    _LAST_CALL.clear()
    _LAST_CALL.update(who=call.get("who") or "", seconds=call_duration(),
                      answered=bool(call.get("answered_at")),
                      spoke=bool(call.get("speaks")))
    _CALL.clear()
    if not call:
        return "not in a call"
    for close in (call.get("ctx"), call.get("pw")):
        try:
            await (close.close() if hasattr(close, "close") else close.stop())
        except Exception:
            pass
    return "left the call"


# Markers that the call is over. Matched on visible text because Teams' post-call
# screen has been restyled more often than it has been reworded.
_ENDED = re.compile(r"call ended|meeting ended|you (have )?left|rejoin", re.I)


async def call_ended(page) -> bool:
    try:
        text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return True          # the page is gone; that counts as ended
    return bool(_ENDED.search(text[:4000]))


def overran(now: float | None = None) -> bool:
    """True once the call has run past the ceiling."""
    if not _CALL:
        return False
    started = float(_CALL.get("joined_at") or 0)
    elapsed = ((_now() if now is None else now) - started) / 60
    return elapsed >= MAX_CALL_MINUTES


# --- live captions -----------------------------------------------------------
#
# Teams will not hand out a transcript unless someone recorded the meeting, which
# is why a recap used to be an offer to go and look for one rather than an answer.
# But the web client renders live captions into the DOM, and reading the DOM is
# something this codebase already does deterministically and for free. So the
# transcript is built by watching captions go past, same as the activity feed.
#
# It is a genuine transcript with genuine limits: captions only exist while Asta
# is in the call, they are speech recognition rather than truth, and they start
# when captions are turned on rather than when the meeting did. Every one of those
# is stated to Arun instead of being smoothed over.

#: Specific first, and the data-tid leads for a second reason beyond ordering:
#: aria-labels are LOCALISED. "Turn on live captions" matches an English UI and
#: nothing else, while `closed-caption-button` is the same string in every locale.
_CAPTION_TOGGLES = [
    '[data-tid="closed-caption-button"]',
    'button[aria-label*="Turn on live captions" i]',
    'div[role="menuitem"][aria-label*="captions" i]',
]
#: SPECIFIC FIRST — `_click_first` takes the first match, so order is behaviour.
#: Probed live: the call screen carries a bare `button[aria-label="More"]`
#: belonging to the APP BAR as well as the call toolbar's own button. Generic was
#: listed first, so opening the call's More menu clicked the app bar, found no
#: "Language and speech", and start_captions returned False — Asta could talk in
#: a call and never hear a word back.
_MORE_MENU = ['[data-tid="callingButtons-showMoreBtn"]',
              'button[aria-label*="More actions" i]',
              'button[aria-label="More"]']
_LANGUAGE_MENU = ['div[role="menuitem"][aria-label*="Language and speech" i]',
                  'div[role="menuitem"][aria-label*="language" i]']
#: One rendered caption line: who spoke, and what the recogniser heard.
_CAPTION_ROWS = ('[data-tid="closed-caption-v2-window"] [data-tid="closed-caption-text"], '
                 '[data-tid="closed-caption-text"], '
                 '[class*="closedCaption"] [class*="captionText"]')


async def start_captions(page) -> bool:
    """Turn live captions on. False when they could not be enabled.

    Tries the direct button first — it exists once the call toolbar is settled —
    and only then goes hunting through More → Language and speech, because menu
    walking is the part most likely to break when Teams reorganises its toolbar.
    """
    if await _click_first(page, _CAPTION_TOGGLES, timeout=3000):
        return True
    if not await _click_first(page, _MORE_MENU, timeout=4000):
        return False
    await asyncio.sleep(1.0)
    await _click_first(page, _LANGUAGE_MENU, timeout=3000)
    await asyncio.sleep(1.0)
    return await _click_first(page, _CAPTION_TOGGLES, timeout=3000)


def _merge_caption(lines: list[dict], speaker: str, text: str) -> None:
    """Fold a caption row into the transcript.

    Captions are not appended, they are REVISED — a line grows word by word as the
    recogniser catches up, so the same utterance is rendered a dozen times, each a
    little longer. Appending every poll produces a transcript of stutters. When the
    newest line from a speaker is a prefix of what just arrived, it is the same
    sentence still being written, so it is replaced rather than added.
    """
    text = " ".join((text or "").split())
    if not text:
        return
    for prev in reversed(lines):
        if prev["speaker"] != speaker:
            break                      # somebody else spoke; this is a new utterance
        if text.startswith(prev["text"]) or prev["text"].startswith(text):
            prev["text"] = max(text, prev["text"], key=len)
            return
        break
    lines.append({"speaker": speaker, "text": text})


async def poll_captions(page, lines: list[dict]) -> None:
    """Read whatever captions are on screen into `lines`."""
    try:
        rows = await page.evaluate(
            """(sel) => Array.from(document.querySelectorAll(sel)).map(n => {
                   const item = n.closest('[data-tid="closed-caption-message"]')
                             || n.closest('li') || n.parentElement || n;
                   const who = item.querySelector(
                       '[data-tid="author"], [class*="authorName"], [class*="displayName"]');
                   return {speaker: ((who && who.innerText) || '').trim(),
                           text: (n.innerText || '').trim()};
               })""", _CAPTION_ROWS)
    except Exception as exc:
        # A caption read must never end the call watch — but a reader that has
        # silently failed for the whole call is why a recap comes back empty and
        # nobody knows why.
        quiet.note("call.poll_captions", exc)
        return
    for r in rows:
        speaker = r.get("speaker") or "someone"
        # Hearing him is what closes Asta's mouth for the rest of the call. Done
        # here rather than at the classification step because it must happen for
        # EVERY caption, including the small talk that never gets classified.
        _note_speaker(speaker)
        _merge_caption(lines, speaker, r.get("text") or "")


def transcript_text(lines: list[dict]) -> str:
    return "\n".join(f"{l['speaker']}: {l['text']}" for l in lines if l.get("text"))


# --- noticing what the other person asked ------------------------------------
#
# Arun wanted this to feel natural: Asta listens while he talks, spots something
# worth looking up, and asks HIM — "can I analyse this that Vinish asked?" — on
# his phone, never out loud in the call.
#
# The split below is the whole design. Some questions Asta can answer from the
# workspace context and it is genuinely useful for it to offer. Others are about
# what ARUN will do — review it, merge it, be free at four — and answering those
# on his behalf would commit him to things he never agreed to. Those are listened
# to, logged, and handed back afterwards.




















async def offer_to_analyse(item: dict) -> None:
    """Ask HIM, on his phone, whether to look something up. Never spoken aloud.

    Uses the same offer engine as CI failures, so "yes" from WhatsApp already
    means the right thing and he has one habit rather than two.
    """
    from . import notify, offers
    if item.get("kind") != "answerable":
        return
    line = item["line"][:200]
    o = offers.offer(
        "analyse",
        subject=f"asked in the call: {line[:80]}",
        context=line,
        prompt=f"Answer this from the workspace context, for Arun to read out: {line}",
    )
    await notify.notify(
        f"🎧 In the call — “{line}”\n\nWant me to look this up? (yes / no)"
        f"\n{o.render() if hasattr(o, 'render') else ''}".rstrip(),
        "call", urgency="direct")


# --- answering, while the call is still running ------------------------------
#
# The rule he set: while HE is speaking to people, Asta does not talk — but it
# still does the work and sends him what he needs. So the thinking happens either
# way and only the delivery changes. Silence is never idleness.













async def _say_quietly(line: str) -> bool:
    """Say a line if allowed to, and never let failing to say it break the call."""
    if not (may_speak() and can_speak()):
        return False
    try:
        await say_in_call(line)
        return True
    except Exception:
        return False








async def handle_ask(item: dict) -> str:
    """React to something asked in the call. Returns what was done, for tests.

    The holding line goes out FIRST and from cache, because it is the only part
    that can be fast. Thinking takes ten to thirty seconds; a pre-synthesised
    acknowledgement takes about four tenths of one, and it is the difference
    between a natural pause and dead air on a call.
    """
    from . import notify
    kind, line = item.get("kind"), item.get("line", "")

    # Things aimed at Arun are still never answered for him. When Asta is on the
    # call alone it says the thing he asked for — a few minutes, I'll check and
    # come back — which commits him to nothing and buys the time honestly.
    if kind == "his":
        # A holding line commits him to nothing — "give me a few minutes, I'll
        # check and come back" is true whatever the question turns out to be — so
        # it does not need the higher bar an actual answer does.
        said = await _say_quietly(HOLDING_LINES["review"])
        await notify.notify(f"🎧 In the call, for you — “{line[:200]}”"
                            + ("\n\nI said you'd come back on it." if said else ""),
                            "call", urgency="direct")
        return "held" if said else "sent to him"

    if kind != "answerable":
        return "ignored"

    # The bar for SPEAKING is higher than the bar for telling him, because the
    # two mistakes cost different things. Misjudging a line and buzzing his phone
    # costs a glance. Misjudging it and saying something out loud costs an
    # incorrect sentence in front of a colleague, in a conversation he cannot
    # take back. So a regex is enough to notify and not enough to speak.
    speaking = may_speak() and can_speak() and await confident(line)
    if speaking:
        await _say_quietly(HOLDING_LINES["checking"])

    started = _now()
    answer = await answer_from_knowledge(line)
    took = _now() - started

    if not answer:
        await offer_to_analyse(item)          # no brain answered; fall back to asking him
        return "offered"

    late = took > ANSWER_BUDGET_SECONDS
    if speaking and not late and may_speak():
        # `may_speak` is checked AGAIN deliberately: he may have started talking
        # during the twenty seconds this spent thinking, and the answer to that
        # is silence, not a sentence over the top of him.
        with contextlib.suppress(Exception):
            await say_in_call(spoken_form(answer))
            await notify.notify(f"🎧 Answered in the call — “{line[:120]}”\n\n{answer[:800]}",
                                "call", urgency="ambient")
            return "spoken"

    why = "took too long to say" if late else "you're on the call"
    await notify.notify(f"🎧 Asked in the call — “{line[:120]}”\n\n{answer[:800]}"
                        f"\n\n(not said out loud — {why})", "call", urgency="direct")
    return "sent to him"





def captured_transcript() -> str:
    """The transcript built during the call Asta is in (empty if none)."""
    return transcript_text(_CALL.get("captions") or [])


async def watch(poll_seconds: float = 30) -> str:
    """Sit in the call until it ends, then leave and say why.

    Two exits, and the second one is the one that matters. A meeting that ends
    normally is detected from the page; a meeting that never ends — because it
    overran, or because a selector changed and the end was never noticed — hits
    the ceiling. Without that, a single missed marker leaves Asta parked in
    someone's call for the rest of the day with his camera light on.

    Captions are polled far more often than the end-of-call check, because a
    caption that scrolls out of the window between polls is gone for good, while
    a call that ended thirty seconds ago is merely thirty seconds stale.
    """
    if not _CALL:
        return "not in a call"
    page = _CALL.get("page")
    lines = _CALL.setdefault("captions", [])
    ticks = max(1, int(poll_seconds // CAPTION_POLL_SECONDS) or 1)

    # A ringing call is neither "ended" nor "overran", so this loop sat on one
    # until MAX_CALL_MINUTES — ninety minutes of ringing a colleague who was away
    # from their desk. `wait_for_answer` knew how to spot it; nothing asked.
    # Conservative in the same direction it already chose: only a call still
    # VISIBLY ringing is dropped, since hanging up on a live call cannot be undone.
    if page is not None and not _CALL.get("answered_at"):
        state = await wait_for_answer(page)
        if state == "no answer":
            await leave()
            return f"no answer after {int(RING_SECONDS)}s — hung up"
        if state == "ended":
            await leave()
            return "the call ended"

    while _CALL:
        if overran():
            await leave()
            return f"left — the call passed {MAX_CALL_MINUTES} minutes"
        if page is not None and await call_ended(page):
            await leave()
            return "the call ended"
        for _ in range(ticks):
            if not _CALL:
                break
            if page is not None:
                await poll_captions(page, lines)
                react_to(lines)
            await asyncio.sleep(CAPTION_POLL_SECONDS)
    return "left the call"


#: Handling one ask can take half a minute of thinking. Captions scroll out of
#: their window in seconds, so reacting must never happen inline with polling —
#: these run alongside it, and asyncio holds only a WEAK reference to a bare task,
#: so the set is what stops them being garbage collected mid-thought.
_REACTING: set = set()
_ASK_LOCK = asyncio.Lock()


def react_to(lines: list[dict]) -> None:
    """Start handling anything newly asked, without stalling caption polling."""
    heard = [l["text"] for l in lines
             if l.get("text") and not speaker_is_arun(l.get("speaker", ""))]
    for item in notice_asks(heard):
        task = asyncio.create_task(_handle_one(item))
        _REACTING.add(task)
        task.add_done_callback(_REACTING.discard)


async def _handle_one(item: dict) -> None:
    """One ask at a time — two answers spoken over each other is worse than slow."""
    async with _ASK_LOCK:
        with contextlib.suppress(Exception):
            await handle_ask(item)


# --- afterwards --------------------------------------------------------------

async def watch_and_report(title: str = "") -> None:
    """Sit in the call, then tell him it's over and offer the part he wants.

    Deliberately an OFFER rather than an automatic summary. Asta has no transcript
    unless Teams was recording and he has it — so promising a recap it cannot
    produce would be the confident lie. It says the call ended, and offers to go
    and summarise what was actually said if there is anything to read.
    """
    from . import notify, offers
    try:
        why = await watch()
    except Exception:
        why = "lost track of the call"
        await leave()
    ended = last_call()
    label = f" — {title or ended.get('who') or ''}".rstrip(" —")
    if ended.get("seconds"):
        why = f"{why} · {spoken_duration(ended['seconds'])}"
    elif ended.get("who"):
        why = f"{why} · never answered"
    text = last_transcript()
    if text:
        # There IS a record now, captured while sitting in the call, so the offer
        # is to summarise something real rather than to go looking for something
        # that probably does not exist.
        action = (f"Arun was not in this call{label}; Asta sat in on it and captured the "
                  f"live captions below. Report ONLY the parts that concern him: decisions "
                  f"affecting his work, anything assigned to him, questions left open for "
                  f"him. Captions are speech recognition — quote them as heard, and if a "
                  f"line is too garbled to be sure of, say so rather than tidying it into "
                  f"something confident.\n\nTranscript:\n{text[:12000]}")
        question = "Want the parts that concern you?"
        context = f"{why} · captured {len(text.split())} words of captions"
    else:
        action = (f"Arun was not in this call{label}; Asta sat in on it but captured no "
                  f"captions. Read whatever record exists — the meeting chat, the Teams "
                  f"recap or transcript if one was recorded — and report ONLY the parts "
                  f"that concern him. If there is no record to read, say so plainly rather "
                  f"than reconstructing it. Do not summarise the rest of the meeting.")
        question = "Want me to go through what was said and pull out anything for you?"
        context = f"{why} · no captions captured"
    offers.propose(subject=f"📞 Call ended{label}", context=context,
                   question=question, action=action)
    await notify.notify(offers.pending().render(), "calls", urgency="ambient")


async def drop_call_lost_to_sleep(gap: float) -> str:
    """End a call the Mac slept through, and say so. '' if there was no call.

    A call cannot survive sleep: the browser, the audio devices and the network
    all stop, and Teams drops the far end within seconds. What does survive is
    Asta's belief that it is still on the call — `teams_in_call` set, a dead
    browser handle in `_CALL` — and the next call is then refused as "already in
    a call" with no way back but a restart.

    Told to him rather than cleaned up quietly. He was on a call with somebody
    and it ended without either of them ending it; finding that out from silence
    is how a colleague gets left talking to nobody.
    """
    from . import notify
    if not (store.kv_get("teams_in_call") or _CALL):
        return ""
    who = _CALL.get("who") or ""
    with contextlib.suppress(Exception):
        await leave()
    label = f" with {who}" if who else ""
    await notify.notify(
        f"📞 The Mac slept for {int(gap // 60)} min during a call{label} — the call "
        f"is gone. Nothing was said or heard after it dropped.", "call", urgency="direct")
    return who or "the call"


async def call_watch(title: str = "") -> None:
    """The whole life of a call Asta placed: ring, answer, sit, hang up, report.

    `call_person` only ever proved a call was DIALLED, and nothing was watching it
    afterwards — so an unanswered call left `teams_in_call` set, the browser
    context open and the next call refused as "already in a call", with no way
    back but a restart. This is the piece that was missing.

    The three endings are deliberately not treated alike. A call still visibly
    ringing at the deadline is hung up, because nobody is coming. A call whose
    state cannot be read is LEFT ALONE and reported, because hanging up on a
    conversation that is actually happening is much worse than staying on one
    that is not.
    """
    from . import notify
    page = _CALL.get("page")
    who = title or _CALL.get("who") or "them"
    state = await wait_for_answer(page)

    if state in ("no answer", "ended"):
        await leave()
        gone = "didn't pick up" if state == "no answer" else "ended before it connected"
        await notify.notify(f"📞 {who} {gone} — hung up after "
                            f"{int(RING_SECONDS)}s.", "call", urgency="direct")
        return

    if state == "connected":
        _CALL["answered_at"] = _now()
    else:
        # Staying on a call it cannot read, and saying so. Silence is enforced
        # because speaking would be talking into a call that may still be ringing.
        _CALL["speaks"] = False
        await notify.notify(
            f"📞 Called {who}, but I can't tell from the page whether they picked "
            f"up — staying on and listening, not speaking. Worth a look at the "
            f"call-screen selectors.", "call", urgency="direct")

    if not _CALL.get("captions_on"):
        await notify.notify(
            f"🎧 On the call with {who} but live captions wouldn't turn on — I "
            f"can't hear what's said, so there'll be no notes from this one.",
            "call", urgency="direct")

    await watch_and_report(title or who)


async def recap(transcript: str, title: str = "") -> tuple[str, bool]:
    """Summarise a finished call and say whether any of it was actually his.

    The filter is the point. He asked for what concerns HIM after a call, not
    minutes — a full recap of a meeting where nothing landed on him is exactly the
    "unwanted related details" he does not want pushed to his phone.
    """
    from . import agent as agent_mod
    body = await agent_mod.meeting_recap(transcript, title)
    return body, agent_mod._recap_needs_arun(body)
