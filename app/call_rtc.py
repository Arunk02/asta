"""Being heard on a Teams call — and knowing it — read from inside the call.

Fifty-odd calls, and the person on the other end never heard a word. Every layer
that claimed the call was working read something BESIDE the call:

  * the microphone was a chain — speech played into BlackHole, the Mac's input
    switched to BlackHole, Chrome granted the Mac's microphone, Teams picking
    the right device — and any link could carry silence while every other link
    looked healthy;
  * "did they pick up?" was read off the screen, and Teams moves its call into
    other windows, renames its buttons, and shows the chat list while the call
    runs elsewhere. On 22 Sep a colleague answered and talked for forty seconds
    while Asta read the chat window and waited for somebody to pick up.

So nothing here is read from the outside:

  * Asta's microphone is a WebAudio stream INSIDE the browser. Teams asks for a
    microphone and gets Asta's; speech is played straight into it. No macOS
    permission, no virtual device, no switching his input and forgetting to put
    it back.
  * "Are they there?" comes from the call's own RTCPeerConnection: packets
    arriving, and the energy of a voice in them. Teams can redesign every
    button; it cannot connect a call without these.
  * "Was I heard?" is the energy of the audio Teams actually SENT while Asta
    spoke. A line that left no trace in the outgoing stream was not said.
  * "What did they say?" is their own audio, recorded from the call and
    transcribed on this Mac (Voicebox's Whisper), not captions scraped off a
    screen that may not even be the call's.

On by default; ASTA_CALL_RTC=0 returns to the old device chain.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import time
from pathlib import Path


def enabled() -> bool:
    return os.environ.get("ASTA_CALL_RTC", "1").strip().lower() not in ("0", "false", "no", "off")


#: Injected into every page and frame before Teams loads. Three jobs: keep a
#: handle on every peer connection Teams makes (so its statistics can be read),
#: hand Teams Asta's own microphone, and tap the far end's audio.
INIT_JS = r"""
(() => {
  if (window.__asta) return;
  const A = window.__asta = {pcs: [], ctx: null, dest: null, gum: 0, tapped: {},
                             chunks: [], levels: [], recording: false, loudMs: 0,
                             lastLoud: 0, level: 0, sinks: []};

  const Native = window.RTCPeerConnection;
  if (Native) {
    const Kept = new Proxy(Native, {construct(target, args, newTarget) {
      const pc = Reflect.construct(target, args, newTarget);
      A.pcs.push(pc);
      return pc;
    }});
    window.RTCPeerConnection = Kept;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Kept;
  }

  const audio = () => {
    if (!A.ctx) {
      A.ctx = new AudioContext({sampleRate: 48000});
      A.dest = A.ctx.createMediaStreamDestination();
      // A constant zero keeps the track alive between lines, so Teams never
      // sees a microphone that stopped.
      const keep = A.ctx.createConstantSource();
      keep.offset.value = 0;
      keep.connect(A.dest);
      keep.start();
    }
    if (A.ctx.state !== 'running') A.ctx.resume().catch(() => {});
    return A.ctx;
  };

  const md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    const native = md.getUserMedia.bind(md);
    md.getUserMedia = async (c) => {
      if (!c || !c.audio) return native(c);
      audio();
      A.gum += 1;
      const out = new MediaStream(A.dest.stream.getAudioTracks().map(t => t.clone()));
      if (c.video) {
        try { (await native({video: c.video})).getVideoTracks().forEach(t => out.addTrack(t)); }
        catch (e) {}
      }
      return out;
    };
  }

  A.playing = [];
  A.say = async (b64) => {
    const ctx = audio();
    const bytes = Uint8Array.from(atob(b64), ch => ch.charCodeAt(0));
    const buf = await ctx.decodeAudioData(bytes.buffer);
    return await new Promise(done => {
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(A.dest);
      A.playing.push(src);
      src.onended = () => { A.playing = A.playing.filter(x => x !== src); done(buf.duration); };
      src.start();
    });
  };
  // Stop mid-sentence: the other person started talking.
  A.stop = () => {
    for (const src of A.playing) { try { src.stop(); } catch (e) {} }
    A.playing = [];
    return true;
  };

  A.check = async () => {
    const ctx = audio();
    const an = ctx.createAnalyser();
    an.fftSize = 2048;
    const src = ctx.createMediaStreamSource(A.dest.stream);
    src.connect(an);
    const osc = ctx.createOscillator();
    osc.frequency.value = 440;
    osc.connect(A.dest);
    osc.start();
    await new Promise(r => setTimeout(r, 250));
    const d = new Float32Array(an.fftSize);
    an.getFloatTimeDomainData(d);
    osc.stop();
    osc.disconnect();
    src.disconnect();
    let peak = 0;
    for (const x of d) if (Math.abs(x) > peak) peak = Math.abs(x);
    return {peak, state: ctx.state};
  };

  A.tap = () => {
    const ctx = audio();
    if (!A.tapNode) {
      A.tapNode = ctx.createScriptProcessor(4096, 1, 1);
      const silent = ctx.createGain();
      silent.gain.value = 0;
      A.tapNode.connect(silent);
      silent.connect(ctx.destination);
      A.tapNode.onaudioprocess = (e) => {
        const x = e.inputBuffer.getChannelData(0);
        let sum = 0;
        for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
        A.level = Math.sqrt(sum / x.length);
        if (A.level > 0.01) {
          A.lastLoud = performance.now();
          A.loudMs += 1000 * x.length / e.inputBuffer.sampleRate;
        }
        if (A.recording) {
          A.chunks.push(new Float32Array(x));
          A.levels.push(A.level);
          if (A.chunks.length > 900) { A.chunks.shift(); A.levels.shift(); }
        }
      };
    }
    let added = 0;
    for (const pc of A.pcs) {
      for (const r of (pc.getReceivers ? pc.getReceivers() : [])) {
        const t = r.track;
        if (!t || t.kind !== 'audio' || t.readyState !== 'live' || A.tapped[t.id]) continue;
        A.tapped[t.id] = 1;
        const ms = new MediaStream([t]);
        // Chrome feeds a remote WebRTC track into WebAudio only while something
        // is playing it.
        const el = new Audio();
        el.muted = true;
        el.srcObject = ms;
        el.play().catch(() => {});
        A.sinks.push(el);
        ctx.createMediaStreamSource(ms).connect(A.tapNode);
        added += 1;
      }
    }
    return added;
  };

  A.record = (on) => {
    A.recording = !!on;
    A.chunks = [];
    A.levels = [];
    A.loudMs = 0;
    A.lastLoud = 0;
  };

  A.take = (keep) => {
    const rate = A.ctx ? A.ctx.sampleRate : 48000;
    // Only the stretch where somebody spoke, with a little either side: long
    // silence is what sends Whisper into repeating one word for ever.
    let first = A.levels.findIndex(l => l > 0.01);
    let last = A.levels.length - 1 - [...A.levels].reverse().findIndex(l => l > 0.01);
    const keepChunks = first < 0 ? [] : A.chunks.slice(Math.max(0, first - 2), Math.min(A.chunks.length, last + 3));
    let n = 0;
    for (const c of keepChunks) n += c.length;
    const pcm = new Float32Array(n);
    let o = 0;
    for (const c of keepChunks) { pcm.set(c, o); o += c.length; }
    if (!keep) { A.chunks = []; A.levels = []; }
    const step = rate / 16000;
    const m = Math.floor(n / step);
    const buf = new ArrayBuffer(44 + m * 2);
    const v = new DataView(buf);
    const w = (p, s) => { for (let i = 0; i < s.length; i++) v.setUint8(p + i, s.charCodeAt(i)); };
    w(0, 'RIFF'); v.setUint32(4, 36 + m * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true);
    v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, m * 2, true);
    let peak = 0;
    for (let i = 0; i < m; i++) {
      const s = Math.max(-1, Math.min(1, pcm[Math.floor(i * step)]));
      if (Math.abs(s) > peak) peak = Math.abs(s);
      v.setInt16(44 + i * 2, s * 0x7fff, true);
    }
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return {wav: btoa(bin), seconds: m / 16000, peak};
  };

  A.snapshot = async () => {
    const s = {pcs: 0, connected: 0, closed: 0, inPackets: 0, inEnergy: 0, outBytes: 0,
               outEnergy: 0, gum: A.gum, level: A.level, loudMs: A.loudMs,
               quietMs: A.lastLoud ? performance.now() - A.lastLoud : -1};
    for (const pc of A.pcs) {
      s.pcs += 1;
      const st = pc.connectionState, ice = pc.iceConnectionState;
      if (st === 'connected' || ice === 'connected' || ice === 'completed') s.connected += 1;
      if (st === 'closed' || st === 'failed' || pc.signalingState === 'closed') s.closed += 1;
      try {
        (await pc.getStats()).forEach(r => {
          const kind = r.kind || r.mediaType;
          if (kind !== 'audio') return;
          if (r.type === 'inbound-rtp') { s.inPackets += r.packetsReceived || 0; s.inEnergy += r.totalAudioEnergy || 0; }
          if (r.type === 'outbound-rtp') s.outBytes += r.bytesSent || 0;
          if (r.type === 'media-source') s.outEnergy += r.totalAudioEnergy || 0;
        });
      } catch (e) {}
    }
    return s;
  };
})();
"""


# --- the browser side ---------------------------------------------------------

async def install(ctx) -> None:
    """Before Teams loads: every page and frame of this browser gets it."""
    await ctx.add_init_script(INIT_JS)


def _frames(ctx) -> list:
    out = []
    for page in list(getattr(ctx, "pages", []) or []):
        try:
            if page.is_closed():
                continue
        except Exception:                                      # noqa: BLE001
            continue
        out.extend(getattr(page, "frames", []) or [])
    return out


async def _each(ctx, js: str, arg=None) -> list[tuple[object, object]]:
    """(frame, result) for every frame where Asta's script is running."""
    got = []
    for frame in _frames(ctx):
        try:
            if arg is None:
                value = await frame.evaluate(f"() => window.__asta ? ({js}) : null")
            else:
                value = await frame.evaluate(f"(a) => window.__asta ? ({js}) : null", arg)
        except Exception:                                      # noqa: BLE001
            continue
        if value is not None:
            got.append((frame, value))
    return got


