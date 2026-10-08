"""What a task committed is measured against the repo's real base. 8 Oct, #268.

The library's base is main. The count was taken against origin/develop alone,
git failed quietly, a real commit (adeb881) read as "no changes", and the
finished task was marked failed.
"""

from __future__ import annotations

import subprocess

import pytest

from app import repo_ops, store, tasks


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_on(tmp_path):
    def make(base: str):
        remote, work = tmp_path / f"{base}.git", tmp_path / "task" / "lib"
        _git(tmp_path, "init", "-q", "--bare", str(remote))
        work.mkdir(parents=True)
        _git(work, "init", "-q", "-b", base)
        _git(work, "config", "user.email", "a@b")
        _git(work, "config", "user.name", "a")
        (work / "A.java").write_text("class A {}\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "base")
        _git(work, "remote", "add", "origin", str(remote))
        _git(work, "push", "-q", "origin", base)
        _git(work, "checkout", "-qb", "feature/asta-1")
        (work / "A.java").write_text("class A { int x; }\n")
        _git(work, "commit", "-qam", "Make configs optional")
        return work
    return make


@pytest.mark.parametrize("base", ["main", "develop", "master"])
def test_a_commit_counts_whatever_the_base_branch(repo_on, base, monkeypatch, tmp_path):
    work = repo_on(base)
    assert repo_ops.base_ref(work) == f"origin/{base}"
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(tmp_path / "task"))
    t = store.create_task("Fix library", "code", "p", "booking")
    landed = tasks.committed_so_far(t["id"], t)
    assert landed and landed[0]["commits"][0].endswith("Make configs optional")


def test_no_remote_base_is_no_answer_not_a_crash(tmp_path):
    _git(tmp_path, "init", "-q")
    assert repo_ops.base_ref(tmp_path) == ""


def test_approving_a_proposed_change_says_it_is_being_implemented(monkeypatch):
    """8 Oct: "Approve task 268" was answered "continuing the existing diff with
    your feedback" — as if his approval had been read as feedback."""
    import asyncio
    import json
    t = store.create_task("Fix library", "code", "p", "booking")
    store.update_task(t["id"], status="awaiting_approval")
    store.kv_set(f"task_gate:{t['id']}", "change")
    store.kv_set(f"task_proposal:{t['id']}", json.dumps(
        {"plan": "Make the three configs @Nullable.", "feedback": "fix startup", "status": "failed"}))
    ran = []

    async def fake_refine(task_id, text, **kw):
        ran.append(kw)
        return f"Task #{task_id}: continuing the existing diff with your feedback."
    monkeypatch.setattr(tasks, "refine", fake_refine)
    out = asyncio.run(tasks.approve(t["id"]))
    assert out == f"Task #{t['id']}: plan approved — implementing now."
    assert ran == [{"code_change": True, "approved_plan": True}]
