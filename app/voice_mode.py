"""Voice mode — Asta's voice and Asta's ears, two switches.

His design, 2 Oct: two buttons, not one.

  🔊 Voice  (⌃⌥A)  Asta SPEAKS: answers, and the updates that matter — a task
                   done, CI red on his PR, someone asking him, a call request.
  🎙 Mic    (⌃⌥M)  Asta LISTENS: he talks, Asta answers, back and forth.

Voice on with the mic off is the film-on case: Asta can tell him things, and
nothing in the room — the film, music, colleagues — can ever reach it. Both
off is how it always was: WhatsApp and the UI. Both on is a conversation.

The menu-bar helper (deploy/voice/AstaVoice.swift) is deliberately thin: two
global hotkeys, the microphone (with the Mac's own echo cancelling), playing
audio, the icon. Everything that decides anything is here, where it is tested:
what to say, how to say it short, whether a sentence was meant for Asta, and
when to stop listening.

Rules that do not bend:
  * Both switches start OFF — after a restart too. The mic is released, not
    ignored; macOS's orange dot is the proof.
  * The mic closes itself after MIC_IDLE_SECONDS without a word for Asta, and
    when his screen locks.
  * A voice turn is an ordinary turn: same brain, guardrails, approvals. A
    send is read back and goes on "send it"; merges, group posts and anyone on
    his manager-and-above list still need a tap — voice can only prepare them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time

from . import store

#: The mic closes after this long without a sentence meant for Asta.
MIC_IDLE_SECONDS = 300.0
#: Within this long of Asta last speaking (or the mic opening), what he says is
#: taken as meant for Asta without asking.
FOLLOW_WINDOW_SECONDS = 120.0
#: A voice turn that has said nothing by now gets a spoken acknowledgement.
#: Measured 2 Oct: 0.9 s to know he stopped, 1.4 s to transcribe, then the brain
#: takes 20-35 s. Silence that long reads as "it didn't hear me".
ACK_SECONDS = 1.2
#: Prepared when the voice comes on, so they play with no synthesis wait.
ACKS = {"question": "Let me check.", "do": "On it.", "now": "Checking now.", "look": "Let me look.",
        "listening": "I'm listening.", "still": "Still on that — I'll tell you.", "moment": "One moment."}
#: He pauses mid-sentence. What he said is held this long for more before it is
#: acted on: a breath after a finished sentence, longer after one that is not
#: ("So I want to do debug on booking like a" went in alone, 2 Oct, and Asta
#: answered the half while he was still saying the rest).
HOLD_SECONDS = 0.25
HOLD_UNFINISHED_SECONDS = 3.0
#: A piece waits at most this long for him to go on. Room sound reads as "he is
#: talking" too, and once held "Yeah, Aastha" for 14 s (2 Oct 13:42).
HOLD_MAX_SECONDS = 4.0
#: A turn still being spoken is decided after this long, whatever happens.
TURN_MAX_SECONDS = 20.0
#: What was said this recently is "the conversation": the decision and the work see it.
CONVERSATION_SECONDS = 180.0
#: A question with no first words by now gets one short "One moment." — never
#: before ("Let me see." before every answer, then nothing, 2 Oct).
FILLER_SECONDS = 4.5
#: A job answering after this long, when he has spoken since, is not read out.
LATE_SECONDS = 20.0
#: How long a voice turn may run before its answer goes to the chat instead.
TURN_SECONDS = 600.0
#: The levels of `notify` that are worth saying out loud, when they are addressed to him.
#: Only these four (2 Oct: reminders and "message sent" read aloud were chatter).
SPOKEN_LEVELS = {"task", "ci", "answer", "calls", "action"}
#: What `speakable` adds when it had to leave something out. Wherever it is
#: said, the whole text really is put in his chat (2 Oct: "it keeps telling me
#: it's in the chat, and nothing came").
IN_CHAT = "The details are in the chat."
#: Sentences spoken of one answer; the rest is in the chat.
SPOKEN_SENTENCES = 2
#: Updates waiting while another app has the mic (he is on a call).
QUEUE_MAX = 10

_STATE: dict = {"speaker": False, "mic": False, "busy": False, "mic_on_at": 0.0,
                "last_heard": 0.0, "last_spoke": 0.0, "barged_at": 0.0, "helper": None,
                "barge_heard": 0.0}
#: What Asta said lately, every line (updates too): its own voice, heard back
#: through the mic, is not him. (when, words)
_SPOKEN: list[tuple[float, str]] = []
#: Asta's own lines are recognised as echo for this long after they are sent —
#: they queue (and now wait while he talks), so they can play well after.
#: 2 Oct 13:46: a line heard back 68 s after it was sent became a request.
ECHO_SECONDS = 150.0
#: Within this long, a line sent recently counts as echo on most of its words;
#: beyond it, only a near-whole repeat does (he may echo Asta's words himself).
ECHO_CLOSE_SECONDS = 30.0
#: Misheard echo is looked for in what Asta said this recently...
ECHO_FUZZY_SECONDS = 120.0
#: ...at this likeness, window by window (his own lines scored at most 0.54).
ECHO_FUZZY = 0.66
#: Most of the words Asta just said, AND this much of its sound.
ECHO_WORDS_AND_SOUND = 0.7
#: The same thing is not said twice within this long ("Got it — only booking PR
#: 1429 and AP PR 1252" three times in a minute, 2 Oct).
REPEAT_SECONDS = 90.0
_QUEUE: list[str] = []
#: The turn being assembled: his pieces so far, a counter that moves when more
#: comes, whether he is talking right now, and transcriptions still in flight.
_TURN: dict = {"parts": [], "gen": 0, "first_at": 0.0, "speaking": False, "pending": 0}
#: The conversation, newest last: (when, "Arun" | "Asta", words).
_HEARD: list[tuple[float, str, str]] = []
_KV = "voice_mode"
#: Ready-made audio for the acknowledgements: phrase -> base64 wav.
_CACHE: dict[str, str] = {}


async def warm_acks() -> None:
    """Make the acknowledgements once, so each plays the instant it is needed."""
    from . import voice
    for phrase in ACKS.values():
        if phrase in _CACHE:
            continue
        with contextlib.suppress(Exception):
            _CACHE[phrase] = base64.b64encode(await voice.speak(phrase, voice="assistant")).decode()


# --- the two switches -----------------------------------------------------------------

def state() -> dict:
    """What the switches say, and whether anything can actually speak or listen."""
    return {"speaker": _STATE["speaker"], "mic": _STATE["mic"], "busy": _STATE["busy"],
            "helper": _STATE["helper"] is not None}


def on() -> bool:
    """Asta's voice is on AND something can play it."""
    return bool(_STATE["speaker"] and _STATE["helper"] is not None)


def listening() -> bool:
    return bool(_STATE["mic"] and _STATE["helper"] is not None)


async def set_mode(speaker: bool | None = None, mic: bool | None = None,
                   why: str = "") -> dict:
    """Flip a switch, tell the helper (icon + chime), remember it for the UI."""
    now = time.time()
    if speaker is not None:
        _STATE["speaker"] = bool(speaker)
    if mic is not None:
        if mic and not _STATE["mic"]:
            _STATE["mic_on_at"] = now
            _STATE["last_heard"] = now
        _STATE["mic"] = bool(mic)
        if not mic:
            _TURN["speaking"] = False
    store.kv_set(_KV, json.dumps({"speaker": _STATE["speaker"], "mic": _STATE["mic"], "at": now}))
    # Warm what is about to be used. The first transcription loads the model —
    # 9.8 s measured, then 1.4 s — and his first sentence must not be the one
    # that pays for it.
    with contextlib.suppress(Exception):
        from . import voice
        from . import voice_talker
        if mic:
            asyncio.ensure_future(voice.warm_the_ears())
            asyncio.ensure_future(voice_talker.warm())
        elif mic is False and not _STATE["speaker"]:
            asyncio.ensure_future(voice_talker.close())
        if speaker:
            asyncio.ensure_future(voice.warm_the_voice())
            asyncio.ensure_future(warm_acks())
            asyncio.ensure_future(voice_talker.warm())
    store.record_outcome("voice", "mode", detail=f"speaker={_STATE['speaker']} "
                                                f"mic={_STATE['mic']} {why}"[:200])
    await _to_helper({"type": "state", **state(), "why": why})
    return state()


