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
#: Measured 2 Oct 12:10, warm, with the real brief and briefing: Sonnet's first
#: words in 1.3-2.2 s, Haiku's in 4.5-8.5 s (and 10-14 s live) — Haiku is the
#: smaller model, not the faster one through the CLI.
MODEL = "sonnet"

PERSONA = """You are Asta, Arun's assistant, talking with him OUT LOUD — like JARVIS:
quick, warm, precise, never chatty. He works on the Telikos booking platform at Maersk.

Reply with ONLY the words you say: one or two short sentences, plain words. The
FIRST sentence under eight words — it is spoken while you write the rest. No
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

If he just calls you or asks you to listen ("Asta", "listen to me", "are you there"),
say "I'm listening." and nothing more.
Never say work is under way, nearly done or "still on that" unless [Working on now: …]
lists it — with no such line, nothing is running.
[Working on now: …] lists work already running: ONLY if he asks for the very same
thing again, say "Still on that." — no [DO]. A different question is answered on its
own. If he adds to running work ("also check pre-prod"), that is new work: [DO].
Questions the briefing answers (his PRs, what is pending, what is scheduled) are
answered from it — never handed on as work.

Do not ask him questions back unless you truly cannot act without the answer.

When [Project knowledge for this question] comes with a line, answer from it in two or
three spoken sentences — the gist, plainly, as you would explain it to a colleague. Never
say you have nothing on it when it is there. If it truly does not cover the question, say
so in one sentence and hand on with [DO].

A line that starts "He said to you:" IS meant for you — never [QUIET]. If it is about
something you just said or did, answer from the conversation. If you cannot make
sense of it, say "Sorry, say that again?".

When a message starts with [RESULT], it is what the work you handed on found:
tell him the outcome in one or two sentences — the answer, not the process.
When a message starts with [UPDATE], it is news for him: say it in one sentence.

Never invent facts. Answer in the language of HIS sentence: English when he spoke
English; Hindi (in Devanagari) only when he spoke Hindi."""

_SENTENCE = re.compile(r"[.!?।]+[\"')\]]*\s+")

_TALKER: dict = {"mind": None, "starting": None, "failed_at": 0.0, "briefed": "", "briefed_at": 0.0,
                 "turns": 0, "fresh": True, "refreshing": False}
#: A talker's session grows with every line, and each reply re-reads all of it
#: through the CLI: the 232k-token chat session took 48 s to its first word
#: (Sep). After this many turns a fresh one is warmed in the background and
#: swapped in, carrying the last few minutes of the conversation.
REFRESH_TURNS = 30
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
    brief = await _brief(m)
    _TALKER.update(mind=m, briefed=brief, briefed_at=time.time() if brief else 0.0, turns=0, fresh=True)
    store.record_outcome("voice", "talker", detail="warm")
    return m


async def _brief(m) -> str:
    """The briefing goes in while nobody is waiting on an answer."""
    with contextlib.suppress(Exception):
        brief = briefing()
        if brief:
            await m._ask(f"[Briefing, {time.strftime('%a %H:%M')}]\n{brief}\n\nReply with just: ok", 60)
            return brief
    return ""


async def _refresh() -> None:
    """A fresh talker, warmed and briefed off to the side, then swapped in."""
    if _TALKER["refreshing"]:
        return
    _TALKER["refreshing"] = True
    try:
        from . import call_mind
        new = await call_mind.spawn(PERSONA, MODEL)
        brief = await _brief(new)
        old = _TALKER["mind"]
        _TALKER.update(mind=new, briefed=brief, briefed_at=time.time() if brief else 0.0, turns=0, fresh=True)
        store.record_outcome("voice", "talker", detail="refreshed")
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()
    except Exception as exc:                                    # noqa: BLE001
        store.record_outcome("voice", "talker_failed", detail=f"refresh: {exc}"[:200])
    finally:
        _TALKER["refreshing"] = False


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
    from . import voice_mode
    if _TALKER["fresh"]:
        # A new talker knows nothing of the last few minutes; tell it once.
        _TALKER["fresh"] = False
        before = voice_mode.recent()
        if before:
            head += "[Conversation so far]\n" + "\n".join(before[-8:]) + "\n\n"
    working = voice_mode.jobs_line()
    if working:
        head += working + "\n"
    if kind == "result":
        return head + text
    if kind == "update":
        return head + f"[UPDATE] {text}"
    if kind == "to_you":
        return head + f'He said to you: "{text}"'
    return head + f'He said: "{text}"'


