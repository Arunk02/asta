"""Away mode: the laptop keeps working while he is away from it.

12 Oct: "when was far away and lap in sleep, if i send any message, im not
getting any respond … even in sleep, while i was far away i has to work."

While the Mac sleeps nothing local runs — not Asta, not the WhatsApp bridge,
not Teams (wake.py says the same: what is fixable is the wake, not the sleep).
A keep-awake assertion (caffeinate) does not survive closing the lid. Only
`pmset -a disablesleep 1` does, and that needs root, so he grants Asta exactly
those two commands once, in sudoers (SETUP below — he runs it, Asta never
does), and Asta switches it:

  - on when he says so ("I'm leaving, stay awake"), or at ASTA_AWAY_AUTO_ON;
  - off when he says so ("normal sleep"), when he is back at the laptop, and —
    the guard — when the battery runs low off the charger, after telling him.

Asta only ever turns off what it turned on: a sleep setting he made himself is
never touched by the guard.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re

from . import store

ON_KEY = "away_mode_on"            # epoch it was switched on, by Asta
MIN_BATTERY = int(os.environ.get("ASTA_AWAY_MIN_BATTERY", "25"))
#: "19:00" switches it on daily when he is not at the laptop; "" never.
AUTO_ON = os.environ.get("ASTA_AWAY_AUTO_ON", "").strip()
CHECK_SECONDS = 120.0
#: Back at the laptop this long (in a row) and Asta's away mode goes off.
BACK_FOR_SECONDS = 300.0

PMSET = "/usr/bin/pmset"
SETUP = (f'echo "$(whoami) ALL=(root) NOPASSWD: {PMSET} -a disablesleep 0, '
         f'{PMSET} -a disablesleep 1" | sudo tee /etc/sudoers.d/asta-away '
         f'&& sudo chmod 440 /etc/sudoers.d/asta-away')

#: "I'm leaving, stay awake", "away mode on", "keep working while I'm away".
TURN_ON = re.compile(
    r"^\s*(?:i'?m\s+(?:leaving|going|off|away)\b[^.?!]*?[,.]?\s*)?"
    r"(?:stay\s+awake|keep\s+(?:the\s+(?:laptop|mac)\s+)?awake|away\s+mode(?:\s+on)?|"
    r"don'?t\s+sleep|keep\s+working\s+while\s+i'?m\s+away)\s*[.!]*\s*$", re.I)
#: "normal sleep", "away mode off", "you can sleep".
TURN_OFF = re.compile(
    r"^\s*(?:normal\s+sleep|away\s+mode\s+off|stop\s+away\s+mode|you\s+can\s+sleep(?:\s+now)?|"
    r"let\s+(?:the\s+)?(?:laptop|mac)\s+sleep)\s*[.!]*\s*$", re.I)


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode(errors="replace")


async def sleep_disabled() -> bool:
    rc, out = await _run(PMSET, "-g")
    return rc == 0 and bool(re.search(r"^\s*SleepDisabled\s+1\b", out, re.M))


async def battery() -> tuple[int | None, bool]:
    """(percent, on the charger). (None, True) for a Mac with no battery."""
    rc, out = await _run(PMSET, "-g", "batt")
    m = re.search(r"(\d+)%", out)
    return (int(m.group(1)) if m and rc == 0 else None), "AC Power" in out


async def _set(disabled: bool) -> tuple[bool, str]:
    # -n: never prompt. Without his sudoers line this fails at once, and the
    # answer is the setup line, not a hung password prompt.
    rc, out = await _run("sudo", "-n", PMSET, "-a", "disablesleep", "1" if disabled else "0")
    return rc == 0, out.strip()


async def turn_on(why: str = "asked") -> str:
    ok, _ = await _set(True)
    if not ok:
        return ("I can't keep the laptop awake yet — it needs a one-time setup. Run this "
                "once in Terminal; it allows exactly the two pmset commands, nothing else:\n\n"
                f"{SETUP}")
    store.kv_set(ON_KEY, str(dt.datetime.now().timestamp()))
    store.record_outcome("away", "on", detail=why)
    pct, charging = await battery()
    power = ("on the charger" if charging else
             f"on battery at {pct}% — below {MIN_BATTERY}% I'll tell you and let it sleep"
             if pct is not None else "")
    return ("🌙 Away mode on — the laptop stays awake with the lid closed, so I keep "
            f"working and answering here{f' ({power})' if power else ''}. "
            "Say “normal sleep” to switch it off; it also goes off when you're back.")


async def turn_off(why: str = "asked") -> str:
    store.kv_del(ON_KEY)
    ok, out = await _set(False)
    store.record_outcome("away", "off", detail=why if ok else f"{why}: {out[:120]}")
    if not ok:
        return f"Couldn't switch normal sleep back on: {out[:200] or 'sudo refused'}"
    return "☀️ Away mode off — normal sleep again."


def is_on() -> bool:
    return bool(store.kv_get(ON_KEY))


async def command(text: str) -> str:
    """The reply, if his words switch away mode — else ''."""
    if TURN_ON.match(text or ""):
        return await turn_on("asked")
    if TURN_OFF.match(text or ""):
        if not is_on() and not await sleep_disabled():
            return "Normal sleep is already on."
        return await turn_off("asked")
    return ""


_BACK: dict = {}


async def check(now: dt.datetime | None = None) -> str:
    """One pass of the guard. Returns what it did ('' for nothing)."""
    from . import notify, presence
    now = now or dt.datetime.now()
    if is_on():
        pct, charging = await battery()
        if pct is not None and not charging and pct < MIN_BATTERY:
            await notify.notify(
                f"🔋 Battery at {pct}% and not charging — switching away mode off, so the "
                "laptop will sleep and I'll stop answering until it wakes. Plug it in and "
                "say “stay awake” to keep me working.", "away", urgency="direct")
            await turn_off(f"battery {pct}%")
            return "battery"
        if await presence.at_laptop():
            _BACK.setdefault("t", now.timestamp())
            if now.timestamp() - _BACK["t"] >= BACK_FOR_SECONDS:
                _BACK.clear()
                await turn_off("back at the laptop")
                await notify.notify("☀️ You're back — away mode off, normal sleep again.",
                                    "away", urgency="ambient")
                return "back"
        else:
            _BACK.clear()
        return ""
    if AUTO_ON and now.strftime("%H:%M") == AUTO_ON[:5] \
            and store.kv_get("away_auto_on_day") != now.date().isoformat() \
            and not await presence.at_laptop():
        store.kv_set("away_auto_on_day", now.date().isoformat())
        await notify.notify(await turn_on(f"auto at {AUTO_ON}"), "away", urgency="ambient")
        return "auto"
    return ""


async def guard_loop() -> None:
    from . import wake
    while True:
        try:
            await check()
        except Exception:                                          # noqa: BLE001
            pass
        # Under a minute when a daily time is set, so the minute is not missed.
        await wake.sleep(50.0 if AUTO_ON else CHECK_SECONDS)
