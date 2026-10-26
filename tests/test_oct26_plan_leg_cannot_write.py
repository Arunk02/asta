"""26 Oct, #419: a planning step wrote the change through Bash and python, with
Edit and Write denied. The tree is checked after a planning leg instead."""
import asyncio
import subprocess
from pathlib import Path

from app import store, tasks


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "svc"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True, check=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "A.java").write_text("class A {}\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    return repo


def test_files_written_by_a_planning_leg_are_stashed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(tasks, "_repos_under", lambda root: [repo])
    tid = store.create_task("t", "code", "x", "booking")["id"]
    clean = asyncio.run(tasks._clean_repos(str(tmp_path)))
    (repo / "A.java").write_text("class A { int written; }\n")
    (repo / "B.java").write_text("class B {}\n")
    note = asyncio.run(tasks._set_aside_plan_writes(tid, clean))
    assert "set aside" in note
    assert (repo / "A.java").read_text() == "class A {}\n" and not (repo / "B.java").exists()
    stash = subprocess.run(["git", "stash", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert f"task #{tid} planning step wrote files" in stash


def test_a_planning_leg_that_only_read_says_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(tasks, "_repos_under", lambda root: [repo])
    tid = store.create_task("t", "code", "x", "booking")["id"]
    clean = asyncio.run(tasks._clean_repos(str(tmp_path)))
    assert asyncio.run(tasks._set_aside_plan_writes(tid, clean)) == ""


def test_a_tree_that_was_already_dirty_is_left_alone(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(tasks, "_repos_under", lambda root: [repo])
    (repo / "A.java").write_text("his own unfinished work\n")
    assert asyncio.run(tasks._clean_repos(str(tmp_path))) == []
