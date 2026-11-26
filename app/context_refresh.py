"""Night refresh of a workspace's CLAUDE.md layer: rebuild, check, fix what's wrong, push.

It replaces the "Want me to bring the context up to date?" ask (refresh._report_stale) for any
workspace with the layer (app/context_layer.py). The ask was never answered, so booking-workspace
went 286 commits stale.

Order: cheap and certain first, the model only when something's wrong.
1. `git fetch` each service repo's develop. Off VPN this fails (Maersk's IP allow list); the run goes on
   from the last fetched develop and says so.
2. `ctx.js index`: deterministic, about 2 s. The code index and entry points follow develop.
3. `ctx.js verify` + `check-drift.js` + the inboxes (legs' doc/code mismatches, lessons.md).
4. Only if 3 found something: one gardener leg (Sonnet) that may edit the workspace's docs and
   CLAUDE.md files, never a service repo. The fence is checked on the tree afterwards.
5. `ctx.js verify` must pass, the diff must scan clean (secrets, real ids), and the eval must not drop.
   Any failure: nothing is pushed and the brief says why.
6. Commit the changed context paths and push with the workspace's own git identity (booking-workspace:
   Arunk540, the default gh). One line goes to the morning brief.

Runs only inside ASTA_REFRESH_WINDOW (default 22:00-06:00), on power, with no task running, no call,
and Claude usage not limited. A refresh asked for outside those conditions waits for the window.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from . import context_layer, store

#: Paths the refresh may change and commit, relative to the workspace root.
ALLOWED = ("CLAUDE.md", ".claude/rules/", "services/", "docs/", ".asta-context/repos/",
           ".asta-context/aliases.json", ".asta-context/_index.json")
BRIEF_KEY = "context_refresh_line"
_SECRET = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token|private[_-]?key)\s*[:=]\s*\S{6,}"
                     r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{30,}")
#: An 11-char booking number or a 12-char service plan number (memory: telikos-identifiers), or
#: a home path. Synthetic examples in docs use placeholders, never one of these shapes.
_LIVE_ID = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{11,12}\b|/Users/[\w.-]+/")


def window() -> tuple[int, int]:
    raw = os.environ.get("ASTA_REFRESH_WINDOW", "22:00-06:00")
    try:
        a, b = raw.split("-")
        return int(a.split(":")[0]), int(b.split(":")[0])
    except ValueError:
        return 22, 6


def in_window(now: float | None = None) -> bool:
    start, end = window()
    h = time.localtime(now or time.time()).tm_hour
    return (start <= h or h < end) if start > end else (start <= h < end)


async def blockers() -> list[str]:
    """Why a refresh may not run right now ([] = it may)."""
    why = []
    if not in_window():
        why.append("outside the night window")
    if any(t.get("status") == "running" for t in store.list_tasks(50)):
        why.append("a task is running")
    try:
        from . import teams_bridge
        if teams_bridge.in_a_call():
            why.append("a call is live")
    except Exception:                                          # noqa: BLE001
        pass
    try:
        from . import away
        _pct, on_power = await away.battery()
        if not on_power:
            why.append("on battery")
    except Exception:                                          # noqa: BLE001
        pass
    from . import claude_cli
    if claude_cli.limited_until():
        why.append("Claude usage is limited")
    return why


async def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


#: Stale docs one night's gardener takes on; the rest wait for the next night, oldest first.
DRIFT_PER_NIGHT = int(os.environ.get("ASTA_CONTEXT_DRIFT_PER_NIGHT", "6"))


def _drift_items(report: str) -> list[str]:
    """check-drift.js lines → at most DRIFT_PER_NIGHT stale docs, rotating night to night."""
    docs = []
    repo = ""
    for line in report.splitlines():
        m = re.match(r"DRIFT (\S+): ", line)
        if m:
            repo = m.group(1)
        m = re.search(r"stale mini-skills: (.+)$", line)
        if m and repo:
            docs += [f".asta-context/repos/{repo}/{d.strip()}" for d in m.group(1).split(",") if d.strip()]
    if not docs:
        return []
    start = int(store.kv_get("context_drift_cursor") or 0) % len(docs)
    pick = (docs[start:] + docs[:start])[:DRIFT_PER_NIGHT]
    store.kv_set("context_drift_cursor", str(start + len(pick)))
    return [f"stale doc (code it covers changed): {d}" for d in pick]


def _inbox(root: Path) -> list[str]:
    """Doc/code mismatches legs reported and lessons legs appended since the last refresh."""
    items = []
    log = root / ".asta-context" / ".mismatch-log.jsonl"
    if log.is_file():
        for line in log.read_text().splitlines()[-40:]:
            try:
                m = json.loads(line)
                items.append(f"mismatch (task {m.get('task')}): {m.get('doc')} says {m.get('says')}; "
                             f"code {m.get('code')}")
            except ValueError:
                continue
    for lessons in sorted((root / ".asta-context").rglob("lessons.md")):
        text = lessons.read_text(errors="replace").strip()
        if text.count("\n") > 2:
            items.append(f"lessons inbox: {lessons.relative_to(root)} ({text.count(chr(10)) + 1} lines)")
    return items


def scan(diff: str) -> list[str]:
    """Secrets and live-looking ids in the ADDED lines of a diff."""
    bad = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if _SECRET.search(line):
            bad.append("secret-like: " + line[:100])
        elif _LIVE_ID.search(line):
            bad.append("id/path-like: " + line[:100])
    return bad


def gardener_prompt(root: Path, problems: list[str]) -> str:
    listing = "\n".join(f"- {p}" for p in problems[:40])
    return f"""You maintain the context files of the workspace at {root}. Edit ONLY these paths there:
