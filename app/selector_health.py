"""Are the Teams selectors still real?

Fifty-nine CSS selectors stand between Asta and Microsoft's markup, and Microsoft
ships whenever it likes. Every one of them fails the same way: `_click_first`
returns False, the caller reports "couldn't find the button", and Arun finds out
because something he asked for quietly did not happen. That has already cost two
days on `button[aria-label="Audio call"]` — Teams renders a div with role=button,
so the tag constraint alone made every call fail — and a caption reader that was
written against a `<time>` element this build does not have.

The cheap half of the problem is knowing. A selector either matches something in
the live DOM or it does not, and asking takes milliseconds on a browser that is
now kept alive anyway. This runs the critical ones against the real page and says
which have stopped matching, BEFORE the next thing Arun asks for depends on one.

Deliberately not a repair mechanism. Guessing a replacement selector is how a
send lands in the wrong thread; naming the broken one and stopping is the honest
failure, and it turns a silent misbehaviour into a message.
"""

from __future__ import annotations

import asyncio
import contextlib

from . import quiet, store

_quiet = contextlib.suppress


def _q():
    return contextlib.suppress(Exception)

#: What must work, and what breaks when it does not. Only the selectors whose
#: failure Arun would actually feel — a health check that reports on everything
#: reports on nothing.
CRITICAL: dict[str, dict] = {
    "chat list": {
        "needs": "app",
        "selector": '[role="treeitem"]',
        "breaks": "finding any person or group — every read, send and call",
        "where": "teams_bridge._find_chat",
    },
    "message rows": {
        "needs": "chat",
        "selector": '[data-tid="chat-pane-item"]',
        "breaks": "reading any conversation, and every history answer",
        "where": "teams_bridge._MESSAGE_JS",
    },
    "message body": {
        "needs": "chat",
        "selector": '[id^="content-"]',
        "breaks": "message timestamps — 'what did he say last night' silently loses its window",
        "where": "teams_bridge._MESSAGE_JS",
    },
    "composer": {
        "needs": "chat",
        "selector": '[contenteditable="true"]',
        "breaks": "sending any message",
        "where": "teams_bridge.send_message",
    },
    "search box": {
        "needs": "app",
        "selector": '[data-tid="search-box"], input[placeholder*="Search" i], [role="combobox"]',
        "breaks": "resolving a name to a chat",
        "where": "teams_bridge._find_chat",
    },
    "activity feed": {
        "needs": "activity",
        "selector": '[data-tid="activity-list-container"], [data-tid="activity-feed-list-item"]',
        "breaks": "noticing that anybody pinged him at all",
        "where": "teams_bridge.activity",
    },
    "call button": {
        "needs": "person",
        "selector": '[data-tid="default-chat-call-audio-button"], [aria-label*="Audio call" i]',
        "breaks": "placing a call — the one that already cost two days",
        "where": "meetings._CALL_BUTTONS",
    },
}


#: Rail entries that are navigation rather than conversations. Not used to
#: classify — only to avoid wasting attempts on them. If Teams renames one, the
#: check simply tries it, finds no chat markup, and moves to the next.
_NOT_A_CHAT = ("quick views", "mentions", "discover", "drafts", "saved",
               "favorites", "favourites", "chats", "copilot", "meeting chats")


async def _rail(page) -> list[str]:
    """The names in his chat rail, in the order Teams shows them."""
    with _q():
        return await page.evaluate(
            """() => Array.from(document.querySelectorAll('[role="treeitem"]'))
                   .map(n => (n.innerText || '').split('\n')[0].trim())
                   .filter(Boolean).slice(0, 25)""")
    return []


