"""An outward act that quietly did not happen is worse than one that failed loudly.

2026-09-07: Asta reported "Calling a colleague now to talk through the two open review
comments — I'll send you what was said when it ends." Twenty-five minutes later there
was no log line, no notification, no outcome row, and no call. The reply was a
claim with nothing behind it.

`discuss_in_call` did `create_task(_go())` and kept no reference. Asyncio holds
only a WEAK reference to a task, so an unreferenced one can be garbage-collected
mid-flight — a fact `daemon.start` documents in its own docstring, two modules
away from the code that needed it.
"""

from __future__ import annotations

import asyncio

import pytest

from app import daemon


def test_a_one_shot_job_is_held_while_it_runs():
    """The reference bug, directly: if nothing holds it, it can vanish."""
    async def go():
        held = list(daemon._ONCE)
        assert held, "the task is not referenced anywhere — it can be collected"
        await asyncio.sleep(0)

    async def main():
        t = daemon.once("probe", go())
        await t

    asyncio.run(main())


def test_it_lets_go_when_finished():
    """Held forever is a leak; this runs per call and per meeting."""
    async def main():
        await daemon.once("probe", asyncio.sleep(0))
        await asyncio.sleep(0)
        assert not [t for t in daemon._ONCE if not t.done()]

    asyncio.run(main())


def test_a_failure_is_reported_not_swallowed(monkeypatch):
    """The half that made this invisible. `_go()` had no except, so a failure
    became "Task exception was never retrieved" — if it was logged at all."""
    said: list[str] = []

    async def fake_notify(text, *a, **k):
        said.append(text)

    from app import notify, store
    monkeypatch.setattr(notify, "notify", fake_notify)

    async def boom():
        raise RuntimeError("teams never opened")

    async def main():
        await daemon.once("call:Alex", boom())

    asyncio.run(main())
    assert said and "did not complete" in said[0]
    assert "teams never opened" in said[0]
    rows = [r for r in store.recent_outcomes(10) if r["kind"] == "background"]
    assert rows and rows[0]["subject"] == "call:Alex"


def test_a_cancelled_job_is_not_reported_as_a_failure(monkeypatch):
    """Shutdown cancels background work. That is not something to wake him for."""
    said: list[str] = []

    async def fake_notify(text, *a, **k):
        said.append(text)

    from app import notify
    monkeypatch.setattr(notify, "notify", fake_notify)

    async def forever():
        await asyncio.sleep(3600)

    async def main():
        t = daemon.once("call:X", forever())
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
    assert said == []


def test_the_outward_paths_use_it():
    """The three that ring a phone or join a room. A fire-and-forget there is a
    claim Asta cannot back."""
    from pathlib import Path
    body = Path("app/agent.py").read_text()
    assert body.count("daemon.once(") >= 3
    assert "_asyncio.create_task(_go())" not in body
    assert "_asyncio.create_task(meetings.watch_and_report" not in body
