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
import contextlib
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


def context_gap(workspace: str) -> list[dict]:
    """Per repo: how far the context map has fallen behind the code it describes.

    `[{repo, verified, behind}]`, newest gap first. `behind` is -1 when it cannot
    be counted — no recorded `verified_against`, or git could not answer.

    This is the number that explains "it already has whole repo knowledge and it
    still lacks". The map is not a little old: on 2026-09-07 the booking-service
    mini-skills were stamped against `6ee7d83b` and develop had moved **105
    commits**. Meanwhile both pipelines tell the agent to route every search
    through that map and never scan a repo blind — so it answers confidently from
    a description of code that no longer exists, and "zero references anywhere in
    this repo" is a claim about the map, not about the repo.

    Deliberately counted in COMMITS, not days. Days measure how long nobody ran a
    script; commits measure how much the agent does not know.
    """
    from . import worktrees as _wt
    ws = ws_mod.get(workspace)
    if ws is None or not ws.exists():
        return []
    # The workspace's own context directory, resolved the way everything else
    # resolves it. Reading a fresh env var here would have invented a second
    # answer to a question the provider already answers — and his booking
    # workspace uses `.contmark`, not the default, so a hardcoded guess is wrong
    # on the only workspace that exists.
    from .workspace.providers.indexed import context_dirname
    ctx = ws.path / context_dirname(ws.path)
    out: list[dict] = []
    for repo in _wt.repos_in(ws.path):
        idx = ctx / "repos" / repo.name / "_index.json"
        verified = ""
        try:
            verified = (json.loads(idx.read_text()).get("verified_against") or "").strip()
        except (OSError, ValueError):
            pass
        behind = -1
        if verified:
            counted = _git(repo, "rev-list", "--count", f"{verified}..origin/develop")
            if counted.strip().isdigit():
                behind = int(counted.strip())
        out.append({"repo": repo.name, "verified": verified[:12], "behind": behind})
    return sorted(out, key=lambda r: r["behind"], reverse=True)


# --- coverage: entry points the map does not know -------------------------------------
#
# Staleness counts commits; it cannot see a class no mini-skill ever declared.
# 31 Oct, booking: 21 of 29 Temporal activity classes were in no mini-skill's
# `entities`, so the symbol index (generated from those) could not route an
# exact-name query to them — the amend activity a real task needed fell back to
# general booking context. Re-indexing cannot fix that; enrichment has to add them.

#: Where a debugging or coding task usually starts, by path or by annotation.
_ENTRY_PATHS = (("temporal activity", re.compile(r"/temporal/activit(?:y|ies)/[^/]+Impl\.(?:java|kt)$")),
                ("temporal workflow", re.compile(r"/temporal/workflows?/[^/]+Impl\.(?:java|kt)$")))
_ENTRY_ANNOTATIONS = (("kafka listener", "@KafkaListener"), ("rest controller", "@RestController"))
#: A repo with at least this many unmapped entry points is called out.
COVERAGE_MIN_MISSING = 3
_coverage_cache: dict[tuple, dict] = {}


def _declared(ctx: Path) -> set[str]:
    names: set[str] = set()
    try:
        gi = json.loads((ctx / "_global_index.json").read_text())
        for f in (gi if isinstance(gi, list) else gi.get("files") or []):
            names.update(e for e in (f.get("entities") or []) if isinstance(e, str))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        names.update((json.loads((ctx / "_symbols.json").read_text()).get("symbols") or {}).keys())
    except (OSError, ValueError, AttributeError):
        pass
    return names


_TEMPORAL_MARKS = (("temporal activity", "@ActivityInterface"),
                   ("temporal workflow", "@WorkflowInterface"))


def _main_hits(repo: Path, *args: str) -> list[str]:
    return [p for p in _git(repo, *args).splitlines() if p and "/src/test/" not in p]


def _temporal_impls(repo: Path) -> dict[str, tuple[str, str]]:
    """Classes implementing a Temporal-annotated interface: name -> (kind, interface).

    By annotation, not folder: the activityplan service keeps its activities in
    `…/activity/` with no `temporal/` segment, and a path rule found 1 of ~37."""
    out: dict[str, tuple[str, str]] = {}
    for kind, mark in _TEMPORAL_MARKS:
        ifaces = {Path(p).stem for p in _main_hits(repo, "grep", "-l", "-F", mark, "--", "*.java", "*.kt")}
        if not ifaces:
            continue
        # git's -E is POSIX and has no \b: let git find candidates, Python decide.
        rx = re.compile(r"(?:implements|:)[^{]*\b(" + "|".join(sorted(map(re.escape, ifaces)))
                        + r")\b")
        for path in _main_hits(repo, "grep", "-l", "-E", "implements|class .*:", "--", "*.java", "*.kt"):
            name = Path(path).stem
            if name in ifaces:
                continue
            text = ""
            with contextlib.suppress(OSError):
                text = (repo / path).read_text(errors="replace")
            hit = rx.search(text)
            if hit:
                out[name] = (kind, hit.group(1))
    return out


