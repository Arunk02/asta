"""Somebody is calling him right now.

    "tell their name who calling it is one to one or group call like that if i
     said accept the call and talk for incoming calls"

Everything Asta had was outbound. `call_state` reads the screen of a call ASTA
placed, from `_CALL`, and there is no `_CALL` when the phone simply rings — so an
incoming call was invisible until it turned up in the Activity feed afterwards as
"Missed call from Vinish Kumar". By then the only honest thing to say is that it
was missed.

**Detected on TEXT, not on a toast selector.** `meetings._RINGING` already works
this way, and says why: visible text is the part of a calling surface Teams has
reworded least. A `data-tid` guess would look correct in review and fail silently
on the one call that mattered — which is how the mute button, the captions menu
and the mic all went wrong this week. The element selectors below are only used to
CLICK once a call has already been identified by its words, and every one of them
is tried in turn.

**Answering is his decision, every time.** The ring is announced with an offer and
nothing is clicked until he says yes: picking up puts Asta into a conversation
with a colleague who thinks they are reaching him, and there is no version of
getting that wrong that is cheap. Silence is also a valid answer — declining
leaves the call to ring out exactly as it would have.
"""

from __future__ import annotations

import contextlib
import os
import re

from . import store

#: Off by default. It clicks things in a live Teams session on his behalf.
def enabled() -> bool:
    return os.environ.get("ASTA_INCOMING", "").strip() not in ("", "0", "false", "no")


#: How often to look. A ring lasts about thirty seconds, so anything slower than
#: this answers a phone that has already stopped.
POLL_SECONDS = float(os.environ.get("ASTA_INCOMING_SECONDS", "8"))

#: The words Teams puts on an incoming call, across its wordings.
_INCOMING = re.compile(
    r"\b(incoming (?:call|video call))\b"
    r"|\b(is|are) calling you\b"
    r"|\bcalling you\b"
    r"|\b(\d+ )?(participants?|others?) (?:are )?in (?:this|the) call\b",
    re.I)

#: "Vinish Kumar is calling you" / "Incoming call from Vinish Kumar".
_WHO = (re.compile(r"^\s*(.{2,60}?)\s+is calling you", re.I | re.M),
        re.compile(r"incoming (?:call|video call)\s+from\s+(.{2,60}?)\s*$", re.I | re.M))

#: A group call names more than one person, or a group.
_GROUP = re.compile(r",|\+\d+\b|\band\b|\bgroup\b|\bmeeting\b", re.I)

#: Tried in order once the WORDS have already identified a call. Never used to
#: decide that a call exists.
ACCEPT = ('[data-tid="toast-accept-audio"]',
          '[data-tid="calling-toast-accept-audio-button"]',
          '[aria-label*="Accept with audio" i]',
          '[aria-label*="Accept" i]',
          'button[title*="Accept" i]')
DECLINE = ('[data-tid="toast-decline"]',
           '[aria-label*="Decline" i]',
           'button[title*="Decline" i]')

_SEEN_KEY = "incoming_seen"


def who_is_calling(text: str) -> str:
    for pattern in _WHO:
        m = pattern.search(text or "")
        if m and m.group(1).strip():
            return re.sub(r"\s+", " ", m.group(1)).strip()[:60]
    return ""


def looks_incoming(text: str) -> bool:
    return bool(_INCOMING.search(text or ""))


def is_group(who: str, text: str) -> bool:
    """A 1:1 names one person. A group names several, or names a room.

    Reported rather than inferred silently, because he asked for it explicitly —
    "it is one to one or group call like that" — and because the two want
    different answers from him.
    """
    return bool(_GROUP.search(who or "")) or bool(
        re.search(r"\b\d+\s+(others?|participants?)\b", text or "", re.I))


def describe(call: dict) -> str:
    kind = "group call" if call.get("group") else "1:1 call"
    who = call.get("who") or "Someone"
    return f"📞 {who} is calling — {kind}."


#: Find the Accept control by what it SAYS, the way `_RINGING` finds a ringing
#: call. The data-tid list below is tried first because it is precise when it
#: matches, but it is guesswork — nobody has rung this laptop to find out — and a
#: selector that silently matches nothing is how the mute button, the captions menu
#: and the microphone all went wrong this week. Text is the part Teams rewords
#: least, so it is the fallback that has to work.
_ACCEPT_BY_TEXT = """
() => {
  const want = /^(accept|answer)\b/i;
  const nodes = document.querySelectorAll(
    'button,[role="button"],[role="menuitem"]');
  for (const n of nodes) {
    const label = ((n.getAttribute('aria-label') || '') + ' ' +
                   (n.getAttribute('title') || '') + ' ' +
                   (n.innerText || '')).trim();
    if (!want.test(label)) continue;
    if (/decline|reject|ignore|video/i.test(label)) continue;   // audio only
    n.setAttribute('data-asta-accept', '1');
    return label.slice(0, 60);
  }
  return '';
}
"""


