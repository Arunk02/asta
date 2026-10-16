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

from . import repo_ops, store, workspace_tools

# A red build is worth knowing about while he is still in the change that caused
# it. Ten minutes was long enough to have moved on to something else. GitHub API
# calls are free-tier cheap and cost zero LLM tokens, so the shorter poll costs
# nothing but the request.
POLL_SECONDS = int(os.environ.get("ASTA_CI_POLL", "300"))
RETRY_UNAUTH_SECONDS = 1800
BAD = ("failure", "cancelled", "timed_out", "startup_failure")
_auth_cache: dict = {"ok": None, "at": 0.0}


async def _run_gh(*args: str, timeout: float = 30) -> tuple[int, str]:
    try:
        env = await repo_ops.github_env(Path.cwd(), *args)
    except RuntimeError as exc:
        return 1, str(exc)
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
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


#: The branches his merges land on. Their builds are his to know about, whoever
#: merged: the run after booking PR 1487 was Vinish's merge, so "runs he
#: triggered" never saw it (16 Oct).
BASE_BRANCHES = re.compile(os.environ.get("ASTA_CI_BASE_BRANCHES",
                                          r"^(?:main|master|develop|release/.+)$"))


#: Jobs that are not CI: the Sanity runs ("sanity error is fine, ignore") and
#: the deploy stages ("if any error in deploy, they will tell us") — 16 Oct.
IGNORED_JOBS = re.compile(os.environ.get(
    "ASTA_CI_IGNORE_JOBS", r"sanity|sleep|\b(?:dev|qa|sit|spt|uat|perf|preprod|prod)\s*$"), re.I)

#: Which of a branch's workflows are its build. Not the bots: empv3's "Auto merge
#: PR" on main is the noise he stopped on 15 Oct; label checks and scans neither.
BRANCH_WORKFLOWS = re.compile(r"build|deploy|\bci\b|test|pipeline|release", re.I)
_NOT_A_BUILD = re.compile(r"auto.?merge|label|codeql|scan|stale|dependabot|lint", re.I)


def _is_branch_build(run: dict) -> bool:
    name = run.get("workflowName") or ""
    return (run.get("event") == "push" and bool(BASE_BRANCHES.match(run.get("headBranch") or ""))
            and bool(BRANCH_WORKFLOWS.search(name)) and not _NOT_A_BUILD.search(name))


def _seen_id(run: dict) -> str:
    """A re-run is a new result for the same run: attempt 3 can pass after 1 and 2 failed."""
    rid, attempt = str(run["databaseId"]), int(run.get("attempt") or 1)
    return rid if attempt <= 1 else f"{rid}:{attempt}"


async def _jobs_verdict(repo: str, rid: str) -> tuple[str, list[str]]:
    """('failure', [jobs]) / ('success', []) for a run parked at an approval gate,
    or ('', []) while any job is still running."""
    rc, out = await _run_gh("run", "view", rid, "-R", repo, "--json", "jobs")
    if rc != 0:
        return "", []
    try:
        jobs = json.loads(out or "{}").get("jobs") or []
    except json.JSONDecodeError:
        return "", []
    ci = [j for j in jobs if not IGNORED_JOBS.search(j.get("name") or "")]
    failed = [j.get("name", "") for j in ci if (j.get("conclusion") or "") in BAD]
    if failed:
        return "failure", failed
    if not ci or any(j.get("status") != "completed" for j in ci):
        return "", []
    return "success", []


_MERGED_PR = re.compile(r"Merge pull request #(\d+)|\(#(\d+)\)\s*$", re.I)


async def _his_merge(repo: str, run: dict, me: str) -> str:
    """The number of HIS pull request this branch build is the merge of, or ''."""
    m = _MERGED_PR.search(run.get("displayTitle") or "")
    if not m or not me:
        return ""
    number = m.group(1) or m.group(2)
    key = f"ci_pr_author:{repo}#{number}"
    author = store.kv_get(key)
    if author is None:
        rc, out = await _run_gh("pr", "view", number, "-R", repo, "--json", "author",
                                "-q", ".author.login")
        if rc != 0:
            return ""
        author = out.strip()
        store.kv_set(key, author)
    return number if author.lower() == me.lower() else ""


