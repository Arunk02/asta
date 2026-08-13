"""The rules Arun gave for how coding work is actually done.

Given 2026-08-12, after a task shipped a change he had not seen planned:

  - always get plan approval before coding, so he corrects the reading upfront
    rather than the diff afterwards;
  - always start from develop, branch named after the ticket;
  - keep answering the PR — CI and review comments — until it merges;
  - his CI only, never a build somebody else triggered;
  - code that reads like an experienced engineer wrote it: functional, named,
    simplified, debuggable.

Each of those is tested here against the thing that would otherwise go wrong.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import repo_ops, store, tasks


# --- the plan gate is unconditional ------------------------------------------

def test_the_small_change_escape_hatch_is_gone():
    """It used to auto-proceed under 2 files / 30 lines. He removed that."""
    assert "AUTO-PROCEED" not in tasks.CODE_OVERRIDES


def test_the_gate_is_stated_as_unconditional():
    body = tasks.CODE_OVERRIDES
    assert "PLAN GATE IS UNCONDITIONAL" in body
    assert "one-line constant change included" in body


def test_the_plan_marks_still_pause_a_run():
    """The gate only works if a finished leg carrying it is recognised."""
    assert "PLAN APPROVED" in tasks._GATE_MARKS


@pytest.mark.asyncio
async def test_a_plan_leg_stops_and_asks_rather_than_implementing(monkeypatch):
    tid = store.create_task("BEPTELIKOS-1: thing", "code", "do it", None)["id"]
    sent = {}

    async def spy(text, level="info", urgency="direct", priority=None):
        sent["text"] = text

    from app import notify
    monkeypatch.setattr(notify, "notify", spy)

    await tasks._finish_code(tid, store.get_task(tid),
                             "here is the plan\n\nPLAN APPROVED to continue?", hops=0)

    assert store.get_task(tid)["status"] == "awaiting_approval"
    assert "PLAN" in sent["text"]


# --- branch discipline --------------------------------------------------------

def test_a_ticket_id_becomes_the_branch_name():
    t = {"title": "BEPTELIKOS-10159: skip vessel schedule for cancelled",
         "prompt": "do it"}
    assert tasks.task_branch(t, 64) == "feature/BEPTELIKOS-10159"


def test_a_ticket_id_in_the_prompt_is_found_too():
    """He pastes the key into either field."""
    t = {"title": "skip vessel schedule", "prompt": "see BEPTELIKOS-10159 for detail"}
    assert tasks.task_branch(t, 64) == "feature/BEPTELIKOS-10159"


def test_without_a_ticket_a_generic_name_is_used():
    got = tasks.task_branch({"title": "tidy the mapper", "prompt": "x"}, 65)
    assert got.startswith("feature/asta-65-")
    assert "tidy-the-mapper" in got


def test_a_messy_title_still_yields_a_valid_branch():
    got = tasks.task_branch({"title": "Fix: the ETA!! (import/vts) — urgent??",
                             "prompt": ""}, 7)
    assert " " not in got and "!" not in got and "?" not in got
    assert got.startswith("feature/")


def test_develop_is_preferred_over_main():
    assert repo_ops.BASE_PREFERENCE[0] == "develop"


def test_the_base_branches_are_still_protected():
    """Nothing may commit straight onto these."""
    for b in ("main", "master", "develop"):
        assert b in repo_ops.BASE_BRANCHES


class FakeGit:
    """A git that records what it was asked to do, and can be told what exists."""

    def __init__(self, has=("develop",), dirty=False, fail=()):
        self.has, self.dirty, self.fail = set(has), dirty, set(fail)
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, cwd, *args, timeout=120):
        self.calls.append(args)
        cmd = " ".join(args)
        for bad in self.fail:
            if bad in cmd:
                return 1, f"fatal: {bad} failed"
        if args[:2] == ("git", "status"):
            return 0, ("M file.java\n" if self.dirty else "")
        if args[:2] == ("git", "rev-parse"):
            branch = args[-1].replace("origin/", "")
            return (0, branch) if branch in self.has else (1, "unknown revision")
        return 0, ""

    def ran(self, *needle) -> bool:
        return any(args[:len(needle)] == needle for args in self.calls)


@pytest.mark.asyncio
async def test_work_starts_from_a_freshly_pulled_develop(monkeypatch, tmp_path):
    git = FakeGit(has=("develop", "main"))
    monkeypatch.setattr(repo_ops, "git", git)

    out = await repo_ops.start_branch(tmp_path, "feature/BEPTELIKOS-1")

    assert out["ok"] and out["base"] == "develop"
    assert git.ran("git", "fetch", "origin")
    assert git.ran("git", "checkout", "develop")
    assert git.ran("git", "pull", "--ff-only", "origin", "develop")
    assert git.ran("git", "checkout", "-b", "feature/BEPTELIKOS-1")


@pytest.mark.asyncio
async def test_the_pull_never_invents_a_merge_commit(monkeypatch, tmp_path):
    """--ff-only: a merge commit appearing in his history unasked is a surprise."""
    git = FakeGit()
    monkeypatch.setattr(repo_ops, "git", git)
    await repo_ops.start_branch(tmp_path, "feature/x")
    pulls = [c for c in git.calls if c[:2] == ("git", "pull")]
    assert pulls and all("--ff-only" in c for c in pulls)


@pytest.mark.asyncio
async def test_a_repo_without_develop_falls_back_and_says_so(monkeypatch, tmp_path):
    git = FakeGit(has=("main",))
    monkeypatch.setattr(repo_ops, "git", git)

    out = await repo_ops.start_branch(tmp_path, "feature/x")
    assert out["ok"] and out["base"] == "main"
    assert "no develop" in out["note"]


@pytest.mark.asyncio
async def test_a_repo_with_no_known_base_is_reported_not_guessed(monkeypatch, tmp_path):
    git = FakeGit(has=())
    monkeypatch.setattr(repo_ops, "git", git)

    out = await repo_ops.start_branch(tmp_path, "feature/x")
    assert not out["ok"]
    assert "no develop/main/master" in out["note"]
    assert not git.ran("git", "checkout", "-b", "feature/x")


@pytest.mark.asyncio
async def test_an_existing_branch_is_continued_not_duplicated(monkeypatch, tmp_path):
    """Re-running a task and a second repo hop both land here legitimately."""
    git = FakeGit(fail=("checkout -b",))
    monkeypatch.setattr(repo_ops, "git", git)

    out = await repo_ops.start_branch(tmp_path, "feature/x")
    assert out["ok"]
    assert "already existed" in out["note"]
    assert git.ran("git", "checkout", "feature/x")


@pytest.mark.asyncio
async def test_uncommitted_work_is_flagged_rather_than_hidden(monkeypatch, tmp_path):
    git = FakeGit(dirty=True)
    monkeypatch.setattr(repo_ops, "git", git)
    out = await repo_ops.start_branch(tmp_path, "feature/x")
    assert out["dirty"] is True


@pytest.mark.asyncio
async def test_no_network_still_lets_the_work_start(monkeypatch, tmp_path):
    """A fetch that fails offline must not stop him working."""
    git = FakeGit(fail=("fetch",))
    monkeypatch.setattr(repo_ops, "git", git)
    out = await repo_ops.start_branch(tmp_path, "feature/x")
    assert out["ok"]
    assert "local copy" in out["note"]


@pytest.mark.asyncio
async def test_a_failed_checkout_is_reported_not_raised(monkeypatch, tmp_path):
    git = FakeGit(fail=("checkout develop",))
    monkeypatch.setattr(repo_ops, "git", git)
    out = await repo_ops.start_branch(tmp_path, "feature/x")
    assert not out["ok"]
    assert "could not check out" in out["note"]


@pytest.mark.asyncio
async def test_every_repo_in_a_workspace_gets_the_same_branch(monkeypatch, tmp_path):
    for name in ("booking", "email", "activityplan"):
        (tmp_path / name / ".git").mkdir(parents=True)

    prepared = []

    async def spy(repo, branch):
        prepared.append((repo.name, branch))
        return {"repo": repo.name, "branch": branch, "ok": True, "note": "", "dirty": False}

    monkeypatch.setattr(repo_ops, "start_branch", spy)
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))

    t = {"title": "BEPTELIKOS-9: fix", "prompt": "", "workspace": "booking-workspace"}
    await tasks._prepare_branches(70, t)

    assert sorted(prepared) == [("activityplan", "feature/BEPTELIKOS-9"),
                                ("booking", "feature/BEPTELIKOS-9"),
                                ("email", "feature/BEPTELIKOS-9")]


@pytest.mark.asyncio
async def test_one_broken_repo_does_not_stop_the_others(monkeypatch, tmp_path):
    for name in ("good", "broken"):
        (tmp_path / name / ".git").mkdir(parents=True)

    async def flaky(repo, branch):
        if repo.name == "broken":
            raise RuntimeError("index.lock exists")
        return {"repo": repo.name, "branch": branch, "ok": True, "note": "", "dirty": False}

    monkeypatch.setattr(repo_ops, "start_branch", flaky)
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))

    sent = {}

    async def spy(text, level="info", urgency="direct", priority=None):
        sent["text"] = text

    from app import notify
    monkeypatch.setattr(notify, "notify", spy)

    out = await tasks._prepare_branches(71, {"title": "x", "prompt": "", "workspace": "w"})
    assert [r["ok"] for r in out].count(True) == 1
    assert "broken" in sent["text"]
    assert "index.lock" in sent["text"]


@pytest.mark.asyncio
async def test_a_clean_preparation_is_silent(monkeypatch, tmp_path):
    """He does not want a ping for the normal case."""
    (tmp_path / ".git").mkdir(parents=True)

    async def fine(repo, branch):
        return {"repo": repo.name, "branch": branch, "ok": True, "note": "", "dirty": False}

    monkeypatch.setattr(repo_ops, "start_branch", fine)
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))

    spoke = []

    async def spy(text, level="info", urgency="direct", priority=None):
        spoke.append(text)

    from app import notify
    monkeypatch.setattr(notify, "notify", spy)
    await tasks._prepare_branches(72, {"title": "x", "prompt": "", "workspace": "w"})
    assert spoke == []


@pytest.mark.asyncio
async def test_a_missing_workspace_directory_is_survivable(monkeypatch):
    monkeypatch.setattr(tasks, "_cwd", lambda ws: "/nope/does/not/exist")
    assert await tasks._prepare_branches(73, {"title": "x", "prompt": "", "workspace": "w"}) == []


# --- answering the PR until it merges -----------------------------------------

def _shipped(**kw):
    t = store.create_task(kw.pop("title", "BEPTELIKOS-1: thing"), "code", "p", None)
    store.update_task(t["id"], status="shipped", finished_at=time.time(),
                      pr_urls="r: https://github.com/o/r/pull/7", **kw)
    return t["id"]


def test_a_human_review_comment_is_carried_through():
    pr = {"reviews": [{"author": {"login": "vinish"}, "state": "CHANGES_REQUESTED",
                       "body": "handle the EXECUTED status too"}]}
    assert "handle the EXECUTED status too" in " ".join(tasks._review_notes(pr))


def test_bot_review_noise_is_dropped():
    """Sonar and friends comment on every PR; they are not asking him for anything."""
    pr = {"reviews": [{"author": {"login": "sonarqubecloud"}, "body": "Quality Gate passed"},
                      {"author": {"login": "github-actions[bot]"}, "body": "coverage 81%"}]}
    assert tasks._review_notes(pr) == []


def test_a_bare_approval_is_not_treated_as_an_ask():
    pr = {"reviews": [{"author": {"login": "vinish"}, "state": "APPROVED", "body": "lgtm"}]}
    assert tasks._review_notes(pr) == []


def test_an_approval_with_real_content_is_kept():
    body = "approving, but please rename the flag before you merge it"
    pr = {"reviews": [{"author": {"login": "vinish"}, "state": "APPROVED", "body": body}]}
    assert tasks._review_notes(pr)


def test_his_own_comments_are_not_asks_of_himself():
    pr = {"comments": [{"author": {"login": "Arunk540"}, "body": "raised it, please check"}]}
    assert tasks._review_notes(pr, me="Arunk540") == []


def test_an_empty_comment_body_is_ignored():
    assert tasks._review_notes({"comments": [{"author": {"login": "v"}, "body": "  "}]}) == []


def test_a_comment_is_reported_once_not_every_poll():
    tid = _shipped()
    notes = ["vinish: rename the flag"]
    assert tasks._new_review_notes(tid, notes) == notes
    assert tasks._new_review_notes(tid, notes) == []


def test_a_second_comment_is_still_reported():
    tid = _shipped()
    tasks._new_review_notes(tid, ["vinish: rename the flag"])
    fresh = tasks._new_review_notes(tid, ["vinish: rename the flag", "sumith: add a test"])
    assert fresh == ["sumith: add a test"]


@pytest.mark.asyncio
async def test_changes_requested_says_what_was_actually_requested(monkeypatch):
    tid = _shipped()

    async def pr(url):
        return {"state": "OPEN", "reviewDecision": "CHANGES_REQUESTED",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "reviews": [{"author": {"login": "vinish"},
                             "state": "CHANGES_REQUESTED",
                             "body": "handle the EXECUTED status too"}]}

    monkeypatch.setattr(tasks, "_pr_state", pr)
    note = await tasks.check_pr(tid)
    assert "handle the EXECUTED status too" in note
    assert f"fix #{tid}" in note


@pytest.mark.asyncio
async def test_a_plain_comment_is_noticed_even_though_no_field_changed(monkeypatch):
    """A comment moves neither the checks nor the review decision."""
    tid = _shipped()
    body = "can you also cover the import leg"

    async def pr(url):
        return {"state": "OPEN", "reviewDecision": "",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "comments": [{"author": {"login": "vinish"}, "body": body}]}

    monkeypatch.setattr(tasks, "_pr_state", pr)
    await tasks.check_pr(tid)          # first poll settles the state
    note = await tasks.check_pr(tid)   # nothing changed except the comment
    assert note and body in note


@pytest.mark.asyncio
async def test_the_same_comment_does_not_nag_forever(monkeypatch):
    tid = _shipped()

    async def pr(url):
        return {"state": "OPEN", "reviewDecision": "",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "comments": [{"author": {"login": "vinish"}, "body": "one thing"}]}

    monkeypatch.setattr(tasks, "_pr_state", pr)
    for _ in range(3):
        await tasks.check_pr(tid)
    assert await tasks.check_pr(tid) is None


@pytest.mark.asyncio
async def test_the_task_stays_open_through_all_of_it(monkeypatch):
    """Which is the point — it is not done until the PR is."""
    tid = _shipped()

    async def pr(url):
        return {"state": "OPEN", "reviewDecision": "CHANGES_REQUESTED",
                "statusCheckRollup": [{"conclusion": "FAILURE"}]}

    monkeypatch.setattr(tasks, "_pr_state", pr)
    await tasks.check_pr(tid)
    assert tid in tasks.open_prs()
    assert store.get_task(tid)["status"] not in tasks.CLOSED_STATUSES


# --- his CI only --------------------------------------------------------------

def test_ci_watching_is_scoped_to_his_own_runs():
    """"dont monitor CI which is not triggered by me" — enforced by --user."""
    src = Path("app/ci_watch.py").read_text()
    assert '"--user", me' in src


def test_pr_watching_only_follows_prs_from_his_own_tasks():
    """The PR watcher reads tasks he shipped, never a repo-wide feed."""
    _shipped()
    other = store.create_task("someone else's work", "code", "p", None)["id"]
    store.update_task(other, status="done")
    assert other not in tasks.open_prs()


# --- the quality bar ----------------------------------------------------------

@pytest.mark.parametrize("rule", [
    "Small, named, single-purpose functions",
    "functional shape",
    "SIMPLIFY",
    "Guard clauses over nesting",
    "Comments explain WHY",
    "Delete what you replace",
    "Tests are part of the change",
])
def test_the_quality_bar_is_stated_to_the_executor(rule):
    assert rule in tasks.CODE_OVERRIDES


def test_the_executor_is_told_not_to_touch_the_branch():
    """Asta already cut it; a second branch would orphan the work."""
    body = tasks.CODE_OVERRIDES
    assert "Do NOT create another branch" in body


def test_the_no_attribution_rule_survived_all_of_this():
    assert "Co-Authored-By" in tasks.CODE_OVERRIDES
    assert "Co-Authored-By" in repo_ops.NO_ATTRIBUTION


# --- the repo Asta is running from is never branched -------------------------
#
# Found the hard way. A code task with no workspace resolved to ROOT — Asta's own
# checkout — so _prepare_branches ran real git on it: fetch, checkout main,
# pull --ff-only, checkout -b feature/asta-1-fix-bug. The running process moved
# the branch it was executing from, with unpushed work on the branch it left, and
# it did the same thing again on every test run that spawned such a task.

@pytest.mark.asyncio
async def test_a_task_with_no_workspace_branches_nothing(monkeypatch):
    """No workspace means no repo of its own — not 'use Asta's'."""
    called = []

    async def spy(repo, branch):
        called.append(repo)
        return {"repo": repo.name, "branch": branch, "ok": True, "note": "", "dirty": False}

    monkeypatch.setattr(repo_ops, "start_branch", spy)
    out = await tasks._prepare_branches(90, {"title": "fix bug", "prompt": "",
                                             "workspace": None})
    assert out == []
    assert called == [], f"ran git in {called}"


@pytest.mark.asyncio
async def test_astas_own_repo_is_never_branched_even_if_named(monkeypatch):
    """Belt and braces: a workspace that resolves to ROOT is still refused."""
    called = []

    async def spy(repo, branch):
        called.append(repo)
        return {"repo": repo.name, "branch": branch, "ok": True, "note": "", "dirty": False}

    monkeypatch.setattr(repo_ops, "start_branch", spy)
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tasks.ROOT))

    out = await tasks._prepare_branches(91, {"title": "x", "prompt": "",
                                             "workspace": "asta"})
    assert out == []
    assert called == []


def test_the_suite_cannot_move_this_repos_branch():
    """A test run must leave HEAD exactly where it found it.

    The guard above is what makes this true; without it, spawning a code task
    anywhere in the suite checked out a new branch in the working copy the tests
    were being read from.
    """
    import subprocess
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                          cwd=tasks.ROOT, capture_output=True, text=True).stdout.strip()
    assert head != "", "could not read HEAD"
    assert not head.startswith("feature/asta-"), (
        f"the suite branched this repo to {head!r} — _prepare_branches escaped its guard")
