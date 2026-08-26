"""A stopped turn must say WHICH of three things happened.

`RuntimeError: Copilot CLI turn timed out after 300s` is true and answers none of
the questions Arun actually had: did it finish, is it still going, is it stuck.
These tests hold the line on the distinction, and on not throwing away the work.
"""

from __future__ import annotations

import asyncio

import pytest

from app import turn_budget as tb

# The DEFAULT context directory, not the one Arun's .env names. Hardcoding
# ".contmark" made these pass on his laptop and fail on the first CI run.
from app.workspace.providers.indexed import DEFAULT_CONTEXT_DIR as _CTX



class _Stream:
    """A stdout that yields planned chunks, with planned gaps between them."""

    def __init__(self, plan):
        self._plan = list(plan)          # (delay_seconds, bytes) — b"" means EOF

    async def read(self, _n):
        if not self._plan:
            await asyncio.sleep(3600)    # silence for ever
        delay, data = self._plan.pop(0)
        await asyncio.sleep(delay)
        return data


def _drain(plan, total, idle):
    return asyncio.run(tb.drain(_Stream(plan), None, total=total, idle=idle))


def test_a_finished_turn_is_done():
    stop = _drain([(0.01, b"hello "), (0.01, b"world"), (0.01, b"")], total=5, idle=1)
    assert stop.reason == "done"
    assert stop.ok
    assert stop.partial == "hello world"


def test_a_silent_brain_is_stuck_not_slow():
    """No output for the idle window means more time would not help."""
    stop = _drain([(0.01, b"starting")], total=30, idle=0.3)
    assert stop.reason == "idle"
    assert not stop.ok
    assert stop.silent_for >= 0.3
    assert "stuck" in stop.why()
    assert "more time would not have helped" in stop.why().lower()


def test_a_streaming_brain_that_runs_out_is_not_stuck():
    """The opposite case, and it must read differently.

    A brain producing output right up to the ceiling is a long job. Killing it
    with the same sentence as a wedged one is what made "is it working?"
    unanswerable.
    """
    plan = [(0.02, b"step ") for _ in range(200)]
    stop = _drain(plan, total=0.3, idle=5)
    assert stop.reason == "ceiling"
    assert not stop.ok
    assert "still working" in stop.why()
    assert "resum" in stop.why().lower(), "it must say resuming beats retrying"


def test_the_partial_work_is_never_thrown_away():
    """The defect this module exists for.

    Both drivers accumulated every chunk and the timeout branch discarded all of
    it to raise a one-line error. The evidence of what happened existed, in
    memory, and the error path deleted it.
    """
    stop = _drain([(0.01, b"edited Foo.java"), (0.01, b" and Bar.java")],
                  total=30, idle=0.3)
    assert stop.reason == "idle"
    assert "Foo.java" in stop.partial and "Bar.java" in stop.partial
    err = tb.TurnStopped(stop)
    assert "Foo.java" in str(err), "the report must carry what it got through"
    assert err.partial == stop.partial


def test_the_three_reasons_read_differently():
    """Three outcomes, three sentences — or the split bought nothing."""
    said = {r: tb.Stop(r, 300.0, 150.0).why()
            for r in ("idle", "ceiling", "done")}
    assert len({said["idle"], said["ceiling"], said["done"]}) == 3


# --- the heartbeat variant, for the driver that parses events ----------------

def test_heartbeat_guard_separates_wedged_from_busy():
    """claude_cli parses NDJSON, so it reports liveness instead of raw bytes.

    Same policy, one module — the two drivers had byte-identical pump loops and
    drifted anyway.
    """
    async def wedged(beat):
        beat.beat()
        await asyncio.sleep(30)

    async def busy(beat):
        for _ in range(200):
            beat.beat()
            await asyncio.sleep(0.01)

    async def finishes(beat):
        beat.beat()

    async def run(fn, total, idle):
        beat = tb.Heartbeat()
        return await tb.guard(fn(beat), beat, total=total, idle=idle)

    assert asyncio.run(run(wedged, 30, 0.3)).reason == "idle"
    assert asyncio.run(run(busy, 0.3, 5)).reason == "ceiling"
    assert asyncio.run(run(finishes, 5, 1)).reason == "done"


