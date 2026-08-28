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
question, and had no capability that did both. Asked to "call Vinish and discuss
the PR comments" it answered "I can't hold a live conversation with Vinish
myself", which was true only because this file did not exist.

It is deliberately NOT a script of prepared lines. A script is what produced the
failure Vinish described himself — "he keep on asking questions, nothing was
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


async def converse(who: str, topic: str, workspace: str = "") -> str:
    """Ring `who` and actually talk with them about `topic`. Returns how it went.

    The shape is: ring, wait to be answered, turn captions on, open, then listen
    and reply until they are done. Two rules it must not break, both learned the
    hard way in front of a colleague:

    * Never speak into a call nobody answered. `wait_for_answer` returning
      "no answer" or "ended" means hang up in silence — and "unknown" does NOT,
      which is the bug that once cut Vinish off mid-sentence.
    * Never hold the line in silence. If the brain cannot produce an answer, say
      so out loud and offer to come back, because the alternative is a colleague
      talking to nothing for forty seconds.
    """
    started = asyncio.get_event_loop().time()

    def elapsed() -> float:
        return asyncio.get_event_loop().time() - started

    try:
        rang = await meetings.call_person(who)
    except RuntimeError as exc:
        return f"Didn't call {who} — {exc}. Nothing rang."

    page = (meetings._CALL or {}).get("page")
    said: list[str] = []
    heard_any = False
    try:
        state = await meetings.wait_for_answer(page, seconds=40)
        if state in ("no answer", "ended"):
            return f"Called {rang} — no answer. I said nothing and hung up."

        with contextlib.suppress(Exception):
            meetings._CALL["captions_on"] = await meetings.start_captions(page)

        opener = (f"Hi, this is Asta, Arun's assistant. Arun asked me to go through "
                  f"{topic} with you. Is now a good time?")
        await meetings.say_in_call(opener)
        said.append(opener)

        lines: list[dict] = []
        turns = 0
        while elapsed() < CONVERSE_SECONDS and turns < MAX_TURNS:
            theirs = await _hear(page, lines, HEAR_SECONDS)
            if not theirs:
                break
            heard_any = True
            turns += 1
            try:
                reply = await asyncio.wait_for(answer_from_knowledge(
                    f"You are Arun's assistant on a live phone call with his colleague "
                    f"{who}, about {topic}. They just said: \"{theirs}\". Reply in ONE "
                    f"or TWO short spoken sentences. Never invent a fact about Arun's "
                    f"intentions or commit him to anything — if you do not know, say "
                    f"you will check with Arun and come back.", workspace), timeout=30)
            except Exception:                                        # noqa: BLE001
                reply = ("Sorry, I could not work that out just now — "
                         "I'll check with Arun and come back to you.")
            reply = spoken_form(reply)
            with contextlib.suppress(Exception):
                await meetings.say_in_call(reply)
            said.append(reply)

        closing = "That's all I needed. Thanks for your time."
        with contextlib.suppress(Exception):
            await meetings.say_in_call(closing)
        transcript = meetings.transcript_text(lines) if lines else ""
    finally:
        with contextlib.suppress(Exception):
            await meetings.leave()

    if not heard_any:
        return (f"Called {rang} and spoke, but captured nothing back — either they "
                f"said nothing or live captions never started, so I can't tell you "
                f"what was said. Treat this as a call that happened, not a discussion.")
    return (f"Talked to {rang} about {topic} for {elapsed():.0f}s.\n\n"
            f"What they said:\n{transcript or '(no captions captured)'}")
