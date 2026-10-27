"""13 Oct, Komal: "Hi Arunkumar, please approve - <booking PR 1479>".

Asta read it as a plain ask (the link stripped, no "PR" or "review" left), ran a
general analysis task, and that task — every tool, permissions bypassed — ran
`gh pr review 1479 --approve` under his login 31 seconds in, from three commands
and a truncated diff, past an open finding from the Copilot reviewer. His phone
then got "approved and posted … Shall I send it?". Earlier, "Komal asked about
wrote to you" with a draft that only said "Looking into the PR details".
"""

import asyncio
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import answers, chat_watch, claude_cli, copilot_cli, responder, review, store, task_fence

PR = "https://github.com/Maersk-Global/telikos-booking-service/pull/1479"

# The real launchers, taken before conftest swaps them for a stub. These tests
# stop at the subprocess call — nothing is spawned.
_REAL = {"claude": claude_cli.one_shot, "copilot": copilot_cli.one_shot}


# --- the fence: the same list for every brain --------------------------------------

def test_no_task_may_approve_merge_or_close():
    for mode in ("none", "code"):
        d = task_fence.denied(mode)
        assert {"gh pr review", "gh pr merge", "gh pr close"} <= set(d)


def test_an_analysis_posts_nothing_a_code_task_may_open_its_own_pr():
    assert {"gh pr create", "gh pr comment", "git push", "git commit"} <= set(task_fence.denied("none"))
    code = task_fence.denied("code")
    assert "gh pr create" not in code and "git push" not in code


def test_outside_a_task_nothing_is_fenced():
    assert task_fence.denied("") == []
    assert "ASTA_TASK_WRITES" not in task_fence.env({"PATH": "/bin"}, mode="")


def test_each_brain_gets_the_list_in_its_own_spelling():
    assert "Bash(gh pr review:*)" in task_fence.claude_deny("none")
    assert "shell(gh pr review)" in task_fence.copilot_deny("none")


# --- the wrappers, against a fake gh/git so nothing reaches GitHub -----------------