def test_guard_cancels_the_pump_it_abandons():
    """Nothing may keep running behind an answer Arun has already been given.

    A brain left alive after the turn was reported keeps editing files and
    burning quota against a question that has already been answered — and the
    next turn then finds a repo that changed under it.

    The test waits INSIDE the same event loop after the guard returns. An earlier
    version returned from `asyncio.run` immediately, which tore the loop down and
    killed the stray task regardless — so it passed whether or not the cancel
    happened, and the mutation that removed the cancel survived it.
    """
    ran = {"after_stop": False}

    async def keeps_going(beat):
        beat.beat()
        await asyncio.sleep(0.4)
        ran["after_stop"] = True         # only reached if it was NOT cancelled

    async def run():
        beat = tb.Heartbeat()
        stop = await tb.guard(keeps_going(beat), beat, total=30, idle=0.1)
        # Well past the pump's own finish time, in the same loop.
        await asyncio.sleep(0.6)
        return stop

    stop = asyncio.run(run())
    assert stop.reason == "idle"
    assert not ran["after_stop"], "the abandoned pump ran on after the turn was reported"


def test_a_real_exception_is_not_swallowed_as_done():
    """A pump that raises must surface, not be reported as a finished turn."""
    async def explodes(beat):
        beat.beat()
        raise ValueError("the brain died")

    async def run():
        beat = tb.Heartbeat()
        return await tb.guard(explodes(beat), beat, total=5, idle=1)

    with pytest.raises(ValueError, match="the brain died"):
        asyncio.run(run())


def test_the_code_ceiling_clears_the_measured_p90():
    """A ceiling below p90 kills work that was going to succeed.

    Measured baseline, n=46: median 7.7 min, p90 32 min. The ceiling was 30 min,
    so roughly the slowest tenth of code tasks died to their own budget and were
    re-run from nothing — paying the whole cost twice.
    """
    from app import tasks
    assert tasks.TASK_TIMEOUT["code"] / 60 > 32, \
        "the code ceiling is at or below the measured p90 of 32 min"


# --- what a workspace's repos actually are -----------------------------------

def _make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)


def test_a_workspace_that_is_itself_a_repo_still_finds_the_code_repos(tmp_path):
    """The booking workspace shape, which was silently mis-read.

    `~/booking-workspace` is a git repo of its own — Arunk540/booking-workspace,
    tracking 234 generated files under {_CTX}/ — with the three service repos
    inside it as ordinary directories. The old rule returned `[root]` the moment
    the root had a .git, so it reported ONE repo and that repo was the context
    repo, not the code.

    Nothing failed loudly, because a wrong repo still exists and still answers
    git commands: worktrees isolated the wrong tree, rollback looked in the wrong
    tree, and the scope-based budget sized a three-repo job as a one-repo job.
    """
    from app import worktrees

    root = tmp_path / "booking-workspace"
    _make_repo(root)                       # the workspace is itself a repo
    for name in ("telikos-booking-service", "telikos-email-service",
                 "telikos-activityplanworkflow-service"):
        _make_repo(root / name)

    code = [p.name for p in worktrees.repos_in(root)]
    assert code == sorted(["telikos-booking-service", "telikos-email-service",
                           "telikos-activityplanworkflow-service"]), \
        f"code repos should be the services, got {code}"
    assert "booking-workspace" not in code, \
        "a worktree of the generated-context repo isolates the wrong thing"


def test_rollback_still_covers_the_workspace_repo_itself(tmp_path):
    """Rollback wants the superset — a change written to the root needs undoing."""
    from app import worktrees

    root = tmp_path / "booking-workspace"
    _make_repo(root)
    _make_repo(root / "telikos-booking-service")

    every = [p.name for p in worktrees.all_repos_in(root)]
    assert "booking-workspace" in every
    assert "telikos-booking-service" in every


def test_a_plain_single_repo_workspace_is_unchanged(tmp_path):
    """The case the original shortcut was written for must keep working."""
    from app import worktrees

    root = tmp_path / "just-one-repo"
    _make_repo(root)
    assert [p.name for p in worktrees.repos_in(root)] == ["just-one-repo"]
    assert [p.name for p in worktrees.all_repos_in(root)] == ["just-one-repo"]


def test_rollback_and_worktrees_read_the_same_definition(tmp_path):
    """Two copies of one rule is how the bug survived in both places."""
    from app import tasks, worktrees

    root = tmp_path / "ws"
    _make_repo(root)
    _make_repo(root / "svc-a")
    _make_repo(root / "svc-b")
    assert tasks._repos_under(root) == worktrees.all_repos_in(root)


