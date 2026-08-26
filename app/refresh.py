"""Auto-refresh of project context (context drift) and graphify.

Cadence follows the the project-context philosophy: drift *detection* is
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
import re
import subprocess
import time
from pathlib import Path

from . import notify, store
from . import workspace as ws_mod

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
    ws = ws_mod.get(workspace)
    if ws is None or not ws.exists():
        return None
    ws_root = ws.path
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


#: Days of neglect that turn a shrug into something worth his day.
#:
#: One threshold, not a ladder. The first draft had two (7 and 21) and both
#: returned the same rank, which reads as an escalation and is not one. Stale
#: context genuinely tops out below breakage: an agent working from month-old
#: notes gives worse answers, but nothing is on fire, and putting that in the
#: same tier as a dead production service is how the top tier stops meaning
#: anything.
STALE_TODAY_DAYS = 7


def drift_key(workspace: str, detail: str) -> str:
    """One identity for one state of staleness.

    Without this, drift is a string and every poll is a brand-new problem: the
    same warning went out five times in eighty minutes on 2026-08-03 and taught
    Arun to stop reading it. Keyed on WHICH mini-skills are stale, so re-detecting
    the same rot is one ledger row with a rising seen_count, while genuinely new
    rot is genuinely new.
    """
    skills = sorted(set(re.findall(r"([a-z-]+/[a-z0-9-]+\.md)", detail or "")))
    from . import triage
    return f"ctx:{workspace}:" + (triage.stable_key(",".join(skills)) if skills else "unknown")


def stale_days(workspace: str, now: float | None = None) -> float:
    """How long this workspace has gone without an enrichment. -1 = never."""
    raw = store.kv_get(f"last_enriched:{workspace}")
    if not raw:
        return -1.0
    return ((now or time.time()) - float(raw)) / 86400


def note_enriched(workspace: str, now: float | None = None) -> None:
    """Stamp that the context was actually brought up to date — not merely checked.

    Deliberately separate from `last_refresh`, which only means "we looked". The
    difference between those two is the entire question, and conflating them is
    how a workspace can be checked every ten minutes for six weeks and still be
    six weeks out of date.
    """
    store.kv_set(f"last_enriched:{workspace}", str(now or time.time()))


def _staleness_priority(workspace: str, now: float | None = None) -> int:
    """How loud stale context has earned the right to be.

    Never enriched counts as the worst case rather than the mildest: no context
    at all is not a gentler version of slightly-old context — every answer about
    that workspace is guesswork, and it will not improve on its own.
    """
    from . import attention
    days = stale_days(workspace, now)
    if days < 0 or days >= STALE_TODAY_DAYS:
        return attention.P_TODAY
    return attention.P_FYI


async def _report_stale(workspace: str, ws_root, detail: str, lines: list[str],
                        reason: str) -> str:
    """Say it once, rank it by how long it has been ignored, and make yes cheap.

    The old path pushed a plain message ending in "say 'rebuild the stale
    context'" — an exact phrase he had to remember, retype, and be at a keyboard
    for. Every other outward act in Asta is a one-tap yes. This one asked him to
    do homework, so it never got done.
    """
    from . import attention, notify, offers
    days = stale_days(workspace)
    age = "never enriched" if days < 0 else f"{days:.0f} days since last enrichment"
    key = drift_key(workspace, detail)
    priority = _staleness_priority(workspace)
    priority, chase = attention.escalate_for_chase(priority, key)

    lines.append(f"⚠️ project context is stale ({age}):\n{detail[:1200]}")
    summary = "\n".join(lines)

    # The ledger answers "has he already been told this exact thing" — the whole
    # reason five identical alerts in eighty minutes cannot happen again.
    speak = attention.consider("context", key, who=workspace,
                               what=f"{workspace} context stale — {age}",
                               priority=priority,
                               why=f"context drift{chase}")
    if not speak:
        store.kv_set(f"last_refresh:{workspace}", str(time.time()))
        return summary + "\n(already reported — not repeated)"

    offers.propose(
        subject=f"🗂 {workspace} context is stale",
        context=f"{age}\n{detail[:900]}",
        question="Want me to bring the context up to date?",
        action=(f"Arun approved refreshing the '{workspace}' project context. Run the "
                f"context enrichment pass over the DRIFTED mini-skills only, following "
                f"the workspace skill's Step 5b quality bar: capture contracts, "
                f"invariants, decisions-with-reasons and cross-repo edges — never "
                f"'added a null check', never a restatement of the diff. Patch at most "
                f"10 lines per mini-skill, keep every fact carrying (source: path:line), "
                f"and where a commit changed nothing the context claims, change nothing "
                f"and say so. Then re-index and stamp verified_against = HEAD."),
        payload={"workspace": workspace})
    await notify.notify(offers.pending().render(), "info",
                        priority=priority if attention.enabled() else None,
                        considered=True)   # attention.consider ran above
    store.kv_set(f"last_refresh:{workspace}", str(time.time()))
    return summary


async def refresh_workspace(workspace: str, reason: str = "manual") -> str:
    """Check whether a workspace's project context is stale, and regenerate the
    graph. Returns a summary.

    Detection is deterministic and free, so it runs whenever code changed. The
    token-costly enrichment is never triggered here — drift is reported and the
    user decides. Notifies only when there is something to act on; a clean check
    stays silent unless it was asked for.
    """
    ws = ws_mod.get(workspace)
    if ws is None or not ws.exists():
        return f"Unknown workspace '{workspace}'."
    ws_root = ws.path
    lines = [f"🔄 Context refresh — {workspace} ({reason})"]
    noteworthy = reason.startswith("manual") or reason.startswith("requested")

    try:
        stale, detail = await ws_mod.drift(workspace)
    except (ValueError, OSError) as exc:
        stale, detail = False, f"drift check failed: {exc}"

    if stale:
        return await _report_stale(workspace, ws_root, detail, lines, reason)
    if detail:
        lines.append(f"✓ project context in sync ({detail[:200]})"
                     if detail.strip() else "✓ project context in sync")
    else:
        lines.append("✓ project context in sync")

    graph_cmd = os.environ.get("GRAPHIFY_CMD", "").strip()
    if graph_cmd:
        rc, out = await _run(graph_cmd.replace("{workspace}", str(ws_root)), ws_root, shell=True)
        if rc != 0:
            noteworthy = True
        lines.append("✓ graph regenerated" if rc == 0 else f"❌ graph regen failed (rc={rc}): {out[-400:]}")
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
            for ws, info in ws_mod.available_workspaces().items():
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
