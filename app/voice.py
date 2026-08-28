"""Voice in and out, through a locally-run Voicebox (github.com/jamiepine/voicebox).

Deliberately plain HTTP rather than Voicebox's MCP server. Speaking is output
plumbing, not a decision the model should be making: if `speak` were an MCP
tool, its schema would ride along in every prompt and the model would have to
*choose* to talk — sometimes forgetting, sometimes talking when it shouldn't.
Here the code decides, costs zero tokens, and always behaves the same way.

Isolation: Voicebox runs as its own process, its own venv, bound to 127.0.0.1.
It never sees asta's .env or database — it takes text and returns audio.
Everything (Whisper, the TTS models, your voice samples) stays on the laptop.

Engines: `kokoro` is an 82M-param model, fast enough to answer conversationally.
`chatterbox` does zero-shot cloning from a reference sample but is much slower —
worth it for a cloned voice, painful for real-time chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re

import httpx

BASE = os.environ.get("VOICEBOX_URL", "http://127.0.0.1:17493")
DEFAULT_ENGINE = os.environ.get("VOICEBOX_ENGINE", "kokoro")
DEFAULT_PROFILE = os.environ.get("VOICEBOX_PROFILE", "")
HINDI_PROFILE = os.environ.get("VOICEBOX_PROFILE_HI", "Asta (Hindi)")
# Kokoro on an M1 Pro is a couple of seconds for a sentence; cloning engines are
# slower, and the first call of the day also pays for loading the model.
GENERATE_TIMEOUT = float(os.environ.get("VOICEBOX_TIMEOUT", "120"))

# --- the two voices ----------------------------------------------------------
#
# Arun asks for one or the other in words — "talk like me", "talk like
# assistant" — so the choice has to survive being typed, spoken, or shouted
# mid-call, and it must never be GUESSED. Defaulting to his clone because the
# request was ambiguous would put his voice in front of someone on the strength
# of a parse failure, so anything unrecognised falls back to the assistant.
#
# Measured on this machine (M1 Pro), which is why they are not interchangeable:
#   assistant  kokoro      ~1.3s a line, faster than real time
#   mine       chatterbox  ~3.4x real time on the GPU, ~6.5x on CPU
VOICE_MINE = "mine"
VOICE_ASSISTANT = "assistant"

#: profile + engine per voice. The clone's profile name is whatever `voice clone`
#: created; the assistant keeps whatever the .env default is.
CLONE_PROFILE = os.environ.get("VOICEBOX_CLONE_PROFILE", "Arun")
CLONE_ENGINE = os.environ.get("VOICEBOX_CLONE_ENGINE", "chatterbox")

_SAY_AS_MINE = re.compile(
    r"\b(?:talk|speak|say\s+it|reply|answer)?\s*(?:like|as|in)\s+"
    r"(?:me|my\s+voice|myself|arun)\b|\bmy\s+voice\b|\bas\s+me\b", re.I)
_SAY_AS_ASSISTANT = re.compile(
    r"\b(?:talk|speak|say\s+it|reply|answer)?\s*(?:like|as|in)\s+"
    r"(?:the\s+)?(?:assistant|asta|bot|yourself)\b|\bassistant\s+voice\b", re.I)


def pick_voice(text: str, current: str = VOICE_ASSISTANT) -> str:
    """Which voice he just asked for, or `current` if he did not say.

    Assistant wins a tie. Two competing instructions in one sentence is not a
    coin toss — it is an unclear instruction, and the safe reading of an unclear
    instruction about whose voice to use is "not his".
    """
    said_assistant = bool(_SAY_AS_ASSISTANT.search(text or ""))
    said_mine = bool(_SAY_AS_MINE.search(text or ""))
    if said_assistant:
        return VOICE_ASSISTANT
    if said_mine:
        return VOICE_MINE
    return current if current in (VOICE_MINE, VOICE_ASSISTANT) else VOICE_ASSISTANT


def voice_settings(voice: str) -> tuple[str, str]:
    """(profile, engine) for a voice name."""
    if voice == VOICE_MINE:
        return CLONE_PROFILE, CLONE_ENGINE
    return DEFAULT_PROFILE, DEFAULT_ENGINE


def strip_voice_instruction(text: str) -> str:
    """The words to SAY, with the 'talk like me' instruction removed.

    Without this the instruction is spoken aloud — Vinish hears "talk like me,
    tell him the build passed", which is both wrong and revealing.
    """
    out = _SAY_AS_ASSISTANT.sub(" ", _SAY_AS_MINE.sub(" ", text or ""))
    return re.sub(r"\s{2,}", " ", out).strip(" ,.:;-—")


# --- playing audio into a call ----------------------------------------------

#: The virtual microphone Teams is pointed at. Empty = Asta cannot be heard.
CALL_DEVICE = os.environ.get("ASTA_CALL_AUDIO_DEVICE", "").strip()


def output_devices() -> list[str]:
    """Output devices this machine can play to. [] if the audio stack is absent."""
    try:
        import sounddevice as sd
        return [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
    except Exception:
        return []


def find_device(name: str) -> int | None:
    """Index of an output device by name, or None. Substring, case-insensitive —
    "BlackHole 2ch" is listed slightly differently across macOS versions."""
    if not name:
        return None
    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except Exception:
        return None
    wanted = name.strip().lower()
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0 and wanted in d["name"].strip().lower():
            return i
    return None


def play_to_device(wav: bytes, device: str = "") -> float:
    """Play WAV bytes to a named output device. Returns seconds played.

    Raises rather than returning quietly on every failure path. The whole reason
    this function exists is that `say_in_call` used to generate audio, drop it,
    and report success — so Arun believed a point had been made in a call when
    nothing had been said at all. A silent failure here is the same bug wearing
    a different hat.
    """
    import io
    import wave as _wave

    name = device or CALL_DEVICE
    if not name:
        raise RuntimeError("no output device configured (ASTA_CALL_AUDIO_DEVICE)")
    idx = find_device(name)
    if idx is None:
        have = ", ".join(output_devices()) or "none"
        raise RuntimeError(f"audio device {name!r} not found — available: {have}")

    with _wave.open(io.BytesIO(wav)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    if not frames:
        raise RuntimeError("audio had no frames — nothing to play")

    import numpy as np
    import sounddevice as sd
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels)
    # blocking on purpose: the caller needs to know it FINISHED speaking before
    # it reports that it spoke, and before anything else is said over the top.
    sd.play(samples, samplerate=rate, device=idx, blocking=True)
    return len(samples) / rate / max(1, channels if samples.ndim == 1 else 1)


async def available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{BASE}/health")).status_code == 200
    except Exception:
        return False


async def profiles() -> list[dict]:
    """Voice profiles Voicebox knows about — presets plus anything you cloned."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{BASE}/profiles")
            if r.status_code != 200:
                return []
            return [
                {"id": p.get("id"), "name": p.get("name"), "engine": p.get("engine")}
                for p in r.json()
            ]
    except Exception:
        return []


