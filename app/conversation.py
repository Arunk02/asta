"""Holding a two-way conversation on a live call.

Split from `call_brain` the moment it was written, because that module has one
property worth protecting: it takes a line of text and returns a judgement, and
touches no microphone, no browser and no call. That is what makes it testable and
evaluable without a call existing — and `test_judgement_does_not_depend_on_call_
machinery` fails the instant something reaches back into the machinery.

This is the machinery. It drives `meetings` (ring, captions, speak, hang up) and
asks `call_brain` what to say. Judgement on one side, mechanism on the other.

Every piece of the loop below was proven live on 27 August in a real call, and
then left in a scratch script — so Asta could ring a person, and could answer a
question, and had no capability that did both. Asked to "call Alex and discuss
the PR comments" it answered "I can't hold a live conversation with Alex
myself", which was true only because this file did not exist.

It is deliberately NOT a script of prepared lines. A script is what produced the
failure Alex described himself — "he keep on asking questions, nothing was
spoken" — because the far side does not follow a script. This reads what he
actually said, answers THAT, and stops when he stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from . import meetings
from .call_brain import answer_from_knowledge, spoken_form


#: How long a conversation may run before Asta winds it up. A call it forgets to
#: end holds his microphone and blocks the next one — `_CALL` stays set and every
#: later call is refused as "already in a call" until the server restarts.
CONVERSE_SECONDS = float(os.environ.get("ASTA_CONVERSE_SECONDS", "240"))

#: Silence, in seconds, that ends a turn and hands the floor back.
HEAR_SECONDS = float(os.environ.get("ASTA_HEAR_SECONDS", "14"))

#: Turns before Asta closes, whatever the clock says. A colleague who has answered
#: this many times has given his answer.
MAX_TURNS = int(os.environ.get("ASTA_CONVERSE_TURNS", "8"))


async def _hear(page, lines: list[dict], seconds: float) -> str:
    """What the other person said next, or "" if they said nothing."""
    before = len(lines)
    end = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < end:
        with contextlib.suppress(Exception):
            await meetings.poll_captions(page, lines)
        if len(lines) > before:
            await asyncio.sleep(1.5)          # let the sentence finish forming
            with contextlib.suppress(Exception):
                await meetings.poll_captions(page, lines)
            break
        await asyncio.sleep(1.0)
    fresh = [ln.get("text", "") for ln in lines[before:]
             if ln.get("text") and not meetings.speaker_is_arun(ln.get("speaker", ""))]
    return " ".join(fresh).strip()[:600]


async def _hear_rtc(lines: list[dict], who: str, keep: bool = False) -> str:
    """Their next turn, from their own audio on the call, transcribed on this Mac.

    `lines` gets both sides, so the transcript he reads afterwards is the call.
    A turn that was clearly speech but came back unreadable is kept as such,
    so Asta asks them to repeat rather than hanging up on a person mid-answer.
    """
    from . import call_rtc
    got = await call_rtc.hear_turn(meetings._CALL.get("ctx"), wait=HEAR_SECONDS,
                                   log=meetings._CALL.get("log"), keep=keep)
    text = (got.get("text") or "").strip()
    if got.get("spoke") and not text:
        text = _UNHEARD
    if text:
        lines.append({"speaker": who, "text": text})
    return text


#: What a turn is recorded as when they spoke and nothing could be made of it.
_UNHEARD = "(said something I could not make out)"

#: Said when an answer is not ready yet. A person on a call hears a pause of
#: more than a few seconds as the line going dead — but the same filler before
#: every reply sounds like a machine, so they take turns.
_MOMENTS = ("Mm, okay.", "Right, one second.", "Okay, let me think.")
_MOMENT = _MOMENTS[0]
MOMENT_AFTER = float(os.environ.get("ASTA_CALL_MOMENT_AFTER", "4"))
_said_moments = {"n": 0}


def _next_moment() -> str:
    line = _MOMENTS[_said_moments["n"] % len(_MOMENTS)]
    _said_moments["n"] += 1
    return line
ANSWER_TIMEOUT = 30


async def _prepare(lines: list[str]) -> None:
    """Synthesise ahead of time; `meetings.synth` caches by text."""
    from . import voice
    for line in lines:
        with contextlib.suppress(Exception):
            await meetings.synth(voice.strip_voice_instruction(line))


async def _answer_without_dead_air(thinking: "asyncio.Task") -> str:
    """The reply — with "one moment" said first if it is slow, so the line
    never goes quiet while a brain works."""
    try:
        return await asyncio.wait_for(asyncio.shield(thinking), timeout=MOMENT_AFTER)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            await meetings.say_in_call(_next_moment())
    except Exception:                                            # noqa: BLE001
        return _SORRY
    try:
        return await asyncio.wait_for(thinking, timeout=ANSWER_TIMEOUT) or _SORRY
    except Exception:                                            # noqa: BLE001
        return _SORRY


_SORRY = "Sorry, I could not work that out just now — I'll check with Arun and come back to you."
_DROPPING_OFF = "Sorry, I have to drop off now — Arun will follow up with you. Thanks!"

#: How long the call brain may take to prime before the dial goes ahead anyway.
BRAIN_READY_SECONDS = 8


async def _mind_if_ready(task: "asyncio.Task", wait: float = 5):
    """The call's warm brain, or None if it failed to start — then the slower
    knowledge path answers instead, so a broken brain never means silence."""
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
    except Exception:                                            # noqa: BLE001
        return None


_STILL_THERE = "Are you still there?"


async def _say(text: str, said: list[str], lines: list[dict], rtc: bool) -> None:
    """Say one line and keep the record of it."""
    if not text:
        return
    with contextlib.suppress(Exception):
        await meetings.say_in_call(text)
    said.append(text)
    if rtc:
        lines.append({"speaker": "Asta", "text": text})


async def _speak_reply(mind, theirs: str, said: list[str], lines: list[dict], rtc: bool,
                       elapsed: float = 0, limit: float = 0) -> tuple[bool, bool]:
    """Say the brain's reply as it is written: (ended, interrupted).

    Each sentence's audio is made the moment the sentence exists, so the next
    one is ready when the last finishes. If nothing is ready in a few seconds a
    short filler covers it; if they start talking, Asta stops and listens.
    """
    from . import call_mind, voice
    queue: asyncio.Queue = asyncio.Queue()

    async def produce() -> None:
        try:
            async for sentence in mind.sentences(theirs, elapsed=elapsed, limit=limit):
                text = spoken_form(sentence.replace(call_mind.END, "").strip())
                if text:
                    asyncio.get_event_loop().create_task(
                        meetings.synth(voice.strip_voice_instruction(text)))
                await queue.put((text, call_mind.END in sentence))
        except Exception as exc:                                 # noqa: BLE001
            # Out of its window mid-call: say so like a person and wrap up,
            # never read the brain's error out loud.
            if isinstance(exc, call_mind.Unavailable):
                await queue.put((_DROPPING_OFF, True))
            else:
                await queue.put((_SORRY, False))
        finally:
            await queue.put(None)

    asyncio.get_event_loop().create_task(produce())
    ended = interrupted = spoke = covered = False
    while True:
        try:
            item = await asyncio.wait_for(queue.get(),
                                          timeout=MOMENT_AFTER if not (spoke or covered) else ANSWER_TIMEOUT)
        except asyncio.TimeoutError:
            if spoke or covered:
                break
            covered = True
            await _say(_next_moment(), said, lines, rtc)
            continue
        if item is None:
            break
        text, last = item
        ended = ended or last
        if text:
            await _say(text, said, lines, rtc)
            spoke = True
            if meetings._CALL.get("interrupted"):
                interrupted = True
                ended = False
                break
    return ended, interrupted


async def _compose_opener(mind_task: "asyncio.Task", fallback: str) -> str:
    """The first line, made before they can pick up.

    Order is the point: the plain greeting first, then the reactions every reply
    opens with, then the fillers — and only then a greeting written by the call's
    own brain, which is used if it is ready by the time they answer.
    """
    from . import call_mind
    await _prepare([fallback, *call_mind.REACTIONS, *_MOMENTS])
    mind = await _mind_if_ready(mind_task, wait=30)
    if mind is None:
        return fallback
    with contextlib.suppress(Exception):
        line = spoken_form(await mind.opener())
        if line:
            await _prepare([line])
            return line
    return fallback


async def _notes_and_close(task: "asyncio.Task", heard_any: bool) -> str:
    """Ask the call's brain — which heard all of it — for notes, then stop it."""
    notes = ""
    if heard_any:
        mind = await _mind_if_ready(task, wait=1)
        if mind is not None:
            with contextlib.suppress(Exception):     # a limit notice is not notes
                notes = (await mind.notes()).strip()
    await _close_mind(task)
    return notes


