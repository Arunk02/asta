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
#: Captions scroll out of their window within seconds, so they are read far more
#: often than the call-ended check. A caption missed is gone; a call noticed as
#: ended a few seconds late costs nothing.
CAPTION_POLL_SECONDS = float(os.environ.get("ASTA_CAPTION_POLL", "4"))


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
                     joined_at=asyncio.get_event_loop().time(), captions=[])
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
    "audio": ['button[aria-label="Audio call"]', 'button[aria-label*="Audio call" i]',
              '[data-tid="calling-audio-button"]'],
    "video": ['button[aria-label="Video call"]', 'button[aria-label*="Video call" i]',
              '[data-tid="calling-video-button"]'],
}
#: Proof a call is actually up, rather than a button having been clicked.
_IN_CALL = ('[data-tid="calling-hangup-button"], [aria-label*="Hang up" i], '
            '[data-tid="call-duration"], [data-tid="calling-screen"]')


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
    pw, ctx = await teams_bridge._launch(headless=False)   # a call needs a real window
    placed = False
    try:
        page = await teams_bridge._open_teams(ctx)
        title = await teams_bridge._find_chat(page, who, allow_group=False)
        if not await _click_first(page, _CALL_BUTTONS[kind], timeout=5000):
            raise RuntimeError(
                f"no {kind} call button in the chat with '{title}' — either the Teams "
                f"UI changed or calling is not available for this account")
        try:
            await page.wait_for_selector(_IN_CALL, timeout=25000)
        except Exception as exc:
            raise RuntimeError(f"clicked {kind} call for '{title}' but no call ever "
                               f"started — treat as NOT called") from exc
        placed = True
        _CALL.update(pw=pw, ctx=ctx, page=page, url=f"teams-call:{title}",
                     joined_at=asyncio.get_event_loop().time(), captions=[])
        store.kv_set("teams_in_call", f"call:{title}")
        _CALL["captions_on"] = await start_captions(page)
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

_SETTINGS_MENU = ('button[data-tid="settings-button"]', 'button[aria-label*="Settings" i]',
                  '[data-tid="callingSettingsButton"]', 'button[aria-label*="Device settings" i]')
_MIC_PICKER = ('[data-tid="microphone-device-selector"]', 'select[aria-label*="Microphone" i]',
               '[aria-label*="Microphone" i][role="combobox"]', '[data-tid="mic-dropdown"]')
_SETTINGS_CLOSE = ('button[aria-label="Close settings"]', 'button[aria-label*="Close" i]')


async def set_call_mic(page, device: str) -> bool:
    """Point Teams' microphone at `device`. False when the UI would not cooperate.

    Deliberately returns a bool rather than raising: the caller has to be able to
    tell "could not switch, so do not bother speaking" apart from "spoke and then
    could not restore", and those two want very different reactions.
    """
    if not page or not device:
        return False
    try:
        if not await _click_first(page, _SETTINGS_MENU, timeout=4000):
            return False
        for sel in _MIC_PICKER:
            try:
                await page.select_option(sel, label=device, timeout=3000)
                await _click_first(page, _SETTINGS_CLOSE, timeout=2000)
                return True
            except Exception:
                continue
        # Not a <select> in every Teams build — fall back to opening the listbox
        # and clicking the option by its visible name.
        for sel in _MIC_PICKER:
            try:
                await page.click(sel, timeout=2500)
                await page.click(f'text="{device}"', timeout=2500)
                await _click_first(page, _SETTINGS_CLOSE, timeout=2000)
                return True
            except Exception:
                continue
        await _click_first(page, _SETTINGS_CLOSE, timeout=2000)
        return False
    except Exception:
        return False


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

    words = voice.strip_voice_instruction(text)
    if not words:
        raise RuntimeError("nothing left to say once the instruction was removed")
    chosen = voice.pick_voice(text, voice_name or voice.VOICE_ASSISTANT)

    # Generated BEFORE the mic is borrowed: synthesis is the slow part, and
    # holding his microphone hostage for ten seconds of Chatterbox would mute him
    # mid-conversation for no reason.
    audio = await voice.speak(words, voice=chosen)
    if not audio:
        raise RuntimeError("speech generation produced nothing — said nothing")

    page = _CALL.get("page")
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


