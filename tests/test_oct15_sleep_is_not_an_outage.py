"""15 Oct: the lid shut on battery (31%, then 8%) and the Mac slept ~100 times a
day between dark wakes. Vitals called it "Teams has not been read for 121 min"
and "the incoming-call check has not run for 55 min" — the sleep, told as faults.
A wake is a fresh start; a Mac awake that long and still not reading is still told.
"""

import time

import pytest

from app import health, incoming, store, teams_bridge, wake


@pytest.fixture
def teams_on(monkeypatch):
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(teams_bridge, "in_a_call", lambda: False)
    monkeypatch.setattr(incoming, "enabled", lambda: True)
    from app import agent
    monkeypatch.setattr(agent, "available", lambda b: True)
    monkeypatch.setattr(agent, "quota_down", lambda b: False)


def _stale(monkeypatch, now):
    monkeypatch.setattr(teams_bridge, "_last_read", lambda: now - 121 * 60)
    store.kv_set(incoming.CHECKED_KEY, str(now - 55 * 60))


def test_a_mac_that_just_woke_is_not_an_outage(teams_on, monkeypatch):
    now = time.time()
    _stale(monkeypatch, now)
    store.kv_set(wake.LAST_GAP_KEY, f"{now - 60:.0f}:900")
    assert health.vitals(now=now) == {}


def test_awake_long_enough_and_still_not_reading_is_told(teams_on, monkeypatch):
    now = time.time()
    _stale(monkeypatch, now)
    store.kv_set(wake.LAST_GAP_KEY, f"{now - 30 * 60:.0f}:900")
    v = health.vitals(now=now)
    assert "Teams has not been read" in v["teams_read"] and "call_check" in v


def test_no_sleep_on_record_keeps_the_old_reading(teams_on, monkeypatch):
    now = time.time()
    _stale(monkeypatch, now)
    store.kv_del(wake.LAST_GAP_KEY)
    v = health.vitals(now=now)
    assert "121 min" in v["teams_read"] and "55 min" in v["call_check"]


def test_whatsapp_reconnecting_is_looked_at_again_before_it_is_called_down(monkeypatch):
    import asyncio

    from app import notify
    looks = iter([{"up": True, "paired": False, "enabled": True},
                  {"up": True, "paired": True, "enabled": True}])

    async def status():
        return next(looks)

    monkeypatch.setattr(notify, "wa_status", status)
    assert "whatsapp" not in asyncio.run(health.checks())


def test_whatsapp_down_on_every_look_is_still_told(monkeypatch):
    import asyncio

    from app import notify

    async def status():
        return {"up": False}

    monkeypatch.setattr(notify, "wa_status", status)
    assert asyncio.run(health.checks())["whatsapp"] == "bridge not running"
