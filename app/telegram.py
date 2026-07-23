"""Telegram bot channel (official Bot API — no ban risk, works from anywhere).

Setup (one-time, ~2 minutes):
  1. In Telegram, talk to @BotFather → /newbot → pick a name (e.g. "Asta") and a
     username (e.g. asta_arun_bot). BotFather gives you a token.
  2. Put it in .env:  TELEGRAM_BOT_TOKEN=123456:ABC-...
  3. Restart Asta, open your bot in Telegram and send:  /start <ASTA_TOKEN>
     (the token from .env — this binds the bot to YOUR chat and nobody else's).

After that: chat with Asta from Telegram like WhatsApp, and every notification
(Jira, missions, mentions, drift) is pushed here too.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from . import store

POLL_TIMEOUT = 50  # long-poll seconds per getUpdates call


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{_token()}/{method}"


def enabled() -> bool:
    return bool(_token())


def chat_id() -> str | None:
    """The single chat this bot is bound to (env overrides the stored binding)."""
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip() or store.kv_get("telegram_chat_id")


def status() -> dict:
    return {
        "enabled": enabled(),
        "bound": bool(chat_id()) if enabled() else False,
        "hint": (
            "set TELEGRAM_BOT_TOKEN in .env (see app/telegram.py header)" if not enabled()
            else ("open your bot in Telegram and send: /start <ASTA_TOKEN>" if not chat_id() else "ok")
        ),
    }


async def send(text: str) -> bool:
    """Push a message to the bound chat; False if unconfigured/unbound/down."""
    cid = chat_id()
    if not enabled() or not cid:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_api("sendMessage"), json={"chat_id": cid, "text": text[:4000]})
            return r.status_code == 200
    except Exception:
        return False


def _try_bind(cid: str, text: str) -> str | None:
    """Handle '/start <token>' — bind this chat if the token matches. Returns reply."""
    supplied = text.split(maxsplit=1)[1].strip() if " " in text else ""
    expected = os.environ.get("ASTA_TOKEN", "")
    if expected and supplied != expected:
        return "Send /start followed by your ASTA_TOKEN to connect this chat."
    store.kv_set("telegram_chat_id", cid)
    name = os.environ.get("ASSISTANT_NAME", "Asta")
    return f"Connected. This chat is now your {name} channel — talk to me, and notifications land here too."


async def poll_loop(handle_turn) -> None:
    """Long-poll getUpdates forever; route bound-chat messages through handle_turn(text) -> reply."""
    offset = int(store.kv_get("telegram_offset") or 0)
    while True:
        try:
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as c:
                r = await c.get(_api("getUpdates"),
                                params={"timeout": POLL_TIMEOUT, "offset": offset + 1,
                                        "allowed_updates": '["message"]'})
                updates = r.json().get("result", []) if r.status_code == 200 else []
            for u in updates:
                offset = max(offset, u["update_id"])
                store.kv_set("telegram_offset", str(offset))
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                cid = str((msg.get("chat") or {}).get("id", ""))
                if not text or not cid:
                    continue
                if text.startswith("/start"):
                    reply = _try_bind(cid, text)
                    async with httpx.AsyncClient(timeout=15) as c:
                        await c.post(_api("sendMessage"), json={"chat_id": cid, "text": reply})
                    continue
                if cid != chat_id():
                    continue  # ignore strangers entirely
                reply = await handle_turn(text)
                if reply:
                    await send(reply)
        except Exception:
            await asyncio.sleep(10)
