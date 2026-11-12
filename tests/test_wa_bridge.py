"""WhatsApp bridge supervision — the child that used to be a manual step.

The end-to-end behaviour (spawn on boot, restart on crash) is proven by running
it; these lock in the branches that are easy to get wrong and dangerous when
they are: never manage a bridge we did not start, and respect the off switch.
"""

from __future__ import annotations

import asyncio

from app import wa_bridge


def test_supervision_can_be_turned_off(monkeypatch, tmp_path):
    """A hand-started bridge must be able to own it instead."""
    _no_launchd_job(monkeypatch, tmp_path)
    monkeypatch.setenv("ASTA_WA_SUPERVISE", "0")
    assert wa_bridge.enabled() is False
    monkeypatch.setenv("ASTA_WA_SUPERVISE", "1")
    # enabled() also needs the bridge script present; on this repo it is.
    assert wa_bridge.enabled() is (wa_bridge.BRIDGE_JS.is_file())


def _no_launchd_job(monkeypatch, tmp_path):
    monkeypatch.setattr(wa_bridge, "LAUNCHD_PLIST", tmp_path / "absent.plist")


def test_status_shape_is_stable_for_the_ui(monkeypatch, tmp_path):
    _no_launchd_job(monkeypatch, tmp_path)
    s = wa_bridge.status()
    assert set(s) == {"owner", "supervised", "child_running", "pid"}
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


def test_an_installed_launchd_job_owns_the_bridge(monkeypatch, tmp_path):
    """A child dies with a crashed server (launchd kills the process group), and
    with it the bridge's "server down" alert. With the bridge's own job installed
    the server must never spawn one, whatever ASTA_WA_SUPERVISE says."""
    plist = tmp_path / "com.asta.whatsapp.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(wa_bridge, "LAUNCHD_PLIST", plist)
    monkeypatch.setenv("ASTA_WA_SUPERVISE", "1")
    assert wa_bridge.enabled() is False
    s = wa_bridge.status()
    assert s["owner"] == "launchd" and s["supervised"] is False
    plist.unlink()
    assert wa_bridge.owner() == ("asta" if wa_bridge.BRIDGE_JS.is_file() else "manual")


def test_install_loads_the_bridge_job_after_the_server():
    """install.sh is the one place the job is loaded: the bridge plist must be in
    place before the server reloads (so it never spawns a child), and the bridge
    job must load after the server (so the old child has freed the port)."""
    import re
    text = (wa_bridge.ROOT / "deploy" / "install.sh").read_text()
    services = re.search(r'^SERVICES="([^"]+)"', text, re.M).group(1).split()
    assert services.index("com.asta.server") < services.index("com.asta.whatsapp")
    assert text.index("cp deploy/com.asta.whatsapp.plist") < text.index("for svc in $SERVICES; do\n  cp")
    assert (wa_bridge.ROOT / "deploy" / "com.asta.whatsapp.plist").is_file()