_SUMMED = ("pcs", "connected", "closed", "inPackets", "inEnergy", "outBytes",
           "outEnergy", "gum", "loudMs")


def merge(parts: list[dict]) -> dict:
    """One view of the call from every frame's view of it."""
    out = {k: 0 for k in _SUMMED}
    out.update(level=0.0, quietMs=-1.0)
    for p in parts:
        for k in _SUMMED:
            out[k] += p.get(k) or 0
        out["level"] = max(out["level"], p.get("level") or 0)
        q = p.get("quietMs")
        if q is not None and q >= 0:
            out["quietMs"] = q if out["quietMs"] < 0 else min(out["quietMs"], q)
    return out


async def mic_ready(page) -> float:
    """Before the dial: Asta's microphone is in this page and carries sound.

    Played into Asta's own stream, not into a call, so nobody hears it. Raises
    with the reason when it is not ready; returns the level it measured.
    """
    try:
        got = await page.evaluate("() => window.__asta ? window.__asta.check() : null")
    except Exception as exc:                                   # noqa: BLE001
        raise RuntimeError(f"Asta's microphone could not be checked ({type(exc).__name__})") from exc
    if not got:
        raise RuntimeError("Asta's microphone did not load into Teams")
    peak = float(got.get("peak") or 0)
    if peak <= 0.01:
        raise RuntimeError(f"Asta's microphone is silent (audio {got.get('state')})")
    return peak


