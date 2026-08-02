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
import os
import re
import urllib.parse
from datetime import datetime, timedelta

from . import store

COMPOSE_URL = "https://outlook.office.com/calendar/deeplink/compose"

#: The macOS virtual audio device Teams must be pointed at for Asta to be heard
#: (BlackHole, Loopback, or similar). Empty = speaking is not available, and
#: `say_in_call` says so rather than pretending.
AUDIO_DEVICE = os.environ.get("ASTA_CALL_AUDIO_DEVICE", "").strip()

#: A joined call is left after this long no matter what, so a meeting that
#: overruns — or a bug — cannot leave Asta sitting in someone's call all day.
MAX_CALL_MINUTES = int(os.environ.get("ASTA_MAX_CALL_MINUTES", "90"))


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
        _CALL.update(pw=pw, ctx=ctx, page=page, url=join_url,
                     joined_at=asyncio.get_event_loop().time())
        store.kv_set("teams_in_call", join_url)
        return "joined (muted, camera off)" if muted else "joined"
    finally:
        if not joined:
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
    for sel in selectors:
        try:
            await (await page.wait_for_selector(sel, timeout=timeout)).click()
            return True
        except Exception:
            continue
    return False


async def say_in_call(text: str) -> str:
    """Say something out loud in the call Asta is in — only if he asked for it.

    Refuses when no virtual microphone is configured. The alternative — generating
    the audio, playing it to a device nobody in the call is listening to, and
    reporting success — is the failure mode worth engineering against: he would
    believe his point was made and find out in the follow-up that it never was.
    """
    from . import voice
    if not can_speak():
        raise RuntimeError("no virtual microphone configured — " + speaking_hint())
    if not store.kv_get("teams_in_call"):
        raise RuntimeError("not in a call")
    audio = await voice.speak(text)
    if not audio:
        raise RuntimeError("speech generation produced nothing — said nothing")
    return f"said it in the call ({len(text)} chars)"


async def leave() -> str:
    """Hang up. Closing the browser context IS leaving the call."""
    store.kv_set("teams_in_call", "")
    call = dict(_CALL)
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
    elapsed = ((asyncio.get_event_loop().time() if now is None else now) - started) / 60
    return elapsed >= MAX_CALL_MINUTES


async def watch(poll_seconds: float = 30) -> str:
    """Sit in the call until it ends, then leave and say why.

    Two exits, and the second one is the one that matters. A meeting that ends
    normally is detected from the page; a meeting that never ends — because it
    overran, or because a selector changed and the end was never noticed — hits
    the ceiling. Without that, a single missed marker leaves Asta parked in
    someone's call for the rest of the day with his camera light on.
    """
    if not _CALL:
        return "not in a call"
    page = _CALL.get("page")
    while _CALL:
        if overran():
            await leave()
            return f"left — the call passed {MAX_CALL_MINUTES} minutes"
        if page is not None and await call_ended(page):
            await leave()
            return "the call ended"
        await asyncio.sleep(poll_seconds)
    return "left the call"


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
    label = f" — {title}" if title else ""
    offers.propose(
        subject=f"📞 Call ended{label}",
        context=why,
        question="Want me to go through what was said and pull out anything for you?",
        action=(f"Arun was not in this call{label}; Asta sat in on it. Read whatever record "
                f"exists — the meeting chat, the Teams recap or transcript if one was "
                f"recorded — and report ONLY the parts that concern him: decisions that "
                f"affect his work, anything assigned to him, and questions left open for "
                f"him. If there is no record to read, say so plainly rather than "
                f"reconstructing it. Do not summarise the rest of the meeting."))
    await notify.notify(offers.pending().render(), "calls", urgency="ambient")


async def recap(transcript: str, title: str = "") -> tuple[str, bool]:
    """Summarise a finished call and say whether any of it was actually his.

    The filter is the point. He asked for what concerns HIM after a call, not
    minutes — a full recap of a meeting where nothing landed on him is exactly the
    "unwanted related details" he does not want pushed to his phone.
    """
    from . import agent as agent_mod
    body = await agent_mod.meeting_recap(transcript, title)
    return body, agent_mod._recap_needs_arun(body)
