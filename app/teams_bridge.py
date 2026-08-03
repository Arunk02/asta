"""Teams web bridge (Playwright) — read chats + send messages until Azure AD arrives.

How it works: a persistent Chromium profile at data/teams_profile/ holds your
Teams web session. You log in ONCE (your organisation's SSO, done by you, in a visible
window); after that Asta drives Teams web headlessly with deterministic scripts —
no LLM tokens are spent unless you ask Asta to reason about what it read.

One-time login (opens a browser window — complete the SSO yourself):
    .venv/bin/python -m app.teams_bridge login

Enable in .env:  TEAMS_BRIDGE=1
Scope by design: read chat threads, send messages. NO meeting joins (your name
would appear in the participant list while you're absent — professional risk;
use Teams' own recording/recap and ask Asta to summarize the transcript instead).

Security note: data/teams_profile/ contains your corporate session cookies.
Same exposure class as your normal browser profile — keep FileVault on, and
remember org token policy will expire the session every few weeks; Asta
notifies you when a re-login is needed.

Fragility note: Teams web DOM changes without notice. Selectors below try
several fallbacks and fail with a clear error instead of guessing.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from . import store

PROFILE_DIR = Path(__file__).resolve().parent.parent / "data" / "teams_profile"
TEAMS_URL = "https://teams.microsoft.com/v2/"
SESSION_CHECK_SECONDS = 1800  # 30 min
_lock = asyncio.Lock()  # one browser at a time — Chromium profiles are single-writer


def enabled() -> bool:
    return os.environ.get("TEAMS_BRIDGE", "").lower() in ("1", "true", "yes")


def logged_in_once() -> bool:
    return (PROFILE_DIR / "Default").exists() or (PROFILE_DIR / "Cookies").exists()


def status() -> dict:
    return {
        "enabled": enabled(),
        "profile": logged_in_once(),
        "session_ok": store.kv_get("teams_session_ok") != "0",
        "hint": (
            "set TEAMS_BRIDGE=1 in .env" if not enabled()
            else ("run: .venv/bin/python -m app.teams_bridge login" if not logged_in_once()
                  else ("session expired — rerun: python -m app.teams_bridge login"
                        if store.kv_get("teams_session_ok") == "0" else "ok"))
        ),
    }


async def _launch(headless: bool = True):
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    return pw, ctx


# Markers that only exist in the authenticated Teams app (NOT its pre-redirect shell
# or the Microsoft login page — generic ones like #app match those too).
APP_MARKERS = '[data-tid="app-bar"], [data-tid="chat-list"], [data-tid="app-layout-area--main"]'


async def _open_teams(ctx, timeout: float = 75.0):
    """Navigate to Teams; poll until the authenticated app renders or we land on login."""
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        url = page.url
        if "microsoftonline" in url or "/login" in url or "signin" in url:
            raise RuntimeError("SESSION_EXPIRED")
        try:
            if await page.query_selector(APP_MARKERS):
                return page
        except Exception:
            pass  # navigation in flight — poll again once the new document is up
        await asyncio.sleep(1)
    raise RuntimeError(f"Teams app did not load within {int(timeout)}s (url: {page.url[:100]})")


async def _chat_title(page) -> str:
    """Name of the conversation currently open (empty string if undetermined)."""
    return await page.evaluate(
        """() => {
            for (const s of ['[data-tid="chat-header-title"]', '[data-tid="threadHeaderTitle"]', 'h2']) {
                for (const el of document.querySelectorAll(s)) {
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 120 && t.toLowerCase() !== 'chat') return t;
                }
            }
            return '';
        }""")


async def _find_chat(page, chat: str, allow_group: bool = False) -> str:
    """Open a chat by name and return the title of what actually opened.

    SAFETY: Teams' search dropdown starts with FILTER CHIPS ("Chats", "Channels",
    "Group chats"), not results. Clicking blindly on the first [role="option"]
    selects a filter, leaves the previously-open thread active, and anything typed
    then goes into THAT thread — which is how a 1:1 message once landed in a team
    channel. So: pick the result by its aria-label role, never by position, and
    verify the opened conversation before the caller types anything.

    Person (1:1 DM) always wins. Groups/channels are only considered when the
    caller explicitly asked for one.
    """
    await page.keyboard.press("Control+E" if sys.platform != "darwin" else "Meta+E")
    box = None
    for sel in ('input[data-tid="searchInput"]', 'input[type="search"]',
                'input[placeholder*="Search"]', '#searchInputField'):
        try:
            box = await page.wait_for_selector(sel, timeout=4000)
            break
        except Exception:
            continue
    if not box:
        raise RuntimeError("search box not found — Teams UI changed; selectors need an update")
    await box.fill("")
    await box.type(chat, delay=25)
    await asyncio.sleep(2.5)  # let suggestions render

    options = await page.evaluate(
        """() => Array.from(document.querySelectorAll('[role="option"]')).map((o, i) => ({
               i, aria: o.getAttribute('aria-label') || '', text: (o.innerText || '').trim(),
           }))""")
    wanted = chat.strip().lower()

    def _kind(o: dict) -> str:
        a = o["aria"].strip().lower()
        for k in ("person", "group chat", "channel", "suggestion", "meeting", "file", "image"):
            if a.startswith(k):
                return k
        return "other"

    def _matches(o: dict) -> bool:
        hay = f"{o['aria']} {o['text']}".lower()
        return all(tok in hay for tok in wanted.split())

    people = [o for o in options if _kind(o) == "person" and _matches(o)]
    groups = [o for o in options if _kind(o) in ("group chat", "channel") and _matches(o)]

    if people:
        pick = people[0]
    elif allow_group and groups:
        pick = groups[0]
    elif groups:
        raise RuntimeError(
            f"'{chat}' only matches a group/channel ({groups[0]['text'][:60]!r}) — refusing: "
            "Arun's rule is 1:1 unless he names the group explicitly")
    else:
        found = ", ".join(f"{_kind(o)}:{o['text'][:30]}" for o in options[:6]) or "nothing"
        raise RuntimeError(f"no person match for '{chat}' in Teams search (saw: {found})")

    handles = await page.query_selector_all('[role="option"]')
    await handles[pick["i"]].click()
    await page.wait_for_selector(
        '[data-tid="messageBodyContent"], [data-tid="chat-pane-message"], [role="main"]',
        timeout=20000)
    await asyncio.sleep(2)  # header re-renders after the thread loads

    title = await _chat_title(page)
    # Last line of defence: if the open conversation is not the one asked for,
    # fail loudly rather than let the caller type into the wrong thread.
    if title and not any(tok in title.lower() for tok in wanted.split()):
        raise RuntimeError(f"opened '{title}' instead of '{chat}' — aborted without typing")
    return title or chat


async def read_chat(chat: str, limit: int = 15) -> list[str]:
    """Return the last `limit` visible messages of a chat as 'Sender: text' lines."""
    async with _lock:
        pw, ctx = await _launch()
        try:
            page = await _open_teams(ctx)
            await _find_chat(page, chat, allow_group=True)  # reading a group is harmless
            msgs = await page.evaluate(
                """(limit) => {
                    const nodes = document.querySelectorAll(
                      '[data-tid="chat-pane-message"], [data-tid="messageBodyContent"]');
                    const out = [];
                    for (const n of Array.from(nodes).slice(-limit)) {
                      const item = n.closest('[data-tid="chat-pane-item"]') || n;
                      const author = item.querySelector(
                        '[data-tid="message-author-name"], [data-tid="threadBodyDisplayName"]');
                      const body = item.querySelector('[data-tid="messageBodyContent"]') || n;
                      const text = (body.innerText || '').trim();
                      if (text) out.push(((author?.innerText || 'me').trim()) + ': ' + text);
                    }
                    return out;
                }""",
                limit,
            )
            store.kv_set("teams_session_ok", "1")
            return msgs
        finally:
            await ctx.close()
            await pw.stop()


async def send_message(chat: str, text: str, allow_group: bool = False) -> str:
    """Send a message to a person's 1:1 chat. Returns the chat it landed in.

    Groups/channels require allow_group=True — Arun's standing rule is that a
    "ping X" means X's personal chat, never a team channel.
    """
    async with _lock:
        pw, ctx = await _launch()
        try:
            page = await _open_teams(ctx)
            title = await _find_chat(page, chat, allow_group=allow_group)
            box = None
            for sel in ('[data-tid="ckeditor"] [contenteditable="true"]',
                        'div[contenteditable="true"][role="textbox"]',
                        '[data-tid="message-input"]'):
                try:
                    box = await page.wait_for_selector(sel, timeout=6000)
                    break
                except Exception:
                    continue
            if not box:
                raise RuntimeError("message box not found — Teams UI changed")
            await box.click()
            await box.focus()
            # Teams' ckeditor sends the message on a bare Enter — it does NOT
            # insert a newline. box.type() presses a real Enter for every "\n"
            # in the string, so a multi-line message used to go out as one
            # fragmented, garbled send per line. Shift+Enter inserts a soft
            # line break instead.
            #
            # insert_text (not type) puts each line in as ONE atomic input event.
            # type() streams per-character keystrokes, and the first few were
            # landing before the editor had finished focusing — the "iff got
            # truncated" instead of "1) Diff got truncated" that reached Vinish.
            # As a bonus, insert_text doesn't fire the keypress that turns a
            # leading "- " into an auto-list, so bullet lines stay literal.
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if line:
                    await page.keyboard.insert_text(line)
                if i < len(lines) - 1:
                    await page.keyboard.press("Shift+Enter")
            # A line starting with "- " or "* " auto-converts to a bullet list
            # in ckeditor; once that happens, a bare Enter just adds another
            # list item instead of submitting, so the message never sends.
            # The Send button always submits regardless of list state — prefer
            # it, and only fall back to Enter if the UI doesn't expose one.
            sent_via_button = False
            for sel in ('button[data-tid="sendMessageCommand"]',
                        'button[aria-label="Send"]',
                        'button[aria-label*="Send message"]'):
                try:
                    send_btn = await page.wait_for_selector(sel, timeout=2000)
                    if send_btn:
                        await send_btn.click()
                        sent_via_button = True
                        break
                except Exception:
                    continue
            if not sent_via_button:
                await page.keyboard.press("Enter")
            await asyncio.sleep(2.5)  # let the send complete before verifying
            # Verify rather than assume: read the thread back and confirm the
            # text is really the last thing in it. "Sent ✅" must mean sent.
            landed = await page.evaluate(
                """(txt) => {
                    const nodes = document.querySelectorAll(
                      '[data-tid="chat-pane-message"], [data-tid="messageBodyContent"]');
                    const tail = Array.from(nodes).slice(-4)
                        .map(n => (n.innerText || '').replace(/\\s+/g, ' ').trim());
                    const want = txt.replace(/\\s+/g, ' ').trim();
                    return tail.some(t => t.includes(want));
                }""", text)
            if not landed:
                raise RuntimeError(
                    f"message does not appear in '{title}' after sending — treat as NOT sent")
            store.kv_set("teams_session_ok", "1")
            return title
        finally:
            await ctx.close()
            await pw.stop()


# --- presence ----------------------------------------------------------------
#
# Teams' own words, lowercased. Matched on the menu's ACCESSIBLE TEXT rather than
# a data-tid, because the me-control's internal ids have changed twice in the life
# of this file while the visible labels have not. Aliases are what Arun actually
# says — "dnd", "away", "brb" — mapped to the label the menu shows.
PRESENCE_STATES = {
    "available": "Available",
    "online": "Available",
    "free": "Available",
    "busy": "Busy",
    "in a meeting": "Busy",
    "dnd": "Do not disturb",
    "do not disturb": "Do not disturb",
    "focus": "Do not disturb",
    "brb": "Be right back",
    "be right back": "Be right back",
    "away": "Appear away",
    "appear away": "Appear away",
    "offline": "Appear offline",
    "appear offline": "Appear offline",
}

# Finds a clickable element by its visible or accessible text. Kept as one JS
# helper because every presence step is the same shape: open a menu, click the
# item that says X.
_CLICK_BY_TEXT = """
(want) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const target = norm(want);
    const nodes = document.querySelectorAll(
        'button, [role="menuitem"], [role="menuitemradio"], [role="option"], a');
    for (const el of nodes) {
        const label = norm(el.getAttribute('aria-label')) || norm(el.innerText);
        if (label === target || label.startsWith(target)) { el.click(); return true; }
    }
    return false;
}
"""

_ME_CONTROL = ('[data-tid="me-control-avatar"], #idna-me-control, '
               '[data-tid="me-control"], button[aria-label*="profile" i], '
               'button[aria-label*="your profile" i]')


def presence_label(wanted: str) -> str:
    """Map what he said to the label Teams shows. Raises on anything unknown.

    Refusing beats guessing here: silently setting "Busy" when he asked for
    something this doesn't recognise would make him look available to nobody and
    unavailable to everybody, and he would have no reason to check.
    """
    key = re.sub(r"\s+", " ", (wanted or "").strip().lower())
    if key in PRESENCE_STATES:
        return PRESENCE_STATES[key]
    raise RuntimeError(f"'{wanted}' isn't a Teams status — one of: "
                       + ", ".join(sorted(set(PRESENCE_STATES.values()))))


async def read_presence() -> str:
    """His current Teams status, as Teams reports it ("" when undetermined)."""
    async with _lock:
        pw, ctx = await _launch()
        try:
            page = await _open_teams(ctx)
            return await page.evaluate(
                """() => {
                    const el = document.querySelector(
                        '[data-tid="me-control-avatar"], #idna-me-control, [data-tid="me-control"]');
                    const label = (el && el.getAttribute('aria-label')) || '';
                    const m = label.match(
                        /(Available|Busy|Do not disturb|Be right back|Away|Offline)/i);
                    return m ? m[1] : '';
                }""")
        finally:
            await ctx.close()
            await pw.stop()


async def set_presence(wanted: str) -> str:
    """Set his Teams status. Returns what it actually reads afterwards.

    Verified, not assumed — the same rule send_message follows. A status change
    that silently failed is worse than one that never ran: he thinks he is on Do
    Not Disturb and takes the call he was avoiding.
    """
    label = presence_label(wanted)
    async with _lock:
        pw, ctx = await _launch()
        try:
            page = await _open_teams(ctx)
            opened = False
            for sel in _ME_CONTROL.split(", "):
                try:
                    await (await page.wait_for_selector(sel, timeout=4000)).click()
                    opened = True
                    break
                except Exception:
                    continue
            if not opened:
                raise RuntimeError("couldn't open the Teams profile menu — UI changed")
            await asyncio.sleep(1.2)
            # The menu shows the CURRENT status as the submenu trigger, so the
            # target label may need one hop through whatever it currently says.
            if not await page.evaluate(_CLICK_BY_TEXT, label):
                for trigger in ("Available", "Busy", "Do not disturb", "Be right back",
                                "Away", "Offline", "Set status"):
                    if await page.evaluate(_CLICK_BY_TEXT, trigger):
                        await asyncio.sleep(1.0)
                        break
                if not await page.evaluate(_CLICK_BY_TEXT, label):
                    raise RuntimeError(f"couldn't find '{label}' in the status menu")
            await asyncio.sleep(2.0)
            now = await page.evaluate(
                """() => {
                    const el = document.querySelector(
                        '[data-tid="me-control-avatar"], #idna-me-control, [data-tid="me-control"]');
                    const m = ((el && el.getAttribute('aria-label')) || '').match(
                        /(Available|Busy|Do not disturb|Be right back|Away|Offline)/i);
                    return m ? m[1] : '';
                }""")
            store.kv_set("teams_session_ok", "1")
            if now and now.lower() not in label.lower():
                raise RuntimeError(
                    f"asked for {label} but Teams still reads {now} — treat as NOT set")
            return now or label
        finally:
            await ctx.close()
            await pw.stop()


#: How many times to re-try opening the Activity tab before giving up on a poll.
_ACTIVITY_ATTEMPTS = 3


async def _open_activity(page) -> None:
    """Click through to the Activity feed, surviving Teams re-rendering under us.

    This was `wait_for_selector(...)` followed by `handle.click()`, and it was
    silently dead in production for an unknown length of time: Teams re-renders
    its rail moments after load, so the handle resolved and was then DETACHED
    before the click landed — "ElementHandle.click: Element is not attached to
    the DOM", every poll, caught and discarded by the watcher's bare `except`.
    Nobody could see it because a dead mention watcher and a quiet afternoon look
    identical.

    `page.click(selector)` re-resolves the selector and auto-retries instead of
    holding a handle across a re-render, which is the whole reason Playwright
    offers it. The retry loop on top covers the slower case where the rail itself
    is still being replaced when the click is attempted.
    """
    last: Exception | None = None
    for attempt in range(_ACTIVITY_ATTEMPTS):
        try:
            await page.click('[aria-label*="Activity"]', timeout=10000)
            await page.wait_for_selector('[data-tid="activity-list-container"]',
                                         timeout=20000)
            return
        except Exception as exc:          # detached, still rendering, or not there yet
            last = exc
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(
        f"could not open the Teams Activity feed after {_ACTIVITY_ATTEMPTS} tries: {last}")


async def read_activity_rows(limit: int = 25) -> list[dict]:
    """Activity feed rows as {text, unread}, newest first. Zero LLM tokens.

    Read state matters because he reads Teams on his phone too: a mention he has
    already opened must stop being pushed. `unread` is None when the page gave no
    usable signal — the caller must then suppress nothing.
    """
    async with _lock:
        pw, ctx = await _launch()
        try:
            page = await _open_teams(ctx)
            await _open_activity(page)
            await asyncio.sleep(3)  # virtualized feed renders after the header
            rows = await page.evaluate(
                """() => {
                    const boxes = Array.from(document.querySelectorAll('[role="listbox"]'))
                      .filter(b => !b.closest('[data-tid="ms-searchux-popup"]')
                                   && b.innerText.trim().length > 20);
                    if (!boxes.length) return [];
                    const box = boxes.sort((a, b) => b.innerText.length - a.innerText.length)[0];
                    let nodes = box.querySelectorAll('[role="option"]');
                    if (!nodes.length) nodes = box.querySelectorAll(':scope > div > div');
                    const out = [];
                    for (const n of nodes) {
                      const lines = n.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
                      if (lines.length < 2) continue;
                      // Teams marks an unread row several ways depending on build:
                      // the accessible name, an explicit unread test-id, or the
                      // little dot. Any of them counts; none of them => unknown.
                      const label = (n.getAttribute('aria-label') || '') + ' ' +
                                    (n.getAttribute('aria-describedby') || '');
                      const marked = /unread/i.test(label)
                        || !!n.querySelector('[data-tid*="unread" i], [class*="unread" i]');
                      out.push({text: lines.slice(0, 4).join(' — '), unread: marked});
                    }
                    return out;
                }""")
            store.kv_set("teams_session_ok", "1")
            rows = rows[:limit]
            # If NOTHING is marked unread the selectors probably just missed on this
            # build — that is unknown, not "he has read everything". Saying unknown
            # keeps the old behaviour (push it) instead of going silent on him.
            if rows and not any(r.get("unread") for r in rows):
                for r in rows:
                    r["unread"] = None
            return rows
        finally:
            await ctx.close()
            await pw.stop()


async def read_activity(limit: int = 25) -> list[str]:
    """Scrape the Teams Activity feed (mentions, replies, calls — muted chats
    included). Returns one-line strings, newest first. Zero LLM tokens."""
    return [r["text"] for r in await read_activity_rows(limit)]


# Background poll is the SAFETY NET (so a mention can reach you on WhatsApp when
# you're not asking). On-demand reads — "any messages for me?" — go through
# read_activity() directly and are always live. 0 disables the poll entirely.
# Someone pinging Arun on Teams is the most time-sensitive thing Asta watches, and
# it was the slowest: half an hour meant a colleague could ask, wait, and give up
# before he was told. Five minutes is the promise; the poll has to match it.
ACTIVITY_POLL_SECONDS = int(os.environ.get("TEAMS_ACTIVITY_POLL", "300"))
ACTIVITY_SEEN_KEY = "teams_activity_seen"
# feed entries worth pinging about; reactions are deliberately excluded as noise
_ACTIVITY_INTERESTING = ("mentioned you", "missed call", "invited you", "replied to")
# Addressed to Arun personally → always delivered, even if he's at the laptop.
# "mentioned Everyone" / "mentioned General" are channel-wide shouts, not him.
_DIRECT_MARKERS = ("mentioned you", "missed call", "in chat with you", "replied to you")


def _activity_key(item: str) -> str:
    """Dedup identity for a feed row.

    Was `item[:150]` — the raw rendering, which carries the item's relative age
    ("2m", "1h", "Yesterday"). That drifts on its own, so the same mention keyed
    differently on every poll, looked new each time, and was pushed again for as
    long as it sat in the feed. Keying on the time-stripped text ends that.
    """
    from . import triage
    return triage.stable_key(item)




def _activity_wanted(item: str) -> bool:
    t = item.lower()
    if "reacted to your message" in t:
        return False
    if any(p in t for p in _ACTIVITY_INTERESTING):
        return True
    from . import msnotify
    return any(k in t for k in msnotify.keywords())


async def _push_activity(notify, wanted: list[str]) -> None:
    """One Teams message: real asks first, everything else a quiet line each.

    Being @mentioned is not the same as being asked for something, so the split is
    now on whether anyone actually wants a move from him — not merely on whether
    his name appeared.
    """
    from . import attention, triage
    verdicts = []
    for it in wanted[:12]:
        who, _, rest = it.partition(" — ")
        addressed = any(m in it.lower() for m in _DIRECT_MARKERS)
        v = triage.classify(who, rest or it, addressed=addressed)
        v = await triage.refine(v, who, rest or it)
        led_key = attention.key_for(it)
        pri, why, due = attention.score(v.action, it, addressed=addressed,
                                        key=led_key, who=who)
        pri, chased = attention.escalate_for_chase(pri, led_key)
        if not attention.consider("teams", led_key, who=who, what=v.one_line,
                                  why=chased or why, priority=pri, due_at=due):
            continue
        verdicts.append(v.ranked(pri, why, due) if attention.enabled() else v)
    text, needs = triage.summarize(verdicts, "💬 Teams")
    if text:
        ranks = [v.priority for v in verdicts if v.priority is not None]
        await notify.notify(text, "teams", urgency="direct" if needs else "ambient",
                            priority=min(ranks) if ranks else None)


async def activity_watch_loop() -> None:
    """Poll the Activity feed and push NEW mentions/replies/missed calls.

    Replaces the macOS-banner watcher's blind spots: works for muted chats,
    DND, and with notifications disabled — it reads Teams itself.
    """
    import json as _json
    from . import attention, notify
    attention.note_watching("teams")     # running, and expected to succeed
    while True:
        await asyncio.sleep(ACTIVITY_POLL_SECONDS)
        if not (enabled() and logged_in_once() and store.kv_get("teams_session_ok") != "0"):
            continue
        try:
            rows = await read_activity_rows()
        except Exception as exc:
            # Still swallowed — a transient DOM hiccup must not kill the loop.
            # But the REASON is kept now. This handler ran silently every five
            # minutes while the watcher was dead, and nothing anywhere said so.
            attention.note_scrape_error("teams", exc)
            continue
        attention.note_scrape("teams")   # only on success — see attention.stale_sources
        if not rows:
            continue
        items = [r["text"] for r in rows]
        raw = store.kv_get(ACTIVITY_SEEN_KEY)
        seen: set[str] | None = set(_json.loads(raw)) if raw else None
        fresh = [] if seen is None else [it for it in items if _activity_key(it) not in seen]
        keys = [_activity_key(it) for it in items]
        if seen is not None:
            keys = keys + [k for k in seen if k not in keys][:300 - len(keys)]
        store.kv_set(ACTIVITY_SEEN_KEY, _json.dumps(keys[:300]))
        # He reads Teams on his phone too — anything he has already opened is
        # settled, and pushing it again is exactly the noise he complained about.
        opened = {_activity_key(r["text"]) for r in rows if r.get("unread") is False}
        fresh = [it for it in fresh if _activity_key(it) not in opened]
        # He opened it on his phone. That was already known here and thrown away
        # after suppressing the re-push — but it is also the cheapest honest
        # answer to "was that interruption worth making", so the ledger gets told.
        for r in rows:
            if r.get("unread") is False:
                attention.note_read(attention.key_for(r["text"]))
        wanted = [it for it in fresh if _activity_wanted(it)]
        if not wanted:
            continue
        await _push_activity(notify, wanted)


async def check_session() -> bool:
    """Headless probe: is the stored Teams session still valid?"""
    async with _lock:
        pw, ctx = await _launch()
        try:
            await _open_teams(ctx)
            store.kv_set("teams_session_ok", "1")
            return True
        except RuntimeError as exc:
            if "SESSION_EXPIRED" in str(exc):
                store.kv_set("teams_session_ok", "0")
                return False
            raise
        finally:
            await ctx.close()
            await pw.stop()


async def session_watch_loop() -> None:
    """Every 30 min verify the session; notify ONCE when it expires."""
    from . import notify
    while True:
        await asyncio.sleep(SESSION_CHECK_SECONDS)
        if not logged_in_once():
            continue
        try:
            was_ok = store.kv_get("teams_session_ok") != "0"
            ok = await check_session()
            if was_ok and not ok:
                await notify.notify(
                    "Teams session expired — run `.venv/bin/python -m app.teams_bridge login` "
                    "to reconnect (your organisation's SSO).", "warn")
        except Exception:
            pass


async def _login() -> None:
    """Headed one-time login: user completes your organisation's SSO themselves."""
    print(f"Opening Teams — complete the your organisation's SSO login in the window.")
    print(f"Profile stored at: {PROFILE_DIR}")
    pw, ctx = await _launch(headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
        print("Waiting for you to finish login (up to 5 minutes)…")
        await page.wait_for_selector(APP_MARKERS, timeout=300000)
        store.kv_set("teams_session_ok", "1")
        print("Logged in. Session saved — Asta can now read/send Teams messages headlessly.")
    finally:
        await ctx.close()
        await pw.stop()


if __name__ == "__main__":
    store.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        asyncio.run(_login())
    elif cmd == "check":
        print("session ok" if asyncio.run(check_session()) else "SESSION EXPIRED — rerun login")
    elif cmd == "activity":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 25
        try:
            for line in asyncio.run(read_activity(limit)):
                print(line)
        except RuntimeError as exc:
            print(f"ERROR: {'session expired — rerun login' if 'SESSION_EXPIRED' in str(exc) else exc}")
            sys.exit(1)
    elif cmd == "read" and len(sys.argv) > 2:
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 15
        try:
            for line in asyncio.run(read_chat(sys.argv[2], limit)):
                print(line)
        except RuntimeError as exc:
            print(f"ERROR: {'session expired — rerun login' if 'SESSION_EXPIRED' in str(exc) else exc}")
            sys.exit(1)
    elif cmd == "send" and len(sys.argv) > 3:
        try:
            # --group must be spelled out; a bare `send <name>` is always 1:1
            to_group = "--group" in sys.argv[4:]
            where = asyncio.run(send_message(sys.argv[2], sys.argv[3], allow_group=to_group))
            print(f"sent to: {where}")
        except RuntimeError as exc:
            print(f"ERROR: {'session expired — rerun login' if 'SESSION_EXPIRED' in str(exc) else exc}")
            sys.exit(1)
    else:
        print(__doc__)
        print("Usage: python -m app.teams_bridge login|check|activity [limit]|read <chat> [limit]"
              "|send <person> <text> [--group]")
        print("NOTE: `send` targets a PERSON's 1:1 chat. Group/channel sends require --group.")
