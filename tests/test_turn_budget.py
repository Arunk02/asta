"""A stopped turn must say WHICH of three things happened.

`RuntimeError: Copilot CLI turn timed out after 300s` is true and answers none of
the questions Arun actually had: did it finish, is it still going, is it stuck.
These tests hold the line on the distinction, and on not throwing away the work.
"""

from __future__ import annotations

import asyncio

import pytest

from app import turn_budget as tb


class _Stream:
    """A stdout that yields planned chunks, with planned gaps between them."""

    def __init__(self, plan):
        self._plan = list(plan)          # (delay_seconds, bytes) — b"" means EOF

    async def read(self, _n):
        if not self._plan:
            await asyncio.sleep(3600)    # silence for ever
        delay, data = self._plan.pop(0)
        await asyncio.sleep(delay)
        return data


def _drain(plan, total, idle):
    return asyncio.run(tb.drain(_Stream(plan), None, total=total, idle=idle))


def test_a_finished_turn_is_done():
    stop = _drain([(0.01, b"hello "), (0.01, b"world"), (0.01, b"")], total=5, idle=1)
    assert stop.reason == "done"
    assert stop.ok
    assert stop.partial == "hello world"


def test_a_silent_brain_is_stuck_not_slow():
    """No output for the idle window means more time would not help."""
    stop = _drain([(0.01, b"starting")], total=30, idle=0.3)
    assert stop.reason == "idle"
    assert not stop.ok
    assert stop.silent_for >= 0.3
    assert "stuck" in stop.why()
    assert "more time would not have helped" in stop.why().lower()


def test_a_streaming_brain_that_runs_out_is_not_stuck():
    """The opposite case, and it must read differently.

    A brain producing output right up to the ceiling is a long job. Killing it
    with the same sentence as a wedged one is what made "is it working?"
    unanswerable.
    """
    plan = [(0.02, b"step ") for _ in range(200)]
    stop = _drain(plan, total=0.3, idle=5)
    assert stop.reason == "ceiling"
    assert not stop.ok
    assert "still working" in stop.why()
    assert "resum" in stop.why().lower(), "it must say resuming beats retrying"


def test_the_partial_work_is_never_thrown_away():
    """The defect this module exists for.

    Both drivers accumulated every chunk and the timeout branch discarded all of
    it to raise a one-line error. The evidence of what happened existed, in
    memory, and the error path deleted it.
    """
    stop = _drain([(0.01, b"edited Foo.java"), (0.01, b" and Bar.java")],
                  total=30, idle=0.3)
    assert stop.reason == "idle"
    assert "Foo.java" in stop.partial and "Bar.java" in stop.partial
    err = tb.TurnStopped(stop)
    assert "Foo.java" in str(err), "the report must carry what it got through"
    assert err.partial == stop.partial


def test_the_three_reasons_read_differently():
    """Three outcomes, three sentences — or the split bought nothing."""
    said = {r: tb.Stop(r, 300.0, 150.0).why()
            for r in ("idle", "ceiling", "done")}
    assert len({said["idle"], said["ceiling"], said["done"]}) == 3


# --- the heartbeat variant, for the driver that parses events ----------------

def test_heartbeat_guard_separates_wedged_from_busy():
    """claude_cli parses NDJSON, so it reports liveness instead of raw bytes.

    Same policy, one module — the two drivers had byte-identical pump loops and
    drifted anyway.
    """
    async def wedged(beat):
        beat.beat()
        await asyncio.sleep(30)

    async def busy(beat):
        for _ in range(200):
            beat.beat()
            await asyncio.sleep(0.01)

    async def finishes(beat):
        beat.beat()

    async def run(fn, total, idle):
        beat = tb.Heartbeat()
        return await tb.guard(fn(beat), beat, total=total, idle=idle)

    assert asyncio.run(run(wedged, 30, 0.3)).reason == "idle"
    assert asyncio.run(run(busy, 0.3, 5)).reason == "ceiling"
    assert asyncio.run(run(finishes, 5, 1)).reason == "done"


def test_guard_cancels_the_pump_it_abandons():
    """Nothing may keep running behind an answer Arun has already been given.

    A brain left alive after the turn was reported keeps editing files and
    burning quota against a question that has already been answered — and the
    next turn then finds a repo that changed under it.

    The test waits INSIDE the same event loop after the guard returns. An earlier
    version returned from `asyncio.run` immediately, which tore the loop down and
    killed the stray task regardless — so it passed whether or not the cancel
    happened, and the mutation that removed the cancel survived it.
    """
    ran = {"after_stop": False}

    async def keeps_going(beat):
        beat.beat()
        await asyncio.sleep(0.4)
        ran["after_stop"] = True         # only reached if it was NOT cancelled

    async def run():
        beat = tb.Heartbeat()
        stop = await tb.guard(keeps_going(beat), beat, total=30, idle=0.1)
        # Well past the pump's own finish time, in the same loop.
        await asyncio.sleep(0.6)
        return stop

    stop = asyncio.run(run())
    assert stop.reason == "idle"
    assert not ran["after_stop"], "the abandoned pump ran on after the turn was reported"


def test_a_real_exception_is_not_swallowed_as_done():
    """A pump that raises must surface, not be reported as a finished turn."""
    async def explodes(beat):
        beat.beat()
        raise ValueError("the brain died")

    async def run():
        beat = tb.Heartbeat()
        return await tb.guard(explodes(beat), beat, total=5, idle=1)

    with pytest.raises(ValueError, match="the brain died"):
        asyncio.run(run())


def test_the_code_ceiling_clears_the_measured_p90():
    """A ceiling below p90 kills work that was going to succeed.

    Measured baseline, n=46: median 7.7 min, p90 32 min. The ceiling was 30 min,
    so roughly the slowest tenth of code tasks died to their own budget and were
    re-run from nothing — paying the whole cost twice.
    """
    from app import tasks
    assert tasks.TASK_TIMEOUT["code"] / 60 > 32, \
        "the code ceiling is at or below the measured p90 of 32 min"
