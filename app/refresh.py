"""Auto-refresh of project context (contmark drift) and graphify.

Cadence follows the contmark-workspace skill's philosophy: drift *detection* is
deterministic and free (check-drift.js diffs verified_against..HEAD — no LLM),
so run it whenever the code actually changed; the token-costly *enrichment*
(evolution loop rewriting mini-skills) is never run automatically — drift is
reported and Arun decides.

- Change-triggered (primary): every 10 min a cheap git fingerprint runs; a refresh
  fires only when a repo got new commits or many dirty files — i.e. a feature was
  actually developed. No change → nothing runs, nothing is notified.
- Weekly baseline: REFRESH_EVERY_DAYS (default 7) at REFRESH_AT, as a safety net
  for drift the fingerprint missed (e.g. rebases that keep HEAD count).
- Notifications only when there IS drift or a failure — "in sync" stays silent.
- Graph regeneration runs GRAPHIFY_CMD (set it in .env, executed at the workspace
  root, "{workspace}" placeholder available). Without it, drift is still checked
  and reported — regeneration is skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from . import notify, store, workspace_tools

BIG_CHANGE_DIRTY_FILES = 40


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20
        ).stdout.strip()
    except Exception:
        return ""


def _workspace_fingerprint(ws_root: Path) -> dict:
    """Per-repo HEAD + dirty file count."""
    fp = {}
    for d in sorted(ws_root.iterdir()):
        if d.is_dir() and (d / ".git").exists():
            head = _git(d, "rev-parse", "HEAD")
            dirty = len(_git(d, "status", "--porcelain").splitlines())
            fp[d.name] = {"head": head, "dirty": dirty}
    return fp


def detect_big_change(workspace: str) -> str | None:
    """Compare fingerprint with last stored; return a reason string if it changed a lot."""
    ws_root = Path(workspace_tools.WORKSPACES[workspace])
    if not ws_root.is_dir():
        return None
    fp = _workspace_fingerprint(ws_root)
    key = f"ws_fingerprint:{workspace}"
    prev_raw = store.kv_get(key)
    store.kv_set(key, json.dumps(fp))
    if not prev_raw:
        return None
    prev = json.loads(prev_raw)
    reasons = []
    for repo, cur in fp.items():
        old = prev.get(repo, {})
        if old.get("head") and old["head"] != cur["head"]:
            reasons.append(f"{repo}: new commits")
        elif cur["dirty"] >= BIG_CHANGE_DIRTY_FILES and cur["dirty"] > old.get("dirty", 0):
            reasons.append(f"{repo}: {cur['dirty']} modified files")
    return "; ".join(reasons) if reasons else None


async def _run(cmd: list[str] | str, cwd: Path, shell: bool = False, timeout: int = 900) -> tuple[int, str]:
    if shell:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "(timed out)"
    return proc.returncode or 0, out.decode(errors="replace")


async def refresh_workspace(workspace: str, reason: str = "manual") -> str:
    """Run check-drift + graph regeneration for one workspace; returns a summary.

    Notifies only when there's something to act on (drift / failure) — a clean
    check stays silent unless it was manually requested.
    """
    ws_root = Path(workspace_tools.WORKSPACES[workspace])
    lines = [f"🔄 Context refresh — {workspace} ({reason})"]
    noteworthy = reason.startswith("manual") or reason.startswith("requested")

    drift_js = ws_root / ".contmark" / "check-drift.js"
    if drift_js.is_file():
        rc, out = await _run(["node", str(drift_js), str(ws_root)], ws_root)
        drift = out.strip()
        if "DRIFT" in drift:
            n = drift.count("DRIFT")
            noteworthy = True
            lines.append(f"⚠️ contmark drift in {n} repo(s):\n" + drift[:1200])
            lines.append("→ say \"update the stale context\" to run the contmark evolution "
                         "loop on it (uses tokens; your call).")
        else:
            lines.append("✓ contmark context in sync")
    else:
        lines.append("no check-drift.js in workspace")

    graph_cmd = os.environ.get("GRAPHIFY_CMD", "").strip()
    if graph_cmd:
        rc, out = await _run(graph_cmd.replace("{workspace}", str(ws_root)), ws_root, shell=True)
        if rc != 0:
            noteworthy = True
        lines.append("✓ graphify regenerated" if rc == 0 else f"❌ graphify failed (rc={rc}): {out[-400:]}")
    else:
        lines.append("graph regen skipped (set GRAPHIFY_CMD in .env to enable)")

    summary = "\n".join(lines)
    if noteworthy:
        await notify.notify(summary, "info")
    store.kv_set(f"last_refresh:{workspace}", str(time.time()))
    return summary


def _weekly_due(now: float) -> bool:
    """True when the weekly baseline refresh is due (REFRESH_EVERY_DAYS at REFRESH_AT)."""
    every_days = int(os.environ.get("REFRESH_EVERY_DAYS", "7") or 7)
    if every_days <= 0:
        return False
    last = float(store.kv_get("last_baseline_refresh") or 0)
    if now - last < every_days * 86400:
        return False
    hh, mm = (int(x) for x in os.environ.get("REFRESH_AT", "18:30").split(":"))
    t = time.localtime(now)
    return (t.tm_hour, t.tm_min) >= (hh, mm)


async def scheduler_loop() -> None:
    """Runs forever: change-triggered refresh (10-min git fingerprint) + weekly baseline."""
    while True:
        try:
            now = time.time()
            baseline = _weekly_due(now)
            if baseline:
                store.kv_set("last_baseline_refresh", str(now))
            for ws, info in workspace_tools.available_workspaces().items():
                if not info["exists"]:
                    continue
                change = await asyncio.to_thread(detect_big_change, ws)
                if change:
                    await refresh_workspace(ws, reason=f"code changed: {change}")
                elif baseline:
                    await refresh_workspace(ws, reason="weekly baseline")
        except Exception:
            pass
        await asyncio.sleep(600)
