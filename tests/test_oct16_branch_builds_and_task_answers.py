"""16 Oct, after booking PR 1487 merged to develop:

- develop's component test failed twice and DEV_Sanity went red, and he heard
  nothing: the run was Vinish's merge (not "his" run) and it sat at a deploy
  approval, so it never "completed";
- task #345 sat 25 minutes on `gh run watch` for that run, which never ends;
- its card said only "tier: T2 / route: plan";
- its answer — "no fix made, nothing to fix" — reached him as "📋 PLAN #345 …
  approve task 345".
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import ci_watch, store, task_fence, tasks

REPO = "Maersk-Global/telikos-booking-service"


def _run(rid, branch="develop", event="push", status="completed", concl="success", attempt=1,
         workflow="MSS-Build and deploy to lower envioronments", pr=1487):
    return {"databaseId": rid, "status": status, "conclusion": concl, "workflowName": workflow,
            "headBranch": branch, "displayTitle": f"Merge pull request #{pr} from Maersk-Global/feature/x",
            "url": f"https://gh/{rid}", "event": event, "attempt": attempt}


CI_OK = [{"name": "cicd / Build", "status": "completed", "conclusion": "success"},
         {"name": "cicd / Component test", "status": "completed", "conclusion": "success"},
         {"name": "cicd / Docker Build and Push", "status": "completed", "conclusion": "success"},
         {"name": "cicd / DEV_Sanity", "status": "completed", "conclusion": "failure"},
         {"name": "cicd / DEV", "status": "completed", "conclusion": "failure"},
         {"name": "cicd / SIT", "status": "waiting", "conclusion": ""}]
CI_RED = [{"name": "cicd / Build", "status": "completed", "conclusion": "success"},
          {"name": "cicd / Component test", "status": "completed", "conclusion": "failure"},
          {"name": "cicd / DEV_Sanity", "status": "completed", "conclusion": "failure"}]
CI_RUNNING = [{"name": "cicd / Build", "status": "completed", "conclusion": "success"},
              {"name": "cicd / Component test", "status": "in_progress", "conclusion": ""}]


def _gh(monkeypatch, runs, jobs=None, authors=None):
    authors = {"1487": "Arunk540", "1488": "VinishKumar1", **(authors or {})}

    async def fake(*args, timeout=30):
        args = list(args)
        if args[:2] == ["pr", "view"]:
            return 0, authors.get(args[2], "someone")
        if args[0] == "pr":
            return 0, "[]"
        if "--user" in args:
            return 0, "[]"                       # the merge runs are not "his" runs
        if args[:2] == ["run", "view"]:
            return 0, json.dumps({"jobs": (jobs or {}).get(args[2], CI_OK)})
        return 0, json.dumps(runs)

    monkeypatch.setattr(ci_watch, "_run_gh", fake)

    async def me():
        return "Arunk540"

    monkeypatch.setattr(ci_watch, "my_login", me)
    monkeypatch.setattr(ci_watch.repo_ops, "office_login", lambda repo: "Arunk540")


def _poll():
    return asyncio.run(ci_watch._poll_repo(REPO))


def _baseline(monkeypatch):
    _gh(monkeypatch, [_run(1)])
    _poll()                                              # first look baselines, silent


def test_ci_passing_after_his_merge_is_told_so_he_can_share_the_build(monkeypatch):
    _baseline(monkeypatch)
    _gh(monkeypatch, [_run(1), _run(2, status="waiting", concl="")])
    notes = _poll()
    assert len(notes) == 1 and "✅ CI passed on develop after your booking PR 1487" in notes[0]
    assert "ready to share for deploy" in notes[0]
    assert _poll() == []                                 # once


def test_ci_failing_after_his_merge_names_the_job_not_sanity_or_deploy(monkeypatch):
    _baseline(monkeypatch)
    _gh(monkeypatch, [_run(1), _run(2, concl="failure")], {"2": CI_RED})
    notes = _poll()
    assert len(notes) == 1 and "🔴 CI failed on develop" in notes[0]
    assert "Component test failed" in notes[0] and "Sanity" not in notes[0]


def test_a_re_run_that_passes_is_told_too(monkeypatch):
    _baseline(monkeypatch)
    _gh(monkeypatch, [_run(1), _run(2, concl="failure")], {"2": CI_RED})
    _poll()
    _gh(monkeypatch, [_run(1), _run(2, attempt=3, status="waiting", concl="")], {"2": CI_OK})
    notes = _poll()
    assert len(notes) == 1 and "passed (re-run)" in notes[0]


def test_still_running_says_nothing_yet(monkeypatch):
    _baseline(monkeypatch)
    _gh(monkeypatch, [_run(1), _run(2, status="in_progress", concl="")], {"2": CI_RUNNING})
    assert _poll() == []


def test_someone_elses_merge_is_not_his(monkeypatch):
    _baseline(monkeypatch)
    _gh(monkeypatch, [_run(1), _run(2, concl="failure", pr=1488)], {"2": CI_RED})
    assert _poll() == []


def test_only_sanity_or_deploy_red_is_a_pass(monkeypatch):
    _baseline(monkeypatch)
    _gh(monkeypatch, [_run(1), _run(2, concl="failure")], {"2": CI_OK})
    assert "✅" in _poll()[0]


BUILD = "MSS-Build and deploy to lower envioronments"


@pytest.mark.parametrize("branch,event,workflow,followed", [
    ("main", "push", BUILD, True), ("release/2.2.4", "push", BUILD, True),
    ("develop", "pull_request", BUILD, False), ("feature/x", "push", BUILD, False),
    ("main", "push", "Auto merge PR", False), ("main", "push", "Internal Label Check", False),
    ("develop", "push", "CodeQL", False), ("develop", "push", "Run IaC Security Scan", False)])
def test_which_builds_are_branch_builds(branch, event, workflow, followed):
    assert ci_watch._is_branch_build(
        {"headBranch": branch, "event": event, "workflowName": workflow}) is followed


# --- a task never waits on CI ------------------------------------------------------------

def test_a_task_may_not_wait_on_ci():
    assert "gh run watch" in task_fence.denied("code") and "gh run watch" in task_fence.denied("none")
    shim = Path(task_fence.SHIMS) / "gh"
    for args in (["run", "watch", "1", "-R", "a/b"], ["pr", "checks", "1", "--watch"]):
        out = subprocess.run([str(shim), *args], env={"PATH": f"{task_fence.SHIMS}:/usr/bin:/bin",
                                                      "ASTA_TASK_WRITES": "code"},
                             capture_output=True, text=True)
        assert out.returncode == 3 and "do not wait on CI" in out.stderr


# --- findings are findings ------------------------------------------------------------------

def test_no_change_proposed_is_findings_and_closes(monkeypatch):
    sent = []

    async def notify(text, *a, **k):
        sent.append(text)

    from app import notify as n
    monkeypatch.setattr(n, "notify", notify)
    monkeypatch.setattr(tasks, "_graph", lambda: type("G", (), {"manages": staticmethod(lambda _id: False)})())
    tid = store.create_task(title="Investigate CT failures in develop", kind="code", prompt="x",
                            workspace="booking")["id"]
    t = store.get_task(tid)
    asyncio.run(tasks.announce_plan(tid, t, "## Findings\nCT passed.\n**No fix made** — there's "
                                            "nothing in this PR's diff to fix."))
    assert "what I found" in sent[0] and "approve task" not in sent[0]
    assert store.get_task(tid)["status"] == "done"


def test_a_real_plan_is_still_a_plan(monkeypatch):
    sent = []

    async def notify(text, *a, **k):
        sent.append(text)

    from app import notify as n
    monkeypatch.setattr(n, "notify", notify)
    tid = store.create_task(title="Fix the validator", kind="code", prompt="x", workspace="booking")["id"]
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), "1. Change the validator\n2. Add tests"))
    assert "PLAN" in sent[0] and store.get_task(tid)["status"] == "awaiting_approval"


def test_the_card_says_what_it_is_doing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tid = store.create_task(title="Investigate CT", kind="code", prompt="x", workspace="booking")["id"]
    store.kv_set(f"task_session:{tid}:claude", "sid-1")
    d = tmp_path / ".claude" / "projects" / "-x-task"
    d.mkdir(parents=True)
    (d / "sid-1.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-16T05:59:29Z",
        "message": {"content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": "gh run watch 38854502635 --exit-status"}}]}}) + "\n")
    store.update_task(tid, status="running")
    from app import frontdesk
    assert "running Bash: gh run watch 38854502635" in frontdesk.task_card(tid)
