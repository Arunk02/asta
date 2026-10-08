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
import subprocess
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


def base_ref(repo: Path | str) -> str:
    """The remote branch this repo's work is measured against: origin/develop,
    else origin/main, else origin/master — "" when none exists.

    One answer for every "what has this branch added" question. 8 Oct, #268:
    the library's base is main, the count was taken against origin/develop
    alone, git failed quietly, and a real commit read as "no changes" — the
    finished task was marked failed."""
    for candidate in BASE_PREFERENCE:
        try:
            rc = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                                 f"origin/{candidate}"], cwd=str(repo),
                                capture_output=True, timeout=10).returncode
        except (OSError, subprocess.SubprocessError):
            return ""
        if rc == 0:
            return f"origin/{candidate}"
    return ""


def office_login(repo: str) -> str:
    """The saved gh account for a configured work owner, never a token on disk."""
    user = os.environ.get("ASTA_GITHUB_WORK_USER", "").strip()
    owners = {s.strip().lower() for s in
              os.environ.get("ASTA_GITHUB_WORK_OWNERS", "").split(",") if s.strip()}
    return user if user and repo.split("/", 1)[0].lower() in owners else ""


def _github_owner(cwd: Path, args: tuple[str, ...]) -> str:
    for i, arg in enumerate(args):
        if arg in ("-R", "--repo") and i + 1 < len(args):
            return args[i + 1].split("/", 1)[0]
        match = re.search(r"(?:github\.com[/:]|(?:^|/)repos/)([^/\s]+)/[^/\s]+",
                          arg, re.I)
        if match:
            return match.group(1)
    try:
        remote = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(r"(?:github\.com[/:])([^/\s]+)/[^/\s]+",
                      remote.stdout.strip(), re.I) if remote.returncode == 0 else None
    return match.group(1) if match else ""


async def github_env(cwd: Path, *args: str) -> dict[str, str] | None:
    """Select the saved office login by target repo; keep personal repos personal."""
    if not (os.environ.get("ASTA_GITHUB_WORK_USER")
            and os.environ.get("ASTA_GITHUB_WORK_OWNERS")):
        return None
    owner = _github_owner(cwd, args)
    if not owner:
        return None
    clean = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    user = office_login(owner)
    if not user:
        return clean
    proc = await asyncio.create_subprocess_exec(
        "gh", "auth", "token", "--user", user, env=clean,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"GitHub login for {user} timed out") from None
    token = raw.decode().strip()
    if proc.returncode != 0 or not token:
        raise RuntimeError(f"GitHub login for {user} unavailable — run gh auth login")
    return {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}


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


async def git(cwd: Path, *args: str, timeout: float = 120,
              stdin: str = "") -> tuple[int, str]:
    """Run a git/gh command, returning (returncode, combined output).

    Never raises on a non-zero exit — callers decide what a failure means, and
    several of them treat one (an existing PR, a clean tree) as success.

    `stdin` is for the commands that take a body rather than a flag —
    `gh api --input -`, whose payload is a JSON document with newlines in it and
    has no business being an argv string.
    """
    remote_git = args[:2] in (("git", "fetch"), ("git", "pull"), ("git", "push"),
                              ("git", "clone"), ("git", "ls-remote"))
    env = await github_env(cwd, *args) if args and (args[0] == "gh" or remote_git) else None
    if env and env.get("GH_TOKEN") and remote_git:
        # The macOS keychain defaults to the personal account. Use the scoped
        # office token for Git too, without changing global gh or git settings.
        args = ("git", "-c", "credential.helper=", "-c",
                "credential.helper=!gh auth git-credential", *args[1:])
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.PIPE if stdin else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        raw, _ = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin else None), timeout=timeout)
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
