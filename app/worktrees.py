"""One isolated checkout per task, so tasks can run at the same time.

The problem this solves, in Arun's own words: he wants a code task running while
he asks for a bug analysis and somebody else asks a question about the repo. Only
one of those was possible. Every code task took `_ws_lock(workspace)` and held it
for up to thirty minutes, because two tasks sharing one checkout fight over the
branch, the index and the working tree — so throughput was capped at roughly one
developer's, which forfeits the main advantage an assistant has over one.

`git worktree` is the standard answer and it is exact: a second working tree over
the SAME object database. Two tasks get two directories, two branches, two
indexes, and share every commit and blob. Nothing is duplicated except the files
actually checked out, and the main checkout — the one Arun has open in his editor
— is never touched at all.

Two consequences worth knowing:

  - **Rollback becomes deletion.** Undoing a task is removing its directory
    rather than resetting a branch he might be standing on. Strictly safer.
  - **His editor stops moving underneath him.** The incident that started all of
    this was a task checking out a different branch in a repo while a test run
    and an editor had it open. A worktree cannot do that.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import repo_ops

#: Where a task's checkouts live. Inside the workspace so everything to do with a
#: workspace stays together, but NOT inside any repo — a directory inside a repo
#: would show up in that repo's `git status` forever.
DIRNAME = ".asta-worktrees"

#: How many code tasks may hold checkouts at once. Each is a full checkout plus a
#: CLI subprocess plus, on verification, a Maven build — so this is bounded by the
#: machine rather than by taste. Three is enough for "one running, one analysing,
#: one answering" without the laptop becoming unusable while he works on it.
MAX_PARALLEL = int(os.environ.get("ASTA_MAX_PARALLEL_TASKS", "3"))


def root_for(workspace_root: Path, task_id: int) -> Path:
    return Path(workspace_root) / DIRNAME / f"task-{task_id}"


def exists(workspace_root: Path, task_id: int) -> bool:
    root = root_for(workspace_root, task_id)
    return root.is_dir() and any(root.iterdir())


def repos_in(workspace_root: Path) -> list[Path]:
    """The git repos of a workspace — itself, or the repos inside it."""
    root = Path(workspace_root)
    if (root / ".git").exists():
        return [root]
    try:
        return sorted(p for p in root.iterdir()
                      if (p / ".git").exists() and p.name != DIRNAME)
    except OSError:
        return []


async def _base_branch(repo: Path) -> str:
    """The branch to cut from: develop, then main, then master — as he insists."""
    for candidate in repo_ops.BASE_PREFERENCE:
        rc, _ = await repo_ops.git(repo, "git", "rev-parse", "--verify",
                                   f"origin/{candidate}")
        if rc == 0:
            return f"origin/{candidate}"
    for candidate in repo_ops.BASE_PREFERENCE:
        rc, _ = await repo_ops.git(repo, "git", "rev-parse", "--verify", candidate)
        if rc == 0:
            return candidate
    return ""


async def create(workspace_root: Path, task_id: int, branch: str) -> list[dict]:
    """A private checkout of every repo in the workspace, on `branch`.

    Reported, never raised: one repo that cannot be prepared must not kill a
    multi-repo run, and Arun is told which and why — the same contract
    `start_branch` already had.
    """
    root = root_for(workspace_root, task_id)
    root.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for repo in repos_in(workspace_root):
        out: dict = {"repo": repo.name, "branch": branch, "base": "", "ok": False,
                     "note": "", "path": str(root / repo.name)}
        # Fetch so the branch is cut from what origin has now, not from whatever
        # was last pulled. Failure is a note, not a stop: working from the local
        # copy beats not working.
        rc, _ = await repo_ops.git(repo, "git", "fetch", "origin", timeout=180)
        if rc != 0:
            out["note"] = "could not fetch origin — cut from the local copy"
        base = await _base_branch(repo)
        if not base:
            out["note"] = f"no {'/'.join(repo_ops.BASE_PREFERENCE)} branch found"
            results.append(out)
            continue
        out["base"] = base
        target = root / repo.name
        if target.exists():
            out["ok"] = True
            out["note"] = "reusing this task's existing checkout"
            results.append(out)
            continue
        rc, msg = await repo_ops.git(repo, "git", "worktree", "add", "-b", branch,
                                     str(target), base, timeout=300)
        if rc != 0:
            # The branch already exists — a re-run, or a second repo hop. Attach
            # the worktree to it rather than failing.
            rc, msg = await repo_ops.git(repo, "git", "worktree", "add",
                                         str(target), branch, timeout=300)
        if rc != 0:
            out["note"] = f"could not create a worktree: {msg.strip()[:160]}"
        else:
            out["ok"] = True
        results.append(out)
    return results


async def remove(workspace_root: Path, task_id: int, force: bool = False) -> list[str]:
    """Drop a task's checkouts. Returns what happened, per repo.

    Refuses a worktree with uncommitted changes unless forced: those edits are
    the only copy, and this is the one place that could delete them.
    """
    root = root_for(workspace_root, task_id)
    if not root.is_dir():
        return []
    notes: list[str] = []
    for repo in repos_in(workspace_root):
        target = root / repo.name
        if not target.exists():
            continue
        if not force:
            rc, dirty = await repo_ops.git(target, "git", "status", "--porcelain")
            if rc == 0 and dirty.strip():
                notes.append(f"{repo.name}: uncommitted changes — kept")
                continue
        args = ["git", "worktree", "remove"] + (["--force"] if force else []) + [str(target)]
        rc, msg = await repo_ops.git(repo, *args, timeout=120)
        if rc != 0:
            notes.append(f"{repo.name}: {msg.strip()[:100]}")
        else:
            notes.append(f"{repo.name}: removed")
        # Git keeps administrative files for a worktree it no longer has.
        await repo_ops.git(repo, "git", "worktree", "prune", timeout=60)
    if root.is_dir() and not any(root.iterdir()):
        shutil.rmtree(root, ignore_errors=True)
    return notes
