"""Noticing that the laptop was asleep, and doing something about it.

A watcher loop is `await asyncio.sleep(300)` in a circle. On macOS the clock
that drives that sleep does not advance while the machine is suspended, so when
the lid opens after eight hours the loop simply carries on with whatever was
left of its five minutes. It is not broken — it just has no idea that eight
hours happened, and neither does anything else in Asta.

Two consequences, both of which Arun hit:

  - the first poll after wake fires into a network that has not reassociated
    yet. The store still carries the evidence: "Outlook did not load in 70s"
    and "could not open the Teams Activity feed". Those are caught, so nothing
    crashes — the loop just fails and then idles out a FULL interval before
    trying again, so the gap keeps growing after the machine is back;
  - nothing backfills, and nothing tells him what arrived while he was away.

What is fixable here is the wake, not the sleep. While the Mac is genuinely
suspended no local process runs, and no amount of code changes that.

The mechanism is a comparison of two clocks. `time.time()` is wall-clock and
keeps counting through suspend; `time.monotonic()` does not. Tick both, and the
difference between how much each advanced IS the time spent asleep. No IOKit
notifications, no privileges, no polling anything external.

Watchers cooperate by sleeping through `wake.sleep()` instead of
`asyncio.sleep()`, which returns early when a wake is detected — so the machine
coming back is what triggers the next scan, rather than the remainder of a timer
that was set before bedtime.
"""

from __future__ import annotations

import asyncio
import os
import time

from . import store

#: How often to compare the clocks. Also the resolution of gap detection, and
#: the slack that separates "suspended" from "the event loop was busy".
TICK_SECONDS = 20.0

#: Below this, it was scheduling jitter or a slow turn, not a sleep. Above it,
#: the machine was actually away and the world has moved on without us.
MIN_GAP_SECONDS = float(os.environ.get("ASTA_WAKE_MIN_GAP", "180"))

#: Catching up is worth doing after any real gap; SAYING so is not. Closing the
#: lid to walk to a meeting room does not need an announcement when he opens it
#: again — the scan still happens, silently. Only a gap long enough that he has
#: genuinely been out of touch earns a line.
ANNOUNCE_AFTER_SECONDS = float(os.environ.get("ASTA_WAKE_ANNOUNCE_AFTER", "1800"))

#: Give up waiting for the network and let the scans try anyway — being wrong
#: about connectivity must not mean never scanning again.
NETWORK_WAIT_SECONDS = float(os.environ.get("ASTA_WAKE_NET_WAIT", "120"))

#: Cheap reachability probe. A TCP handshake, no DNS trust, no HTTP client.
PROBE_HOST = os.environ.get("ASTA_WAKE_PROBE_HOST", "1.1.1.1")
PROBE_PORT = int(os.environ.get("ASTA_WAKE_PROBE_PORT", "443"))

LAST_GAP_KEY = "wake_last_gap"

#: Bumped on every detected wake. A watcher compares the value it went to sleep
#: under with the value it wakes to, so a wake that lands between two sleeps is
#: still seen — an Event alone would be missed if it fired at the wrong moment.
_generation = 0
_woke: asyncio.Event | None = None


def _event() -> asyncio.Event:
    """The wake flag, created lazily so importing this module needs no loop."""
    global _woke
    if _woke is None:
        _woke = asyncio.Event()
    return _woke


def generation() -> int:
    return _generation


def _mark_wake(gap: float) -> None:
    global _generation
    _generation += 1
    store.kv_set(LAST_GAP_KEY, f"{time.time():.0f}:{gap:.0f}")
    ev = _event()
    ev.set()
    # Cleared immediately: the Event is an interrupt, not a state. Anything that
    # needs to know "did I miss a wake" reads the generation counter instead.
    ev.clear()


def last_gap() -> tuple[float, float]:
    """(when the wake happened, how many seconds were lost). (0, 0) if never."""
    raw = store.kv_get(LAST_GAP_KEY)
    if not raw or ":" not in raw:
        return (0.0, 0.0)
    when, _, gap = raw.partition(":")
    try:
        return (float(when), float(gap))
    except ValueError:
        return (0.0, 0.0)


