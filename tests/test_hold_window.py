"""Ambient notifications must reach him whether he is online or offline.

Arun: "sometimes i off my teams, outlooks everything and work on other things,
basically u have to support both." Presence was the only release condition, and
`at_laptop()` is true whenever he is touching the machine — so with Teams closed
an ambient item could sit held for the entire afternoon.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import notify, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    store.kv_set(notify.HELD_KEY, "[]")
    monkeypatch.setenv("ASTA_HOLD_MAX_MINUTES", "45")
    yield


@pytest.fixture
def pushed(monkeypatch):
    """Capture what actually reaches the phone channels."""
    out = []

    async def wa(text):
        out.append(("wa", text))
        return True

    async def tg(text):
        out.append(("tg", text))
        return True

    monkeypatch.setattr(notify, "wa_send", wa)
    monkeypatch.setattr(notify.telegram, "send", tg)
    return out


def _at_laptop(monkeypatch, present: bool):
    from app import presence

    async def fake():
        return present

    monkeypatch.setattr(presence, "at_laptop", fake)


# --- the courtesy still works ----------------------------------------------

def test_ambient_is_held_while_he_is_at_the_laptop(monkeypatch, pushed):
    _at_laptop(monkeypatch, True)
    r = asyncio.run(notify.notify("CI went green", "ci", urgency="ambient"))
    assert r["held"] is True
    assert pushed == []                              # his focus is protected


def test_direct_always_goes_regardless_of_presence(monkeypatch, pushed):
    _at_laptop(monkeypatch, True)
    r = asyncio.run(notify.notify("Priya needs you", "teams", urgency="direct"))
    assert r["held"] is False
    assert pushed                                    # someone is waiting on him


# --- ...but a hold now expires (the online/offline fix) ---------------------

def test_a_held_item_is_released_once_it_has_waited_too_long(monkeypatch, pushed):
    """THE fix: he is still 'at the laptop', but Teams is closed and the item has
    aged out. It must go out anyway rather than wait for a departure."""
    _at_laptop(monkeypatch, True)
    old = time.time() - 46 * 60
    store.kv_set(notify.HELD_KEY, json.dumps([{"at": old, "text": "nightly finished"}]))

    r = asyncio.run(notify.notify("another update", "ci", urgency="ambient"))
    assert r["held"] is False
    assert pushed                                    # delivered while he is present
    assert "waited long enough" in pushed[0][1]
    assert "nightly finished" in pushed[0][1]        # the old one came too


def test_a_fresh_hold_is_not_released_early(monkeypatch, pushed):
    _at_laptop(monkeypatch, True)
    store.kv_set(notify.HELD_KEY,
                 json.dumps([{"at": time.time() - 60, "text": "one minute old"}]))
    r = asyncio.run(notify.notify("another", "ci", urgency="ambient"))
    assert r["held"] is True
    assert pushed == []


def test_stepping_away_still_flushes_immediately(monkeypatch, pushed):
    _at_laptop(monkeypatch, False)
    store.kv_set(notify.HELD_KEY,
                 json.dumps([{"at": time.time(), "text": "held earlier"}]))
    asyncio.run(notify.flush_held())
    assert "held earlier" in pushed[0][1]
    assert "while you were at the laptop" in pushed[0][1]


def test_the_window_is_tunable_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ASTA_HOLD_MAX_MINUTES", "5")
    assert notify.hold_max_minutes() == 5
    items = [{"at": time.time() - 6 * 60, "text": "x"}]
    assert notify._stale(items)

    monkeypatch.setenv("ASTA_HOLD_MAX_MINUTES", "0")
    assert notify._stale(items) is False             # opt out explicitly

    monkeypatch.setenv("ASTA_HOLD_MAX_MINUTES", "junk")
    assert notify.hold_max_minutes() == 45           # a bad value never strands mail


# --- the storage change must not lose anything -----------------------------

def test_legacy_bare_string_holds_are_still_delivered():
    """Items held by the previous build were plain strings. They must not be
    dropped on upgrade — and with no timestamp they count as overdue."""
    store.kv_set(notify.HELD_KEY, json.dumps(["from the old build"]))
    items = notify._held_items()
    assert items == [{"at": 0.0, "text": "from the old build"}]
    assert notify._stale(items)                      # unknown age → send it


def test_corrupt_held_state_never_crashes_a_notification():
    store.kv_set(notify.HELD_KEY, "not json at all")
    assert notify._held_items() == []


def test_held_list_is_capped(monkeypatch, pushed):
    _at_laptop(monkeypatch, True)
    monkeypatch.setenv("ASTA_HOLD_MAX_MINUTES", "0")     # no age release
    for i in range(notify.HELD_MAX + 10):
        asyncio.run(notify.notify(f"item {i}", "ci", urgency="ambient"))
    assert len(notify._held_items()) == notify.HELD_MAX


def test_flush_reports_the_count_and_keeps_the_overflow_in_the_app(monkeypatch, pushed):
    store.kv_set(notify.HELD_KEY, json.dumps(
        [{"at": time.time(), "text": f"n{i}"} for i in range(14)]))
    asyncio.run(notify.flush_held())
    text = pushed[0][1]
    assert "Held (14)" in text
    assert "+4 more in the app" in text