async def _wait_for_generation(client: httpx.AsyncClient, gen_id: str) -> None:
    """Consume the SSE status stream until the generation finishes or fails."""
    async with client.stream("GET", f"{BASE}/generate/{gen_id}/status") as r:
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            status = payload.get("status")
            if status == "failed":
                raise RuntimeError(payload.get("error") or "speech generation failed")
            if status == "not_found":
                raise RuntimeError(f"generation {gen_id} vanished")
            if status == "completed":
                return
    raise RuntimeError("status stream ended before the audio was ready")


async def profile_id(name: str) -> str:
    """UUID for a profile name — what /generate actually wants.

    Voicebox identifies voices by id; a name means nothing to it. Passing the
    name is silently ignored rather than rejected, so this lookup is the only
    thing standing between "speak as Arun" and "speak as whoever the default
    happens to be".
    """
    if not name:
        return ""
    wanted = name.strip().lower()
    for p in await profiles():
        if (p.get("name") or "").strip().lower() == wanted:
            return p.get("id") or ""
    return ""


def pick_profile(text: str, requested: str = "") -> str:
    """Match the voice to the script the reply is written in.

    An English voice reading Devanagari mispronounces every word, so a Hindi
    answer needs a Hindi voice. Script detection rather than language detection:
    it's exact, instant, and costs nothing. Tamil and Telugu are detected too,
    but only so we can stay on a sane voice — neither Kokoro nor Chatterbox can
    actually speak them (see status()["speaks"]).
    """
    if requested:
        return requested
    if any("ऀ" <= ch <= "ॿ" for ch in text):  # Devanagari → Hindi
        return HINDI_PROFILE or DEFAULT_PROFILE
    return DEFAULT_PROFILE