async def sentences(text: str, kind: str = "said", timeout: float = 30, context: str = ""):
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
    _TALKER["turns"] += 1
    if _TALKER["turns"] >= REFRESH_TURNS:
        asyncio.ensure_future(_refresh())
    message = _message(text, kind)
    if context:
        # His documents and the repo summaries, for this question only — a few
        # hundred tokens, not the files.
        message = ("[Project knowledge for this question — answer from it, in your own words]\n"
                   f"{context}\n\n{message}")
    asyncio.ensure_future(_read_whole(m, message, timeout, out, end))
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


# --- the instant decision, on his Mac --------------------------------------------------
#
# Measured 2 Oct on a Mac at load 18: the warm Claude took 4-20 s to its first
# words; the local model (Gemma 4 E4B, shown four examples) decided in 0.6-0.8 s
# and got every quiet / listening / hand-off case right — but could not answer a
# status question (it thought for 20-30 s and said nothing). So it DECIDES, and
# only "needs an answer" goes to Claude.

LISTEN = "[LISTEN]"
ANSWER = "[ANSWER]"

ROUTER = """You decide what Arun's voice assistant does with one sentence he said out loud.
Reply with exactly one of:
[QUIET]            not meant for the assistant: talking to someone else, on a call,
                   a casual remark, filler, a fragment, background noise
I'm listening.     he only called the assistant ("Asta", "listen to me", "are you there")
On it. [DO]        work: check, look up, send, draft, review, investigate, schedule, fix
[ANSWER]           a question about his own work that needs an answer (PRs, pending,
                   tasks, who is waiting) — or anything you are unsure about
When the conversation so far is shown, use it: a follow-up to it — a question about
what the assistant is doing or found, a correction, more detail — is for the assistant.
Nothing else. Never explain."""

_SHOTS = [("yeah", "[QUIET]"), ("haha no I told him already", "[QUIET]"),
          ("Asta, are you there?", "I'm listening."),
          ("Hello", "I'm listening."),
          ("check the logs for booking ABC123 in prod", "On it. [DO]"),
          ("send Vinish a reminder about the PR", "On it. [DO]"),
          ("how many PRs do I have open?", "[ANSWER]"),
          ("what's pending today?", "[ANSWER]"),
          ("Conversation so far:\nArun: check the booking Rajendra shared\nAsta: On it.\n\n"
           'He said: "which booking did you check, the one from yesterday?"', "[ANSWER]"),
          ("Conversation so far:\nArun: Asta, are you there?\nAsta: I'm listening.\n\n"
           'He said: "yesterday Rajendra shared a booking in Teams, debug it"', "On it. [DO]")]

#: How long the local decision may take before Claude decides instead.
ROUTE_SECONDS = 4.0


async def route(text: str, context: list[str] | None = None) -> str | None:
    """QUIET, LISTEN, DO or ANSWER for this sentence — or None when the local
    model is not there (or too slow), and the Claude talker decides instead.

    `context` is the conversation so far: "which booking did you check?" is room
    talk on its own and plainly for Asta after "check the booking" (2 Oct)."""
    import httpx
    from . import memory
    model = await asyncio.to_thread(memory.local_llm_model)
    if not model or "embed" in model:
        return None
    msgs = [{"role": "system", "content": ROUTER}]
    for said, reply in _SHOTS:
        msgs += [{"role": "user", "content": said if said.startswith("Conversation") else f'He said: "{said}"'},
                 {"role": "assistant", "content": reply}]
    head = ("Conversation so far:\n" + "\n".join(context[-6:]) + "\n\n") if context else ""
    msgs.append({"role": "user", "content": f'{head}He said: "{text}"'})
    try:
        async with httpx.AsyncClient(timeout=ROUTE_SECONDS) as c:
            r = await c.post(f"{memory.local_llm_base()}/chat/completions", json={
                "model": model, "messages": msgs, "max_tokens": 24, "temperature": 0})
            out = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception:                                           # noqa: BLE001
        return None
    out = out.strip()
    if QUIET in out:
        return QUIET
    if DO in out:
        return DO
    if "listening" in out.lower():
        return LISTEN
    return ANSWER
