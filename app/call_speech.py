"""Share prepared speech with playback instead of generating it twice."""

from __future__ import annotations

import asyncio
import hashlib

from . import voice

_VOICE_CACHE: dict[str, bytes] = {}
_VOICE_INFLIGHT: dict[str, asyncio.Task[bytes]] = {}


def _cache_key(text: str, voice_name: str) -> str:
    return f"{voice_name}:{hashlib.sha1(text.encode()).hexdigest()[:16]}"


async def synth(text: str, voice_name: str = "") -> bytes:
    """Return cached audio or share a synthesis still in progress."""
    chosen = voice_name or voice.in_voice()
    key = _cache_key(text, chosen)
    cached = _VOICE_CACHE.get(key)
    if cached:
        return cached
    task = _VOICE_INFLIGHT.get(key)
    if task is not None and task.done():
        _VOICE_INFLIGHT.pop(key, None)
        task = None
    if task is None:
        async def generate() -> bytes:
            audio = await voice.speak(text, voice=chosen)
            if audio:
                _VOICE_CACHE[key] = audio
            return audio

        task = asyncio.create_task(generate())
        _VOICE_INFLIGHT[key] = task

        def forget(done: asyncio.Task[bytes]) -> None:
            if _VOICE_INFLIGHT.get(key) is done:
                del _VOICE_INFLIGHT[key]

        task.add_done_callback(forget)
    return await asyncio.shield(task)
