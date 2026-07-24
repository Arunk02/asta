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

_FALSEY = ("0", "false", "no", "off", "")


def enabled() -> bool:
    """On by default. Bounded + gated, so on-by-default is still safe."""
    return os.environ.get("ASTA_LOOP", "1").strip().lower() not in _FALSEY


def max_steps() -> int:
    """How many times a single user message may auto-continue before Asta pauses
    and hands control back. The ceiling is what makes on-by-default safe."""
    try:
        return max(0, int(os.environ.get("ASTA_LOOP_MAX_STEPS", "4")))
    except ValueError:
        return 4


# The intent a turn left behind, read once by the conductor then cleared.
_next: dict[str, dict] = {}
# Autonomous steps spent on the CURRENT user message; reset each time Arun speaks.
_steps: dict[str, int] = {}
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
    _steps.pop(cid, None)


def bump_steps(cid: str) -> int:
    _steps[cid] = _steps.get(cid, 0) + 1
    return _steps[cid]


def budget_left(cid: str) -> bool:
    return _steps.get(cid, 0) < max_steps()


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
    _awaiting.pop(cid, None)
