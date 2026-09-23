"""A rehearsal call: Asta rings a simulated colleague, end to end, before a person.

Every live call on 22 Sep found a failure the suite could not: a microphone
check that misread silence, a screen that was the wrong window, captions that
took 32 seconds to switch on, a short "yes" below a threshold, a greeting stuck
behind thirteen other lines, a colleague's reply turned into one word repeated
two hundred times. Each was found by ringing a real person, who then had to sit
through it. The colleagues were the test bench.

This is the bench instead. Two browser contexts in one real Chrome: Asta's side
has Asta's injected microphone exactly as Teams would (app/call_rtc.py), the
colleague's side has its own voice and ears, and a real WebRTC connection runs
between them. The REAL conversation code places the "call", hears, thinks and
speaks — only the Teams dial and hang-up are swapped for connecting and closing
the two sides. The colleague follows a script (say hello over the greeting,
answer "yes", interrupt, go quiet, be a voicemail, sit on a noisy speakerphone)
and the colleague's own ears time every gap and hear every word Asta sends.

    .venv/bin/python -m app.call_rehearsal            # every scenario
    .venv/bin/python -m app.call_rehearsal quick-yes  # one

Isolated like WorkWorld: its own temporary database, nothing reaches his phone,
his Teams, or his microphone. It does use the real voice server and a real
brain, because those are exactly the parts a live call exercises.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

#: The colleague's side. Its own microphone (lines, noise, an echo of what it
#: hears) and its own ears on what Asta sends, timed on the wall clock.
COLLEAGUE_JS = r"""
(() => {
  const C = window.__colleague = {spans: [], loud: false, lastLoud: 0, quietSince: 0};
  C.ctx = new AudioContext({sampleRate: 48000});
  C.dest = C.ctx.createMediaStreamDestination();
  const keep = C.ctx.createConstantSource(); keep.offset.value = 0; keep.connect(C.dest); keep.start();

  C.say = async (b64) => {
    const bytes = Uint8Array.from(atob(b64), ch => ch.charCodeAt(0));
    const buf = await C.ctx.decodeAudioData(bytes.buffer);
    return await new Promise(done => {
      const src = C.ctx.createBufferSource();
      src.buffer = buf; src.connect(C.dest);
      src.onended = () => done(Date.now());
      src.start();
    });
  };

  C.noise = (level) => {
    if (C.noiseNode) { C.noiseNode.stop(); C.noiseNode = null; }
    if (!level) return;
    const len = C.ctx.sampleRate * 2, buf = C.ctx.createBuffer(1, len, C.ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1);
    const src = C.ctx.createBufferSource(); src.buffer = buf; src.loop = true;
    const g = C.ctx.createGain(); g.gain.value = level;
    src.connect(g); g.connect(C.dest); src.start(); C.noiseNode = src;
  };

  C.pc = new RTCPeerConnection();
  C.dest.stream.getTracks().forEach(t => C.pc.addTrack(t, C.dest.stream));
  C.pc.ontrack = (e) => {
    const ms = new MediaStream([e.track]);
    const el = new Audio(); el.muted = true; el.srcObject = ms; el.play().catch(() => {});
    C.sink = el;
    const src = C.ctx.createMediaStreamSource(ms);
    C.heard = src;
    const tap = C.ctx.createScriptProcessor(4096, 1, 1);
    const silent = C.ctx.createGain(); silent.gain.value = 0;
    src.connect(tap); tap.connect(silent); silent.connect(C.ctx.destination);
    tap.onaudioprocess = (ev) => {
      const x = ev.inputBuffer.getChannelData(0);
      let s = 0; for (let i = 0; i < x.length; i++) s += x[i] * x[i];
      const level = Math.sqrt(s / x.length), now = Date.now();
      if (level > 0.01) {
        if (!C.loud && (!C.lastLoud || now - C.lastLoud > 400)) C.spans.push({start: now, end: now});
        C.loud = true; C.lastLoud = now;
        if (C.spans.length) C.spans[C.spans.length - 1].end = now;
      } else {
        C.loud = false;
      }
    };
    if (C.echoGain) C.echo(C.echoGain);
  };

  // A speakerphone with no echo cancelling: what it hears goes back out.
  C.echo = (gain) => {
    C.echoGain = gain;
    if (!C.heard || !gain) return;
    const delay = C.ctx.createDelay(1.0); delay.delayTime.value = 0.15;
    const g = C.ctx.createGain(); g.gain.value = gain;
    C.heard.connect(delay); delay.connect(g); g.connect(C.dest);
  };

  C.offer = async () => {
    await C.pc.setLocalDescription(await C.pc.createOffer());
    await new Promise(r => {
      if (C.pc.iceGatheringState === 'complete') return r();
      C.pc.onicegatheringstatechange = () => C.pc.iceGatheringState === 'complete' && r();
      setTimeout(r, 3000);
    });
    return C.pc.localDescription.sdp;
  };
  C.accept = async (sdp) => C.pc.setRemoteDescription({type: 'answer', sdp});
  C.hangUp = () => C.pc.close();
  C.state = () => ({spans: C.spans, conn: C.pc.connectionState, now: Date.now()});
})();
"""

#: Asta's side: a page that takes "a microphone" the way Teams does, and answers.
ASTA_JS = r"""
window.__answer = async (sdp) => {
  const mic = await navigator.mediaDevices.getUserMedia({audio: true});
  const pc = new RTCPeerConnection();
  mic.getTracks().forEach(t => pc.addTrack(t, mic));
  await pc.setRemoteDescription({type: 'offer', sdp});
  await pc.setLocalDescription(await pc.createAnswer());
  await new Promise(r => {
    if (pc.iceGatheringState === 'complete') return r();
    pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && r();
    setTimeout(r, 3000);
  });
  window.__pc = pc;
  return pc.localDescription.sdp;
};
"""

_PAGE = "<!doctype html><html><body>{who}</body></html>"
VOICEMAIL = ("The person you are calling is not available. Please leave a message after "
             "the tone. When you've finished recording, hang up or press the pound key "
             "for more options.")


@dataclass
class Scenario:
    """What the colleague does, and what must be true of the call afterwards.

    Steps are (trigger, action) pairs. Triggers: "connected" (the call is up),
    "asta_done" (Asta spoke and then went quiet), "asta_speaking" (Asta started a
    line). Actions: {"say": text} with optional "after" seconds and "then"/"pause"
    for a two-part line, {"silence": seconds}, {"hang_up": True}.
    """
    name: str
    title: str
    steps: list[tuple[str, dict]]
    minutes: float = 2.0
    noise: float = 0.0
    echo: float = 0.0
    expect: dict = field(default_factory=dict)


SCENARIOS: list[Scenario] = [
    Scenario("quick-yes", "Hello over the greeting, a short yes, a goodbye", [
        ("connected", {"say": "Hello?", "after": 0.3}),
        ("asta_done", {"say": "Yeah, sure."}),
        ("asta_done", {"say": "Yes."}),
        ("asta_done", {"say": "Sounds good. Okay, bye!"}),
    ], expect={"greeting_within": 2.5, "reply_within": 2.5, "sound_within": 2.2,
               "greeting_whole": True, "asta_ends": True}),
    Scenario("interrupts", "Talks over Asta's second line with a question", [
        ("connected", {"say": "Hello?", "after": 0.3}),
        ("asta_done", {"say": "Yes, go ahead."}),
        ("asta_speaking", {"say": "Sorry, wait, who is this?", "after": 1.5}),
        ("asta_done", {"say": "Okay, got it. Bye."}),
    ], expect={"stops_within": 1.5, "reply_within": 2.5, "sound_within": 2.2}),
    Scenario("goes-quiet", "Answers once, then says nothing", [
        ("connected", {"say": "Hello?", "after": 0.3}),
        ("asta_done", {"say": "Yes."}),
        ("asta_done", {"silence": 40}),
    ], minutes=2.0, expect={"nudges": True, "reply_within": 2.5}),
    Scenario("voicemail", "Voicemail picks up", [
        ("connected", {"say": VOICEMAIL, "after": 0.2}),
        ("connected", {"silence": 20}),
    ], expect={"asta_silent": True}),
    Scenario("thinking-pause", "Pauses mid-sentence to think", [
        ("connected", {"say": "Hello?", "after": 0.3}),
        ("asta_done", {"say": "So what I think is", "pause": 1.1,
                       "then": "we should try it again tomorrow morning."}),
        ("asta_done", {"say": "Okay, bye."}),
    ], expect={"no_cut_in": True, "reply_within": 3.0}),
    Scenario("noisy-line", "A noisy line the whole way through", [
        ("connected", {"say": "Hello?", "after": 0.3}),
        ("asta_done", {"say": "Yes, go ahead."}),
        ("asta_done", {"say": "It sounds fine to me."}),
        ("asta_done", {"say": "Okay, bye."}),
    ], noise=0.02, expect={"reply_within": 3.0, "sound_within": 2.5}),
    Scenario("speakerphone-echo", "A speakerphone that sends Asta's voice back", [
        ("connected", {"say": "Hello?", "after": 0.3}),
        ("asta_done", {"say": "Yes."}),
        ("asta_done", {"say": "Sounds good."}),
        ("asta_done", {"say": "Okay, bye."}),
    ], echo=0.3, expect={"reply_within": 3.0, "no_self_talk": True}),
]


# --- running one ----------------------------------------------------------------------

#: Shorter than this is a listener's "mm-hm", not a turn — people wait it out.
BACKCHANNEL_MS = 700
#: Asta's own "mm-hm" / "mm." — shorter than any reply.
ACK_MS = 500


def _real(spans: list[dict]) -> list[dict]:
    return [s for s in spans if s["end"] - s["start"] >= BACKCHANNEL_MS]


async def _wait_asta(page, since: float, done: bool, timeout: float) -> float | None:
    """Wall-clock ms when Asta started (done=False) or finished (done=True) a real
    line after `since`; None on timeout. A lone "mm-hm" is not Asta's turn."""
    end = time.time() + timeout
    while time.time() < end:
        st = await page.evaluate("window.__colleague.state()")
        spans = [s for s in st["spans"] if s["start"] >= since]
        if spans:
            if not done:
                return spans[0]["start"]
            last = spans[-1]
            if _real(spans) and st["now"] - last["end"] >= 800:
                return last["end"]
        await asyncio.sleep(0.1)
    return None


