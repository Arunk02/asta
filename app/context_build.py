"""Build a workspace's project context — the expensive, one-time pass.

The deterministic generators (indexes, symbols, links) build their output FROM
per-repo mini-skills. Nothing creates those mini-skills: that needs a model to
read a codebase and write down what it owns. This module is that missing half.

Why it is a job and not a side effect of registering a workspace:

  It spends real money. Reading a service properly is 15-25 file reads per repo
  on a code-capable executor. Firing that silently when someone clicks "Add"
  would be a nasty surprise, so the caller asks for it explicitly and Asta
  reports the cost shape up front (`plan()`).

Ordering matters and is enforced here:

  1. bootstrap  per repo, in parallel — writes repos/<key>/OVERVIEW.md + _index.json
  2. provision  once, after ALL repos — the generators read every _index.json,
     so running them early produces a half-built index that looks valid

A repo that already has an index is skipped, so this is safe to re-run and is
also the refresh path: pass the repos that drifted.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from . import agents, notify, store
from . import workspace as ws_mod

#: One repo's read-and-summarise pass. Generous — a large service legitimately
#: takes a while — but bounded so a stuck run cannot burn a whole quota.
REPO_TIMEOUT = 1800

#: More than this in flight at once and the executors start contending; the
#: wall-clock win flattens while the failure blast radius grows.
MAX_PARALLEL = 3


def default_executor() -> str:
    """Which CLI runs the pass. Mirrors the task engine's choice but writes no
    per-task state — this is not a task, and `_resolve_executor(0)` would leave
    a kv row for a task id that does not exist."""
    from . import claude_cli, repo_ops, tasks

    ex = repo_ops.default_executor()
    if ex == "copilot" and tasks._copilot_quota_down() and claude_cli.available():
        ex = "claude"
    return ex


def context_dir(workspace: str) -> Path:
    """Where this workspace keeps its context. Asked of the provider, never
    assumed, so a workspace on another layout is written to correctly."""
    provider = ws_mod.provider_for(workspace)
    ctx = getattr(provider, "ctx", None)
    if ctx is not None:
        return Path(ctx)
    from .workspace.providers.indexed import context_dirname
    return ws_mod.get(workspace).path / context_dirname(ws_mod.get(workspace).path)


def repos_needing_context(workspace: str) -> list[str]:
    """Selected repos with no per-repo index yet."""
    ctx = context_dir(workspace)
    out = []
    for repo in ws_mod.list_services(workspace):
        if not (ctx / "repos" / repo / "_index.json").is_file():
            out.append(repo)
    return out


def plan(workspace: str) -> dict:
    """What a build would do, without doing it. Drives the UI's confirmation."""
    ws = ws_mod.get(workspace)
    if ws is None or not ws.exists():
        return {"ok": False, "error": f"Unknown workspace '{workspace}'"}
    selected = ws_mod.list_services(workspace)
    todo = repos_needing_context(workspace)
    return {
        "ok": True,
        "workspace": workspace,
        "context_dir": str(context_dir(workspace)),
        "repos_selected": selected,
        "repos_to_build": todo,
        "repos_already_done": [r for r in selected if r not in todo],
        "needs_build": bool(todo),
        # Deliberately a range, not a promise: it depends on repo size.
        "estimate": (f"{len(todo)} repo(s) — expect a few minutes each and real "
                     f"token spend on your code executor.") if todo else
                    "Nothing to build; every selected repo already has an index.",
    }


def _prompt(workspace: str, repo: str) -> str:
    """Bootstrap instructions for one repo. The pipeline body is prepended by
    the executor layer; this supplies only the facts for THIS run."""
    ctx = context_dir(workspace)
    target = ctx / "repos" / repo
    return (
        f"## This run\n"
        f"Build the project context for the repository `{repo}`.\n\n"
        f"- Repository root: `{ws_mod.get(workspace).path / repo}`\n"
        f"- Write your output to: `{target}/`\n"
        f"  · `{target}/OVERVIEW.md`\n"
        f"  · `{target}/_index.json`\n"
        f"- Create the directory if it does not exist.\n"
        f"- `verified_against` must be this repo's real HEAD SHA "
        f"(`git -C {ws_mod.get(workspace).path / repo} rev-parse HEAD`).\n"
        f"- Write NOTHING outside that directory. Do not modify source.\n"
    )


async def _build_one(workspace: str, repo: str, executor: str = "") -> tuple[str, bool, str]:
    """Run the bootstrap pipeline for one repo. Returns (repo, ok, detail)."""
    from . import claude_cli, copilot_cli, tasks

    prompt = _prompt(workspace, repo)
    cwd = str(ws_mod.get(workspace).path)
    ex = executor or default_executor()
    effort = tasks._effort_for("analysis", ex)
    try:
        if ex == "claude":
            out = await claude_cli.one_shot(
                prompt, cwd=cwd, timeout=REPO_TIMEOUT,
                agent_file=str(agents.path_for("bootstrap") or ""), effort=effort)
        else:
            out = await copilot_cli.one_shot(
                tasks._with_pipeline("bootstrap", prompt),
                cwd=cwd, timeout=REPO_TIMEOUT, effort=effort)
    except Exception as exc:
        return repo, False, f"{type(exc).__name__}: {exc}"[:300]

    # Trust the artefact, not the narration: an executor that says it succeeded
    # but wrote nothing has failed.
    index = context_dir(workspace) / "repos" / repo / "_index.json"
    if not index.is_file():
        return repo, False, "finished but wrote no _index.json"
    return repo, True, (out or "").strip()[-200:]


async def build(workspace: str, repos: list[str] | None = None,
                executor: str = "", notify_when_done: bool = True) -> str:
    """Build context for the repos that need it, then run the generators once.

    Returns a human-readable report. Safe to re-run: repos that already have an
    index are skipped unless named explicitly.
    """
    ws = ws_mod.get(workspace)
    if ws is None or not ws.exists():
        return f"Unknown workspace '{workspace}'."

    todo = repos or repos_needing_context(workspace)
    if not todo:
        return f"{workspace}: every selected repo already has context."

    started = time.time()
    lines = [f"🧠 Building project context for {workspace} ({len(todo)} repo(s))"]
    store.kv_set(f"context_build:{workspace}", str(started))

    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def guarded(repo: str):
        async with sem:
            return await _build_one(workspace, repo, executor)

    results = await asyncio.gather(*(guarded(r) for r in todo), return_exceptions=True)

    built, failed = [], []
    for r in results:
        if isinstance(r, BaseException):
            failed.append(("?", f"{type(r).__name__}: {r}"))
            continue
        repo, ok, detail = r
        (built if ok else failed).append((repo, detail))

    for repo, _ in built:
        lines.append(f"  ✓ {repo}")
    for repo, detail in failed:
        lines.append(f"  ✗ {repo}: {detail}")

    # Generators run ONCE, after every repo — they flatten all _index.json files,
    # so running per-repo would leave a partial index that still validates.
    if built:
        lines.append("")
        lines.append(await ws_mod.provision(workspace))

    lines.append("")
    lines.append(f"provider is now: {ws_mod.provider_for(workspace).id}")
    lines.append(f"took {round(time.time() - started)}s")

    store.kv_del(f"context_build:{workspace}")
    report = "\n".join(lines)
    if notify_when_done:
        await notify.notify(report[:1200], "task")
    return report


def in_progress(workspace: str) -> bool:
    return bool(store.kv_get(f"context_build:{workspace}"))
