"""The check runs in the task's own tree, per repo, found by the repo's name.

Found on 13 Sep, driving a real task end to end: a code task builds in
`.asta-worktrees/task-N/<repo>`, but the gate ran his tests in the shared
checkout — code the task had never touched — and looked the command up by the
folder's name, which in a worktree is `task-N` and matches nothing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import verify


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: elsewhere")          # what a worktree has
    return path


def test_each_repo_in_a_task_tree_is_found_by_its_own_name(tmp_path, monkeypatch):
    tree = tmp_path / ".asta-worktrees" / "task-7"
    _repo(tree / "booking-service")
    _repo(tree / "no-check-repo")
    table = tmp_path / "verify-commands.json"
    table.write_text(json.dumps({"booking-service": "mvn -q test"}))
    monkeypatch.setattr(verify, "COMMANDS_FILE", table)
    assert verify.targets(str(tree)) == [(str(tree / "booking-service"), "mvn -q test")]


def test_the_workspace_pins_are_read_for_a_repo_checked_out_elsewhere(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    pins = workspace / ".contmark" / "repos" / "ap-service" / "_pins.yml"
    pins.parent.mkdir(parents=True)
    pins.write_text("commands:\n  unit_test: mvn -q -f service/pom.xml clean test\n")
    tree = workspace / ".asta-worktrees" / "task-9"
    _repo(tree / "ap-service")
    monkeypatch.setattr(verify, "COMMANDS_FILE", tmp_path / "none.json")
    assert verify.targets(str(tree), workspace_root=str(workspace)) == [
        (str(tree / "ap-service"), "mvn -q -f service/pom.xml clean test")]


def test_a_single_repo_tree_is_checked_where_it_is(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(verify, "COMMANDS_FILE", tmp_path / "none.json")
    assert verify.targets(str(tmp_path)) == [(str(tmp_path), "python -m pytest -q")]


def test_the_first_red_repo_is_the_answer_and_green_needs_all(tmp_path, monkeypatch):
    tree = tmp_path / "task-3"
    _repo(tree / "a")
    _repo(tree / "b")
    table = tmp_path / "t.json"
    table.write_text(json.dumps({"a": "check-a", "b": "check-b"}))
    monkeypatch.setattr(verify, "COMMANDS_FILE", table)
    results = {"check-a": True, "check-b": False}

    async def run(cwd, cmd, _retried=False):
        return verify.VerifyResult(ran=True, ok=results[cmd], command=cmd, code=0, tail=cmd)

    monkeypatch.setattr(verify, "run", run)
    red = asyncio.run(verify.check_tree(str(tree)))
    assert not red.ok and red.command == "check-b"
    results["check-b"] = True
    green = asyncio.run(verify.check_tree(str(tree)))
    assert green.ok and green.command == "check-a; check-b"


def test_the_finished_diff_is_read_from_the_tasks_own_tree(tmp_path, monkeypatch):
    """Found on the same run: DONE carried no review, because the reviewer looked
    for the diff in the shared checkout, where a worktree task never commits."""
    import subprocess
    from app import store, tasks, review

    def git(repo, *a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    shared = tmp_path / "ws" / "calc"
    shared.mkdir(parents=True)
    git(shared, "init", "-q", "-b", "develop")
    (shared / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(shared, "add", "-A")
    git(shared, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    own = tmp_path / "ws" / ".asta-worktrees" / "task-5"
    git(shared, "worktree", "add", "-q", "-b", "feature/x", str(own / "calc"), "develop")
    monkeypatch.setattr(tasks, "code_cwd", lambda ws: str(tmp_path / "ws"))
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(own))
    t = store.create_task("fix add", "code", "p", None)
    store.kv_set(f"task_rollback:{t['id']}", json.dumps({"calc": {"sha": subprocess.run(
        ["git", "-C", str(shared), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()}}))
    (own / "calc" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(own / "calc", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fix")
    seen = {}

    async def fake(diff, workspace="", **kw):
        seen["diff"] = diff
        return "- no test for negative numbers"

    monkeypatch.setattr(review, "review_own_diff", fake)
    monkeypatch.setattr(tasks, "_second_reviewer", lambda tid: "")
    note = asyncio.run(tasks._self_review(t["id"], {"workspace": None}, "done"))
    assert "return a + b" in seen.get("diff", "") and "negative numbers" in note
