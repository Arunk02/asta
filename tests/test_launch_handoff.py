"""Real Chrome exits quietly where Chrome for Testing fails loudly.

Given a `--user-data-dir` somebody still owns:

  Chrome for Testing -> "Failed to create a ProcessSingleton", a real exception
  real Chrome        -> hands the command to the owning instance and exits 0

So Playwright is handed a context that is already closed, and the next call dies
with "TargetClosedError: BrowserContext.new_page". From outside it is a window
appearing and vanishing — "Chrome getting close while coming up only".

`close_pool()` returns as soon as it has ASKED the browser to go; the process is
still on its way out. Three voice checks in a row failed this way on 2026-09-07,
immediately after switching binaries to get a microphone.
"""

from __future__ import annotations

import asyncio

from app import teams_bridge as tb


def test_it_waits_for_the_last_browser_to_exit(monkeypatch):
    """The fix: wait, do not press on. Chrome will not tell us it lost."""
    alive = {"n": 4}

    def draining():
        alive["n"] -= 1
        return [777] if alive["n"] > 0 else []

    monkeypatch.setattr(tb, "profile_processes", draining)
    asyncio.run(tb._wait_profile_free(timeout=5))
    assert alive["n"] <= 0


def test_it_returns_at_once_when_the_profile_is_free(monkeypatch):
    """The common case — no browser, no delay before every launch."""
    monkeypatch.setattr(tb, "profile_processes", lambda: [])

    async def timed():
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await tb._wait_profile_free(timeout=5)
        return loop.time() - t0

    assert asyncio.run(timed()) < 0.2


def test_it_gives_up_rather_than_hanging(monkeypatch):
    """A browser that never exits must not wedge every future launch."""
    monkeypatch.setattr(tb, "profile_processes", lambda: [777])

    async def timed():
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await tb._wait_profile_free(timeout=0.6)
        return loop.time() - t0

    assert 0.5 < asyncio.run(timed()) < 3.0


def test_it_never_kills_anything(monkeypatch):
    """Reaping is what hung up on a colleague mid-dial. Waiting is the whole point."""
    killed = {"n": 0}
    monkeypatch.setattr(tb, "reap_orphans", lambda: killed.__setitem__("n", 1))
    monkeypatch.setattr(tb, "profile_processes", lambda: [])
    asyncio.run(tb._wait_profile_free(timeout=0.3))
    assert killed["n"] == 0


def test_launch_waits_before_it_launches():
    """Read from the file: conftest seals `_launch` so a test cannot open a
    browser, so `inspect` would read the stub."""
    from pathlib import Path
    src = Path("app/teams_bridge.py").read_text()
    body = src[src.index("async def _launch("):]
    body = body[:body.index("_launch_ctx(pw")]
    assert "_wait_profile_free()" in body


# --- a call owns the browser ------------------------------------------------------

def test_the_shared_pool_stands_down_during_a_call(monkeypatch):
    """Chromium tolerates one writer per profile. A background reader launching
    a second browser during a call does not read slowly — it contends, and real
    Chrome resolves that by handing off and exiting silently.

    Three calls died this way: dialled, then eleven seconds later
    "TargetClosedError: Keyboard.press" while still searching for the name.
    """
    from app import meetings
    monkeypatch.setattr(meetings, "_CALL", {"page": object()})

    async def _pool_dead():
        return False

    monkeypatch.setattr(tb, "_pool_alive", _pool_dead)
    launched = {"n": 0}

    async def _no(*a, **k):
        launched["n"] += 1

    monkeypatch.setattr(tb, "_launch", _no)

    async def go():
        try:
            await tb._pooled_page()
        except RuntimeError as exc:
            return str(exc)
        return ""

    msg = asyncio.run(go())
    assert "call is in progress" in msg
    assert launched["n"] == 0, "it launched a competing browser during a call"


def test_the_pool_works_normally_when_no_call_is_live(monkeypatch):
    """The guard must not become a way to stop reading his messages."""
    from app import meetings
    monkeypatch.setattr(meetings, "_CALL", {})

    async def _alive():
        return True

    monkeypatch.setattr(tb, "_pool_alive", _alive)
    monkeypatch.setitem(tb._POOL, "page", "the-page")
    assert asyncio.run(tb._pooled_page()) == "the-page"


def test_one_definition_of_in_a_call():
    """chat_watch defers to the bridge — two answers to "is a call live" is how
    they drift apart."""
    from app import chat_watch, meetings
    meetings._CALL.clear()
    assert chat_watch.in_a_call() is tb.in_a_call() is False


def test_the_claim_covers_setup_not_just_the_placed_call():
    """`_CALL` is what tells background readers to stand down, and it was set
    only AFTER the dial — so launch, Teams load, mic check and chat search all
    ran with it clear, and the 60-second pollers were free to take the profile.
    Four calls died in that window."""
    from pathlib import Path
    src = Path("app/meetings.py").read_text()
    body = src[src.index("async def call_person("):]
    body = body[:body.index("async def join_by_phrase(")]
    claim = body.index('_CALL["claiming"] = True')
    assert claim < body.index("close_pool()"), "claimed after the browser was touched"
    assert claim < body.index("_launch(headless=False)")
    assert "_CALL.clear()" in body, "a failed call must drop its claim"


def test_a_failed_call_does_not_silence_the_chat_reader():
    """A claim that outlives a failed call leaves in_a_call() true for ever, and
    the reader stands down permanently — silence that looks like a quiet day."""
    from pathlib import Path
    src = Path("app/meetings.py").read_text()
    body = src[src.index("async def call_person("):]
    body = body[:body.index("async def join_by_phrase(")]
    tail = body[body.index("finally:"):]
    assert "_CALL.clear()" in tail