def _entry_points(repo: Path) -> dict[str, str]:
    """Main-source entry-point classes in a repo: name -> kind. Tracked files only."""
    found: dict[str, str] = {}
    for name, (kind, _iface) in _temporal_impls(repo).items():
        found[name] = kind
    for path in _git(repo, "ls-files", "*.java", "*.kt").splitlines():
        if "/src/test/" in path:
            continue
        for kind, rx in _ENTRY_PATHS:
            if rx.search("/" + path):
                found[Path(path).stem] = kind
    for kind, mark in _ENTRY_ANNOTATIONS:
        for path in _git(repo, "grep", "-l", "-F", mark, "--", "*.java", "*.kt").splitlines():
            if "/src/test/" not in path:
                found.setdefault(Path(path).stem, kind)
    return found


def coverage_gap(workspace: str) -> list[dict]:
    """Per repo: entry-point classes in the source that no mini-skill declares.

    `[{repo, total, mapped, missing: [names], kinds: {name: kind}}]`, worst first.
    Cached per repo HEAD and index file, so a code task's trust note costs nothing
    when nothing changed."""
    from . import worktrees as _wt
    from .workspace.providers.indexed import context_dirname
    ws = ws_mod.get(workspace)
    if ws is None or not ws.exists():
        return []
    ctx = ws.path / context_dirname(ws.path)
    try:
        stamp = (ctx / "_global_index.json").stat().st_mtime
    except OSError:
        stamp = 0.0
    declared = None
    out = []
    for repo in _wt.repos_in(ws.path):
        key = (str(repo), _git(repo, "rev-parse", "HEAD").strip(), stamp)
        if key not in _coverage_cache:
            declared = _declared(ctx) if declared is None else declared
            entries = _entry_points(repo)
            ifaces = {n: i for n, (_k, i) in _temporal_impls(repo).items() if i}
            missing = sorted(n for n in entries
                             if n not in declared and ifaces.get(n, "\0") not in declared)
            _coverage_cache[key] = {"repo": repo.name, "total": len(entries),
                                    "mapped": len(entries) - len(missing), "missing": missing,
                                    "kinds": {n: entries[n] for n in missing}}
        if _coverage_cache[key]["total"]:
            out.append(dict(_coverage_cache[key]))
    return sorted(out, key=lambda r: len(r["missing"]), reverse=True)


def trust_note(workspace: str, min_behind: int = 0) -> str:
    """What a code task must be told before it trusts the context map, or "".

    Only the repos far enough behind to matter, so a healthy workspace adds
    nothing to the prompt. The instruction is the important half: the agent is
    told everywhere else to route through the map and never scan blind, so
    without an explicit exception it treats a silent map as an authoritative "no".
    """
    min_behind = min_behind or STALE_BEHIND
    bad = [r for r in context_gap(workspace)
           if r["behind"] < 0 or r["behind"] >= min_behind]
    try:
        thin = [c for c in coverage_gap(workspace) if len(c["missing"]) >= COVERAGE_MIN_MISSING]
    except Exception:                                          # noqa: BLE001
        thin = []
    if not bad and not thin:
        return ""
    lines = []
    for r in bad:
        how = "never verified" if r["behind"] < 0 else f"{r['behind']} commits behind"
        lines.append(f"  - {r['repo']}: context {how}")
    for c in thin:
        lines.append(f"  - {c['repo']}: {len(c['missing'])} of {c['total']} entry points are in "
                     f"no mini-skill (e.g. {', '.join(c['missing'][:4])})")
    return ("\n\n[CONTEXT TRUST — read this before believing the resolver]\n"
            + "\n".join(lines) +
            "\nFor these repos the map describes older code. Use it to find WHERE to "
            "look, never as proof of what exists. Before concluding a field, class or "
            "mapping is absent, grep the source and say which you checked. "
            "'Not in the index' is not 'not in the repo'.")


ENRICH_CONTRACT = Path(__file__).resolve().parent.parent / "skills" / "workspace-context" / "ENRICH.md"
ENRICH_TIMEOUT = int(os.environ.get("ASTA_ENRICH_TIMEOUT", "2700") or 2700)
CONTEXT_DIR = ".asta-context"

