"""Supervise the WhatsApp bridge as a child of Asta's own lifecycle.

The bridge is a Node process (whatsapp/bridge.js) that Asta reaches over HTTP.
It kept being "not running" because starting it was a manual step, separate from
starting the server — so every restart of Asta silently dropped WhatsApp until
someone remembered to launch the bridge by hand.

A supervised child fixes that: when Asta starts, the bridge starts; if it
crashes, it is restarted with backoff; when Asta stops, the child is stopped
with it. This is the same thing launchd would do for a top-level service, but
scoped to Asta so dev and prod behave alike and there is one thing to run.

Ownership, so two Astas don't fight over one bridge: the supervisor only manages
a bridge it started itself. If a bridge is already answering on the port (a
hand-started one, or launchd's), it leaves it alone and just uses it. It never
kills a process it did not spawn.

Off switch: ASTA_WA_SUPERVISE=0 for when launchd owns the bridge, or when you
want to run it by hand.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = ROOT / "whatsapp"
BRIDGE_JS = BRIDGE_DIR / "bridge.js"

CHECK_SECONDS = 20
# Exponential backoff ceiling. A bridge that crashes instantly (missing deps,
# bad node) must not become a spawn loop that pins a core.
BACKOFF_START = 2
BACKOFF_MAX = 120

_proc: asyncio.subprocess.Process | None = None


def enabled() -> bool:
    """Supervise unless told not to, and only if the bridge is actually present."""
    if os.environ.get("ASTA_WA_SUPERVISE", "1").lower() in ("0", "false", "no"):
        return False
    return BRIDGE_JS.is_file()


def _port() -> int:
    return int(os.environ.get("WA_BRIDGE_PORT", "8323"))


async def _bridge_answering() -> bool:
    """True if SOMETHING is already serving the bridge HTTP — ours or not."""
    url = os.environ.get("WA_BRIDGE_URL", f"http://127.0.0.1:{_port()}")
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{url}/status")
            return r.status_code == 200
    except Exception:
        return False


async def _spawn() -> asyncio.subprocess.Process | None:
    """Launch the Node bridge as a child, logging where the server logs."""
    import shutil
    node = shutil.which("node")
    if not node:
        return None
    logdir = ROOT / "data" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    log = open(logdir / "whatsapp.log", "a")  # noqa: SIM115 — lives with the child
    log.write("\n--- bridge (re)started by Asta supervisor ---\n")
    log.flush()
    return await asyncio.create_subprocess_exec(
        node, str(BRIDGE_JS),
        cwd=str(BRIDGE_DIR),
        stdout=log, stderr=log,
        # The child inherits Asta's env, so ASTA_TOKEN and WA_* reach the bridge
        # without a second config to keep in sync.
        env=os.environ.copy(),
    )


async def supervise() -> None:
    """Keep the bridge alive for as long as Asta runs. Started once at startup."""
    global _proc
    backoff = BACKOFF_START
    while True:
        try:
            ours_alive = _proc is not None and _proc.returncode is None
            if ours_alive:
                backoff = BACKOFF_START            # healthy — reset the penalty
            elif await _bridge_answering():
                # Someone else owns a working bridge (hand-started / launchd).
                # Use it; never adopt or kill a process we didn't spawn.
                _proc = None
            else:
                proc = await _spawn()
                if proc is None:
                    # No node, or spawn failed. Back off and try again — the user
                    # may install node while Asta is running.
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                _proc = proc
                # Give it a moment; if it dies instantly, backoff on the next tick.
                await asyncio.sleep(3)
        except Exception:
            pass
        await asyncio.sleep(CHECK_SECONDS)


async def stop() -> None:
    """Stop the bridge WE started; leave a foreign one running."""
    global _proc
    if _proc is not None and _proc.returncode is None:
        try:
            _proc.terminate()
            try:
                await asyncio.wait_for(_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                _proc.kill()
        except ProcessLookupError:
            pass
    _proc = None


def status() -> dict:
    """For /api/status and health — is a supervised child alive?"""
    alive = _proc is not None and _proc.returncode is None
    return {"supervised": enabled(), "child_running": alive,
            "pid": (_proc.pid if alive else None)}