async def sleep(seconds: float) -> bool:
    """Sleep, but come back early if the machine woke from suspend.

    Returns True when it was cut short by a wake, so a caller can tell a routine
    poll apart from a catch-up one. Drop-in for `asyncio.sleep` otherwise.
    """
    before = _generation
    try:
        await asyncio.wait_for(_event().wait(), timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError):
        pass
    # The counter, not the Event, is the source of truth — a wake raised while
    # this coroutine was not yet waiting still shows up here.
    return _generation != before


async def online(timeout: float = 3.0) -> bool:
    """Can we open a TCP connection out? One handshake, then closed."""
    try:
        fut = asyncio.open_connection(PROBE_HOST, PROBE_PORT)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def wait_for_network(limit: float = NETWORK_WAIT_SECONDS) -> bool:
    """Block until the network answers, or `limit` passes.

    Returns whether it actually came back. Callers scan either way: a probe host
    can be blocked by a corporate network that routes Teams perfectly well, and
    trusting the probe over the real thing would turn a false negative into
    permanent silence.
    """
    deadline = time.monotonic() + max(0.0, limit)
    while True:
        if await online():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(3)


def describe(gap: float) -> str:
    """'8h 12m' — for telling him how long Asta was out."""
    minutes = int(gap // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


async def _announce(gap: float, had_network: bool) -> None:
    """One line so a burst of catch-up notifications has a reason attached.

    Without it the ranked items still arrive, but they look like a normal sweep
    that happens to be large — and he can't tell "these are from the night" from
    "these just happened".
    """
    from . import notify
    note = "" if had_network else " (network still flaky — retrying)"
    # Ambient, deliberately. Waking the machine is precisely the moment he is
    # sitting in front of it, and his rule there is that Asta stays quiet and
    # lets him ask. The line is in the UI bell immediately and goes to his phone
    # only if he walks away again. Anything in the gap that is actually
    # addressed to him still arrives as "direct" through the normal pipeline —
    # this is the framing, not the news.
    await notify.notify(
        f"⏰ Asta was asleep with the laptop for {describe(gap)}. "
        f"Catching up on Teams and Outlook now{note} — anything worth your "
        f"attention follows.",
        "info", urgency="ambient")


async def watch_loop() -> None:
    """Compare the two clocks forever; on a gap, wake the watchers.

    Supervised by `daemon.start`, so an exception here is a restart rather than
    the end of wake detection.
    """
    wall = time.time()
    mono = time.monotonic()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        now_wall, now_mono = time.time(), time.monotonic()
        # How much wall-clock time passed that the monotonic clock did not see.
        gap = (now_wall - wall) - (now_mono - mono)
        wall, mono = now_wall, now_mono
        if gap < MIN_GAP_SECONDS:
            continue
        # Order matters. The watchers are released only AFTER the network is
        # back — release them first and the wake spends its one catch-up scan
        # failing against a dead interface, which is precisely what the
        # un-woken code already did.
        had_network = await wait_for_network()
        if gap >= ANNOUNCE_AFTER_SECONDS:
            try:
                await _announce(gap, had_network)
            except Exception:
                # Telling him is nice; scanning is the job. A dead WhatsApp
                # bridge must not be why the catch-up never happens.
                pass
        # Before the watchers catch up on what they missed: a call cannot have
        # survived the sleep, and believing it did blocks every later call.
        try:
            from . import meetings
            await meetings.drop_call_lost_to_sleep(gap)
        except Exception:
            pass
        _mark_wake(gap)
        # Re-baseline against now rather than the top of the tick: probing and
        # announcing can take a couple of minutes, and leaving that time on the
        # books would have the next comparison measure it all over again.
        wall, mono = time.time(), time.monotonic()


def status() -> dict:
    when, gap = last_gap()
    return {
        "generation": _generation,
        "last_wake_at": when or None,
        "last_gap_seconds": int(gap) or None,
        "last_gap": describe(gap) if gap else None,
        "min_gap_seconds": MIN_GAP_SECONDS,
    }
