"""Which microphone the call is using, and giving it back.

Split out of `meetings` because it is one self-contained job with one hard rule,
and because `meetings` had grown past the ceiling its own test guards.

Teams listens to ONE input at a time. While it is pointed at the virtual device
Asta speaks through, it cannot hear Arun at all — so leaving it there after an
utterance silently mutes him for the rest of the call, and he finds out when
somebody asks why he has gone quiet. The switch merely fails to be heard; the
missing restore fails to be heard FROM, which is the worse half by a long way.
"""

from __future__ import annotations

import asyncio
import os

from . import store

#: The device Asta speaks THROUGH — a virtual input Teams can be pointed at.
AUDIO_DEVICE = os.environ.get("ASTA_CALL_AUDIO_DEVICE", "").strip()

#: His real microphone — restored after every utterance.
HIS_MIC = os.environ.get("ASTA_HIS_MIC", "MacBook Pro Microphone")

#: The macOS input switcher (brew install switchaudio-osx). Teams must have its
#: microphone left on "Same as System" for this to reach it.
SWITCH_AUDIO = os.environ.get("ASTA_SWITCH_AUDIO", "/opt/homebrew/bin/SwitchAudioSource")


def can_switch_mic() -> bool:
    """Whether the input device can be changed at all."""
    from pathlib import Path as _P
    return _P(SWITCH_AUDIO).is_file()


async def set_call_mic(page=None, device: str = "") -> bool:
    """Point the microphone at `device`. False when it could not be done.

    This switches the SYSTEM default input rather than Teams' own setting.
    Driving the Teams UI was the first attempt and it does not survive contact:
    settings sit behind a React flyout off the "Settings and more" menu, the
    picker has no stable data-tid, and a pre-flight against live Teams returned
    False on every selector — meaning a call would have connected and then sat
    silent. Teams follows the system default when its device is left on "Same as
    System", so this is both simpler and one less thing to break when Teams
    ships a UI change.

    `page` is accepted and ignored so callers and tests keep the same shape.

    Returns a bool rather than raising: the caller must tell "could not switch,
    so do not speak" apart from "spoke and then could not restore", and those
    two want very different reactions.
    """
    if not device:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            SWITCH_AUDIO, "-t", "input", "-s", device,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=10)
        if proc.returncode != 0:
            return False
    except Exception:
        return False
    # Verified, not assumed: the switcher exits 0 for a name it did not apply.
    return (await current_mic()) == device


async def current_mic() -> str:
    """Whatever the system input is right now ('' if it cannot be read)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            SWITCH_AUDIO, "-c", "-t", "input",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return out.decode(errors="replace").strip()
    except Exception:
        return ""


async def _restore_mic(page) -> None:
    """Give the microphone back. Shouts only if it really did not go back.

    `set_call_mic` verifies by reading the input straight after writing it, and
    that read RACES: Teams grabs the system input for itself while a call is up
    (there is a "Microsoft Teams Audio" device, and it wins), so a switch that
    lands a moment later looked like a failure. Arun got the "you may be muted"
    warning again and again on calls where his microphone was fine — which is
    worse than useless, because the one time it is true he will have learned to
    ignore it.

    So: retry, then check what the input ACTUALLY is before saying anything, and
    say which device it is on rather than only that it is wrong.
    """
    from . import notify
    for attempt in range(3):
        if await set_call_mic(page, HIS_MIC):
            store.kv_set("mic_warned_for", "")
            return
        await asyncio.sleep(0.4 * (attempt + 1))
    now = await current_mic()
    if now == HIS_MIC:
        return                       # it landed; only the verify read was early
    # Once per call, not once per attempt. Repeating it is what made it noise.
    if store.kv_get("mic_warned_for") == now:
        return
    store.kv_set("mic_warned_for", now)
    await notify.notify(
        f"🎙️ Your mic is on {now or 'an unknown device'}, not {HIS_MIC} — you may "
        f"be muted. Set it in Teams → Settings → Devices.", "warn", urgency="direct")