async def speak(text: str, profile: str = "", engine: str = "",
                voice: str = "") -> bytes:
    """Render text to speech; returns audio bytes (wav/mp3 as Voicebox produced).

    `voice` is the high-level choice — "mine" (his clone) or "assistant" — and
    fills in profile+engine when they were not named explicitly. An explicit
    profile/engine still wins, so existing callers behave exactly as before.

    Raises rather than returning empty audio so callers can fall back to the
    browser's built-in voice instead of playing silence.
    """
    if voice and not (profile or engine):
        profile, engine = voice_settings(voice)
    body: dict = {"text": text[:10000], "engine": engine or DEFAULT_ENGINE}
    chosen = pick_profile(text, profile)
    if chosen:
        # /speak takes `profile` as a name OR an id. The id is sent when it can
        # be resolved: a name that has drifted (renamed, re-cloned, deleted)
        # otherwise falls through to Voicebox's default voice, and the failure
        # is silent — audio comes back sounding like a stranger with nothing in
        # any response to say why. Resolving first turns that into an error.
        body["profile"] = await profile_id(chosen) or chosen
    async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT) as c:
        r = await c.post(f"{BASE}/speak", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"voicebox /speak returned {r.status_code}: {r.text[:200]}")
        gen_id = r.json().get("id")
        if not gen_id:
            raise RuntimeError("voicebox /speak gave no generation id")
        await _wait_for_generation(c, gen_id)
        audio = await c.get(f"{BASE}/audio/{gen_id}")
        if audio.status_code != 200:
            raise RuntimeError(f"voicebox audio fetch returned {audio.status_code}")
        return audio.content


async def transcribe(data: bytes, filename: str = "speech.webm",
                     language: str = "") -> str:
    """Whisper transcription of a recorded clip.

    Language is left blank on purpose: Whisper then detects it per clip, so a
    colleague switching to Hindi mid-conversation is transcribed as Hindi
    instead of being mangled into English-sounding nonsense.
    """
    async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/transcribe",
            files={"file": (filename, data, "application/octet-stream")},
            data={"language": language} if language else {},
        )
        if r.status_code != 200:
            raise RuntimeError(f"voicebox /transcribe returned {r.status_code}: {r.text[:200]}")
        return (r.json().get("text") or "").strip()


# --- cloning Arun's own voice ------------------------------------------------

# Read once, aloud, in a quiet room. Zero-shot cloning takes what it needs from
# ~20-30 seconds — it is NOT trained and does not improve with more recordings,
# so this is a one-off, not a daily habit. The paragraph is deliberately broad
# phonetically (hard consonants, long vowels, sibilants, digits, a question and
# an exclamation) because the clone copies the sounds it actually hears; a flat
# monotone sample produces a flat monotone voice.
# Voicebox concatenates EVERY sample on a profile into one combined reference
# (backend/backends/base.py: combine_voice_prompts) — so several short takes
# genuinely beat one. What they buy is COVERAGE of delivery, not raw minutes:
# the clone reproduces the prosody it hears, so a reference of only calm
# statements makes a voice that cannot ask a question. Around 60-90s total
# across these five is the sweet spot; past a couple of minutes there is
# nothing left to learn and generation just gets slower.
# WRITTEN IN ARUN'S OWN IDIOM, not in correct English.
#
# The first version of these was fluent, full-sentence prose — "Here is where
# things stand this morning", "I have been working through the vessel ETA
# ticket". He read it and said: follow how I speak, not how you write. He is
# right, and the reason is technical rather than cosmetic. Chatterbox clones
# PROSODY: the rhythm, the stress, where a sentence lifts and where it drops.
# Somebody reading a sentence they would never say produces read-aloud rhythm,
# and the clone then sounds like that for ever — a stranger with his timbre.
#
# So every line below is built from phrases he actually sent, taken out of his
# own Teams history: "bro", "na" as a tag question, "u" and "ur", "once",
# "post that" for afterwards, "couldn't able to". Reading them should feel like
# talking, because they are already his words. That is the whole point.
CLONE_SCRIPTS: dict[str, str] = {
    "1-status": (
        "bro, build finished around nine forty, all sixty two tests passed. "
        "vessel schedule sync is running fine, no issues from our side. "
        "i pushed one change last night, post that CI is green. "
        "nothing pending for u now."
    ),
    "2-one-to-one": (
        "hi bro, good to catch up. i was on that vessel ETA ticket most of "
        "this week, i think we are close now. one part i want ur view once "
        "before i raise the PR, because it is touching the service plan "
        "logic, everyone is depending on that one. tell me when u free, "
        "we can discuss and then merge."
    ),
    "3-quick": (
        "yes. no, not that one. go ahead. on it. give me a minute. "
        "done bro. approved. hold on, let me check. then fine. "
        "call me bro. all merged."
    ),
    "4-question": (
        "did the preprod deployment actually pass? who picked up the "
        "incident last night? just ur bug fix is fine na? are we sure this "
        "is the right branch? prod fix? CT u marked as skipped bro? "
        "then what is that change?"
    ),
    "5-technical": (
        "the ticket is BEPTELIKOS dash one zero one five nine, on the "
        "booking service repo. it is failing in TmsServiceImpl, null "
        "pointer when getServicePlanLegs returns null. i shared the PR link "
        "in the group, one three seven one. grafana is showing error rate "
        "around zero point four percent in preprod."
    ),
}

