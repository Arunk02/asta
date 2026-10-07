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
