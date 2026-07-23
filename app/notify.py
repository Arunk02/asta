"""Notifications: stored for the UI bell, and fanned out to WhatsApp + Telegram."""

from __future__ import annotations

import asyncio
import json
import os

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


async def notify(text: str, level: str = "info", urgency: str = "direct") -> None:
    """Record for the UI bell and fan out to WhatsApp + Telegram.

    urgency="direct"  — someone is actually addressing Arun (1:1 message, @mention,
        mail to him), or he asked for it. Always delivered immediately.
    urgency="ambient" — useful-but-not-addressed-to-him (CI results, general channel
        traffic). Delivered only when he's AWAY from the laptop; while he's sitting
        there it is held, because he'd rather ask than be pinged. Held items are
        released the moment he steps away, and are always in the UI bell meanwhile.
    """
    store.add_notification(text, level)  # the bell always gets everything
    if urgency == "ambient":
        from . import presence
        if await presence.at_laptop():
            held = json.loads(store.kv_get(HELD_KEY) or "[]")
            held.append(text)
            store.kv_set(HELD_KEY, json.dumps(held[-HELD_MAX:]))
            return
    await wa_send(text)
    await telegram.send(text)


async def flush_held() -> None:
    """Deliver notifications that were held while he was at the laptop."""
    held = json.loads(store.kv_get(HELD_KEY) or "[]")
    if not held:
        return
    store.kv_set(HELD_KEY, "[]")
    head = f"🔕 While you were at the laptop ({len(held)} held):\n\n"
    body = "\n\n".join(held[-10:])
    if len(held) > 10:
        body += f"\n\n(+{len(held) - 10} more in the app)"
    await wa_send(head + body)
    await telegram.send(head + body)


async def held_watch_loop() -> None:
    """Release held ambient notifications once Arun steps away from the laptop."""
    from . import presence
    was_present = True
    while True:
        await asyncio.sleep(60)
        try:
            present = await presence.at_laptop()
            if was_present and not present:
                await flush_held()
            was_present = present
        except Exception:
            pass
