"""Voice-mode benchmark: the real talker, the real Asta voice, a fake worker.

Run:  .venv/bin/python scripts/voice_bench.py
Nothing outward happens: the worker is a stand-in that waits and returns a
canned finding, and the "helper" is a recorder. What is measured is what he
hears and when — from the moment his sentence is known (speech-to-text done)
to the first audio being ready, plus what was said, what was left unsaid,
and how many jobs started.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store, voice_mode as vm, voice_talker as vt  # noqa: E402

vm._JOBS.clear()

store.init()


class Ear:
    """The menu-bar app, as a recorder of what would be played and when."""

    def __init__(self):
        self.lines: list[tuple[float, str]] = []

    async def send_text(self, text):
        m = json.loads(text)
        if m.get("type") == "say":
            self.lines.append((time.time(), m["text"]))


WORK: dict[str, tuple[float, str]] = {
    "uat": (6.0, "Checked Loki in UAT for H69LMCN6KZY over 72h: no hits. It never reached UAT."),
    "pre-prod": (4.0, "Pre-prod: booking H69LMCN6KZY was created 09:12 and is in RFP."),
    "draft": (3.0, "Drafted for Vinish: 'bro can u review booking PR 1429 when free'."),
}
started_jobs: list[str] = []


async def fake_dispatch(conv, text, sink, channel):
    started_jobs.append(text)
    key = next((k for k in WORK if k in text.lower()), "")
    delay, finding = WORK.get(key, (3.0, f"Looked into it: {text}. Nothing unusual."))

    async def work():
        await asyncio.sleep(delay)
        await sink.send({"type": "delta", "text": finding})
    return asyncio.ensure_future(work())


async def say_to(ear, text, wait=0.3):
    t0 = time.time()
    n = len(ear.lines)
    out = await vm.handle(text)
    await asyncio.sleep(wait)          # a handed-on job registers on the next tick
    new = ear.lines[n:]
    first = (new[0][0] - t0) if new else None
    return out, first, [l for _, l in new]


async def main_bench():
    ear = Ear()
    vm._STATE.update(helper=ear)
    await vm.set_mode(speaker=True, mic=True, why="bench")
    main._dispatch = fake_dispatch
    t = time.time()
    await vt.mind()
    await vm.warm_acks()
    rows = [("warm-up (talker + briefing + acks)", f"{time.time() - t:.1f}s", "", "info")]

    def row(name, first, said, ok, target):
        f = f"{first:.2f}s" if first is not None else "—"
        rows.append((name, f, " | ".join(said)[:90], "PASS" if ok else f"FAIL ({target})"))

    for q in ["Asta, what's pending with my PRs?", "which of my PRs is waiting on Vinish?",
              "मेरे pull request का क्या हाल है?"]:
        n0 = len(started_jobs)
        out, first, said = await say_to(ear, q)
        row(f"status: {q[:34]}", first, said,
            first is not None and first <= 2.5 and len(started_jobs) == n0, "≤2.5s, no job")

    for q in ["yeah", "Go go go go go", "haha okay cool, I'll send you the deck after lunch",
              "one sec, I'm on a call", "hmm let me think", "no no, I was telling Vinish"]:
        out, first, said = await say_to(ear, q)
        row(f"quiet: {q[:34]}", first, said, not said and out["did"] in ("not_for_asta", "ignored"),
            "silent")

    out, first, said = await say_to(ear, "Asta, listen to me")
    row("call: Asta, listen to me", first, said, said == ["I'm listening."], "\"I'm listening.\"")

    n0 = len(started_jobs)
    out, first, said = await say_to(ear, "check if booking H69LMCN6KZY reached UAT")
    row("work: check H69 in UAT", first, said,
        first is not None and first <= 2.5 and len(said) == 1 and len(started_jobs) == n0 + 1,
        "≤2.5s, one ack, one job")

    for i in range(2):
        out, first, said = await say_to(ear, "check if booking H69LMCN6KZY reached UAT")
        row(f"repeat #{i + 2} of the same ask", first, said,
            len(started_jobs) == n0 + 1 and all("still" in s.lower() for s in said),
            "no new job, 'still on that'")

    out, first, said = await say_to(ear, "also check pre-prod for that booking")
    row("parallel: also check pre-prod", first, said, len(started_jobs) == n0 + 2,
        "a second job, alongside")

    await asyncio.sleep(12)
    everything = [l.lower() for _, l in ear.lines]
    uat = [l for l in everything if "no hits" in l and "uat" in l]
    pre = [l for l in everything if "09:12" in l]
    row("results: UAT and pre-prod, each said once", None, uat[:1] + pre[:1],
        len(uat) == 1 and len(pre) == 1, "each finding said exactly once")

    # He talks over the answer: barge arrives once the first line is out.
    vm._STATE["barged_at"] = 0.0
    n = len(ear.lines)

    async def barge_after_first_line():
        while len(ear.lines) == n:
            await asyncio.sleep(0.05)
        vm._STATE["barged_at"] = time.time()
    watcher = asyncio.ensure_future(barge_after_first_line())
    out, first, said = await say_to(ear, "what's pending with my PRs and what is scheduled?", wait=3)
    watcher.cancel()
    row("talked over: only the first line", first, said, len(said) <= 1, "stops after the line in flight")
    vm._STATE["barged_at"] = 0.0

    await vt.close()
    width = max(len(r[0]) for r in rows)
    print(f"\n{'scenario'.ljust(width)}  first   result  said")
    for name, first, said, verdict in rows:
        print(f"{name.ljust(width)}  {first:>6}  {verdict:<6}  {said}")
    fails = [r for r in rows if r[3].startswith("FAIL")]
    print(f"\n{len(rows) - 1 - len(fails)}/{len(rows) - 1} passed")


if __name__ == "__main__":
    asyncio.run(main_bench())
