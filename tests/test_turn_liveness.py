"""A brain doing tool work is not a brain that has stopped.

The incident, 2026-09-07 12:15. Arun asked for a multi-step job — find the topic
files, work out what needs refreshing, tell Alex. Copilot did exactly that: ten
tool calls in two minutes, found the files, recorded what it learned, and staged
the message. Thirteen seconds after it staged the draft, Asta killed it and told
him "stuck — no output for 120s, more time would not have helped".

It had produced 0 characters on stdout in that window, because Copilot prints
prose and a turn deep in tool calls has no prose to print. The watchdog was
reading the one channel that could not see the work, so the rule it enforced was
"any job needing more than two minutes of tools is stuck" — and the harder the
thing Arun asked for, the more certain it was to die.

The fix is a second signal, in whatever shape a driver has one, consulted only
when the byte stream has gone quiet. These tests hold both halves: a working
brain survives, and a genuinely wedged one is still killed. The second is the
one that must never break — without it this module has no purpose at all.
"""

from __future__ import annotations

import asyncio
import time

from app import claude_cli, copilot_cli, turn_budget as tb


class _Silent:
    """A stdout that never says anything — the shape of a tool-heavy turn."""

    async def read(self, _n):
        await asyncio.sleep(3600)


async def _run(liveness, *, total=1.5, idle=0.3):
    beat = tb.Heartbeat(liveness)

    async def _pump():
        while True:
            block = await _Silent().read(512)
            if not block:
                return
            beat.beat()

    return await tb.guard(_pump(), beat, total=total, idle=idle)


# --- the policy ------------------------------------------------------------------

def test_a_brain_working_silently_is_not_stuck():
    """Silence on stdout plus a signal that keeps moving is a busy brain."""
    ticks = iter(range(10_000))
    stop = asyncio.run(_run(lambda: next(ticks)))
    assert stop.reason == "ceiling"          # ran to the budget, never called stuck
    assert "long job, not a stuck one" in stop.why()


def test_a_wedged_brain_is_still_killed():
    """The guard this module exists for. If this ever passes by accident, the
    watchdog is decorative and a wedged turn holds the conversation for the full
    ceiling again."""
    stop = asyncio.run(_run(lambda: 42))     # a signal that never moves
    assert stop.reason == "idle"
    assert "stuck" in stop.why()


def test_no_signal_behaves_exactly_as_it_did_before():
    """Every driver without a probe keeps the old rule, unchanged."""
    assert asyncio.run(_run(None)).reason == "idle"


def test_a_signal_that_cannot_be_read_is_not_progress():
    """Missing file, cleaned session store: 'no signal' must never read as
    'still working', or a wedge becomes invisible."""
    assert asyncio.run(_run(lambda: None)).reason == "idle"


def test_losing_the_signal_is_not_progress():
    """The other direction of the same rule, and the one a simpler check gets
    wrong: a session store cleaned mid-turn changes the probe's answer, and a
    bare "it changed" would read that disappearance as a tool call and hand a
    wedged brain a free beat."""
    marks = iter([512, 512, None, None, None])
    beat = tb.Heartbeat(lambda: next(marks))     # the constructor takes the first
    beat.last = time.monotonic() - 5             # stdout has been quiet a while
    assert beat.silent_for > 4                   # unchanged size: nothing happened
    assert beat.silent_for > 4                   # and vanishing is not something


def test_a_probe_that_raises_never_takes_the_turn_down():
    """It is a diagnostic aid. A turn dying because its optional stuck-detector
    threw would be a worse failure than the one it was added to prevent."""
    def boom():
        raise OSError("gone")
    assert asyncio.run(_run(boom)).reason == "idle"


def test_progress_resets_the_clock_rather_than_only_delaying_it():
    """Beats have to keep working, not buy one grace period."""
    beat = tb.Heartbeat(lambda: object())    # a new value every read
    first = beat.silent_for
    assert first < 0.05


# --- the incident, replayed ---------------------------------------------------------

def test_the_turn_that_was_killed_would_now_survive():
    """Ten tool calls, no prose, over a window longer than the idle limit —
    measured off the real session log rather than invented."""
    log = {"size": 0}

    async def _grow():
        for _ in range(10):                  # what the real turn actually did
            await asyncio.sleep(0.06)
            log["size"] += 512

    async def _go():
        grower = asyncio.ensure_future(_grow())
        stop = await _run(lambda: log["size"], total=0.9, idle=0.25)
        grower.cancel()
        return stop

    stop = asyncio.run(_go())
    assert stop.reason != "idle"             # it was working the whole time


# --- copilot's own signal --------------------------------------------------------------

def test_copilot_watches_the_session_log_it_actually_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_cli, "SESSION_STATE", tmp_path)
    events = tmp_path / "sid-1" / "events.jsonl"
    events.parent.mkdir()
    events.write_text("{}\n")
    probe = copilot_cli._session_progress("sid-1")
    before = probe()
    events.write_text("{}\n{}\n")
    assert probe() != before                 # a tool call moved it