ENRICH_BRIEF = (
    "Arun approved refreshing the '{workspace}' project context. Bring the DRIFTED "
    "mini-skills under {context_dir}/ up to date with the code, following the enrichment "
    "contract below exactly. What drifted:\n{detail}\n\n"
    "Rules for this run:\n"
    "- Edit files under {context_dir}/ ONLY. Read the service repos to verify; never edit, "
    "commit, stash or check out anything in them.\n"
    "- For each drifted mini-skill: read the commits in its range that touch what it "
    "describes, patch at most 10 lines where the context is now wrong or missing, and "
    "stamp verified_against = the repo's HEAD sha even when nothing changed.\n"
    "- Update {context_dir}/repos/<repo>/_index.json for any new or moved fact, or it "
    "cannot be found.\n"
    "- Do not commit; Asta commits what you changed.\n"
    "- End with a short report: per repo, the files you patched and one line each on why, "
    "and the files reviewed with nothing to change.\n"
    "- This run is HEADLESS: nobody can answer a question. Never stop to ask. If a step "
    "cannot be done, do everything else and say in the report what was skipped and why.\n\n")

#: The index tools the contract names, in Asta's checkout — the run works inside the
#: workspace, where they do not exist (3 Nov: it stopped to ask where they were).
INDEX_TOOLS = Path(__file__).resolve().parent.parent / "skills" / "workspace-context" / "resources"


def index_tools(root: Path, ctx_dir: str) -> str:
    """The exact commands the contract means by "regenerate the indexes"."""
    env = f"ASTA_CONTEXT_DIR={ctx_dir}"
    steps = [f"{env} node {INDEX_TOOLS / name} {root} --write"
             for name in ("generate-indexes.js", "generate-symbols.js", "reconcile-router.js")]
    steps.append(f"{env} node {INDEX_TOOLS / 'validate-indexes.js'} {root}")
    return ("INDEX TOOLS — after editing, run these from the workspace root, in this order "
            "(they are Asta's, outside this workspace; never copy them in):\n"
            + "\n".join(f"  {s}" for s in steps)
            + f"\nThen verify each added class routes: node {root / ctx_dir / 'resolve-task.js'} "
              f"{root} \"<ClassName>\"\n\n")


def _contract() -> str:
    """The enrichment contract without its maintainer header (how to regenerate the
    file itself), which a worker read as an instruction to run a missing script."""
    text = ENRICH_CONTRACT.read_text()
    return re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S)


def coverage_brief(coverage) -> str:
    """The part of the refresh brief that asks for the unmapped classes, or ""."""
    rows = [c for c in coverage if c["missing"]]
    if not rows:
        return ""
    lines = []
    for c in rows:
        names = ", ".join(f"{n} ({c['kinds'].get(n, 'entry point')})" for n in c["missing"][:40])
        more = f" and {len(c['missing']) - 40} more" if len(c["missing"]) > 40 else ""
        lines.append(f"- {c['repo']} ({c['mapped']}/{c['total']} mapped): {names}{more}")
    return ("COVERAGE — these classes exist in the source but no mini-skill declares them, so "
            "an exact-name query cannot find them. Read each one, then add it to the "
            "`entities` of the mini-skill that owns its flow (or a new mini-skill if none "
            "does), with a source-backed line on what it does, and regenerate the indexes "
            "as the contract below says:\n" + "\n".join(lines) + "\n\n")


async def _porcelain(root: Path) -> set[str]:
    rc, out = await _run(["git", "status", "--porcelain", "--untracked-files=all"], root)
    return {ln[3:].strip() for ln in out.splitlines() if ln.strip()} if rc == 0 else set()