async def toggle(which: str, why: str = "") -> dict:
    if which == "mic":
        return await set_mode(mic=not _STATE["mic"], why=why)
    return await set_mode(speaker=not _STATE["speaker"], why=why)


def startup() -> bool:
    """Both off on every start. True when they had been on — he is told once."""
    try:
        was = json.loads(store.kv_get(_KV) or "{}")
    except (ValueError, TypeError):
        was = {}
    _STATE.update(speaker=False, mic=False, busy=False)
    store.kv_set(_KV, json.dumps({"speaker": False, "mic": False, "at": time.time()}))
    return bool(was.get("speaker") or was.get("mic"))


#: "voice on", "mic off", "asta voice off", "speaker on" — from WhatsApp or the UI.
COMMAND = re.compile(r"^\s*(?:asta\s+)?(voice|speaker|mic|microphone|listening)\s+(on|off)\s*[.!]*\s*$",
                     re.I)


async def command(text: str) -> str:
    """The reply for a switch command, or "" when it is not one."""
    m = COMMAND.match(text or "")
    if not m:
        return ""
    which = "mic" if m.group(1).lower() in ("mic", "microphone", "listening") else "speaker"
    value = m.group(2).lower() == "on"
    await set_mode(**{which: value}, why="asked in chat")
    if _STATE["helper"] is None:
        return ("⚠️ The Asta Voice menu-bar app is not running, so nothing can "
                f"{'speak' if which == 'speaker' else 'listen'} yet — it starts at login.")
    name = "Voice" if which == "speaker" else "Mic"
    return f"{'🔊' if which == 'speaker' else '🎙'} {name} {'on' if value else 'off'}."


# --- saying things --------------------------------------------------------------------

_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"```.*?```", re.S)
_MARK = re.compile(r"[*_`#>|]+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")


def _sentences(text: str) -> tuple[list[str], bool]:
    """The answer as plain spoken sentences, and whether it carried links or code."""
    raw = _CODE.sub(" ", text or "")
    extra = bool(_URL.search(raw)) or "```" in (text or "")
    raw = _URL.sub(" ", raw)
    raw = _EMOJI.sub(" ", _MARK.sub(" ", raw))
    lines = [ln.strip(" -•\t") for ln in raw.splitlines() if ln.strip(" -•\t")]
    flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return [p for p in re.split(r"(?<=[.!?।])\s+", flat) if p], extra


def first_sentence(text: str) -> str:
    """The first sentence of an answer still being written — only once it is
    complete (more text follows it) and long enough to be worth saying alone."""
    if (text or "").count("```") % 2:
        return ""                                  # inside a code block
    parts, _ = _sentences(text)
    if len(parts) > 1 and len(parts[0]) >= 20:
        return parts[0]
    return ""


def speakable(text: str, sentences: int = SPOKEN_SENTENCES, skip: int = 0) -> str:
    """What of a written answer can be said out loud: no links, code, markup or
    emoji; the first sentences (after `skip` already said); and where the rest is."""
    parts, extra = _sentences(text)
    said = " ".join(parts[skip:sentences]).strip()
    more = len(parts) > sentences or extra
    if more and (said or skip):
        said = (said + " " + IN_CHAT).strip()
    return said[:600]


async def _to_helper(msg: dict) -> bool:
    ws = _STATE["helper"]
    if ws is None:
        return False
    try:
        await ws.send_text(json.dumps(msg))
        return True
    except Exception:                                          # noqa: BLE001
        _STATE["helper"] = None
        return False


async def to_chat(text: str) -> None:
    """The whole answer, in his WhatsApp — what "the details are in the chat" promises."""
    with contextlib.suppress(Exception):
        from . import notify
        await notify.wa_send("🎙 " + text.strip())


async def say_lines(text: str) -> bool:
    """A written answer, said a sentence at a time: the first plays while the
    next is being made. One block of two sentences took 2.6-3.5 s to start."""
    words = speakable(text)
    if not words:
        return False
    if IN_CHAT in words:
        asyncio.ensure_future(to_chat(text))
    parts = [p for p in re.split(r"(?<=[.!?।])\s+", words) if p.strip()]
    started = time.time()
    ok = False
    for part in parts:
        if _STATE["barged_at"] > started:
            break                               # he talked over it: the rest is in the chat
        ok = await say(part, kind="answer", since=started) or ok
    return ok


async def say(text: str, kind: str = "answer", since: float = 0.0) -> bool:
    """Speak it, if Asta's voice is on. False when it could not be said.

    The Asta voice from Voicebox when it is up; the helper falls back to the
    Mac's own voice with the text alone, so a missing TTS never means silence."""
    if not on():
        return False
    words = speakable(text)
    if not words:
        return False
    if IN_CHAT in words and IN_CHAT not in (text or "") and kind == "answer":
        asyncio.ensure_future(to_chat(text))        # cut short here: the rest goes to the chat
    if kind == "answer" and words not in ACKS.values() and said_lately(words):
        store.record_outcome("voice", "not_repeated", detail=words[:120])
        return True                             # already said: once is enough
    if _STATE["busy"] and kind == "update":
        _QUEUE.append(words)
        del _QUEUE[:-QUEUE_MAX]
        return True
    audio = _CACHE.get(words, "")
    if not audio:
        with contextlib.suppress(Exception):
            from . import voice
            audio = base64.b64encode(await voice.speak(words, voice="assistant")).decode()
    if since and _STATE["barged_at"] > since:
        return False                            # he talked over it while this was being made
    sent = await _to_helper({"type": "say", "text": words, "audio": audio,
                             "chime": kind == "update"})
    if sent:
        _STATE["last_spoke"] = time.time()
        _SPOKEN.append((time.time(), words))
        del _SPOKEN[:-20]
        if kind != "update":
            remember("Asta", words)
    return sent


