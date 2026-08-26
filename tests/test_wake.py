"""Noticing that the laptop was asleep.

The symptom being tested: Arun closes the lid at 23:00, opens it at 08:00, and
Asta carries on with the four minutes left on a five-minute timer as though
nothing happened — no catch-up scan, no idea it missed nine hours, and a first
poll fired into a network that has not come back yet.

Detection is a comparison of two clocks, so the tests drive two fake clocks
rather than actually suspending anything.
"""

from __future__ import annotations

import asyncio

import pytest

from app import wake

#: Captured before any test patches it. `wake.asyncio` is the asyncio module
#: itself, so patching `wake.asyncio.sleep` patches it EVERYWHERE — including
#: inside the fake that does the patching, which recurses instead of yielding.
_real_sleep = asyncio.sleep


def _driver(clocks, ticks: int, on_tick=None):
    """An asyncio.sleep stand-in that advances the fake clocks and then stops.

    Returns the replacement; each call is one turn of the watch loop.
    """
    count = {"n": 0}

    async def fake_sleep(seconds, *a, **k):
        count["n"] += 1
        if count["n"] > ticks:
            raise asyncio.CancelledError
        if on_tick:
            on_tick(count["n"])
        else:
            clocks.tick(seconds)
        await _real_sleep(0)

    return fake_sleep


@pytest.fixture(autouse=True)
def _reset_generation():
    wake._generation = 0
    wake._woke = None
    yield
    wake._generation = 0
    wake._woke = None


class Clocks:
    """A wall clock and a monotonic clock that can be advanced independently.

    That independence IS the signal: during suspend the wall clock keeps going
    and the monotonic one does not, so advancing only `wall` is what "the
    machine was asleep" looks like from inside the process.
    """

    def __init__(self):
        self.wall = 1_000_000.0
        self.mono = 5_000.0

    def tick(self, seconds: float):
        """Time passing normally — both clocks move together."""
        self.wall += seconds
        self.mono += seconds

    def suspend(self, seconds: float):
        """The lid was shut. Only the wall clock notices."""
        self.wall += seconds


@pytest.fixture
def clocks(monkeypatch):
    c = Clocks()
    monkeypatch.setattr(wake.time, "time", lambda: c.wall)
    monkeypatch.setattr(wake.time, "monotonic", lambda: c.mono)
    return c


async def _run_watch_for(ticks: int, clocks: Clocks, monkeypatch,
                         on_tick=None) -> list[float]:
    """Drive wake.watch_loop for N ticks with sleeps and the network stubbed."""
    seen: list[float] = []

    monkeypatch.setattr(wake.asyncio, "sleep", _driver(clocks, ticks, on_tick))
    monkeypatch.setattr(wake, "wait_for_network", lambda *a, **k: _true())

    async def spy_announce(gap, had_network):
        seen.append(gap)

    monkeypatch.setattr(wake, "_announce", spy_announce)

    with pytest.raises(asyncio.CancelledError):
        await wake.watch_loop()
    return seen


async def _true():
    return True


@pytest.mark.asyncio
async def test_normal_running_is_never_mistaken_for_sleep(clocks, monkeypatch):
    """Both clocks advancing together is just the machine being on."""
    seen = await _run_watch_for(5, clocks, monkeypatch)
    assert seen == []
    assert wake.generation() == 0


@pytest.mark.asyncio
async def test_an_overnight_suspend_is_detected(clocks, monkeypatch):
    """Nine hours with the lid shut."""
    def tick(n):
        clocks.tick(wake.TICK_SECONDS)
        if n == 2:
            clocks.suspend(9 * 3600)

    seen = await _run_watch_for(4, clocks, monkeypatch, on_tick=tick)
    assert len(seen) == 1
    assert 9 * 3600 - 60 < seen[0] < 9 * 3600 + 60, seen
    assert wake.generation() == 1


@pytest.mark.asyncio
async def test_a_short_stall_does_not_count_as_sleep(clocks, monkeypatch):
    """A busy event loop or a slow turn must not fire a catch-up sweep."""
    def tick(n):
        clocks.tick(wake.TICK_SECONDS)
        if n == 2:
            clocks.suspend(30)   # well under MIN_GAP_SECONDS

    seen = await _run_watch_for(4, clocks, monkeypatch, on_tick=tick)
    assert seen == []


@pytest.mark.asyncio
async def test_the_gap_is_not_counted_twice(clocks, monkeypatch):
    """Re-baselining after the announce; otherwise every later tick re-reports."""
    def tick(n):
        clocks.tick(wake.TICK_SECONDS)
        if n == 2:
            clocks.suspend(4 * 3600)

    seen = await _run_watch_for(8, clocks, monkeypatch, on_tick=tick)
    assert len(seen) == 1, f"phantom second wake: {seen}"


