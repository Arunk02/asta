"""Settle "delivery not confirmed" from the chat itself, never by sending again.

An automatic reply (#174) or a task's update to a named person (#179) that hit a
timeout, a crash or an unreadable confirmation is recorded as UNCERTAIN, and
nothing resends it — a second copy in a colleague's chat is worse than a late
one. Until now nothing ever looked again either, so "not confirmed" was the
final word and he had to open Teams to find out.

This reads the chat history Asta already stores (chat_watch keeps it current)
and settles each one:

  delivered      — the exact words are there, from his account, after the send
                   began. The receipt becomes `sent`; nothing to tell him.
  not delivered  — the stored history demonstrably covers the moments after the
                   send (`store.teams_history_covers`) and the words are not
                   there. He is told once; nothing is resent on its own.
  unknown        — history does not cover it yet. Left for the next sweep, and
                   after a day said once as unverifiable.

A `sending` receipt older than SENDING_STALE_SECONDS is a send a crash
interrupted, and is treated the same way.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import store

SWEEP_SECONDS = 300
SENDING_STALE_SECONDS = 600
#: Margin before the send began, for clock skew between Teams and this machine.
BEFORE = 120
#: How long after the send a covering read must reach before "absent" is said.
AFTER = 300
GIVE_UP_SECONDS = 24 * 3600

#: kv prefix -> how to read (recipient, exact text, send start) from a receipt.
SOURCES = {
    "answer_delivery:": lambda v: ((v.get("intent") or {}).get("to", ""),
                                   (v.get("intent") or {}).get("what", ""), v.get("at")),
    "task_inform_receipt:": lambda v: (v.get("to", ""), v.get("text", ""), v.get("at")),
}


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def verdict(to: str, text: str, at: float, now: float | None = None) -> str:
    """'delivered', 'absent' or 'unknown' — from stored history only."""
    from . import chat_watch
    if not to or not text or not at:
        return "unknown"
    want = _norm(text)
    for row in store.teams_messages(to, since=float(at) - BEFORE):
        sender = row.get("sender") or ""
        if (chat_watch.is_from_him(sender) or sender.lower() == "me") \
                and _norm(row.get("text", "")) == want:
            return "delivered"
    if store.teams_history_covers(to, float(at) - BEFORE, float(at) + AFTER):
        return "absent"
    return "unknown"


def _open(now: float) -> list[tuple[str, dict, tuple]]:
    out = []
    for prefix, read in SOURCES.items():
        for key, raw in store.kv_like(prefix + "%").items():
            try:
                value = json.loads(raw or "{}")
            except ValueError:
                continue
            state = value.get("state")
            started = float(value.get("at") or 0)
            if state == "uncertain" or (state == "sending" and started
                                        and now - started > SENDING_STALE_SECONDS):
                out.append((key, value, read(value)))
    return out


async def sweep(now: float | None = None) -> list[str]:
    """Settle what history can settle. Returns the lines he should read."""
    now = time.time() if now is None else now
    told = []
    for key, value, (to, text, at) in _open(now):
        result = verdict(to, text, at, now)
        if result == "delivered":
            store.kv_set(key, json.dumps({**value, "state": "sent", "reconciled_at": now,
                                          "reconciled": "found in the chat"}))
            store.record_outcome("delivery", "reconciled sent", subject=to[:80], detail=text[:160])
        elif result == "absent":
            store.kv_set(key, json.dumps({**value, "state": "not_delivered", "reconciled_at": now}))
            store.record_outcome("delivery", "reconciled absent", subject=to[:80], detail=text[:160])
            told.append(f"✉️ The message to {to} did not arrive — it is not in their chat. "
                        f"Nothing was resent:\n“{text[:300]}”")
        elif at and now - float(at) > GIVE_UP_SECONDS and not value.get("unverifiable"):
            store.kv_set(key, json.dumps({**value, "unverifiable": now}))
            told.append(f"✉️ Still can't tell whether the message to {to} arrived — their chat "
                        f"has not been read since. Check it before sending again:\n“{text[:300]}”")
    return told


async def loop() -> None:
    from . import notify, wake
    while True:
        await wake.sleep(SWEEP_SECONDS)
        try:
            for line in await sweep():
                await notify.notify(line, "answer", urgency="direct", considered=True)
        except Exception:                                    # noqa: BLE001
            continue        # history unreadable this time; the next sweep tries again
