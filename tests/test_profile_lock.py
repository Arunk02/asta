"""Chromium tolerates one writer per profile, and a call must not lose to a lock.

2026-09-07: a test call died with "Failed to create a ProcessSingleton for
your profile directory" while nothing was actually running — the lock files had
outlived a browser that was killed rather than closed. I cleared it by hand and
dialled again, which is a fix available to me at a keyboard and to nobody at two
in the morning.
"""

from __future__ import annotations

import asyncio

import pytest

from app import teams_bridge as tb


def test_a_stale_lock_is_cleared_when_nothing_holds_the_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(tb, "reap_orphans", lambda: 0)
    monkeypatch.setattr(tb, "profile_processes", lambda: [])
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (tmp_path / name).write_text("stale")

    asyncio.run(tb.free_profile())

    assert not any((tmp_path / n).exists()
                   for n in ("SingletonLock", "SingletonSocket", "SingletonCookie"))


def test_a_live_browser_is_never_unlocked(tmp_path, monkeypatch):
    """The dangerous direction. Removing the lock from under a running browser
    invites two writers into a store that tolerates one."""
    monkeypatch.setattr(tb, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(tb, "reap_orphans", lambda: 0)
    monkeypatch.setattr(tb, "profile_processes", lambda: [4242])
    (tmp_path / "SingletonLock").write_text("held")

    asyncio.run(tb.free_profile(timeout=0.3))

    assert (tmp_path / "SingletonLock").exists()


def test_it_waits_for_a_reaped_browser_to_actually_exit(tmp_path, monkeypatch):
    """`reap_orphans` sends SIGTERM and returns. A launch straight afterwards can
    still lose the race, which is exactly what a call does."""
    monkeypatch.setattr(tb, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(tb, "reap_orphans", lambda: 1)
    alive = {"n": 3}

    def dying():
        alive["n"] -= 1
        return [999] if alive["n"] > 0 else []

    monkeypatch.setattr(tb, "profile_processes", dying)
    (tmp_path / "SingletonLock").write_text("held")

    asyncio.run(tb.free_profile(timeout=5))

    assert alive["n"] <= 0                       # it waited rather than pressing on
    assert not (tmp_path / "SingletonLock").exists()


def test_a_live_call_is_never_reaped(tmp_path, monkeypatch):
    """The worst thing this recovery could do. A call holds the profile, so the
    60-second chat sweep finds it locked, "recovers" by killing every browser on
    it, and hangs up on a colleague mid-sentence. That is exactly how the 18:42
    test call died — TargetClosedError twelve seconds after dialling."""
    from app import meetings
    monkeypatch.setattr(tb, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(meetings, "_CALL", {"page": object()})
    reaped = {"n": 0}
    monkeypatch.setattr(tb, "reap_orphans", lambda: reaped.__setitem__("n", reaped["n"] + 1))
    (tmp_path / "SingletonLock").write_text("held by the call")

    asyncio.run(tb.free_profile(timeout=0.2))

    assert reaped["n"] == 0, "a background poll killed a live call"
    assert (tmp_path / "SingletonLock").exists()


def test_a_missing_profile_dir_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "PROFILE_DIR", tmp_path / "gone")
    monkeypatch.setattr(tb, "reap_orphans", lambda: 0)
    monkeypatch.setattr(tb, "profile_processes", lambda: [])
    asyncio.run(tb.free_profile())               # must not raise


def test_launch_frees_the_profile_first():
    """The fix is worthless if the launch path does not call it.

    Read from the FILE, not via `inspect`: conftest replaces `_launch` with a
    stub that raises, because a test once opened a real browser and dialled a
    colleague. Inspecting the patched attribute reads the stub."""
    from pathlib import Path
    src = Path("app/teams_bridge.py").read_text()
    body = src[src.index("async def _launch("):]
    body = body[:body.index("launch_persistent_context")]
    assert "await free_profile()" in body, (
        "the profile is not freed before the browser launches")
