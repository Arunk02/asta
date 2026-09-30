"""P6 and the leftovers, 30 Sep.

- merge_pr could never have worked: merge_state and merge ran `gh gh pr …`
  (the helper already prepends gh). Every test mocked merge_state whole, so the
  broken argv was never seen. And "activityplan-worfklow-service" was used as a
  folder name letter for letter.
- "merge task 166's PR" cost twelve model calls finding the PR: the task's own
  record has the link.
- The scorecard's "tokens per turn" summed every call in a turn and read like a
  context size; it is now split into context per call and calls per turn.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from app import review
    root = tmp_path / "booking-workspace"
    for name in ("telikos-activityplanworkflow-service", "telikos-booking-service"):
        (root / name / ".git").mkdir(parents=True)

    class P:
        pass
    prov = P()
    prov.root = str(root)
    monkeypatch.setattr(review.ws_mod, "provider_for", lambda ws: prov)
    return root


@pytest.mark.parametrize("said,folder", [
    ("activityplan-worfklow-service", "telikos-activityplanworkflow-service"),
    ("activityplanworkflow-service", "telikos-activityplanworkflow-service"),
    ("telikos-booking-service", "telikos-booking-service"),
    ("Maersk-Global/telikos-booking-service", "telikos-booking-service"),
    ("billing", ""),
])
def test_a_repo_is_found_the_way_he_spells_it(workspace, said, folder):
    from app import review
    assert review.resolve_repo(workspace, said) == folder


def test_the_merge_checks_run_gh_once_in_the_right_folder(workspace, monkeypatch):
    from app import review
    ran = []

    async def git(cwd, *args, timeout=120, stdin=""):
        ran.append((str(cwd), list(args)))
        return 0, json.dumps({"number": 1251, "title": "t", "state": "OPEN", "isDraft": False,
                              "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                              "reviewDecision": "APPROVED", "statusCheckRollup": [],
                              "headRefName": "h", "baseRefName": "develop"})

    monkeypatch.setattr(review.repo_ops, "git", git)
    asyncio.run(review.merge_state("1251", "booking", "activityplan-worfklow-service"))
    cwd, argv = ran[0]
    assert argv[:3] == ["gh", "pr", "view"], f"not `gh gh`: {argv}"
    assert cwd.endswith("telikos-activityplanworkflow-service")


def test_a_pr_link_needs_no_clone_at_all(workspace, monkeypatch):
    from app import review
    ran = []

    async def git(cwd, *args, timeout=120, stdin=""):
        ran.append(list(args))
        return 0, json.dumps({"number": 1251, "state": "OPEN", "statusCheckRollup": [],
                              "mergeable": "MERGEABLE", "reviewDecision": "APPROVED"})

    monkeypatch.setattr(review.repo_ops, "git", git)
    asyncio.run(review.merge(
        "https://github.com/Maersk-Global/telikos-activityplanworkflow-service/pull/1251",
        "booking"))
    merge_call = ran[-1]
    assert merge_call[:3] == ["gh", "pr", "merge"]
    assert "-R" in merge_call and "Maersk-Global/telikos-activityplanworkflow-service" in merge_call


def test_a_task_he_names_comes_with_its_links():
    from app import copilot_cli
    t = store.create_task("Navya R asked: can you please merge this", "analysis",
                          "prompt", "booking")
    tid = t["id"] if isinstance(t, dict) else int(t)
    store.update_task(tid, status="done", result=(
        "PR #1251 is a downmerge… https://github.com/Maersk-Global/"
        "telikos-activityplanworkflow-service/pull/1251 all green"))
    got = copilot_cli._tasks_named(f"no task {tid} - navya PR can you merge it")
    assert f"Task #{tid}" in got and "/pull/1251" in got and "#1251" in got
    assert copilot_cli._tasks_named("merge it") == ""


def test_calls_per_turn_is_measured_separately_from_context(monkeypatch):
    from app import scorecard
    now = time.time()
    monkeypatch.setattr(scorecard, "_rows", lambda sql, args=(): [
        {"channel": "whatsapp", "total_ms": 20000, "first_token_ms": 9000,
         "input_tokens": 10, "cached_tokens": 150_000, "context_tokens": 30_000}])
    r = scorecard.replies(now - 3600, now)
    assert round(r["calls_per_turn_avg"]) == 5 and r["context_per_call_avg"] == 30_000


# --- the usage limit is respected, not hammered --------------------------------------

def _fresh_claude_cli():
    import importlib.util
    from app import claude_cli as sealed
    spec = importlib.util.spec_from_file_location("app._claude_cli_breaker", sealed.__file__)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "app"
    spec.loader.exec_module(mod)
    return mod


def test_a_session_limit_stops_further_calls_until_it_resets(monkeypatch):
    cli = _fresh_claude_cli()
    now = time.time()
    until = cli.note_limit("You've hit your session limit · resets 3:40am (Asia/Calcutta)", now=now)
    assert until and now < until <= now + cli.LIMIT_CAP_SECONDS
    spawned = []

    async def spawn(*a, **k):
        spawned.append(a)
        raise AssertionError("must not spawn while limited")

    monkeypatch.setattr(cli, "available", lambda: True)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(cli.one_shot("x", tools_off=True, model="haiku"))
    from app import agent
    assert agent.transient_limit(str(exc.value)), "tasks still read it as a pause"
    assert spawned == []


def test_an_ordinary_failure_is_not_a_limit():
    cli = _fresh_claude_cli()
    assert cli.note_limit("Error: invalid JSON in settings") is None
    assert cli.limited_until() == 0.0


def test_a_limit_is_forgotten_once_it_lifts():
    cli = _fresh_claude_cli()
    past = time.time() - 7200
    store.kv_set("claude_limited_until", str(past))
    assert cli.limited_until() == 0.0


# --- the reader sees the reply, not the quote ----------------------------------------

def test_a_quoted_reply_is_read_as_the_reply():
    from app import chat_watch
    t = ("Arunkumar K\n25/09/2026 15:50\nHi Sankalp could you please confirm that this is "
         "the customer\nyes it is")
    got = chat_watch.as_read(t, {"Hi Sankalp could you please confirm that this is the customer"})
    assert got == "yes it is (replying to an earlier message)"
    assert chat_watch.as_read("plain words", None) == "plain words"


# --- accuracy is measured on what Asta DOES ------------------------------------------

@pytest.mark.parametrize("state,subject,handled,act", [
    ("closing", "", False, "quiet"), ("status", "", False, "quiet"),
    ("ask", "stated", True, "quiet"),
    ("opener", "", False, "clarify"), ("ask", "unclear", False, "clarify"),
    ("ask", "continuing", False, "work"), ("urgent", "stated", False, "work"),
])
def test_readings_map_to_what_asta_does(state, subject, handled, act):
    from app import understand_corpus
    assert understand_corpus.action(state, subject, handled) == act


# --- his rule, enforced: a bare message names no subject -----------------------------

@pytest.mark.parametrize("text,bare", [
    ("call?", True), ("bro", True), ("Hii Arun ...", True), ("Arunkumar K can we connect?", True),
    ("can we connect for a min", True), ("call? about PR 1251", False), ("ok", False),
    ("H65ZMWX52B2", False), ("Hi Arunkumar bro for this change are we creating a new topic?", False),
])
def test_what_counts_as_bare(text, bare):
    from app import understand
    assert understand._bare([text]) is bare


def test_a_bare_call_is_never_assumed_to_be_about_the_old_topic():
    from app import understand
    d = {"state": "ask", "subject": "continuing", "source": "model", "work": "talk"}
    got = understand.settle(d, {"new": ["Arunkumar K Call ?"]})
    assert got["subject"] == "unclear"


def test_a_bare_greeting_is_an_opener_not_news():
    from app import understand
    d = {"state": "status", "subject": "stated", "source": "model"}
    assert understand.settle(d, {"new": ["Hii Arun ..."]})["state"] == "opener"
    assert understand.settle(d, {"new": ["ok"]})["state"] == "status"
    assert understand.settle(d, {"new": ["Hii Arun"], "handled_by_him": True})["state"] == "status"


def test_the_reader_is_told_when_he_is_in_the_exchange():
    from app import understand
    text = understand.prompt([{"id": "x", "who": "S", "new": ["aana prod odudhu ilai"],
                               "arun_minutes_ago": 2}])
    assert '"arun_last_spoke_minutes_ago": 2' in text


def test_a_bare_call_is_asked_about_with_the_guess_not_the_generic_line():
    from app import understand
    d = {"state": "ask", "subject": "continuing", "source": "model", "work": "talk",
         "question": "", "guess": "the corrupt-message defect closure"}
    got = understand.settle(d, {"new": ["Arunkumar K Call ?"]})
    assert got["question"] == ("Sure — is this about the corrupt-message defect closure, "
                               "or something else?")
    assert understand.safe_question(got["question"]), "fit to send unapproved"
