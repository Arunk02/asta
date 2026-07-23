"""GitHub Actions pipeline watcher — failures come to your phone, not to a tab you forgot.

Uses the `gh` CLI (one-time: `gh auth login` — token lives in the system keychain,
never in .env). Watches every workspace repo with a github.com remote:

- 🔴 notify when a run COMPLETES with failure/cancelled/timed_out — ONCE per
  workflow+branch; retries of an already-red pipeline stay silent
- 🟢 notify when a previously-failing workflow+branch goes green again (recovery,
  with the failure count it took to get there)
- silent otherwise; the first poll only baselines history (no notification spam)

Poll: every 10 min (GitHub API calls are free-tier cheap; zero LLM tokens).
On demand: ask "ci status" in chat or GET /api/ci.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path

from . import store, workspace_tools

POLL_SECONDS = 600
RETRY_UNAUTH_SECONDS = 1800
BAD = ("failure", "cancelled", "timed_out", "startup_failure")
_auth_cache: dict = {"ok": None, "at": 0.0}


async def _run_gh(*args: str, timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "gh timed out"
    return proc.returncode or 0, raw.decode(errors="replace")


async def gh_ok(force: bool = False) -> bool:
    if not shutil.which("gh"):
        return False
    if not force and _auth_cache["ok"] is not None and time.time() - _auth_cache["at"] < 600:
        return _auth_cache["ok"]
    rc, _ = await _run_gh("auth", "status", timeout=15)
    _auth_cache.update(ok=(rc == 0), at=time.time())
    return _auth_cache["ok"]


def repos() -> list[str]:
    """owner/repo for every workspace service with a github.com remote."""
    found = []
    for ws_root in workspace_tools.WORKSPACES.values():
        # workspace root itself may be one big repo (e.g. iom-workspace)
        for repo in [Path(ws_root), *sorted(Path(ws_root).iterdir())]:
            cfg = repo / ".git" / "config"
            if not cfg.is_file():
                continue
            m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$",
                          cfg.read_text(), re.MULTILINE)
            if m:
                found.append(m.group(1))
    return found


def status() -> dict:
    authed = _auth_cache["ok"]
    return {
        "enabled": bool(shutil.which("gh")),
        "authed": authed,
        "repos": len(repos()),
        "only_runs_by": store.kv_get("gh_login") or "(resolving…)",
        "hint": ("install GitHub CLI: brew install gh" if not shutil.which("gh")
                 else ("run once: gh auth login" if authed is False
                       else ("checking auth…" if authed is None else "ok"))),
    }


async def my_login() -> str:
    """Arun's GitHub login (cached) — used to ignore other people's pipeline runs."""
    cached = store.kv_get("gh_login")
    if cached:
        return cached
    rc, out = await _run_gh("api", "user", "--jq", ".login", timeout=20)
    login = out.strip() if rc == 0 else ""
    if login:
        store.kv_set("gh_login", login)
    return login


def _prev_state(entry) -> tuple[str, int]:
    """Last conclusion for a workflow+branch and how many times it has repeated.

    Older entries were a bare conclusion string; treat those as a streak of one.
    """
    if isinstance(entry, list) and entry:
        return str(entry[0]), int(entry[1]) if len(entry) > 1 else 1
    return (entry or ""), 1


async def _poll_repo(repo: str) -> list[str]:
    """Diff this repo's recent runs against last poll; return notification lines.

    Only runs ARUN triggered are reported. Watching the whole team's commits was
    noise — ten failure pings in an evening for other people's branches.
    """
    rc, out = await _run_gh(
        "run", "list", "-R", repo, "--limit", "10", "--json",
        "databaseId,status,conclusion,workflowName,headBranch,displayTitle,url,event")
    if rc != 0:
        return []  # transient API/network error — next poll catches up
    runs = json.loads(out or "[]")
    me = await my_login()
    if me:
        rc2, out2 = await _run_gh(
            "run", "list", "-R", repo, "--limit", "10", "--user", me, "--json", "databaseId")
        if rc2 == 0:
            mine = {str(r["databaseId"]) for r in json.loads(out2 or "[]")}
            runs = [r for r in runs if str(r["databaseId"]) in mine]
    seen_key, wb_key = f"ci_seen:{repo}", f"ci_wb:{repo}"
    seen = json.loads(store.kv_get(seen_key) or "{}")
    wb = json.loads(store.kv_get(wb_key) or "{}")
    first_poll = not seen
    notes: list[str] = []
    for r in runs:
        rid, concl = str(r["databaseId"]), (r.get("conclusion") or "")
        if r.get("status") != "completed" or seen.get(rid) == "completed":
            continue
        seen[rid] = "completed"
        wbk = f"{r['workflowName']}|{r['headBranch']}"
        prev, streak = _prev_state(wb.get(wbk))
        repeat = concl in BAD and prev in BAD
        wb[wbk] = [concl, streak + 1 if repeat else 1]
        if first_poll:
            continue  # baseline history silently
        name = repo.split("/")[-1]
        if concl in BAD:
            if repeat:
                continue  # he already knows this workflow+branch is red; one ping is enough
            notes.append(f"🔴 CI {concl}: {name} · {r['workflowName']} "
                         f"({r['headBranch']}) — {r['displayTitle'][:60]}\n{r['url']}")
        elif concl == "success" and prev in BAD:
            after = f" after {streak} failures" if streak > 1 else ""
            notes.append(f"🟢 CI recovered{after}: {name} · {r['workflowName']} "
                         f"({r['headBranch']})")
    store.kv_set(seen_key, json.dumps(dict(list(seen.items())[-40:])))
    store.kv_set(wb_key, json.dumps(wb))
    return notes


async def check_all() -> list[str]:
    notes: list[str] = []
    for repo in repos():
        try:
            notes += await _poll_repo(repo)
        except Exception:
            continue
    return notes


async def recent_runs(limit: int = 8) -> str:
    """Human-readable recent runs across repos — the on-demand 'ci status'."""
    if not await gh_ok():
        return "CI watcher is off — " + status()["hint"]
    lines = []
    for repo in repos():
        rc, out = await _run_gh(
            "run", "list", "-R", repo, "--limit", "3", "--json",
            "status,conclusion,workflowName,headBranch,displayTitle")
        if rc != 0:
            continue
        for r in json.loads(out or "[]")[:limit]:
            state = r.get("conclusion") or r.get("status")
            lines.append(f"{repo.split('/')[-1]}: [{state}] {r['workflowName']} "
                         f"({r['headBranch']}) {r['displayTitle'][:50]}")
    return "\n".join(lines) or "No recent workflow runs found."


async def loop() -> None:
    from . import notify
    while True:
        try:
            if not await gh_ok():
                await asyncio.sleep(RETRY_UNAUTH_SECONDS)
                continue
            for note in await check_all():
                # His own pipelines, but still not something addressed TO him:
                # held while he's at the laptop, delivered when he steps away.
                await notify.notify(note, "ci", urgency="ambient")
        except Exception:
            pass
        await asyncio.sleep(POLL_SECONDS)
