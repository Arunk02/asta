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

Same module for both brains rather than a copy in each. The two drivers had
byte-identical pump loops and drifted anyway; a rule that lives in one place is
the only kind that stays consistent.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field


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

    def __init__(self, stop: Stop, tail_chars: int = 1200):
        self.stop = stop
        self.partial = stop.partial
        tail = self.partial[-tail_chars:] if self.partial else ""
        message = stop.why()
        if tail:
            message += f"\n\nIt got this far:\n{tail}"
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