@pytest.fixture
def fake_bin(tmp_path):
    for name in ("gh", "git"):
        p = tmp_path / name
        p.write_text(f'#!/bin/bash\necho "REAL {name} $*"\n')
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def _run(fake_bin, mode, *args):
    env = task_fence.env({"PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin"}, mode=mode)
    p = subprocess.run(list(args), env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


@pytest.mark.parametrize("mode,args,blocked", [
    ("none", ("gh", "pr", "review", "1479", "-R", "o/r", "--approve"), True),
    ("none", ("gh", "-R", "o/r", "pr", "merge", "1"), True),
    ("none", ("gh", "pr", "comment", "1", "-b", "x"), True),
    ("none", ("gh", "pr", "create", "--title", "x"), True),
    ("none", ("gh", "api", "-X", "POST", "repos/o/r/pulls/1/reviews"), True),
    ("none", ("gh", "api", "repos/o/r/issues/1/comments", "-f", "body=x"), True),
    ("none", ("gh", "pr", "view", "1479", "--json", "state"), False),
    ("none", ("gh", "pr", "diff", "1479"), False),
    ("none", ("gh", "api", "repos/o/r/pulls/1/comments"), False),
    ("code", ("gh", "pr", "review", "1", "--approve"), True),
    ("code", ("gh", "pr", "merge", "1"), True),
    ("code", ("gh", "api", "-X", "PUT", "repos/o/r/pulls/1/merge"), True),
    ("code", ("gh", "pr", "create", "--title", "x"), False),
    ("code", ("gh", "pr", "comment", "1", "-b", "x"), False),
    ("none", ("git", "push"), True),
    ("none", ("git", "-C", ".", "commit", "-m", "x"), True),
    ("none", ("git", "log", "-1"), False),
    ("code", ("git", "push"), False),
])
def test_the_wrapper_refuses_writes_and_passes_reads(fake_bin, mode, args, blocked):
    rc, out = _run(fake_bin, mode, *args)
    if blocked:
        assert rc == 3 and "blocked in this task" in out and "REAL" not in out
    else:
        assert rc == 0 and f"REAL {args[0]}" in out


def test_outside_a_task_the_wrapper_is_not_even_on_the_path(fake_bin):
    rc, out = _run(fake_bin, "", "gh", "pr", "review", "1", "--approve")
    assert rc == 0 and "REAL gh" in out


# --- both brains start a task leg fenced -------------------------------------------

def _capture(monkeypatch, module):
    seen = {}
    name = "claude" if module is claude_cli else "copilot"
    monkeypatch.setattr(module, "one_shot", _REAL[name])
    monkeypatch.setattr(module, "available", lambda: True)
    if module is claude_cli:
        monkeypatch.setattr(claude_cli, "limited_until", lambda: 0)

    async def fake_exec(*cmd, **kw):
        seen["cmd"], seen["env"] = list(cmd), kw.get("env") or {}
        raise RuntimeError("captured")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
    return seen


def test_a_claude_task_leg_is_fenced(monkeypatch):
    seen = _capture(monkeypatch, claude_cli)
    token = task_fence.MODE.set("none")
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(claude_cli.one_shot("x", timeout=5))
    finally:
        task_fence.MODE.reset(token)
    deny = seen["cmd"][seen["cmd"].index("--disallowed-tools") + 1]
    assert "Bash(gh pr review:*)" in deny
    assert seen["env"]["PATH"].startswith(str(task_fence.SHIMS))
    assert seen["env"]["ASTA_TASK_WRITES"] == "none"


def test_a_planning_leg_keeps_its_own_ban_too(monkeypatch):
    seen = _capture(monkeypatch, claude_cli)
    token = task_fence.MODE.set("code")
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(claude_cli.one_shot("x", timeout=5, plan_only=True))
    finally:
        task_fence.MODE.reset(token)
    assert seen["cmd"].count("--disallowed-tools") == 1
    deny = seen["cmd"][seen["cmd"].index("--disallowed-tools") + 1]
    assert "Write" in deny and "Bash(gh pr merge:*)" in deny


def test_a_copilot_task_leg_is_fenced(monkeypatch):
    seen = _capture(monkeypatch, copilot_cli)
    token = task_fence.MODE.set("none")
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(copilot_cli.one_shot("x", timeout=5))
    finally:
        task_fence.MODE.reset(token)
    assert "shell(gh pr review)" in seen["cmd"]
    assert seen["env"]["PATH"].startswith(str(task_fence.SHIMS))


def test_a_chat_turn_is_not_a_task(monkeypatch):
    seen = _capture(monkeypatch, claude_cli)
    with pytest.raises(RuntimeError):
        asyncio.run(claude_cli.one_shot("x", timeout=5))
    assert "--disallowed-tools" not in seen["cmd"]
    assert "ASTA_TASK_WRITES" not in seen["env"]


def test_an_analysis_task_runs_inside_the_fence(monkeypatch):
    from app import tasks
    seen = {}

    async def leg(task_id, t, prompt):
        seen["mode"], seen["prompt"] = task_fence.MODE.get(), prompt
        return "ok"

    monkeypatch.setattr(tasks, "_simple_leg", leg)
    task = store.create_task('Read PR', 'analysis', 'look at PR 1479', None)
    asyncio.run(tasks._run_simple(task['id'], task, "look at PR 1479"))
    assert seen["mode"] == "none"
    assert "please approve" in seen["prompt"] and "blocked here" in seen["prompt"]
    assert task_fence.MODE.get() == "", "the fence ends with the leg"


def test_a_code_leg_runs_inside_the_code_fence(monkeypatch):
    from app import tasks
    seen = {}

    async def leg(task_id, prompt, cwd, **kw):
        seen["mode"] = task_fence.MODE.get()
        return "ok"

    monkeypatch.setattr(tasks, "_code_leg", leg)
    task = store.create_task('Fix it', 'code', 'fix it', None)
    asyncio.run(tasks._run_code_leg(task['id'], "fix it", "/tmp", resume=False, effort=""))
    assert seen["mode"] == "code"


# --- "please approve <link>" is his review, done properly ---------------------------

@pytest.mark.parametrize("text", [f"Hi Arunkumar, please approve - {PR}", PR,
                                  f"Hi Arun {PR}", f"can you check once {PR}"])
def test_a_pr_link_to_approve_is_a_review_request(text):
    assert responder.what_it_asks(text) == "review_request"


def test_feedback_on_his_pr_and_an_outage_keep_their_reading():
    assert responder.what_it_asks(f"I left comments on your PR {PR}") == "pr_review"
    assert responder.what_it_asks(f"prod is down after {PR}") == "incident"


def test_the_review_path_never_approves_itself():
    brief = responder._BRIEFS["review_request"]
    assert "do not approve anything yourself" in brief and "propose_pr_review" in brief


def test_the_review_sees_what_others_already_raised(monkeypatch):
    async def gh(cwd, *args, timeout=120):
        if "reviews" in args:
            return 0, ('{"reviews":[{"author":{"login":"copilot-pull-request-reviewer"},'
                       '"state":"COMMENTED","body":"<b>Changes recommended</b> isServiceOrder '
                       'not propagated"}]}')
        return 0, ('[{"user":{"login":"copilot-pull-request-reviewer"},"path":"A.java",'
                   '"line":42,"body":"Propagate isServiceOrder"}]')

    monkeypatch.setattr(review, "_gh", gh)
    text = asyncio.run(review._already_raised(Path("."), "1479", ["-R", "o/r"], {}))
    assert "Changes recommended isServiceOrder" in text and "<b>" not in text
    assert "`A.java:42` Propagate isServiceOrder" in text
    assert "{others}" in review.REVIEW_BRIEF and "never APPROVE past an open one" in review.REVIEW_BRIEF


# --- what reaches his phone -----------------------------------------------------------

def test_a_header_that_says_what_happened_reads_as_a_sentence():
    out = answers.render("Komal Jayswal", "wrote to you", "", "Looking into it")
    assert "asked about wrote to you" not in out
    assert "*Komal Jayswal* wrote to you." in out
    assert "*Komal Jayswal* asked about the PR approved." in \
        answers.render("Komal Jayswal", "The PR approved", "", "x")


@pytest.mark.parametrize("line,dropped", [
    ("Looking into the PR details", True), ("Sure, checking now.", True),
    ("will review and update you", True), ("checking PR 1479", True),
    ("Looking into it — the 400 came from billing.", False),
    ("Looking into it, the booking is in QA", False),
    ("Got it, the ack was consumed.", False),
])
def test_only_a_pure_progress_line_is_dropped(line, dropped):
    assert bool(chat_watch._PROGRESS_ONLY.match(line)) is dropped


def test_a_held_progress_line_is_not_put_to_him(monkeypatch):
    from app import teams_bridge as tb
    shown = []

    async def present(**kw):
        shown.append(kw)
        return True

    monkeypatch.setattr(tb, "enabled", lambda: False)
    monkeypatch.setattr(answers, "present", present)
    assert asyncio.run(chat_watch._say("Komal Jayswal", "Looking into the PR details")) is False
    assert shown == []
