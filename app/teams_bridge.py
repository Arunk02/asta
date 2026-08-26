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
import contextlib
import json
import os
import time
import re
import sys
from pathlib import Path

from . import quiet, store, wake

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
        # Teams cannot start a call without getUserMedia, and a fresh Playwright
        # context denies microphone access SILENTLY — the call button clicks
        # fine, Teams asks the browser for a mic, is refused, and simply never
        # starts a call. From the outside that looks exactly like a bad
        # selector, which is where two hours went.
        permissions=["microphone", "camera"],
        args=[
            "--disable-blink-features=AutomationControlled",
            # Belt and braces: suppress the media picker Chromium would
            # otherwise raise, which nothing is there to click.
            "--use-fake-ui-for-media-stream",
        ],
    )
    return pw, ctx


# --- one browser, kept alive --------------------------------------------------
#
# Every Teams operation used to launch Chromium, boot the Teams SPA, do its work
# and throw the whole thing away: 2.49 seconds measured — 0.74s launch, 1.75s app
# boot — paid before any actual work, on every read, every send, every poll of the
# activity feed. With a five-minute poll that overhead is invisible; the moment
# Arun asks for something it is most of the wait.
#
# The context is now kept and reused. What makes that safe rather than clever is
# that it is VERIFIED live before every hand-out and discarded on any doubt: a
# stale context costs one relaunch, while a stale context believed healthy costs
# a silent failure in front of somebody.
#
# Headed contexts (calls) are deliberately never pooled — a call owns its window
# for as long as the call lasts, and closing it is what hanging up means.

_POOL: dict = {}
#: Recycled on this cadence regardless of health. A browser alive for hours grows,
#: and Teams' own session handling is happier with a fresh app boot now and then.
POOL_MAX_AGE = float(os.environ.get("TEAMS_POOL_MAX_AGE", "1800"))


async def _pool_alive() -> bool:
    """Whether the pooled page can still be used. Cheap, and never optimistic."""
    page = _POOL.get("page")
    if page is None:
        return False
    if time.time() - _POOL.get("born", 0) > POOL_MAX_AGE:
        return False
    try:
        # A real round-trip to the page. `page.is_closed()` alone lies: the tab can
        # be open while the renderer behind it is gone.
        return bool(await page.evaluate("() => !!document.querySelector('body')"))
    except Exception:
        return False


async def _discard_pool() -> None:
    ctx, pw = _POOL.get("ctx"), _POOL.get("pw")
    _POOL.clear()
    for closer in (ctx, pw):
        if closer is None:
            continue
        with contextlib.suppress(Exception):
            await (closer.close() if hasattr(closer, "close") else closer.stop())


async def _pooled_page():
    """A live, authenticated Teams page — reused when possible, rebuilt when not."""
    if await _pool_alive():
        return _POOL["page"]
    await _discard_pool()
    pw, ctx = await _launch(headless=True)
    try:
        page = await _open_teams(ctx)
    except Exception:
        with contextlib.suppress(Exception):
            await ctx.close()
        with contextlib.suppress(Exception):
            await pw.stop()
        raise
    _POOL.update(pw=pw, ctx=ctx, page=page, born=time.time())
    return page


@contextlib.asynccontextmanager
async def teams_page():
    """The one way a headless operation gets at Teams.

    Serialised by the same lock as before — a Chromium profile is single-writer —
    but the expensive part now happens once rather than per operation.
    """
    async with _lock:
        page = await _pooled_page()
        try:
            yield page
        except Exception:
            # The operation failed with the page in an unknown state: half-typed
            # into a composer, a dialog open, a navigation in flight. Reusing that
            # is how one failure becomes several, so it is thrown away.
            await _discard_pool()
            raise


async def close_pool() -> None:
    """Drop the pooled browser — on shutdown, or when the session is re-logged."""
    async with _lock:
        await _discard_pool()


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


#: How long to wait for the thread header to catch up with the click (10 × 0.4s).
_TITLE_ATTEMPTS = 10
_TITLE_POLL = 0.4


def _title_matches(title: str, wanted: str) -> bool:
    """Whether the open conversation is the one that was asked for.

    Any token, not all: he asks for "Vinish" and the header reads "Vinish Kumar",
    he asks for "Daily deployment slot" and the header carries the full name with
    the environments appended.
    """
    return any(tok in (title or "").lower() for tok in wanted.split())


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


