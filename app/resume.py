"""Picking work back up where a brain ran out, instead of starting it again.

The failure this exists for: Copilot hits its monthly credits, or the Claude
subscription's five-hour window closes, twelve minutes into a real task. The
fallback already existed — hand the turn to another brain — but it handed over
the ORIGINAL message and nothing else. The second brain therefore began at zero:
re-read the same files, re-derived the same conclusion, and billed a second brain
for work the first one had already done. When no brain was left, the turn simply
errored and the request was gone.

What actually carries a task across a brain boundary is not the request. It is
what has already been established. So a dying turn leaves a CHECKPOINT — the
request, who was working, what they had produced, and when — and whoever picks it
up is told to continue from there rather than to start.

Two ways it gets picked up, because he asked for both:

  immediately   another brain is up, so the handoff happens inside the same turn
                and he sees one note saying who took over.
  later         nothing is up, or he would rather wait for quota to come back.
                The checkpoint persists and "resume" from any channel continues
                it — including from his phone, hours later, after a restart.

Checkpoints expire. A day-old "continue what you were doing" is worse than
nothing: the branch has moved, he has moved on, and resuming it would produce
confident work against a world that no longer exists.
"""

from __future__ import annotations

import json
import os
import time

from . import store

KEY = "resume_point"        # one per conversation: f"{KEY}:{conv_id}"

#: How much of the dead brain's output to carry. The tail is what matters — the
#: conclusion it had reached — and the whole thing would just re-pay for context
#: the new brain can rebuild more cheaply from the repo.
MAX_PARTIAL = 4000


def ttl_seconds() -> int:
    """How long a checkpoint stays resumable (default 24h). 0 = never expires."""
    try:
        return max(0, int(os.environ.get("ASTA_RESUME_TTL", "86400")))
    except ValueError:
        return 86400


def _key(cid: str) -> str:
    return f"{KEY}:{cid}"


def save(cid: str, request: str, brain: str, partial: str = "",
         channel: str = "web", why: str = "quota") -> dict:
    """Record where a turn stopped. Returns the checkpoint."""
    point = {
        "request": request or "",
        "brain": brain or "",
        "partial": (partial or "").strip()[-MAX_PARTIAL:],
        "channel": channel or "web",
        "why": why,
        "at": time.time(),
    }
    store.kv_set(_key(cid), json.dumps(point))
    return point


def get(cid: str) -> dict | None:
    """The open checkpoint for this conversation, or None if there is none or it
    has aged out."""
    raw = store.kv_get(_key(cid))
    if not raw:
        return None
    try:
        point = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(point, dict) or not point.get("request"):
        return None
    ttl = ttl_seconds()
    if ttl and time.time() - float(point.get("at") or 0) > ttl:
        clear(cid)
        return None
    return point


def clear(cid: str) -> None:
    store.kv_set(_key(cid), "")


def age_minutes(point: dict) -> int:
    return max(0, int((time.time() - float(point.get("at") or 0)) // 60))


def handoff_prompt(point: dict, taking_over: str = "") -> str:
    """The instruction that continues the work on a different brain.

    Written to be honest about provenance. The partial output came from a
    different model and was cut off mid-thought, so it is offered as evidence to
    verify rather than as findings to build on — a truncated conclusion presented
    as established fact is exactly how a handoff turns one brain's guess into two
    brains' certainty.
    """
    who = point.get("brain") or "another brain"
    got = point.get("partial", "").strip()
    lines = [
        f"Arun asked for this, and {who} was working on it when it ran out of "
        f"quota — you are continuing that work, not starting it over.",
        "",
        f"HIS ORIGINAL REQUEST:\n{point.get('request', '')}",
    ]
    if got:
        lines += [
            "",
            f"HOW FAR {who.upper()} GOT (its output, cut off mid-task — treat it as a "
            f"lead to verify, not as settled fact):",
            "———",
            got,
            "———",
        ]
    lines += [
        "",
        "Continue from there. Do not redo work that is already clearly done, and do "
        "not re-explain what is above — Arun has already read it. Pick up at the "
        "next unfinished step and carry the task to the end. If you draft anything "
        "to send outside this chat, stage it with prepare_to_send.",
    ]
    if taking_over:
        lines.append(f"You are {taking_over}.")
    return "\n".join(lines)


def _why(point: dict) -> str:
    """Why it stopped, in his words rather than an exception's.

    Copilot's is a monthly credit pool and Claude's a rolling five-hour window;
    which one it was decides whether he tops up or just waits, so the distinction
    is worth carrying rather than flattening to "ran out".
    """
    return point.get("why") or f"{point.get('brain', 'that brain')} ran out"


def note(point: dict, taking_over: str) -> str:
    """The one line he reads when a handoff happens mid-turn."""
    got = " (carrying over what it had already worked out)" if point.get("partial") else ""
    return f"⚡ {_why(point)} — {taking_over} is picking it up{got}."


def parked_note(point: dict) -> str:
    """The one line he reads when NOTHING is available to take over.

    It names the two real options rather than reporting a failure. The work is
    not lost, and that is the part he needs to know before deciding whether to go
    and add a key.
    """
    return (f"⚠️ {_why(point)} and nothing else is configured to take over — "
            f"but I've kept the place.\n\n"
            f"Say “resume” when quota is back, or “use <brain>” to switch and carry "
            f"on from here.")
