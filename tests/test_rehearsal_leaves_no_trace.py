"""A live rehearsal ("e2e" workspace) teaches nothing and pushes nothing.

17 Nov: scratch calc tasks had promoted four arithmetic skills into his live
skill index, and their plans reached his phone."""
from __future__ import annotations

import asyncio

from app import learn, notify, rehearsal, store, tasks


def _task(workspace):
    return store.create_task("calc: add subtract", "code", "p", workspace)["id"]


def test_only_e2e_workspaces_are_rehearsals():
    assert rehearsal.is_rehearsal(_task("e2e"))
    assert rehearsal.is_rehearsal(_task("e2e-check"))
    assert not rehearsal.is_rehearsal(_task("booking"))
    assert not rehearsal.is_rehearsal(None)


def test_a_rehearsal_teaches_no_skill_and_is_not_audited(monkeypatch):
    calls = []
    monkeypatch.setattr(learn, "should_extract", lambda *a, **k: calls.append("extract") or True)
    monkeypatch.setattr(learn, "credit", lambda *a, **k: calls.append("credit"))
    tid = _task("e2e")
    tasks._learn_from(tid, "calc", "done", "done")
    tasks.learn_from_stop(tid, store.get_task(tid), "cancelled", "test over")
    assert calls == []
    assert tasks._audit_note(tid) == ""


def test_a_real_task_still_learns(monkeypatch):
    calls = []
    monkeypatch.setattr(learn, "credit", lambda *a, **k: calls.append("credit"))
    monkeypatch.setattr(learn, "should_extract", lambda *a, **k: False)
    tid = _task("booking")
    store.update_task(tid, started_at=1.0)
    tasks._learn_from(tid, "real", "done", "done")
    assert calls == ["credit"]


def test_a_rehearsal_push_stays_in_the_bell(monkeypatch):
    sent = []

    async def wa(text, *a, **k):
        sent.append(text)
        return True
    monkeypatch.setattr(notify, "wa_send", wa)

    async def run():
        rehearsal.enter(_task("e2e"))
        return await notify.notify("plan ready for calc", "task", urgency="direct")
    out = asyncio.run(run())
    assert out.get("rehearsal") and not out["whatsapp"]
    assert sent == []
    assert not rehearsal.active()     # the run's context did not leak out
