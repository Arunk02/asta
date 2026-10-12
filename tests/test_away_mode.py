"""Away mode — 12 Oct: "even in sleep, while i was far away i has to work".

pmset itself is never run here: every call goes through away._run, faked.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import away, main, store


class _Mac:
    """A fake pmset / sudo: what was asked, what the battery says."""

    def __init__(self, battery="'Battery Power'\n -InternalBattery-0 80%; discharging",
                 sudo_ok=True):
        self.calls: list[tuple] = []
        self.battery = battery
        self.sudo_ok = sudo_ok
        self.disabled = False

    async def run(self, *args):
        self.calls.append(args)
        if args[0] == "sudo":
            if not self.sudo_ok:
                return 1, "sudo: a password is required"
            self.disabled = args[-1] == "1"
            return 0, ""
        if args[1:] == ("-g", "batt"):
            return 0, self.battery
        return 0, f" SleepDisabled\t\t{1 if self.disabled else 0}\n sleep 1\n"


@pytest.fixture
def mac(monkeypatch):
    m = _Mac()
    monkeypatch.setattr(away, "_run", m.run)
    store.init()
    away._BACK.clear()
    return m


@pytest.fixture
def phone(monkeypatch):
    said = []

    async def _n(text, *a, **k):
        said.append(text)
    monkeypatch.setattr("app.notify.notify", _n)
    return said


def _present(monkeypatch, here: bool):
    async def at():
        return here
    monkeypatch.setattr("app.presence.at_laptop", at)


@pytest.mark.parametrize("text", [
    "I'm leaving, stay awake", "stay awake", "away mode on", "away mode",
    "keep the laptop awake", "im going out, stay awake", "don't sleep",
])
def test_his_words_switch_it_on(mac, text):
    said = asyncio.run(away.command(text))
    assert "Away mode on" in said and mac.disabled
    assert ("sudo", "-n", away.PMSET, "-a", "disablesleep", "1") in mac.calls


@pytest.mark.parametrize("text", ["normal sleep", "away mode off", "you can sleep now"])
def test_his_words_switch_it_off(mac, text):
    asyncio.run(away.command("stay awake"))
    assert "normal sleep again" in asyncio.run(away.command(text))
    assert not mac.disabled and not away.is_on()


@pytest.mark.parametrize("text", ["why did it not stay awake?", "is away mode on",
                                  "check the sleep logs", "stay awake for the deploy and then ship it"])
def test_other_messages_are_not_commands(mac, text):
    assert asyncio.run(away.command(text)) == ""
    assert not mac.calls


def test_without_the_sudoers_line_he_gets_the_setup_not_a_prompt(mac):
    mac.sudo_ok = False
    said = asyncio.run(away.command("stay awake"))
    assert "one-time setup" in said and "/etc/sudoers.d/asta-away" in said
    assert "disablesleep 0, /usr/bin/pmset -a disablesleep 1" in said
    assert not away.is_on()


def test_low_battery_off_the_charger_tells_him_then_sleeps(mac, phone, monkeypatch):
    _present(monkeypatch, False)
    asyncio.run(away.turn_on())
    mac.battery = "'Battery Power'\n -InternalBattery-0 22%; discharging"
    assert asyncio.run(away.check()) == "battery"
    assert "Battery at 22%" in phone[0]
    assert not mac.disabled and not away.is_on()


def test_on_the_charger_a_low_battery_keeps_it_awake(mac, phone, monkeypatch):
    _present(monkeypatch, False)
    asyncio.run(away.turn_on())
    mac.battery = "'AC Power'\n -InternalBattery-0 10%; charging"
    assert asyncio.run(away.check()) == "" and mac.disabled and not phone


def test_back_at_the_laptop_for_five_minutes_puts_sleep_back(mac, phone, monkeypatch):
    _present(monkeypatch, True)
    asyncio.run(away.turn_on())
    t0 = dt.datetime(2026, 10, 12, 9, 0)
    assert asyncio.run(away.check(t0)) == ""
    assert asyncio.run(away.check(t0 + dt.timedelta(minutes=6))) == "back"
    assert not mac.disabled and "You're back" in phone[-1]


def test_the_guard_never_touches_a_setting_asta_did_not_make(mac, phone, monkeypatch):
    _present(monkeypatch, True)
    mac.disabled = True                       # he disabled sleep himself
    t0 = dt.datetime(2026, 10, 12, 9, 0)
    asyncio.run(away.check(t0))
    asyncio.run(away.check(t0 + dt.timedelta(minutes=10)))
    assert mac.disabled and not any(c[0] == "sudo" for c in mac.calls)


def test_the_daily_time_switches_it_on_once(mac, phone, monkeypatch):
    _present(monkeypatch, False)
    monkeypatch.setattr(away, "AUTO_ON", "19:00")
    at = dt.datetime(2026, 10, 12, 19, 0, 20)
    assert asyncio.run(away.check(at)) == "auto" and away.is_on()
    asyncio.run(away.turn_off())
    assert asyncio.run(away.check(at + dt.timedelta(seconds=30))) == "", "once a day"


def test_it_is_answered_before_any_brain_on_every_channel(mac, monkeypatch):
    conv = store.create_conversation("claude_cli", None)
    out = []

    class _Sink:
        async def send(self, ev):
            out.append(ev)

    async def no_brain(*a, **k):
        raise AssertionError("a brain was asked")
    monkeypatch.setattr(main, "_start_turn", no_brain)
    assert asyncio.run(main._dispatch(conv, "I'm leaving, stay awake", _Sink(), "whatsapp")) is None
    assert any("Away mode on" in (e.get("text") or "") for e in out)


def test_every_brain_can_switch_it():
    from app import capabilities
    cap = capabilities.registry()["away_mode"]
    assert cap.http.startswith("POST /api/away") and cap.write
