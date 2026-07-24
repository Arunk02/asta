"""Brain-router — answer a trivially-cheap turn on the free local model instead of
spawning a paid CLI to say "you're welcome".

Deliberately conservative: it diverts ONLY pure pleasantries (greetings, thanks,
acknowledgements) — turns where every brain gives the same throwaway answer — so a
real request is never downgraded to the local model. Everything with actual content
keeps the brain Arun picked. The bigger move (routing short factual Q&A to local too)
is the natural extension, left off until the trivial slice is proven, because
mis-routing a real question is the expensive mistake.

Each diverted turn skips a Copilot/Claude CLI spawn (~24k input tokens), so on a day
of "hi / thanks / great" this is real money for zero quality cost.
"""

from __future__ import annotations

import asyncio
import os
import re

# Turns any model answers identically. Kept tiny on purpose — when in doubt it is
# NOT trivial, and the real brain handles it.
_TRIVIAL = re.compile(
    r"""^\s*(
        hi|hey|hello|yo|hiya|sup|
        good\s*(morning|afternoon|evening|night)|gm|gn|
        thank\s*you|thanks|thanx|thx|ty|cheers|much\s*appreciated|
        nice|great|cool|awesome|perfect|brilliant|lovely|excellent|
        ok|okay|k|kk|got\s*it|makes\s*sense|sounds?\s*good|will\s*do|
        bye|goodbye|see\s*ya|see\s*you|later|ttyl|good\s*night
    )[\s!.,]*$""",
    re.IGNORECASE | re.VERBOSE,
)


def enabled() -> bool:
    return os.environ.get("ASTA_ROUTER", "1").strip().lower() in ("1", "true", "yes", "on")


def is_trivial(text: str) -> bool:
    """True only for a short, self-contained pleasantry — never for real content."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 40 and _TRIVIAL.match(t) is not None


def _canned(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in ("thank", "thx", "ty", "cheers", "appreciated")):
        return "Anytime! 👍"
    if any(w in t for w in ("bye", "later", "see y", "good night", "gn", "ttyl")):
        return "Catch you later! 👋"
    if any(w in t for w in ("ok", "cool", "great", "nice", "perfect", "got it", "sounds good", "will do")):
        return "👍"
    return "Hey! What can I do for you?"


async def reply(user_text: str) -> str:
    """A one-line answer for a trivial turn — the local model if it's up, else a
    canned line. Never spawns a paid brain, never calls a tool."""
    from . import memory
    prompt = ("You are Arun's assistant. Reply to this in ONE short, warm sentence. "
              "Do not ask questions or use tools.\n\n" + (user_text or "").strip())
    out = await asyncio.to_thread(memory.local_llm_complete, prompt, 60)
    return (out or "").strip() or _canned(user_text)
