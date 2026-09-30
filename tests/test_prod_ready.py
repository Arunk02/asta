"""30 Sep, "make it prod daily usable": the gaps the audit found, closed.

- 3 of 16 code tasks died on "unknown workspace '<a repo name>'";
- IntelliJ, VS Code, Teams, Postman… have no scripting door, so do_in_app
  could only open them;
- nothing told him in the morning whether Asta could be relied on that day,
  or what it had learned.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app import store


@pytest.fixture
def booking(tmp_path, monkeypatch):
    from app import workspace_tools
    root = tmp_path / "booking-workspace"
    for name in ("telikos-activityplanworkflow-service", "telikos-booking-service",
                 "telikos-email-service"):
        (root / name / ".git").mkdir(parents=True)
    other = tmp_path / "empv3-tenant-intake"
    (other / ".git").mkdir(parents=True)
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": root, "empv3": other})
    return root


@pytest.mark.parametrize("said,ws", [
    ("telikos-activityplanworkflow-service", "booking"), ("activity-plan-service", "booking"),
    ("activityplan-worfklow-service", "booking"), ("booking-service", "booking"),
    ("booking-workspace", "booking"), ("empv3", "empv3"), ("asta", ""), ("billing", ""),
])
def test_a_repo_named_as_the_workspace_finds_its_workspace(booking, said, ws):
    from app import tasks
    assert tasks.resolve_workspace(said) == ws


def test_a_code_task_naming_a_repo_runs_in_its_workspace(booking):
    from app import tasks
    assert tasks.code_cwd("activity-plan-service") == str(booking)


# --- apps with no scripting door ------------------------------------------------------

@pytest.fixture
def idea(monkeypatch):
    from app import app_tasks, apps, screen
    monkeypatch.setenv("ASTA_APPS", "1")
    monkeypatch.setenv("ASTA_SCREEN", "1")
    plans, followed, looked = [], [], []

    async def ask(text):
        return plans.pop(0)

    async def look(process):
        looked.append(process)
        return 'menu bar: Edit > Find > Find in Files… ; window "booking – BookingController.java"'

    async def follow(process, steps, why=""):
        followed.append((process, [s.render() for s in steps]))
        if any("Nope" in s.target for s in steps):
            raise screen.ScreenError("step 1 (click Nope) did not come true")
        return {"ok": True, "process": process, "steps": [s.render() for s in steps], "why": why}

    async def open_app(name, asked=True):
        return {"ok": True}

    monkeypatch.setattr(app_tasks, "_ask_model", ask)
    monkeypatch.setattr(screen, "look", look)
    monkeypatch.setattr(screen, "follow", follow)
    monkeypatch.setattr(apps, "open_app", open_app)
    monkeypatch.setattr(app_tasks, "resolve", lambda n: (Path("/Applications/IntelliJ IDEA.app"), ""))
    monkeypatch.setattr(app_tasks, "dictionary", lambda path, goal="": "")
    return app_tasks, plans, followed, looked


def _steps(*targets, destructive=False):
    return {"steps": [{"do": "menu", "target": t, "expect": "window: Find in Files"} for t in targets],
            "changes": "opens Find in Files", "destructive": destructive, "question": ""}


def test_an_app_with_no_dictionary_is_driven_by_its_named_controls(idea):
    app_tasks, plans, followed, looked = idea
    plans.append(_steps("Edit > Find > Find in Files…"))
    out = asyncio.run(app_tasks.do("intellij", "open the find in files dialog"))
    assert "Done in IntelliJ IDEA on screen" in out and "Each step checked" in out
    assert looked == ["IntelliJ IDEA"] and followed[0][0] == "IntelliJ IDEA"


def test_a_failed_step_gets_one_fresh_look_then_stops(idea):
    app_tasks, plans, followed, looked = idea
    plans += [{"steps": [{"do": "click", "target": 'button "Nope"', "expect": "gone: x"}],
               "changes": "", "destructive": False},
              _steps("Edit > Find > Find in Files…")]
    assert "Done" in asyncio.run(app_tasks.do("intellij", "open find in files"))
    assert len(looked) == 2, "looked again before the retry"


def test_something_destructive_on_screen_waits_for_his_yes(idea):
    app_tasks, plans, followed, looked = idea
    plans.append({"steps": [{"do": "menu", "target": "Git > Rollback…", "expect": "window: Rollback"}],
                  "changes": "rolls back local changes", "destructive": False})
    out = asyncio.run(app_tasks.do("intellij", "discard my local changes"))
    assert "Say yes" in out and followed == []


# --- the morning: can he rely on it, and what did it learn ------------------------------

def test_readiness_says_when_claude_is_limited_and_what_is_waiting(monkeypatch):
    from app import briefing
    now = time.time()
    store.kv_set("claude_limited_until", str(now + 3600))
    store.kv_set("answers_queue", json.dumps([{"type": "answer", "who": "Vinish"}]))
    store.kv_set("attention_scrape:teams-chat", str(now - 60))
    line = briefing.readiness(now)
    assert line.startswith("⚠️ Not fully ready")
    assert "Claude is limited until" in line and "1 decision(s) waiting" in line and "Teams ✓" in line


def test_readiness_is_plainly_ready_when_it_is(monkeypatch):
    from app import briefing
    now = time.time()
    store.kv_set("attention_scrape:teams-chat", str(now - 60))
    store.kv_set("attention_scrape:outlook", str(now - 120))
    assert briefing.readiness(now).startswith("✅ Ready")


def test_what_it_learned_is_said_not_claimed():
    from app import briefing, ledger
    since = time.time() - 60
    store.record_outcome("skill", "written", subject="verify-repo-existence-before-adding-to-workspace")
    ledger.record("push", "teams-chat", "as_is")
    line = briefing.learned_line(since)
    assert "1 skill(s)" in line and "verify repo existence before adding to workspace" in line
    assert "1 of your decisions recorded" in line
    assert briefing.learned_line(time.time() + 60) == ""
