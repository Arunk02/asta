""""…and inform Vinish" travels with the coding task and is sent once, at its milestone.

Teams is a fake: each test reads what WOULD have been typed, and to whom.
"""
from __future__ import annotations

import asyncio

import pytest

from app import answers, go, loop, ops, senior, steward, store, task_inform, tasks, teams_bridge

PR = "https://github.com/acme/booking/pull/1409"


@pytest.fixture
def teams(monkeypatch):
    sent: list[dict] = []
    state = {"opened": "Vinish Kumar", "fail": None}

    async def send(to="", text="", to_group=False):
        if state["fail"]:
            raise state["fail"]
        sent.append({"to": to, "text": text, "group": to_group,
                     "automatic": teams_bridge._automatic_send.get()})
        return f"✅ Sent to {state['opened']}."

    async def resolve(chat, allow_group=False):
        return {"asked": chat, "opened": state["opened"]}

    monkeypatch.setitem(ops.REGISTRY["teams_send"], "run", send)
    monkeypatch.setattr(teams_bridge, "resolve_target", resolve)
    monkeypatch.setattr(steward, "sends_held", lambda: False)
    monkeypatch.setattr(senior, "is_senior", lambda target: False)
    shown: list[int] = []

    async def present(task_id, t, answered=False):
        shown.append(task_id)
        return True

    monkeypatch.setattr(answers, "present_task_draft", present)
    return sent, state, shown


def code_task(words, status="shipped"):
    t = store.create_task("Retry fix", "code", "fix it", "booking")
    store.update_task(t["id"], status=status, pr_urls=PR)
    task_inform.record(t["id"], words)
    return t["id"]


@pytest.mark.parametrize("words, expected", [
    ("fix the retry, raise PR and inform Vinish", ("Vinish", "pr")),
    ("raise the PR, merge after CI and tell Vinish Kumar", ("Vinish Kumar", "merged")),
    ("raise PR and let Vinish know", ("Vinish", "pr")),
    ("fix it and don't tell Vinish", None),
    ("raise PR and inform the team", None),
    ("raise PR and tell me when done", None),
    ("raise PR and ping Vinish and Komal", None),
    ("fix the retry", None),
])
def test_only_one_named_person_in_his_words_is_kept(words, expected):
    assert task_inform.parse(words) == expected


def test_only_a_code_task_carries_it():
    t = store.create_task("Look", "analysis", "x", None)
    assert task_inform.record(t["id"], "check it and inform Vinish") is None


def test_told_once_when_the_pr_is_up_without_asking_again(teams):
    sent, _, shown = teams
    tid = code_task("raise PR and inform Vinish")
    line = asyncio.run(task_inform.reached(tid, "pr"))
    assert "Told Vinish" in line and shown == []
    assert len(sent) == 1 and sent[0]["to"] == "Vinish" and not sent[0]["group"]
    assert PR.split("/")[-1] in sent[0]["text"] and sent[0]["text"].startswith("Hi Vinish")
    assert sent[0]["automatic"] == "Vinish", "Asta's line, not his words"
    assert asyncio.run(task_inform.reached(tid, "pr")) is None and len(sent) == 1


def test_a_merge_instruction_waits_for_the_merge(teams):
    sent, _, _ = teams
    tid = code_task("raise PR, merge after CI and inform Vinish")
    assert asyncio.run(task_inform.reached(tid, "pr")) is None and sent == []
    assert "Told Vinish" in asyncio.run(task_inform.reached(tid, "merged"))
    assert "merged" in sent[0]["text"]


def test_the_pr_watcher_tells_them_when_it_merges(teams, monkeypatch):
    sent, _, _ = teams
    tid = code_task("merge after CI and inform Vinish")

    async def merged(url):
        return {"state": "MERGED", "mergedAt": "2026-10-27T17:00:00Z"}

    monkeypatch.setattr(tasks, "_pr_state", merged)
    line = asyncio.run(tasks.check_pr(tid))
    assert "Merged" in line and "Told Vinish" in line and len(sent) == 1
    assert asyncio.run(tasks.check_pr(tid)) is None and len(sent) == 1


