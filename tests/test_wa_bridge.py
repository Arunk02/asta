"""WhatsApp bridge supervision — the child that used to be a manual step.

The end-to-end behaviour (spawn on boot, restart on crash) is proven by running
it; these lock in the branches that are easy to get wrong and dangerous when
they are: never manage a bridge we did not start, and respect the off switch.
"""

from __future__ import annotations

import asyncio

from app import wa_bridge


def test_supervision_can_be_turned_off(monkeypatch):
    """launchd or a hand-started bridge must be able to own it instead."""
    monkeypatch.setenv("ASTA_WA_SUPERVISE", "0")
    assert wa_bridge.enabled() is False
    monkeypatch.setenv("ASTA_WA_SUPERVISE", "1")
    # enabled() also needs the bridge script present; on this repo it is.
    assert wa_bridge.enabled() is (wa_bridge.BRIDGE_JS.is_file())


def test_status_shape_is_stable_for_the_ui():
    s = wa_bridge.status()
    assert set(s) == {"supervised", "child_running", "pid"}
    assert s["child_running"] is False and s["pid"] is None  # nothing spawned in-test


def test_stop_is_safe_when_nothing_was_started():
    """A restart calls stop() unconditionally; with no child it must no-op, not raise."""
    asyncio.run(wa_bridge.stop())


def test_a_foreign_bridge_is_used_never_adopted(monkeypatch):
    """If something already answers on the port, the supervisor must not spawn a
    second one — and must not record it as its own child to later kill."""
    calls = {"spawn": 0}

    async def already_up():
        return True

    async def fake_spawn():
        calls["spawn"] += 1
        return None

    monkeypatch.setattr(wa_bridge, "_bridge_answering", already_up)
    monkeypatch.setattr(wa_bridge, "_spawn", fake_spawn)
    monkeypatch.setattr(wa_bridge, "CHECK_SECONDS", 0.01)

    async def run_one_tick():
        task = asyncio.create_task(wa_bridge.supervise())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_one_tick())
    assert calls["spawn"] == 0, "must not spawn when a bridge already answers"
    assert wa_bridge._proc is None, "a foreign bridge must never be recorded as our child"
