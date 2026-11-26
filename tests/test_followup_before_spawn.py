"""25 Nov, live (rehearsal #752 → #753): "add a docstring to subtract in calc.py",
right after the subtract task finished, became a new task. The work route spawned
before the "recently finished" branch could place it. Through the real dispatcher."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import main, offers, store, tasks


class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)


@pytest.fixture
def finished(monkeypatch):
    offers.clear()
    conv = store.create_conversation(model="claude_cli", workspace="e2e")
    conv["model"] = "claude_cli"
    t = store.create_task("In calc.py add subtract(a, b) and a test for it in test_calc.py", "code",
                          "In calc.py add subtract(a, b) and a test for it in test_calc.py, "
                          "in the style of add().", "e2e")
    store.update_task(t["id"], status="done", finished_at=time.time())
    tasks.link_task(conv["id"], t["id"])
    calls = {"spawn": [], "refine": []}

    def spawn(title, prompt, kind, ws, **k):
        calls["spawn"].append(prompt)
        return {"id": 999, "title": title}

    async def refine(task_id, text, **k):
        calls["refine"].append((task_id, text))
        return f"Task #{task_id}: amendment coming"
    monkeypatch.setattr(tasks, "spawn", spawn)
    monkeypatch.setattr(tasks, "refine", refine)
    monkeypatch.setattr(main, "_workspace_repos", lambda ws: ("calc",))
    return conv, t["id"], calls


def _say(conv, text):
    asyncio.run(main._dispatch(conv, text, _Sink(), "web"))


def test_a_follow_up_on_the_finished_task_continues_it(finished):
    conv, tid, calls = finished
    _say(conv, "add a docstring to subtract in calc.py")
    assert calls["refine"] == [(tid, "add a docstring to subtract in calc.py")]
    assert calls["spawn"] == []


def test_unrelated_work_still_starts_a_new_task(finished):
    conv, tid, calls = finished
    _say(conv, "Implement a parser for config.yaml in the calc repo")
    assert calls["spawn"] and calls["refine"] == []


def test_new_task_words_still_start_a_new_task(finished):
    conv, tid, calls = finished
    _say(conv, "new task: Add a power(base, exp) function to mathutils.py in the calc repo")
    assert calls["spawn"] and calls["refine"] == []
