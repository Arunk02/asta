"""Notifications: stored for the UI bell, and fanned out to WhatsApp + Telegram."""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx

from . import store, telegram


def bridge_url() -> str:
    return os.environ.get("WA_BRIDGE_URL", "http://127.0.0.1:8323")


async def wa_send(text: str) -> bool:
    """Push a message through the WhatsApp bridge; False if bridge is down/unpaired."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{bridge_url()}/send",
                json={"text": text},
                headers={"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")},
            )
            return r.status_code == 200
    except Exception:
        return False


async def wa_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{bridge_url()}/status")
            return r.json()
    except Exception:
        return {"up": False, "paired": False}


async def wa_create_group(name: str) -> dict | None:
    """Ask the bridge to create the dedicated assistant group chat; None if bridge down."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{bridge_url()}/create-group",
                json={"name": name},
                headers={"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")},
            )
            return r.json()
    except Exception:
        return None


async def wa_config(changes: dict) -> dict | None:
    """Push config (enabled / allowed_jid) to the bridge; None if bridge is down."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                f"{bridge_url()}/config",
                json=changes,
                headers={"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")},
            )
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


HELD_KEY = "held_ambient_notifications"
HELD_MAX = 40


def hold_max_minutes() -> int:
    """How long an ambient item may sit held before it is delivered anyway.

    Presence was the ONLY release condition, and `at_laptop()` is true whenever he
    is touching the machine — including the afternoons he shuts Teams and Outlook
    and works on something else entirely. Held items could therefore wait hours
    for a departure that never came. A hold is a courtesy, not a black hole: past
    this age it goes out regardless of where he is. 0 disables the age release.
    """
    try:
        return max(0, int(os.environ.get("ASTA_HOLD_MAX_MINUTES", "45")))
    except ValueError:
        return 45


def _held_items() -> list[dict]:
    """Held entries as {at, text}. Tolerates the old bare-string format."""
    try:
        raw = json.loads(store.kv_get(HELD_KEY) or "[]")
    except Exception:
        return []
    out = []
    for it in raw:
        if isinstance(it, dict) and it.get("text"):
            out.append({"at": float(it.get("at") or 0), "text": it["text"]})
        elif isinstance(it, str) and it:
            out.append({"at": 0.0, "text": it})   # legacy: age unknown → overdue
    return out


def _stale(items: list[dict], now: float | None = None) -> bool:
    """True when the oldest held item has waited longer than the courtesy window."""
    limit = hold_max_minutes()
    if limit <= 0 or not items:
        return False
    now = time.time() if now is None else now
    return any(now - it["at"] >= limit * 60 for it in items)


async def notify(text: str, level: str = "info", urgency: str = "direct") -> dict:
    """Record for the UI bell and fan out to WhatsApp + Telegram.

    urgency="direct"  — someone is actually addressing Arun (1:1 message, @mention,
        mail to him), or he asked for it. Always delivered immediately.
    urgency="ambient" — useful-but-not-addressed-to-him (CI results, general channel
        traffic). Delivered only when he's AWAY from the laptop; while he's sitting
        there it is held, because he'd rather ask than be pinged. Held items are
        released the moment he steps away, and are always in the UI bell meanwhile.

    Returns which channels actually took it. This used to return None, so a fire
    that reached NOBODY (both channels down) looked identical to one delivered —
    which is exactly how reminders "sent" for days while landing only in a bell
    no one was looking at.
    """
    store.add_notification(text, level)  # the bell always gets everything
    if urgency == "ambient":
        from . import presence
        if await presence.at_laptop():
            held = _held_items()
            held.append({"at": time.time(), "text": text})
            held = held[-HELD_MAX:]
            # Holding is a courtesy with an expiry. If something has now waited out
            # the window, release the whole batch rather than keeping it hostage to
            # a departure that may not come today.
            if _stale(held):
                store.kv_set(HELD_KEY, json.dumps(held))
                await flush_held(reason="waited long enough")
                return {"bell": True, "held": False, "whatsapp": True, "telegram": True}
            store.kv_set(HELD_KEY, json.dumps(held))
            return {"bell": True, "held": True, "whatsapp": False, "telegram": False}
    wa = await wa_send(text)
    tg = await telegram.send(text)
    if not (wa or tg):
        # Delivered to the bell and nowhere else. Record it so health can surface
        # a mute assistant instead of it being silently swallowed.
        store.kv_set("last_push_failure",
                     json.dumps({"at": time.time(), "text": text[:120]}))
    return {"bell": True, "held": False, "whatsapp": wa, "telegram": tg}


async def live_push_channels() -> list[str]:
    """Which phone channels are actually connected right now (a real probe, not
    just configured). Used to tell the truth at the moment a reminder is set,
    rather than promising delivery that cannot happen."""
    out: list[str] = []
    try:
        st = await wa_status()
        if st.get("up") and st.get("paired"):
            out.append("WhatsApp")
    except Exception:
        pass
    if telegram.enabled() and telegram.chat_id():
        out.append("Telegram")
    return out


async def flush_held(reason: str = "while you were at the laptop") -> None:
    """Deliver notifications that were held. Says WHY they are arriving now."""
    held = _held_items()
    if not held:
        return
    store.kv_set(HELD_KEY, "[]")
    texts = [it["text"] for it in held]
    head = f"🔕 Held ({len(texts)}) — {reason}:\n\n"
    body = "\n\n".join(texts[-10:])
    if len(texts) > 10:
        body += f"\n\n(+{len(texts) - 10} more in the app)"
    await wa_send(head + body)
    await telegram.send(head + body)


async def held_watch_loop() -> None:
    """Release held ambient notifications — on departure OR on age.

    Two release conditions, because presence alone strands things: he can sit at
    the laptop all afternoon with Teams and Outlook closed, and a departure-only
    rule would keep every held item until evening.
    """
    from . import presence
    was_present = True
    while True:
        await asyncio.sleep(60)
        try:
            present = await presence.at_laptop()
            if was_present and not present:
                await flush_held()
            elif _stale(_held_items()):
                await flush_held(reason="waited long enough")
            was_present = present
        except Exception:
            pass
