"""15 Oct: the live bench timed out every night from 7 Oct, and a timeout
returned before it was recorded, so the loop ran it again — an hour of brain
time two or three times a night — and the call rehearsal after it never ran.
Its last result (6 Oct) aged out, and health read that as "no failures".
"""

import asyncio
import json
import time

from app import call_rehearsal, health
from app.workworld import nightly


class _Hung:
    def __init__(self):
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(5)
        return b"", b""

    def kill(self):
        self.killed = True


def test_a_timed_out_bench_is_the_nights_run_and_calls_are_still_rehearsed(monkeypatch, tmp_path):
    procs = []

    async def spawn(*a, **k):
        procs.append(_Hung())
        return procs[-1]

    rehearsed = []

    async def calls():
        rehearsed.append(1)
        return "3/3 rehearsals passed"

    saved = {}
    monkeypatch.setattr(nightly.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(nightly, "LIVE_TIMEOUT", 0.05)
    monkeypatch.setattr(nightly, "_rehearse_calls", calls)
    monkeypatch.setattr(nightly, "_budget", lambda: {})
    monkeypatch.setattr(nightly, "_save", saved.update)
    note = asyncio.run(nightly.run_once())
    assert procs[0].killed and rehearsed == [1]
    assert "timed out" in note and "rehearsals passed" in note
    assert saved["last_run"][:10] == time.strftime("%Y-%m-%d") and saved["spent_week"] > 0


def _latest(at):
    path = call_rehearsal._latest_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": at, "results": [{"scenario": "quick-yes", "passed": True}]}))


def test_a_rehearsal_that_stopped_running_is_a_problem(monkeypatch):
    _latest(time.time() - 9 * 86400)
    assert call_rehearsal.stale()
    monkeypatch.setattr(nightly, "enabled", lambda: True)
    found = asyncio.run(health.checks())
    assert "has not run since" in found.get("calls", "")


def test_a_recent_rehearsal_is_not(monkeypatch):
    _latest(time.time() - 3600)
    assert call_rehearsal.stale() == ""
    monkeypatch.setattr(nightly, "enabled", lambda: True)
    assert "calls" not in asyncio.run(health.checks())


def test_never_rehearsed_is_not_called_stale():
    assert call_rehearsal.stale() == ""


def test_a_hung_live_run_fails_by_name_and_the_bench_goes_on(monkeypatch):
    from app.workworld import scenario as S

    async def hung(sc, seed=0, live=False):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(S, "run", hung)
    monkeypatch.setattr(S, "LIVE_RUN_TIMEOUT", 0.05)

    class Sc:
        id = "live-hangs"
        set = "live"

    t = time.monotonic()
    res = asyncio.run(S.run_scenario(Sc(), k=2, live=True))
    assert time.monotonic() - t < 1
    assert not res.passed and res.failures == ["[run 0] timed out after 0s", "[run 1] timed out after 0s"]