def sounds_like(text: str, lines: list[str]) -> float:
    """How closely `text` matches some stretch of Asta's own lines, by sound-
    alike spelling: the best ratio over windows of the same length."""
    import difflib
    def norm(t: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", (t or "").lower()))
    h = norm(text)
    if len(h) < 5:
        return 0.0
    best = 0.0
    for line in lines:
        a = norm(line)
        n = len(h)
        windows = [a] if len(a) <= n else [a[i:i + n] for i in range(0, len(a) - n + 1, 2)]
        for w in windows:
            best = max(best, difflib.SequenceMatcher(None, h, w, autojunk=False).ratio())
            if best >= 0.99:
                return best
    return best


def said_lately(words: str, now: float | None = None) -> bool:
    """Asta said this — or nearly this — in the last REPEAT_SECONDS."""
    now = time.time() if now is None else now
    mine = set(_tokens(words))
    if len(mine) < 4:
        return False
    for at, w in _SPOKEN:
        # Asked again, it is said again: only a repeat with no word from him
        # in between is dropped.
        if now - at < REPEAT_SECONDS and at > _STATE.get("his_turn_at", 0.0):
            theirs = set(_tokens(w))
            if theirs and len(mine & theirs) / len(mine | theirs) >= 0.75:
                return True
    return False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9\u0900-\u097f']+", (text or "").lower())


def echo(text: str, while_speaking: bool = False, now: float | None = None) -> str:
    """What is left of `text` once Asta's own recent words are taken out —
    '' when it was all Asta, heard back through the mic.

    2 Oct, 13:05: on speakers, Asta's answers came back through the mic as
    clean sentences (the Mac's recognizer was 98% sure of them), counted as
    him talking over it, and cut Asta off — "no response"."""
    now = time.time() if now is None else now
    lines = [w for at, w in _SPOKEN if now - at < ECHO_SECONDS]
    if not lines:
        return text
    from .call_rtc import strip_echo
    close = set(_tokens(" ".join(w for at, w in _SPOKEN if now - at < ECHO_CLOSE_SECONDS)))
    ours = set(_tokens(" ".join(lines)))
    his = _tokens(text)
    if not his:
        return text
    near = sum(1 for w in his if w in close) / len(his)
    recent = [w for at, w in _SPOKEN if now - at < ECHO_FUZZY_SECONDS]
    like = sounds_like(text, recent) if recent else 0.0
    # Echo is Asta's own PHRASING coming back, not its vocabulary: 2 Oct 17:54
    # his "use my workspace knowledge, not Teams" shared Asta's words and was
    # dropped as echo. Words in common count only with the sound to match.
    if (len(his) >= 3 or while_speaking) and like >= ECHO_FUZZY:
        return ""               # misheard echo too: "Telecos Dark Space Copa"
    if near >= 0.7 and like >= ECHO_WORDS_AND_SOUND and (len(his) >= 3 or while_speaking):
        return ""
    # Asta's tail glued to the front of what he said.
    if not while_speaking:
        return text
    rest = strip_echo(text, " ".join(lines[-2:]))
    if not _tokens(rest) or _tokens(rest) == his:
        return text
    end = re.search(r"[.!?।]+$", text.strip())
    return rest + (end.group(0) if end else "")


def worth_saying(text: str, level: str, urgency: str) -> bool:
    """An update addressed to him, of a kind he asked to hear."""
    return on() and urgency == "direct" and level in SPOKEN_LEVELS and bool((text or "").strip())


def asks_him(text: str) -> bool:
    """Does this update want an answer from him?"""
    return bool(re.search(r"\?|\breply\b|\byes\b.*\bno\b|\bapprove\b|\bsend\b", text or "", re.I))


async def update(text: str, level: str, urgency: str) -> bool:
    """Called by `notify`: True when it was said aloud INSTEAD of being pushed.

    A question goes to his phone as well whenever the mic is off — he may be
    across the room, and a tap is the only way he can answer it."""
    if not worth_saying(text, level, urgency):
        return False
    if not await say(text, kind="update"):
        return False
    # Said in full, and wants nothing back: the phone stays quiet. A question
    # goes to the phone too (answerable either way), and so does anything that
    # was cut short — "the details are in the chat" must be true.
    return not asks_him(text) and IN_CHAT not in speakable(text)


async def set_busy(busy: bool) -> None:
    """Another app has the mic (a Teams or Zoom call) — or no longer does."""
    _STATE["busy"] = bool(busy)
    if not busy and _QUEUE:
        waiting, _QUEUE[:] = list(_QUEUE), []
        head = f"While you were on the call: {len(waiting)} update{'s' if len(waiting) > 1 else ''}. "
        await say(head + " ".join(waiting[:3]), kind="answer")


# --- hearing things -------------------------------------------------------------------

#: "Asta, go offline", "stop listening", "mic off" — said, not typed.
_GO_OFF = re.compile(r"\b(?:go\s+offline|stop\s+listening|mic\s+off|mute\s+(?:yourself|the\s+mic)|"
                     r"that'?s\s+all|bye\s+asta)\b", re.I)
_QUIET = re.compile(r"\b(?:voice\s+off|stop\s+talking|be\s+quiet|shut\s+up)\b", re.I)
#: Fillers, and what Whisper writes for a cough or a silence ("Thank you.", "you").
_NOISE = re.compile(r"^\W*(?:um+|uh+|hmm+|ah+|oh+|thank you|thanks|you|bye)\W*$", re.I)
#: One word that IS an answer — to a draft waiting for "send", or a question.
#: Only words that cannot be mistaken for the room: "Yeah." and "Go go go" were
#: background audio, live on 2 Oct, and each started a brain turn.
_ONE_WORD = re.compile(r"^\W*(?:send|yes|no|approve|approved|stop|cancel|retry|hello|hi|hey)\W*$", re.I)


#: Scripts he speaks: Latin (English, romanised Hindi) and Devanagari.
_HIS_SCRIPTS = re.compile(r"[A-Za-z\u0900-\u097F]")
_MUSIC = re.compile(r"\b(?:music|♪|♫)\b|[♪♫]|موسيقى", re.I)


def foreign(text: str) -> bool:
    """Written in letters he never uses: not plain English, not Devanagari."""
    return any(c.isalpha() and not ("a" <= c.lower() <= "z" or "\u0900" <= c <= "\u097f")
               for c in text or "")


def is_noise(text: str) -> bool:
    """Nothing to act on: a filler, Whisper's silence hallucination, music, a
    script he does not speak, or one stray word that is not an answer.

    2 Oct: a song in the room came through as "موسيقى موسيقى موسيقى موسيقى" —
    Whisper writing "music", in Arabic, four times."""
    t = " ".join((text or "").split())
    if len(t) < 2 or _NOISE.match(t) or _MUSIC.search(t):
        return True
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if _HIS_SCRIPTS.match(c)) < len(letters) * 0.6:
        return True
    words = re.findall(r"\w+", t.lower())
    if len(words) >= 3 and max(words.count(w) for w in words) >= max(3, len(words) * 0.5):
        return True                                 # one word, over and over ("Go Go Go Go Go Thank you.")
    return len(t.split()) < 2 and not _ONE_WORD.match(t) and "asta" not in t.lower()


async def meant_for_asta(text: str, now: float | None = None) -> bool:
    """Was this said to Asta — or to the person next to him?

    His name, or a follow-up within the window: yes. Otherwise a quick one-word
    check; when that cannot be had, no — a missed sentence costs him repeating
    it, a wrong one costs Asta butting into his conversation."""
    now = time.time() if now is None else now
    if named(text):
        return True
    last = max(_STATE["last_spoke"], _STATE["mic_on_at"], _STATE["last_heard"])
    if now - last < FOLLOW_WINDOW_SECONDS:
        return True
    from . import memory
    verdict = await memory.quick_verdict(
        "Arun has his assistant's microphone open. He just said, out loud:\n\n"
        f"  {text[:300]}\n\n"
        "Is this said TO the assistant (an instruction or question for it), or to "
        "someone else in the room / on the phone? Answer with one word: ASSISTANT or OTHER.",
        8, timeout=12)
    return (verdict or "").strip().upper().startswith("ASSISTANT")


#: How sure the Mac's own recognizer must be for its words to be used as they
#: are. Below it (Hindi through an English recognizer comes back as confident-
#: sounding English with low scores) Whisper hears the clip instead.
EARS_CONFIDENCE = 0.4
#: The Mac's words are trusted at a lower bar when they hold her name.
NAME_CONFIDENCE = 0.25


def mac_words(said: str, confidence: float) -> str:
    """The Mac's transcription when it can be trusted, else ''."""
    said = " ".join((said or "").split())
    if said and named(said) and not foreign(said) and confidence >= NAME_CONFIDENCE:
        # Her name, as the Mac heard it, beats Whisper's guess. 2 Oct 17:46: the
        # Mac had "Aastha" / "Hey Aastha" right (33-52% sure) and Whisper made
        # them "y hasta y hasta", "Yeah, stop.", "He hasta" — three calls ignored.
        return said
    if not said or foreign(said) or confidence < EARS_CONFIDENCE:
        return ""
    if len(_tokens(said)) < 3:
        # Short is where it fails while sure of itself: "Hello Asta", said again
        # and again on 2 Oct, came back as "Hello" at 98-100% — the name gone,
        # and a lone "Hello" thrown away. Whisper hears short clips too.
        return ""
    return said


async def heard(wav: bytes, dry: bool = False, said: str = "", confidence: float = 0.0,
                asta: bool | None = None) -> dict:
    """One utterance from the helper: transcribe, decide, act. Returns what happened.

    `said` is the helper's own on-device transcription, made while he talked —
    used when it is sure, so the 1.4 s Whisper pass is skipped."""
    from . import voice
    _TURN["pending"] += 1
    try:
        text = mac_words(said, confidence)
        if text:
            store.record_outcome("voice", "ears", detail=f"mac {confidence:.2f}: {text[:80]}")
        else:
            text = (await voice.transcribe(wav, filename="speech.wav")).strip()
            if foreign(text):
                # Whisper guessed a language he does not speak — "Hey Asta, are you
                # there?" came back as "Hérsta er þú der." (Icelandic), 2 Oct. Short
                # clips are where its guess goes wrong; English is what he means.
                text = (await voice.transcribe(wav, filename="speech.wav", language="en")).strip()
            if not text and said.strip():
                text = said.strip()             # Whisper heard nothing; the Mac did
            elif said.strip() and named(said) and not named(text) and not foreign(said):
                # The Mac heard her name and Whisper did not — the Mac wins, at any
                # confidence: "Yeah, Astha" (0.00) became "Yeah, stop." (19:33).
                text = said.strip()
            if said:
                store.record_outcome("voice", "ears", detail=f"whisper (mac {confidence:.2f}: {said[:60]})")
    except Exception as exc:                                    # noqa: BLE001
        store.record_outcome("voice", "stt_failed", detail=str(exc)[:200])
        await _to_helper({"type": "error", "text": "I could not hear that — speech-to-text is down."})
        return {"text": "", "did": "stt_failed"}
    finally:
        _TURN["pending"] = max(0, _TURN["pending"] - 1)
    if dry:
        return {"text": text, "did": "dry"}
    barge = time.time() - _STATE["barge_heard"] < 20
    # The helper knows whether Asta was audible while this was recorded. If it
    # was not, this cannot be Asta's echo, however much it sounds like Asta's
    # words: "Can you explain the booking service?", said in silence, was
    # dropped as echo of an earlier answer (2 Oct 17:55).
    mine = text if asta is False else echo(text, while_speaking=barge)
    if barge and mine.strip() and is_noise(mine):
        # A cough or the room over Asta comes back as Whisper's "Thank you." —
        # 2 Oct 17:47 it stopped an answer mid-sentence. Not him: carry on.
        mine = ""
    if not mine.strip():
        store.record_outcome("voice", "echo", detail=text[:200])
        if barge:
            _STATE["barge_heard"] = 0.0
            await _to_helper({"type": "unduck"})        # it was Asta: carry on as you were
        return {"text": text, "did": "echo"}
    if barge:
        # Confirmed: it is him. What Asta was saying stops, and is in the chat.
        _STATE["barge_heard"] = 0.0
        _STATE["barged_at"] = time.time()
        store.record_outcome("voice", "barge", detail=mine[:120])
        await _to_helper({"type": "hush"})
    return await assemble(mine)


