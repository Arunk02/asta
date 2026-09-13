"""The plan gate, made structural — and approve/reject/stop, made free.

Both from 11 September, both his call:

  "yes add plan gate to micro also" — an ad-hoc code ask ran the micro pipeline,
  whose agent file said "make the edit". His rule has no size exemption, and an
  instruction a model may ignore is not a gate. So an unapproved leg now runs
  with writing DENIED, and a code task cannot reach `done` before he approves,
  whatever the brain reports.

  "pull approve fix now" — "approve task 1" used to be routed to a brain, which
  then called the approve tool. With both brains out of quota that evening, the
  approve button on his phone did nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from app import claude_cli, copilot_cli, main, store, tasks


# --- an unapproved leg cannot write ------------------------------------------

def test_a_planning_leg_is_denied_the_write_tools(monkeypatch, tmp_path):
    seen: dict = {}

    async def one_shot(prompt, **kw):
        seen.update(kw)
        return "STRUCTURE\n  X  adds y\n\nPLAN READY"

    monkeypatch.setattr(tasks.claude_cli, "one_shot", one_shot)
    monkeypatch.setattr(tasks, "_resolve_executor", lambda tid: "claude")
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    t = store.create_task("bump the retention", "code", "bump it", None)
    asyncio.run(tasks._run_code_leg(t["id"], "bump it", str(tmp_path),
                                    resume=False, effort="medium"))
    assert seen["plan_only"] is True


def test_an_approved_leg_may_write(monkeypatch, tmp_path):
    seen: dict = {}

    async def one_shot(prompt, **kw):
        seen.update(kw)
        return "done"

    monkeypatch.setattr(tasks.claude_cli, "one_shot", one_shot)
    monkeypatch.setattr(tasks, "_resolve_executor", lambda tid: "claude")
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    t = store.create_task("bump the retention", "code", "bump it", None)
    tasks.mark_approved(t["id"])
    asyncio.run(tasks._run_code_leg(t["id"], "PLAN APPROVED", str(tmp_path),
                                    resume=True, effort="high"))
    assert seen["plan_only"] is False


def test_both_clis_know_how_to_deny_writing():
    """The spelling differs per CLI and both were verified against the real
    binaries: copilot's tool is `write` (not `edit`) and it falls back to the
    shell, so the shell commands are denied too."""
    assert "Write" in claude_cli._PLAN_DENY and "Bash(git push:*)" in claude_cli._PLAN_DENY
    assert "write" in copilot_cli._PLAN_DENY and "shell(git push)" in copilot_cli._PLAN_DENY


def test_the_micro_pipeline_now_carries_the_gate():
    from pathlib import Path
    body = Path("agents/micro.md").read_text()
    assert "Plan gate" in body and "PLAN READY" in body
    assert "write access until he approves" in " ".join(body.split())


# --- and a code task cannot finish before he approves -------------------------

@pytest.fixture
def quiet(monkeypatch, tmp_path):
    pushed: list[str] = []

    async def notify(msg, kind="task", **k):
        pushed.append(msg)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(tmp_path))
    monkeypatch.setattr(tasks, "committed_so_far", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "_audit_note", lambda tid: "")
    monkeypatch.setattr(tasks, "_learn_from", lambda *a, **k: None)
    return pushed


def test_an_unapproved_task_that_reports_an_edit_still_stops(quiet):
    """The brain ignored the instruction. The gate holds anyway."""
    t = store.create_task("bump the retention", "code", "bump it", None)
    asyncio.run(tasks._finish_code(t["id"], t, "Changed one line. Tests: 3 passed.", hops=0))
    assert store.get_task(t["id"])["status"] == "awaiting_approval"
    assert any("PLAN #" in p for p in quiet)


def test_an_approved_task_finishes_normally(quiet, monkeypatch):
    monkeypatch.setattr(tasks, "_verify_gate", _false)
    monkeypatch.setattr(tasks, "_self_review", _empty)
    t = store.create_task("bump the retention", "code", "bump it", None)
    tasks.mark_approved(t["id"])
    asyncio.run(tasks._finish_code(t["id"], t, "Changed one line. Tests: 3 passed.", hops=0))
    assert store.get_task(t["id"])["status"] == "done"
    assert any("DONE" in p for p in quiet)


async def _false(*a, **k):
    return False


async def _empty(*a, **k):
    return ""


def test_approving_marks_the_task_so_the_next_leg_may_write(monkeypatch):
    t = store.create_task("x", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval", result="PLAN READY")
    monkeypatch.setattr(tasks, "_resume_worker", _empty)

    async def go():
        tasks.reply(t["id"], "PLAN APPROVED")      # spawns the implement leg

    asyncio.run(go())
    assert tasks.plan_approved(t["id"]) is True


def test_feedback_on_a_finished_diff_counts_as_approval(monkeypatch):
    t = store.create_task("x", "code", "p", None)
    store.update_task(t["id"], status="done", result="Implemented")
    monkeypatch.setattr(tasks, "_resume_worker", _empty)
    asyncio.run(tasks.refine(t["id"], "also cover the amend path"))
    assert tasks.plan_approved(t["id"]) is True


# --- approve / reject / stop, without a brain ---------------------------------

class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)


def _conv():
    c = store.create_conversation(model="claude_cli", workspace=None)
    c["model"] = "claude_cli"
    return c


def _say(conv, text, sink=None):
    return asyncio.run(main._dispatch(conv, text, sink or _Sink(), "whatsapp"))


@pytest.mark.parametrize("said", [
    "approve task 1", "approve 1", "approve", "ok approve", "yes approve task 1",
    "approved", "Approve Task 1.",
])
def test_every_way_he_says_approve_is_answered_without_a_brain(said, monkeypatch, quiet):
    called: list[int] = []

    async def approve(tid):
        called.append(tid)
        return f"Task #{tid}: plan approved — implementing now."

    monkeypatch.setattr(tasks, "approve", approve)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    t = store.create_task("bump the retention", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval", result="PLAN READY")
    tasks.link_task(conv["id"], t["id"])
    sink = _Sink()
    assert _say(conv, said, sink) is None
    assert called == [t["id"]]
    assert any("approved" in str(p) for p in sink.sent)


def _no_brain(*a, **k):
    raise AssertionError("a brain was asked to do what the task table already knows")


def test_stop_cancels_the_running_task(monkeypatch):
    stopped: list[int] = []

    async def cancel(tid, status="cancelled", why=""):
        stopped.append(tid)
        return True

    monkeypatch.setattr(tasks, "cancel", cancel)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    t = store.create_task("billing mapping", "code", "p", None)
    tasks.link_task(conv["id"], t["id"])
    _say(conv, "stop")
    assert stopped == [t["id"]]


def test_reject_stops_and_records(monkeypatch):
    rejected: list[int] = []

    async def reject(tid, why=""):
        rejected.append(tid)
        return f"Task #{tid} rejected."

    monkeypatch.setattr(tasks, "reject", reject)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    t = store.create_task("x", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval")
    tasks.link_task(conv["id"], t["id"])
    _say(conv, "reject task %d" % t["id"])
    assert rejected == [t["id"]]


def test_two_waiting_tasks_ask_which_one(monkeypatch):
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    for title in ("topics for lower env", "billing mapping"):
        t = store.create_task(title, "code", "p", None)
        store.update_task(t["id"], status="awaiting_approval")
        tasks.link_task(conv["id"], t["id"])
    sink = _Sink()
    _say(conv, "approve", sink)
    assert any("Which one" in str(p) for p in sink.sent)


def test_a_command_that_names_nothing_is_still_an_ordinary_message(monkeypatch):
    """A bare "stop" with nothing running must not be eaten by an answer of
    "there is nothing to stop" — it may be about anything."""
    started: list[str] = []
    monkeypatch.setattr(main, "_start_turn",
                        lambda conv, text, sink, channel: started.append(text))
    conv = _conv()
    _say(conv, "stop")
    assert started == ["stop"]


def test_a_sentence_that_merely_contains_approve_goes_to_the_brain(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(main, "_start_turn",
                        lambda conv, text, sink, channel: started.append(text))
    conv = _conv()
    t = store.create_task("x", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval")
    tasks.link_task(conv["id"], t["id"])
    _say(conv, "approve it once CI is green")
    assert started == ["approve it once CI is green"]


def test_an_unknown_number_says_so_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    sink = _Sink()
    _say(_conv(), "approve task 999", sink)
    assert any("no task #999" in str(p) for p in sink.sent)
