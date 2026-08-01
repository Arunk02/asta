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
import os
import re
import shutil
import time
from pathlib import Path

from . import store, workspace_tools

# A red build is worth knowing about while he is still in the change that caused
# it. Ten minutes was long enough to have moved on to something else. GitHub API
# calls are free-tier cheap and cost zero LLM tokens, so the shorter poll costs
# nothing but the request.
POLL_SECONDS = int(os.environ.get("ASTA_CI_POLL", "300"))
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
        "also_watching": [s["match"] for s in subscriptions()],
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


SUBS_KEY = "ci_watch_subs"


def subscriptions() -> list[dict]:
    """Builds he explicitly asked to be told about, beyond his own work."""
    try:
        subs = json.loads(store.kv_get(SUBS_KEY) or "[]")
        return [s for s in subs if isinstance(s, dict) and s.get("match")]
    except (json.JSONDecodeError, TypeError):
        return []


def watch(what: str, repo: str = "") -> str:
    """Subscribe to a workflow, branch, or repo he does not own runs on.

    The default is deliberately narrow — his own work only — because the noisy
    version of this feature is the one that gets muted, and a muted watcher tells
    him nothing when it matters. This is the escape hatch for the release branch
    he cares about this week.
    """
    what = (what or "").strip()
    if not what:
        return "Name a workflow, branch or repo to watch."
    subs = subscriptions()
    if any(s["match"].lower() == what.lower() and s.get("repo", "") == repo for s in subs):
        return f"Already watching “{what}”."
    subs.append({"match": what, "repo": repo, "added": time.time()})
    store.kv_set(SUBS_KEY, json.dumps(subs[-20:]))
    where = f" in {repo}" if repo else ""
    return f"👁 Watching “{what}”{where} — I'll tell you when it goes red or recovers."


def unwatch(what: str) -> str:
    subs = subscriptions()
    kept = [s for s in subs if s["match"].lower() != (what or "").strip().lower()]
    store.kv_set(SUBS_KEY, json.dumps(kept))
    return (f"Stopped watching “{what}”." if len(kept) != len(subs)
            else f"Wasn't watching “{what}”.")


def _subscribed(repo: str, run: dict) -> bool:
    """Does an explicit subscription cover this run?

    Matched loosely against the repo, workflow and branch, because he says "watch
    the release build", not a fully-qualified triple.
    """
    haystack = " ".join((repo, run.get("workflowName", ""), run.get("headBranch", ""))).lower()
    for sub in subscriptions():
        if sub.get("repo") and sub["repo"] != repo:
            continue
        if sub["match"].lower() in haystack:
            return True
    return False


async def _my_branches(repo: str) -> set[str]:
    """Head branches of the open PRs he authored.

    "Runs he triggered" is not the same set as "his PRs", and the gap is where it
    hurt: a colleague pushes a fix to his branch, CI goes red on HIS pull request,
    and the actor filter drops it because the push was not his.
    """
    rc, out = await _run_gh("pr", "list", "-R", repo, "--author", "@me",
                            "--state", "open", "--json", "headRefName", timeout=20)
    if rc != 0:
        return set()
    try:
        return {p.get("headRefName", "") for p in json.loads(out or "[]")}
    except json.JSONDecodeError:
        return set()


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
            branches = await _my_branches(repo)
            runs = [r for r in runs
                    if str(r["databaseId"]) in mine
                    or r.get("headBranch") in branches
                    or _subscribed(repo, r)]
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


def _analyse_payload(note: str) -> dict | None:
    """Which workspace this failing repo lives in, so a "yes" runs the analysis
    with that project's context and code rather than in Asta's own repo. None
    when it can't be told — the brain falls back to figuring it out, as before.
    """
    try:
        from . import workspace as ws_mod
        wsname = ws_mod.infer(text=note)
        return {"workspace": wsname} if wsname else None
    except Exception:
        return None


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
    from . import notify, offers
    while True:
        try:
            if not await gh_ok():
                await asyncio.sleep(RETRY_UNAUTH_SECONDS)
                continue
            for note in await check_all():
                if note.startswith("🔴"):
                    # A red pipeline goes out IMMEDIATELY, not held until he steps
                    # away. It is his own branch and the useful moment to hear
                    # about it is while he is still in the change that caused it —
                    # which is precisely when the ambient hold was suppressing it.
                    #
                    # And it is an offer, not just a report: he can reply "yes"
                    # from his phone and the investigation starts without him
                    # opening a laptop. Nothing runs unasked.
                    o = offers.offer("analyse", note.split("\n")[0],
                                     "\n".join(note.split("\n")[1:]),
                                     "Want me to analyse the failure?",
                                     payload=_analyse_payload(note))
                    await notify.notify(o.render(), "ci", urgency="direct")
                else:
                    # Recovery is good news and good news does not interrupt.
                    await notify.notify(note, "ci", urgency="ambient")
        except Exception:
            pass
        await asyncio.sleep(POLL_SECONDS)