# --- his words, for the Mac's recognizer --------------------------------------------------

#: Words his speech is full of and a general recognizer gets wrong.
_TERMS = ["Asta", "Arun", "Telikos", "Maersk", "booking", "booking ID", "UAT", "pre-prod", "prod",
          "ATA", "ATD", "ETA", "ETD", "RFP", "PR", "Jira", "Temporal", "Grafana", "Loki", "Copilot",
          "Claude", "Teams", "WhatsApp", "activity plan", "service plan", "topic refresh", "CI",
          "merge", "rebase", "Kafka", "contract test", "facility city code", "Vault", "workflow",
          "debug", "debugging", "shared a booking", "consumed", "review comments", "investigate",
          # His domain, which the recognizers turned into "Evans API", "I am service",
          # "absent system" (2 Oct 19:40) — and Asta called his explanation unclear.
          "IOM", "service plan", "service plan event", "Solar", "TMS", "SAP TMS", "FACT", "NFTP", "CMD",
          "CCD", "mEPC", "Athena Lite", "VTS", "IDOC", "FinOps", "SendGrid", "Temporal",
          "send to execution", "booking confirmed", "ready for planning", "ready for invoicing",
          "transport order", "activity plan service", "email service", "booking service"]


def vocabulary(limit: int = 100) -> list[str]:
    """The people he talks to most and the words of his work — for the Mac's
    recognizer, which takes up to about a hundred such phrases."""
    words: list[str] = list(_TERMS)
    with contextlib.suppress(Exception):
        from . import prname
        for alias in prname.aliases().values():
            words += [alias, f"{alias} PR"]
    with contextlib.suppress(Exception):
        for row in store.teams_senders_known(40):
            name = row["sender"]
            words.append(name)
            first = name.split()[0]
            if len(first) > 2:
                words.append(first)
    with contextlib.suppress(Exception):
        from . import senior
        words += senior.people()
    seen: set[str] = set()
    out = [w for w in words if w and not (w.lower() in seen or seen.add(w.lower()))]
    return out[:limit]


# --- a second ear, for calls ------------------------------------------------------------

_EARS: dict[int, asyncio.Future] = {}


async def second_ear(wav: bytes, timeout: float = 4.0) -> str:
    """The Mac's recognizer on a clip — for a call turn Whisper heard nothing in
    (1 Oct, Vinish: two of his sentences came back empty). '' without a helper."""
    if _STATE["helper"] is None or not wav:
        return ""
    rid = max(_EARS, default=0) + 1
    fut = asyncio.get_event_loop().create_future()
    _EARS[rid] = fut
    try:
        if not await _to_helper({"type": "transcribe", "id": rid,
                                 "wav": base64.b64encode(wav).decode()}):
            return ""
        text, confidence = await asyncio.wait_for(fut, timeout)
        return text if confidence >= EARS_CONFIDENCE or (text and confidence == 0) else ""
    except Exception:                                          # noqa: BLE001
        return ""
    finally:
        _EARS.pop(rid, None)


def transcript(msg: dict) -> None:
    """The helper's answer to `second_ear`."""
    fut = _EARS.get(int(msg.get("id") or 0))
    if fut is not None and not fut.done():
        fut.set_result((" ".join(str(msg.get("text") or "").split()), float(msg.get("confidence") or 0)))


#: The words a sentence that is not finished ends on.
_TRAILING = re.compile(
    r"(?:\b(?:and|or|but|so|like|a|an|the|to|of|for|on|in|at|with|that|which|who|is|are|was|"
    r"i|we|my|your|this|about|because|if|then|also|um+|uh+)|[,\-–—…:]|\.\.\.)\W*$", re.I)


def unfinished(text: str) -> bool:
    """Reads as cut off: no closing mark, or it ends on "and", "like a", a comma."""
    t = (text or "").strip()
    if re.search(r"[?!]$", t):
        return False                    # "what are you working on?" is finished
    return not re.search(r"[.।]$", t) or bool(_TRAILING.search(t))


async def assemble(text: str) -> dict:
    """His pieces joined into one turn, decided once he has finished.

    The helper ends a piece at a short pause; people pause mid-sentence. So a
    piece waits a moment for the next — longer when it reads unfinished, for as
    long as he is still talking, and while another piece is being transcribed —
    and the turn is decided once, whole."""
    if is_noise(text):
        return {"text": text, "did": "ignored"}     # a waiting turn goes on waiting
    if not _TURN["parts"]:
        _TURN["first_at"] = time.time()
    _TURN["parts"].append(text)
    _TURN["gen"] += 1
    gen = _TURN["gen"]
    calling = len(_tokens(text)) <= 4 and (named(text) or _ONE_WORD.match(text))
    hold = 0.0 if calling else HOLD_UNFINISHED_SECONDS if unfinished(text) else HOLD_SECONDS
    arrived = time.time()
    deadline = arrived + hold
    while True:
        await asyncio.sleep(0.05)
        if _TURN["gen"] != gen:
            return {"text": text, "did": "joined"}  # more came: the newest piece decides
        if time.time() - _TURN["first_at"] > TURN_MAX_SECONDS or time.time() - arrived > HOLD_MAX_SECONDS:
            break
        if time.time() < deadline or (not calling and (_TURN["speaking"] or _TURN["pending"])):
            continue
        break
    whole = " ".join(_TURN["parts"])
    _TURN["parts"] = []
    return await handle(whole)