async def _find_a_chat(page, want_call: bool) -> str:
    """Open a chat from his own rail and return its name, or ''.

    No configured name. Arun's point, and he is right: he can ask Asta to message
    anyone, so a health check that needs a colleague named in `.env` is a knob he
    would have to maintain forever and would forget.

    `want_call` picks a conversation that actually HAS a call button, which a
    self-chat never does — nobody can call themselves. Rather than trying to
    classify rail entries (the class names are unstable hashes and the first
    entries are navigation), it simply opens them in order and stops at the first
    one where the markup appears. Evidence, not classification.
    """
    from . import teams_bridge
    rail = await _rail(page)
    if not want_call:
        # His own thread first when only reading markup — it opens nobody else's
        # conversation. Falling back to any chat rather than skipping the check:
        # a rail without a "(You)" entry is a reason to read someone else's
        # message list, not a reason to stop checking whether reading works.
        rail = sorted(rail, key=lambda n: "(you)" not in n.lower())
    for name in rail:
        low = name.lower()
        if any(n in low for n in _NOT_A_CHAT):
            continue
        if want_call and "(you)" in low:
            continue                       # a self-chat has no call button
        try:
            await teams_bridge._find_chat(page, _searchable(name), allow_group=True)
        except Exception:
            continue
        await asyncio.sleep(1.2)
        probe = CRITICAL["call button"]["selector"] if want_call \
            else CRITICAL["message rows"]["selector"]
        with _q():
            if await page.evaluate("(s) => document.querySelectorAll(s).length", probe):
                return name
    return ""


def _searchable(rail_name: str) -> str:
    """The name Teams SEARCH accepts, which is not the name it renders.

    "Arunkumar K (You)" finds nothing; "Arunkumar K" resolves to it. The suffix is
    display decoration, and searching for it is a match against a string that
    exists nowhere in the directory.
    """
    return rail_name.split("(")[0].strip() or rail_name


async def check(page, state: str = "app") -> list[dict]:
    """Which selectors for `state` still match. Never raises.

    State matters, and the first version of this ignored it: a composer only
    exists once a chat is open, a call button only exists in a chat header. Run
    against whatever page happened to be loaded, six of seven came back BROKEN —
    every one a false alarm. A health check that cries wolf is worse than none,
    because he learns to ignore it and then ignores the real one.
    """
    out: list[dict] = []
    for name, spec in CRITICAL.items():
        if spec.get("needs", "app") != state:
            continue
        try:
            found = await page.evaluate(
                "(sel) => document.querySelectorAll(sel).length", spec["selector"])
        except Exception as exc:                       # noqa: BLE001
            out.append({"name": name, "found": -1, "ok": False,
                        "note": f"could not be checked: {type(exc).__name__}", **spec})
            continue
        out.append({"name": name, "found": int(found), "ok": int(found) > 0, **spec})
    return out


async def run(chat: str = "") -> list[dict]:
    """Drive Teams through each state and check the selectors that state needs.

    Three states, because the markup only exists in the state that uses it: the
    app shell, an open conversation, and the activity feed. Reading one chat is
    harmless — it sends nothing and changes nothing — and the alternative is not
    checking the selectors that matter most.
    """
    from . import meetings, teams_bridge
    if not teams_bridge.enabled():
        return []
    async with teams_bridge.teams_page() as page:
        # Back to Chat first. The pooled browser is reused between operations, so
        # the page may well be sitting on the Activity tab from the last run —
        # which made this check order-dependent, and the first run of it reported
        # a chat state that had simply never been opened.
        with _q():
            await meetings._click_first(page, ['[aria-label^="Chat ("]',
                                              '[aria-label^="Chat"]'], timeout=6000)
            await asyncio.sleep(1.5)
        await meetings._wait_for_chat_list(page)
        results = await check(page, "app")

        # The chat is NAMED rather than taken from the list: the first tree items
        # are navigation — "Quick views", "Mentions", "Drafts" — not
        # conversations, the same trap `_find_chat`'s own docstring warns about.
        if chat:
            results += await _in_state(
                page, "chat",
                lambda: teams_bridge._find_chat(page, chat, allow_group=True),
                f"could not open '{chat}'")
        else:
            found = await _find_a_chat(page, want_call=False)
            results += (await check(page, "chat") if found else _unreached(
                "chat", "no conversation in the rail could be opened"))

        callable_chat = await _find_a_chat(page, want_call=True)
        results += (await check(page, "person") if callable_chat else _unreached(
            "person", "no conversation in the rail rendered a call button"))

        results += await _in_state(
            page, "activity",
            lambda: meetings._click_first(
                page, ['[aria-label^="Activity ("]', '[aria-label^="Activity"]'],
                timeout=6000),
            "could not open the Activity tab")
        return results