def _display_name(o: dict) -> str:
    """The name Teams shows, without the role/participant tail it appends."""
    return (o.get("text") or "").split("\n")[0].strip()


def _matches(o: dict, wanted: str) -> bool:
    """Whole-word match, not substring.

    Substring was matching "Vinisha Vijay Shetty" for "Vinish" — a different
    person entirely, who then counted as a rival candidate and made an
    unambiguous name look ambiguous. Names are words; "Vinish" is not a partial
    spelling of anybody, it is somebody.
    """
    hay = f"{o.get('aria', '')} {o.get('text', '')}".lower()
    words = set(re.findall(r"[a-z0-9]+", hay))
    return all(tok in words for tok in re.findall(r"[a-z0-9]+", wanted.lower()))


#: The rail on the left, in the order Teams shows it — most recent first. Leaf
#: rows only: the section containers ("Chats", "Favorites") are treeitems too and
#: their text is every child concatenated.
_CHAT_ROWS = """
    () => Array.from(document.querySelectorAll('[role="treeitem"]'))
        .filter(n => !n.querySelector('[role="treeitem"]'))
        .map(n => (n.innerText || '').split('\\n')[0].trim())
        .filter(t => t && t.length < 80)
"""
#: Rail entries that are furniture rather than conversations.
_NOT_A_CHAT = {"copilot", "mentions", "discover", "drafts", "saved", "chats",
               "favorites", "quick views", "new chat", "unread"}


#: Names ever seen on his rail, newest first. Persisted because the rail is
#: VIRTUALISED — only the rendered rows are readable, so two identical runs can
#: see different halves of it. Live-only, "Suraj" resolved on one poll and
#: refused on the next with nothing changed but scroll position, and an assistant
#: that answers differently to the same question twice cannot be trusted with
#: either answer. Remembering makes it monotonic: the set only grows, and it
#: grows toward exactly the people he deals with.
_RAIL_KEY = "teams_rail_names"
_RAIL_MAX = 400


async def recent_chats(page) -> set[str]:
    """Lowercased names of the conversations he actually has, most recent first.

    This is the difference between who Arun talks to and who exists at Maersk. The
    directory has three Vinish Kumars and five Harikas; his rail has one of each,
    because it is a record of real conversations rather than a name index. When he
    means somebody new he types the full name — so the common case is someone
    already here, and that is the case worth being right about.
    """
    try:
        rows = await page.evaluate(_CHAT_ROWS)
    except Exception:
        rows = []               # no rail is a reason to fall back, not to fail
    live = [r.strip().lower() for r in rows
            if r.strip().lower() and r.strip().lower() not in _NOT_A_CHAT]
    try:
        remembered = json.loads(store.kv_get(_RAIL_KEY) or "[]")
    except Exception:
        remembered = []
    # Live first so the most recent conversations stay at the head when capped.
    merged = list(dict.fromkeys([*live, *remembered]))[:_RAIL_MAX]
    if merged != remembered:
        store.kv_set(_RAIL_KEY, json.dumps(merged))
    return set(merged)


def _known(o: dict, chats: set[str]) -> bool:
    """Whether this candidate is someone he already has a conversation with."""
    name = _display_name(o).lower()
    if not name:
        return False
    # "Vinish Kumar" on the rail should match the search row for Vinish Kumar even
    # when one of them carries a trailing "(You)" or an alias in brackets.
    return any(name == c or name.startswith(c) or c.startswith(name) for c in chats)


def _is_top_hit(o: dict) -> bool:
    """Whether Teams itself ranked this as a top hit rather than a directory row.

    Teams splits its own suggestions: TOPHITS is who you actually deal with,
    PEOPLE is everyone else in a 100,000-person company who shares the name. Arun
    has one Vinish; the directory has three plus a Vinisha. Using Teams' own
    ranking beats inventing a heuristic, because it is derived from his real
    interaction history rather than from my guess about names.
    """
    return "TOPHITS" in (o.get("tid") or "").upper()


def _dedupe(options) -> list[dict]:
    """One entry per person/group. Teams lists the same person more than once
    (chat result, contact card, directory hit) and three rows for one human must
    not read as three candidates."""
    seen: dict[str, dict] = {}
    for o in options:
        seen.setdefault(_display_name(o).lower(), o)
    return list(seen.values())