async def snapshot(ctx) -> dict:
    await _each(ctx, "window.__asta.tap()")
    return merge([v for _, v in await _each(ctx, "window.__asta.snapshot()")])


async def record(ctx, on: bool) -> None:
    await _each(ctx, f"window.__asta.record({'true' if on else 'false'})")


async def take(ctx, keep: bool = False) -> tuple[bytes, float, float]:
    """(wav, seconds, peak) of what their side said since recording started.
    `keep` reads it without clearing — a first look while they may go on."""
    best = (b"", 0.0, 0.0)
    for _, v in await _each(ctx, f"window.__asta.take({'true' if keep else 'false'})"):
        if isinstance(v, dict) and float(v.get("seconds") or 0) > best[1]:
            best = (base64.b64decode(v.get("wav") or ""), float(v["seconds"]),
                    float(v.get("peak") or 0))
    return best


# --- deciding -----------------------------------------------------------------

#: Energy that counts as having been SENT. Speech from Voicebox lands orders of
#: magnitude above this; a muted or silent track stays at zero.
SENT_ENERGY = 1e-4

#: A voice on their side: this long of sound above the floor. For deciding that
#: somebody PICKED UP — a click on the line is not a hello.
VOICE_MS = 350

#: Once Asta has asked something, far less counts as an answer: "yes" is a
#: quarter of a second. On 22 Sep her 341 ms "yes" fell under 350 and Asta
#: waited in silence until she hung up — "it expects a certain response".
TURN_VOICE_MS = 170

#: Talking for this long is a person, whatever the screen says.
SURE_VOICE_MS = 1500


