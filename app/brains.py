"""How much of each brain is left — measured, said up front, never guessed.

His words, 30 Sep: "if the session is going to be reached, tell me upfront; do
as much as you can and continue from there when the session comes back; for
Copilot say the monthly quota is out — tell it exactly — and ask if I want to
switch brains to pick up from there."

Claude: every call anyone makes on his subscription — Asta's CLI calls, his own
Claude Code sessions — is written by Claude Code to ~/.claude/projects/**.jsonl
with its token usage. That is the one complete record, so it is read (new bytes
only, per file) rather than Asta metering itself and missing the rest.
Measured 30 Sep: in the 5 hours to 11:50, 99% of the usage was his interactive
Claude Code session and 1% was Asta.

The ceiling is LEARNED from the limit hits themselves: what the window held
when the CLI said "You've hit your session limit". Calibrated on the two hits
of 30 Sep, the only weighting under which they agree counts cache writes and
output and not cache reads (14.1M and 12.1M weighted, against 40.7M and 23.4M
with cache reads in).

Copilot: a monthly premium-request pool. When it is out, it is out until the
billing month turns (the 1st) — said as a date, not as "try later".
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import time
from pathlib import Path

from . import store

WINDOW = 5 * 3600
PROJECTS = Path(os.path.expanduser(os.environ.get("ASTA_CLAUDE_PROJECTS", "~/.claude/projects")))
#: Share of the learned ceiling at which he is told, once per window.
WARN_AT = float(os.environ.get("ASTA_CLAUDE_WARN_AT", "0.8") or 0.8)
#: Share at which Asta's own background reading steps down to a cheaper model.
TIGHT_AT = float(os.environ.get("ASTA_CLAUDE_TIGHT_AT", "0.7") or 0.7)
#: Cache reads, per token. 0 is what the two measured limit hits agree on.
CACHE_READ_WEIGHT = float(os.environ.get("ASTA_CLAUDE_CACHE_READ_WEIGHT", "0") or 0)
#: Used until a limit hit has been measured on this machine.
DEFAULT_CEILING = 12_000_000.0

_LEVELS_KEY = "claude_limit_levels"
_RESETS_KEY = "claude_resets"
_WARNED_KEY = "brains_warned_window"
_START_KEY = "claude_window_start"
_STATUS_KEY = "brains_status"

#: path -> (bytes read, {message id: (ts, weight, is_asta)})
_cache: dict[str, tuple[int, dict]] = {}


def _factor(model: str) -> float:
    m = (model or "").lower()
    return 5.0 if "opus" in m else (1 / 3 if "haiku" in m else 1.0)


def weight(usage: dict, model: str) -> float:
    u = usage or {}
    return (u.get("input_tokens", 0) + 1.25 * u.get("cache_creation_input_tokens", 0)
            + CACHE_READ_WEIGHT * u.get("cache_read_input_tokens", 0)
            + 5 * u.get("output_tokens", 0)) * _factor(model)


def _is_asta(path: str) -> bool:
    """Asta's own calls run in its repo or in a task worktree."""
    d = Path(path).parent.name
    return d.endswith("-help-asta") or "-asta-worktrees-" in d or d.endswith("-booking-workspace")


def _read(path: str) -> dict:
    """New usage rows in one session file, cached by byte offset."""
    size = os.path.getsize(path)
    done, rows = _cache.get(path, (0, {}))
    if size < done:                           # rewritten: start again
        done, rows = 0, {}
    if size == done:
        return rows
    asta = _is_asta(path)
    with open(path, "rb") as fh:
        fh.seek(done)
        for raw in fh:
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            m = d.get("message") or {}
            mid = m.get("id") or d.get("requestId")
            if not mid or not m.get("usage") or not d.get("timestamp"):
                continue
            try:
                ts = dt.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            rows[mid] = (ts, weight(m["usage"], m.get("model", "")), asta)
    _cache[path] = (size, rows)
    return rows