def _one_of(matches: list[dict], asked: str, noun: str,
            chats: set[str] | None = None) -> dict:
    """Narrow to one, or refuse and name the candidates.

    Taking the first of several was the old behaviour, and it is the failure with
    the worst ending: "Kumar" quietly opened Vinish Kumar's chat while four other
    Kumars sat behind it in the same list. Nobody finds out from Asta — they find
    out from the person who received it.

    Four rounds, ordered from fact to inference:

      1. One match. Nothing to decide.
      2. The exact name, so "Vinish Kumar" is not made ambiguous by a "Vinish
         Kumar Balaji" also existing in a company of a hundred thousand people.
      3. Someone he is ALREADY talking to. This is the one that carries the
         common case, and it is Arun's own reasoning: a half-name is nearly
         always somebody in his rail, because when he means a stranger he types
         the full name. An open conversation is a fact about him; directory
         ranking is a guess about the org.
      4. Teams' own top hit — the fallback when the rail is unreadable or the
         person is new.

    Anything still ambiguous is genuinely ambiguous: two people he really does
    both talk to. The names go back to him rather than to a coin toss.
    """
    if len(matches) == 1:
        return matches[0]
    wanted = asked.strip().lower()
    exact = [m for m in matches if _display_name(m).lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    known = [m for m in matches if _known(m, chats or set())]
    if len(known) == 1:
        return known[0]
    top = [m for m in (known or matches) if _is_top_hit(m)]
    if len(top) == 1:
        return top[0]
    pool = known or top or matches
    names = ", ".join(sorted(_display_name(m) or "?" for m in pool))
    talks = " (you have open chats with both)" if len(known) > 1 else ""
    raise RuntimeError(
        f"'{asked}' matches {len(pool)} {noun} in Teams{talks} — {names}. "
        f"Refusing to guess: ask Arun which one he means and use the full name.")


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
    # Read the rail BEFORE opening search — once search takes over, the list of
    # his real conversations is no longer on screen to read.
    chats = await recent_chats(page)
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
               tid: o.getAttribute('data-tid') || '',
           }))""")
    wanted = chat.strip().lower()

    def _kind(o: dict) -> str:
        a = o["aria"].strip().lower()
        for k in ("person", "group chat", "channel", "suggestion", "meeting", "file", "image"):
            if a.startswith(k):
                return k
        return "other"

    people = _dedupe(o for o in options if _kind(o) == "person" and _matches(o, wanted))
    groups = _dedupe(o for o in options
                     if _kind(o) in ("group chat", "channel") and _matches(o, wanted))

    if people:
        pick = _one_of(people, chat, "people", chats)
    elif allow_group and groups:
        pick = _one_of(groups, chat, "groups", chats)
    elif groups:
        raise RuntimeError(
            f"'{chat}' only matches a group/channel ({groups[0]['text'][:60]!r}) — refusing: "
            "Arun's rule is 1:1 unless he names the group explicitly")
    else:
        found = ", ".join(f"{_kind(o)}:{o['text'][:30]}" for o in options[:6]) or "nothing"
        raise RuntimeError(f"no person match for '{chat}' in Teams search (saw: {found})")

    # Mark and click, rather than index into a fresh query_selector_all(). The
    # options were read by an earlier evaluate(), and Teams streams its results
    # in — files, meetings and Loop cards arrive after the people do. Anything
    # that re-resolves the list by POSITION is reading an index from one render
    # against the DOM of a later one, which is how a message ends up in the wrong
    # thread. Choosing and marking happen in the same evaluate, so no re-render
    # can land between them, and page.click then re-resolves the marker itself
    # with Playwright's own auto-retry.
    marked = await page.evaluate(
        """(i) => {
            const opts = document.querySelectorAll('[role="option"]');
            document.querySelectorAll('[data-asta-pick]').forEach(
                e => e.removeAttribute('data-asta-pick'));
            if (!opts[i]) return false;
            opts[i].setAttribute('data-asta-pick', '1');
            return true;
        }""", pick["i"])
    if not marked:
        raise RuntimeError(
            f"the Teams search results changed while picking '{chat}' — nothing opened")
    before = await _chat_title(page)
    await page.click('[data-asta-pick="1"]', timeout=10000)
    await page.wait_for_selector(
        '[data-tid="messageBodyContent"], [data-tid="chat-pane-message"], [role="main"]',
        timeout=20000)

    # Wait for the header to actually BECOME the thread asked for, rather than
    # sleeping two seconds and hoping. That selector above is already satisfied by
    # whichever conversation was open before the click, so on a page that already
    # had one, the old wait proved nothing and the title read straight back the
    # PREVIOUS chat — which the check below then reported as opening the wrong
    # thread. The guard was right; the wait underneath it was the bug.
    title = before
    for _ in range(_TITLE_ATTEMPTS):
        title = await _chat_title(page)
        if title and title != before and _title_matches(title, wanted):
            return title
        await asyncio.sleep(_TITLE_POLL)

    # Last line of defence: if the open conversation is not the one asked for,
    # fail loudly rather than let the caller type into the wrong thread.
    if title and not _title_matches(title, wanted):
        raise RuntimeError(f"opened '{title}' instead of '{chat}' — aborted without typing")
    return title or chat


#: Pull every message out of the open thread WITH the time Teams attached to it.
#:
#: The old version of this read `innerText` and nothing else, which is why a
#: question with a "when" in it could never be answered. Teams exposes the time
#: in more than one shape depending on client version, so every known shape is
#: tried and the first machine-readable one wins:
#:
#:   1. <time datetime="..."> — an ISO string, authoritative when present;
#:   2. a title/aria-label on the timestamp element, which carries the full date
#:      even when the visible text is just "9:14 PM";
#:   3. the message id, which in Teams web is the send time in epoch millis.
#:
#: If none of them yields a time, the message is still returned with sent_at
#: null. Guessing would be worse: a message assigned to the wrong evening is a
#: confident wrong answer, and the whole point of this change is not giving one.
_MESSAGE_JS = """
(limit) => {
  const ITEM = '[data-tid="chat-pane-item"]';
  const nodes = document.querySelectorAll(
    '[data-tid="chat-pane-message"], [data-tid="messageBodyContent"]');
  const seen = new Set();
  const out = [];
  for (const n of Array.from(nodes)) {
    const item = n.closest(ITEM) || n;
    if (seen.has(item)) continue;
    seen.add(item);

    const author = item.querySelector(
      '[data-tid="message-author-name"], [data-tid="threadBodyDisplayName"]');
    const body = item.querySelector('[data-tid="messageBodyContent"]') || n;
    const text = (body.innerText || '').trim();
    if (!text) continue;

    let iso = '', stamp = '';
    const t = item.querySelector('time[datetime], [data-tid="message-timestamp"]');
    if (t) {
      stamp = (t.getAttribute('title') || t.innerText || '').trim();
      iso = t.getAttribute('datetime') || '';
      if (!iso) {
        // "11 August 2026 9:14 PM" in a title attribute parses fine; the bare
        // "9:14 PM" in innerText does not, and must not be treated as a date.
        const label = t.getAttribute('title') || t.getAttribute('aria-label') || '';
        if (label && !isNaN(Date.parse(label))) iso = new Date(label).toISOString();
      }
    }
    if (!iso) {
      // What this Teams build actually ships. There is no <time> element
      // anywhere in a message; the send time is the id of the content div,
      // as `content-1786525691522` — epoch milliseconds. Verified against the
      // live DOM rather than assumed, because the assumed version returned
      // every message with no time at all and looked like it worked.
      const holder = item.querySelector('[id^="content-"], [id^="timestamp-"]')
                     || item;
      const id = holder.id || item.getAttribute('data-mid') || item.id || '';
      const ms = (id.match(/(\\d{13})/) || [])[1];
      if (ms) {
        const d = new Date(Number(ms));
        // A 13-digit number that is not a plausible send time is some other
        // id that happens to be the right length.
        if (!isNaN(d) && d.getFullYear() > 2015) iso = d.toISOString();
      }
    }
    out.push({
      sender: (author?.innerText || 'me').trim(),
      text: text,
      iso: iso,
      stamp: stamp,
    });
  }
  return limit > 0 ? out.slice(-limit) : out;
}
"""

#: Teams virtualises the thread: what is not on screen is not in the DOM. To
#: reach last night's message we have to make Teams render it, which means
#: scrolling the pane up and letting it fetch. Capped so a thread with five
#: years of history cannot turn one question into an unbounded crawl.
_SCROLL_JS = """
() => {
  const sel = ['[data-tid="message-pane-list-viewport"]',
               '[data-tid="messages-pane"]',
               '[role="log"]', '[role="list"]'];
  for (const s of sel) {
    for (const el of document.querySelectorAll(s)) {
      if (el.scrollHeight > el.clientHeight + 50) { el.scrollTop = 0; return true; }
    }
  }
  return false;
}
"""

MAX_SCROLLBACKS = int(os.environ.get("TEAMS_MAX_SCROLLBACK", "12"))
SCROLL_SETTLE_SECONDS = 1.2


def _msg_key(chat: str, m: dict) -> str:
    """Stable identity for one message, so re-reading a thread never duplicates it."""
    import hashlib
    basis = f"{chat}|{m.get('sender','')}|{m.get('iso','')}|{m.get('text','')}"
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:20]


def _to_epoch(iso: str) -> float | None:
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _capture(chat: str, raw: list[dict]) -> list[dict]:
    """Turn scraped rows into storable ones and persist them."""
    rows = []
    for m in raw:
        rows.append({
            "key": _msg_key(chat, m),
            "chat": chat,
            "sender": m.get("sender", ""),
            "text": m.get("text", ""),
            "sent_at": _to_epoch(m.get("iso", "")),
            "stamp": m.get("stamp", ""),
        })
    if rows:
        try:
            store.save_teams_messages(rows)
        except Exception:
            # History is a bonus on top of the read, never a reason to fail it.
            pass
    return rows


def fmt_message(r: dict) -> str:
    """'[Mon 21:14] Vinish Kumar: …' — time first, because that is what was missing."""
    import datetime as _dt
    when = r.get("sent_at")
    if when:
        prefix = _dt.datetime.fromtimestamp(when).strftime("[%a %d %b %H:%M] ")
    elif r.get("stamp"):
        prefix = f"[{r['stamp']}] "
    else:
        prefix = ""
    return f"{prefix}{r.get('sender') or 'me'}: {r.get('text', '')}"


async def read_chat(chat: str, limit: int = 15) -> list[str]:
    """Return the last `limit` messages of a chat as timestamped lines.

    Still returns strings, so every existing caller keeps working — but each one
    now carries when it was sent, and every message read is written to history
    on the way past.
    """
    rows = await read_history(chat, limit=limit)
    return [fmt_message(r) for r in rows]


async def read_history(chat: str, since: float | None = None, limit: int = 200,
                       max_scrolls: int = MAX_SCROLLBACKS) -> list[dict]:
    """Read a thread, scrolling back far enough to cover `since`.

    Returns message dicts oldest-first. Everything read is persisted, so the
    next question about the same thread can often be answered without opening a
    browser at all.
    """
    async with teams_page() as page:
        await _find_chat(page, chat, allow_group=True)  # reading a group is harmless
        title = await _chat_title(page) or chat

        raw = await page.evaluate(_MESSAGE_JS, 0)
        # Only a time-windowed question justifies scrolling. "The last 15
        # messages" is already on screen, and paying seconds to fetch older
        # ones nobody asked for is how a read turns into half a minute.
        for _ in range(max(0, max_scrolls) if since is not None else 0):
            # Stop as soon as the thread reaches back past what was asked
            # for — scrolling further would cost seconds and buy nothing.
            if raw:
                oldest = _to_epoch(raw[0].get("iso", ""))
                if oldest is not None and oldest <= since:
                    break
            if not await page.evaluate(_SCROLL_JS):
                break
            await asyncio.sleep(SCROLL_SETTLE_SECONDS)
            grown = await page.evaluate(_MESSAGE_JS, 0)
            # No new messages loaded means we are at the top of the thread;
            # continuing would spin against an unmoving pane.
            if len(grown) <= len(raw):
                raw = grown
                break
            raw = grown

        store.kv_set("teams_session_ok", "1")

    rows = _capture(title, raw)
    if since is not None:
        rows = [r for r in rows if r["sent_at"] is not None and r["sent_at"] >= since]
    return rows[-limit:] if limit > 0 else rows


async def send_message(chat: str, text: str, allow_group: bool = False) -> str:
    """Send a message to a person's 1:1 chat. Returns the chat it landed in.

    Groups/channels require allow_group=True — Arun's standing rule is that a
    "ping X" means X's personal chat, never a team channel.
    """
    async with teams_page() as page:
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


async def resolve_target(chat: str, allow_group: bool = False) -> dict:
    """Open what a send WOULD target, and report it — without typing anything.

    The send path already refuses to type into a thread it cannot verify, but
    that check happens with the message in hand. This runs the identical
    resolution with nothing to send, which is the only way to answer "who would
    this actually reach" before committing to it — and the only way to exercise
    group targeting without putting a test message in front of fourteen people.
    """
    async with teams_page() as page:
        title = await _find_chat(page, chat, allow_group=allow_group)
        store.kv_set("teams_session_ok", "1")
        return {"asked": chat, "opened": title, "allow_group": allow_group}


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
    async with teams_page() as page:
        return await page.evaluate(
            """() => {
                const el = document.querySelector(
                    '[data-tid="me-control-avatar"], #idna-me-control, [data-tid="me-control"]');
                const label = (el && el.getAttribute('aria-label')) || '';
                const m = label.match(
                    /(Available|Busy|Do not disturb|Be right back|Away|Offline)/i);
                return m ? m[1] : '';
            }""")


async def set_presence(wanted: str) -> str:
    """Set his Teams status. Returns what it actually reads afterwards.

    Verified, not assumed — the same rule send_message follows. A status change
    that silently failed is worse than one that never ran: he thinks he is on Do
    Not Disturb and takes the call he was avoiding.
    """
    label = presence_label(wanted)
    async with teams_page() as page:
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
    async with teams_page() as page:
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
# Sixty seconds, not five minutes. The old interval was chosen when every poll
# cost a browser launch — 2.49s of pure overhead — so polling often was expensive.
# With the context pooled a poll costs 0.01s plus the read, and Arun's actual
# complaint was that a ping does not reach him immediately. Five minutes was the
# thing standing between someone asking him a question and him knowing about it.
ACTIVITY_POLL_SECONDS = int(os.environ.get("TEAMS_ACTIVITY_POLL", "60"))
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
                            priority=min(ranks) if ranks else None,
                            considered=True)   # attention.consider ran above


async def activity_watch_loop() -> None:
    """Poll the Activity feed and push NEW mentions/replies/missed calls.

    Replaces the macOS-banner watcher's blind spots: works for muted chats,
    DND, and with notifications disabled — it reads Teams itself.
    """
    import json as _json
    from . import attention, notify
    attention.note_watching("teams")     # running, and expected to succeed
    while True:
        # wake.sleep, not asyncio.sleep: when the lid opens after eight hours
        # this returns immediately instead of idling out the remainder of a
        # five-minute timer that was set before the machine went under.
        await wake.sleep(ACTIVITY_POLL_SECONDS)
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


async def session_watch_loop() -> None:
    """Every 30 min verify the session; notify ONCE when it expires."""
    from . import notify
    while True:
        # A suspended laptop is the most likely moment for the Teams session to
        # have gone stale, so waking is exactly when it is worth re-checking.
        await wake.sleep(SESSION_CHECK_SECONDS)
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
    elif cmd == "history" and len(sys.argv) > 2:
        from . import when as when_mod
        phrase = sys.argv[3] if len(sys.argv) > 3 else "last night"
        since, until, label = when_mod.parse(phrase)
        print(f"# {sys.argv[2]} — {label} ({when_mod.describe(since, until)})")
        try:
            rows = asyncio.run(read_history(sys.argv[2], since=since))
            for r in rows:
                if r["sent_at"] and r["sent_at"] <= until:
                    print(fmt_message(r))
        except RuntimeError as exc:
            print(f"ERROR: {'session expired — rerun login' if 'SESSION_EXPIRED' in str(exc) else exc}")
            sys.exit(1)
    elif cmd == "search" and len(sys.argv) > 2:
        # Searches what has already been READ — Asta's own record, not all of
        # Teams — so it opens no browser and answers in milliseconds.
        from . import agent as _agent
        print(_agent.teams_search(" ".join(sys.argv[2:])))
    elif cmd == "resolve" and len(sys.argv) > 2:
        # Read-only counterpart to `send`: says who it WOULD reach and stops.
        try:
            r = asyncio.run(resolve_target(sys.argv[2], allow_group="--group" in sys.argv[3:]))
            print(f"would open: {r['opened']}  (nothing sent)")
        except RuntimeError as exc:
            print(f"ERROR: {'session expired — rerun login' if 'SESSION_EXPIRED' in str(exc) else exc}")
            sys.exit(1)
    elif cmd == "call" and len(sys.argv) > 2:
        from . import meetings
        try:
            who = asyncio.run(meetings.call_person(sys.argv[2], video="--video" in sys.argv[3:]))
            print(f"calling: {who}")
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
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
              "|send <person> <text> [--group]|resolve <name> [--group]|call <person> [--video]")
        print("NOTE: `send` targets a PERSON's 1:1 chat. Group/channel sends require --group.")
        print("      `resolve` says who a send WOULD reach and sends nothing.")
