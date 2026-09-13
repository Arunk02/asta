"""Reading what a Teams call screen is actually doing.

Split out of `meetings` because it is a self-contained question — given a page,
where is this call? — and because `meetings` had grown past the ceiling its own
test guards. Nothing here touches call state, audio devices or the browser
lifecycle; it looks at a page and answers.

The hard-won part is that NO SINGLE SIGNAL IS TRUSTWORTHY. Teams renames its
selectors, keeps ringing chrome on screen after a call connects, and renders its
clock differently between builds. So `call_state` stacks four independent
detectors and only ever returns "connected" on positive evidence — the two ways
of being wrong are not equally bad. Calling a ringing call connected makes Asta
talk to a phone nobody picked up; calling a connected call ringing costs a few
seconds of silence, and at worst a hang-up on somebody who did answer.
"""

from __future__ import annotations

import contextlib
import re

#: Still ringing. Matched on visible text because that is the part of a calling
#: screen Teams has reworded least.
_RINGING = re.compile(r"\bringing\b|\bcalling\b|waiting for (others|them)", re.I)

#: Somebody answered. UNVERIFIED against a live connected call — nobody was rung
#: to find out — so it is deliberately not the only evidence `connected` accepts.
#: The reliable half is captions: a caption line existing means a human is talking,
#: which cannot happen before the call connects. If these selectors turn out to be
#: wrong the cost is a slower answer, not a wrong one.
_CONNECTED = '[data-tid="call-duration"], [data-tid="calling-timer"]'

#: A LIVE REMOTE STREAM is attached. The evidence that does not depend on Teams'
#: wording at all, and the only audio signal that can be trusted before the
#: ringing text clears.
#:
#: The discriminator is `srcObject`, not "is something playing". Teams plays its
#: ringback tone from a file through the same elements, so a plain playback check
#: calls every unanswered call connected. A remote peer's audio arrives as a
#: MediaStream with live tracks, which cannot exist until the call is actually
#: connected — WebRTC does not negotiate one for a phone still ringing.
#:
#: This is not hypothetical. The colleague accepted the 8 Sep test call and said
#: Arun's name twice while every text detector read "ringing" for the whole
#: 45-second deadline; Asta stayed silent and hung up on her reporting "no answer".
_AUDIO_JS = """() => {
    for (const el of document.querySelectorAll('audio,video')) {
        const s = el.srcObject;
        if (!s || typeof s.getAudioTracks !== 'function') continue;
        for (const t of s.getAudioTracks()) {
            if (t.readyState === 'live' && !t.muted) return true;
        }
    }
    return false;
}"""

#: A running m:ss timer with no children — Teams renders the call clock this way
#: whatever it names the element that day.
_TIMER_JS = """() => {
    for (const n of document.querySelectorAll('span,div')) {
        if (n.children.length) continue;
        if (/^\\d{1,2}:\\d{2}(:\\d{2})?$/.test((n.innerText || '').trim())) return true;
    }
    return false;
}"""


async def call_state(page, captions=None) -> str:
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
    if captions:
        return "connected"
    with contextlib.suppress(Exception):
        if await page.query_selector(_CONNECTED):
            return "connected"
    # BEFORE the ringing text, deliberately — the text is the detector that was
    # wrong. A live remote MediaStream cannot exist on a call still ringing, so
    # it is safe to let it win, and letting the text win is what hung up on her.
    with contextlib.suppress(Exception):
        if await page.evaluate(_AUDIO_JS):
            return "connected"
    if _RINGING.search(text[:4000]):
        return "ringing"
    with contextlib.suppress(Exception):
        if await page.evaluate(_TIMER_JS):
            return "connected"
    return "unknown"


_ENDED = re.compile(r"call ended|meeting ended|you (have )?left|rejoin", re.I)


async def call_ended(page) -> bool:
    try:
        text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return True          # the page is gone; that counts as ended
    return bool(_ENDED.search(text[:4000]))


async def call_page(ctx, fallback=None):
    """The page the call is actually ON, which is usually not the one it started
    from.

    Teams opens a call in its OWN window. Every reader here was handed
    `ctx.pages[0]` — the main Teams page — and kept it for the life of the call,
    so once the call moved out, every detector looked at a window with no call
    chrome on it and honestly reported "unknown" for ever. That is how the third
    test call ended: not one signal fired, because none of them were
    looking at the call.

    Newest first, because a call window opened seconds ago beats a stale one from
    a previous attempt.
    """
    best = None
    for page in reversed(list(getattr(ctx, "pages", []) or [])):
        with contextlib.suppress(Exception):
            if page.is_closed():
                continue
            if await page.query_selector(_CALL_CHROME):
                return page
            text = (await page.evaluate("() => document.body.innerText || ''"))[:2000]
            if _RINGING.search(text) or _ENDED.search(text):
                best = best or page
    return best or fallback


#: Anything that only exists on a call window. Used to FIND the window, so it is
#: deliberately broad — a false match costs one wrong page, a miss costs the call.
_CALL_CHROME = ('[data-tid="calling-hangup-button"], [aria-label*="Hang up" i], '
                '[data-tid="call-duration"], [data-tid="calling-screen"], '
                '[data-tid="calling-timer"]')


async def describe(page) -> str:
    """What the call screen actually says, for when no detector fires.

    "unknown" on its own is unactionable — it says a reader failed without
    saying what it read. Three calls were spent guessing at that.
    """
    if page is None:
        return "no page"
    with contextlib.suppress(Exception):
        text = await page.evaluate("() => document.body.innerText || ''")
        line = " / ".join(t.strip() for t in text.split("\n") if t.strip())[:300]
        return line or "(the page has no visible text)"
    return "(could not read the page)"
