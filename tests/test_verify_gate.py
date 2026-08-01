"""The verifier gate wired into _finish_code — the loop actually closing.

Drives the real _finish_code with a fake code leg and a REAL subprocess check, so
it proves the control flow end to end without spending a model token: red loops to
fix, green finishes, a stuck check parks for Arun (never infinite), and — the
safety contract — a disabled gate or a repo with no check is a pure no-op.
"""

from __future__ import annotations

import asyncio

import pytest

from app import store, tasks


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A code task in a tmp 'workspace', notifications captured, learning stubbed
    (its background extraction would try to reach a brain), and the code leg faked."""
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))
    t = store.create_task("verify me", "code", "do the thing", "tw")

    notes: list[str] = []

    async def fake_notify(text, channel="task"):
        notes.append(text)

    async def no_extract(*a, **k):
        return None

    monkeypatch.setattr("app.notify.notify", fake_notify)
    monkeypatch.setattr("app.learn.extract", no_extract)

    calls = {"legs": 0, "on_leg": None}

    async def fake_leg(task_id, prompt, cwd, *, resume, effort, workspace=None):
        calls["legs"] += 1
        if calls["on_leg"]:
            calls["on_leg"](calls["legs"])
        return "Implemented. git diff --stat: 1 file changed, 2 insertions(+)"

    monkeypatch.setattr(tasks, "_run_code_leg", fake_leg)
    monkeypatch.setenv("ASTA_VERIFY", "1")
    return {"t": t, "tid": t["id"], "notes": notes, "calls": calls, "cwd": tmp_path}


def _counts(kind: str) -> dict[str, int]:
    return {r["outcome"]: r["n"] for r in store.outcome_counts(0.0) if r["kind"] == kind}


# --- the safety contract: additive, never subtractive --------------------------

def test_disabled_is_a_noop(wired, monkeypatch):
    monkeypatch.delenv("ASTA_VERIFY", raising=False)
    monkeypatch.setenv("ASTA_VERIFY_CMD", "false")     # would be RED if the gate ran
    asyncio.run(tasks._finish_code(wired["tid"], wired["t"], "done result", 0))
    assert store.get_task(wired["tid"])["status"] == "done"
    assert wired["calls"]["legs"] == 0                 # no fix loop
    assert _counts("verify") == {}                     # gate never engaged


def test_no_oracle_is_a_noop(wired, monkeypatch):
    monkeypatch.delenv("ASTA_VERIFY_CMD", raising=False)   # empty tmp dir -> nothing resolves
    asyncio.run(tasks._finish_code(wired["tid"], wired["t"], "done result", 0))
    assert store.get_task(wired["tid"])["status"] == "done"
    assert wired["calls"]["legs"] == 0


def test_broken_check_is_skipped_not_looped(wired, monkeypatch):
    monkeypatch.setenv("ASTA_VERIFY_CMD", "this_binary_does_not_exist_xyz")
    asyncio.run(tasks._finish_code(wired["tid"], wired["t"], "done result", 0))
    assert store.get_task(wired["tid"])["status"] == "done"   # a typo can't brick completion
    assert wired["calls"]["legs"] == 0


# --- the resilient behaviour ---------------------------------------------------

def test_green_first_try_finishes_clean(wired, monkeypatch):
    monkeypatch.setenv("ASTA_VERIFY_CMD", "true")
    asyncio.run(tasks._finish_code(wired["tid"], wired["t"], "done result", 0))
    assert store.get_task(wired["tid"])["status"] == "done"
    assert wired["calls"]["legs"] == 0                 # nothing to fix
    assert _counts("verify") == {"passed": 1}


def test_red_then_green_loops_once_then_done(wired, monkeypatch):
    marker = wired["cwd"] / "fixed"
    monkeypatch.setenv("ASTA_VERIFY_CMD", f"test -f {marker}")   # red until the leg fixes it

    def fix_on_first_leg(n):
        marker.write_text("ok")                        # the fixing leg makes the check pass

    wired["calls"]["on_leg"] = fix_on_first_leg
    asyncio.run(tasks._finish_code(wired["tid"], wired["t"], "done result", 0))

    assert store.get_task(wired["tid"])["status"] == "done"
    assert wired["calls"]["legs"] == 1                 # exactly one fix round
    assert _counts("verify") == {"passed": 1}          # terminal outcome is a clean pass
    assert _counts("verify_round").get("failed") == 1  # one fix round, kept as telemetry
    # red->green marks the run as escalated, so the teacher half learns the fix
    assert store.kv_get(f"task_escalated:{wired['tid']}") == "1"


def test_stuck_check_parks_for_arun_never_infinite(wired, monkeypatch):
    monkeypatch.setenv("ASTA_VERIFY_CMD", "false")     # never goes green
    monkeypatch.setenv("ASTA_VERIFY_MAX_ROUNDS", "2")
    asyncio.run(tasks._finish_code(wired["tid"], wired["t"], "done result", 0))

    task = store.get_task(wired["tid"])
    assert task["status"] == "awaiting_approval"       # parked, not "done" over red
    assert wired["calls"]["legs"] == 2                 # bounded by max_rounds, not infinite
    assert store.kv_get(f"task_gate:{wired['tid']}") == "verify"
    assert _counts("verify").get("unresolved") == 1
    assert any("STILL failing" in n for n in wired["notes"])