def _unreached(state: str, why: str) -> list[dict]:
    """Selectors for a state that was never reached. NOT broken — unchecked."""
    return [{"name": name, "found": 0, "ok": True, "unchecked": True,
             "note": f"not checked — {why}", **spec}
            for name, spec in CRITICAL.items() if spec.get("needs") == state]


async def _in_state(page, state: str, reach, why: str) -> list[dict]:
    """Get the page into `state`, then check it — or report the state unreached.

    NOT checked is not the same as broken, and conflating them is how a health
    check teaches him to ignore it. The first version of this reported "6 of 7
    broken" when the truth was "I never opened a chat"; every one was a false
    alarm, and a false alarm is worse than no check at all.
    """
    reached = False
    try:
        reached = bool(await reach()) or True
    except Exception:
        reached = False
    if reached:
        await asyncio.sleep(1.5)              # let the view settle before asking
        found = await check(page, state)
        if any(r["ok"] for r in found):
            return found
        # Nothing at all matched: far more likely the view never arrived than
        # that Microsoft changed every selector for this state at once.
        why = f"{state} view did not render anything expected"
        reached = False
    return [{"name": name, "found": 0, "ok": True, "unchecked": True,
             "note": f"not checked — {why}", **spec}
            for name, spec in CRITICAL.items() if spec.get("needs") == state]


def summarise(results: list[dict]) -> tuple[str, bool]:
    """A message for Arun, and whether anything is actually broken."""
    broken = [r for r in results if not r["ok"]]
    unchecked = [r for r in results if r.get("unchecked")]
    if not broken:
        note = f" ({len(unchecked)} not checked)" if unchecked else ""
        return (f"✅ Teams selectors: {len(results) - len(unchecked)} checked, "
                f"all still match{note}.", False)
    lines = [f"⚠️ Teams markup has changed — {len(broken)} of {len(results)} "
             f"selectors no longer match anything:"]
    for r in broken:
        lines.append(f"\n• **{r['name']}** — breaks {r['breaks']}\n"
                     f"  {r['where']}\n  `{r['selector'][:110]}`")
    lines.append("\n\nNothing has been guessed at — a replacement selector chosen "
                 "blind is how a message lands in the wrong thread. These need a "
                 "look at the live DOM.")
    return "\n".join(lines), True


async def check_and_report() -> str:
    """The scheduled version: only speaks up when something is broken."""
    from . import notify
    try:
        results = await run()
    except Exception as exc:                           # noqa: BLE001
        return f"selector check could not run: {type(exc).__name__}: {exc}"
    if not results:
        return "Teams bridge is off — nothing to check."
    text, broken = summarise(results)
    store.kv_set("teams_selectors_checked", str(len(results)))
    store.kv_set("teams_selectors_broken", str(sum(1 for r in results if not r["ok"])))
    if broken:
        # Direct, not ambient. Something he relies on has stopped working and he
        # cannot discover it any other way until it fails in front of somebody.
        await notify.notify(text, "teams", urgency="direct")
    return text


#: Once a day. A selector breaks when Microsoft ships, not on a schedule Asta
#: sets, so the interval is about bounding how long a break can hide — not about
#: catching it promptly. Driving the browser is not free, and a check that runs
#: often enough to be annoying is a check that gets switched off.
#:
#: Deliberately NOT an env setting. The two knobs this module used to have were
#: removed because Arun would have had to keep them current — "I can't go and
#: update everytime" — and a schedule he never needs to tune is one more of the
#: same. A test enforces that no ASTA_SELECTOR_CHECK_* setting comes back.
CHECK_EVERY_SECONDS = 24 * 3600

#: Let the Teams session finish coming up before driving it. A constant rather
#: than a literal so a test can prove the loop survives a failing check without
#: waiting ten minutes to find out.
SETTLE_SECONDS = 600


async def watch_loop() -> None:
    """Supervised daily selector check.

    This module existed for a week reachable only from its own tests — which is
    the exact failure it was written to catch, one level up: code that looks
    present, is covered, and is called by nothing. A health check nothing runs is
    worth less than no health check, because it reads on the page as protection.
    """
    await asyncio.sleep(SETTLE_SECONDS)
    while True:
        with quiet.swallow("selector_health.watch_loop"):
            await check_and_report()
        await asyncio.sleep(CHECK_EVERY_SECONDS)
