"""In-flight voice synthesis should not compete with call playback."""

import asyncio

import pytest

from app import meetings, voice


@pytest.mark.asyncio
async def test_playback_shares_synthesis_already_started_by_call_brain(monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def speak(text, voice=""):
        calls.append((text, voice))
        started.set()
        await finish.wait()
        return b"RIFFvoice"

    monkeypatch.setattr(voice, "speak", speak)
    text = "isolated in-flight call synthesis"
    first = asyncio.create_task(meetings.synth(text, "assistant"))
    await started.wait()
    second = asyncio.create_task(meetings.synth(text, "assistant"))
    await asyncio.sleep(0)
    assert calls == [(text, "assistant")]
    finish.set()
    assert await asyncio.gather(first, second) == [b"RIFFvoice", b"RIFFvoice"]
    assert await meetings.synth(text, "assistant") == b"RIFFvoice"
    assert calls == [(text, "assistant")]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_call_audio(monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def speak(text, voice=""):
        calls.append(text)
        started.set()
        await finish.wait()
        return b"RIFFvoice"

    monkeypatch.setattr(voice, "speak", speak)
    text = "isolated call synthesis after cancellation"
    first = asyncio.create_task(meetings.synth(text, "assistant"))
    await started.wait()
    second = asyncio.create_task(meetings.synth(text, "assistant"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    assert await second == b"RIFFvoice"
    assert calls == [text]


@pytest.mark.asyncio
async def test_failed_synthesis_retries_and_different_voices_do_not_share(monkeypatch):
    calls = []

    async def speak(text, voice=""):
        calls.append(voice)
        if len(calls) == 1:
            raise RuntimeError("voice service unavailable")
        return b"RIFFvoice"

    monkeypatch.setattr(voice, "speak", speak)
    text = "isolated call synthesis failure and voice choice"
    with pytest.raises(RuntimeError, match="voice service unavailable"):
        await meetings.synth(text, "assistant")
    assert await meetings.synth(text, "assistant") == b"RIFFvoice"
    assert await meetings.synth(text, "mine") == b"RIFFvoice"
    assert calls == ["assistant", "assistant", "mine"]