def test_the_code_budget_scales_with_how_many_repos_are_in_reach(monkeypatch, tmp_path):
    """Arun's point: a small change across three repos is not a one-repo job.

    Verification alone is per-repo — `mvn clean test` on each — so a task that can
    touch three pays it three times. A flat number is wrong in both directions.
    """
    from app import tasks, workspace as workspace_mod

    one = tmp_path / "one"
    _make_repo(one)
    three = tmp_path / "three"
    _make_repo(three)
    for n in ("a", "b", "c"):
        _make_repo(three / n)

    monkeypatch.setattr(workspace_mod, "WORKSPACES",
                        {"one": one, "three": three}, raising=False)

    small = tasks.code_timeout("one")
    big = tasks.code_timeout("three")
    assert big > small, "three repos got the same budget as one"
    assert small / 60 > 32, "even a one-repo budget must clear the measured p90 of 32 min"
    assert big <= tasks.CODE_MAX_SECONDS, "the budget must stay capped"


def test_the_budget_is_capped(monkeypatch, tmp_path):
    """Past a point a run that long is a task that should have been split."""
    from app import tasks, workspace as workspace_mod

    many = tmp_path / "many"
    _make_repo(many)
    for i in range(40):
        _make_repo(many / f"repo{i}")
    monkeypatch.setattr(workspace_mod, "WORKSPACES", {"many": many}, raising=False)
    assert tasks.code_timeout("many") == tasks.CODE_MAX_SECONDS


def test_an_unknown_workspace_gets_a_sane_budget():
    """A missing workspace must not mean a zero-second or unbounded budget."""
    from app import tasks
    assert 32 * 60 < tasks.code_timeout(None) <= tasks.CODE_MAX_SECONDS
    assert 32 * 60 < tasks.code_timeout("does-not-exist") <= tasks.CODE_MAX_SECONDS


# --- a task prepares the repos it needs, not every repo ----------------------

def test_a_task_prepares_only_the_repo_it_names(tmp_path):
    """Two tasks on different services do not conflict, so neither should pay
    for the other's repos.

    Once the workspace stopped mis-reporting itself as one repo, preparing
    everything became three `git fetch`es and three checkouts for a one-line
    change in one service.
    """
    from app import worktrees

    root = tmp_path / "ws"
    _make_repo(root)
    for n in ("telikos-booking-service", "telikos-email-service",
              "telikos-activityplanworkflow-service"):
        _make_repo(root / n)

    picked = [p.name for p in worktrees.repos_for(
        root, "BEPTELIKOS-10159 validate vessel ETA in the booking service")]
    assert picked == ["telikos-booking-service"]

    other = [p.name for p in worktrees.repos_for(root, "fix the email template")]
    assert other == ["telikos-email-service"]


def test_an_unrecognised_task_prepares_everything(tmp_path):
    """The fallback direction is deliberate.

    Preparing a repo that turns out unnecessary costs a fetch. Failing to prepare
    one the task then needs costs the task. A wrong guess must fail towards more
    work, never towards a broken run.
    """
    from app import worktrees

    root = tmp_path / "ws"
    _make_repo(root)
    for n in ("svc-alpha", "svc-beta"):
        _make_repo(root / n)

    assert len(worktrees.repos_for(root, "fix the bug")) == 2
    assert len(worktrees.repos_for(root, "")) == 2
    assert len(worktrees.repos_for(root)) == 2


def test_a_generic_word_does_not_select_every_repo(tmp_path):
    """Repos in one workspace share a prefix and a suffix.

    Matching on "service" or "telikos" would select all of them and quietly turn
    the scoping off while looking like it worked.
    """
    from app import worktrees

    root = tmp_path / "ws"
    _make_repo(root)
    for n in ("telikos-booking-service", "telikos-email-service"):
        _make_repo(root / n)

    picked = [p.name for p in worktrees.repos_for(root, "update the telikos service")]
    assert len(picked) == 2, "a generic word must fall back, not pretend to match"

    exact = [p.name for p in worktrees.repos_for(root, "update booking")]
    assert exact == ["telikos-booking-service"]


def test_repo_discovery_has_exactly_one_definition():
    """Three inline copies of this rule existed and all three had the same bug.

    One of them decided where PRs get raised: with the old shortcut, shipping
    looked for the task's branch in the generated-context repo and would have
    raised no PR for the services the work was actually in.
    """
    from pathlib import Path as _P
    src = _P("app/tasks.py").read_text()
    assert 'iterdir() if (p / ".git")' not in src, \
        "tasks.py restated the repo-discovery rule instead of delegating to worktrees"
