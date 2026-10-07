"""A follow-up run reports what it actually did. 7 Oct, task #257 and #263.

#257 was told to fix the red component test on booking PR 1470; it started the
log download in the background, stopped, and was announced as "✅ DONE … Already
pushed — the PR is updated" with nothing changed and CI still red. #263's
question said "the two options above" and showed none, then showed raw JSON.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from app import main, store, tasks


@pytest.fixture
def pushed(tmp_path, monkeypatch):
    repo = tmp_path / "telikos-booking-service"
    repo.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "a@b"],
                ["git", "config", "user.name", "a"]):
        subprocess.run(cmd, cwd=repo, check=True)
    (repo / "A.java").write_text("class A {}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
    from app import worktrees
    monkeypatch.setattr(tasks, "task_cwd", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(worktrees, "repos_in", lambda root: [repo])
    t = store.create_task("Booking job open", "code", "p", "booking")
    store.update_task(t["id"], status="pr_ci_failed", pr_state="red/REVIEW_REQUIRED",
                      pr_urls="telikos-booking-service: "
                              "https://github.com/acme/telikos-booking-service/pull/1470")
    said = []

    async def notify(text, *a, **k):
        said.append(text)
        return {}
    from app import notify as _notify
    monkeypatch.setattr(_notify, "notify", notify)
    return {"repo": repo, "task": store.get_task(t["id"]), "said": said}


def test_a_followup_that_changed_nothing_is_not_done(pushed, monkeypatch):
    t = pushed["task"]
    asyncio.run(tasks._record_followup_start(t["id"], t))
    store.update_task(t["id"], status="running")

    async def pushed_already(*a):
        return ["telikos-booking-service: https://github.com/acme/telikos-booking-service/pull/1470"]
    monkeypatch.setattr(tasks, "_already_pushed", pushed_already)
    asyncio.run(tasks.complete(t["id"], store.get_task(t["id"]),
                               "Waiting on both `gh run view --log-failed` fetches to finish "
                               "downloading; will analyze as soon as they land."))
    said = pushed["said"][-1]
    assert "no change made" in said and "Waiting on both" in said
    assert "pull/1470" in said and "still red" in said and "fix #" in said
    assert "DONE" not in said and "PR is updated" not in said
    assert store.get_task(t["id"])["status"] == "pr_ci_failed"


def test_a_followup_that_committed_is_reported_as_usual(pushed):
    t = pushed["task"]
    asyncio.run(tasks._record_followup_start(t["id"], t))
    (pushed["repo"] / "A.java").write_text("class A { int x; }\n")
    subprocess.run(["git", "commit", "-qam", "fix"], cwd=pushed["repo"], check=True)
    assert not asyncio.run(tasks._followup_changed_nothing(t["id"], t, "Fixed the CT step."))


def test_the_worker_is_told_never_to_wait_in_the_background():
    assert "Never run a command in the background" in tasks.CODE_OVERRIDES


def test_a_task_question_shows_the_options_not_raw_json():
    result = ('Logged as needs-input — nothing changed. Waiting on your call between the '
              'two options above.\n\n```outcome\n{"kind": "needs_input", "summary": '
              '"Removing the booking JAAS default reopens the crash. Pick: accept CT breaks, '
              'or downgrade the library."}\n```')
    shown = tasks.readable_outcome(result)
    assert "```" not in shown and '"kind"' not in shown
    assert "Pick: accept CT breaks, or downgrade the library." in shown


def test_asta_handing_an_answer_to_its_own_task_is_not_a_false_send():
    assert main._INTERNAL_RELAY.search("Sent to task #263 — it'll strip the booking JAAS config")
    assert not main._INTERNAL_RELAY.search("Sent it to Vinish on Teams")


def test_a_ci_fix_starts_from_the_failed_logs_asta_read(monkeypatch):
    t = store.create_task("Booking job open", "code", "p", "booking")
    store.update_task(t["id"], pr_urls="r: https://github.com/acme/booking/pull/1470")
    pr = {"statusCheckRollup": [
        {"name": "cicd / Component test", "conclusion": "FAILURE",
         "detailsUrl": "https://github.com/acme/booking/actions/runs/1/job/2"}]}

    async def state(url):
        return pr

    async def why(pr, url, timeout=120):
        return "\nFailed: ReadyForPlanningTests JOB_OPENED step expected SOFT_CLOSED"
    monkeypatch.setattr(tasks, "_pr_state", state)
    monkeypatch.setattr(tasks, "_why_red", why)
    monkeypatch.setenv("ASTA_CI_PREFETCH", "1")
    got = asyncio.run(tasks._ci_failure_for(store.get_task(t["id"]), "fix the CI failure"))
    assert "cicd / Component test" in got and "expected SOFT_CLOSED" in got
    assert asyncio.run(tasks._ci_failure_for(store.get_task(t["id"]), "rename the flag")) == ""
    monkeypatch.setenv("ASTA_CI_PREFETCH", "0")
    assert asyncio.run(tasks._ci_failure_for(store.get_task(t["id"]), "fix the CI failure")) == ""


# --- the task owns its red CI until merge -------------------------------------------

LOG_END = ("2026-10-07T13:38:35.7604587Z [ERROR] Tests run: 53, Failures: 1, Errors: 0, "
           "Skipped: 0 <<< FAILURE! -- in booking.events.processor.TestRunner\n"
           "2026-10-07T13:38:35.7607168Z org.junit.ComparisonFailure: Work Process Name in "
           "Booking DB do not match expected:<[JOB_OPENED]> but was:<[SEND_TO_TMS]>\n"
           "2026-10-07T13:38:35.8082884Z [ERROR] BUILD FAILURE\n"
           "2026-10-07T13:38:35.9Z some ordinary line\n")


def test_a_failed_jobs_log_is_read_from_its_end(monkeypatch, tmp_path):
    """153 MB for booking's component test: only the tail is fetched."""
    import httpx
    asked = []

    class _R:
        def __init__(self, status=200, headers=None, text=""):
            self.status_code, self.headers, self.text = status, headers or {}, text
            self.is_redirect = status in (301, 302, 307)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, follow_redirects=True):
            asked.append(("GET", url, (headers or {}).get("Range")))
            if "api.github.com" in url:
                return _R(302, {"location": "https://blob.example/log"})
            return _R(206, text=LOG_END)

        async def head(self, url):
            return _R(200, {"content-length": "153460815"})

    async def token(repo):
        return "t"
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(tasks, "_github_token", token)
    path, hits = asyncio.run(tasks._job_log_tail("acme/booking", "112816764094", tmp_path))
    assert ("GET", "https://blob.example/log", f"bytes={153460815 - 3000000}-153460814") in asked
    assert any("expected:<[JOB_OPENED]> but was:<[SEND_TO_TMS]>" in h for h in hits)
    assert not any("ordinary line" in h for h in hits)
    assert path.endswith("ci-job-112816764094-tail.log")