def events(since: float) -> list[tuple[float, float, bool]]:
    out: dict = {}
    for path in glob.glob(str(PROJECTS / "*" / "*.jsonl")):
        try:
            if os.path.getmtime(path) < since:
                continue
            out.update(_read(path))
        except OSError:
            continue
    return sorted(v for v in out.values() if v[0] >= since)


def _floats(key: str) -> list[float]:
    try:
        return [float(x) for x in json.loads(store.kv_get(key) or "[]")]
    except (ValueError, TypeError):
        return []


def window_start(now: float, evs: list | None = None) -> float:
    """Where the current 5-hour window began.

    A stated reset still ahead ("resets 3:40pm") fixes it exactly: five hours
    before. Otherwise windows are fixed blocks, as the subscription counts
    them: from the first use after the previous block ended, for five hours.

    The previous block's end has to be a FACT — a reset that was stated, or the
    block this function last settled on — never "the first use I can still
    see". Chaining from the oldest event inside a ten-hour lookback made the
    chain's origin slide with the clock: on 30 Sep the warning said "resets
    3:03pm", then 3:06pm, then 3:09pm, stayed at 100% after the real 3:00pm
    reset, and was sent again each time because the window kept being new."""
    resets = _floats(_RESETS_KEY)
    ahead = [r for r in resets if now < r <= now + WINDOW]
    if ahead:
        return min(ahead) - WINDOW
    kept = (_floats(_START_KEY) or [0.0])[-1]
    if kept and kept <= now < kept + WINDOW:
        return kept
    stamps = sorted(e[0] for e in (evs if evs is not None else events(now - 2 * WINDOW))
                    if e[0] <= now)
    ended = max([r for r in resets if r <= now] + ([kept + WINDOW] if kept else []),
                default=0.0)
    start = None
    for ts in stamps:
        if ts < ended:
            continue
        if start is None or ts >= start + WINDOW:
            start = ts
    if start is None or now >= start + WINDOW:
        return now                             # nothing used in a live window yet
    store.kv_set(_START_KEY, json.dumps([start]))
    return start


def ceiling() -> float:
    levels = _floats(_LEVELS_KEY)
    return min(levels[-5:]) if levels else DEFAULT_CEILING


def record_hit(now: float, reset_at: float | None) -> None:
    """A limit was hit: what the window held is the ceiling, as measured."""
    start = (reset_at - WINDOW) if reset_at else now - WINDOW
    used = sum(w for ts, w, _asta in events(start) if ts < now)
    if used > 0:
        levels = _floats(_LEVELS_KEY)[-9:] + [used]
        store.kv_set(_LEVELS_KEY, json.dumps(levels))
    if reset_at:
        store.kv_set(_RESETS_KEY, json.dumps((_floats(_RESETS_KEY) + [reset_at])[-20:]))


def calibrate_from_history(days: float = 3) -> int:
    """Seed the ceiling from limit hits already on record (claude_cli writes an
    outcome for each). Once — later hits are recorded as they happen."""
    if store.kv_get(_LEVELS_KEY):
        return 0
    n = 0
    try:
        rows = [r for r in store.recent_outcomes(3000)
                if r.get("kind") == "claude" and r.get("outcome") == "limited"
                and r.get("created_at", 0) > time.time() - days * 86400]
    except Exception:                                          # noqa: BLE001
        return 0
    for r in sorted(rows, key=lambda r: r["created_at"]):
        hit = float(r["created_at"])
        reset = _reset_from(r.get("detail") or "", hit)
        record_hit(hit, reset)
        n += 1
    return n


def _reset_from(detail: str, hit: float) -> float | None:
    from . import agent
    return agent.limit_reset_at(detail, now=hit)


