"""One simulated working day — the measure he actually feels.

A scenario proves a rule. A day proves the BALANCE: how many times Asta
interrupted him, how much of it was worth an interrupt, and what it missed while
being quiet. On 1 September the real number was 156 pushes; the target in the
plan is 20 with nothing missed, and no single scenario can show whether that
holds, because the failure is in the aggregate.

The day is generated from a seed, so it is the same day every time until the
generator changes: ~150 events across nine working hours — colleagues asking and
chatting, a ticket feed, mail, CI and PR movements, and Arun himself. Each event
carries what SHOULD happen (`needs` = he must hear about it, `fyi` = it can
wait, `noise` = neither), which is what makes a miss countable.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from . import world as W

PEOPLE = ["Vinish Kumar", "Priya Nair", "Rahul Verma", "Meera Iyer", "Harini S"]
TEAM_CHAT = "Team Booking and AP"

ASKS = [
    "can you check why STF is not done for this one",
    "is the schema change merged in release 2.2.4",
    "did the topic refresh run for uat",
    "the consumer looks stuck in prod, can you look",
    "can you share the PR link for the mapping change",
]
FYI = [
    "deploying the release build now",
    "I merged the doc update",
    "standup moved to 10:15 tomorrow",
    "adding the new avro field on my side today",
]
NOISE = [
    "thanks!", "okay", "sure will do", "😄", "ack",
    "shall we join here now?", "sorry i was in a call",
]
TICKETS = [
    "Incident INC{n} has been assigned to group OH - TELIKOS - L2. Priority: 3 - Medium",
    "Change CHG{n} scheduled for this weekend",
]
HIS = [
    "any updates for me",
    "what is pending on my side",
    "raise the PR for the booking repo",
    "approve task 1",
]


@dataclass
class Event:
    at: float
    kind: str                 # colleague | ticket | his | ci
    who: str
    text: str
    expect: str               # needs | fyi | noise
    chat: str = ""
    source: str = "teams-chat"


@dataclass
class DayResult:
    events: list[Event] = field(default_factory=list)
    pushes: list[dict] = field(default_factory=list)
    missed: list[Event] = field(default_factory=list)
    false_interrupts: list[str] = field(default_factory=list)
    investigations: int = 0
    brain_calls: int = 0
    seconds: float = 0.0


def generate(seed: int = 20260911, count: int = 150) -> list[Event]:
    rng = random.Random(seed)
    day0 = time.time() - 9 * 3600
    out: list[Event] = []
    for i in range(count):
        at = day0 + i * (9 * 3600 / count)
        roll = rng.random()
        if roll < 0.18:
            out.append(Event(at, "colleague", rng.choice(PEOPLE), rng.choice(ASKS), "needs"))
        elif roll < 0.40:
            out.append(Event(at, "colleague", rng.choice(PEOPLE), rng.choice(FYI), "fyi",
                             chat=TEAM_CHAT))
        elif roll < 0.72:
            out.append(Event(at, "colleague", rng.choice(PEOPLE), rng.choice(NOISE), "noise",
                             chat=TEAM_CHAT))
        elif roll < 0.86:
            out.append(Event(at, "ticket", "IT Service Desk",
                             rng.choice(TICKETS).format(n=rng.randrange(9_000_000, 9_999_999)),
                             "noise", source="outlook"))
        elif roll < 0.95:
            out.append(Event(at, "his", "Arun", rng.choice(HIS), "needs"))
        else:
            out.append(Event(at, "ci", "github", "CI red on the PR for the mapping change",
                             "needs", source="ci"))
    return out


async def run(seed: int = 20260911, count: int = 150, batch: int = 6) -> DayResult:
    """Play the day through the REAL intake and count what he would have felt.

    Teams messages go through `chat_watch.sweep`, mail through
    `outlook._push_mail` — the actual paths, with their batching, their triage
    and their attention ledger. Anything less measures the harness: a day driven
    straight at `responder.respond` reports 22 missed items and 4 pushes,
    because the code that decides what reaches his phone was never called.
    """
    import contextlib
    from app import chat_watch, main, notify, outlook
    t0 = time.monotonic()
    world = W.World()
    world.install(chat_brain=W.ScriptedBrain(
        [W.BrainReply(text="Nothing outstanding on your side.")] * 60, world, "chat"),
        task_brain=W.ScriptedBrain([W.BrainReply(text="PLAN READY")] * 40, world, "task"))
    world.assert_sandboxed()
    res = DayResult(events=generate(seed, count))
    conv = W.new_conversation(workspace="booking")
    sink = W.Sink(world)
    patch = W.Patcher()
    pending: dict[str, list[dict]] = {}
    mail: list[dict] = []

    patch.set(chat_watch, "candidates", _async(lambda: list(pending)))
    patch.set(chat_watch, "new_in", _async(lambda chat, advance=True: pending.pop(chat, [])))
    patch.set(chat_watch, "answered_by_him", lambda chat, m: False)
    patch.set(chat_watch, "_people_he_talks_to", lambda: list(PEOPLE))

    async def flush() -> None:
        if pending:
            await chat_watch.sweep(notify.notify)
        if mail:
            # `_push_mail` takes the notify MODULE (it calls notify.notify
            # inside), where `sweep` takes the function. Not a nicety: passing
            # the wrong one is an AttributeError halfway through a real batch.
            await outlook._push_mail(notify, list(mail))
            mail.clear()
        await W.settle(timeout=8)

    try:
        for i, ev in enumerate(res.events):
            before = len(world.pushes)
            if ev.kind == "colleague":
                chat = ev.chat or ev.who
                pending.setdefault(chat, []).append(
                    {"sender": ev.who, "text": ev.text, "sent_at": ev.at})
            elif ev.kind == "ticket":
                mail.append({"sender": ev.who, "subject": ev.text[:60],
                             "preview": ev.text, "unread": True, "when": "now"})
            elif ev.kind == "his":
                await flush()
                turn = await main._dispatch(conv, ev.text, sink, "whatsapp")
                if turn is not None:
                    with contextlib.suppress(Exception):
                        await turn
                await W.settle(timeout=8)
            else:
                await notify.notify(ev.text, "ci", urgency="direct")
            if i % batch == batch - 1:
                await flush()
            if ev.expect == "noise" and len(world.pushes) > before:
                res.false_interrupts.append(f"pushed noise: {ev.text[:50]}")
        await flush()
        res.pushes = list(world.pushes)
        res.brain_calls = len(world.brain_calls)
        res.investigations = sum(1 for b in world.brain_calls if b["kind"] == "task")
        # A "needs" item counts as reached if his phone or the chat mentions it.
        said = world.everything_said().lower()
        for ev in res.events:
            if ev.expect == "needs" and ev.kind == "colleague":
                head = " ".join(ev.text.split()[:4]).lower()
                if head not in said:
                    res.missed.append(ev)
    finally:
        patch.undo()
        world.uninstall()
    res.seconds = round(time.monotonic() - t0, 1)
    return res


def _async(fn):
    async def call(*a, **k):
        return fn(*a, **k)
    return call


def report(res: DayResult) -> str:
    needs = [e for e in res.events if e.expect == "needs"]
    return "\n".join([
        f"A day in his life · {len(res.events)} events · {res.seconds}s",
        f"  pushes to his phone   {len(res.pushes):3}   (target ≤ 20)",
        f"  things he needed      {len(needs):3}   missed: {len(res.missed)}",
        f"  interrupted for noise {len(res.false_interrupts):3}",
        f"  investigations        {res.investigations:3}",
        f"  brain calls           {res.brain_calls:3}",
    ])


def summary(res: DayResult) -> dict:
    return {"events": len(res.events), "pushes": len(res.pushes),
            "missed": len(res.missed), "false_interrupts": len(res.false_interrupts),
            "investigations": res.investigations, "brain_calls": res.brain_calls}
