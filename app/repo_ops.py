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