#: When he would rather just TALK than read — which produces a better clone,
#: because nobody reads with their own rhythm. One prompt per delivery; he
#: answers each out loud for fifteen or twenty seconds, in whatever words come.
CLONE_PROMPTS: dict[str, str] = {
    "1-status": "Give this morning's update out loud — build, tests, what is pending.",
    "2-one-to-one": "Tell Vinish where you got to on the ETA ticket and what you want his view on.",
    "3-quick": "Answer ten things quickly — yes, no, go ahead, on it, done, hold on.",
    "4-question": "Ask six things you genuinely need answers to today.",
    "5-technical": "Say the ticket id, the class it fails in, the PR number and the error rate.",
}

# Kept for callers that just want one paragraph.
CLONE_SCRIPT = CLONE_SCRIPTS["1-status"]


async def clone_from_sample(name: str, samples: list[tuple[str, bytes]],
                            language: str = "en") -> dict:
    """Create a voice profile from one or more read-aloud takes.

    samples: (filename, audio bytes) — several short takes covering different
    deliveries beat one long monotone one, because Voicebox concatenates them
    all into a single combined reference.

    The reference text for each take comes from WHISPER, not from the script we
    handed out: the engine aligns audio against that transcript, so it has to be
    what was actually said. Pairing files to scripts positionally breaks the
    moment a word is misread or takes are recorded out of order — transcribing
    each one is both more robust and lets Arun ad-lib.

    Everything stays on the laptop: Voicebox is bound to 127.0.0.1.
    """
    if not samples:
        raise ValueError("no audio samples given")
    async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT) as c:
        r = await c.post(f"{BASE}/profiles", json={
            "name": name,
            "description": f"Cloned from {len(samples)} read-aloud take(s)",
            "language": language,
            "voice_type": "cloned",
            # Chatterbox is the zero-shot cloning engine; kokoro is presets only.
            "default_engine": "chatterbox",
        })
        if r.status_code not in (200, 201):
            raise RuntimeError(f"voicebox /profiles returned {r.status_code}: {r.text[:200]}")
        profile = r.json()
        pid = profile.get("id")
        if not pid:
            raise RuntimeError("voicebox created no profile id")

        added = []
        for filename, audio in samples:
            text = await transcribe(audio, filename)
            if not text:
                raise RuntimeError(f"could not transcribe {filename} — is it silent?")
            r2 = await c.post(
                f"{BASE}/profiles/{pid}/samples",
                files={"file": (filename, audio, "application/octet-stream")},
                data={"reference_text": text},
            )
            if r2.status_code not in (200, 201):
                raise RuntimeError(
                    f"voicebox sample upload returned {r2.status_code}: {r2.text[:200]}")
            added.append({"file": filename, "words": len(text.split())})
    return {"id": pid, "name": profile.get("name", name), "samples": added}


async def status() -> dict:
    up = await available()
    return {
        "available": up,
        "url": BASE,
        "engine": DEFAULT_ENGINE,
        "profile": DEFAULT_PROFILE or "(voicebox default)",
        "hindi_profile": HINDI_PROFILE,
        # What it can actually SAY. Whisper understands far more than this,
        # including Tamil and Telugu — speaking them is the gap.
        "speaks": ["en", "hi", "zh", "ja", "es", "fr", "it", "pt"],
        "profiles": await profiles() if up else [],
        "hint": "ok" if up else "Voicebox backend not running — browser voice is used instead",
    }


