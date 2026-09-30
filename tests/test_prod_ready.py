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
    monkeypatch.setattr(app_tasks, "resolve", lambda n: (Path("/Applications/Postman.app"), ""))
    monkeypatch.setattr(app_tasks, "dictionary", lambda path, goal="": "")
    return app_tasks, plans, followed, looked


def _steps(*targets, destructive=False):
    return {"steps": [{"do": "menu", "target": t, "expect": "window: Find in Files"} for t in targets],
            "changes": "opens Find in Files", "destructive": destructive, "question": ""}


def test_an_app_with_no_dictionary_is_driven_by_its_named_controls(idea):
    app_tasks, plans, followed, looked = idea
    plans.append(_steps("Edit > Find > Find in Files…"))
    out = asyncio.run(app_tasks.do("postman", "open the find in files dialog"))
    assert "Done in Postman on screen" in out and "Each step checked" in out
    assert looked == ["Postman"] and followed[0][0] == "Postman"


def test_a_failed_step_gets_one_fresh_look_then_stops(idea):
    app_tasks, plans, followed, looked = idea
    plans += [{"steps": [{"do": "click", "target": 'button "Nope"', "expect": "gone: x"}],
               "changes": "", "destructive": False},
              _steps("Edit > Find > Find in Files…")]
    assert "Done" in asyncio.run(app_tasks.do("postman", "open find in files"))
    assert len(looked) == 2, "looked again before the retry"


def test_something_destructive_on_screen_waits_for_his_yes(idea):
    app_tasks, plans, followed, looked = idea
    plans.append({"steps": [{"do": "menu", "target": "Git > Rollback…", "expect": "window: Rollback"}],
                  "changes": "rolls back local changes", "destructive": False})
    out = asyncio.run(app_tasks.do("postman", "discard my local changes"))
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


def test_a_step_the_app_rejects_is_retried_not_a_crash(idea, monkeypatch):
    from app import apps, screen
    app_tasks, plans, followed, looked = idea
    calls = []

    async def follow(process, steps, why=""):
        calls.append(1)
        if len(calls) == 1:
            raise apps.AppError("System Events got an error: Can't get menu item "
                                "\"Find in Files...\" (-1728)")
        return {"ok": True, "process": process, "steps": [s.render() for s in steps]}

    monkeypatch.setattr(screen, "follow", follow)
    plans += [_steps("Edit > Find in Files..."), _steps("Edit > Find > Find in Files…")]
    assert "Done" in asyncio.run(app_tasks.do("postman", "open find in files"))
    assert len(calls) == 2


def test_a_menu_of_any_depth_is_clicked_with_its_names_as_arguments():
    from app import screen
    script = screen._menu_script("IntelliJ IDEA", ["Edit", "Find", "Find in Files…"])
    assert "click menu item (item 3 of argv) of menu 1 of menu item (item 2 of argv) of menu 1 " \
           "of menu bar item (item 1 of argv) of menu bar 1" in script
    assert "…" not in script and "\\u2026" not in script, "names travel as arguments"


def test_named_keys_are_key_codes_not_typed_words(monkeypatch):
    from app import apps, screen
    monkeypatch.setenv("ASTA_SCREEN", "1")
    ran = []

    async def osa(script, args):
        ran.append((script, args))
        return "window \"Find in Files\"" if "entire contents" in script or "UI elements" in script else ""

    async def look(process):
        return ""

    monkeypatch.setattr(apps, "_osascript", osa)
    monkeypatch.setattr(screen, "look", look)
    monkeypatch.setattr(screen, "_met", lambda expect, seen: True)
    monkeypatch.setattr(apps, "RECIPES", {})
    asyncio.run(screen.follow("IntelliJ IDEA", [
        screen.Step("menu", "Edit > Find > Find in Files…", "window: Find in Files"),
        screen.Step("key", "escape", "gone: Find in Files")]))
    menu, key = ran[0], ran[1]
    assert menu[1] == ["Edit", "Find", "Find in Files…"]
    assert "key code 53" in key[0]


def test_a_big_ide_is_read_the_fast_way_and_a_slow_app_is_remembered(monkeypatch):
    from app import apps, screen
    monkeypatch.setenv("ASTA_SCREEN", "1")
    scripts = []

    async def osa(script, args):
        scripts.append(script)
        if "entire contents" in script:
            raise apps.AppError("the app did not answer within 25s")
        return "menu: Edit\n  Edit > Find\n"

    monkeypatch.setattr(apps, "_osascript", osa)
    assert "Edit > Find" in asyncio.run(screen.look("IntelliJ IDEA"))
    assert "entire contents" not in scripts[0], "an IDE is never walked element by element"
    scripts.clear()
    asyncio.run(screen.look("Postman"))
    assert "entire contents" in scripts[0] and "every menu item" in scripts[1]
    scripts.clear()
    asyncio.run(screen.look("Postman"))
    assert len(scripts) == 1 and "every menu item" in scripts[0], "remembered as big"


@pytest.mark.parametrize("expect,seen,ok", [
    ("windows: 2", "windows: 2\nmenu: Edit\n", True),
    ("windows: 2", "windows: 1\nelement: 2 problems\n", False),
    ("window: Find in Files", "windows: 2\nwindow: Find in Files\n", True),
    ("gone: Find in Files", "windows: 1\n", True),
])
def test_a_window_count_is_a_check_that_works_on_java_apps(expect, seen, ok):
    from app import screen
    assert screen._met(expect, seen) is ok


@pytest.fixture
def ide(tmp_path, monkeypatch):
    from app import app_tasks, walkthrough, workspace_tools
    root = tmp_path / "booking-workspace"
    src = root / "telikos-booking-service" / "src" / "main" / "java" / "x"
    src.mkdir(parents=True)
    (root / "telikos-booking-service" / ".git").mkdir()
    (src / "BookingController.java").write_text("class BookingController {}")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": root})
    monkeypatch.setenv("ASTA_APPS", "1")
    ran = []

    async def spawn(*args, **kw):
        ran.append(list(args))

        class P:
            async def wait(self):
                return 0
        return P()

    monkeypatch.setattr(app_tasks.asyncio if hasattr(app_tasks, "asyncio") else __import__("asyncio"),
                        "create_subprocess_exec", spawn)
    monkeypatch.setattr(app_tasks, "resolve", lambda n: (Path("/Applications/IntelliJ IDEA.app"), ""))
    return root, ran


def test_intellij_opens_a_file_at_a_line_through_its_launcher(ide):
    from app import app_tasks, walkthrough
    root, ran = ide
    out = asyncio.run(app_tasks.do("intellij", "open BookingController.java line 88"))
    assert "Opened" in out and "BookingController.java:88" in out
    assert ran[0][0] == walkthrough.IDEA and "--line" in ran[0] and "88" in ran[0]


def test_intellij_opens_a_project_by_its_repo_name(ide):
    from app import app_tasks
    root, ran = ide
    out = asyncio.run(app_tasks.do("intellij", "open project booking-service"))
    assert "Opened telikos-booking-service" in out


def test_code_work_in_intellij_becomes_a_code_task_not_clicks(ide):
    from app import app_tasks
    root, ran = ide
    out = asyncio.run(app_tasks.do("intellij", "refactor the priority service"))
    assert "code task" in out and ran == []