async def _say(page, wav: bytes) -> float:
    return float(await page.evaluate("(b) => window.__colleague.say(b)",
                                     base64.b64encode(wav).decode()))


async def run(sc: Scenario, keep_dir: Path) -> dict:
    """One rehearsal. Returns the measurements and the verdict."""
    from playwright.async_api import async_playwright

    from . import call_rtc, conversation, meetings, store, voice
    tmp = Path(tempfile.mkdtemp(prefix=f"asta-rehearsal-{sc.name}-"))
    store.DB_PATH = tmp / "asta.db"
    store.init()
    who = "Riya Test"
    lines: dict[str, bytes] = {}

    async def line(text: str) -> bytes:
        if text not in lines:
            lines[text] = await voice.speak(text, profile="Asta (male)")
        return lines[text]

    for _, act in sc.steps:
        for key in ("say", "then"):
            if act.get(key):
                await line(act[key])

    timeline: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel=os.environ.get("ASTA_BROWSER_CHANNEL", "chrome") or None, headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream"])
        try:
            asta_ctx = await browser.new_context(permissions=["microphone"])
            await call_rtc.install(asta_ctx)
            asta_page = await asta_ctx.new_page()
            await asta_page.route("https://asta.test/**", lambda r: r.fulfill(
                status=200, content_type="text/html", body=_PAGE.format(who="Asta")))
            await asta_page.goto("https://asta.test/")
            await asta_page.add_script_tag(content=ASTA_JS)

            col_ctx = await browser.new_context(permissions=["microphone"])
            col_page = await col_ctx.new_page()
            await col_page.route("https://colleague.test/**", lambda r: r.fulfill(
                status=200, content_type="text/html", body=_PAGE.format(who=who)))
            await col_page.goto("https://colleague.test/")
            await col_page.add_script_tag(content=COLLEAGUE_JS)

            connected = asyncio.Event()

            async def call_person(name, video=False):
                offer = await col_page.evaluate("window.__colleague.offer()")
                answer = await asta_page.evaluate("(s) => window.__answer(s)", offer)
                await col_page.evaluate("(s) => window.__colleague.accept(s)", answer)
                meetings._CALL.clear()
                meetings._CALL.update(ctx=asta_ctx, page=asta_page, pw=None, rtc=True,
                                      url="rehearsal", joined_at=meetings._now(), captions=[],
                                      answered_at=0.0, speaks=True, who=name, mic_proven=1.0,
                                      log=call_rtc.recorder(f"rehearsal-{sc.name}"))
                store.kv_set("teams_in_call", f"call:{name}")
                connected.set()
                return name

            real_call_person = meetings.call_person
            meetings.call_person = call_person
            try:
                async def colleague() -> None:
                    await connected.wait()
                    await col_page.evaluate(f"window.__colleague.noise({sc.noise})")
                    if sc.echo:
                        await col_page.evaluate(f"window.__colleague.echo({sc.echo})")
                    mark = time.time() * 1000
                    timeline.append({"event": "connected", "at": mark})
                    for trigger, act in sc.steps:
                        if trigger == "asta_done":
                            done = await _wait_asta(col_page, mark, True, 40)
                            if done is None:
                                timeline.append({"event": "asta never finished", "at": time.time() * 1000})
                                return
                        elif trigger == "asta_speaking":
                            started = await _wait_asta(col_page, mark, False, 40)
                            if started is None:
                                return
                        await asyncio.sleep(act.get("after", 0.4))
                        if act.get("silence"):
                            timeline.append({"event": "silence", "at": time.time() * 1000,
                                             "for": act["silence"]})
                            await asyncio.sleep(act["silence"])
                            continue
                        if act.get("hang_up"):
                            await col_page.evaluate("window.__colleague.hangUp()")
                            return
                        start = time.time() * 1000
                        ended = await _say(col_page, await line(act["say"]))
                        if act.get("then"):
                            await asyncio.sleep(act.get("pause", 1.0))
                            timeline.append({"event": "paused", "at": ended})
                            ended = await _say(col_page, await line(act["then"]))
                        timeline.append({"event": "colleague", "text": act["say"] + (
                            " … " + act["then"] if act.get("then") else ""),
                                         "start": start, "end": ended})
                        mark = ended

                bot = asyncio.ensure_future(colleague())
                started = time.time()
                outcome = await asyncio.wait_for(conversation.converse(
                    who, "a quick rehearsal call", seconds=sc.minutes * 60,
                    agenda="check they can hear you; ask one or two easy questions"),
                    timeout=sc.minutes * 60 + 90)
                took = time.time() - started
                bot.cancel()
                heard = await col_page.evaluate("window.__colleague.state()")
            finally:
                meetings.call_person = real_call_person
        finally:
            with contextlib.suppress(Exception):
                await browser.close()

    log = sorted((tmp / "calls").glob("*.jsonl"))
    saying = _lines_said(log[-1]) if log else []
    report = _judge(sc, timeline, heard["spans"], outcome, took, saying)
    report["flight_recorder"] = str(log[-1]) if log else ""
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / f"{sc.name}.json").write_text(json.dumps(report, indent=1))
    return report


