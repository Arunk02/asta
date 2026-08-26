"""Repository primitives shared by every pipeline: git, branch naming, playbooks.

These used to live in `missions.py`, which was one of two engines that both ran
plan → approve → implement → verify → ship. `tasks.py` is now the only engine,
and it imported these helpers back out of the module it replaced — an import
that quietly kept the dead engine alive. They belong to neither engine, so they
live here.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

# Arun's rule: commits and PRs must look like HIS work. No Claude/Copilot
# co-author trailers, no "Generated with" footers — which tool he used is his
# business, and it would show up in the PR for the whole team to see.
NO_ATTRIBUTION = (
    "\n\nCOMMIT RULES (strict):\n"
    "- Use plain `git commit -m \"<message>\"`. NOTHING else in the message.\n"
    "- NEVER add a Co-Authored-By trailer, an AI/assistant name, an emoji robot, "
    "or any 'Generated with …' line. The commit must read as Arun's own work.\n"
    "- Do not pass --author or amend authorship; the repo's configured identity is correct.\n"
)

#: Branches a pipeline must never commit straight onto.
BASE_BRANCHES = ("main", "master", "develop")

#: Where a new feature branch is cut from, in order of preference.
#:
#: Arun's rule is "always shift to develop and start work there". The fallback
#: exists because not every repo has a develop; when it is used, the caller says
#: so out loud rather than branching off something unexpected in silence.
BASE_PREFERENCE = ("develop", "main", "master")


async def start_branch(repo: Path, branch: str) -> dict:
    """Cut `branch` from a freshly-pulled base. Returns what actually happened.

    Nothing did this before: a task began on whatever branch the repo happened
    to be left on, which after a previous task is the PREVIOUS task's feature
    branch. The change then carries someone else's unmerged commits, and the PR
    shows both.

    Never raises. A repo that cannot be prepared is reported, not thrown, so one
    awkward repo in a multi-repo workspace does not kill the whole run.
    """
    out: dict = {"repo": repo.name, "branch": branch, "base": "", "ok": False,
                 "note": "", "dirty": False}

    rc, dirty = await git(repo, "git", "status", "--porcelain")
    out["dirty"] = bool(rc == 0 and dirty.strip())

    rc, _ = await git(repo, "git", "fetch", "origin", timeout=180)
    if rc != 0:
        out["note"] = "could not fetch origin — working from the local copy"

    # First base that actually exists here. Checking remote-tracking refs rather
    # than local ones: a fresh clone may never have checked develop out.
    for candidate in BASE_PREFERENCE:
        rc, _ = await git(repo, "git", "rev-parse", "--verify", f"origin/{candidate}")
        if rc == 0:
            out["base"] = candidate
            break
    if not out["base"]:
        out["note"] = f"no {'/'.join(BASE_PREFERENCE)} branch found"
        return out

    rc, msg = await git(repo, "git", "checkout", out["base"])
    if rc != 0:
        out["note"] = f"could not check out {out['base']}: {msg.strip()[:160]}"
        return out
    # --ff-only: a merge commit invented here would be a surprise in his history.
    await git(repo, "git", "pull", "--ff-only", "origin", out["base"], timeout=180)

    rc, msg = await git(repo, "git", "checkout", "-b", branch)
    if rc != 0:
        # Already exists — reuse it rather than failing. Re-running a task, or a
        # second repo hop, both land here legitimately.
        rc, msg = await git(repo, "git", "checkout", branch)
        if rc != 0:
            out["note"] = f"could not create or switch to {branch}: {msg.strip()[:160]}"
            return out
        out["note"] = "branch already existed — continued on it"

    out["ok"] = True
    if out["base"] != BASE_PREFERENCE[0] and not out["note"]:
        # Said out loud, because branching off main in a repo that normally uses
        # develop is the kind of thing he would want to know before the PR.
        out["note"] = f"no develop in this repo — branched from {out['base']}"
    return out


def default_executor() -> str:
    """The CLI that runs headless work unless a task pins one."""
    return os.environ.get("ASTA_EXECUTOR", "copilot")


async def git(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    """Run a git/gh command, returning (returncode, combined output).

    Never raises on a non-zero exit — callers decide what a failure means, and
    several of them treat one (an existing PR, a clean tree) as success.
    """
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"timed out: {' '.join(args)}"
    return proc.returncode or 0, raw.decode(errors="replace")


def branch_name(jira_key: str = "", title: str = "", task_id: int | str = "") -> str:
    if jira_key:
        return f"feature/{jira_key}"
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "change").lower()).strip("-")[:40]
    return f"feature/asta-{task_id}-{slug}" if task_id != "" else f"feature/asta-{slug}"


def playbook_block(repo_dir: Path) -> str:
    """Point the executor at the repo's own agent playbooks/skills, if present.

    Some workspaces ship .github/agents (implement, unit-test, component-test,
    review) and .github/skills (build profiles, language conventions, domain
    patterns). Headless executors must code and test the way those playbooks
    specify, not their own defaults.
    """
    for base in (repo_dir / ".github", repo_dir.parent / ".github"):
        if (base / "agents").is_dir() or (base / "skills").is_dir():
            return (
                f"\n\nIMPORTANT — this codebase ships its own engineering playbooks under {base}:\n"
                "- agents/implement.agent.md — how implementation is done here "
                "(boot skills, build command from pins, prohibited actions)\n"
                "- agents/unit-test.agent.md and component-test.agent.md — "
                "how tests must be written\n"
                "- skills/ — build profiles (maven/gradle), language conventions "
                "(spring-java/kotlin/react), domain patterns (kafka/temporal)\n"
                "BEFORE coding: read the implement agent + the convention and build skills "
                "relevant to this stack, and follow them exactly. Write unit/component tests "
                "per the test agents' conventions."
            )
    return ""