def spoke(before: dict, after: dict) -> bool:
    """Did audio with a voice in it leave through the call between the two readings?"""
    return (after.get("outEnergy", 0) - before.get("outEnergy", 0)) > SENT_ENERGY


#: Energy arriving between two readings that is a voice, not comfort noise —
#: WebRTC's own measure, independent of Asta's tap on the same audio.
HEARD_ENERGY = 5e-4


def they_are_talking(s: dict, within_ms: float = 1500, prev: dict | None = None) -> bool:
    q = s.get("quietMs", -1)
    if s.get("loudMs", 0) >= VOICE_MS and 0 <= q <= within_ms:
        return True
    return bool(prev) and (s.get("inEnergy", 0) - prev.get("inEnergy", 0)) > HEARD_ENERGY


def media_flowing(prev: dict | None, now: dict) -> bool:
    return bool(prev) and now.get("connected", 0) > 0 and now.get("inPackets", 0) > prev.get("inPackets", 0)


async def wait_for_them(ctx, seconds: float = 45, quiet_open: float = 4.0,
                        ringing=None, clock=time.monotonic, nap=asyncio.sleep,
                        log=None) -> str:
    """'voice' | 'connected' | 'ended' | 'no answer'.

    A person picking up usually says hello, so their voice is the answer —
    and the cue to speak. Someone who picks up in silence still has a call
    carrying packets; after `quiet_open` seconds of that, Asta opens anyway.
    `ringing` is an optional async check of the screen: while Teams still says
    it is ringing, sound on the line is a ringback tone, not a person.
    """
    start = clock()
    prev: dict | None = None
    flowing_since = 0.0
    was_connected = False
    while clock() - start < seconds:
        s = await snapshot(ctx)
        if log:
            log(s)
        if s.get("connected"):
            was_connected = True
        if was_connected and s.get("pcs") and s.get("closed") >= s.get("pcs"):
            return "ended"
        still_ringing = bool(ringing and await ringing())
        if log:
            log({"ringing": still_ringing})
        # The screen may only veto a short sound. Someone who has talked for a
        # second and a half has picked up, whatever the screen still says —
        # trusting a stuck "ringing" is how a colleague talks to silence.
        talking = they_are_talking(s, prev=prev)
        if talking and (not still_ringing or s.get("loudMs", 0) >= SURE_VOICE_MS):
            return "voice"
        if not still_ringing and media_flowing(prev, s):
            flowing_since = flowing_since or clock()
            if clock() - flowing_since >= quiet_open:
                return "connected"
        prev = s
        await nap(0.25)
    return "no answer"


async def hear_turn(ctx, wait: float = 14, pause_ms: float = 600, longest: float = 30,
                    clock=time.monotonic, nap=asyncio.sleep, transcribe=None,
                    log=None, keep: bool = False) -> dict:
    """What they say next, up to the pause that ends it. {'text', 'seconds', 'spoke'}.

    Records their side only — the tap never sees Asta's own voice — so Asta
    cannot answer itself. `keep` continues a recording already running: they
    interrupted Asta, and what they said over it is the start of this turn.
    """
    if not keep:
        await record(ctx, True)
    from . import voice
    listen = transcribe or voice.transcribe
    start = clock()
    began = 0.0
    early = None                      # (loudMs when taken, transcription task)
    while True:
        s = await snapshot(ctx)
        if log:
            log(s)
        if not began and s.get("loudMs", 0) >= TURN_VOICE_MS:
            began = clock()
        quiet = s.get("quietMs", -1)
        # A first look at the first short pause: if it turns out to be the end,
        # the words are already transcribed by the time the pause is confirmed.
        if began and early is None and quiet >= EARLY_LOOK_MS:
            wav_so_far, _, _ = await take(ctx, keep=True)
            early = (s.get("loudMs", 0), asyncio.ensure_future(_quietly(listen, wav_so_far)))
        if early is not None and s.get("loudMs", 0) > early[0]:
            early = None              # they went on: that look is out of date
        if began and (quiet >= pause_ms or clock() - began > longest):
            break
        if not began and clock() - start > wait:
            break
        if s.get("pcs") and s.get("closed") >= s.get("pcs"):
            break
        await nap(0.15)
    wav, seconds, peak = await take(ctx)
    await record(ctx, False)
    if not began or not wav:
        return {"text": "", "seconds": seconds, "spoke": False}
    text = await early[1] if early is not None else await _quietly(listen, wav)
    return {"text": (text or "").strip(), "seconds": seconds, "spoke": True}