def remember(who: str, words: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if who == "Arun":
        _STATE["his_turn_at"] = now
    _HEARD.append((now, who, " ".join((words or "").split())[:300]))
    del _HEARD[:-12]


def recent(now: float | None = None, before: float | None = None) -> list[str]:
    """The conversation of the last few minutes, as "Arun: ..." / "Asta: ..." lines."""
    now = time.time() if now is None else now
    return [f"{who}: {words}" for at, who, words in _HEARD
            if now - at < CONVERSATION_SECONDS and (before is None or at < before)]


#: Asta's name as the recognizers write it: "Aastha", "Asta", "Astha", "Aasta".
#: Live, 2 Oct 17:46: "Aastha", "Sastha", "He hasta", "y hasta", "Hasta" — the
#: recognizers' spellings of her name.
_NAME = re.compile(r"\b(?:s?h?a+s+t+h?a+o?|ashta|asthaa?)\b", re.I)


def named(text: str) -> bool:
    return bool(_NAME.search(text or ""))


def in_conversation(now: float | None = None) -> bool:
    """He and Asta are mid-exchange: Asta spoke, or he called it, lately."""
    now = time.time() if now is None else now
    return now - max(_STATE["last_spoke"], _STATE["last_heard"]) < FOLLOW_WINDOW_SECONDS


async def handle(text: str) -> dict:
    """What he said, already as text."""
    if not listening() or is_noise(text):
        return {"text": text, "did": "ignored"}
    command = len(_tokens(text)) <= 7     # a switch is said on its own, not inside a long sentence
    if command and _GO_OFF.search(text):
        await set_mode(mic=False, why="he said so")
        await say("Okay, mic off.", kind="answer")
        return {"text": text, "did": "mic_off"}
    if command and _QUIET.search(text):
        await say("Okay, going quiet.", kind="answer")
        await set_mode(speaker=False, why="he said so")
        return {"text": text, "did": "speaker_off"}
    return await converse(text)


async def converse(text: str) -> dict:
    """The talker answers, stays quiet, or hands the work on — in about a second.

    Falls back to the slow path (addressee check, then a full turn) only when the
    talker cannot be had at all."""
    from . import voice_talker
    if await answer_draft(text):
        remember("Arun", text)
        return {"text": text, "did": "answered_draft"}
    started = time.time()
    context = recent(started)
    decided = await voice_talker.route(text, context)
    second_look = False
    if decided == voice_talker.QUIET and (named(text) or (in_conversation(started) and "?" in text)):
        # Called by name, or a question mid-exchange: it was for Asta. 2 Oct:
        # "Hello Aastha, are you responding?" was taken for room talk.
        decided = voice_talker.ANSWER
    elif decided == voice_talker.QUIET and in_conversation(started) and len(text.split()) >= 14:
        # Mid-exchange and a whole explanation: that is him talking TO Asta —
        # "the trigger once, then we process each milestone one by one… very
        # lacking" got silence (19:39).
        decided = voice_talker.ANSWER
    elif decided == voice_talker.QUIET and in_conversation(started) and len(text.split()) >= 5:
        # Mid-exchange and more than "okay, fine" — "I haven't received anything
        # in the chat" was silenced. The talker, which sees the conversation,
        # takes a second look and may still stay quiet ("I'll send you the deck").
        decided, second_look = None, True
    correcting = in_conversation(started) and bool(_CORRECTION.search(text))
    if decided == voice_talker.DO and correcting:
        # Correcting what Asta just said is not work: "Email only in booking
        # confirmation and execution… not on it, you have to correct it" got
        # "On it." twice and two jobs (19:37). The talker takes the correction.
        decided = voice_talker.ANSWER
    if decided == voice_talker.DO and len(_tokens(text)) < 4:
        # "Then also add" is not a job — it is half a thought. Asta asks.
        decided = voice_talker.ANSWER
    if decided is not None or second_look:
        store.record_outcome("voice", "routed", detail=f"{decided or '[SECOND LOOK]'} "
                                                      f"{time.time() - started:.1f}s · {text[:100]}")
    if decided == voice_talker.QUIET:
        store.record_outcome("voice", "not_for_asta", detail=text[:200])
        return {"text": text, "did": "not_for_asta"}
    remember("Arun", text, started)
    # He has moved on: lines still queued from earlier answers are dropped, so
    # this one is answered now — 2 Oct 19:4x, "it's still telling the old convo
    # and not acking the new message".
    await _to_helper({"type": "flush"})
    if decided in (voice_talker.ANSWER, voice_talker.DO, None):
        if await answer_question(text):
            return {"text": text, "did": "answered_question"}
        asked = asking_job(started) if not knows_about(text) else None
        if asked is not None:
            # The job asked him something; this is his reply, in that job's own
            # conversation — not a fresh job that knows nothing (2 Oct, #9).
            asked["followed"] = True
            _STATE["last_heard"] = time.time()
            await say("Got it.", kind="answer")
            asyncio.ensure_future(_work(text, context, cid=asked["cid"]))
            return {"text": text, "did": "followed_up", "job": asked["id"]}
    before = answered_before(text, started) if decided in (voice_talker.DO, voice_talker.ANSWER) else None
    if before is not None:
        _STATE["last_heard"] = time.time()
        store.record_outcome("voice", "said_again", detail=f"#{before['id']} {text[:100]}")
        await say_lines(before["result"])
        return {"text": text, "did": "said_again", "job": before["id"]}
    same = duplicate_of(text) if decided != voice_talker.LISTEN else None
    if same is not None:
        # Asked again while it runs: no second job and no brain — at once.
        _STATE["last_heard"] = time.time()
        await say("Still on that — I'll tell you.", kind="answer")
        return {"text": text, "did": "already_on_it", "job": same["id"]}
    kb = ""
    if decided == voice_talker.ANSWER:
        # What Asta's own records answer, instantly, comes first: its jobs, his
        # PRs, what is pending.
        with contextlib.suppress(Exception):
            from . import frontdesk
            known = jobs_answer(text) or pr_answer(text) or frontdesk.answer_from_state(text)
            if known:
                _STATE["last_heard"] = time.time()
                await say_lines(known)
                return {"text": text, "did": "answered_from_state"}
    if decided in (voice_talker.ANSWER, voice_talker.DO) and knows_about(text):
        # "What is Telikos Inland Booking?" — his documents and the repo
        # summaries answer it in seconds; a job took 20-60 s and, with no
        # workspace, found nothing (2 Oct).
        with contextlib.suppress(Exception):
            from . import project_knowledge
            kb = project_knowledge.lookup(text, budget=2000, channel="voice")
        if kb:
            decided = voice_talker.ANSWER
            store.record_outcome("voice", "knowledge", detail=f"{len(kb)} chars · {text[:100]}")
    if decided == voice_talker.ANSWER and not kb:
        from . import booking_case
        if (booking_case.points_at_one(text) or booking_case.spoken_ids(text)) \
                and booking_case.asks(text):
            # About one booking: the worker checks its logs (or asks which
            # booking, which environment) — the talker would answer from the
            # documents as if they knew this booking (2 Oct).
            decided = voice_talker.DO
    if decided == voice_talker.LISTEN:
        _STATE["last_heard"] = time.time()
        await say("I'm listening.", kind="answer")
        return {"text": text, "did": "listening"}
    if decided == voice_talker.DO:
        _STATE["last_heard"] = time.time()
        await say("On it.", kind="answer")
        asyncio.ensure_future(_work(text, context))
        return {"text": text, "did": "handed_on"}
    spoken = 0
    handed = False
    filler = asyncio.ensure_future(_filler(started))
    try:
        # Routed as a question for Asta: the talker answers it, never silence.
        kind = "to_you" if decided == voice_talker.ANSWER else "said"
        async for line in voice_talker.sentences(text, kind, **({"context": kb} if kb else {})):
            filler.cancel()
            if line == voice_talker.QUIET:
                store.record_outcome("voice", "not_for_asta", detail=text[:200])
                return {"text": text, "did": "not_for_asta"}
            if line == voice_talker.DO:
                handed = True
                continue
            if _STATE.get("filler_at", 0) > started and _JUST_ACK.match(line):
                continue        # "One moment." then "Let me look." — said once (2 Oct 17:49)
            if line.lower().startswith("still on that") and not running():
                # Nothing is running: "still on that" is a promise about no work
                # at all (2 Oct 17:50, asked to read the project knowledge). It
                # becomes the work.
                handed = True
                await say("On it.", kind="answer")
                spoken += 1
                continue
            if _STATE["barged_at"] > started:
                break
            if spoken == 0:
                store.record_outcome("voice", "first_words", detail=f"{time.time() - started:.1f}s")
            await say(line, kind="answer")
            spoken += 1
    except Exception as exc:                                    # noqa: BLE001
        filler.cancel()
        if spoken or handed:
            store.record_outcome("voice", "talker_broke", detail=str(exc)[:200])
        else:
            if decided is None and not second_look and not await meant_for_asta(text):
                store.record_outcome("voice", "not_for_asta", detail=text[:200])
                return {"text": text, "did": "not_for_asta"}
            _STATE["last_heard"] = time.time()
            reply = await turn(text)
            return {"text": text, "did": "answered", "reply": reply}
    finally:
        filler.cancel()
    _STATE["last_heard"] = time.time()
    store.record_outcome("voice", "heard", detail=text[:200])
    if correcting and not handed:
        # He corrected a domain fact: kept, so it is right from now on — on
        # every channel, not only in this conversation.
        with contextlib.suppress(Exception):
            from . import project_knowledge
            restated = " ".join(w for at, w in _SPOKEN if at >= started)[:300]
            if project_knowledge.learn(text, restated, where="voice"):
                store.record_outcome("voice", "learned", detail=text[:160])
    if handed:
        same = duplicate_of(text)
        if same is not None:
            # The backstop under the talker's own briefing: asked again is not
            # a second job (2 Oct: one task said three times, answered three
            # times, ten minutes apart).
            if spoken == 0:
                await say("Still on that — I'll tell you.", kind="answer")
            return {"text": text, "did": "already_on_it", "job": same["id"]}
        asyncio.ensure_future(_work(text, context))
        return {"text": text, "did": "handed_on"}
    return {"text": text, "did": "answered"}


#: Work, not a question about how things are: a booking id, "check", "debug"...
_WORK_WORDS = re.compile(r"\b(?:check|debug|logs?|send|message|ping|fix|investigate|review|merge|deploy|"
                         r"draft|remind|schedule|call|book\s+a|create)\b|(?-i:\b(?=[A-Z0-9]*\d)[A-Z0-9]{8,}\b)", re.I)


def knows_about(text: str) -> bool:
    """A question about what something is or how it works — the knowledge
    answers it — rather than work to do."""
    from . import booking_case, project_knowledge
    # "…for this booking" is about ONE booking: the logs answer it, not the
    # documents — it goes to the worker, which finds the booking or asks (2 Oct).
    return project_knowledge.is_knowledge_question(text) and not _WORK_WORDS.search(text or "") \
        and not booking_case.points_at_one(text)


async def _filler(started: float) -> None:
    """One "One moment." when the answer is slow to start — and only then."""
    await asyncio.sleep(FILLER_SECONDS)
    if _STATE["barged_at"] < started:
        _STATE["filler_at"] = time.time()
        await say(ACKS["moment"], kind="answer")


#: He is correcting what Asta said.
_CORRECTION = re.compile(r"\b(?:you have to correct|correct (?:it|that|yourself)|that'?s (?:wrong|not right|"
                         r"incorrect)|not correct|it'?s wrong|you(?:'re| are) wrong|not on it|"
                         r"only (?:in|on|for|when)|no,? (?:it|that)(?:'s| is))\b", re.I)

#: A line that only acknowledges: after "One moment." it is a second filler.
_JUST_ACK = re.compile(r"^\W*(?:on it|let me (?:look|check|see)|checking(?: it)? now|got it|sure|"
                       r"one (?:moment|sec))\W*$", re.I)


#: "What are you working on?", "any update?", "which booking did you check?"
_STATUS = re.compile(
    r"\b(?:what(?:'s| is| are)?\s+(?:you|u)\s+(?:working|doing|checking|up\s+to|on)|any\s+updates?|"
    r"(?:the|an?)\s+update|status\s+of\s+(?:it|that|the\s+task)|how\s+far|how\s+long\s+(?:it|will|does|is)|"
    r"did\s+(?:you|u)\s+(?:check|find|send|do|ask)|which\s+(?:\w+\s+)?(?:are\s+|did\s+)?(?:you|u)\s+"
    r"(?:are\s+)?(?:taking|checking|working|looking|check))\b", re.I)
#: A question about progress itself, which "nothing is running" answers truly.
_PROGRESS = re.compile(r"\bhow\s+(?:long|far)\b|\bany\s+updates?\b|"
                       r"\bwhat(?:'s| is| are)?\s+(?:you|u)\s+(?:working|doing)\b", re.I)
_ASK_LEAD = re.compile(r"^(?:(?:hey|hi|hello|ok(?:ay)?|so)[,\s]+)*(?:asta[,\s]+)?"
                       r"(?:(?:can|could|would)\s+(?:you|u)\s+(?:please\s+)?|i\s+want\s+(?:you\s+)?to\s+)"
                       r"|^please\s+", re.I)


def _gist(text: str) -> str:
    """His request, short enough to say back: "ask Vinish when he will be free"."""
    t = _ASK_LEAD.sub("", " ".join((text or "").split())).rstrip(" .?!")
    return t if len(t) <= 70 else t[:70].rsplit(" ", 1)[0]


def jobs_answer(text: str, now: float | None = None) -> str:
    """"What are you working on?" — answered from Asta's own jobs, at once.

    The worker was asked it once (2 Oct) and, with no idea of the voice jobs,
    said "no active task matches"."""
    if not _STATUS.search(text or ""):
        return ""
    now = time.time() if now is None else now
    live = sorted(running(), key=lambda j: j["started"])
    done = sorted((j for j in _JOBS.values() if j["done_at"] and now - j["done_at"] < 900),
                  key=lambda j: j["done_at"])
    about = [j for j in live + done if _about(text, j["text"])]
    if about:
        j = max(about, key=lambda j: j["done_at"] or j["started"])
        if not j["done_at"]:
            return f"Still on it — {_gist(j['text'])}, {int(now - j['started'])} seconds in."
        return speakable(j["result"]) or "That one came back empty."
    if live:
        said = "; and ".join(f"{_gist(j['text'])} — " + (
            f"{int(now - j['started'])} seconds in" if now - j["started"] >= 5 else "just started")
            for j in live[-3:])
        return f"I'm on: {said}. I'll tell you when it's done."
    if done:
        j = done[-1]
        found = speakable(j["result"], sentences=1) or "it came back empty"
        return f"Nothing running. Last one was: {_gist(j['text'])}. {found}"
    # Nothing running and nothing done, asked about progress: say so — never
    # "shouldn't be much longer" about work that does not exist (2 Oct 17:51).
    if _PROGRESS.search(text or ""):
        return "Nothing's running right now — tell me what to pick up."
    return ""


async def answer_question(text: str) -> bool:
    """His spoken reply to a question Asta asked him OUT LOUD — handed to what
    asked it, as his words alone.

    2 Oct: a job's question went to WhatsApp only; his next spoken line, with
    the conversation appended, was filed as the answer to it, and he heard
    "Answered #12 — passed back to chat"."""
    from . import asking
    q = asking.pending_for_reply(text)
    if q is None or not _was_said(q.get("text", ""), since=q.get("created_at", 0)):
        return False
    if not asking.answer(q["id"], text):
        return False
    _STATE["last_heard"] = time.time()
    store.record_outcome("voice", "answered_question", detail=f"#{q['id']} {text[:120]}")
    await say("Got it.", kind="answer")
    return True


def _was_said(text: str, since: float) -> bool:
    """Did Asta say (the start of) this out loud since then?"""
    head = " ".join(_tokens(speakable(text, sentences=1)))[:60]
    return bool(head) and any(at >= since - 1 and head in " ".join(_tokens(w)) for at, w in _SPOKEN)


def _ends_by_asking(result: str) -> bool:
    """The job's answer closes on a question to him — in its last two
    sentences, not only the last character: "…where you saw this term — a
    ticket, email, or chat? I'll look there." asked, and his reply went to a
    fresh job that knew nothing (2 Oct 17:50)."""
    parts, _ = _sentences(result)
    # An offer ("Want me to go deeper?") is not a question waiting on him: his
    # next, new question went into that job and got "Got it." (19:34).
    return any(p.rstrip().endswith("?") and not _OFFER.match(p.strip()) for p in parts[-2:])


_OFFER = re.compile(r"^(?:\W*)(?:want me to|should i|shall i|do you want|would you like|"
                    r"need me to|anything else|let me know)\b", re.I)


def asking_job(now: float | None = None) -> dict | None:
    """A voice job that finished lately by asking him something, not yet answered."""
    now = time.time() if now is None else now
    asked = [j for j in _JOBS.values() if j["done_at"] and now - j["done_at"] < 300 and j["cid"]
             and not j.get("followed") and _ends_by_asking(j["result"] or "")]
    return max(asked, key=lambda j: j["done_at"]) if asked else None


# --- the work behind the talker: parallel, one job per request -----------------------

#: A request this close to one running (or finished this recently) is the same request.
DUPLICATE_WINDOW_SECONDS = 600.0
_JOBS: dict[int, dict] = {}
_STOP = {"the", "a", "an", "to", "and", "for", "of", "in", "on", "is", "it", "please", "can",
         "you", "u", "me", "my", "asta", "check", "also", "now", "again", "once"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9\u0900-\u097f]+", (text or "").lower())
            if w not in _STOP and len(w) > 1}