def test_red_ci_after_the_rerun_is_analysed_and_planned_for_approval(monkeypatch):
    monkeypatch.setenv("ASTA_CI_AUTOFIX", "1")
    t = store.create_task("Booking job open", "code", "p", "booking")
    store.update_task(t["id"], status="shipped", pr_urls="r: https://github.com/o/r/pull/7")
    store.kv_set(f"task_ci_rerun:{t['id']}", "1")             # the one re-run already happened

    async def red(url):
        return {"state": "OPEN", "statusCheckRollup": [{"conclusion": "FAILURE"}]}
    fixes = []

    async def propose(task_id, feedback):
        fixes.append((task_id, feedback))
        return "planning"

    async def must_not_fix(*a, **k):
        raise AssertionError("changed code without his approval")
    monkeypatch.setattr(tasks, "_pr_state", red)
    monkeypatch.setattr(tasks, "propose_change", propose)
    monkeypatch.setattr(tasks, "refine", must_not_fix)
    note = asyncio.run(tasks.check_pr(t["id"]))
    assert fixes and fixes[0][0] == t["id"] and "do not download CI logs" in fixes[0][1]
    assert "working out why" in note and "nothing changes before you do" in note
    assert "Say *rerun ci" not in note
    store.kv_set(f"task_ci_autofix:{t['id']}", str(tasks.CI_AUTOFIX_MAX))
    store.update_task(t["id"], pr_state="")
    store.kv_del(f"pr_told:https://github.com/o/r/pull/7")
    note = asyncio.run(tasks.check_pr(t["id"]))
    assert "it needs you" in note and len(fixes) == 1


def test_a_test_change_is_planned_then_applied_only_after_approval(pushed, monkeypatch):
    """"Show the plan for a CT upfront" (7 Oct) — and only then write it."""
    t = pushed["task"]
    store.update_task(t["id"], status="shipped")
    legs = []

    async def leg(task_id, prompt, cwd, **k):
        legs.append((tasks.plan_approved(task_id), prompt))
        return "1. CAUSE: step checks the booking work process\n2. PLAN: assert the finance work process"
    resumed = []

    async def resume(task_id, text, approved=False):
        resumed.append(text)
    monkeypatch.setattr(tasks, "_run_code_leg", leg)
    monkeypatch.setattr(tasks, "_resume_worker", resume)

    async def go():
        out = await tasks.refine(t["id"], "add the JOB_OPENED CT in the existing scenario")
        await asyncio.sleep(0.05)
        return out
    out = asyncio.run(go())
    assert "you'll approve it" in out
    assert legs and legs[0][0] is False and "PLAN ONLY" in legs[0][1], "the planning leg cannot write"
    assert store.get_task(t["id"])["status"] == "awaiting_approval"
    assert "proposed change (nothing changed yet)" in pushed["said"][-1]
    assert not resumed

    async def approve():
        out = await tasks.approve(t["id"])
        await asyncio.sleep(0.05)
        return out
    asyncio.run(approve())
    assert resumed and "assert the finance work process" in resumed[0]
    assert tasks.plan_approved(t["id"])


def test_a_bare_ship_with_two_possible_tasks_asks_which(monkeypatch):
    """22:31 — "ship" was for #257 (finished on its open PR); it approved #263."""
    from app import go
    plan = store.create_task("Remove unneeded booking JAAS config", "code", "p", "email")
    store.update_task(plan["id"], status="awaiting_approval")
    fix = store.create_task("Booking job open", "code", "p", "booking")
    store.update_task(fix["id"], status="done",
                      pr_urls="r: https://github.com/acme/booking/pull/1470")
    t, problem = go.target(None)
    assert t is None and "Which one" in problem
    assert f"#{plan['id']}" in problem and f"#{fix['id']}" in problem
    t, problem = go.target(fix["id"])
    assert t["id"] == fix["id"] and not problem