if __name__ == "__main__":
    import asyncio as _aio
    import sys
    from pathlib import Path as _Path

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "script":
        # `script` prints all five; `script 2` prints one, for recording take by take.
        which = sys.argv[2] if len(sys.argv) > 2 else ""
        for key, text in CLONE_SCRIPTS.items():
            if which and not key.startswith(which):
                continue
            print(f"\n--- {key} ---\n{text}")
        if not which:
            print("\nRecord each as its own file, in a quiet room, in your normal "
                  "voice.\nRead 3-quick briskly and 4-question with real "
                  "intonation — a flat reference cannot ask a question.")
    elif cmd == "clone":
        # python -m app.voice clone "Arun" take1.m4a take2.m4a ...
        if len(sys.argv) < 4:
            print('usage: python -m app.voice clone "<profile name>" <audio> [<audio> ...]')
            raise SystemExit(2)
        pname = sys.argv[2]
        paths = [_Path(a).expanduser() for a in sys.argv[3:]]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            print("no such file(s): " + ", ".join(missing))
            raise SystemExit(2)
        out = _aio.run(clone_from_sample(
            pname, [(p.name, p.read_bytes()) for p in paths]))
        print(f"cloned: {out['name']}  (id {out['id']})")
        for s_ in out["samples"]:
            print(f"  + {s_['file']}  ({s_['words']} words transcribed)")
        print(f"\nnow set in .env:\n  VOICEBOX_PROFILE={out['name']}\n  VOICEBOX_ENGINE=chatterbox")
    elif cmd == "profiles":
        for p_ in _aio.run(profiles()):
            print(f"  {p_['name']:24} engine={p_['engine']}  id={p_['id']}")
    elif cmd == "say":
        text = " ".join(sys.argv[2:]) or "Testing the cloned voice."
        audio = _aio.run(speak(text))
        out_path = _Path("/tmp/asta-voice-test.wav")
        out_path.write_bytes(audio)
        print(f"wrote {out_path} ({len(audio)} bytes) — open it to hear the clone")
    else:
        print("usage: python -m app.voice [script [N] | clone <name> <audio...> | profiles | say <text>]")


#: Measure what a browser's microphone actually delivers. Level only — nothing is
#: recorded, kept, or sent anywhere.
_LEVEL_JS = """
async (ms) => {
  let s;
  try { s = await navigator.mediaDevices.getUserMedia({audio: true}); }
  catch (e) { return {error: e.message}; }
  const t = s.getAudioTracks()[0];
  const ctx = new AudioContext();
  const an = ctx.createAnalyser(); an.fftSize = 2048;
  ctx.createMediaStreamSource(s).connect(an);
  const buf = new Float32Array(an.fftSize);
  let peak = 0;
  const until = Date.now() + ms;
  while (Date.now() < until) {
    an.getFloatTimeDomainData(buf);
    for (let i = 0; i < buf.length; i++) if (Math.abs(buf[i]) > peak) peak = Math.abs(buf[i]);
    await new Promise(r => setTimeout(r, 40));
  }
  const label = t ? t.label : '';
  s.getTracks().forEach(x => x.stop());
  await ctx.close();
  return {label, peak};
}"""


async def browser_mic_delivers(page, ms: int = 900) -> dict:
    """Does this browser receive ANY audio? {'peak': float, 'label': str}.

    macOS denies microphone access to an app by handing it a perfectly valid,
    correctly-labelled track that produces digital silence. No exception, no
    warning — `getUserMedia` succeeds and every sample is zero.

    Playwright's browser is "Google Chrome for Testing", which never shows a
    permission prompt because nobody is there to click it. Measured on this
    machine: peak 0.00000 from BlackHole AND from the built-in microphone, with
    Chrome's audio processing both on and off. Five calls were placed to a
    colleague across which Asta reported speaking every time and transmitted
    silence, because `say_in_call` measured audio PLAYED and never audio SENT.
    """
    try:
        return await page.evaluate(_LEVEL_JS, ms) or {}
    except Exception as exc:                                    # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


#: The mute button's aria-label states the ACTION, not the state: "Mute" means the
#: mic is currently live, "Unmute" means it is currently muted. Reading it the
#: wrong way round mutes a working call, so the two are matched explicitly.
_MUTE_STATE_JS = """
() => {
  for (const b of document.querySelectorAll('button,[role="button"]')) {
    const a = (b.getAttribute('aria-label') || '');
    const t = (b.getAttribute('data-tid') || '');
    if (!/mute/i.test(a) && !/mute/i.test(t)) continue;
    if (!(b.offsetWidth || b.offsetHeight)) continue;
    return {aria: a, tid: t, muted: /unmute/i.test(a),
            pressed: b.getAttribute('aria-pressed')};
  }
  return null;
}"""