def _keep_notes(who: str, topic: str, transcript: str, notes: str) -> None:
    """The call on disk beside its flight recording: data/calls/<when>-<who>.md."""
    from pathlib import Path

    from . import store
    if not (transcript or notes):
        return
    with contextlib.suppress(Exception):
        folder = Path(store.DB_PATH).parent / "calls"
        folder.mkdir(parents=True, exist_ok=True)
        import time as _t
        path = folder / f"{_t.strftime('%Y%m%d-%H%M%S')}-{(who or 'call')[:30].replace(' ', '_')}.md"
        path.write_text(f"# Call with {who} — {topic}\n\n## Notes\n\n{notes or '(none)'}\n\n"
                        f"## Transcript\n\n{transcript or '(none)'}\n")


async def _close_mind(task: "asyncio.Task") -> None:
    with contextlib.suppress(Exception):
        if task.done() and not task.cancelled() and task.exception() is None:
            await task.result().close()
        else:
            task.cancel()


async def converse(who: str, topic: str, workspace: str = "", seconds: float = 0,
                   agenda: str = "") -> str:
    """Ring `who` and actually talk with them about `topic`. Returns how it went.

    The shape is: ring, wait to be answered, turn captions on, open, then listen
    and reply until they are done. Two rules it must not break, both learned the
    hard way in front of a colleague:

    * Never speak into a call nobody answered. `wait_for_answer` returning
      "no answer" or "ended" means hang up in silence — and "unknown" does NOT,
      which is the bug that once cut Alex off mid-sentence.
    * Never hold the line in silence. If the brain cannot produce an answer, say
      so out loud and offer to come back, because the alternative is a colleague
      talking to nothing for forty seconds.
    """
    started = asyncio.get_event_loop().time()
    # Why he rang. Stashed rather than threaded through, because the thing that
    # needs it — the voice note left when nobody picks up — is decided several
    # frames away in `call_watch`, which only ever knew WHO was called.
    from . import store
    store.kv_set("call_topic", (topic or "")[:400])

    def elapsed() -> float:
        return asyncio.get_event_loop().time() - started

    limit = seconds or CONVERSE_SECONDS
    max_turns = max(MAX_TURNS, int(limit // 10))
    # The call's own brain starts now, so it is warm by the time they answer.
    from . import call_mind, voice
    thinking_ahead = asyncio.get_event_loop().create_task(
        call_mind.start(who, topic, agenda=agenda, minutes=round(limit / 60, 1) if seconds else 0))
    asyncio.get_event_loop().create_task(voice.warm_the_ears())
    # Nobody's phone rings unless something can talk to them. The brain primes in
    # a few seconds; if it is out of its usage window the call is not placed.
    with contextlib.suppress(Exception):      # slow or broken: the call still goes ahead
        await asyncio.wait_for(asyncio.shield(thinking_ahead), timeout=BRAIN_READY_SECONDS)
    if thinking_ahead.done() and isinstance(thinking_ahead.exception(), call_mind.Unavailable):
        return (f"Didn't call {who} — the brain that would talk to them is unavailable "
                f"({thinking_ahead.exception()}). Nothing rang.")
    try:
        rang = await meetings.call_person(who)
    except RuntimeError as exc:
        await _close_mind(thinking_ahead)
        return f"Didn't call {who} — {exc}. Nothing rang."

    page = (meetings._CALL or {}).get("page")
    said: list[str] = []
    heard_any = False
    opener = (f"Hi, this is Asta, Arun's assistant. Arun asked me to call you about "
              f"{topic}. Is now a good time?")
    # Made while it rings, so the greeting plays the moment they say hello —
    # people speak the instant they pick up. The plain greeting is made FIRST,
    # before anything else is queued on the voice server: on 22 Sep it waited
    # behind thirteen other lines and started seven seconds after her hello.
    ready = asyncio.get_event_loop().create_task(_compose_opener(thinking_ahead, opener))
    try:
        state = await meetings.wait_for_answer(page, seconds=40)
        if state in ("no answer", "ended"):
            return f"Called {rang} — no answer. I said nothing and hung up."

        rtc = bool(meetings._CALL.get("rtc"))
        if not rtc:
            with contextlib.suppress(Exception):
                meetings._CALL["captions_on"] = await meetings.start_captions(page)
        elif meetings._CALL.get("answered_by") == "voice":
            from . import call_rtc
            answered = await call_rtc.greeting(meetings._CALL["ctx"])
            log = meetings._CALL.get("log")
            if log:
                log({"greeting": answered})
            if answered["voicemail"]:
                return (f"Called {rang} — it went to voicemail, so I said nothing "
                        f"and hung up.")

        # The brain's own greeting only if it is already written AND made;
        # otherwise the plain one, which is ready. Never wait here: they spoke.
        if ready.done() and not ready.cancelled() and ready.exception() is None:
            opener = ready.result() or opener
        # They may talk over Asta, and should be able to: it stops and listens.
        meetings._CALL["barge_in"] = rtc
        await meetings.say_in_call(opener)
        said.append(opener)

        lines: list[dict] = [{"speaker": "Asta", "text": opener}] if rtc else []
        turns = 0
        ended = False
        interrupted = bool(meetings._CALL.get("interrupted"))
        nudged = False
        while elapsed() < limit and turns < max_turns:
            theirs = (await _hear_rtc(lines, rang, keep=interrupted) if rtc
                      else await _hear(page, lines, HEAR_SECONDS))
            if not theirs:
                # One nudge, the way a person would, before deciding they have gone.
                if rtc and not nudged and heard_any:
                    nudged = True
                    await _say(_STILL_THERE, said, lines, rtc)
                    interrupted = bool(meetings._CALL.get("interrupted"))
                    continue
                break
            heard_any = True
            turns += 1
            if theirs == _UNHEARD:
                await _say("Sorry, I didn't catch that. Could you say it again?", said, lines, rtc)
                interrupted = bool(meetings._CALL.get("interrupted"))
                continue
            mind = await _mind_if_ready(thinking_ahead)
            if mind is not None:
                ended, interrupted = await _speak_reply(
                    mind, theirs, said, lines, rtc,
                    elapsed=elapsed(), limit=limit if seconds else 0)
            else:
                thinking = asyncio.get_event_loop().create_task(answer_from_knowledge(
                    f"You are Arun's assistant on a live phone call with his colleague "
                    f"{who}, about {topic}. They just said: \"{theirs}\". Reply in ONE "
                    f"or TWO short spoken sentences. Never invent a fact about Arun's "
                    f"intentions or commit him to anything — if you do not know, say "
                    f"you will check with Arun and come back.", workspace))
                await _say(spoken_form(await _answer_without_dead_air(thinking)), said, lines, rtc)
                interrupted = bool(meetings._CALL.get("interrupted"))
            if ended:
                break

        if not ended:
            await _say("That's all I needed. Thanks for your time.", said, lines, rtc)
        transcript = meetings.transcript_text(lines) if lines else ""
    finally:
        with contextlib.suppress(Exception):
            await meetings.leave()
        notes = await _notes_and_close(thinking_ahead, heard_any)

    if not heard_any:
        return (f"Called {rang} and spoke, but captured nothing back — either they "
                f"said nothing or it could not be heard, so I can't tell you what was "
                f"said. Treat this as a call that happened, not a discussion.")
    _keep_notes(rang, topic, transcript, notes)
    tail = f"\n\nNotes:\n{notes}" if notes else ""
    return (f"Talked to {rang} about {topic} for {elapsed():.0f}s.\n\n"
            f"What they said:\n{transcript or '(no captions captured)'}{tail}")
