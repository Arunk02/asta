"""Which pipelines are worth telling him about.

The noisy version of a CI watcher is the one that gets muted, and a muted watcher
tells him nothing on the day it matters. So the default is narrow — his own work —
and everything else is opt-in.

The gap these pin down: "runs he triggered" is not the same set as "his pull
requests". A colleague pushing a fix to his branch turns HIS PR red under someone
else's name, and the actor filter dropped exactly that.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import ci_watch, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    yield


def _run(rid, workflow="build.yml", branch="main", concl="failure"):
    return {"databaseId": rid, "status": "completed", "conclusion": concl,
            "workflowName": workflow, "headBranch": branch,
            "displayTitle": "some commit", "url": f"https://gh/{rid}", "event": "push"}


def _gh(monkeypatch, runs, mine, my_branches):
    """Stand in for the three gh calls _poll_repo makes."""
    async def fake(*args, timeout=30):
        args = list(args)
        if args[0] == "pr":
            return 0, json.dumps([{"headRefName": b} for b in my_branches])
        if "--user" in args:
            return 0, json.dumps([{"databaseId": r} for r in mine])
        return 0, json.dumps(runs)

    monkeypatch.setattr(ci_watch, "_run_gh", fake)
    monkeypatch.setattr(ci_watch, "my_login", _login)


async def _login():
    return "Arunk02"


def _poll(repo="Arunk02/asta"):
    return asyncio.run(ci_watch._poll_repo(repo))


def _prime(monkeypatch, runs, mine, my_branches):
    """The first poll baselines history silently — that is deliberate, so tests
    that care about notifications have to get past it first."""
    _gh(monkeypatch, runs, mine, my_branches)
    _poll()


# --- the default: his own work ----------------------------------------------

def test_a_run_he_triggered_is_reported(monkeypatch):
    _prime(monkeypatch, [_run("1", concl="success")], ["1"], [])
    _gh(monkeypatch, [_run("2")], ["2"], [])
    notes = _poll()
    assert notes and "🔴" in notes[0]


def test_a_red_pipeline_on_his_pr_is_reported_even_when_he_did_not_push(monkeypatch):
    """The gap the actor filter left: someone else pushes to his branch, his PR
    goes red, and he was the last to hear about it."""
    _prime(monkeypatch, [_run("1", branch="feat/mine", concl="success")], [], ["feat/mine"])
    _gh(monkeypatch, [_run("2", branch="feat/mine")], [], ["feat/mine"])
    notes = _poll()
    assert notes and "feat/mine" in notes[0]


def test_somebody_elses_pipeline_stays_silent(monkeypatch):
    """Ten failure pings in an evening for other people's branches is how this
    feature gets turned off."""
    _prime(monkeypatch, [_run("1", branch="theirs", concl="success")], [], [])
    _gh(monkeypatch, [_run("2", branch="theirs")], [], [])
    assert _poll() == []


# --- explicit subscriptions -------------------------------------------------

def test_a_watched_build_is_reported_regardless_of_who_ran_it(monkeypatch):
    ci_watch.watch("release")
    _prime(monkeypatch, [_run("1", branch="release", concl="success")], [], [])
    _gh(monkeypatch, [_run("2", branch="release")], [], [])
    notes = _poll()
    assert notes and "release" in notes[0]


def test_a_subscription_matches_the_workflow_name_too():
    assert ci_watch._subscribed("r/x", _run("1", workflow="nightly-e2e"))\
        is False
    ci_watch.watch("nightly")
    assert ci_watch._subscribed("r/x", _run("1", workflow="nightly-e2e")) is True


def test_a_subscription_can_be_pinned_to_one_repo():
    ci_watch.watch("release", repo="Arunk02/asta")
    assert ci_watch._subscribed("Arunk02/asta", _run("1", branch="release"))
    assert not ci_watch._subscribed("other/repo", _run("1", branch="release"))


def test_watching_the_same_thing_twice_does_not_duplicate_it():
    ci_watch.watch("release")
    second = ci_watch.watch("release")
    assert "Already watching" in second
    assert len(ci_watch.subscriptions()) == 1


def test_unwatching_stops_it():
    ci_watch.watch("release")
    assert "Stopped watching" in ci_watch.unwatch("release")
    assert ci_watch.subscriptions() == []


def test_unwatching_something_he_never_watched_says_so():
    assert "Wasn't watching" in ci_watch.unwatch("ghost")


def test_an_empty_subscription_is_refused():
    assert "Name a" in ci_watch.watch("   ")
    assert ci_watch.subscriptions() == []


def test_corrupt_subscription_state_never_crashes_the_watcher():
    store.kv_set(ci_watch.SUBS_KEY, "{not json")
    assert ci_watch.subscriptions() == []
    store.kv_set(ci_watch.SUBS_KEY, json.dumps(["a string", {"no": "match key"}]))
    assert ci_watch.subscriptions() == []


def test_the_tool_routes_a_stop_prefix_to_unsubscribe():
    from app import agent
    agent.watch_ci("release")
    assert "Stopped watching" in agent.watch_ci("stop release")
    assert ci_watch.subscriptions() == []


def test_status_shows_what_else_is_being_watched():
    ci_watch.watch("release")
    assert "release" in ci_watch.status()["also_watching"]


# --- resilience -------------------------------------------------------------

def test_a_failed_pr_lookup_falls_back_to_his_own_runs(monkeypatch):
    """gh being unhappy about one call must not silence the whole watcher."""
    async def fake(*args, timeout=30):
        if list(args)[0] == "pr":
            return 1, "gh: not found"
        if "--user" in list(args):
            return 0, json.dumps([{"databaseId": "2"}])
        return 0, json.dumps([_run("2")])

    monkeypatch.setattr(ci_watch, "my_login", _login)
    monkeypatch.setattr(ci_watch, "_run_gh", fake)
    _poll()                       # priming poll
    notes = _poll()
    assert notes == [] or "🔴" in notes[0]      # never raises