#: Silence after which their words are transcribed speculatively.
EARLY_LOOK_MS = 300


#: Longest a transcription may take before the turn counts as not understood —
#: a Whisper that has started repeating itself can run far past this.
TRANSCRIBE_TIMEOUT = 5.0


def garbled(text: str) -> bool:
    """Whisper's failure mode on noise: one word, over and over.

    22 Sep: a colleague's reply came back as "Haruki" two hundred times, and a
    reply to that would have been a reply to nothing.
    """
    words = re.findall(r"[\w']+", (text or "").lower())
    if len(words) < 6:
        return False
    top = max(words.count(w) for w in set(words))
    return top / len(words) > 0.5 or len(set(words)) <= 2


async def _quietly(listen, wav: bytes) -> str:
    try:
        text = await asyncio.wait_for(listen(wav, filename="turn.wav"), TRANSCRIBE_TIMEOUT)
    except Exception:                                          # noqa: BLE001
        return ""
    return "" if garbled(text) else text


#: A person answers with a word or two and waits. A recorded greeting talks on.
HUMAN_GREETING_MS = 2500

_VOICEMAIL = re.compile(
    r"leave (?:a|your) message|after the (?:tone|beep)|not available|unavailable|"
    r"voice ?mail|mailbox|record(?:ing)? your message|(?:pound|hash|star) key|"
    r"key for more options|can'?t (?:take|answer) (?:your|the|this) call", re.I)


def is_voicemail(text: str) -> bool:
    return bool(_VOICEMAIL.search(text or ""))


async def greeting(ctx, pause_ms: float = 700, longest: float = 7.0,
                   clock=time.monotonic, nap=asyncio.sleep, transcribe=None) -> dict:
    """What answered: {'voicemail': bool, 'text': str, 'spoke_ms': float}.

    Listens only until their first pause — a person says "hello?" and waits,
    so this costs them well under a second. Only a greeting that runs on past
    what a person says is transcribed and checked for a voicemail's words: on
    22 Sep Teams voicemail answered, and Asta greeted a recording.
    """
    await record(ctx, True)
    start = clock()
    spoke = 0.0
    while clock() - start < longest:
        s = await snapshot(ctx)
        spoke = s.get("loudMs", 0)
        waited = clock() - start
        if (spoke == 0 and waited >= 0.8) or (spoke > 0 and s.get("quietMs", -1) >= pause_ms):
            break
        if s.get("pcs") and s.get("closed") >= s.get("pcs"):
            break
        await nap(0.2)
    wav, _, _ = await take(ctx)
    await record(ctx, False)
    if spoke < HUMAN_GREETING_MS:
        return {"voicemail": False, "text": "", "spoke_ms": spoke}
    from . import voice
    text = ""
    with contextlib.suppress(Exception):
        text = await (transcribe or voice.transcribe)(wav, filename="greeting.wav")
    return {"voicemail": is_voicemail(text), "text": text or "", "spoke_ms": spoke}


#: Their voice over Asta's for this long means they are talking, not coughing —
#: and not Asta's own voice coming back off their speaker.
BARGE_IN_MS = 600
#: A breath, a "hello?" and an echo at the start of Asta's line are not an
#: interruption.
BARGE_IN_GRACE = 1.0


async def say(ctx, wav: bytes, interruptible: bool = False,
              clock=time.monotonic, nap=asyncio.sleep) -> dict:
    """Play `wav` into Asta's microphone. {'seconds', 'sent', 'interrupted'}.

    `sent` is measured on the call's outgoing audio. With `interruptible`, their
    side is recorded while Asta speaks, and if they start talking Asta stops —
    a person does not keep talking over someone who has started to answer. The
    recording is left running so their first words are part of the next turn.
    """
    before = await snapshot(ctx)
    b64 = base64.b64encode(wav).decode()
    targets = [f for f, v in await _each(ctx, "{gum: window.__asta.gum, pcs: window.__asta.pcs.length}")
               if isinstance(v, dict) and v.get("gum")]
    if not targets:
        raise RuntimeError("Teams never took Asta's microphone in this call — said nothing")
    if interruptible:
        await record(ctx, True)
    playing = [asyncio.ensure_future(f.evaluate("(b) => window.__asta.say(b)", b64)) for f in targets]
    interrupted = False
    if interruptible:
        started = clock()
        while not all(p.done() for p in playing):
            await nap(0.15)
            if clock() - started < BARGE_IN_GRACE:
                continue
            s = await snapshot(ctx)
            if s.get("loudMs", 0) >= BARGE_IN_MS and 0 <= s.get("quietMs", -1) <= 400:
                interrupted = True
                await _each(ctx, "window.__asta.stop()")
                # What was recorded under Asta's own voice may be its echo; the
                # turn starts from the moment they cut in.
                await record(ctx, True)
                break
    seconds = 0.0
    for p in playing:
        with contextlib.suppress(Exception):
            seconds = max(seconds, float(await p or 0))
    await nap(0.3)
    after = await snapshot(ctx)
    return {"seconds": seconds, "sent": spoke(before, after), "interrupted": interrupted,
            "energy": after.get("outEnergy", 0) - before.get("outEnergy", 0)}


