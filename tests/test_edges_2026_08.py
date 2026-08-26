"""Edge cases for the August work — the inputs that were never tried.

Each fix in this register was tested on the case it was written for. This file is
the other half: empty, corrupt, hostile and boundary inputs, plus the interactions
between fixes that landed hours apart and never met.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import capabilities, offers, store, tool_index, turn_budget, worktrees


# --- offers: corrupt and boundary state --------------------------------------

@pytest.fixture(autouse=True)
def _clean_offers():
    store.init()
    offers.drop_all()
    yield
    offers.drop_all()


def test_a_corrupt_queue_does_not_take_the_open_offer_down():
    """Bad state must degrade to "no queue", never to "no offer".

    The head is what he is answering; losing it to a JSON error in a row behind it
    would turn a storage problem into a lost approval.
    """
    o = offers.offer("analyse", "first", "c", "a?")
    store.kv_set(offers.QUEUE_KEY, "{not json")
    assert offers.pending().id == o.id
    assert offers.waiting() == []
    assert offers.accept().id == o.id


def test_a_queue_holding_only_expired_offers_promotes_nothing(monkeypatch):
    """Accepting must not resurrect a question from yesterday."""
    offers.offer("analyse", "head", "c", "a?")
    stale = offers.offer("analyse", "old news", "c", "b?")

    rows = json.loads(store.kv_get(offers.QUEUE_KEY))
    for row in rows:
        row["created"] = 0.0
    store.kv_set(offers.QUEUE_KEY, json.dumps(rows))
    monkeypatch.setattr(offers, "ttl_seconds", lambda: 60)

    offers.accept()
    assert offers.pending() is None, f"promoted an expired offer: {stale.subject}"
    assert offers.waiting() == []


def test_two_ops_differing_only_in_arguments_are_different_questions():
    """Dedup compares the recorded call, so the arguments are the identity."""
    offers.staged_write("jira_comment", {"key": "ABC-1", "text": "one"},
                        "Comment", "c", "Post it?")
    offers.staged_write("jira_comment", {"key": "ABC-1", "text": "two"},
                        "Comment", "c", "Post it?")
    assert len(offers.waiting()) == 1, "a different comment body was treated as the same"


def test_an_offer_with_no_op_falls_back_to_its_subject():
    """Prose offers have no recorded call to compare — subject is the identity."""
    offers.offer("analyse", "head", "c", "a?")
    offers.propose("same news", "first telling", "Do it?", action="x")
    offers.propose("same news", "second telling", "Do it?", action="x")
    same = [o for o in offers.waiting() if o.subject == "same news"]
    assert len(same) == 1
    assert same[0].context == "second telling", "kept the older telling"


def test_drop_all_on_an_empty_state_is_harmless():
    offers.drop_all()
    offers.drop_all()
    assert offers.pending() is None and offers.waiting() == []


# --- turn_budget: degenerate budgets -----------------------------------------

class _Stream:
    def __init__(self, plan):
        self._plan = list(plan)

    async def read(self, _n):
        if not self._plan:
            await asyncio.sleep(3600)
        delay, data = self._plan.pop(0)
        await asyncio.sleep(delay)
        return data


def test_a_zero_budget_stops_immediately_and_says_ceiling():
    """A misconfigured budget must not read as a wedged brain."""
    stop = asyncio.run(turn_budget.drain(_Stream([(0.01, b"x")]), None, total=0, idle=5))
    assert stop.reason == "ceiling"
    assert not stop.ok


def test_an_idle_window_wider_than_the_budget_still_ends_at_the_ceiling():
    """idle > total must not let a turn run past its ceiling waiting for silence."""
    plan = [(0.02, b"tick") for _ in range(200)]
    stop = asyncio.run(turn_budget.drain(_Stream(plan), None, total=0.2, idle=999))
    assert stop.reason == "ceiling"
    assert stop.elapsed < 5


def test_undecodable_output_is_kept_not_dropped():
    """A brain emitting a stray byte must not cost the whole partial answer."""
    stop = asyncio.run(turn_budget.drain(
        _Stream([(0.01, b"edited \xff\xfe Foo.java"), (0.01, b"")]), None,
        total=5, idle=1))
    assert stop.reason == "done"
    assert "Foo.java" in stop.partial


def test_a_stopped_turn_with_no_output_still_explains_itself():
    """The message must stand on its own when there is nothing to show."""
    err = turn_budget.TurnStopped(turn_budget.Stop("idle", 200.0, 150.0, []))
    text = str(err)
    assert "stuck" in text
    assert "It got this far" not in text, "promised output it does not have"


# --- worktrees: hostile workspaces -------------------------------------------

def test_a_missing_workspace_yields_no_repos(tmp_path):
    assert worktrees.repos_in(tmp_path / "nope") == []
    assert worktrees.all_repos_in(tmp_path / "nope") == []
    assert worktrees.repos_for(tmp_path / "nope", "anything") == []


def test_an_empty_workspace_yields_no_repos(tmp_path):
    (tmp_path / "empty").mkdir()
    assert worktrees.repos_in(tmp_path / "empty") == []


def test_a_hint_matching_no_repo_falls_back_to_all(tmp_path):
    root = tmp_path / "ws"
    (root / ".git").mkdir(parents=True)
    for n in ("alpha", "beta"):
        (root / n / ".git").mkdir(parents=True)
    assert len(worktrees.repos_for(root, "something entirely unrelated")) == 2


def test_a_single_repo_workspace_never_yields_zero_repos(tmp_path):
    """A hint that matches nothing must never scope a workspace down to nothing.

    This is a property of the fallback (`named or repos`), not of the `len <= 1`
    early return — mutation testing showed removing that shortcut changes nothing
    here, because the fallback already covers it. Stated honestly rather than
    claiming to test a line it does not reach: the shortcut is there to skip the
    regex work, and the guarantee lives in the fallback.
    """
    root = tmp_path / "solo"
    (root / ".git").mkdir(parents=True)
    assert [p.name for p in worktrees.repos_for(root, "unrelated words")] == ["solo"]
    assert [p.name for p in worktrees.repos_for(root, "")] == ["solo"]


def test_the_worktree_directory_is_never_mistaken_for_a_repo(tmp_path):
    """Worktrees live inside the workspace; treating them as repos would recurse."""
    root = tmp_path / "ws"
    (root / ".git").mkdir(parents=True)
    (root / "svc" / ".git").mkdir(parents=True)
    (root / worktrees.DIRNAME / ".git").mkdir(parents=True)
    names = [p.name for p in worktrees.repos_in(root)]
    assert worktrees.DIRNAME not in names
    assert names == ["svc"]


# --- the always-core, at the boundary ----------------------------------------

def test_the_floor_survives_even_if_it_is_bigger_than_the_cap(monkeypatch):
    """A cap below the floor must keep the floor, not empty the toolset."""
    monkeypatch.setattr(tool_index, "STICKY_MAX_TOOLS", 2)
    monkeypatch.setattr(tool_index, "STICKY_SLACK", 0)
    floor = tool_index._floor()
    picked = ["jira_issue"]
    out = tool_index._recent(dict.fromkeys(capabilities.names()), picked)

    for name in floor:
        assert name in out, f"{name} is in ALWAYS and was dropped by a tight cap"
    assert "jira_issue" in out, "this turn's own pick was dropped"

    # And the cap must still BIND. Asserting only that the protected names
    # survived passed even when the room calculation was wrong, because protected
    # names are added before any trimming — the bound is the part worth checking.
    protected = set(floor) | set(picked)
    carried = [n for n in out if n not in protected]
    assert not carried, (
        f"the cap ({tool_index.STICKY_MAX_TOOLS}) is already exceeded by the "
        f"protected set ({len(protected)}), so nothing extra may be carried; "
        f"got {carried}")


# --- a side turn may answer, never start work that edits code -----------------

def test_a_side_turn_cannot_spawn_a_code_task():
    """delegate_task is write=False because it sends nothing outward — true, and
    not the point: it spawns a worker that changes repos.

    Without this the guarantee that makes concurrent answering safe ("the worst it
    can do is read something and answer") would not hold, because the guard that
    would otherwise catch it lives behind ASTA_RELEVANCE, which is off.
    """
    from app import agent

    token = capabilities.READ_ONLY_TURN.set(True)
    try:
        out = agent.delegate_task("t", "p", kind="code", workspace="test-workspace")
    finally:
        capabilities.READ_ONLY_TURN.reset(token)
    assert "won't start a code task" in out
    assert store.list_tasks() == [] or all(
        t["kind"] != "code" or t["title"] != "t" for t in store.list_tasks())


def test_a_normal_turn_may_still_spawn_a_code_task(monkeypatch):
    """The block must be scoped to side turns, not a new blanket refusal."""
    from app import agent, tasks

    spawned = {}

    def fake_spawn(title, prompt, kind, workspace, teams_chat=""):
        spawned.update({"title": title, "kind": kind})
        return {"id": 1, "title": title}

    monkeypatch.setattr(tasks, "spawn", fake_spawn)
    token = capabilities.READ_ONLY_TURN.set(False)
    try:
        agent.delegate_task("real work", "p", kind="code", workspace="test-workspace")
    finally:
        capabilities.READ_ONLY_TURN.reset(token)
    assert spawned.get("kind") == "code", "a normal turn was blocked from delegating"
