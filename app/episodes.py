"""A phone conversation is a series of sittings, not one endless thread.

His WhatsApp is a single conversation of 490 messages. Every turn re-read
between 107k and 300k tokens of it — two minutes for a one-line draft on 29 Sep —
and most of what it re-read was about things long finished.

The web UI never had this problem: it has one context per chat, and a new topic
is a new chat. A phone has no "new chat" button. So a sitting boundary stands in
for one: when he writes after a long gap, the brain session starts fresh, is
handed a short recap of where things stood, and what the last sitting was about
is written to memory, where a later "that PR from Tuesday" can find it.

The size cap (`main.retire_session_for_size`) still applies within a sitting.
This adds the cheaper, earlier boundary that a person's own rhythm already draws.
"""

from __future__ import annotations

import os
import time

from . import store

#: Channels that are one endless thread. The web UI has its own chats.
_PHONE = ("whatsapp", "telegram")


def applies_to(channel: str) -> bool:
    return (channel or "").strip().lower() in _PHONE


def gap_seconds() -> float:
    """How long a silence ends a sitting. ASTA_EPISODE_GAP_MINUTES, default 120."""
    try:
        return float(os.environ.get("ASTA_EPISODE_GAP_MINUTES", "120") or 120) * 60
    except ValueError:
        return 7200.0


def _key(cid: str) -> str:
    return f"episode_last:{cid}"


def touch(cid: str, now: float | None = None) -> None:
    """He has just written in this conversation."""
    store.kv_set(_key(cid), f"{time.time() if now is None else now:.0f}")


def gap_elapsed(cid: str, now: float | None = None) -> bool:
    """Has enough time passed since his last message that this is a new sitting?

    False for a conversation never seen before: there is no previous sitting to
    close, and rotating an empty session would only throw away the orientation.
    """
    try:
        last = float(store.kv_get(_key(cid)) or 0)
    except ValueError:
        return False
    if not last:
        return False
    now = time.time() if now is None else now
    return now - last > gap_seconds()
