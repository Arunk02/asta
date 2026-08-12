"""A supervised loop must not be killable.

The bug these exist for is in the log verbatim: activity_watch_loop raised
OperationalError from the kv_get on its own first line, the task ended, and Asta
stopped watching Teams for the rest of the process. Nothing retried it and
health had no idea. So the tests are written against that exact shape — raise
from the first statement, and assert the loop is still running afterwards.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import daemon


@pytest.fixture(autouse=True)
def _clean_registry():
    daemon._running.clear()
    yield
    daemon._running.clear()


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Real backoff starts at 5s; these tests would otherwise spend minutes asleep."""
    monkeypatch.setattr(daemon, "BACKOFF_START", 0.001)
    monkeypatch.setattr(daemon, "BACKOFF_MAX", 0.005)


async def _settle(times: int = 12):
    """Let the supervisor cycle.

    Real sleeps, not `sleep(0)`: the backoff between restarts is a genuine timer
    on the event loop clock, and yielding without letting any time pass never
    lets it expire.
    """
    for _ in range(times):
        await asyncio.sleep(0.002)


@pytest.mark.asyncio
async def test_the_exact_crash_that_killed_teams_now_restarts():
    """The regression: OperationalError out of the loop's guard statement."""
    attempts = []

    async def body():
        attempts.append(1)
        # Precisely what store.kv_get raised when the process ran out of file
        # descriptors — outside any try, on the first line of the loop body.
        raise sqlite3.OperationalError("unable to open database file")

    task = daemon.start("teams_activity", body)
    await _settle(40)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(attempts) > 1, "loop was not restarted after the crash that killed it"


@pytest.mark.asyncio
async def test_a_loop_that_returns_is_treated_as_a_failure():
    """A watcher that decides it is finished has stopped watching."""
    runs = []

    async def body():
        runs.append(1)
        return  # no exception — just quietly stops

    task = daemon.start("quitter", body)
    await _settle(40)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(runs) > 1
    assert "returned" in (daemon._running["quitter"]["last_error"] or "")


@pytest.mark.asyncio
async def test_cancellation_stops_it_rather_than_restarting():
    """Shutdown must actually shut down, or the process can never exit."""
    started = asyncio.Event()

    async def body():
        started.set()
        await asyncio.sleep(3600)

    task = daemon.start("sleeper", body)
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()


@pytest.mark.asyncio
async def test_health_sees_a_flapping_loop_that_is_technically_alive():
    """The quiet failure: restarting constantly still counts as 'running'."""
    async def body():
        raise RuntimeError("selector gone")

    task = daemon.start("teams_activity", body)
    await _settle(80)

    problems = daemon.problems()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any("teams_activity" in p for p in problems), problems
    assert any("selector gone" in p for p in problems), problems


@pytest.mark.asyncio
async def test_a_healthy_loop_is_not_reported_as_a_problem():
    async def body():
        await asyncio.sleep(3600)

    task = daemon.start("calm", body)
    await _settle()
    problems = daemon.problems()
    status = daemon.status()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert problems == []
    assert status["calm"]["alive"] is True
    assert status["calm"]["restarts"] == 0


@pytest.mark.asyncio
async def test_backoff_grows_so_an_instant_failure_cannot_spin(monkeypatch):
    """Without growth, a loop failing on line one retries thousands of times a second."""
    delays = []
    real_sleep = asyncio.sleep

    async def spy(seconds, *a, **k):
        # Collapse the wait so the test does not actually sleep 15 seconds, but
        # keep what was ASKED for — that is the thing under test.
        delays.append(seconds)
        return await real_sleep(0)

    monkeypatch.setattr(daemon.asyncio, "sleep", spy)
    monkeypatch.setattr(daemon, "BACKOFF_START", 1.0)
    monkeypatch.setattr(daemon, "BACKOFF_MAX", 8.0)

    async def body():
        raise ValueError("nope")

    task = daemon.start("flapper", body)
    # The spy is global to asyncio, so settling has to use the real sleep it
    # captured — otherwise the test's own pauses land in `delays` as backoff.
    for _ in range(60):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert delays[:4] == [1.0, 2.0, 4.0, 8.0], delays[:4]
    assert max(delays) <= 8.0, "backoff exceeded its ceiling"


@pytest.mark.asyncio
async def test_a_store_that_is_down_does_not_become_the_crash(monkeypatch):
    """Recording the failure must never itself fail the supervisor.

    Otherwise the module written to survive a broken store dies of a broken
    store — which is the same bug one level up.
    """
    def explode(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(daemon.store, "kv_set", explode)
    monkeypatch.setattr(daemon.store, "kv_get", explode)

    attempts = []

    async def body():
        attempts.append(1)
        raise RuntimeError("boom")

    task = daemon.start("teams_activity", body)
    await _settle(40)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(attempts) > 1


@pytest.mark.asyncio
async def test_the_task_is_referenced_so_gc_cannot_collect_it():
    """asyncio keeps only a weak reference; an unheld task can vanish mid-flight."""
    async def body():
        await asyncio.sleep(3600)

    task = daemon.start("held", body)
    await _settle()
    assert daemon._running["held"]["task"] is task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