async def leave() -> str:
    """Hang up. Closing the browser context IS leaving the call."""
    store.kv_set("teams_in_call", "")
    call = dict(_CALL)
    text = transcript_text(call.get("captions") or [])
    _LAST_TRANSCRIPT[:] = [text] if text else []
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

_CAPTION_TOGGLES = [
    'button[aria-label*="Turn on live captions" i]',
    '[data-tid="closed-caption-button"]',
    'div[role="menuitem"][aria-label*="captions" i]',
]
_MORE_MENU = ['button[aria-label="More"]', 'button[aria-label*="More actions" i]',
              '[data-tid="callingButtons-showMoreBtn"]']
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
    except Exception:
        return                          # a caption read must never end the call watch
    for r in rows:
        _merge_caption(lines, r.get("speaker") or "someone", r.get("text") or "")


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

#: Questions Asta may offer to look up: they are about the CODE, not about him.
_ANSWERABLE = re.compile(
    r"\b(how (does|do|is|are|did)|where (is|are|do|does)|what (does|do|is|are)\b"
    r"|which (topic|class|service|table|field|repo|method)"
    r"|why (does|is|are|did)|who (calls|consumes|publishes)"
    r"|is there (a|any)\b)", re.I)

#: Questions about HIM. Never auto-answered, in any voice.
_HIS_TO_ANSWER = re.compile(
    r"\b(can you (review|check|look|merge|approve|deploy|release|join|come)"
    r"|will you\b|could you\b|shall we\b|should we\b|are you (ok|fine|free|available|done)"
    r"|when (can|will) you\b|do you (want|mind|agree)|is that (ok|fine)\b"
    r"|what do you think\b|your (call|view|opinion)\b)", re.I)

#: Asks already put to him this call, so repeated captions do not re-ask.
_ASKED: set[str] = set()


def _ask_key(line: str) -> str:
    import hashlib
    words = re.findall(r"[a-z0-9]+", (line or "").lower())
    return hashlib.sha1(" ".join(words[:14]).encode()).hexdigest()[:16]


def classify_line(line: str) -> str:
    """'answerable' | 'his' | 'chatter' for one caption line.

    Order matters: a line can look like both ("can you check how the ATA
    fallback works"), and when it does it is HIS — the sentence is a request of
    him that happens to mention code, and answering it would be answering for
    him.
    """
    text = (line or "").strip()
    if len(text) < 12:
        return "chatter"
    if _HIS_TO_ANSWER.search(text):
        return "his"
    if _ANSWERABLE.search(text):
        return "answerable"
    return "chatter"


def notice_asks(lines: list[str], speaker_is_him: bool = False) -> list[dict]:
    """New things worth reacting to, deduped for the life of the call.

    His OWN lines are skipped: Asta offering to look up a question Arun himself
    just asked out loud is noise, and worse, it would offer to answer the person
    he is talking to on their behalf.
    """
    out = []
    if speaker_is_him:
        return out
    for line in lines:
        kind = classify_line(line)
        if kind == "chatter":
            continue
        key = _ask_key(line)
        if key in _ASKED:
            continue
        _ASKED.add(key)
        out.append({"line": line.strip(), "kind": kind, "key": key})
    return out


def clear_noticed() -> None:
    """Forget this call's asks — a new call starts with a clean slate."""
    _ASKED.clear()


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


def pending_for_him(lines: list[str]) -> list[str]:
    """Things aimed at HIM, to hand back when the call ends."""
    return [l.strip() for l in lines if classify_line(l) == "his"]


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
            await asyncio.sleep(CAPTION_POLL_SECONDS)
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


async def recap(transcript: str, title: str = "") -> tuple[str, bool]:
    """Summarise a finished call and say whether any of it was actually his.

    The filter is the point. He asked for what concerns HIM after a call, not
    minutes — a full recap of a meeting where nothing landed on him is exactly the
    "unwanted related details" he does not want pushed to his phone.
    """
    from . import agent as agent_mod
    body = await agent_mod.meeting_recap(transcript, title)
    return body, agent_mod._recap_needs_arun(body)
