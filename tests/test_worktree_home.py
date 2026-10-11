"""11 Oct: "in intellij in bookingworkspace asta keep on adding worktree so that
alone will occupy more memory space and affect system".

29 task checkouts (1.3 GB) sat in `booking-workspace/.asta-worktrees`, inside the
folder IntelliJ has open, and only an undone or rejected task ever lost its
checkout. Now new checkouts live outside the workspace, finished tasks' are
swept, and a swept one comes back on its branch before anything works in it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from app import store, tasks, worktrees

DAY = 86400


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace with one real repo on develop, and a home for checkouts."""
    root = tmp_path / "booking-workspace"
    repo = root / "telikos-booking-service"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "start")
    home = tmp_path / "home-trees"
    monkeypatch.setenv("ASTA_WORKTREES_DIR", str(home))
    monkeypatch.setattr(tasks, "code_cwd", lambda w: str(root))

    async def _no_fetch(repo, *args, **kw):
        if args[:2] == ("git", "fetch"):
            return 1, "offline"
        return await _real(repo, *args, **kw)
    _real = worktrees.repo_ops.git
    monkeypatch.setattr(worktrees.repo_ops, "git", _no_fetch)
    store.init()
    return root, home


def _task(status: str, age_days: float, kind: str = "code") -> dict:
    t = store.create_task("customs statuses", kind, "update customs statuses", "booking")
    when = time.time() - age_days * DAY
    store.update_task(t["id"], status=status, finished_at=when)
    with store._connect() as c:
        c.execute("UPDATE tasks SET created_at=? WHERE id=?", (when, t["id"]))
    return store.get_task(t["id"])


def _checkout(root: Path, t: dict) -> Path:
    asyncio.run(worktrees.create(root, t["id"], f"feature/customs-{t['id']}",
                                 "telikos-booking-service"))
    return worktrees.root_for(root, t["id"])


# --- where checkouts live -----------------------------------------------------------

def test_a_new_checkout_lives_outside_the_workspace(ws):
    root, home = ws
    tree = _checkout(root, _task("running", 0))
    assert tree.is_relative_to(home / "booking-workspace")
    assert not (root / worktrees.DIRNAME).exists(), "nothing new inside the IntelliJ folder"
    assert (tree / "telikos-booking-service" / ".git").exists()


def test_a_task_already_inside_the_workspace_keeps_its_place(ws):
    root, _ = ws
    legacy = root / worktrees.DIRNAME / "task-272"
    (legacy / "telikos-booking-service").mkdir(parents=True)
    assert worktrees.root_for(root, 272) == legacy, "its CLI session is keyed by this path"


# --- which ones go, and when ----------------------------------------------------------

@pytest.mark.parametrize("status, age, due", [
    ("merged", 2, True), ("pr_closed", 2, True), ("cancelled", 2, True), ("rejected", 2, True),
    ("merged", 0.5, False),                       # a day's grace
    ("done", 15, True), ("failed", 15, True), ("done", 5, False),
    ("shipped", 60, False),                       # waiting on CI or review
    ("awaiting_approval", 60, False), ("running", 60, False),
])
def test_when_a_checkout_may_go(status, age, due):
    t = {"kind": "code", "status": status, "created_at": time.time() - age * DAY}
    assert tasks.checkout_due(t, time.time()) is due


def test_an_analysis_task_has_no_checkout_to_sweep():
    assert not tasks.checkout_due({"kind": "analysis", "status": "done",
                                   "created_at": 0}, time.time())


def test_the_sweep_removes_finished_work_and_keeps_the_branch(ws):
    root, _ = ws
    t = _task("merged", 3)
    tree = _checkout(root, t)
    assert asyncio.run(tasks.sweep_checkouts()) == [t["id"]]
    assert not tree.exists()
    branches = subprocess.run(["git", "branch"], cwd=root / "telikos-booking-service",
                              capture_output=True, text=True).stdout
    assert f"feature/customs-{t['id']}" in branches


def test_the_sweep_never_drops_uncommitted_work(ws):
    root, _ = ws
    t = _task("merged", 3)
    tree = _checkout(root, t)
    (tree / "telikos-booking-service" / "wip.txt").write_text("only copy")
    assert asyncio.run(tasks.sweep_checkouts()) == []
    assert (tree / "telikos-booking-service" / "wip.txt").exists()


def test_a_running_task_is_never_swept(ws):
    root, _ = ws
    tree = _checkout(root, _task("awaiting_approval", 30))
    asyncio.run(tasks.sweep_checkouts())
    assert tree.exists()


# --- coming back to a swept task ------------------------------------------------------

def test_a_swept_task_never_runs_in_his_own_checkout(ws):
    root, _ = ws
    t = _task("done", 20)
    _checkout(root, t)
    asyncio.run(tasks.sweep_checkouts())
    assert tasks.task_cwd(t["id"], "booking") != str(root)


def test_a_swept_checkout_comes_back_on_its_branch(ws):
    root, _ = ws
    t = _task("done", 20)
    store.kv_set(f"task_branch:{t['id']}", f"feature/customs-{t['id']}")
    _checkout(root, t)
    asyncio.run(tasks.sweep_checkouts())
    asyncio.run(tasks.ensure_checkout(t["id"]))
    tree = Path(tasks.task_cwd(t["id"], "booking")) / "telikos-booking-service"
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=tree,
                          capture_output=True, text=True).stdout.strip()
    assert head == f"feature/customs-{t['id']}"


# --- PR review copies -----------------------------------------------------------------

def test_an_old_review_checkout_is_removed(ws):
    root, home = ws
    repo = root / "telikos-booking-service"
    tree = worktrees.review_root(root, repo.name, "1459")
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    old = time.time() - 8 * DAY
    os.utime(tree, (old, old))
    assert asyncio.run(worktrees.sweep_reviews(root)) == ["review-telikos-booking-service-1459"]
    assert not tree.exists()