async def capture_toast(page) -> str:
    """Record the ring's markup the FIRST time one is seen, for calibration.

    The accept selectors above were written without a real incoming call to check
    them against. Rather than leave that as a permanent unknown, the first genuine
    ring writes what Teams actually rendered to data/ so the guess can be replaced
    with the truth. Read-only, once, and never overwritten.
    """
    from pathlib import Path
    out = Path(__file__).resolve().parent.parent / "data" / "incoming-toast.html"
    if out.exists():
        return ""
    try:
        html = await page.evaluate(
            "() => document.body ? document.body.innerHTML.slice(0, 200000) : ''")
    except Exception:                                          # noqa: BLE001
        return ""
    with contextlib.suppress(Exception):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html)
        return str(out)
    return ""


async def look(page) -> dict | None:
    """Is a call ringing on this page right now? {who, group} or None."""
    if page is None:
        return None
    try:
        text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:                                          # noqa: BLE001
        return None
    head = (text or "")[:4000]
    if not looks_incoming(head):
        return None
    who = who_is_calling(head)
    return {"who": who, "group": is_group(who, head), "seen_text": head[:200]}


def already_offered(call: dict) -> bool:
    """One offer per ring. Polling every eight seconds must not ask four times."""
    return (store.kv_get(_SEEN_KEY) or "") == _ring_key(call)


def _ring_key(call: dict) -> str:
    return f"{(call.get('who') or '').lower()}|{int(call.get('group', False))}"


def note_offered(call: dict) -> None:
    store.kv_set(_SEEN_KEY, _ring_key(call))


def clear() -> None:
    """The ring stopped. The next one from the same person is a new call."""
    store.kv_set(_SEEN_KEY, "")


async def answer(page, speak: bool = False) -> str:
    """Pick up. Returns what happened, in the words he reads.

    Only ever called after he said yes.
    """
    from . import meetings, voice
    if speak and not voice.can_speak():
        return ("Didn't answer — you asked me to talk, and there is no virtual "
                "microphone configured, so I would have picked up mute. "
                + meetings.speaking_hint())
    clicked = await meetings._click_first(page, ACCEPT, timeout=4000)
    if not clicked:
        # The data-tid guesses missed. Fall back to whatever the button SAYS.
        with contextlib.suppress(Exception):
            if await page.evaluate(_ACCEPT_BY_TEXT):
                await page.click('[data-asta-accept="1"]', timeout=3000)
                clicked = True
    if not clicked:
        return "Couldn't find the Accept button — the call was not answered."
    meetings._CALL.update(page=page, joined_at=meetings._now(), captions=[],
                          answered_at=meetings._now(), speaks=bool(speak), who="")
    store.kv_set("teams_in_call", "incoming")
    with contextlib.suppress(Exception):
        meetings._CALL["captions_on"] = await meetings.start_captions(page)
    if speak:
        with contextlib.suppress(Exception):
            await voice.ensure_unmuted(page)
        return "Answered and talking."
    return "Answered — listening only."


async def decline(page) -> str:
    from . import meetings
    if await meetings._click_first(page, DECLINE, timeout=4000):
        clear()
        return "Declined."
    return "Left it ringing."


async def watch_loop() -> None:
    """Notice a ring, tell him who it is, and wait for his answer.

    Nothing is clicked here. The offer carries the op that answers, so his "yes"
    runs the recorded call rather than a brain re-deciding what he meant.
    """
    from . import notify, offers, teams_bridge, wake
    while True:
        await wake.sleep(POLL_SECONDS)
        if not (enabled() and teams_bridge.enabled() and teams_bridge.logged_in_once()
                and store.kv_get("teams_session_ok") != "0"):
            continue
        if meetings_busy():
            continue
        saved = ""
        try:
            async with teams_bridge.teams_page() as page:
                call = await look(page)
                if call:
                    saved = await capture_toast(page)
        except Exception:                                      # noqa: BLE001
            continue
        if not call:
            clear()                    # the ring stopped
            continue
        if already_offered(call):
            continue
        note_offered(call)
        if saved:
            from . import quiet
            quiet.note("incoming.toast_captured", RuntimeError(saved))
        offers.staged_write(
            "call_answer", {"speak": False},
            describe(call),
            f"{describe(call)} Say yes and I'll pick up and listen; "
            f"say 'answer and talk' if you want me to speak to them.",
            "Answer it?", kind="call")
        with contextlib.suppress(Exception):
            await notify.notify(offers.pending().render(), "calls", urgency="direct")


def meetings_busy() -> bool:
    """Already in a call — a second one is not ours to answer."""
    from . import meetings
    return bool(meetings._CALL)
