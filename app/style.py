"""How Arun writes — learned from what he actually sent, not described.

His words, 29 Sep: "follow my msg talk template, get some context and learn from
it … that is what brings [Asta to sound] like me". A rule like "short, plain,
polite" gives a reply that sounds like nobody. 3,523 of his own Teams messages
are already in the store, and they show the thing a rule cannot: he writes to
Vinish "AP u deploy in SIT as well as pp bro ?" and to Aayush "Hi Aayush - Good
evening, this one was request triggered from our end…".

So every draft in his name is given a handful of his REAL messages to the same
person — the register, length and words he uses with them — plus what he
changed when he edited earlier drafts. Retrieval, not training: it improves
with every message he sends, and costs a few hundred tokens.

The examples set the tone, never the content: the drafting brief says so.
"""

from __future__ import annotations

import re
from collections import Counter

from . import store

#: A quoted reply Teams renders inline: "Brindha Manokaran 03/09/2026 16:41 …"
_QUOTE = re.compile(r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}")
_REACTION = re.compile(r"\b\d+\s+[\w ]{1,30}\breactions?\.?\s*$", re.I)
_URL = re.compile(r"https?://\S+")
_FILE = re.compile(r"\.(pdf|png|jpe?g|xlsx?|docx?|pptx?|zip|txt|csv|md|json|ya?ml)\s*$", re.I)
#: Emoji and punctuation, for telling "now it is me 👍" and "now it is me" apart: they are one.
_NOISE = re.compile(r"[^\w\s]", re.U)

AT_MOST = 6
#: Fewer than this to the person, and his general 1:1 voice fills in.
ENOUGH = 3


def _usable(text: str) -> str:
    """The message as an example, or '' if it teaches nothing about how he writes."""
    t = " ".join((text or "").split())
    if _QUOTE.search(t) or _FILE.search(t):
        return ""
    t = _REACTION.sub("", t).strip()
    words = _URL.sub("", t).split()
    if len(words) < 3 or len(" ".join(words)) < 12 or len(t) > 220:
        return ""
    return t


def _key(text: str) -> str:
    return " ".join(_NOISE.sub(" ", text.lower()).split())


def _asta_said() -> set[str]:
    """Lines ASTA sent under his name — they are in his chats, and not his voice."""
    try:
        rows = store.recent_outcomes(2000)
    except Exception:                                          # noqa: BLE001
        return set()
    return {_key(r.get("detail") or "") for r in rows
            if (r.get("kind"), r.get("outcome")) in (("thread", "said"), ("steward", "asked back"),
                                                     ("thread", "answered"))}


def _his(chat: str | None, limit: int) -> list[str]:
    from . import chat_watch
    try:
        rows = store.teams_messages(chat=chat, limit=limit) if chat else \
            store.teams_messages(limit=limit)
    except Exception:                                          # noqa: BLE001
        return []
    rows = sorted(rows, key=lambda r: float(r.get("sent_at") or 0), reverse=True)
    not_his = _asta_said()
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not chat_watch.is_from_him(r.get("sender", "")):
            continue
        t = _usable(r.get("text") or "")
        k = _key(t)
        if t and k not in seen and k not in not_his:
            seen.add(k)
            out.append(t)
    return out


def examples(to: str, k: int = AT_MOST) -> list[str]:
    """His own recent messages in the conversation with `to`, newest first; topped
    up from his other chats when there are too few to go on."""
    got = _his(to, 400)[:k] if to else []
    if len(got) < ENOUGH:
        have = {_key(g) for g in got}
        for t in _his(None, 1500):
            if len(got) >= k:
                break
            if _key(t) not in have:
                got.append(t)
    return got[:k]


def lessons(limit: int = 3) -> list[str]:
    """What he changed when he edited drafts, most frequent first ("shorter")."""
    try:
        from . import ledger
        rows = ledger.recent(300, "send")
    except Exception:                                          # noqa: BLE001
        return []
    counts: Counter[str] = Counter()
    for r in rows:
        if r.get("verdict") != "amended":
            continue
        for part in re.split(r"[;,]\s*", r.get("edit") or ""):
            if part.strip():
                counts[part.strip()] += 1
    return [f"{what} ({n}x)" for what, n in counts.most_common(limit)]


def rider(to: str) -> str:
    """The block a drafting brief carries so the draft sounds like him."""
    ex = examples(to)
    if not ex:
        return ""
    lines = [f"How Arun actually writes to {to or 'colleagues'} — his own recent messages. "
             "Match their tone, length and wording (his casual words, his level of "
             "formality with this person). Never reuse their content, and never drop "
             "the courtesy:"]
    lines += [f"- {e}" for e in ex]
    learned = lessons()
    if learned:
        lines.append("When he edited earlier drafts, he: " + "; ".join(learned) + ".")
    return "\n".join(lines)
