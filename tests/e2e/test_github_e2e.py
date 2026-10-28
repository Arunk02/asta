"""Merging, reviewing and telling, against a real git remote and a stand-in `gh`.

Asta's own code runs the `gh` it finds on PATH, exactly as in production; here
that is tests/e2e/fake_gh.py, which answers from a real bare repository and
merges into it for real. Only Teams is replaced, at the send itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys

import pytest

from app import (agent, capabilities, go, ops, pr_authority, review, senior, steward, store,
                 task_inform, tasks, teams_bridge)

URL = "https://github.com/acme/booking/pull/1409"
NOTES = "VERDICT: APPROVE — the retry is capped.\n\nNON-BLOCKING:\n- `retry.py:2` — name the cap → MAX_RETRY"


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def github(tmp_path, monkeypatch):
    origin = tmp_path / "booking.git"
    seed = tmp_path / "seed"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    run("git", "clone", "-q", str(origin), str(seed))
    for k, v in (("user.email", "dev@example.test"), ("user.name", "dev")):
        run("git", "config", k, v, cwd=seed)
    (seed / "retry.py").write_text("RETRIES = None\n")
    run("git", "add", "-A", cwd=seed); run("git", "commit", "-qm", "base", cwd=seed)
    run("git", "push", "-q", "origin", "main", cwd=seed)
    run("git", "checkout", "-q", "-b", "feature", cwd=seed)
    (seed / "retry.py").write_text("RETRIES = 3\n")
    run("git", "commit", "-qam", "cap the retry", cwd=seed)
    run("git", "push", "-q", "origin", "feature", cwd=seed)
    state_file = tmp_path / "github.json"
    state = {"origin": str(origin), "viewer": "arunk02",
             "prs": {"1409": {"repo": "acme/booking", "head": "feature", "base": "main",
                              "author": "a-colleague", "title": "Cap the retry", "checks": {}}}}
    state_file.write_text(json.dumps(state))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nexec {sys.executable} {os.path.dirname(__file__)}/fake_gh.py \"$@\"\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state_file))
    for name in ("ASTA_GITHUB_WORK_USER", "ASTA_GITHUB_WORK_OWNERS", "ASTA_GITHUB_LOGINS"):
        monkeypatch.delenv(name, raising=False)

    class GitHub:
        def state(self):
            return json.loads(state_file.read_text())

        def edit(self, fn):
            s = self.state(); fn(s); state_file.write_text(json.dumps(s))

        def head(self):
            return run("git", "--git-dir", str(origin), "rev-parse", "refs/heads/feature")

        def green(self):
            sha = self.head()
            self.edit(lambda s: s["prs"]["1409"]["checks"].__setitem__(
                sha, [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]))

        def push(self, text):
            (seed / "retry.py").write_text(text)
            run("git", "commit", "-qam", "follow-up", cwd=seed)
            run("git", "push", "-q", "origin", "feature", cwd=seed)

        def main_file(self):
            return run("git", "--git-dir", str(origin), "show", "main:retry.py")

        def ran(self, *prefix):
            return [a for a in self.state().get("log", []) if a[:len(prefix)] == list(prefix)]

    return GitHub()


@pytest.fixture
def teams(monkeypatch):
    sent = []

    async def send(to="", text="", to_group=False):
        sent.append({"to": to, "text": text})
        return f"✅ Sent to {to} Kumar."

    async def resolve(chat, allow_group=False):
        return {"asked": chat, "opened": "Vinish Kumar"}

    monkeypatch.setitem(ops.REGISTRY["teams_send"], "run", send)
    monkeypatch.setattr(teams_bridge, "resolve_target", resolve)
    monkeypatch.setattr(steward, "sends_held", lambda: False)
    monkeypatch.setattr(senior, "is_senior", lambda target: False)
    return sent


def shipped_task(words):
    t = store.create_task("Cap the retry", "code", "cap the retry", None)
    store.update_task(t["id"], status="shipped", pr_urls=URL)
    go.grant(t["id"], words, ship=True)
    task_inform.record(t["id"], words)
    return t["id"]


def test_a_task_merges_through_real_git_once_ci_is_green_then_tells_vinish(github, teams):
    tid = shipped_task("raise the PR, merge after CI passes and inform Vinish")
    asyncio.run(tasks.check_pr(tid))                                  # CI still running
    assert github.ran("pr", "merge") == [] and teams == []
    github.green()
    head = github.head()
    asyncio.run(tasks.check_pr(tid))
    merge, = github.ran("pr", "merge")
    assert merge[merge.index("--match-head-commit") + 1] == head
    assert github.main_file() == "RETRIES = 3", "the change is really on main"
    line = asyncio.run(tasks.check_pr(tid))                           # GitHub now says MERGED
    assert "Merged" in line and "Told Vinish" in line
    assert len(teams) == 1 and teams[0]["to"] == "Vinish" and "1409" in teams[0]["text"]
    asyncio.run(tasks.check_pr(tid))
    assert len(github.ran("pr", "merge")) == 1 and len(teams) == 1


def test_a_commit_pushed_after_green_ci_waits_for_its_own_ci(github, teams):
    tid = shipped_task("merge after CI passes")
    github.green()
    github.push("RETRIES = 4\n")                                       # new head, no CI yet
    asyncio.run(tasks.check_pr(tid))
    assert github.ran("pr", "merge") == []
    github.green()
    asyncio.run(tasks.check_pr(tid))
    merge, = github.ran("pr", "merge")
    assert merge[merge.index("--match-head-commit") + 1] == github.head()
    assert github.main_file() == "RETRIES = 4"


def _ask(words, monkeypatch):
    spawned = []

    def spawn(title, prompt, kind="analysis", workspace=None, **kw):
        t = store.create_task(title, kind, prompt, workspace)
        spawned.append(t)
        return t

    monkeypatch.setattr(tasks, "spawn", spawn)
    token = capabilities.TURN_TEXT.set(words)
    try:
        said = asyncio.run(agent.review_pr(URL))
    finally:
        capabilities.TURN_TEXT.reset(token)
    return said, spawned


def _verdict(tid, notes=NOTES):
    token = capabilities.FROM_TASK.set(str(tid))
    try:
        return asyncio.run(agent.propose_pr_review(URL, notes))
    finally:
        capabilities.FROM_TASK.reset(token)


def test_review_approve_and_merge_if_good_runs_against_real_git(github, monkeypatch):
    github.green()
    said, spawned = _ask(f"review {URL}, approve and merge if good", monkeypatch)
    assert "approve and merge it under your instruction" in said
    assert "RETRIES = 3" in spawned[0]["prompt"], "the worker's brief carries the real diff"
    line = _verdict(spawned[0]["id"])
    review_post, = github.state()["reviews"]
    assert review_post["event"] == "APPROVE" and review_post["head"] == github.head()
    assert review_post["comments"] == 1
    merge, = github.ran("pr", "merge")
    assert merge[merge.index("--match-head-commit") + 1] == github.head()
    assert github.main_file() == "RETRIES = 3" and "merged" in line


def test_his_own_pr_is_never_sent_an_approval(github, monkeypatch):
    github.edit(lambda s: s["prs"]["1409"].__setitem__("author", "arunk02"))
    github.green()
    _, spawned = _ask(f"review {URL}, approve and merge if good", monkeypatch)
    line = _verdict(spawned[0]["id"])
    assert "your own PR" in line and "reviews" not in github.state()
    assert github.main_file() == "RETRIES = 3", "the merge still went through GitHub's own rules"


def test_a_head_pushed_during_the_review_is_reviewed_again_not_approved(github, monkeypatch):
    github.green()
    _, spawned = _ask(f"review {URL}, approve and merge if good", monkeypatch)
    github.push("RETRIES = 5\n")
    line = _verdict(spawned[0]["id"])
    assert "reviewing the new head" in line
    assert "reviews" not in github.state() and github.ran("pr", "merge") == []
    rerun = spawned[-1]
    assert pr_authority.get(rerun["id"])["revision"].split("@")[1].startswith(github.head())


def test_a_plain_review_ask_posts_and_merges_nothing(github, monkeypatch):
    github.green()
    _, spawned = _ask(f"review {URL}", monkeypatch)
    assert "Staged" in _verdict(spawned[0]["id"])
    assert "reviews" not in github.state() and github.ran("pr", "merge") == []
