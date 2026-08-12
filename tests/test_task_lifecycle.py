"""A task is not finished when the PR is raised.

Two reported failures, one shape: the task ended too early and lost its context.

  - "it's marking the task as closed [when the PR is raised] — until the PR is
    merged or closed it has to keep that in track for CI monitoring";
  - "if I give feedback it's creating new tasks and reprocessing".

The second was caused partly by the first and partly by `live_tasks_for`, which
rewrote the conversation's task list to the LIVE subset on every call — deleting
the only trail back to the work that had just finished.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import store, tasks


def _task(status="done", kind="code", title="Skip vessel schedule for cancelled bookings",
          finished=None, workspace=None, **kw):
    t = store.create_task(title, kind, "prompt", workspace, "")
    store.update_task(t["id"], status=status,
                      finished_at=time.time() if finished is None else finished, **kw)
    return t["id"]


# --- the PR keeps the task open ----------------------------------------------

def test_a_shipped_task_is_not_finished():
    tid = _task(status="shipped", pr_urls="repo: https://github.com/o/r/pull/7",
                pr_state="OPEN")
    assert tid in tasks.open_prs()
    assert store.get_task(tid)["status"] not in tasks.CLOSED_STATUSES


def test_a_done_task_is_not_watched_as_a_pr():
    """Nothing was pushed, so there is nothing to follow."""
    assert _task(status="done") not in tasks.open_prs()


@pytest.mark.asyncio
async def test_a_merge_closes_the_task(monkeypatch):
    tid = _task(status="shipped", pr_urls="repo: https://github.com/o/r/pull/7")

    async def merged(url):
        return {"state": "MERGED", "mergedAt": "2026-08-12T10:00:00Z"}

    monkeypatch.setattr(tasks, "_pr_state", merged)
    note = await tasks.check_pr(tid)
    assert "Merged" in note
    assert store.get_task(tid)["status"] == "merged"
    assert tid not in tasks.open_prs()


@pytest.mark.asyncio
async def test_a_pr_closed_unmerged_says_the_work_can_be_picked_back_up(monkeypatch):
    tid = _task(status="shipped", pr_urls="repo: https://github.com/o/r/pull/7")

    async def closed(url):
        return {"state": "CLOSED"}

    monkeypatch.setattr(tasks, "_pr_state", closed)
    note = await tasks.check_pr(tid)
    assert store.get_task(tid)["status"] == "pr_closed"
    assert "pick the task back up" in note


@pytest.mark.asyncio
async def test_red_ci_on_the_pr_reaches_the_task_that_produced_it(monkeypatch):
    """The thing that was landing nowhere before."""
    tid = _task(status="shipped", pr_urls="repo: https://github.com/o/r/pull/7")

    async def red(url):
        return {"state": "OPEN",
                "statusCheckRollup": [{"conclusion": "FAILURE"}]}

    monkeypatch.setattr(tasks, "_pr_state", red)
    note = await tasks.check_pr(tid)
    assert "CI red" in note
    assert f"fix #{tid}" in note
    assert store.get_task(tid)["status"] == "pr_ci_failed"
    # Still watched — a red PR is not a finished task.
    assert tid in tasks.open_prs()


@pytest.mark.asyncio
async def test_changes_requested_reaches_the_task(monkeypatch):
    tid = _task(status="shipped", pr_urls="repo: https://github.com/o/r/pull/7")

    async def review(url):
        return {"state": "OPEN", "reviewDecision": "CHANGES_REQUESTED",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}]}

    monkeypatch.setattr(tasks, "_pr_state", review)
    note = await tasks.check_pr(tid)
    assert "Changes requested" in note
    assert store.get_task(tid)["status"] == "pr_changes_requested"


@pytest.mark.asyncio
async def test_the_same_state_twice_is_reported_once(monkeypatch):
    """A watcher that repeats itself every five minutes gets muted."""
    tid = _task(status="shipped", pr_urls="repo: https://github.com/o/r/pull/7")

    async def red(url):
        return {"state": "OPEN", "statusCheckRollup": [{"conclusion": "FAILURE"}]}

    monkeypatch.setattr(tasks, "_pr_state", red)
    assert await tasks.check_pr(tid) is not None
    assert await tasks.check_pr(tid) is None, "re-reported an unchanged PR"


@pytest.mark.asyncio
async def test_an_unreadable_pr_changes_nothing(monkeypatch):
    """gh being down must not silently mark work as merged or closed."""
    tid = _task(status="shipped", pr_urls="repo: https://github.com/o/r/pull/7")

    async def nothing(url):
        return {}

    monkeypatch.setattr(tasks, "_pr_state", nothing)
    assert await tasks.check_pr(tid) is None
    assert store.get_task(tid)["status"] == "shipped"


def test_a_run_still_in_flight_is_not_called_green():
    assert tasks._checks_verdict({"statusCheckRollup": [
        {"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS", "state": "PENDING"}]}) == "pending"


def test_all_checks_passing_is_green():
    assert tasks._checks_verdict({"statusCheckRollup": [
        {"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}]}) == "green"


def test_any_failure_is_red():
    assert tasks._checks_verdict({"statusCheckRollup": [
        {"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]}) == "red"


def test_no_checks_at_all_is_pending_not_green():
    """A PR whose workflows have not started yet has not passed anything."""
    assert tasks._checks_verdict({"statusCheckRollup": []}) == "pending"


def test_pr_urls_survive_the_repo_label_ship_adds():
    t = {"pr_urls": "telikos-booking-service: https://github.com/o/r/pull/7\n"
                    "telikos-email-service: https://github.com/o/e/pull/3"}
    assert tasks._pr_links(t) == ["https://github.com/o/r/pull/7",
                                  "https://github.com/o/e/pull/3"]


# --- the conversation keeps its link -----------------------------------------

def test_a_finished_task_stays_linked_to_its_conversation():
    """The deletion that made feedback impossible to route."""
    tid = _task(status="done")
    tasks.link_task("conv-1", tid)
    assert tasks.live_tasks_for("conv-1") == []      # not live, correctly
    assert tasks.refinable_for("conv-1") == [tid], "link was thrown away"


def test_a_rejected_task_is_forgotten():
    """Pruning still happens — for work that is actually over."""
    tid = _task(status="rejected")
    tasks.link_task("conv-1", tid)
    tasks.live_tasks_for("conv-1")
    assert tasks.refinable_for("conv-1") == []
    assert tid not in tasks._linked_ids("conv-1")


def test_feedback_days_later_is_not_treated_as_feedback():
    """At some point a similar request is genuinely new work."""
    old = time.time() - (tasks.REFINE_WINDOW_SECONDS + 60)
    tid = _task(status="done", finished=old)
    tasks.link_task("conv-1", tid)
    assert tasks.refinable_for("conv-1") == []


def test_a_shipped_task_can_still_take_feedback():
    tid = _task(status="shipped")
    tasks.link_task("conv-1", tid)
    assert tasks.refinable_for("conv-1") == [tid]


# --- feedback continues, it does not respawn ---------------------------------

@pytest.mark.asyncio
async def test_feedback_resumes_the_same_task(monkeypatch):
    tid = _task(status="done")
    resumed = {}

    async def spy(task_id, text, approved=False):
        resumed.update(task_id=task_id, text=text, approved=approved)

    monkeypatch.setattr(tasks, "_resume_worker", spy)
    out = await tasks.refine(tid, "also handle the EXECUTED status")
    await asyncio.sleep(0)      # refine schedules the worker; let it start

    assert resumed["task_id"] == tid
    assert "EXECUTED" in resumed["text"]
    assert "not a new task" in resumed["text"]
    assert "do not start over" in resumed["text"].lower()
    assert store.get_task(tid)["status"] == "running"
    assert "same session" in out


@pytest.mark.asyncio
async def test_feedback_on_shipped_work_commits_onto_the_open_branch(monkeypatch):
    """The branch is already pushed — a fresh branch would orphan the PR."""
    tid = _task(status="shipped", pr_urls="r: https://github.com/o/r/pull/7")
    seen = {}

    async def spy(task_id, text, approved=False):
        seen["text"] = text

    monkeypatch.setattr(tasks, "_resume_worker", spy)
    out = await tasks.refine(tid, "rename the flag")
    await asyncio.sleep(0)
    assert "already pushed" in seen["text"]
    assert "open PR" in out


@pytest.mark.asyncio
async def test_feedback_on_a_live_task_buffers_instead_of_restarting(monkeypatch):
    """A running task has a cheaper door — augment, delivered at its next gate."""
    tid = _task(status="running")
    called = {}

    async def must_not_resume(*a, **k):
        called["resumed"] = True

    monkeypatch.setattr(tasks, "_resume_worker", must_not_resume)
    out = await tasks.refine(tid, "also cover the amend path")
    assert "resumed" not in called
    assert store.get_task(tid)["status"] == "running"
    assert "amend path" in (store.kv_get(f"task_addenda:{tid}") or "")


@pytest.mark.asyncio
async def test_a_merged_task_cannot_be_reopened_by_feedback():
    tid = _task(status="merged")
    with pytest.raises(ValueError, match="cannot be continued"):
        await tasks.refine(tid, "one more thing")


@pytest.mark.asyncio
async def test_a_failed_task_can_be_continued(monkeypatch):
    """Failure is the case where the accumulated context is worth the most."""
    tid = _task(status="failed")

    async def spy(*a, **k):
        pass

    monkeypatch.setattr(tasks, "_resume_worker", spy)
    await tasks.refine(tid, "the import path was wrong")
    assert store.get_task(tid)["status"] == "running"


# --- the guard that stops a respawn ------------------------------------------

def test_a_restated_title_is_recognised_as_feedback():
    tid = _task(status="done", title="Skip vessel schedule updates for cancelled bookings")
    m = tasks.refinable_match("Skip vessel schedule updates for cancelled bookings",
                              "also handle EXECUTED")
    assert m and m["id"] == tid


def test_genuinely_new_work_is_not_swallowed():
    _task(status="done", title="Skip vessel schedule updates for cancelled bookings")
    assert tasks.refinable_match(
        "Add chassis type to the transport order payload",
        "map chassisServiceType through the mapper") is None


def test_a_vague_request_never_matches():
    """Two words of overlap is not evidence, and a false match blocks real work."""
    _task(status="done", title="Skip vessel schedule updates for cancelled bookings")
    assert tasks.refinable_match("fix it", "please") is None


def test_a_match_in_another_workspace_is_not_a_match():
    _task(status="done", title="Skip vessel schedule updates for cancelled bookings",
          workspace="booking-workspace")
    assert tasks.refinable_match("Skip vessel schedule updates for cancelled bookings",
                                 "again", workspace="iom-workspace") is None


def test_an_old_task_does_not_block_new_work():
    _task(status="done", title="Skip vessel schedule updates for cancelled bookings",
          finished=time.time() - (tasks.REFINE_WINDOW_SECONDS + 60))
    assert tasks.refinable_match("Skip vessel schedule updates for cancelled bookings",
                                 "do it again") is None


# --- the endpoints ------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setenv("ASTA_TOKEN", "qa-token")
    return TestClient(main.app)


def test_the_pr_listing_is_not_swallowed_by_the_task_id_route(client):
    """FastAPI matches in declaration order, so /api/tasks/prs must come first.

    Declared after /api/tasks/{task_id} it returned 422 — the router tried to
    parse "prs" as an integer id.
    """
    r = client.get("/api/tasks/prs", headers={"Authorization": "Bearer qa-token"})
    assert r.status_code == 200, r.text
    assert "open" in r.json()


def test_fetching_one_task_by_id_still_works(client):
    tid = _task(status="shipped")
    r = client.get(f"/api/tasks/{tid}", headers={"Authorization": "Bearer qa-token"})
    assert r.status_code == 200
    assert r.json()["id"] == tid


def test_refine_needs_text(client):
    tid = _task(status="done")
    r = client.post(f"/api/tasks/{tid}/refine", json={},
                    headers={"Authorization": "Bearer qa-token"})
    assert r.status_code == 400


def test_refining_a_merged_task_is_refused_over_http(client):
    tid = _task(status="merged")
    r = client.post(f"/api/tasks/{tid}/refine", json={"text": "more"},
                    headers={"Authorization": "Bearer qa-token"})
    assert r.status_code == 400
    assert "cannot be continued" in r.text


def test_a_long_prompt_cannot_dilute_its_way_past_the_guard():
    """Scored against the OLD task's terms, so padding does not help."""
    tid = _task(status="done", title="Skip vessel schedule updates for cancelled bookings")
    m = tasks.refinable_match(
        "Skip vessel schedule updates for cancelled bookings",
        "also handle EXECUTED " + " ".join(f"unrelated{i}" for i in range(200)))
    assert m and m["id"] == tid
