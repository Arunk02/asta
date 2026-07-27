"""The autonomous conductor loop — let Asta keep working instead of idling.

The turn engine (`main._conducted_turn`) is reactive: a turn ends the moment the
model stops typing, and nothing happens until Arun sends another message. That
means Asta stops even when the task is half-done and it already knows the next
step. He asked for the opposite — drive the work forward on its own, and stop to
ask only when there's a real decision, above all before anything leaves the chat.

This module is the small, pure state + policy the loop runs on. A turn can leave
behind exactly one of two signals (set by the `continue_working` / `prepare_to_send`
tools while the turn runs):

- **continue** — the model isn't done and named the next step. The loop runs that
  step ITSELF, without waiting for Arun, bounded by `max_steps()` so a confused
  model can't spin forever.
- **send** — the model drafted something outward (a Teams reply, an email, a Jira
  comment, a PR body). It is STAGED, never sent: the loop shows Arun the draft and
  asks "can I send this?" — the one hard gate, honouring the sending-safety rule.

State is in-memory and per live conversation on purpose: a loop that died with the
process must not silently resume autonomous work — or fire a staged send — after a
restart. That would be the least expected and least safe behaviour.
"""

from __future__ import annotations

import os
import time

_FALSEY = ("0", "false", "no", "off", "")


def enabled() -> bool:
    """On by default. Bounded + gated, so on-by-default is still safe."""
    return os.environ.get("ASTA_LOOP", "1").strip().lower() not in _FALSEY


def max_steps() -> int:
    """How many times a single user message may auto-continue before Asta pauses
    and hands control back. The ceiling is what makes on-by-default safe.

    Dropped 4 -> 2: four auto-steps of a CLI brain is twenty-plus minutes of
    silence, which is not "working autonomously", it is being unusable.
    """
    try:
        return max(0, int(os.environ.get("ASTA_LOOP_MAX_STEPS", "2")))
    except ValueError:
        return 2


def deadline_seconds() -> int:
    """Wall-clock ceiling for everything one user message may trigger.

    A step COUNT cannot bound latency — each step is a whole CLI turn of unknown
    length, so "at most 4 steps" silently meant "at most forty minutes". Time is
    what he actually experiences, so time is what gets budgeted. 0 disables.
    """
    try:
        return max(0, int(os.environ.get("ASTA_LOOP_DEADLINE", "600")))
    except ValueError:
        return 600


# The intent a turn left behind, read once by the conductor then cleared.
_next: dict[str, dict] = {}
# Autonomous steps spent on the CURRENT user message; reset each time Arun speaks.
_steps: dict[str, int] = {}
# When the CURRENT user message started, for the wall-clock deadline.
_started: dict[str, float] = {}
# A drafted outbound message staged for Arun's yes/no.
_awaiting: dict[str, dict] = {}


def set_continue(cid: str, next_step: str) -> None:
    if cid:
        _next[cid] = {"kind": "continue", "next_step": (next_step or "").strip()}


def set_pending_send(cid: str, what: str, to: str = "", channel: str = "chat") -> None:
    if cid:
        _next[cid] = {
            "kind": "send",
            "what": (what or "").strip(),
            "to": (to or "").strip(),
            "channel": (channel or "chat").strip() or "chat",
        }


def take(cid: str) -> dict | None:
    """The intent a just-finished turn left — read once, then cleared."""
    return _next.pop(cid, None)


def reset_steps(cid: str) -> None:
    """A new user message resets BOTH budgets — the step count and the clock."""
    _steps.pop(cid, None)
    if cid:
        _started[cid] = time.monotonic()


def bump_steps(cid: str) -> int:
    _steps[cid] = _steps.get(cid, 0) + 1
    return _steps[cid]


def elapsed(cid: str) -> float:
    """Seconds since Arun's message kicked this off (0 if never started)."""
    start = _started.get(cid)
    return 0.0 if start is None else time.monotonic() - start


def time_left(cid: str) -> bool:
    """False once this message has eaten its wall-clock budget."""
    limit = deadline_seconds()
    return True if limit <= 0 else elapsed(cid) < limit


def budget_left(cid: str) -> bool:
    """Auto-continue only while BOTH budgets hold: steps taken and time spent.

    Time is the one that actually protects him — steps say nothing about how long
    each one runs.
    """
    return _steps.get(cid, 0) < max_steps() and time_left(cid)


def stage(cid: str, intent: dict) -> None:
    """Hold a drafted send until Arun answers."""
    if cid:
        _awaiting[cid] = intent


def awaiting(cid: str) -> dict | None:
    return _awaiting.get(cid)


def clear_awaiting(cid: str) -> dict | None:
    return _awaiting.pop(cid, None)


def clear(cid: str) -> None:
    """Forget everything for a conversation — on error, on a fresh start."""
    _next.pop(cid, None)
    _steps.pop(cid, None)
    _started.pop(cid, None)
    _awaiting.pop(cid, None)