def test_his_stop_cancels_it(teams):
    sent, _, _ = teams
    tid = code_task("raise PR and inform Vinish")
    assert go.command("don't tell Vinish") == (True, None)
    assert "won't tell Vinish" in asyncio.run(go.act("don't tell Vinish", None))
    assert asyncio.run(task_inform.reached(tid, "pr")) is None and sent == []


def test_a_cancelled_task_tells_nobody(teams):
    sent, _, _ = teams
    tid = code_task("raise PR and inform Vinish", status="cancelled")
    assert asyncio.run(task_inform.reached(tid, "pr")) is None and sent == []


@pytest.mark.parametrize("block", ["held", "senior", "other_chat"])
def test_a_refused_send_is_staged_for_his_yes_not_sent(teams, monkeypatch, block):
    sent, state, shown = teams
    if block == "held":
        monkeypatch.setattr(steward, "sends_held", lambda: True)
    elif block == "senior":
        monkeypatch.setattr(senior, "is_senior", lambda target: True)
    else:
        state["opened"] = "Vinisha Rao"
    tid = code_task("raise PR and inform Vinish")
    line = asyncio.run(task_inform.reached(tid, "pr"))
    assert sent == [] and "waiting for your yes" in line and shown == [tid]
    assert loop.take_task_send(tid)["to"] == "Vinish"


def test_an_uncertain_send_is_reported_and_never_resent(teams):
    sent, state, _ = teams
    tid = code_task("raise PR and inform Vinish")
    state["fail"] = RuntimeError("Teams page closed")
    assert "not confirmed" in asyncio.run(task_inform.reached(tid, "pr"))
    state["fail"] = None
    assert asyncio.run(task_inform.reached(tid, "pr")) is None and sent == []


def test_concurrent_milestones_send_once(teams):
    sent, _, _ = teams
    tid = code_task("raise PR and inform Vinish")

    async def both():
        return await asyncio.gather(task_inform.reached(tid, "pr"),
                                    task_inform.reached(tid, "merged"))

    asyncio.run(both())
    assert len(sent) == 1


def test_the_workers_own_draft_to_the_same_person_is_not_asked_again():
    tid = code_task("raise PR and inform Vinish")
    loop.set_task_send(tid, "Vinish, PR is up", "Vinish", "teams")
    assert asyncio.run(answers.present_task_draft(tid, store.get_task(tid))) is False


# --- his go-ahead, with the tell on the end -------------------------------------------

from app import main  # noqa: E402
from tests.test_say_it_once import _Sink, _conv, _no_brain, quiet  # noqa: E402,F401


def test_go_and_inform_on_a_running_task_is_kept_for_the_pr(monkeypatch, quiet, teams):
    sent, _, _ = teams
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    t = store.create_task("Enable MX", "code", "p", None)
    store.update_task(t["id"], status="running")
    sink = _Sink()
    asyncio.run(main._dispatch(_conv(), "push and raise PR and inform Vinish", sink, "whatsapp"))
    assert task_inform.get(t["id"])["person"] == "Vinish" and sent == []
    assert "I'll tell Vinish once the PR is up" in str(sink.sent)


def test_go_and_inform_on_a_finished_task_ships_then_tells_once(monkeypatch, quiet, teams):
    sent, _, _ = teams

    async def ship(tid):
        store.update_task(tid, status="shipped", pr_urls=PR)
        return f"🔀 Task #{tid} shipped"

    monkeypatch.setattr(tasks, "ship", ship)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    t = store.create_task("Enable MX", "code", "p", None)
    store.update_task(t["id"], status="done", result="Implemented")
    sink = _Sink()
    asyncio.run(main._dispatch(_conv(), "push and raise PR and inform Vinish", sink, "whatsapp"))
    assert len(sent) == 1 and sent[0]["to"] == "Vinish"
    assert "Told Vinish" in str(sink.sent)


def test_a_tell_with_no_task_still_goes_to_the_brain(monkeypatch, quiet):
    turns = []
    monkeypatch.setattr(main, "_start_turn", lambda conv, text, sink, channel: turns.append(text))

    async def rerun(text):
        return "re-ran the failed jobs"

    from app import go as go_mod
    monkeypatch.setattr(go_mod, "rerun", rerun)
    asyncio.run(main._dispatch(_conv(), "rerun it and inform Vinish", _Sink(), "whatsapp"))
    assert turns and turns[0].startswith("inform Vinish")
