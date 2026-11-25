"""25 Nov, live rehearsal: "add a docstring to subtract in calc.py", sent while
the subtract task waited at its gate, became a new task. A follow-up about what
the live task is about is about the task — whichever gate it waits at."""
from __future__ import annotations

import asyncio

import pytest

from app import main, offers, store, tasks


class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)


@pytest.fixture
def live_task(monkeypatch):
    offers.clear()
    conv = store.create_conversation(model="copilot", workspace=None)
    conv["model"] = "copilot"
    t = store.create_task("Add subtract(a, b) to calc.py with test", "code",
                          "In calc.py add subtract(a, b) and a test in test_calc.py", None)
    store.update_task(t["id"], status="awaiting_approval")
    tasks.link_task(conv["id"], t["id"])
    noted = []

    async def note(task_id, text, **kw):
        noted.append((task_id, text))
        return f"noted for #{task_id}"
    spawned = []
    monkeypatch.setattr(tasks, "note_for_live_task", note)
    monkeypatch.setattr(tasks, "spawn", lambda *a, **k: spawned.append(a) or {"id": 999, "title": "x"})
    monkeypatch.setattr(main, "_conversation_is_on_task", lambda cid, tid: False)
    return conv, t["id"], noted, spawned


def test_a_follow_up_on_the_live_tasks_subject_is_added_to_it(live_task, monkeypatch):
    conv, tid, noted, spawned = live_task
    monkeypatch.setattr(tasks, "live_tasks_for", lambda cid: [tid])
    asyncio.run(main._dispatch(conv, "add a docstring to subtract in calc.py", _Sink(), "web"))
    assert noted and noted[0][0] == tid
    assert spawned == []


def test_other_work_still_starts_its_own_task(live_task, monkeypatch):
    conv, tid, noted, spawned = live_task
    monkeypatch.setattr(tasks, "live_tasks_for", lambda cid: [tid])
    assert not tasks.same_subject(tid, "fix the flaky readyForPlanning test in AP")
    assert not tasks.same_subject(tid, "ABC-91 bump the kafka client version")
