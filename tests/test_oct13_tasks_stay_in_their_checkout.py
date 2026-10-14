"""13 Oct, email PR 676 ("bump the event-router library, remove the workaround").

#295 needed PR 676's branch; #251 had failed on 7 Oct and its checkout still
held it, so #295 edited #251's checkout instead and reported "blocked". #298, a
second task for the same ask, named its workspace "email": `_prepare_branches`
resolved that to Asta's own repo and prepared nothing, while `task_cwd` resolved
it to ~/booking-workspace — so it cut a branch and committed in the checkout he
has open, on develop, with the old version.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from app import store, tasks, workspace_tools, worktrees


def _repo(root: Path, name: str) -> Path:
    """A real repo with an origin, so 'pushed' means something."""
    origin = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True)
    repo = root / "ws" / name
    repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)  # noqa: E731
    run("git", "config", "user.email", "t@example.com"); run("git", "config", "user.name", "T")
    run("git", "checkout", "-q", "-b", "develop")
    (repo / "pom.xml").write_text("<version>3.2.26</version>\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "base"); run("git", "push", "-q", "origin", "develop")
    return repo


@pytest.fixture
def ws(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "telikos-email-service")
    root = repo.parent
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": root})
    return root


def _task(kind="code", status="running", workspace="booking", title="bump the library"):
    tid = store.create_task(title, kind, "prompt", workspace)["id"]
    store.update_task(tid, status=status)
    return tid


# --- one resolver -----------------------------------------------------------------------

def test_a_workspace_named_by_its_directory_is_prepared_where_it_runs(ws, monkeypatch):
    """'email' is no registry key. Both resolvers must agree on where it is."""
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    seen = {}

    async def create(root, task_id, branch, *hints, **_kw):
        seen["root"] = Path(root)
        return [{"repo": "telikos-email-service", "branch": branch, "ok": True, "note": ""}]

    monkeypatch.setattr(worktrees, "create", create)
    tid = _task(workspace="booking")
    asyncio.run(tasks._prepare_branches(tid, store.get_task(tid)))
    assert seen["root"].resolve() == Path(tasks.code_cwd("booking")).resolve()
    assert seen["root"].resolve() != tasks.ROOT.resolve()


def test_prepare_never_uses_the_reading_resolver():
    import inspect
    src = inspect.getsource(tasks._prepare_branches)
    assert "code_cwd(" in src and "_cwd(t.get(\"workspace\"))" not in src.replace("code_cwd", "")


# --- never in his tree --------------------------------------------------------------------

def test_a_code_leg_without_its_own_checkout_refuses_his_tree(ws):
    tid = _task()
    with pytest.raises(RuntimeError, match="the tree Arun has open"):
        tasks.refuse_shared_tree(tid, str(ws), "booking")


def test_a_code_leg_in_its_own_checkout_runs(ws):
    tid = _task()
    assert all(r["ok"] for r in asyncio.run(worktrees.create(ws, tid, f"feature/t-{tid}")))
    tasks.refuse_shared_tree(tid, tasks.task_cwd(tid, "booking"), "booking")


def test_the_leg_wrapper_refuses_before_any_brain_runs(ws, monkeypatch):
    called = []

    async def leg(*a, **k):
        called.append(1)
        return "ok"

    monkeypatch.setattr(tasks, "_code_leg", leg)
    tid = _task()
    with pytest.raises(RuntimeError):
        asyncio.run(tasks._run_code_leg(tid, "x", str(ws), resume=False, effort="", workspace="booking"))
    assert called == []


# --- a finished task's checkout does not hold a branch hostage ----------------------------

def _checkout(ws, status, *, dirty=False, unpushed=False):
    tid = _task(status=status)
    branch = f"release/temporal-cert-{tid}"
    assert all(r["ok"] for r in asyncio.run(worktrees.create(ws, tid, branch)))
    path = Path(tasks.task_cwd(tid, "booking")) / "telikos-email-service"
    run = lambda *a: subprocess.run(a, cwd=path, capture_output=True, text=True)  # noqa: E731
    run("git", "push", "-q", "origin", branch)
    if unpushed:
        (path / "x.txt").write_text("x"); run("git", "add", "-A"); run("git", "commit", "-qm", "local")
    if dirty:
        (path / "pom.xml").write_text("<version>3.2.27-SNAPSHOT</version>\n")
    return tid


def test_a_clean_pushed_failed_task_frees_its_branch(ws):
    held = _checkout(ws, "failed")
    freed = asyncio.run(tasks.free_idle_checkouts(ws, but=999))
    assert held in freed and not worktrees.exists(ws, held)


def test_uncommitted_or_unpushed_work_is_never_freed(ws):
    dirty = _checkout(ws, "failed", dirty=True)
    local = _checkout(ws, "done", unpushed=True)
    asyncio.run(tasks.free_idle_checkouts(ws, but=999))
    assert worktrees.exists(ws, dirty) and worktrees.exists(ws, local)


def test_a_running_or_waiting_task_keeps_its_checkout(ws):
    for status in ("running", "awaiting_approval", "shipped", "paused"):
        tid = _checkout(ws, status)
        asyncio.run(tasks.free_idle_checkouts(ws, but=999))
        assert worktrees.exists(ws, tid), status


def test_a_freed_checkout_comes_back_when_its_task_resumes(ws):
    held = _checkout(ws, "failed")
    asyncio.run(tasks.free_idle_checkouts(ws, but=999))
    asyncio.run(tasks.ensure_checkout(held))
    assert worktrees.exists(ws, held)


# --- if anything else changed while it ran, he hears ----------------------------------------

def test_an_edit_in_another_checkout_is_reported(ws, monkeypatch):
    other = _checkout(ws, "failed")
    me = _task()
    asyncio.run(worktrees.create(ws, me, f"feature/me-{me}"))
    mine = tasks.task_cwd(me, "booking")
    before = asyncio.run(tasks.other_trees("booking", mine))
    (Path(tasks.task_cwd(other, "booking")) / "telikos-email-service" / "pom.xml").write_text("changed\n")
    told = []

    async def notify(text, *a, **k):
        told.append(text)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    asyncio.run(tasks._report_strays(me, before, asyncio.run(tasks.other_trees("booking", mine))))
    assert told and f"task-{other}" in told[0] and "files changed" in told[0]


def test_his_own_checkout_switching_branch_is_reported(ws, monkeypatch):
    me = _task()
    asyncio.run(worktrees.create(ws, me, f"feature/me-{me}"))
    mine = tasks.task_cwd(me, "booking")
    before = asyncio.run(tasks.other_trees("booking", mine))
    subprocess.run(["git", "checkout", "-q", "-b", "chore/bump"], cwd=ws / "telikos-email-service")
    told = []

    async def notify(text, *a, **k):
        told.append(text)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    asyncio.run(tasks._report_strays(me, before, asyncio.run(tasks.other_trees("booking", mine))))
    assert told and "develop → chore/bump" in told[0]


def test_nothing_changed_nothing_said(ws, monkeypatch):
    me = _task()
    asyncio.run(worktrees.create(ws, me, f"feature/me-{me}"))
    mine = tasks.task_cwd(me, "booking")
    snap = asyncio.run(tasks.other_trees("booking", mine))
    told = []

    async def notify(text, *a, **k):
        told.append(text)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    asyncio.run(tasks._report_strays(me, snap, asyncio.run(tasks.other_trees("booking", mine))))
    assert told == []


def test_the_leg_is_told_to_stay_inside():
    assert "never edit" in tasks._stay_inside("/x/task-1") and "/x/task-1" in tasks._stay_inside("/x/task-1")


def test_a_resumed_leg_frees_the_branch_too(ws, monkeypatch):
    """'task 295: continue' never prepares again — the leg itself frees."""
    held = _checkout(ws, "failed")
    me = _task(status="running")
    asyncio.run(worktrees.create(ws, me, f"feature/me-{me}"))

    async def leg(*a, **k):
        return "ok"

    monkeypatch.setattr(tasks, "_code_leg", leg)
    asyncio.run(tasks._run_code_leg(me, "continue", tasks.task_cwd(me, "booking"),
                                    resume=True, effort="", workspace="booking"))
    assert not worktrees.exists(ws, held)