async def mic_is_live(page) -> dict:
    """What Teams thinks of its own microphone: {} when it cannot be read."""
    try:
        return await page.evaluate(_MUTE_STATE_JS) or {}
    except Exception:
        return {}


async def ensure_unmuted(page) -> bool:
    """False ONLY when Teams is definitely muted and unmuting it failed.

    Nothing checked this for a PLACED call: `join` mutes deliberately and says so,
    `call_person` never touched mute, and `say_in_call` checked the audio DEVICE
    and never whether Teams was transmitting.

    Unreadable is not muted. Refusing on an unreadable toolbar would silence every
    call whose DOM moved — the same rule `wait_for_answer` follows when it declines
    to call an unreadable call screen "no answer".
    """
    state = await mic_is_live(page)
    if not state or not state.get("muted"):
        return True
    for sel in ('[data-tid="toggle-mute"]', 'button[aria-label*="Unmute" i]'):
        try:
            await page.click(sel, timeout=3000)
            break
        except Exception:
            continue
    await asyncio.sleep(0.6)
    return not (await mic_is_live(page)).get("muted", True)
_CAMERA_TOGGLES = ('[data-tid="toggle-video"]', 'button[aria-label*="camera" i]')



#: The live call, if there is one. Held in the module rather than only in kv
#: because a browser context is not serialisable — and without the handle, nothing
#: can later hang up or notice the call ended. The kv row is the durable "am I in
#: a call" flag that survives a restart; this is what can act on it.
_CALL: dict = {}


def can_speak() -> bool:
    """Whether Asta can actually be heard in a call on this machine."""
    return bool(CALL_DEVICE)


def speaking_hint() -> str:
    return ("Speaking in calls needs a virtual microphone Teams can select "
            "(BlackHole or Loopback on macOS), then ASTA_CALL_AUDIO_DEVICE set to "
            "its name in .env. Until then I can join and listen, but I cannot say "
            "anything.")




async def self_test() -> dict:
    """Can Asta actually BE HEARD from this process? Measured, not assumed.

    The one check that would have saved four calls. `say_in_call` measured audio
    PLAYED and reported success; Vinish heard silence every time. macOS denies
    microphone access by handing the app a valid, correctly-labelled track that
    produces digital silence — no exception, no prompt — so every layer looked
    healthy and nothing was transmitted.

    It matters WHICH process asks. macOS grants the microphone to the responsible
    app, and a browser Playwright launches inherits the grant of whatever launched
    it. Proven at the time: the identical script measured peak 0.999969 run from
    Terminal and peak 0 run from Claude Code. Asta now runs under launchd, which is
    a third responsible process again — so the only honest answer is to run the
    whole path here and read the number.

    Plays a tone into the virtual mic while a real browser listens, exactly as a
    call does. The system input is restored in a `finally`: leaving it on BlackHole
    breaks his own Teams calls, which happened twice in one day.
    """
    from . import meetings, teams_bridge
    out: dict = {"device": CALL_DEVICE, "restored": False}
    if not CALL_DEVICE:
        out["error"] = "no virtual microphone configured (ASTA_CALL_AUDIO_DEVICE)"
        return out
    was = await meetings.current_mic()
    out["was"] = was
    pw = ctx = None
    try:
        out["switched"] = await meetings.set_call_mic(device=CALL_DEVICE)
        if not out["switched"]:
            out["error"] = f"could not select {CALL_DEVICE!r}"
            return out
        wav = await speak("Testing one two three. This is a microphone check.")
        out["synth_bytes"] = len(wav or b"")
        if not wav:
            out["error"] = "speech synthesis produced nothing"
            return out
        await teams_bridge.close_pool()          # one writer per profile
        pw, ctx = await teams_bridge._launch(headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://teams.microsoft.com/v2/",
                        wait_until="domcontentloaded", timeout=60000)
        # Play while the browser is already listening — starting playback first is
        # a measurement of nothing, which cost a whole cycle the first time.
        import asyncio as _a
        listen = _a.create_task(browser_mic_delivers(page, 2500))
        await _a.sleep(0.4)
        await _a.to_thread(play_to_device, wav, CALL_DEVICE)
        heard = await listen
        out.update(heard or {})
        out["heard"] = float((heard or {}).get("peak") or 0) > 0.01
    except Exception as exc:                                     # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            if ctx is not None:
                await ctx.close()
            if pw is not None:
                await pw.stop()
        # Never leave his input on the virtual device.
        if was and was != CALL_DEVICE:
            with contextlib.suppress(Exception):
                out["restored"] = await meetings.set_call_mic(device=was)
    return out
