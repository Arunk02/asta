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

import json
import os

import httpx

BASE = os.environ.get("VOICEBOX_URL", "http://127.0.0.1:17493")
DEFAULT_ENGINE = os.environ.get("VOICEBOX_ENGINE", "kokoro")
DEFAULT_PROFILE = os.environ.get("VOICEBOX_PROFILE", "")
HINDI_PROFILE = os.environ.get("VOICEBOX_PROFILE_HI", "Asta (Hindi)")
# Kokoro on an M1 Pro is a couple of seconds for a sentence; cloning engines are
# slower, and the first call of the day also pays for loading the model.
GENERATE_TIMEOUT = float(os.environ.get("VOICEBOX_TIMEOUT", "120"))


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


async def speak(text: str, profile: str = "", engine: str = "") -> bytes:
    """Render text to speech; returns audio bytes (wav/mp3 as Voicebox produced).

    Raises rather than returning empty audio so callers can fall back to the
    browser's built-in voice instead of playing silence.
    """
    body: dict = {"text": text[:10000], "engine": engine or DEFAULT_ENGINE}
    chosen = pick_profile(text, profile)
    if chosen:
        body["profile"] = chosen
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
CLONE_SCRIPTS: dict[str, str] = {
    "1-status": (
        "Here is where things stand this morning. The booking service build "
        "finished at nine forty, all sixty-two tests passed, and the vessel "
        "schedule sync is running normally. Nothing needs your attention yet."
    ),
    "2-one-to-one": (
        "Hi, good to catch up. I have been working through the vessel ETA "
        "ticket for most of the week, and I think we are close. There is one "
        "part I want your view on before I raise the pull request, because it "
        "touches the service plan logic that everyone depends on."
    ),
    "3-quick": (
        "Yes. No, not that one. Go ahead. On it. Give me a minute. "
        "That is done. Approved. Hold on, let me check. Perfect, thanks."
    ),
    "4-question": (
        "Did the deployment to preprod actually pass? Who picked up the "
        "incident overnight? Are we sure this is the right branch? "
        "Excellent, that is exactly what I wanted to hear! Careful there, "
        "that change would break the amend flow."
    ),
    "5-technical": (
        "The ticket is BEPTELIKOS nine three nine seven, on the telikos "
        "booking service repository. It fails in VesselInformationDomainService "
        "with a null pointer when getServiceTypeModes returns null. "
        "Grafana shows the error rate at zero point four percent in preprod."
    ),
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
