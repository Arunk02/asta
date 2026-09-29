"""Who he means when he says "her".

29 Sep, 16:51: Asta pushed "Navya R: need ur help". At 16:52 he replied "ask her
what it is" and was asked who "her" was.

Nothing was forgotten. Pushes go to the notifications table and the brain reads
the conversation table, so the person Asta had just named was never in the
thread it answered from. On his phone it is one chat; in the backend it was two
places that never met.

This is the bridge: a short, recency-ordered list of the people Asta has just
told him about, handed to every chat turn. Short on purpose — it resolves a
pronoun, it does not carry their conversations. Those live in their own threads.
"""

from __future__ import annotations

import json
import time

from . import store

_KEY = "referents"

#: Enough to cover a busy morning's pushes, few enough to stay a few lines.
KEEP = 8

#: "Her" means someone from today, not someone from last week.
MAX_AGE = 36 * 3600


def _load() -> list[dict]:
    try:
        rows = json.loads(store.kv_get(_KEY) or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("who")]


def note(who: str, gist: str, source: str = "", now: float | None = None) -> None:
    """He has just been told about this person. Newest first, never twice."""
    who = (who or "").strip()
    if not who:
        return
    now = time.time() if now is None else now
    rows = [r for r in _load() if r["who"].lower() != who.lower()]
    rows.insert(0, {"who": who, "gist": " ".join((gist or "").split())[:120],
                    "source": source, "at": now})
    store.kv_set(_KEY, json.dumps(rows[:KEEP]))


def recent(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [r for r in _load() if now - float(r.get("at") or 0) <= MAX_AGE]


def block(now: float | None = None) -> str:
    """One line per person, for the top of a chat turn. '' when there is nobody."""
    rows = recent(now)
    if not rows:
        return ""
    lines = []
    for r in rows:
        when = time.strftime("%H:%M", time.localtime(float(r["at"])))
        where = f" ({r['source']}, {when})" if r.get("source") else f" ({when})"
        lines.append(f"- {r['who']}{where}: {r['gist']}")
    return ("People you just told him about, newest first — \"her\", \"him\" or "
            "\"them\" most likely means the first one:\n" + "\n".join(lines))