#: Asta's own "mm-hm" lines — a listener's noise, not the start of a reply.
_ACK_LINES = ("mm-hm", "mm")


def _lines_said(path: Path) -> list[tuple[float, str]]:
    """(wall-clock ms, text) of every line Asta started, from its flight recorder."""
    out = []
    with contextlib.suppress(Exception):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if "saying" in row:
                out.append((row["at"] * 1000, row["saying"]))
    return out


def _judge(sc: Scenario, timeline: list[dict], spans: list[dict], outcome: str,
           took: float, saying: list[tuple[float, str]] | None = None) -> dict:
    """Latencies and the verdict, from what the colleague's own ears measured —
    and, where Asta's own record says which line was which, from that."""
    connected = next((t["at"] for t in timeline if t["event"] == "connected"), None)
    said = [t for t in timeline if t["event"] == "colleague"]
    gaps, sounds = [], []
    for s in said:
        after = [sp for sp in spans if sp["start"] > s["end"] - 200]
        first_sp = after[0] if after else None
        sounds.append(round((first_sp["start"] - s["end"]) / 1000, 2) if first_sp else None)
        if saying:
            reply = next((at for at, text in saying if at > s["end"] - 200
                          and text.strip(" .!").lower() not in _ACK_LINES), None)
            gaps.append(round((reply - s["end"]) / 1000, 2) if reply else None)
            continue
        # The reply proper: past a listener's "mm-hm" if that came first.
        if first_sp and first_sp["end"] - first_sp["start"] < ACK_MS and len(after) > 1:
            first_sp = after[1]
        gaps.append(round((first_sp["start"] - s["end"]) / 1000, 2) if first_sp else None)
    first = spans[0]["start"] if spans else None
    fails: list[str] = []
    ex = sc.expect
    greeting_at = (round((first - connected) / 1000, 2)
                   if first is not None and connected is not None else None)
    if ex.get("asta_silent"):
        if spans:
            fails.append(f"spoke into a voicemail ({len(spans)} spans)")
    else:
        if greeting_at is None:
            fails.append("never spoke")
        elif ex.get("greeting_within") and greeting_at > ex["greeting_within"]:
            fails.append(f"greeting started {greeting_at}s after pick-up")
        replies = [g for g, s in zip(gaps, said) if s is not said[0] or sc.steps[0][0] != "connected"]
        late = [g for g in replies if g is None or (ex.get("reply_within") and g > ex["reply_within"])]
        # the last line is often a goodbye Asta closes on; missing reply there is fine
        if late and not (len(late) == 1 and late[-1] is None and said and "bye" in said[-1]["text"].lower()):
            fails.append(f"replies late or missing: {gaps}")
    if ex.get("asta_ends") and "Talked to" not in outcome:
        fails.append("the call did not end as a conversation")
    if ex.get("nudges") and "still there" not in outcome.lower():
        fails.append("never asked if they were still there")
    if ex.get("no_cut_in"):
        pause = next((t for t in timeline if t["event"] == "paused"), None)
        if pause and any(pause["at"] < sp["start"] < pause["at"] + 1100 for sp in _real(spans)):
            fails.append("cut in while they paused to think")
    if ex.get("stops_within"):
        inter = next((s for s in said if "who is this" in s["text"].lower()), None)
        if inter:
            talking = [sp for sp in spans if sp["start"] < inter["start"] < sp["end"]]
            if talking and (talking[0]["end"] - inter["start"]) / 1000 > ex["stops_within"]:
                fails.append(f"kept talking {(talking[0]['end'] - inter['start']) / 1000:.1f}s over them")
    if ex.get("no_self_talk") and outcome.lower().count("didn't catch") > 1:
        fails.append("answered its own echo")
    firsts = [g for g, s in zip(sounds[1:], said[1:]) if g is not None]
    if ex.get("sound_within") and any(g > ex["sound_within"] for g in firsts):
        fails.append(f"silence after they stopped: {sounds}")
    return {"scenario": sc.name, "title": sc.title, "passed": not fails, "fails": fails,
            "greeting_after": greeting_at, "reply_gaps": gaps, "first_sound": sounds,
            "asta_spans": len(spans),
            "seconds": round(took, 1), "outcome": outcome[:1500]}


