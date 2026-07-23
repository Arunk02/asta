"""Is Arun actually at the laptop right now?

Drives notification etiquette: when he's sitting in front of the machine he does
not want ambient pings (CI chatter, general Teams/Outlook noise) — he'll ask.
The moment he steps away / the lid closes / the display sleeps, those get
delivered instead of dropped.

DIRECT things (a 1:1 message, an @mention, mail addressed to him) ignore this
entirely and always go out immediately — see notify.notify(urgency="direct").

Signal: HIDIdleTime from IOKit — nanoseconds since the last keyboard/mouse/
trackpad event. Cheap (one ioreg call), no permissions, no polling loop.
"""

from __future__ import annotations

import asyncio
import os
import re

# Idle longer than this and we treat him as away from the laptop.
AWAY_AFTER_SECONDS = int(os.environ.get("PRESENCE_AWAY_AFTER", "300"))

_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


async def idle_seconds() -> float | None:
    """Seconds since the last human input, or None if it can't be determined."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ioreg", "-c", "IOHIDSystem",  # no -d: HIDIdleTime lives below depth 1
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
    except Exception:
        return None
    m = _IDLE_RE.search(raw.decode(errors="replace"))
    return int(m.group(1)) / 1_000_000_000 if m else None


async def at_laptop() -> bool:
    """True when he's using the machine. Unknown → assume True (stay quiet).

    Assuming "present" on failure is the safe default: the cost of a missed
    ambient ping is that he asks; the cost of a wrong ping is interrupting him.
    Direct messages never take this path.
    """
    idle = await idle_seconds()
    if idle is None:
        return True
    return idle < AWAY_AFTER_SECONDS


async def state() -> dict:
    idle = await idle_seconds()
    return {
        "idle_seconds": round(idle, 1) if idle is not None else None,
        "at_laptop": (idle is None or idle < AWAY_AFTER_SECONDS),
        "away_after_seconds": AWAY_AFTER_SECONDS,
    }
