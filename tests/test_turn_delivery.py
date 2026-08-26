"""What the chat layer actually gets back when a turn goes quiet.

`turn_budget` decides what a stop MEANS; these tests check the two drivers act
on it — that a brain which answered and then waited on a CI run hands back its
answer instead of raising, and that a genuine wedge still raises.
"""

from __future__ import annotations

import asyncio

import pytest

from app import copilot_cli, store, turn_budget as tb


class _Proc:
    """A subprocess whose stdout delivers a plan, then falls silent for ever."""

    def __init__(self, plan):
        self.stdout = _Stdout(plan)
        self.stderr = None
        self.killed = False
        self.returncode = 0

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


class _Stdout:
    def __init__(self, plan):
        self._plan = list(plan)

    async def read(self, _n):
        if not self._plan:
            await asyncio.sleep(3600)
        delay, data = self._plan.pop(0)
        await asyncio.sleep(delay)
        return data


@pytest.fixture
def _copilot(monkeypatch):
    """A Copilot CLI that exists, needs no prefetch, and runs our fake process."""
    holder = {}

    def _spawn(plan):
        async def _exec(*_a, **_k):
            holder["proc"] = _Proc(plan)
            return holder["proc"]
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
        return holder

    monkeypatch.setattr(copilot_cli, "available", lambda: True)
    monkeypatch.setattr(copilot_cli, "mcp_cli_enabled", lambda: True)
    monkeypatch.setattr(copilot_cli, "_build_cmd", lambda *a, **k: ["true"])
    monkeypatch.setattr(copilot_cli, "_cwd", lambda _c: None)
    monkeypatch.setattr(copilot_cli, "turn_timeout", lambda: 30)
    monkeypatch.setenv("ASTA_TURN_IDLE", "30")     # floor is 30; idle is forced below
    monkeypatch.setattr(tb, "idle_seconds", lambda: 0.3)
    return _spawn


#: Verbatim from the 26 Aug screenshot: a turn that had answered in full and was
#: reported as "stuck — more time would not have helped".
_ANSWER = ("Still running (Component test job in progress, ~6-7 min in so far). "
           "I'll keep watching and let you know the moment it finishes.")


def test_an_answer_followed_by_silence_comes_back_as_the_answer(_copilot):
    """The whole complaint: this turn had SUCCEEDED and was reported as stuck."""
    _copilot([(0.01, _ANSWER.encode())])
    out = asyncio.run(copilot_cli.run_turn({"id": "c1"}, "any news on the CT job?"))
    assert out == _ANSWER


def test_the_process_is_still_killed(_copilot):
    """Nothing more is coming; leaving it alive holds a subprocess for nothing."""
    holder = _copilot([(0.01, _ANSWER.encode())])
    asyncio.run(copilot_cli.run_turn({"id": "c1"}, "any news?"))
    assert holder["proc"].killed


def test_it_is_recorded_so_the_split_can_be_measured(_copilot):
    """Reclassifying a stop as a success is a judgement call, so it leaves a
    trace — otherwise "is this firing too often?" has no answer but a feeling."""
    _copilot([(0.01, _ANSWER.encode())])
    asyncio.run(copilot_cli.run_turn({"id": "c1"}, "any news?"))
    kinds = [(r["kind"], r["outcome"]) for r in store.recent_outcomes(20)]
    assert ("turn", "answered_then_idle") in kinds


def test_a_wedge_mid_edit_still_raises(_copilot):
    """The failure the reclassification must never swallow."""
    _copilot([(0.01, b"Editing BookingService.java, replacing the comparison with")])
    with pytest.raises(tb.TurnStopped) as exc:
        asyncio.run(copilot_cli.run_turn({"id": "c1"}, "implement it"))
    assert "stuck" in str(exc.value)


def test_a_wedge_that_was_streamed_is_not_repeated_in_the_error(_copilot):
    """on_delta already put it on his phone."""
    partial = "Editing BookingService.java, replacing the comparison with"
    _copilot([(0.01, partial.encode())])
    seen: list[str] = []

    async def _sink(text):
        seen.append(text)

    with pytest.raises(tb.TurnStopped) as exc:
        asyncio.run(copilot_cli.run_turn({"id": "c1"}, "implement it", _sink))
    assert seen == [partial]
    assert partial not in str(exc.value)


def test_a_wedge_with_no_sink_keeps_the_evidence(_copilot):
    """A background task shows nothing as it goes, so the error is the only
    place the work it managed to do can survive."""
    partial = "Editing BookingService.java, replacing the comparison with"
    _copilot([(0.01, partial.encode())])
    with pytest.raises(tb.TurnStopped) as exc:
        asyncio.run(copilot_cli.run_turn({"id": "c1"}, "implement it"))
    assert partial in str(exc.value)
