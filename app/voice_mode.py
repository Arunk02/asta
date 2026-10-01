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
ACKS = {"question": "Let me check.", "do": "On it.", "long": "Still on it."}
#: A turn still working after this says "Still on it" once.
STILL_SECONDS = 25.0
#: How long a voice turn may run before its answer goes to the chat instead.
TURN_SECONDS = 600.0
#: The levels of `notify` that are worth saying out loud, when they are addressed to him.
SPOKEN_LEVELS = {"task", "ci", "answer", "calls", "reminder", "send", "offer", "plan", "voice"}
#: Sentences spoken of one answer; the rest is in the chat.
SPOKEN_SENTENCES = 2
#: Updates waiting while another app has the mic (he is on a call).
QUEUE_MAX = 10

_STATE: dict = {"speaker": False, "mic": False, "busy": False, "mic_on_at": 0.0,
                "last_heard": 0.0, "last_spoke": 0.0, "barged_at": 0.0, "helper": None}
_QUEUE: list[str] = []
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
    store.kv_set(_KV, json.dumps({"speaker": _STATE["speaker"], "mic": _STATE["mic"], "at": now}))
    # Warm what is about to be used. The first transcription loads the model —
    # 9.8 s measured, then 1.4 s — and his first sentence must not be the one
    # that pays for it.
    with contextlib.suppress(Exception):
        from . import voice
        if mic:
            asyncio.ensure_future(voice.warm_the_ears())
        if speaker:
            asyncio.ensure_future(voice.warm_the_voice())
            asyncio.ensure_future(warm_acks())
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
        said = (said + " The details are in the chat.").strip()
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


async def say(text: str, kind: str = "answer") -> bool:
    """Speak it, if Asta's voice is on. False when it could not be said.

    The Asta voice from Voicebox when it is up; the helper falls back to the
    Mac's own voice with the text alone, so a missing TTS never means silence."""
    if not on():
        return False
    words = speakable(text)
    if not words:
        return False
    if _STATE["busy"] and kind == "update":
        _QUEUE.append(words)
        del _QUEUE[:-QUEUE_MAX]
        return True
    audio = _CACHE.get(words, "")
    if not audio:
        with contextlib.suppress(Exception):
            from . import voice
            audio = base64.b64encode(await voice.speak(words, voice="assistant")).decode()
    sent = await _to_helper({"type": "say", "text": words, "audio": audio,
                             "chime": kind == "update"})
    if sent:
        _STATE["last_spoke"] = time.time()
    return sent


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
    return listening() or not asks_him(text)


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
_ONE_WORD = re.compile(r"^\W*(?:send|yes|yeah|yep|no|nope|approve|approved|ok|okay|stop|cancel|"
                       r"done|sure|go|skip|drop|retry)\W*$", re.I)


def is_noise(text: str) -> bool:
    """Nothing to act on: a filler, Whisper's silence hallucination, or one stray
    word that is not an answer."""
    t = " ".join((text or "").split())
    if len(t) < 2 or _NOISE.match(t):
        return True
    return len(t.split()) < 2 and not _ONE_WORD.match(t) and "asta" not in t.lower()


async def meant_for_asta(text: str, now: float | None = None) -> bool:
    """Was this said to Asta — or to the person next to him?

    His name, or a follow-up within the window: yes. Otherwise a quick one-word
    check; when that cannot be had, no — a missed sentence costs him repeating
    it, a wrong one costs Asta butting into his conversation."""
    now = time.time() if now is None else now
    if re.search(r"\basta\b", text or "", re.I):
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


async def heard(wav: bytes, dry: bool = False) -> dict:
    """One utterance from the helper: transcribe, decide, act. Returns what happened."""
    from . import voice
    try:
        text = (await voice.transcribe(wav, filename="speech.wav")).strip()
    except Exception as exc:                                    # noqa: BLE001
        store.record_outcome("voice", "stt_failed", detail=str(exc)[:200])
        await _to_helper({"type": "error", "text": "I could not hear that — speech-to-text is down."})
        return {"text": "", "did": "stt_failed"}
    if dry:
        return {"text": text, "did": "dry"}
    return await handle(text)


async def handle(text: str) -> dict:
    """What he said, already as text."""
    if not listening() or is_noise(text):
        return {"text": text, "did": "ignored"}
    if _GO_OFF.search(text):
        await set_mode(mic=False, why="he said so")
        await say("Okay, mic off.", kind="answer")
        return {"text": text, "did": "mic_off"}
    if _QUIET.search(text):
        await say("Okay, going quiet.", kind="answer")
        await set_mode(speaker=False, why="he said so")
        return {"text": text, "did": "speaker_off"}
    if not await meant_for_asta(text):
        store.record_outcome("voice", "not_for_asta", detail=text[:200])
        return {"text": text, "did": "not_for_asta"}
    _STATE["last_heard"] = time.time()
    store.record_outcome("voice", "heard", detail=text[:200])
    reply = await turn(text)
    return {"text": text, "did": "answered", "reply": reply}


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
    conv["model"] = main._channel_model(conv)
    sink = VoiceSink()
    started = time.time()
    job = await main._dispatch(conv, text, sink, "voice")
    if job is not None:
        done, _ = await asyncio.wait({job}, timeout=ACK_SECONDS)
        if not done and sink.spoken == 0 and not sink.cut():
            await say(ACKS[_kind_of(text)], kind="answer")
        if not done:
            done, _ = await asyncio.wait({job}, timeout=max(0.0, STILL_SECONDS - (time.time() - started)))
        if not done and sink.spoken == 0 and not sink.cut():
            await say(ACKS["long"], kind="answer")
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


async def _heard_quietly(wav: bytes) -> None:
    try:
        await heard(wav)
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
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "toggle":
                await toggle("mic" if msg.get("what") == "mic" else "speaker", why="hotkey")
            elif kind == "utterance":
                wav = base64.b64decode(msg.get("wav") or "")
                if msg.get("dry"):
                    result = await heard(wav, dry=True)
                    await _to_helper({"type": "heard", **result})
                else:
                    # In the background: a turn can take minutes, and the hotkeys
                    # must keep working while it does.
                    asyncio.ensure_future(_heard_quietly(wav))
            elif kind == "barge":
                # He talked over Asta: what it was saying is dropped — it is in
                # the chat — and what he says next is the turn.
                _STATE["barged_at"] = time.time()
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