async def enrich(workspace: str, detail: str = "") -> str:
    """Do the refresh his yes to "context is stale" asked for — as a job that can write.

    26 Oct: his "go ahead" ran the offer as a chat turn, and chat turns may not edit
    files (ASTA_CHAT_MAY_EDIT=0), so the mini-skills could never change; and nothing
    called note_enriched, so even a good pass stayed "27 days stale". The brain edits
    only the context directory; Asta commits exactly what it changed there, locally,
    and stamps the workspace as enriched."""
    from . import claude_cli, task_fence
    from .workspace.providers.indexed import context_dirname
    ws = ws_mod.get(workspace)
    if ws is None or not ws.exists():
        return f"Unknown workspace '{workspace}'."
    root = Path(ws.path)
    # The directory THIS workspace uses. A hardcoded .asta-context treated the
    # booking workspace's .contmark edits as stray files: never committed, yet
    # the run was stamped as a successful refresh.
    ctx_dir = context_dirname(root)
    if not detail:
        with contextlib.suppress(ValueError, OSError):
            _stale, detail = await ws_mod.drift(workspace)
    before = await _porcelain(root)
    gap_before = {r["repo"]: r for r in context_gap(workspace)}
    cov_before = {c["repo"]: c for c in coverage_gap(workspace)}
    prompt = (ENRICH_BRIEF.format(workspace=workspace, context_dir=ctx_dir,
                                  detail=(detail or "(run the drift check)")[:3000])
              + coverage_brief(cov_before.values()) + index_tools(root, ctx_dir) + _contract())
    token = task_fence.MODE.set("none")      # nothing outward: no push, no PR, no posts
    try:
        report = await claude_cli.one_shot(prompt, cwd=str(root), timeout=ENRICH_TIMEOUT,
                                           effort="medium")
    finally:
        task_fence.MODE.reset(token)
    after = await _porcelain(root)
    touched = sorted(after - before)
    ours = [f for f in touched if f.startswith(ctx_dir + "/")]
    elsewhere = [f for f in touched if not f.startswith(ctx_dir + "/")]
    committed = ""
    if ours:
        rc, _ = await _run(["git", "add", "--", *ours], root)
        if rc == 0:
            msg = f"Refresh {workspace} workspace context against current code"
            rc, out = await _run(["git", "commit", "-q", "-m", msg, "--", *ours], root)
            committed = "committed" if rc == 0 else f"not committed: {out[-200:]}"
    # Verified, not assumed: a repo is refreshed when its verified_against moved.
    gap_after = {r["repo"]: r for r in context_gap(workspace)}
    repos = []
    for name in sorted(set(gap_before) | set(gap_after)):
        b, a = gap_before.get(name, {}), gap_after.get(name, {})
        repos.append({"repo": name, "verified_before": b.get("verified", ""),
                      "verified_after": a.get("verified", ""),
                      "behind_before": b.get("behind", -1), "behind_after": a.get("behind", -1)})
    _coverage_cache.clear()                          # the index files just changed
    cov_after = {c["repo"]: c for c in coverage_gap(workspace)}
    coverage = {name: {"before": f"{cov_before[name]['mapped']}/{cov_before[name]['total']}",
                       "after": (f"{cov_after[name]['mapped']}/{cov_after[name]['total']}"
                                 if name in cov_after else "?")}
                for name in cov_before}
    stale_before = [r for r in repos if r["behind_before"] < 0 or r["behind_before"] >= STALE_BEHIND]
    moved = [r for r in repos if r["verified_after"] and r["verified_after"] != r["verified_before"]]
    still = [r for r in stale_before if r not in moved]
    fresh = bool(ours) and (not stale_before or bool(moved))
    if fresh:
        note_enriched(workspace)
    store.kv_set(f"context_provenance:{workspace}", json.dumps({
        "at": time.time(), "fresh": fresh, "context_dir": ctx_dir, "files": ours,
        "committed": committed, "repos": repos, "coverage": coverage}))
    lines = [f"🗂 {workspace} context refreshed — {len(ours)} file(s) updated"
             + (f", {committed}" if committed else "")]
    for r in moved:
        lines.append(f"✓ {r['repo']}: verified {r['verified_before'] or 'never'} → "
                     f"{r['verified_after']} ({_behind(r['behind_before'])} → "
                     f"{_behind(r['behind_after'])})")
    if not fresh:
        lines[0] = f"⚠️ {workspace} context NOT brought up to date — {len(ours)} file(s) changed"
        lines.append("Still stale: " + (", ".join(
            f"{r['repo']} ({_behind(r['behind_after'])})" for r in still) or
            f"nothing under {ctx_dir}/ changed") + ". It stays marked stale.")
    elif still:
        lines.append("Not re-verified this run: " + ", ".join(
            f"{r['repo']} ({_behind(r['behind_after'])})" for r in still))
    for name, c in coverage.items():
        if c["before"] != c["after"]:
            lines.append(f"🧭 {name}: entry points mapped {c['before']} → {c['after']}")
    if elsewhere:
        lines.append(f"⚠️ Files outside {ctx_dir}/ changed during the run and were left "
                     f"as they are: {', '.join(elsewhere[:8])}")
    lines.append(report.strip()[-1500:])
    summary = "\n\n".join(lines)
    await notify.notify(summary, "info", urgency="direct")
    store.record_outcome("context", "enriched" if fresh else "enrich unverified", subject=workspace,
                         detail=f"{len(ours)} files; moved={len(moved)}; outside={len(elsewhere)}")
    return summary


#: A repo this many commits behind its context map counts as stale (trust_note too).
STALE_BEHIND = 25


def _behind(n: int) -> str:
    return "never verified" if n < 0 else f"{n} behind"


def enrich_in_background(workspace: str, detail: str = "") -> None:
    """Start `enrich` without holding the turn; a failure is said, never swallowed."""
    async def _run_it() -> None:
        try:
            await enrich(workspace, detail)
        except Exception as exc:                            # noqa: BLE001
            store.record_outcome("context", "enrich failed", subject=workspace,
                                 detail=str(exc)[:300])
            await notify.notify(f"❌ {workspace} context refresh failed: {str(exc)[:300]}",
                                "info", urgency="direct")
    asyncio.get_running_loop().create_task(_run_it())


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

    o = offers.propose(
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
        payload={"workspace": workspace, "enrich": True, "detail": detail[:3000]})
    await notify.notify(o.render(), "info",
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