def same_request(a: str, b: str) -> bool:
    """He asked it again — because nothing came back, or in other words."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.6 or wa <= wb or wb <= wa


#: "Check again", "once more", "re-check": he wants it done afresh, not repeated.
_AFRESH = re.compile(r"\b(?:again|once\s+more|re-?check|re-?run|fresh|latest|now\s+check)\b", re.I)


def answered_before(text: str, now: float | None = None) -> dict | None:
    """The same thing, asked again after it was answered lately — he did not
    hear it. 2 Oct: the Rajendra booking was asked four times; each time a new
    job, an answer cut off, and "still not shared once"."""
    now = time.time() if now is None else now
    if _AFRESH.search(text or "") or len(_words(text)) < 2:
        return None             # "Hello?" is not the question asked again (17:52)
    done = [j for j in _JOBS.values() if j["done_at"] and now - j["done_at"] < DUPLICATE_WINDOW_SECONDS
            and (j["result"] or "").strip() and _about(text, j["text"])
            # Just said: replaying it is how its own echo became a loop (17:56).
            and not _just_said(j["result"], now)]
    return max(done, key=lambda j: j["done_at"]) if done else None


def _just_said(result: str, now: float, within: float = 120.0) -> bool:
    first = " ".join(_tokens(speakable(result, sentences=1)))[:60]
    return bool(first) and any(now - at < within and first in " ".join(_tokens(w)) for at, w in _SPOKEN)


def _about(asked: str, job_text: str) -> bool:
    """Is this question about that job — the same request, or its distinctive
    words (a booking id, a name)?"""
    if same_request(asked, job_text):
        return True
    generic = {"update", "status", "booking", "pr", "what", "any", "done", "investigated",
               "investigate", "yet", "not", "or", "did", "that", "this", "on", "how", "long", "will", "take"}
    generic |= {"know", "do", "you", "get", "give", "tell", "explain", "about", "me", "can", "could",
                "what", "why", "brief", "hello", "hey", "properly", "want", "like"}
    mine = _words(asked) - generic
    shared = mine & (_words(job_text) - generic)
    # Two real words in common, half of what he asked: "do you know how to get a
    # book?" matched a booking job on "know" alone and got its answer (17:57).
    # ...or one distinctive one: a name or an id ("Rajendra", "H69LMCN6KZY").
    distinctive = {w for w in shared if len(w) >= 6 or any(c.isdigit() for c in w)}
    return bool(distinctive) or (len(shared) >= 2 and len(shared) >= len(mine) / 2)


def duplicate_of(text: str, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    for job in sorted(_JOBS.values(), key=lambda j: -j["started"]):
        # Running only: asked again after the answer came is a real re-check.
        if not job["done_at"] and now - job["started"] < DUPLICATE_WINDOW_SECONDS \
                and same_request(text, job["text"]):
            return job
    return None


def running() -> list[dict]:
    return [j for j in _JOBS.values() if not j["done_at"]]


def jobs_line(now: float | None = None) -> str:
    """What the talker is told is in progress, so it never starts it twice."""
    now = time.time() if now is None else now
    live = running()
    done = [j for j in _JOBS.values() if j["done_at"] and now - j["done_at"] < 900]
    out = ""
    if live:
        out = "[Working on now: " + "; ".join(
            f"#{j['id']} {j['text'][:70]} ({int(now - j['started'])}s)" for j in live) + "]"
    if done:
        out += ("\n" if out else "") + "[Just finished: " + "; ".join(
            f"#{j['id']} {j['text'][:60]} → {speakable(j['result'], sentences=1)[:140] or 'nothing'}"
            for j in sorted(done, key=lambda j: j["done_at"])[-3:]) + "]"
    return out


def _job_conversation(text: str) -> dict:
    """Each job its own conversation, so two requests run side by side instead
    of the second waiting behind the first ("I'm working on the previous one")."""
    from . import main
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.update_conversation(conv["id"], title=f"🎙 {text[:50]}")
    phone = store.get_conversation(store.kv_get("wa_conversation") or "") or conv
    with contextlib.suppress(Exception):
        conv["model"] = main._channel_model(phone)
    ws = phone.get("workspace") or ""
    if not ws:
        # The project he is talking about, else his stated default. 2 Oct: every
        # voice job ran with NO workspace — "what is Telikos Inland Booking" had
        # no project knowledge to read, and was asked about four times.
        with contextlib.suppress(Exception):
            from .workspace import registry
            ws = registry.infer(text) or ""
        if not ws:
            with contextlib.suppress(Exception):
                from . import policy
                ws = policy.prefer("workspace") or ""
    if ws:
        conv["workspace"] = ws
        with contextlib.suppress(Exception):
            store.update_conversation(conv["id"], workspace=ws)
    return conv


def with_context(text: str, context: list[str]) -> str:
    """His request, with what was said just before it — the worker starts each
    job fresh, and "the booking Rajendra shared" is only clear with the rest."""
    before = [ln for ln in context if ln != f"Arun: {' '.join(text.split())[:300]}"][-6:]
    from . import booking_case
    spelled = [i for i in booking_case.spoken_ids(text) if i not in booking_case.ids(text)]
    if spelled:
        # "M H 6 5 W 8…" as recognised speech: the id it spells, marked as heard.
        text += f"\n(The id he spelled, as heard — may be misheard: {', '.join(spelled)})"
    if not before:
        return text
    return text + "\n\n(Said out loud to Asta — speech recognition mishears names and terms, so read " \
        "it charitably and never quote misheard words back. Just before, in this conversation:\n" + \
        "\n".join(f"  {ln}" for ln in before) + ")"


async def _work(text: str, context: list[str] | None = None, cid: str = "") -> None:
    """The real work, behind the talker; the outcome is said once when it is done.
    `cid` continues a job's own conversation (his reply to what it asked)."""
    from . import loop, main, voice_talker
    started = time.time()
    job = {"id": max(_JOBS, default=0) + 1, "text": text, "started": started, "done_at": 0.0,
           "cid": "", "result": ""}
    _JOBS[job["id"]] = job
    try:
        conv = (store.get_conversation(cid) if cid else None) or _job_conversation(text)
        job["cid"] = conv["id"]
        sink = _WorkSink()
        handle_ = await main._dispatch(conv, text if cid else with_context(text, context or []), sink, "voice")
        if handle_ is not None:
            await asyncio.wait({handle_}, timeout=TURN_SECONDS)
        reply = sink.text()
        job.update(done_at=time.time(), result=reply)
        store.record_outcome("voice", "work_done", detail=f"#{job['id']} {time.time() - started:.1f}s · {text[:120]}")
        if loop.awaiting(conv["id"]):
            reply += "\n(A draft is waiting for his yes — ask him: send it, or change it?)"
        if not reply:
            return
        if _STATE.get("his_turn_at", 0) > started + 1 and time.time() - started > LATE_SECONDS:
            # He has moved on to something else since he asked: the answer goes to
            # his chat, and he hears one line — not a paragraph about the old topic
            # over the new one.
            await to_chat(reply)
            await say(f"The answer on {_gist(text)} is in your chat.", kind="answer")
            return
        # The worker's own first sentences — it already leads with the answer.
        # A second Claude pass to "summarise" cost 5-20 s on a loaded Mac.
        await say_lines(reply)
    except Exception as exc:                                    # noqa: BLE001
        job["done_at"] = job["done_at"] or time.time()
        from . import quiet
        quiet.note("voice.work", exc)
    finally:
        for jid in [j for j, v in _JOBS.items() if v["done_at"] and time.time() - v["done_at"] > 1800]:
            _JOBS.pop(jid, None)


