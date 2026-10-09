"""Task #272, 9 Oct, watched end to end — every way it went wrong.

A stray ~/Projects clone named `new` became its checkout; the phone showed the plan
from point 5; a five-file plan was sized as "1 file" and approved by itself — past
his "share the plan to Vinish, get an approval and implement" and the plan's own
questions; his correction mid-build was queued until after the code was written,
then dropped when the task was marked done; and "reference sent to AP" was
"corrected" as a false send.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from app import go, main, routing, store, tasks, worktrees

PLAN = """## Plan — container + order-level customs status consolidation

**Files to touch** (telikos-booking-service):
1. `domain/models/enums/WorkProcessStatusEnum.java` — add CUSTOMS_CLEARED, CUSTOMS_NOT_RELEASED.
2. `common/BookingConstants.java` — add the string constants.
3. `domain/common/DomainHelperUtils.java` — rewrite validateAndUpdateCustomsReference.
4. `domain/inland/service/api/CustomsStatusUpdateDomainService.java` — call the new method.
5. Tests: add cases to `DomainHelperUtilsTest.java` and `CustomsStatusUpdateDomainServiceTest.java`.

On "share the plan to Vinish": I'll implement as soon as you confirm the plan above."""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", f"https://github.com/acme/{origin}.git")
    return path


# --- 1. the checkout: by the GitHub repo, never a clone of a workspace service ------

@pytest.fixture
def projects(tmp_path, monkeypatch):
    ws, proj = tmp_path / "booking-workspace", tmp_path / "Projects"
    _repo(ws / "telikos-booking-service", "telikos-booking-service")
    _repo(proj / "new", "telikos-booking-service")            # a stray clone
    _repo(proj / "event-router-library", "telikos-event-router-library")
    _repo(proj / "telikos-event-router-library", "telikos-event-router-library")
    monkeypatch.setattr(worktrees, "REPO_DIRS", str(proj))
    return ws, proj


def test_a_word_in_his_ask_never_picks_a_stray_clone(projects):
    ws, _ = projects
    inside = worktrees.repos_in(ws)
    assert worktrees.outside("prepare for the new customs new status changes", inside=inside) == []


def test_a_library_named_by_its_repo_is_found_and_its_own_folder_preferred(projects):
    ws, proj = projects
    got = worktrees.outside("telikos-event-router-library isn't checked out",
                            inside=worktrees.repos_in(ws))
    assert got == [proj / "telikos-event-router-library"]


def test_a_clone_of_a_workspace_service_is_never_used(projects):
    ws, _ = projects
    assert worktrees.outside("fix telikos-booking-service", inside=worktrees.repos_in(ws)) == []


# --- 2. the phone shows a plan from the top ------------------------------------------

def test_the_plan_on_the_phone_starts_with_what_changes():
    shown = tasks._phone_text(PLAN * 3, 700, head_first=True)
    assert "WorkProcessStatusEnum.java" in shown and shown.index("Files to touch") < 120
    assert "(the rest is in the app)" in shown


# --- 3. the size is the files it names, not their extensions -------------------------

def test_a_five_file_plan_is_not_one_file():
    tier, why = routing.plan_tier(PLAN, {"repos": ["telikos-booking-service"]})
    assert tier >= 2 and why.startswith(("6", "5", "7")), why


# --- 4. nothing goes ahead past an approval he asked for, or the plan's questions ----

def _task(prompt="prepare for the new customs status changes"):
    return store.create_task("Prepare for new customs status", "code", prompt, "booking")


def test_his_ask_for_vinishs_approval_holds_the_plan(monkeypatch):
    monkeypatch.setattr(go, "APPROVAL_FROM_TIER", 2)
    t = _task()
    go.grant(t["id"], "do it", ship=False)                    # even with his go
    tasks.record_answer(t["id"], t, "in task 272, consolidate at order level as well. "
                        "post that share the plan to vinish get an approval and implement")
    assert go.no_ask(t["id"], "STRUCTURE\n  A.java  one line") == ""


def test_a_plan_with_its_own_open_question_waits(monkeypatch):
    monkeypatch.setattr(go, "APPROVAL_FROM_TIER", 4)          # even if nothing waited
    t = _task()
    assert go.no_ask(t["id"], PLAN) == ""


def test_a_small_plain_plan_still_goes_ahead(monkeypatch):
    monkeypatch.setattr(go, "APPROVAL_FROM_TIER", 2)          # the live setting
    t = _task("rename the logger field")
    assert go.no_ask(t["id"], "STRUCTURE\n  A.java  rename the field").startswith("small change")


def test_approving_a_plan_closes_its_gate():
    t = _task()
    store.kv_set(f"task_gate:{t['id']}", "plan")
    tasks.record_answer(t["id"], t, "PLAN APPROVED")
    assert not store.kv_get(f"task_gate:{t['id']}")


# --- 5. a correction mid-build re-plans; a queued note is never dropped ---------------

def test_saying_the_approved_design_is_wrong_stops_the_build_and_replans(monkeypatch):
    t = _task()
    store.update_task(t["id"], status="running")
    tasks.mark_approved(t["id"])
    proposed = []

    async def propose(tid, text):
        proposed.append(text)
        return "planning"

    async def no_job(tid, status="cancelled", why=""):
        return False
    monkeypatch.setattr(tasks, "propose_change", propose)
    monkeypatch.setattr(tasks, "cancel", no_job)
    out = asyncio.run(tasks.note_for_live_task(
        t["id"], "hey incorrect task 272 - update the workprocess and send to IOM, "
                 "not true or false"))
    assert "stopped the build" in out
    assert proposed and "send to IOM" in proposed[0]
    assert not tasks.note_waiting(t["id"])


def test_an_ordinary_note_to_a_running_task_is_kept_for_its_next_step():
    t = _task()
    store.update_task(t["id"], status="running")
    tasks.mark_approved(t["id"])
    out = asyncio.run(tasks.note_for_live_task(t["id"], "also add a debug log line",
                                               code_change=True))
    assert "noted" in out and "also add a debug log" in tasks.note_waiting(t["id"])


def test_a_task_never_finishes_with_his_note_unapplied(monkeypatch):
    t = _task()
    store.update_task(t["id"], status="running")
    tasks.augment(t["id"], "incorrect — update the workprocess, not true or false",
                  code_change=True)
    pushed, proposed = [], []

    async def notify(text, *a, **k):
        pushed.append(text)

    async def propose(tid, text):
        proposed.append(text)
    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    monkeypatch.setattr(tasks, "propose_change", propose)

    async def run():
        await tasks.complete(t["id"], t, "Implemented. Tests: 99 passed.")
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert pushed and "NOT ready" in pushed[0] and "Local implementation ready" not in pushed[0]
    assert proposed and "not true or false" in proposed[0]


# --- 6. a description is not a claim of a send ---------------------------------------

def test_reference_sent_to_ap_is_not_a_false_send():
    reply = ("Correction folded into task #272 — `isCustomsCleared` true/false/absent is "
             "derived only as the downstream reference sent to AP.")
    assert main.unproven_send(reply, time.time()) == ""
    assert main.unproven_send("Sent to Vinish: the plan, for his approval.", time.time())
