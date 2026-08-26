"""Why a turn stopped — not just that it did.

`RuntimeError: Copilot CLI turn timed out after 300s` is true and useless. It
leaves the one question that matters unanswered: did it finish, is it still
working, or is it wedged? Arun then has to open the repo and guess.

Two things were wrong, and they compound.

**The output was thrown away.** Both CLI drivers accumulate every chunk the brain
streams — every file it opened, every edit it narrated — and the timeout branch
discarded all of it to raise a one-line error. The evidence of what happened
existed, in memory, and the error path deleted it.

**The budget was total elapsed time.** A brain streaming steadily and a brain
silent since second three were killed at the same moment with the same sentence.
Those are opposite situations: one needs more time, the other needs killing. The
code never looked at WHEN output last arrived, so it could not tell them apart.

So a turn now stops for a named reason:

    done     the brain finished
    idle     nothing for IDLE_SECONDS — wedged, and more time will not help
    ceiling  still producing output when the budget ran out — a long job, not a
             stuck one, and the right response is to resume rather than retry

An idle stop then splits once more, because two very different things look
identical to a clock. A brain that wedged mid-edit has said nothing complete; a
brain that answered in full and then sat waiting on a twelve-minute CI run has.
`Stop.answered()` separates them, and only the first is a failure — see there.

Same module for both brains rather than a copy in each. The two drivers had
byte-identical pump loops and drifted anyway; a rule that lives in one place is
the only kind that stays consistent.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import dataclass, field

#: Output that actually ENDS — sentence punctuation at the very end, allowing the
#: markdown and quoting that usually trails it. Anchored, so a full stop in the
#: MIDDLE does not count: a turn killed mid-sentence, or one that answered and
#: then began narrating a tool call, fails this. That is the whole point of it.
_FINISHED = re.compile(r"[.!?\u2026][\"\'\u2019\u201d)\]`*_]*$")


def idle_seconds() -> int:
    """Silence long enough to mean wedged rather than thinking.

    Deliberately generous. A CLI brain compiling a multi-module Maven build, or
    waiting on a slow MCP call, can legitimately say nothing for a while — and
    calling that stuck would kill work that was about to succeed. What it is NOT
    generous enough to allow is the failure this exists to catch: a process that
    has stopped doing anything at all and holds the turn to the full ceiling.
    """
    try:
        return max(30, int(os.environ.get("ASTA_TURN_IDLE", "120")))
    except ValueError:
        return 120


@dataclass
class Stop:
    """How a turn ended, with enough to say something useful about it."""
    reason: str                     # done | idle | ceiling
    elapsed: float = 0.0            # seconds the turn ran
    silent_for: float = 0.0         # seconds since the last output
    chunks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reason == "done"

    @property
    def partial(self) -> str:
        return "".join(self.chunks).strip()

    def answered(self, min_chars: int = 80) -> bool:
        """Did it finish saying something before it went quiet?

        A brain that streamed a complete answer and THEN fell silent is not
        wedged. It has said what it had to say and is waiting on something
        outside itself — a CI run it promised to watch, a build, a colleague.
        Reporting that as "stuck, more time would not have helped" turned a
        perfectly good answer into a warning, and then reprinted the answer
        underneath the warning, so Arun paid for the same paragraphs twice and
        still could not tell whether the work had finished.

        Deliberately conservative, because the failure it must not mask is a
        brain that wedged halfway through an edit. Three things have to hold:
        the stop was idle (a ceiling stop really was cut short), the body is
        substantial, and the body ENDS in sentence punctuation. A turn killed
        mid-sentence, or one that answered and then began narrating a tool call,
        fails the last of those and is still reported as stopped.

        `min_chars` is calibrated against real traffic rather than picked. The
        complete answers in the messages that prompted this run 127 and 166
        characters; the dangerous near-miss — a brain announcing intent and THEN
        wedging ("I'll look at the failing test now.") — runs 6 to 45. Eighty
        sits in the gap. A first guess of 160 was measured against his actual
        screenshot and rejected the very message this exists to rescue.
        """
        if self.reason != "idle":
            return False
        body = self.partial
        # An earlier version walked to the last line before matching. The regex
        # is anchored and `partial` is already stripped, so it was doing that
        # anyway — mutation testing showed removing the walk changed nothing,
        # which is the only reason to know it was there for no reason.
        return len(body) >= min_chars and bool(_FINISHED.search(body))

    def why(self) -> str:
        """One line Arun can act on, which is the whole point of the split."""
        mins = self.elapsed / 60
        if self.reason == "idle":
            return (f"stuck — no output for {int(self.silent_for)}s, "
                    f"{mins:.1f} min into the turn. More time would not have helped, "
                    f"so I stopped it.")
        if self.reason == "ceiling":
            return (f"still working when the {mins:.0f} min budget ran out — this is a "
                    f"long job, not a stuck one. Resuming continues it; retrying "
                    f"starts again from nothing.")
        return "finished"


async def drain(stream, on_delta=None, *, total: float, idle: float | None = None) -> Stop:
    """Read a process's stdout until EOF, silence, or the ceiling.

    Returns rather than raises, because the caller needs the partial output and
    the reason — and an exception that carries neither is what made the original
    message useless.
    """
    if idle is None:
        idle = idle_seconds()
    chunks: list[str] = []
    started = time.monotonic()
    last = started
    while True:
        now = time.monotonic()
        left_total = total - (now - started)
        if left_total <= 0:
            return Stop("ceiling", now - started, now - last, chunks)
        # Wake at whichever limit comes first, so silence is noticed promptly
        # instead of at the end of a budget the brain was never going to use.
        try:
            chunk = await asyncio.wait_for(stream.read(512),
                                           timeout=min(left_total, idle))
        except (TimeoutError, asyncio.TimeoutError):
            now = time.monotonic()
            if now - started >= total:
                return Stop("ceiling", now - started, now - last, chunks)
            return Stop("idle", now - started, now - last, chunks)
        if not chunk:
            now = time.monotonic()
            return Stop("done", now - started, now - last, chunks)
        last = time.monotonic()
        text = chunk.decode(errors="replace")
        chunks.append(text)
        if on_delta:
            await on_delta(text)


class TurnStopped(RuntimeError):
    """A turn that did not finish, carrying what it managed to do.

    The partial output rides on the exception because every caller that reports
    this to Arun needs it, and the alternative — reconstructing it — is not
    possible once the process is killed.
    """

    def __init__(self, stop: Stop, tail_chars: int = 1200,
                 already_shown: bool = False):
        """`already_shown` — the caller streamed this text to Arun as it arrived.

        Repeating it under "It got this far:" is then pure duplication: he reads
        the same three paragraphs twice on a phone, and pays for them twice on
        the way out. What he still needs is the SIZE of what he already saw, so
        he can tell "it stopped having done nothing" from "it stopped after a
        full answer" without scrolling.
        """
        self.stop = stop
        self.partial = stop.partial
        message = stop.why()
        if self.partial and already_shown:
            message += (f"\n\n(The {len(self.partial)} characters above are what it "
                        f"produced — not repeated here.)")
        elif self.partial:
            message += f"\n\nIt got this far:\n{self.partial[-tail_chars:]}"
        super().__init__(message)


class Heartbeat:
    """A "still alive" marker for a pump that is not a plain byte stream.

    `claude_cli` parses NDJSON events rather than raw chunks, so it cannot use
    `drain`. It can still say when it last saw something, and that is the whole
    signal the idle/ceiling split needs. Keeping the POLICY here and letting each
    driver report activity in its own shape is the difference between one rule and
    two implementations that agree until they quietly do not.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.last = self.started

    def beat(self) -> None:
        self.last = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def silent_for(self) -> float:
        return time.monotonic() - self.last


async def guard(pump, beat: Heartbeat, *, total: float,
                idle: float | None = None) -> Stop:
    """Run `pump` until it finishes, goes silent, or runs out of budget.

    Returns a Stop; the caller kills the process and decides what to report. The
    pump is cancelled on either limit, so nothing keeps running behind the answer.
    """
    if idle is None:
        idle = idle_seconds()
    task = asyncio.ensure_future(pump)
    try:
        while True:
            if task.done():
                await task                      # surface any real exception
                return Stop("done", beat.elapsed, beat.silent_for)
            if beat.elapsed >= total:
                return Stop("ceiling", beat.elapsed, beat.silent_for)
            if beat.silent_for >= idle:
                return Stop("idle", beat.elapsed, beat.silent_for)
            # Wake at whichever limit lands first, never later than a second, so
            # a wedge is noticed promptly rather than at the end of the ceiling.
            nap = min(1.0, max(0.05, total - beat.elapsed), max(0.05, idle - beat.silent_for))
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=nap)
            except (TimeoutError, asyncio.TimeoutError):
                continue
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