@pytest.mark.asyncio
async def test_sleep_returns_early_when_the_machine_wakes():
    """The whole point: a watcher mid-poll-interval is pulled forward."""
    async def sleeper():
        return await wake.sleep(30)

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0.01)
    wake._mark_wake(3600)
    woke = await asyncio.wait_for(task, timeout=2)
    assert woke is True


@pytest.mark.asyncio
async def test_sleep_reports_a_normal_timeout_as_not_woken():
    assert await wake.sleep(0.01) is False


@pytest.mark.asyncio
async def test_a_wake_between_two_sleeps_is_still_seen():
    """Why the counter exists rather than a bare Event.

    An Event set while nothing is waiting is cleared and forgotten; a watcher
    that was between polls at that instant would sleep out its full interval and
    the wake would be lost.
    """
    wake._mark_wake(3600)          # fires while nobody is waiting
    before = wake.generation()
    assert await wake.sleep(0.01) is False
    assert wake.generation() == before


@pytest.mark.asyncio
async def test_watchers_are_released_only_after_the_network_is_back(clocks, monkeypatch):
    """Order matters — releasing first spends the catch-up scan on a dead interface."""
    order: list[str] = []

    async def slow_network(*a, **k):
        order.append("network")
        return True

    monkeypatch.setattr(wake, "wait_for_network", slow_network)

    real_mark = wake._mark_wake

    def spy_mark(gap):
        order.append("release")
        real_mark(gap)

    monkeypatch.setattr(wake, "_mark_wake", spy_mark)

    async def quiet(gap, had_network):
        order.append("announce")

    monkeypatch.setattr(wake, "_announce", quiet)

    def tick(n):
        clocks.tick(wake.TICK_SECONDS)
        if n == 2:
            clocks.suspend(5 * 3600)

    monkeypatch.setattr(wake.asyncio, "sleep", _driver(clocks, 4, tick))
    with pytest.raises(asyncio.CancelledError):
        await wake.watch_loop()

    assert order.index("network") < order.index("release"), order


@pytest.mark.asyncio
async def test_a_failed_announce_still_releases_the_watchers(clocks, monkeypatch):
    """Telling him is nice; scanning is the job. One must not block the other."""
    async def broken(gap, had_network):
        raise RuntimeError("whatsapp down")

    monkeypatch.setattr(wake, "_announce", broken)
    monkeypatch.setattr(wake, "wait_for_network", lambda *a, **k: _true())

    def tick(n):
        clocks.tick(wake.TICK_SECONDS)
        if n == 2:
            clocks.suspend(6 * 3600)

    monkeypatch.setattr(wake.asyncio, "sleep", _driver(clocks, 4, tick))
    with pytest.raises(asyncio.CancelledError):
        await wake.watch_loop()

    assert wake.generation() == 1, "watchers were never released"


@pytest.mark.asyncio
async def test_a_short_absence_catches_up_without_announcing(clocks, monkeypatch):
    """Closing the lid to walk to a meeting room is not news when he gets back."""
    def tick(n):
        clocks.tick(wake.TICK_SECONDS)
        if n == 2:
            clocks.suspend(10 * 60)   # over MIN_GAP, under ANNOUNCE_AFTER

    seen = await _run_watch_for(4, clocks, monkeypatch, on_tick=tick)
    assert seen == [], "announced a ten-minute absence"
    assert wake.generation() == 1, "skipped the catch-up scan as well"


@pytest.mark.asyncio
async def test_the_announcement_is_ambient_so_it_does_not_ping_him_at_his_desk(
        monkeypatch):
    """Waking the machine IS him sitting down at it — his rule there is silence.

    The catch-up items still arrive as "direct" through the normal pipeline;
    this framing line must not.
    """
    sent = {}
    from app import notify as notify_mod

    async def spy(text, level="info", urgency="direct", priority=None):
        sent.update(text=text, urgency=urgency)
        return {}

    monkeypatch.setattr(notify_mod, "notify", spy)
    await wake._announce(9 * 3600, True)
    assert sent["urgency"] == "ambient"
    assert "9h 00m" in sent["text"]


def test_describe_reads_the_way_he_would_say_it():
    assert wake.describe(9 * 3600 + 12 * 60) == "9h 12m"
    assert wake.describe(45 * 60) == "45m"


def test_last_gap_survives_a_restart():
    """It goes through the store, so the catch-up survives the process dying."""
    wake._mark_wake(7200)
    when, gap = wake.last_gap()
    assert gap == 7200
    assert when > 0


def test_last_gap_is_zero_when_nothing_was_ever_recorded():
    assert wake.last_gap() == (0.0, 0.0)


@pytest.mark.asyncio
async def test_scanning_happens_even_when_the_probe_never_answers(monkeypatch):
    """A corporate network can block 1.1.1.1 and route Teams perfectly well.

    Trusting the probe over the real thing would turn a false negative into
    permanent silence, which is the failure this whole change is about.
    """
    async def never(*a, **k):
        return False

    monkeypatch.setattr(wake, "online", never)
    assert await wake.wait_for_network(limit=0) is False