# --- the call path on top of it -------------------------------------------------
# meetings.py keeps the call's lifecycle; these are its steps on Asta's own
# microphone, kept here so the call module stays the size it was brought down to.

async def dial_check(page, who: str) -> dict:
    """Before the phone rings: Asta's microphone is in this page and carries sound."""
    try:
        peak = await mic_ready(page)
    except RuntimeError as exc:
        raise RuntimeError(f"not dialling {who} — {exc}. Nobody was rung.") from exc
    return {"peak": peak, "label": "Asta's microphone"}


async def wait_for_answer(page, seconds: float = 0) -> str:
    """Somebody picked up — read from the call itself.

    Their voice on the line is the answer and the cue: people say hello when
    they pick up. The screen is consulted only to know it is still ringing, so
    a ringback tone is never mistaken for a person.
    """
    from . import call_screen, meetings
    call = meetings._CALL

    async def ringing() -> bool:
        with contextlib.suppress(Exception):
            return (await meetings.call_state(await meetings._follow_call_window(page))) == "ringing"
        return False

    got = await wait_for_them(call["ctx"], seconds or meetings.RING_SECONDS,
                              ringing=ringing, log=call.get("log"))
    if got in ("voice", "connected"):
        call["answered_at"] = meetings._now()
        call["answered_by"] = got
        return "connected"
    if got == "no answer":
        with contextlib.suppress(Exception):
            call["last_screen"] = await call_screen.describe(page)
    return got


async def say_line(text: str, voice_name: str = "") -> str:
    """Say it through Asta's own microphone, and only report what was SENT.

    The measure is the call's outgoing audio: energy that left through Teams
    while Asta spoke. A line that left none — Teams muted, the track swapped —
    raises, because he must never be told a point was made when nobody heard it.
    """
    from . import meetings, store, voice
    call = meetings._CALL
    if not store.kv_get("teams_in_call"):
        raise RuntimeError("not in a call")
    ctx = call["ctx"]
    if not call.get("answered_at"):
        reading = await snapshot(ctx)
        if not (reading.get("connected") and reading.get("inPackets")):
            raise RuntimeError("nobody has picked up yet — said nothing")
        call["answered_at"] = meetings._now()
    words = voice.strip_voice_instruction(text)
    if not words:
        raise RuntimeError("nothing left to say once the instruction was removed")
    chosen = voice.pick_voice(text, voice_name or voice.VOICE_ASSISTANT)
    audio = await meetings.synth(words, chosen)
    if not audio:
        raise RuntimeError("speech generation produced nothing — said nothing")
    out = await say(ctx, audio, interruptible=bool(call.get("barge_in")))
    call["interrupted"] = bool(out.get("interrupted"))
    log = call.get("log")
    if log:
        log({"said": words[:200], "sent": out["sent"], "energy": out["energy"],
             "interrupted": call["interrupted"]})
    if not out["sent"]:
        raise RuntimeError("played it into Asta's microphone, but the call sent no "
                           "sound — Teams may be muted. Treat it as NOT said.")
    store.kv_set("teams_last_spoken", words[:500])
    return f"said it in the call in {chosen} voice ({out['seconds']:.1f}s): {words[:120]}"


# --- the flight recorder --------------------------------------------------------

def recorder(label: str):
    """Every reading of a call, one JSON line each, in data/calls/. The next time a
    call goes wrong, what the call itself reported is on the record — not
    re-guessed from a colleague's memory of it."""
    from . import store
    folder = Path(store.DB_PATH).parent / "calls"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{label[:30].replace(' ', '_')}.jsonl"

    def log(reading: dict) -> None:
        try:
            with path.open("a") as fh:
                fh.write(json.dumps({"at": round(time.time(), 2), **reading}) + "\n")
        except Exception:                                      # noqa: BLE001
            pass
    log.path = path
    return log