async def answer_draft(text: str) -> bool:
    """A spoken "send it" / "no" goes to the draft a voice job left waiting.
    True when it did — the talker is not asked."""
    from . import loop, main
    approved, _ = main._affirmation(text)
    declined = bool(main._DECLINE.match(text or ""))
    if not (approved or declined):
        return False
    waiting = [j for j in _JOBS.values() if j["cid"] and loop.awaiting(j["cid"])]
    if not waiting:
        return False
    job = max(waiting, key=lambda j: j["done_at"] or j["started"])
    conv = store.get_conversation(job["cid"])
    if conv is None:
        return False
    sink = _WorkSink()
    await main._dispatch(conv, text, sink, "voice")
    await say(speakable(sink.text()) or ("Sent." if approved else "Dropped it."), kind="answer")
    return True


class VoiceSink:
    """Collects a voice turn's words — and says the first sentence the moment it
    is written, instead of after the whole answer. Nothing goes to WhatsApp."""

    def __init__(self) -> None:
        self.alive = True
        self._parts: list[str] = []
        self.spoken = 0                     # sentences of the answer already said
        self.started = time.time()

    def cut(self) -> bool:
        """He talked over this answer: say no more of it."""
        return _STATE["barged_at"] > self.started

    async def send(self, payload: dict) -> None:
        typ = payload.get("type")
        if typ == "delta":
            self._parts.append(payload.get("text", ""))
            if self.spoken == 0 and not self.cut():
                first = first_sentence("".join(self._parts))
                if first:
                    self.spoken = 1
                    await say(first, kind="answer")
        elif typ == "note":
            self._parts.append("\n" + payload.get("text", "") + "\n")
        elif typ == "error":
            self._parts.append(f"\nSomething went wrong: {payload.get('message', 'error')}\n")

    def text(self) -> str:
        return "".join(self._parts).strip()

    async def close(self) -> None:
        return None