async def _merged_results(repo: str, merges: list[dict], me: str) -> list[str]:
    """One line per new CI result of a build after his merge — the first poll
    only baselines. A re-run is a new result."""
    key = f"ci_merged:{repo}"
    seen = json.loads(store.kv_get(key) or "{}")
    first = not seen and not store.kv_get(key)
    notes = []
    service = repo.split("/")[-1].removeprefix("telikos-").removesuffix("-service")
    for r in sorted(merges, key=lambda r: int(r["databaseId"])):
        sid = _seen_id(r)
        if sid in seen:
            continue
        concl, failed = await _jobs_verdict(repo, str(r["databaseId"]))
        if not concl:
            continue                                     # CI still running
        seen[sid] = concl
        if first:
            continue
        number = await _his_merge(repo, r, me)
        again = " (re-run)" if int(r.get("attempt") or 1) > 1 else ""
        if concl == "success":
            notes.append(f"✅ CI passed{again} on {r['headBranch']} after your {service} PR "
                         f"{number} — the build is ready to share for deploy.\n{r['url']}")
        else:
            notes.append(f"🔴 CI failed{again} on {r['headBranch']} after your {service} PR "
                         f"{number} — {', '.join(failed)} failed.\n{r['url']}")
    store.kv_set(key, json.dumps(dict(list(seen.items())[-60:])))
    return notes


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
        "databaseId,status,conclusion,workflowName,headBranch,displayTitle,url,event,attempt")
    if rc != 0:
        return []  # transient API/network error — next poll catches up
    runs = json.loads(out or "[]")
    # Fail CLOSED. When his login or his own runs could not be read, this used
    # to keep EVERY run — 15 Oct: a bot's nightly "Auto merge PR" failures in
    # empv3-tenant-intake reached his phone five times. Nothing is marked seen
    # here, so the next poll that can read them catches up.
    me = repo_ops.office_login(repo) or await my_login()
    if not me:
        return []
    rc2, out2 = await _run_gh(
        "run", "list", "-R", repo, "--limit", "10", "--user", me, "--json", "databaseId")
    if rc2 != 0:
        return []
    mine = {str(r["databaseId"]) for r in json.loads(out2 or "[]")}
    branches = await _my_branches(repo)
    # The build on develop/main/release after HIS pull request merged: told once
    # per result, pass or fail — "post merge, my PR only; once CI passes tell me
    # and we share the build for deploy" (16 Oct). Other people's merges, and the
    # deploy stages after CI, are not his.
    merges = []
    for r in runs:
        if _is_branch_build(r) and await _his_merge(repo, r, me):
            merges.append(r)
    merged_notes = await _merged_results(repo, merges, me)
    runs = [r for r in runs if r not in merges and (
            str(r["databaseId"]) in mine
            or r.get("headBranch") in branches
            or _subscribed(repo, r))]
    seen_key, wb_key = f"ci_seen:{repo}", f"ci_wb:{repo}"
    seen = json.loads(store.kv_get(seen_key) or "{}")
    wb = json.loads(store.kv_get(wb_key) or "{}")
    first_poll = not seen
    # In the order they happened. `gh` lists newest first, and reading it that
    # way left the stored state on the OLDER run — a pipeline that went red and
    # then green was remembered as red.
    runs = sorted(runs, key=lambda r: int(str(r.get("databaseId") or 0) or 0))
    # One net change per workflow+branch per poll. A poll that sees a failed run
    # AND the re-run that fixed it reported both, in the same second: 4 of the 19
    # CI pushes to 22 Sep were that.
    moved: dict[str, dict] = {}
    for r in runs:
        rid, concl = _seen_id(r), (r.get("conclusion") or "")
        if r.get("status") != "completed" or seen.get(rid) == "completed":
            continue
        seen[rid] = "completed"
        wbk = f"{r['workflowName']}|{r['headBranch']}"
        prev, streak = _prev_state(wb.get(wbk))
        m = moved.setdefault(wbk, {"before": prev, "failed": False,
                                   "fails": streak if prev in BAD else 0})
        repeat = concl in BAD and prev in BAD
        wb[wbk] = [concl, streak + 1 if repeat else 1]
        m.update(last=r, concl=concl)
        if concl in BAD:
            m["failed"] = True
            m["fails"] = wb[wbk][1]
    notes: list[str] = []
    name = repo.split("/")[-1]
    for m in ([] if first_poll else moved.values()):   # the first poll baselines silently
        r, concl, before = m["last"], m["concl"], m["before"]
        if concl in BAD:
            if before in BAD:
                continue  # he already knows this workflow+branch is red; one ping is enough
            jobs = f" — {', '.join(r['_failed_jobs'])} failed" if r.get("_failed_jobs") else ""
            notes.append(f"🔴 CI {concl}: {name} · {r['workflowName']} "
                         f"({r['headBranch']}){jobs} — {r['displayTitle'][:60]}\n{r['url']}")
        elif concl == "success" and before in BAD:
            after = f" after {m['fails']} failures" if m["fails"] > 1 else ""
            notes.append(f"🟢 CI recovered{after}: {name} · {r['workflowName']} "
                         f"({r['headBranch']})")
        elif concl == "success" and m["failed"]:
            notes.append(f"🟡 CI flaked and recovered: {name} · {r['workflowName']} "
                         f"({r['headBranch']})")
    store.kv_set(seen_key, json.dumps(dict(list(seen.items())[-40:])))
    store.kv_set(wb_key, json.dumps(wb))
    return merged_notes + notes


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


async def announce(note: str) -> None:
    """Tell him about one change — a failure on his phone, good news in the digest."""
    from . import digest, notify, offers
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
        # Recovery is good news, and good news does not interrupt: an ambient
        # push still reached his phone once he stepped away — 7 of the 19 CI
        # pushes to 22 Sep. The digest is where it belongs.
        store.add_notification(note, "ci")
        digest.add(note, source="ci", why="good news does not interrupt")


async def loop() -> None:
    while True:
        try:
            if not await gh_ok():
                await asyncio.sleep(RETRY_UNAUTH_SECONDS)
                continue
            for note in await check_all():
                await announce(note)
        except Exception:
            pass
        await asyncio.sleep(POLL_SECONDS)