def claude_status(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    evs = events(now - 2 * WINDOW)
    start = window_start(now, evs)
    inside = [e for e in evs if start <= e[0] <= now]
    used = sum(w for _, w, _ in inside)
    asta = sum(w for _, w, a in inside if a)
    from . import claude_cli
    limited = claude_cli.limited_until(now)
    ceil = ceiling()
    return {"used": used, "asta": asta, "yours": used - asta, "ceiling": ceil,
            "share": min(1.0, used / ceil) if ceil else 0.0,
            "resets_at": limited or (start + WINDOW), "limited": bool(limited),
            "measured_hits": len(_floats(_LEVELS_KEY))}


def copilot_status(now: float | None = None) -> dict:
    from . import agent
    now = time.time() if now is None else now
    down = False
    try:
        down = agent.quota_down("copilot")
    except Exception:                                          # noqa: BLE001
        pass
    d = dt.date.fromtimestamp(now)
    first = (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    return {"out": down, "resets_on": first.strftime("%-d %b")}


def local_status() -> dict:
    try:
        from . import memory
        return {"on": bool(memory.local_llm_model())}
    except Exception:                                          # noqa: BLE001
        return {"on": False}


def _hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%-I:%M%p").lower()


def brain_status(now: float | None = None) -> str:
    """"brain status" — every brain, exactly."""
    now = time.time() if now is None else now
    c = claude_status(now)
    cp = copilot_status(now)
    lo = local_status()
    if c["limited"]:
        claude = f"Claude: limit reached, resets {_hhmm(c['resets_at'])}"
    else:
        claude = (f"Claude: {c['share']:.0%} of this session window used, resets "
                  f"{_hhmm(c['resets_at'])} (you {c['yours'] / 1e6:.1f}M · Asta {c['asta'] / 1e6:.1f}M)")
    copilot = (f"Copilot: monthly quota out, resets {cp['resets_on']}" if cp["out"]
               else "Copilot: available")
    local = "Local model: on (chat only — it cannot run tasks)" if lo["on"] else "Local model: off"
    from . import tasks
    q = tasks.queue_summary()
    work = f"Work: {q['running']} running, {q['queued']} queued, {q['paused']} paused"
    return "\n".join([claude, copilot, local, work])


def tight(now: float | None = None) -> bool:
    """Is the Claude window nearly spent? Read from the cached status the loop
    keeps, so the reader can ask on every sweep at no cost."""
    try:
        s = json.loads(store.kv_get(_STATUS_KEY) or "{}")
    except ValueError:
        return False
    return bool(s) and (s.get("limited") or float(s.get("share") or 0) >= TIGHT_AT)


async def tick(notify=None, now: float | None = None) -> dict:
    """Refresh the cached status; warn ONCE per window at WARN_AT."""
    import asyncio
    now = time.time() if now is None else now
    s = await asyncio.to_thread(claude_status, now)
    store.kv_set(_STATUS_KEY, json.dumps(s))
    window = str(int(s["resets_at"] // 60))
    if (s["share"] >= WARN_AT and not s["limited"]
            and store.kv_get(_WARNED_KEY) != window and notify):
        store.kv_set(_WARNED_KEY, window)
        from . import tasks
        q = tasks.queue_summary()
        cp = copilot_status(now)
        alt = ("Say “use copilot” to move the work there now."
               if not cp["out"] else
               f"Copilot's monthly quota is out until {cp['resets_on']}, so it stays on Claude.")
        await notify(
            f"⚠️ Claude is at {s['share']:.0%} of this session window, which resets "
            f"{_hhmm(s['resets_at'])} (you {s['yours'] / 1e6:.1f}M, Asta "
            f"{s['asta'] / 1e6:.1f}M). {q['running']} running, {q['queued']} queued — I'll keep "
            f"going, and anything that doesn't fit continues at {_hhmm(s['resets_at'])}. {alt}",
            "brains", urgency="direct", considered=True)
    return s


async def loop() -> None:
    import asyncio
    from . import notify
    await asyncio.to_thread(calibrate_from_history)
    while True:
        try:
            await tick(notify.notify)
        except Exception as exc:                               # noqa: BLE001
            from . import quiet
            quiet.note("brains.tick", exc)
        await asyncio.sleep(int(os.environ.get("ASTA_BRAINS_EVERY_SECONDS", "300") or 300))