async def main(names: list[str]) -> int:
    from . import store
    chosen = [s for s in SCENARIOS if not names or s.name in names]
    real_db = store.DB_PATH
    keep = Path(real_db).parent / "calls" / "rehearsals" / time.strftime("%Y%m%d-%H%M%S")
    failed = 0
    results: list[dict] = []
    for sc in chosen:
        try:
            r = await run(sc, keep)
        except Exception as exc:                                  # noqa: BLE001
            r = {"scenario": sc.name, "passed": False, "fails": [f"crashed: {type(exc).__name__}: {exc}"]}
        results.append(r)
        failed += 0 if r["passed"] else 1
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{mark} {sc.name:18} greeting {r.get('greeting_after')}s  first sound {r.get('first_sound')}  "
              f"reply {r.get('reply_gaps')}  {'; '.join(r.get('fails', []))}", flush=True)
    print(f"\n{len(chosen) - failed}/{len(chosen)} rehearsals passed · reports in {keep}")
    store.DB_PATH = real_db              # each run() pointed it at its own sandbox
    _write_latest(keep, results)
    return 1 if failed else 0


def _latest_file() -> Path:
    from . import store
    return Path(store.DB_PATH).parent / "calls" / "rehearsals" / "latest.json"


def _write_latest(keep: Path, results: list[dict]) -> None:
    with contextlib.suppress(Exception):
        _latest_file().write_text(json.dumps({
            "at": time.time(), "dir": str(keep),
            "results": [{"scenario": r.get("scenario"), "passed": r.get("passed"),
                         "fails": r.get("fails", [])} for r in results]}, indent=1))


def latest_failures(max_age_hours: float = 48) -> str:
    """'' when the last rehearsal passed (or is too old to speak for today)."""
    try:
        data = json.loads(_latest_file().read_text())
    except (OSError, ValueError):
        return ""
    if time.time() - float(data.get("at") or 0) > max_age_hours * 3600:
        return ""
    bad = [r["scenario"] for r in data.get("results", []) if not r.get("passed")]
    return f"{', '.join(bad)} ({len(bad)}/{len(data.get('results', []))})" if bad else ""


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
