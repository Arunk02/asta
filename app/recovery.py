"""Fixing it, instead of describing it — the half Asta never had.

Every prior round on the dead-Teams-watcher problem added a better sensor.
`note_watching`, `never_succeeded`, `note_scrape_error`, `last_error`,
`stale_sources`, `daemon.problems`, `health.checks` — all of them work, and on
the night of 26 August they worked perfectly: the store held the exact cause
("Teams app did not load within 75s"), the exact age (741 minutes) and the exact
subsystem. Then nothing happened for thirteen and a half hours, because every
one of those paths terminates in "tell Arun", and Arun was asleep.

Detection without recovery is a more articulate way of staying broken. This is
the actuator.

The shape is a ladder, cheapest rung first, and it is generic on purpose. A
Teams-specific fix would leave Outlook, the local model, the WhatsApp bridge and
everything added next year with the same hole — which is exactly how this became
a recurring bug rather than a one-off one. A watcher declares how it can be
repaired; the supervisor runs the rungs.

    rung 1  recycle the resource      seconds     silent
    rung 2  restart the subsystem     ~30s        silent
    rung 3  repair the durable state  ~60s        silent
    rung 4  tell him, ONCE, naming everything that was tried

Three properties that stop this from becoming a retry loop that hides failures:

  EVERY ATTEMPT IS RECORDED. `store.record_outcome("recovery", …)` on each rung,
  win or lose. Self-healing that quietly stops healing is the same silent failure
  in a new costume, and the only defence is that the healing itself is measured.

  IT GIVES UP OUT LOUD. When every rung fails he is told, and the message names
  what was attempted so it is a report rather than an alarm.

  IT WILL NOT THRASH. A cooldown per source, so a genuinely broken subsystem is
  repaired at a sane cadence instead of relaunching a browser every poll.
"""

from __future__ import annotations

import os
import time
from typing import Awaitable, Callable

from . import store

#: Consecutive failed polls before the ladder engages. One failure is a hiccup —
#: a network that has not reassociated, a DOM mid-rerender. Three is a pattern.
DEFAULT_THRESHOLD = int(os.environ.get("ASTA_RECOVERY_THRESHOLD", "3"))

#: Minimum seconds between ladder runs for one source. Repairing costs real
#: resources; doing it every poll turns a broken watcher into a busy one.
COOLDOWN_SECONDS = float(os.environ.get("ASTA_RECOVERY_COOLDOWN", "600"))

#: Set to 0 to disable self-healing entirely and go back to being told.
ENABLED = os.environ.get("ASTA_RECOVERY", "1").strip().lower() not in ("0", "false", "no", "off")

_LAST_RUN_KEY = "recovery_last_run:"
_ESCALATED_KEY = "recovery_escalated:"


def _last_run(source: str) -> float:
    try:
        return float(store.kv_get(_LAST_RUN_KEY + source) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cooling_down(source: str, now: float | None = None) -> bool:
    """Whether this source was repaired too recently to try again."""
    now = time.time() if now is None else now
    return (now - _last_run(source)) < COOLDOWN_SECONDS


def note_escalated(source: str, escalated: bool) -> None:
    """Remember that he has already been told, so he is told once, not hourly."""
    store.kv_set(_ESCALATED_KEY + source, "1" if escalated else "")


def already_escalated(source: str) -> bool:
    return bool(store.kv_get(_ESCALATED_KEY + source))


async def ladder(source: str,
                 rungs: list[tuple[str, Callable[[], Awaitable[bool]]]],
                 stale_polls: int,
                 threshold: int = DEFAULT_THRESHOLD,
                 now: float | None = None,
                 notify=None) -> dict:
    """Climb the rungs until one heals `source`. Speak only if none do.

    Returns what happened, which is also what makes it testable: whether it
    healed, which rung did it, and whether he had to be involved.
    """
    now = time.time() if now is None else now
    out = {"source": source, "healed": False, "healed_by": "",
           "told_him": False, "attempts": [], "skipped": ""}

    if not ENABLED:
        out["skipped"] = "disabled"
        return out
    if stale_polls < threshold:
        out["skipped"] = "not stale enough"
        return out
    if cooling_down(source, now):
        out["skipped"] = "cooling down"
        return out

    store.kv_set(_LAST_RUN_KEY + source, str(now))

    for name, attempt in rungs:
        try:
            healed = bool(await attempt())
        except Exception as exc:                                # noqa: BLE001
            # A rung that throws is a rung that failed. It must not abort the
            # ladder: the whole point is that a later, blunter repair still runs.
            healed = False
            store.record_outcome("recovery", "rung_error", subject=source,
                                 detail=f"{name}: {type(exc).__name__}: {exc}"[:200])
        out["attempts"].append({"rung": name, "healed": healed})
        store.record_outcome("recovery", "healed" if healed else "failed",
                             subject=source, detail=name)
        if healed:
            out["healed"], out["healed_by"] = True, name
            # He was never told there was a problem, so there is nothing to
            # retract — but if he WAS told last time, the flag has to clear or
            # the next outage stays silent.
            note_escalated(source, False)
            return out

    # Every rung failed. This is the one case that is his.
    tried = ", ".join(a["rung"] for a in out["attempts"]) or "nothing"
    if not already_escalated(source):
        out["told_him"] = True
        note_escalated(source, True)
        store.record_outcome("recovery", "escalated", subject=source, detail=tried)
        if notify is not None:
            await notify(
                f"⚠️ {source} is still broken after trying: {tried}. "
                f"That is everything I can do automatically — this one needs you.",
                "warn")
    return out


def health_line() -> str:
    """One line for the health report: is self-healing working, or just running?

    A recovery system nobody measures is indistinguishable from a recovery system
    that stopped working, which is the failure mode this whole module exists to
    end. So the report says how often it fired and how often it actually helped.
    """
    rows = [r for r in store.recent_outcomes(400) if r.get("kind") == "recovery"]
    if not rows:
        return "self-healing: never needed"
    healed = sum(1 for r in rows if r.get("outcome") == "healed")
    failed = sum(1 for r in rows if r.get("outcome") == "failed")
    escalated = sum(1 for r in rows if r.get("outcome") == "escalated")
    total = healed + failed
    rate = f"{healed}/{total}" if total else "0/0"
    return (f"self-healing: {rate} rungs succeeded, "
            f"{escalated} escalation(s) to you")
