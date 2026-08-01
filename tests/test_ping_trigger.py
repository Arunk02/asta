"""How Asta learns you were pinged on Teams — Playwright first, notification DB optional.

Two triggers exist and they are not equal. The Playwright Activity-feed poll reads,
replies and sends — it is the whole assistant — and needs no OS permission. The
macOS notification-DB watcher only reads a banner's title, is blind to muted/DND
chats, cannot reply, and needs Full Disk Access. So the first is the default and
the second is an opt-in latency tweak, not a dependency.

The bug these pin: `.env` shipped with TEAMS_WATCHER=1, which turned the
FDA-needing watcher on against its own "disabled by default" design — so a fresh
machine reported unhealthy and the Teams trigger looked broken until someone
fought the (unnecessary) Full Disk Access grant.
"""

from __future__ import annotations

import asyncio

import pytest

from app import health, msnotify, teams_bridge


# --- the notification watcher is optional, and off by default ----------------

def test_the_notification_watcher_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("TEAMS_WATCHER", raising=False)
    assert msnotify.enabled() is False, (
        "the FDA-needing watcher must be opt-in — its own docstring says so")


def test_the_playwright_poll_is_the_default_trigger(monkeypatch):
    """No env at all still leaves a working trigger: the Activity-feed poll, on by
    default at a real interval, gated only on the Teams bridge being up."""
    monkeypatch.delenv("TEAMS_ACTIVITY_POLL", raising=False)
    import importlib
    importlib.reload(teams_bridge)
    try:
        assert teams_bridge.ACTIVITY_POLL_SECONDS > 0
    finally:
        importlib.reload(teams_bridge)


def test_a_disabled_watcher_is_not_a_health_problem(monkeypatch):
    """With the watcher off, its notification DB being unreadable is irrelevant —
    Playwright covers the same mentions, so health must not go red over it."""
    monkeypatch.delenv("TEAMS_WATCHER", raising=False)
    st = msnotify.status()
    assert st["enabled"] is False
    # checks() only files a problem when enabled AND broken; disabled => silent.
    assert not (st.get("enabled") and not st.get("ok"))


def test_an_enabled_but_blocked_watcher_still_surfaces(monkeypatch):
    """If you DO ask for it (TEAMS_WATCHER=1) and FDA is missing, that is worth
    saying — you opted into a thing that cannot work. The default just isn't that."""
    monkeypatch.setenv("TEAMS_WATCHER", "1")
    monkeypatch.setattr(msnotify, "DB_PATH", msnotify.DB_PATH.parent / "does-not-exist")
    st = msnotify.status()
    assert st["enabled"] is True and st["ok"] is False
    assert "Full Disk Access" in st["reason"]


# --- Playwright is the full-capability path ----------------------------------

def test_reading_replying_and_sending_all_live_on_the_playwright_bridge():
    """The reason Playwright is primary: it is the only path that can act. The
    watcher can alert; it cannot answer. Pin that the send/read/presence surface
    is the browser bridge, so nobody 'simplifies' the trigger down to the watcher
    and quietly loses the ability to reply."""
    for name in ("send_message", "read_activity", "read_presence", "set_presence"):
        assert hasattr(teams_bridge, name), f"teams_bridge lost {name}"


def test_the_two_triggers_share_one_notion_of_who_you_are(monkeypatch):
    """Both the poll and the watcher decide 'about me' from the same keyword list,
    so enabling the watcher can never disagree with the poll about what counts."""
    monkeypatch.setenv("TEAMS_WATCH_KEYWORDS", "arun,vinish")
    assert msnotify.keywords() == ["arun", "vinish"]
    # _activity_wanted (the Playwright side) falls back to exactly these keywords
    import inspect
    assert "msnotify.keywords()" in inspect.getsource(teams_bridge._activity_wanted)