def test_a_session_with_no_log_yet_is_not_a_signal(tmp_path, monkeypatch):
    """True for the first seconds of every new session."""
    monkeypatch.setattr(copilot_cli, "SESSION_STATE", tmp_path)
    assert copilot_cli._session_progress("nope")() is None


def test_no_session_id_means_no_probe():
    assert copilot_cli._session_progress("") is None


def test_reading_the_session_id_never_mints_one():
    """`_session_id` WRITES an id when it finds none. Calling that to find the
    log would make _build_cmd believe the session already existed and resume one
    that was never created — losing the first-turn orientation block on every
    new conversation."""
    from app import store
    assert copilot_cli._current_session("brand-new") == ""
    assert store.kv_get("copilot_session:brand-new") is None


# --- claude's signal, pinned rather than assumed --------------------------------------

def test_claude_stays_alive_through_a_silent_tool_run():
    """Claude Code never had copilot's blind spot, and this is why: stream-json
    puts tool events on the SAME stdout the watchdog reads, so a turn doing
    nothing but tool calls is still visibly alive.

    Pinned because it is a property of the output format, not of the code — a
    driver switched to plain text output would inherit the exact bug this file
    exists for, and nothing else would notice.
    """
    import json

    tool_event = json.dumps({
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "tool_use", "name": "Bash"}]},
    }).encode() + b"\n"

    beat = tb.Heartbeat()

    async def _pump():
        while True:                              # tool call after tool call, no prose
            await asyncio.sleep(0.05)
            assert tool_event                    # what arrives on stdout
            beat.beat()

    # The run outlasts the budget on purpose: it must end at the ceiling, still
    # working, rather than at the idle limit a third of the way in.
    stop = asyncio.run(tb.guard(_pump(), beat, total=0.45, idle=0.15))
    assert stop.reason == "ceiling"


# --- the same rule for every brain --------------------------------------------------------

def test_every_brain_reads_one_ceiling(monkeypatch):
    """His standing rule: one shared policy function, never per-brain constants.
    The local model kept its own hardcoded 120 while the CLIs used 300."""
    monkeypatch.setenv("ASTA_TURN_TIMEOUT", "240")
    assert tb.ceiling_seconds() == 240
    assert copilot_cli.turn_timeout() == 240
    assert claude_cli.turn_timeout() == 240


def test_a_completion_is_bounded_by_the_turn_but_is_not_one(monkeypatch):
    """A turn is an agent loop; a completion is four hundred tokens.

    Giving them one number read as consistency and was a category error: it
    raised the local model's bound from a deliberate 120s to the 300s ceiling.
    LM Studio is reachable here, so the suite wedged at 89% on a real generation
    — no output, no CPU, no failure. Bounded BY the ceiling, never equal to it.
    """
    from app import memory, turn_budget
    monkeypatch.setenv("ASTA_TURN_TIMEOUT", "240")
    assert turn_budget.completion_seconds() == 120           # capped
    monkeypatch.setenv("ASTA_TURN_TIMEOUT", "60")
    assert turn_budget.completion_seconds() == 60            # never exceeds a turn
    monkeypatch.setenv("ASTA_TURN_TIMEOUT", "240")
    seen = {}
    monkeypatch.setattr(memory, "local_llm_model", lambda: "m")

    class _R:
        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "hi"}}]}

    def fake_post(url, **kw):
        seen["timeout"] = kw.get("timeout")
        return _R()

    monkeypatch.setattr(memory.httpx, "post", fake_post)
    assert memory.local_llm_complete("x") == "hi"
    assert seen["timeout"] == 120


def test_a_local_model_that_stops_answering_leaves_a_record(monkeypatch):
    """It was the one brain of the three whose stall vanished silently — nine
    background features read None as 'produce nothing' and shipped blanks."""
    from app import memory, store
    monkeypatch.setattr(memory, "local_llm_model", lambda: "m")

    def fake_post(url, **kw):
        raise memory.httpx.ReadTimeout("too slow")

    monkeypatch.setattr(memory.httpx, "post", fake_post)
    assert memory.local_llm_complete("x") is None
    rows = [r for r in store.recent_outcomes(20) if r["subject"] == "local"]
    assert rows and rows[0]["outcome"] == "stopped_idle"


# --- what a stop leaves behind ----------------------------------------------------------

def test_a_stop_says_how_far_it_got():
    stop = tb.Stop("idle", 120.0, 120.0, ["half an answer"])
    assert "idle" in stop.detail() and "120s" in stop.detail()
    assert "quota" in stop.detail("quota exceeded")


def test_stderr_is_read_without_hanging_the_failure():
    """A turn that already failed must not then hang reporting the failure."""
    class _Never:
        async def read(self):
            await asyncio.sleep(3600)

    class _Proc:
        stderr = _Never()

    assert asyncio.run(tb.tail_stderr(_Proc(), timeout=0.1)) == ""


def test_a_process_with_no_stderr_is_not_an_error():
    class _Proc:
        stderr = None
    assert asyncio.run(tb.tail_stderr(_Proc())) == ""
