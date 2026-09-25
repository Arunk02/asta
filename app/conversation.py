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
import re

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

    Anything heard while Asta was talking — a quick "yes" over the end of its
    line, or its own words off a speakerphone — is kept, and Asta's own words
    are taken off the front before anyone replies to it. A turn that turns out
    to be nothing but Asta's echo is not a turn: listen again.
    """
    from . import call_rtc
    call = meetings._CALL
    keep = keep or float(call.get("heard_during") or 0) >= call_rtc.TURN_VOICE_MS
    for _ in range(3):
        got = await call_rtc.hear_turn(call.get("ctx"), wait=HEAR_SECONDS,
                                       log=call.get("log"), keep=keep,
                                       on_pause=_acknowledge)
        raw = (got.get("text") or "").strip()
        text = call_rtc.strip_echo(raw, call.get("last_said", "")) if keep else raw
        log = call.get("log")
        if log:
            log({"heard": raw[:200], "kept": text[:200], "spoke": got.get("spoke")})
        if keep and raw and not text:
            keep = False                  # only Asta's echo: listen for them
            continue
        break
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

#: Said from their words the moment they are understood, before any brain has
#: answered — the brain's speed on the day decides nothing about when Asta
#: reacts. Measured 22 Sep: the same first sentence took 0.8 s on one run and
#: 6.7 s on the next.
#: Just saying hello back. Asta's own greeting answers this, so a canned word
#: in front of it is the stutter Harika heard: "Hello?" → "Sure."
#: Only an actual hello. "Yes." mid-call is agreement, and it has its own
#: reaction — a greeting pattern that swallowed it would mute the answer.
_GREETING = re.compile(r"^\W*(?:hi|hey|hello|hallo|namaste)\b[\s\W]*$"
                       r"|^\W*(?:hi|hey|hello)[,\s]+(?:how can i help|there|Asta)", re.I)

#: A bare acknowledgement — "okay", "sure", "got it", "thanks" with nothing else
#: in it. There is nothing to react TO: what follows is Asta's closing line, and
#: a canned word in front of it is the stutter Harika heard at the end of the
#: 24 Sep call ("Okay" → "Great." → "Sounds good — thanks for the update").
_ACKNOWLEDGEMENT = re.compile(
    r"^\W*(?:ok|okay|k|alright|all right|right|sure|fine|good|great|cool|"
    r"got it|understood|noted|thanks|thank you|thanks a lot|hmm|mm|yeah|yep|ya)"
    r"[\s\W]*$", re.I)

#: (pattern, what to say, may it fire on a QUESTION). The flag is the lesson of
#: "No, apart from this can you discuss any other things?" — that sentence starts
#: with "no" and is a question, and it got "No worries.", which answers the word
#: rather than the sentence. Only a rule that makes sense as a reply to a
#: question may fire on one.
_REACT_RULES = (
    (re.compile(r"\bwho (?:is|'?s) (?:this|that|calling)\b|\bwho are you\b", re.I),
     "Oh, sorry.", True),
    (re.compile(r"\b(?:bye|goodbye|talk (?:to you )?later|gotta go|have to go)\b|\bthank(?:s| you)\b", re.I),
     "Sounds good.", False),
    (re.compile(r"^\W*(?:no|nope|not really|not now|busy|later)\b", re.I),
     "No worries.", False),
    (re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|okay|ok|go ahead|fine|good|haan|ha)\b", re.I),
     "Great.", False),
)
_DEFAULT_REACTIONS = ("Got it.", "Okay.")
_defaults = {"n": 0}

#: A sentence that ASKS for something, whether or not it ends in a question
#: mark. Whisper writes no punctuation it did not hear, and a demand is not
#: phrased as a question anyway: "Yeah, sure. I want to know the reason for this
#: call or disconnect" got "Great." on the 25 Sep call — Asta agreeing brightly
#: with somebody who was asking why it had rung them. The leading "yeah" is what
#: the old rule matched on, so what a sentence STARTS with cannot be the test.
_ASKING = re.compile(
    r"\b(?:i (?:want|need|would like) to know|tell me|let me know|explain|"
    r"what(?:'?s| is| are|s)? (?:this|that|it|the)|why (?:are|is|did|do|you)|"
    r"can you (?:tell|explain|say|share)|could you (?:tell|explain|say|share)|"
    r"who (?:is|'?s|are)|what (?:do|did|does) you|reason for (?:this|the) call)\b",
    re.I)

#: What to say while thinking about a QUESTION. "Sure." used to answer every
#: sentence ending in a question mark, so "Hello?" and "how can I help you?"
#: both got "Sure." — a word that answers a request, not a question, and the
#: first thing that made Asta sound like a machine on the 24 Sep call.
_THINKING = "Mm."


def quick_reaction(theirs: str) -> str:
    """The ready-made reaction that fits what they just said — '' for none.

    Nothing is safer than the wrong thing. A greeting is answered by Asta's own
    next sentence, and a question gets a thinking noise rather than a word that
    pretends to answer it.
    """
    theirs = theirs or ""
    if _GREETING.search(theirs) or _ACKNOWLEDGEMENT.search(theirs):
        return ""
    question = theirs.rstrip().endswith("?") or bool(_ASKING.search(theirs))
    for pattern, line, on_question in _REACT_RULES:
        if pattern.search(theirs) and (on_question or not question):
            return line
    if question:
        return _THINKING
    line = _DEFAULT_REACTIONS[_defaults["n"] % len(_DEFAULT_REACTIONS)]
    _defaults["n"] += 1
    return line


def without_echo(text: str, reaction: str) -> str:
    """The brain's sentence with an opener Asta has JUST said taken off.

    "No worries." followed by "No worries — do you have a rough sense…" is how
    it actually came out on the call. The old guard only caught a sentence that
    was nothing BUT a reaction, so a reaction with the answer attached to it
    said the same two words twice.
    """
    said = (reaction or "").strip().strip(".!,").lower()
    if not said or not text:
        return text
    body = text.lstrip()
    if body.lower().startswith(said):
        rest = body[len(said):].lstrip(" ,.—–-!:;")
        # Only when something is actually left: "No worries." on its own stays
        # the brain's whole answer, and is dropped by the caller instead.
        if rest:
            return rest[0].upper() + rest[1:] if rest[0].islower() else rest
    return text


#: How long the brain has to produce its own first sentence before Asta reacts
#: by itself.
QUICK_REACTION_AFTER = 0.35

#: What a listener says the instant the other person stops. Not from the reply
#: reactions ("Okay.", "Great."), so the two never sound like a stutter.
_ACKS = ("Mm-hm.", "Mm.")
_acks = {"n": 0}


def _acknowledge() -> None:
    """Say a short "mm-hm" without waiting — the reply is still being thought."""
    line = _ACKS[_acks["n"] % len(_ACKS)]
    _acks["n"] += 1

    async def go() -> None:
        from . import call_rtc
        with contextlib.suppress(Exception):
            await call_rtc.say_quick(line)
    task = asyncio.get_event_loop().create_task(go())
    _ACKING.add(task)
    task.add_done_callback(_ACKING.discard)


_ACKING: set = set()
_SAY_AGAIN = "Sorry, I didn't catch that. Could you say it again?"

#: Asta has ALREADY asked them to repeat themselves — in its own words, from the
#: brain, rather than in the canned line. Saying the canned one on top of it is
#: two apologies for one unheard turn, which is what the 25 Sep call opened with.
_ALREADY_ASKED = re.compile(
    r"\b(?:say (?:that|it) again|repeat (?:that|it)|didn'?t (?:catch|get) that|"
    r"came through .{0,20}garbled|could not make (?:that )?out)\b", re.I)


def ask_again(last_said: str) -> str:
    """The line to say when their turn could not be heard — '' if Asta has just
    asked for exactly that and is still waiting for it."""
    return "" if _ALREADY_ASKED.search(last_said or "") else _SAY_AGAIN


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
                made = (asyncio.get_event_loop().create_task(
                    meetings.synth(voice.strip_voice_instruction(text))) if text else None)
                await queue.put((text, call_mind.END in sentence, made))
        except Exception as exc:                                 # noqa: BLE001
            # Out of its window mid-call: say so like a person and wrap up,
            # never read the brain's error out loud.
            if isinstance(exc, call_mind.Unavailable):
                await queue.put((_DROPPING_OFF, True, None))
            else:
                await queue.put((_SORRY, False, None))
        finally:
            await queue.put(None)

    asyncio.get_event_loop().create_task(produce())
    meetings._CALL["last_said"] = ""          # echo is judged against this reply
    asked_at = asyncio.get_event_loop().time()
    log = meetings._CALL.get("log")
    ended = interrupted = spoke = covered = False
    reacted = False
    ahead: list = []
    try:
        first = await asyncio.wait_for(queue.get(), timeout=QUICK_REACTION_AFTER)
        ahead.append(first)
    except asyncio.TimeoutError:
        # The brain is slow today; react now, from their words.
        reaction = quick_reaction(theirs)
        if reaction:
            await _say(reaction, said, lines, rtc)
            reacted = spoke = True
    while True:
        if ahead:
            item = ahead.pop(0)
        else:
            try:
                item = await asyncio.wait_for(queue.get(),
                                              timeout=MOMENT_AFTER if not covered else ANSWER_TIMEOUT)
            except asyncio.TimeoutError:
                if covered:
                    break
                covered = True
                await _say(_next_moment(), said, lines, rtc)
                continue
        if item is None:
            break
        text, last, _made = item
        ended = ended or last
        if log and not spoke:
            log({"first_sentence_after_s": round(asyncio.get_event_loop().time() - asked_at, 2),
                 "first": text[:80]})
        if not text:
            continue
        if reacted:
            if text in call_mind.REACTIONS:
                reacted = False       # already reacted; the brain's own would stutter
                continue
            # Or the brain opened its answer with the same words Asta just said.
            trimmed = without_echo(text, said[-1] if said else "")
            reacted = False
            if trimmed != text:
                text = trimmed
        if spoke and rtc and await _they_started(meetings._CALL.get("ctx")):
            # They began talking in the gap between two of Asta's sentences:
            # stop here and listen, as a person would.
            interrupted, ended = True, False
            meetings._CALL["interrupted"] = True
            break
        if text in call_mind.REACTIONS and not last:
            # A reaction runs straight into what follows it; a gap after
            # "Great." is where the other person takes their turn.
            nxt = await _next_ready(queue, ahead)
            if nxt is not None:
                ahead.append(nxt)
        await _say(text, said, lines, rtc)
        spoke = True
        if meetings._CALL.get("interrupted"):
            interrupted = True
            ended = False
            break
    return ended, interrupted


#: How long a reaction waits for the sentence after it to be ready.
FOLLOW_ON_WAIT = 0.25


async def _next_ready(queue: asyncio.Queue, ahead: list):
    """The next sentence, once its audio is made — or None if it is not coming soon."""
    try:
        nxt = await asyncio.wait_for(queue.get(), timeout=FOLLOW_ON_WAIT)
    except asyncio.TimeoutError:
        return None
    if nxt is not None and nxt[2] is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(nxt[2]), timeout=FOLLOW_ON_WAIT)
    return nxt


async def _they_started(ctx) -> bool:
    from . import call_rtc
    if ctx is None:
        return False
    with contextlib.suppress(Exception):
        return await call_rtc.talking_now(ctx)
    return False


def opening_lines(opener: str) -> tuple[str, str]:
    """The greeting, and everything after it.

    Said as two lines rather than one because of what one line costs: on 25 Sep
    a three-sentence opener in his cloned voice was synthesised whole before any
    of it left the call, and the colleague who picked up heard two minutes and
    ten seconds of nothing. A greeting is short, so a greeting on its own is
    ready in a fraction of the time — and it is the part that must not wait.
    """
    line = (opener or "").strip()
    cut = -1
    for stop in (". ", "! ", "? "):
        at = line.find(stop)
        if at != -1 and (cut == -1 or at < cut):
            cut = at
    if cut == -1:
        return line, ""
    return line[:cut + 1].strip(), line[cut + 2:].strip()


async def _prepare_opening(opener: str) -> None:
    """Make the greeting FIRST, then the rest of the opener.

    Order of synthesis is the dead air: whatever is made first is what can be
    said first, and everything queued in front of the greeting is silence on a
    line somebody has just answered.
    """
    hello, rest = opening_lines(opener)
    await _prepare([x for x in (hello, rest) if x])


async def _compose_opener(mind_task: "asyncio.Task", fallback: str) -> str:
    """The first line, made before they can pick up.

    Order is the point: the plain greeting first, then the reactions every reply
    opens with, then the fillers — and only then a greeting written by the call's
    own brain, which is used if it is ready by the time they answer.
    """
    from . import call_mind
    hello, rest = opening_lines(fallback)
    order = [hello, rest, *call_mind.REACTIONS, "Oh, sorry.", *_ACKS, *_MOMENTS,
             _SAY_AGAIN, _STILL_THERE]
    from . import voice as _voice
    if _voice.in_voice() == _voice.VOICE_MINE:
        # His clone costs ~7s a line against Kokoro's ~0.4s, so the whole list
        # does not fit in one ring. The acks come first after the greeting: an
        # ack exists to cover synthesis time, and an uncached one costs exactly
        # the silence it was meant to hide.
        order = [hello, rest, *_ACKS, *_MOMENTS, *call_mind.REACTIONS, "Oh, sorry.",
                 _SAY_AGAIN, _STILL_THERE]
    await _prepare([x for x in order if x])
    mind = await _mind_if_ready(mind_task, wait=30)
    if mind is None:
        return fallback
    with contextlib.suppress(Exception):
        line = spoken_form(await mind.opener())
        if line:
            await _prepare_opening(line)
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
                   agenda: str = "", languages: str = "",
                   voice_name: str = "") -> str:
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
    # Which languages this call may be in ("en,hi" for a call that runs in both).
    # Set before a word is heard: it decides how every turn is transcribed.
    from . import call_rtc as _rtc
    from . import voice as _voice
    _rtc.speaking(languages)
    # And whose voice it is held in, set before a single line is synthesised —
    # the opener is made while the phone rings, so any later would leave the
    # first thing they hear in the wrong voice.
    _voice.in_voice(voice_name or _voice.VOICE_ASSISTANT)
    his_voice = _voice.in_voice() == _voice.VOICE_MINE
    # The call's own brain starts now, so it is warm by the time they answer.
    from . import call_mind, voice
    thinking_ahead = asyncio.get_event_loop().create_task(
        call_mind.start(who, topic, agenda=agenda,
                        minutes=round(limit / 60, 1) if seconds else 0,
                        as_him=his_voice))
    asyncio.get_event_loop().create_task(voice.warm_the_ears())
    asyncio.get_event_loop().create_task(
        voice.warm_the_voice(languages, _voice.in_voice()))
    # Nobody's phone rings unless something can talk to them: no voice, no call.
    from . import call_rtc
    if call_rtc.enabled() and not await voice.available():
        await _close_mind(thinking_ahead)
        return (f"Didn't call {who} — the voice service is not answering, so Asta "
                f"could not speak. Nothing rang.")
    # The brain primes in a few seconds; if it is out of its usage window the
    # call is not placed either.
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
    opener = (f"Hi, is now a good time for {topic}?" if his_voice else
              f"Hi, it's Asta, Arun's assistant — is now a good time for {topic}?")
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
        # The greeting plays to the end: people say "hello?" over it as they pick
        # up. After it they may talk over Asta, and it stops and listens.
        meetings._CALL["barge_in"] = False
        meetings._CALL["last_said"] = ""
        hello, rest = opening_lines(opener)
        await meetings.say_in_call(hello)
        said.append(hello)
        # Only the greeting itself plays through a "hello?" — from here on they
        # may talk over Asta, including over the rest of the opener.
        meetings._CALL["barge_in"] = rtc
        if rest:
            await meetings.say_in_call(rest)
            said.append(rest)

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
                await _say(ask_again(said[-1] if said else ""), said, lines, rtc)
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
        # His voice belongs to this call only. Left set, the next call — one he
        # never asked to be in his voice — would go out in it.
        _voice.in_voice(_voice.VOICE_ASSISTANT)
        notes = await _notes_and_close(thinking_ahead, heard_any)

    if not heard_any:
        return (f"Called {rang} and spoke, but captured nothing back — either they "
                f"said nothing or it could not be heard, so I can't tell you what was "
                f"said. Treat this as a call that happened, not a discussion.")
    _keep_notes(rang, topic, transcript, notes)
    tail = f"\n\nNotes:\n{notes}" if notes else ""
    return (f"Talked to {rang} about {topic} for {elapsed():.0f}s.\n\n"
            f"What they said:\n{transcript or '(no captions captured)'}{tail}")
