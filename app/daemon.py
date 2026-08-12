"""Background loops that cannot die quietly.

Asta's watchers are `while True` loops started with `asyncio.create_task`. That
is fine right up until one of them raises somewhere its own `try` doesn't cover —
then the task ends, asyncio prints "Task exception was never retrieved" into a
log nobody reads, and the loop is gone until the next server restart. Nothing
retries it and nothing anywhere says it is missing.

This is not hypothetical. From data/logs/server.log:

    Task exception was never retrieved
    future: <Task finished coro=<activity_watch_loop() done>
             exception=OperationalError('unable to open database file')>
      File "app/teams_bridge.py", line 559, in activity_watch_loop
        if not (enabled() and logged_in_once() and store.kv_get(...) != "0"):

The `kv_get` guarding the loop was itself outside the guard. One transient SQLite
error and Asta went blind to Teams for the rest of the process's life — which is
what "it couldn't see if anyone sent a message" actually was.

Wrapping the body in a bigger `try` inside each loop is the obvious fix and the
wrong one: it has to be remembered every time a loop is written, and the failure
mode when it is forgotten is silence. So supervision lives out here instead. A
supervised loop cannot die: it is restarted with backoff, and every restart is
recorded where `health` can see it, because a watcher that has restarted forty
times is broken even though it is technically running.

Backoff exists so a loop that fails *immediately* — bad selector, missing
credential — doesn't spin the CPU retrying thousands of times a minute. It grows
to a ceiling and resets once the loop has run cleanly for a while, so a nightly
network blip never inherits yesterday's penalty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from . import store

log = logging.getLogger("asta.daemon")

#: First retry delay, and the ceiling it doubles towards.
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0
#: Run cleanly for this long and the next failure starts from BACKOFF_START
#: again. Without it, a loop that crashes once a day converges on the ceiling
#: and a *transient* fault ends up looking permanent.
HEALTHY_AFTER = 600.0

STATE_KEY = "daemon_state"

#: Live registry, so `status()` reports what is actually running in THIS process
#: rather than what the store remembers from some previous one.
_running: dict[str, dict] = {}


def _load() -> dict:
    raw = store.kv_get(STATE_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _record(name: str, **fields) -> None:
    """Merge facts about one daemon into the durable record.

    Best-effort on purpose: the whole point of this module is surviving a store
    that is momentarily unavailable, so failing to write the note about a crash
    must never itself become the crash.
    """
    try:
        state = _load()
        entry = state.get(name) or {}
        entry.update(fields)
        state[name] = entry
        store.kv_set(STATE_KEY, json.dumps(state))
    except Exception:
        pass


async def supervise(name: str, body: Callable[[], Awaitable[None]]) -> None:
    """Run `body()` forever, restarting it on any failure.

    `body` is expected to be a long-running loop. If it returns on its own that
    is treated as a fault too — a watcher that decides it is finished is a
    watcher that has stopped watching, and it should come back the same way a
    crashed one does.
    """
    delay = BACKOFF_START
    # Updated, never replaced: `start()` has already stashed the task handle in
    # here, and assigning a fresh dict would drop it — leaving the task with only
    # asyncio's weak reference, free to be collected mid-flight. Which is the
    # silent disappearance this module exists to prevent, reintroduced by the
    # module itself.
    live = _running.setdefault(name, {})
    live.update({"started": time.time(), "restarts": 0, "last_error": None})
    _record(name, started=time.time(), restarts=0, last_error=None)

    while True:
        began = time.monotonic()
        try:
            await body()
            reason = "returned"
        except asyncio.CancelledError:
            # Shutdown, not failure. Propagate so the process can actually exit.
            _record(name, stopped=time.time())
            raise
        except BaseException as exc:  # noqa: BLE001 — nothing may escape here
            reason = f"{type(exc).__name__}: {exc}"
            log.exception("daemon %s crashed", name)

        ran_for = time.monotonic() - began
        if ran_for >= HEALTHY_AFTER:
            delay = BACKOFF_START

        live = _running.setdefault(name, {"restarts": 0})
        live["restarts"] = int(live.get("restarts", 0)) + 1
        live["last_error"] = reason
        live["last_failed"] = time.time()
        _record(name, restarts=live["restarts"], last_error=reason,
                last_failed=time.time(), ran_for=round(ran_for, 1))

        await asyncio.sleep(delay)
        delay = min(delay * 2, BACKOFF_MAX)


def start(name: str, body: Callable[[], Awaitable[None]]) -> asyncio.Task:
    """Launch a supervised daemon. Drop-in for `asyncio.create_task(body())`.

    The task reference is kept in `_running` because asyncio only holds a weak
    reference to tasks — an unreferenced task can be garbage-collected mid-flight,
    which is the same silent disappearance this module exists to prevent.
    """
    task = asyncio.create_task(supervise(name, body), name=f"daemon:{name}")
    _running.setdefault(name, {})["task"] = task
    return task


def status() -> dict:
    """What every supervised daemon is doing — for /api/health.

    `alive` is read from the task itself, not from a flag we set hopefully at
    startup, so it reflects reality even if supervision were somehow bypassed.
    """
    out: dict[str, dict] = {}
    for name, live in _running.items():
        task = live.get("task")
        out[name] = {
            "alive": bool(task) and not task.done(),
            "restarts": int(live.get("restarts", 0)),
            "last_error": live.get("last_error"),
        }
    return out


def problems() -> list[str]:
    """Daemons a human should be told about.

    A dead task is the loud case. A high restart count is the quiet one: the loop
    is running, the heartbeat looks fine, and it is in fact failing every few
    seconds — which reads as healthy to anything that only checks liveness.
    """
    out = []
    for name, info in status().items():
        if not info["alive"]:
            out.append(f"{name} (stopped: {info.get('last_error') or 'unknown'})")
        elif info["restarts"] >= 5:
            out.append(f"{name} ({info['restarts']} restarts: {info.get('last_error')})")
    return out