CLAUDE.md, .claude/rules/, services/*/CLAUDE.md, docs/, .asta-context/repos/**, .asta-context/aliases.json.
Never edit, build in, or commit to a service repo (the telikos-* folders); read them only.

Problems found tonight:
{listing}

How:
- The code on origin/develop is the truth. Check every fact you keep or write against it (git show origin/develop:<path>).
- A wrong fact gets fixed. A fact you can't check becomes `[gap] <what is unknown>`.
- Lessons inbox: move each lesson into the strongest place that fits, then delete it from lessons.md:
  a "Things that bite" line in services/<svc>/CLAUDE.md if it changes how code is written or built;
  otherwise a sentence in the matching .asta-context/repos/<repo>/ doc. Keep the date and the task/PR it came from.
- Budgets (ctx verify enforces them): root CLAUDE.md 2.6k chars, rules 1.8k total, each service file 3.2k.
  A new line has to replace a weaker one.
- No real booking numbers, customer names, people or secret values. Use placeholders.
- When done, run `node {root}/.asta-context/ctx.js verify` until it prints "context ok". Then reply with one line per change.
Do not commit or push. Asta does that after its own checks."""


async def refresh(workspace: str, root: Path, *, force: bool = False) -> str:
    """One night refresh. Returns the brief line (also stored for the morning brief)."""
    if not force:
        why = await blockers()
        if why:
            store.kv_set(f"context_refresh_pending:{workspace}", "1")
            return f"deferred: {', '.join(why)}"
    store.kv_set(f"context_refresh_pending:{workspace}", "")
    svcs = context_layer.services(root)
    notes = []
    fetched = 0
    for repo in svcs.values():
        rc, _ = await _run(["git", "fetch", "-q", "origin", "develop"], root / repo, timeout=120)
        fetched += rc == 0
    if fetched < len(svcs):
        notes.append(f"fetch failed for {len(svcs) - fetched}/{len(svcs)} repos (VPN?); used last fetched develop")
    ctx = root / ".asta-context" / "ctx.js"
    rc, idx = await _run(["node", str(ctx), "index"], root)
    if rc != 0:
        return _brief(workspace, f"❌ context refresh: index failed: {idx[-160:]}")
    learned = await _learn(root, workspace)
    rc, ver = await _run(["node", str(ctx), "verify"], root)
    problems = [l[5:] for l in ver.splitlines() if l.startswith("FAIL ")]
    rc_d, drift = await _run(["node", str(root / ".asta-context" / "check-drift.js"), str(root)], root)
    if rc_d not in (0,) and drift.strip():
        problems += _drift_items(drift)
    problems += _inbox(root)
    changed = ""
    if problems:
        before = {repo: await _status(root / repo) for repo in svcs.values()}
        out = await _gardener(root, problems)
        fence = await _fence(root, svcs, before)
        if fence:
            return _brief(workspace, f"❌ context refresh: gardener touched {fence}; reverted, nothing pushed")
        rc, ver = await _run(["node", str(ctx), "verify"], root)
        if rc != 0:
            await _restore(root)
            left = [l[5:] for l in ver.splitlines() if l.startswith("FAIL ")]
            return _brief(workspace, f"⚠️ context refresh: {len(left)} problem(s) left, nothing pushed: {left[0][:120] if left else ''}")
        changed = out
    pushed = await _commit_and_push(root, workspace)
    if pushed.startswith("❌") or pushed.startswith("⚠️"):
        return _brief(workspace, pushed)
    shas = idx.strip().split(": ", 1)[-1]
    line = (f"🗂 {workspace} context: {pushed}; {len(problems)} issue(s) found"
            + (f", fixed ({_count_lines(changed)} change(s))" if problems else "")
            + f" · develop {shas}" + (f" · {learned}" if learned else "")
            + (f" · {'; '.join(notes)}" if notes else ""))
    if problems:
        _clear_inbox(root)
    return _brief(workspace, line)


def learn_on() -> bool:
    return os.environ.get("ASTA_CONTEXT_LEARN", "1").strip().lower() not in ("0", "false", "no", "off")


async def _learn(root: Path, workspace: str) -> str:
    """Grow aliases from the last 120 days of merged PRs: PR title → the files it changed. An alias
    is kept only if it lifts hit@5 on one half of the cases and doesn't lower the other (eval.js).
    The gold set is work data, so it lives in Asta's data/, never in the workspace repo."""
    ev = root / ".asta-context" / "eval.js"
    if not learn_on() or not ev.is_file():
        return ""
    from . import store as _s
    gold = Path(_s.DB_PATH).parent / "context-eval" / f"{workspace}-gold.jsonl"
    gold.parent.mkdir(parents=True, exist_ok=True)
    rc, _ = await _run(["node", str(ev), "build", str(gold), "--since", "120 days ago"], root, timeout=300)
    if rc != 0:
        return ""
    rc, out = await _run(["node", str(ev), "learn", str(gold), "--write"], root, timeout=1800)
    m = re.search(r"kept (\d+).*\n.*hold (\d+%) → (\d+%)", out)
    rc, res = await _run(["node", str(ev), "run", str(gold)], root, timeout=300)
    hit = re.search(r"hit@5 (\d+%)", res)
    return (f"find hit@5 {hit.group(1)}" if hit else "") + (f" (+{m.group(1)} aliases)" if m and m.group(1) != "0" else "")


def _count_lines(text: str) -> int:
    return sum(1 for l in (text or "").splitlines() if l.strip())


async def _gardener(root: Path, problems: list[str]) -> str:
    from . import claude_cli
    return await claude_cli.one_shot(gardener_prompt(root, problems), cwd=str(root), timeout=1800,
                                     model=os.environ.get("ASTA_CONTEXT_GARDENER_MODEL", "sonnet"),
                                     builtins=claude_cli.CODE_TOOLS, purpose="context_refresh")


async def _status(repo: Path) -> str:
    rc, st = await _run(["git", "status", "--porcelain"], repo)
    return st


async def _fence(root: Path, svcs: dict[str, str], before: dict[str, str]) -> str:
    """'' when the gardener stayed inside ALLOWED, else what it touched. Workspace paths outside
    ALLOWED are reverted; a service repo is never reverted (his own work may be there): it is named."""
    touched = []
    for repo in svcs.values():
        if await _status(root / repo) != before.get(repo, ""):
            touched.append(repo)
    rc, st = await _run(["git", "status", "--porcelain"], root)
    for l in st.splitlines():
        path = l[3:].strip()
        if path and not path.startswith(ALLOWED) and not path.startswith((".asta-context/.", "docs/generated/")):
            touched.append(path)
    if touched:
        await _restore(root)
    return ", ".join(touched[:5])


def _existing(root: Path) -> list[str]:
    return [p for p in ALLOWED if (root / p).exists()]


async def _restore(root: Path) -> None:
    await _run(["git", "checkout", "--", *_existing(root)], root)


async def _commit_and_push(root: Path, workspace: str) -> str:
    paths = _existing(root)
    rc, st = await _run(["git", "status", "--porcelain", "--", *paths], root)
    if not st.strip():
        return "nothing to commit"
    await _run(["git", "add", "--", *paths], root)
    rc, diff = await _run(["git", "diff", "--cached"], root)
    bad = scan(diff)
    if bad:
        await _run(["git", "reset", "-q"], root)
        return f"⚠️ context refresh: scan flagged {len(bad)} line(s), nothing pushed: {bad[0]}"
    rc, out = await _run(["git", "commit", "-q", "-m", "Refresh workspace context against develop"], root)
    if rc != 0:
        return f"❌ context refresh: commit failed: {out[-160:]}"
    rc, out = await _run(["git", "pull", "-q", "--rebase", "origin", "main"], root, timeout=120)
    if rc != 0:
        await _run(["git", "rebase", "--abort"], root)
        return "⚠️ context refresh: committed locally; pull failed, not pushed"
    rc, out = await _run(["git", "push", "-q", "origin", "HEAD:main"], root, timeout=120)
    if rc != 0:
        return f"⚠️ context refresh: committed locally; push failed: {out[-120:]}"
    rc, sha = await _run(["git", "rev-parse", "--short", "HEAD"], root)
    return f"pushed {sha.strip()}"


def _clear_inbox(root: Path) -> None:
    log = root / ".asta-context" / ".mismatch-log.jsonl"
    if log.is_file():
        log.rename(log.with_suffix(f".{int(time.time())}.done"))


def _brief(workspace: str, line: str) -> str:
    store.kv_set(BRIEF_KEY, json.dumps({"at": time.time(), "line": line, "workspace": workspace}))
    return line


def brief_line(since: float) -> str:
    """The last refresh's line for the morning brief, if it ran since `since`."""
    try:
        rec = json.loads(store.kv_get(BRIEF_KEY) or "{}")
    except ValueError:
        return ""
    return rec.get("line", "") if rec.get("at", 0) >= since else ""
