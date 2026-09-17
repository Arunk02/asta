"""When the test bench may spend a brain, and how much of one.

His decision, taken on 11 September: the live tier runs at night, on quota that
would otherwise expire unused, and never on anything he pays for by the token.
That is a policy, so it lives in code with the reasons attached rather than in a
crontab nobody reads.

  WHOSE QUOTA. The local model first; then the Claude and Codex subscription
  windows, which reset whether or not they were used. Never Copilot's monthly
  pool — that one does not reset until the billing period and every leg spent
  here is a leg he does not have for work. Never a metered API key.

  WHEN. Between 01:00 and 05:00, and only if he has not used that brain in the
  last hour — a run that competes with him for a session window is worse than no
  run at all.

  HOW MUCH. 30 legs a night, 120 a week. If he hits a usage limit the morning
  after a run, the next night's budget halves itself, because the evidence says
  the run took something he needed.

The run itself happens in a SUBPROCESS (`python -m app.workworld run --live`),
never in the server: the bench patches the database and every outward door, and
doing that inside the live process would take Asta off the air while it ran.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BUDGET_FILE = ROOT / "data" / "workworld" / "budget.json"

WINDOW = (1, 5)                     # local hours, [start, end)
NIGHTLY_LEGS = int(os.environ.get("ASTA_BENCH_LEGS", "30"))
WEEKLY_LEGS = int(os.environ.get("ASTA_BENCH_WEEKLY_LEGS", "120"))
IDLE_BEFORE_MINUTES = 60
POLL_SECONDS = 900


def enabled() -> bool:
    return os.environ.get("ASTA_BENCH_NIGHTLY", "").strip().lower() in ("1", "true", "yes", "on")


def _budget() -> dict:
    try:
        return json.loads(BUDGET_FILE.read_text())
    except (OSError, ValueError):
        return {"nightly_legs": NIGHTLY_LEGS, "spent_week": 0, "week": "", "last_run": ""}


def _save(b: dict) -> None:
    BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_FILE.write_text(json.dumps(b, indent=1))


def _week_of(now: dt.datetime) -> str:
    return f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"


def quiet_window(now: dt.datetime | None = None) -> str:
    """'' when the night is free for work that costs NO brain, else the reason.

    The deterministic bench and the evolution loop spend no legs at all, so
    gating them on the live tier's flag and leg budget meant P6 could be on and
    never run — which is exactly what it did the first night it was switched on.
    The two things they do share is his machine and his sleep: the window, and
    not competing with him while he is working.
    """
    from app import store
    now = now or dt.datetime.now()
    if not (WINDOW[0] <= now.hour < WINDOW[1]):
        return f"outside the window {WINDOW[0]:02d}:00–{WINDOW[1]:02d}:00"
    try:
        last = float(store.kv_get("last_user_message_at") or 0)
    except (TypeError, ValueError):
        last = 0
    if last and (time.time() - last) < IDLE_BEFORE_MINUTES * 60:
        return "he was working within the hour"
    return ""


def why_not(now: dt.datetime | None = None) -> str:
    """'' when the bench may run tonight, else the reason it may not."""
    from app import agent, store
    now = now or dt.datetime.now()
    if not enabled():
        return "nightly bench is off (ASTA_BENCH_NIGHTLY)"
    if not (WINDOW[0] <= now.hour < WINDOW[1]):
        return f"outside the window {WINDOW[0]:02d}:00–{WINDOW[1]:02d}:00"
    b = _budget()
    if b.get("last_run", "")[:10] == now.date().isoformat():
        return "already ran today"
    if b.get("week") == _week_of(now) and b.get("spent_week", 0) >= WEEKLY_LEGS:
        return f"the week's {WEEKLY_LEGS} legs are spent"
    brains = [c for c in ("claude_cli", "codex") if agent.available(c)
              and not agent.quota_down(c)]
    if not brains:
        return "no subscription brain is up"
    # He was working — his window is his.
    try:
        last = float(store.kv_get("last_user_message_at") or 0)
    except (TypeError, ValueError):
        last = 0
    if last and (time.time() - last) < IDLE_BEFORE_MINUTES * 60:
        return "he used a brain within the hour"
    return ""


def note_limit_hit_today() -> None:
    """Called when a usage limit lands the morning after a run: halve the budget.

    Self-shrinking rather than self-explaining: the only honest evidence that
    the bench is taking something he needs is him running out after it ran.
    """
    b = _budget()
    b["nightly_legs"] = max(5, int(b.get("nightly_legs", NIGHTLY_LEGS) // 2))
    b["shrunk_at"] = dt.datetime.now().isoformat(timespec="minutes")
    _save(b)


async def run_once(k: int = 4) -> str:
    """Spend the night's budget. Returns a one-line result for the log."""
    now = dt.datetime.now()
    b = _budget()
    legs = int(b.get("nightly_legs", NIGHTLY_LEGS))
    cmd = [sys.executable, "-m", "app.workworld", "run", "--live", "--set", "live",
           "--k", str(k), "--quiet"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3600)
    except asyncio.TimeoutError:
        proc.kill()
        return "live bench timed out after an hour"
    spent = min(legs, k * 3)             # three live scenarios, k runs each
    if b.get("week") != _week_of(now):
        b["week"], b["spent_week"] = _week_of(now), 0
    b["spent_week"] = b.get("spent_week", 0) + spent
    b["last_run"] = now.isoformat(timespec="minutes")
    _save(b)
    return (out or b"").decode()[-400:].strip() or "live bench finished"


async def loop() -> None:
    """Supervised by daemon.start. Pushes nothing: the scorecard is where a
    night's result belongs, and a notification at 02:00 is the opposite of the
    interrupt budget this whole plan is trying to earn."""
    from app import store
    while True:
        await asyncio.sleep(POLL_SECONDS)
        if why_not():
            continue
        try:
            note = await run_once()
        except Exception as exc:                                # noqa: BLE001
            store.record_outcome("bench", "failed", subject="nightly",
                                 detail=f"{type(exc).__name__}: {exc}"[:200])
            continue
        store.record_outcome("bench", "ran", subject="nightly", detail=note[:200])
