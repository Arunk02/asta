"""The voice talker — a fast front for voice mode; the work goes behind it.

Live on 2 Oct, voice mode sent every sentence to the work brain, which took 20
to 115 seconds, while the voice layer waited and filled the silence: "very
delay", "on it, on it every interval", and casual words and music turned into
actions. He asked for a Jarvis-level benchmark.

So two brains, the shape real voice assistants use:

  talker  a warm Claude (the call engine's — first words in about a second),
          no tools, briefed with his live work. It answers what it can, stays
          SILENT for what is not meant for it, and hands real work on.
  worker  the existing pipeline, unchanged, run in the background. Its answer
          comes back to the talker, which says the outcome once.

The talker's reply protocol, kept deliberately small:
  [QUIET]  not meant for Asta — say nothing
  [DO]     needs the worker — what is said before it is the one acknowledgement
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time

from . import store

QUIET = "[QUIET]"
DO = "[DO]"

#: The talker's model. Speed first: anything that needs depth is the worker's.
MODEL = "sonnet"

PERSONA = """You are Asta, Arun's assistant, talking with him OUT LOUD — like JARVIS:
quick, warm, precise, never chatty. He works on the Telikos booking platform at Maersk.

Reply with ONLY the words you say: one or two short sentences, plain words. No
markdown, lists, links, code, emoji.

Decide every time:
1. Not meant for you — he is talking to someone else, on the phone, thinking aloud,
   a casual remark ("yeah", "hmm", "okay cool"), a fragment, background noise, or
   anything you are not sure was said to you: reply exactly [QUIET] and nothing else.
2. You can answer from the briefing (his PRs, what is pending, scheduled follow-ups,
   who is waiting on him, running tasks): answer in one or two sentences. Name PRs
   by service and number ("booking PR 1429").
3. Anything that needs real work — checking logs or a booking, reading a PR or
   chat, sending or drafting a message, reviewing, investigating, scheduling,
   fixing, anything the briefing does not answer: say exactly ONE of these, then
   [DO] — "On it.", "Checking now.", "Let me look." (they are pre-recorded, so
   they play instantly). Never say it is done; never guess the answer.

When a message starts with [RESULT], it is what the work you handed on found:
tell him the outcome in one or two sentences — the answer, not the process.
When a message starts with [UPDATE], it is news for him: say it in one sentence.

Never invent facts. Speak his language — English, Hindi, or the mix he uses; write
Hindi in Devanagari."""

_SENTENCE = re.compile(r"[.!?।]+[\"')\]]*\s+")

_TALKER: dict = {"mind": None, "starting": None, "failed_at": 0.0, "briefed": "", "briefed_at": 0.0}
#: The briefing travels with a message at most this often: 4 KB on every line
#: made the first answer 7 s (measured 2 Oct).
BRIEF_EVERY_SECONDS = 300.0
#: After a failed start (quota, no CLI), the old path is used for this long.
RETRY_SECONDS = 300.0


def briefing() -> str:
    """What he would expect his assistant to know without looking anything up."""
    parts: list[str] = []
    with contextlib.suppress(Exception):
        from . import copilot_cli
        work = copilot_cli.open_work()
        if work:
            parts.append(work)
    with contextlib.suppress(Exception):
        from . import activity
        parts.append("Background work:\n" + activity.summary()[:1200])
    with contextlib.suppress(Exception):
        from . import chat_watch
        waiting = chat_watch.open_with_him()
        if waiting:
            parts.append("Open with him on Teams:\n" + "\n".join(waiting[:8]))
    return "\n\n".join(p for p in parts if p.strip())[:4000]


async def mind():
    """The warm talker, started on first use; None when it cannot be had."""
    if _TALKER["mind"] is not None and _TALKER["mind"].proc.returncode is None:
        return _TALKER["mind"]
    if time.time() - _TALKER["failed_at"] < RETRY_SECONDS:
        return None
    if _TALKER["starting"] is None:
        _TALKER["starting"] = asyncio.ensure_future(_start())
    try:
        return await _TALKER["starting"]
    finally:
        _TALKER["starting"] = None


async def _start():
    from . import call_mind
    try:
        m = await call_mind.spawn(PERSONA, MODEL)
    except Exception as exc:                                    # noqa: BLE001
        _TALKER["failed_at"] = time.time()
        store.record_outcome("voice", "talker_failed", detail=str(exc)[:200])
        return None
    _TALKER.update(mind=m, briefed="", briefed_at=0.0)
    # The briefing goes in now, while nobody is waiting on an answer.
    with contextlib.suppress(Exception):
        brief = briefing()
        if brief:
            await m._ask(f"[Briefing, {time.strftime('%a %H:%M')}]\n{brief}\n\nReply with just: ok", 60)
            _TALKER.update(briefed=brief, briefed_at=time.time())
    store.record_outcome("voice", "talker", detail="warm")
    return m


async def warm() -> None:
    with contextlib.suppress(Exception):
        await mind()


async def close() -> None:
    m = _TALKER["mind"]
    _TALKER.update(mind=None, briefed="")
    if m is not None:
        with contextlib.suppress(Exception):
            await m.close()


def _message(text: str, kind: str = "said") -> str:
    """What the talker is sent: the briefing when it is due again, then the line."""
    head = ""
    if time.time() - _TALKER["briefed_at"] > BRIEF_EVERY_SECONDS:
        brief = briefing()
        _TALKER["briefed_at"] = time.time()
        if brief and brief != _TALKER["briefed"]:
            _TALKER["briefed"] = brief
            head = f"[Briefing, {time.strftime('%a %H:%M')}]\n{brief}\n\n"
    if kind == "result":
        return head + text
    if kind == "update":
        return head + f"[UPDATE] {text}"
    return head + f'He said: "{text}"'


async def sentences(text: str, kind: str = "said", timeout: float = 30):
    """The talker's reply, a sentence at a time, as fast as it is written.

    Yields QUIET alone when it chose silence, DO last when it handed work on.
    Raises when there is no talker — the caller falls back.

    The reply is read to its end by a task of its own, whatever the caller does
    with it. Stopping early (on [QUIET], or when he talks over it) once left the
    rest in the pipe, and the NEXT question got the old answer's tail (2 Oct)."""
    m = await mind()
    if m is None:
        raise RuntimeError("no talker")
    out: asyncio.Queue = asyncio.Queue()
    end = object()
    asyncio.ensure_future(_read_whole(m, _message(text, kind), timeout, out, end))
    while True:
        item = await out.get()
        if item is end:
            return
        if isinstance(item, Exception):
            raise item
        yield item
        if item == QUIET:
            return


async def _read_whole(m, message: str, timeout: float, out: asyncio.Queue, end) -> None:
    from . import call_mind
    buffer = ""
    whole = ""
    quiet = False
    try:
        async for piece in m._stream(message, timeout):
            if piece is call_mind._COMPLETE:
                continue
            whole += piece
            if quiet:
                continue
            if QUIET in whole:
                quiet = True
                await out.put(QUIET)
                continue
            buffer += piece
            while (match := _SENTENCE.search(buffer)):
                said = buffer[:match.end()].replace(DO, "").strip()
                buffer = buffer[match.end():]
                if said:
                    await out.put(said)
        if not quiet:
            rest = buffer.replace(DO, "").strip()
            if rest:
                await out.put(rest)
            if DO in whole:
                await out.put(DO)
    except Exception as exc:                                    # noqa: BLE001
        # A talker that died mid-answer is replaced next time, not reused.
        await close()
        await out.put(exc)
    finally:
        await out.put(end)