#: "what's pending with my PRs", "which PRs are waiting on Vinish", "my open PRs".
_ABOUT_MY_PRS = re.compile(
    r"\b(?:my|open|pending|all)\s+(?:prs?|pull\s+requests?)\b|\b(?:prs?|pull\s+requests?)\s+(?:status|pending)\b|"
    r"pull\s+request\s+का", re.I)
#: A question about something specific — a booking, a person's chat, one PR —
#: is not "my PRs". 2 Oct 13:49: "check that Rajendra booking… pre-prod" got
#: "You have 15 open PRs".
_SPECIFIC = re.compile(r"\bbooking|(?-i:\b(?=[A-Z0-9]*\d)[A-Z0-9]{8,}\b)|\bPR\s*#?\d+|\bdebug|\bcheck\b", re.I)


def pr_answer(text: str) -> str:
    """His PRs, said from what Asta already holds — no model, no wait.

    The talker once sent "what's pending with my PRs?" off as work and told the
    next two questions "still on that" (bench, 2 Oct)."""
    if not _ABOUT_MY_PRS.search(text or "") or _SPECIFIC.search(text or "") or len((text or "").split()) > 16:
        return ""
    from . import prname, reminders
    lines = [ln.lstrip("• ").strip() for ln in prname.his_open_prs().splitlines() if ln.strip()]
    if not lines:
        return ""
    def short(ln: str) -> str:
        name, _, what = ln.partition(" — ")
        return f"{name} ({' '.join(what.split()[:5])})" if what else name
    count = f"You have {len(lines)} open PRs."
    recent = f"Most recent: {', '.join(short(l) for l in lines[:3])}."
    about: list[str] = []
    who = [w for w in re.findall(r"[A-Za-z]{3,}", text or "")
           if w.lower() not in {"which", "what", "waiting", "pending", "prs", "pull", "requests", "asta",
                                "are", "with", "the", "for", "status", "open", "mine"}]
    for r in store.list_reminders():
        if r["text"].startswith(reminders.SEND_PREFIX):
            send = reminders._send_of(r)
            if any(w.lower() in (send.get("to") or "").lower() for w in who):
                import datetime as _dt
                when = _dt.datetime.fromtimestamp(r["due_at"]).strftime("%A %H:%M")
                about.append(f"Your message to {send['to'].split()[0]} about them goes out {when}.")
    # Asked about someone: that part first — the two-sentence limit cut it off.
    return " ".join(about[:1] + [count, recent]) if about else f"{count} {recent}"


class _WorkSink(VoiceSink):
    """The worker's words, collected and never spoken raw: the talker says the outcome."""

    async def send(self, payload: dict) -> None:
        typ = payload.get("type")
        if typ == "delta":
            self._parts.append(payload.get("text", ""))
        elif typ == "note":
            self._parts.append("\n" + payload.get("text", "") + "\n")
        elif typ == "error":
            self._parts.append(f"\nSomething went wrong: {payload.get('message', 'error')}\n")


def _kind_of(text: str) -> str:
    t = (text or "").strip().lower()
    if t.endswith("?") or re.match(r"^(?:asta,?\s+)?(?:what|why|how|when|where|who|which|is|are|"
                                   r"was|were|do|does|did|can|could|any|has|have)\b", t):
        return "question"
    return "do"


async def turn(text: str) -> str:
    """Run what he said through the same pipeline as WhatsApp, and say the answer.

    He hears something within about a second — the acknowledgement, or the
    answer's first sentence the moment the brain writes it — and the rest when
    it is done. Whatever he says meanwhile is its own turn: a comment on work in
    progress is folded into it by the dispatcher, as it is on WhatsApp."""
    from . import main
    cid = store.kv_get("wa_conversation") or ""
    conv = store.get_conversation(cid) if cid else None
    if conv is None:
        conv = store.create_conversation(model="claude_cli", workspace=None)
        store.kv_set("wa_conversation", conv["id"])
    # The brain his phone conversation uses; if that cannot be worked out, the
    # conversation's own — never no answer.
    with contextlib.suppress(Exception):
        conv["model"] = main._channel_model(conv)
    sink = VoiceSink()
    started = time.time()
    job = await main._dispatch(conv, text, sink, "voice")
    if job is not None:
        done, _ = await asyncio.wait({job}, timeout=ACK_SECONDS)
        if not done and sink.spoken == 0 and not sink.cut():
            await say(ACKS[_kind_of(text)], kind="answer")
        if not done:
            # No timed "still on it": he called that nagging (2 Oct). He heard
            # the acknowledgement; the answer comes when it comes.
            await asyncio.wait({job}, timeout=TURN_SECONDS)
    reply = sink.text()
    rest = "" if sink.cut() else speakable(reply, skip=sink.spoken)
    if rest:
        await say(rest, kind="answer")
    store.record_outcome("voice", "turn", detail=f"{time.time() - started:.1f}s · {text[:120]}")
    return reply


async def idle_loop() -> None:
    """Close the mic after MIC_IDLE_SECONDS without a word for Asta."""
    while True:
        await asyncio.sleep(30)
        if _STATE["mic"] and time.time() - _STATE["last_heard"] > MIC_IDLE_SECONDS:
            await set_mode(mic=False, why="5 minutes quiet")
            from . import notify
            with contextlib.suppress(Exception):
                await notify.notify("🎙 Mic off — 5 minutes without a word for me. ⌃⌥M to talk again.",
                                    "voice", urgency="direct", considered=True)


async def _heard_quietly(wav: bytes, said: str = "", confidence: float = 0.0,
                         asta: bool | None = None) -> None:
    try:
        await heard(wav, said=said, confidence=confidence, asta=asta)
    except Exception as exc:                                    # noqa: BLE001
        from . import quiet
        quiet.note("voice.heard", exc)


# --- the helper's connection ----------------------------------------------------------

async def serve(ws) -> None:
    """One menu-bar helper, talking over a local websocket."""
    old = _STATE["helper"]
    _STATE["helper"] = ws
    if old is not None and old is not ws:
        with contextlib.suppress(Exception):
            await old.close()
    store.record_outcome("voice", "helper", detail="connected")
    await _to_helper({"type": "state", **state(), "why": "connected"})
    await _to_helper({"type": "vocab", "words": vocabulary()})
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "toggle":
                await toggle("mic" if msg.get("what") == "mic" else "speaker", why="hotkey")
            elif kind == "speaking":
                # He started (or a too-short sound ended): a turn being held
                # waits while he talks.
                _TURN["speaking"] = bool(msg.get("value"))
            elif kind == "utterance":
                _TURN["speaking"] = False
                wav = base64.b64decode(msg.get("wav") or "")
                said = str(msg.get("text") or "")
                confidence = float(msg.get("confidence") or 0)
                if msg.get("dry"):
                    result = await heard(wav, dry=True, said=said, confidence=confidence)
                    await _to_helper({"type": "heard", **result})
                else:
                    # In the background: a turn can take minutes, and the hotkeys
                    # must keep working while it does.
                    asta = msg.get("asta")
                    asyncio.ensure_future(_heard_quietly(
                        wav, said, confidence, asta if isinstance(asta, bool) else None))
            elif kind == "transcript":
                transcript(msg)
            elif kind == "barge":
                # Sound while Asta talks. The helper has turned Asta DOWN, not
                # off: it may be Asta's own voice coming back. What he says is
                # checked when it arrives — him: "hush"; Asta's echo: "unduck".
                _STATE["barge_heard"] = time.time()
                _TURN["speaking"] = True
            elif kind == "busy":
                await set_busy(bool(msg.get("value")))
            elif kind == "locked":
                if _STATE["mic"]:
                    await set_mode(mic=False, why="screen locked")
            elif kind == "hello":
                await _to_helper({"type": "state", **state(), "why": "hello"})
    except Exception:                                          # noqa: BLE001
        pass
    finally:
        if _STATE["helper"] is ws:
            _STATE["helper"] = None
            store.record_outcome("voice", "helper", detail="disconnected")
