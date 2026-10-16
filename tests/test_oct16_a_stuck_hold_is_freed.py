"""16 Oct: Teams and Outlook went unread from 03:51 to 10:51.

A wake reset at 04:07 closed the browser with something still holding the one
Teams lock. The watchdog frees a stuck hold by closing the browser — with none
open it did nothing. Every reader queued behind the hold, every daemon "alive",
and the hold's owner was reported as "tasks.wait_for" (asyncio's own frame).
"""

import asyncio
import time

from app import daemon, store, teams_bridge


class _Page:
    def is_closed(self):
        return False


def _no_browser(monkeypatch):
    async def page():
        return _Page()

    monkeypatch.setattr(teams_bridge, "_pooled_page", page)
    monkeypatch.setattr(teams_bridge, "in_a_call", lambda: False)
    teams_bridge._POOL.clear()


def test_the_holder_is_named_through_asyncio(monkeypatch):
    _no_browser(monkeypatch)
    seen = {}

    async def chat_reader():
        async with teams_bridge.teams_page():
            seen["who"] = teams_bridge.holder()

    async def go():
        await asyncio.wait_for(chat_reader(), 5)

    asyncio.run(go())
    assert seen["who"].endswith(".chat_reader"), seen["who"]


def test_a_hold_with_no_browser_open_is_taken_back_and_the_daemon_restarts(monkeypatch):
    _no_browser(monkeypatch)
    monkeypatch.setattr(daemon, "BACKOFF_START", 0.01, raising=False)
    runs = []

    async def reader():
        runs.append(time.time())
        async with teams_bridge.teams_page():
            if len(runs) == 1:
                await asyncio.Event().wait()          # stuck forever, holding the lock

    async def go():
        task = daemon.start("teams_chats_test", reader)
        while not teams_bridge.stuck_for():
            await asyncio.sleep(0.01)
        teams_bridge._HELD["since"] = time.time() - 10_000
        await teams_bridge.free("an operation held the browser for 10000s")
        for _ in range(200):
            if len(runs) >= 2:
                break
            await asyncio.sleep(0.01)
        assert not task.done(), "the daemon was stopped, not restarted"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    assert len(runs) >= 2                             # freed, restarted, read again
    assert not teams_bridge._lock.locked()
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome='cancelled holder'").fetchone()[0] == 1


def test_with_a_browser_open_it_is_closed_as_before(monkeypatch):
    called = []

    async def reset(why):
        called.append(why)

    monkeypatch.setattr(teams_bridge, "reset", reset)
    monkeypatch.setattr(teams_bridge, "in_a_call", lambda: False)
    teams_bridge._POOL["page"] = _Page()
    try:
        asyncio.run(teams_bridge.free("stuck"))
    finally:
        teams_bridge._POOL.clear()
    assert called == ["stuck"]


def test_never_during_a_call(monkeypatch):
    monkeypatch.setattr(teams_bridge, "in_a_call", lambda: True)
    teams_bridge._HELD["task"] = None
    asyncio.run(teams_bridge.free("stuck"))          # nothing to cancel, nothing raised
