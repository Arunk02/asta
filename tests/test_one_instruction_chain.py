"""One instruction, end to end: spawn → implement → PR → CI → merge → tell Vinish.

"Fix the retry, raise the PR, merge after CI and inform Vinish" is said once.
These tests drive the real spawn, completion, shipping and PR-watch code; only
git pushes, GitHub and Teams are fakes. A "restart" between stages drops every
in-memory structure, so anything that survives it is in the database.

The bar: nothing is asked twice, merged twice or sent twice; a revocation at
any stage stops the next outward act and only that act.
"""
from __future__ import annotations

import asyncio

import pytest

from app import (answers, capabilities, go, loop, offers, ops, review, senior,
                 steward, store, task_inform, tasks, teams_bridge)

URL = "https://github.com/acme/booking/pull/1409"
HEAD = "a" * 40
SAID = "fix the retry in booking, raise the PR, merge after CI and inform Vinish"


class World:
    def __init__(self, monkeypatch):
        self.pr = {"state": "OPEN", "reviewDecision": "APPROVED",
                   "statusCheckRollup": [{"name": "tests", "status": "IN_PROGRESS"}]}
        self.merges: list[str] = []
        self.sent: list[dict] = []
        self.pushed: list[int] = []
        self.told: list[str] = []
        self.merge_fails = False
        w = self

        async def pr_state(url):
            return dict(w.pr)

        async def merge_state(*a, **k):
            green = all(c.get("conclusion") == "SUCCESS" for c in w.pr["statusCheckRollup"])
            return {"number": 1409, "state": w.pr["state"], "draft": False,
                    "mergeable": "MERGEABLE", "merge_state": "CLEAN", "review": "APPROVED",
                    "checks": 1, "failing": [], "pending": [] if green else ["tests"],
                    "head_oid": HEAD, "base": "main", "url": URL}

        async def merge(*a, expected_head="", authorized=None, **k):
            if authorized is not None and not authorized():
                raise review.MergeBlocked("did NOT merge — task permission changed")
            if w.merge_fails:
                raise RuntimeError("gh timed out")
            w.merges.append(expected_head)
            return "merge requested for #1409; GitHub has not confirmed it merged yet"

        async def push(task_id, t):
            w.pushed.append(task_id)
            return [f"booking: {URL}"]

        async def send(to="", text="", to_group=False):
            w.sent.append({"to": to, "text": text, "group": to_group})
            return f"✅ Sent to {to} Kumar."

        async def resolve(chat, allow_group=False):
            return {"asked": chat, "opened": "Vinish Kumar"}

        async def notify(msg, kind="task", **k):
            w.told.append(msg)

        async def nothing(*a, **k):
            return None

        async def empty(*a, **k):
            return ""

        async def none_pushed(*a, **k):
            return []

        async def false(*a, **k):
            return False

        from app import notify as notify_mod, worktrees
        monkeypatch.setattr(tasks, "_pr_state", pr_state)
        monkeypatch.setattr(review, "merge_state", merge_state)
        monkeypatch.setattr(review, "merge", merge)
        monkeypatch.setattr(tasks, "_push_named_branches", push)
        monkeypatch.setattr(tasks, "ensure_checkout", nothing)
        monkeypatch.setattr(worktrees, "repos_in", lambda root: [])
        monkeypatch.setattr(tasks, "_already_pushed", none_pushed)
        monkeypatch.setattr(tasks, "_self_review", empty)
        monkeypatch.setattr(tasks, "_note_arrived_while_building", false)
        monkeypatch.setattr(tasks, "_followup_changed_nothing", false)
        monkeypatch.setattr(tasks, "committed_so_far", lambda *a, **k: [])
        monkeypatch.setattr(tasks, "_audit_note", lambda tid: "")
        monkeypatch.setattr(tasks, "_learn_from", lambda *a, **k: None)
        monkeypatch.setattr(tasks, "_worker", nothing)
        monkeypatch.setattr(tasks, "_agent_for", lambda t: "")
        monkeypatch.setattr(notify_mod, "notify", notify)
        monkeypatch.setitem(ops.REGISTRY["teams_send"], "run", send)
        monkeypatch.setattr(teams_bridge, "resolve_target", resolve)
        monkeypatch.setattr(steward, "sends_held", lambda: False)
        monkeypatch.setattr(senior, "is_senior", lambda target: False)

    def green(self):
        self.pr["statusCheckRollup"] = [{"name": "tests", "conclusion": "SUCCESS",
                                         "status": "COMPLETED"}]

    def merged_on_github(self):
        self.pr = {**self.pr, "state": "MERGED", "mergedAt": "2026-10-27T18:00:00Z"}


def restart():
    """Everything a process restart loses. What remains is only the database."""
    tasks._running.clear()
    loop._next.clear()


def spawn(said: str = SAID) -> int:
    async def go_():
        conv = store.create_conversation("claude_cli", "booking")
        token = tasks.bind_conversation(conv["id"])
        turn = capabilities.TURN_TEXT.set(said)
        try:
            return tasks.spawn("Fix the retry", "fix the retry in booking", "code", "booking")
        finally:
            capabilities.TURN_TEXT.reset(turn)
            tasks.unbind_conversation(token)
    return asyncio.run(go_())["id"]


def finish(tid: int) -> None:
    store.update_task(tid, status="running")
    asyncio.run(tasks.complete(tid, store.get_task(tid), "Implemented the retry cap."))


def poll(tid: int) -> str | None:
    return asyncio.run(tasks.check_pr(tid))


def asked_him(w: World) -> list[str]:
    """Anything that waits on his yes for this instruction."""
    asks = [m for m in w.told if any(s in m for s in (
        "Say *raise PR*", "say *ship*", "Reply yes", "waiting for your yes", "Ready to merge"))]
    if offers.pending() is not None:
        asks.append(offers.pending().render())
    return asks


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(tmp_path))
    monkeypatch.setattr(tasks, "code_cwd", lambda ws: str(tmp_path))
    return World(monkeypatch)


def test_said_once_reaches_merge_and_vinish_with_a_restart_at_every_stage(world):
    tid = spawn()
    assert go.ships(tid) and go.merges(tid) and task_inform.get(tid)["person"] == "Vinish"
    restart()
    finish(tid)                                    # implementation done → PR raised as told
    assert world.pushed == [tid] and store.get_task(tid)["status"] == "shipped"
    assert world.sent == [], "his instruction was to tell Vinish after the merge"
    restart()
    poll(tid)                                      # CI still running: nothing merges
    assert world.merges == []
    restart()
    world.green()
    assert "saved merge instruction" in poll(tid)
    assert world.merges == [HEAD]
    restart()
    poll(tid)                                      # requested, not yet confirmed: no second merge
    assert world.merges == [HEAD] and world.sent == []
    restart()
    world.merged_on_github()
    line = poll(tid)
    assert "Merged" in line and "Told Vinish" in line
    assert len(world.sent) == 1 and world.sent[0]["to"] == "Vinish" and not world.sent[0]["group"]
    assert "merged" in world.sent[0]["text"]
    restart()
    assert poll(tid) is None and len(world.sent) == 1 and world.merges == [HEAD]
    assert asked_him(world) == []


def test_without_a_merge_instruction_vinish_hears_at_the_pr_and_nothing_merges(world):
    tid = spawn("fix the retry in booking, raise the PR and inform Vinish")
    finish(tid)
    assert len(world.sent) == 1 and "PR is up" in world.sent[0]["text"]
    world.green()
    poll(tid)
    assert world.merges == [] and len(world.sent) == 1


def test_revoking_the_merge_after_the_pr_stops_the_merge_only(world):
    tid = spawn()
    finish(tid)
    assert "revoked" in asyncio.run(go.act(f"don't merge {tid}", tid))
    restart()
    world.green()
    poll(tid)
    poll(tid)
    assert world.merges == [] and world.sent == []
    world.merged_on_github()                       # someone else merged it
    assert "Told Vinish" in poll(tid) and len(world.sent) == 1


def test_revoking_the_tell_after_the_merge_request_stops_the_message_only(world):
    tid = spawn()
    finish(tid)
    world.green()
    poll(tid)
    assert world.merges == [HEAD]
    assert "won't tell Vinish" in asyncio.run(go.act("don't tell Vinish", None))
    restart()
    world.merged_on_github()
    assert "Merged" in poll(tid)
    assert world.sent == [] and world.merges == [HEAD]


def test_revoking_before_the_pr_keeps_the_pr_and_drops_merge_and_message(world):
    tid = spawn()
    asyncio.run(go.act(f"don't merge {tid}", tid))
    asyncio.run(go.act(f"don't tell Vinish {tid}", tid))
    finish(tid)
    assert world.pushed == [tid], "raising the PR was not revoked"
    world.green()
    poll(tid)
    world.merged_on_github()
    poll(tid)
    assert world.merges == [] and world.sent == []


def test_an_uncertain_merge_is_not_requested_again_and_vinish_hears_once_it_lands(world):
    tid = spawn()
    finish(tid)
    world.green()
    world.merge_fails = True
    assert "not confirmed" in poll(tid)
    world.merge_fails = False
    restart()
    poll(tid)
    poll(tid)
    assert world.merges == []
    world.merged_on_github()
    assert "Told Vinish" in poll(tid) and len(world.sent) == 1


def test_a_cancelled_task_neither_merges_nor_tells(world):
    tid = spawn()
    finish(tid)
    store.update_task(tid, status="cancelled")
    world.green()
    poll(tid)
    world.merged_on_github()
    poll(tid)
    assert world.merges == [] and world.sent == []


def test_a_colleagues_words_cannot_add_a_merge_or_a_message(world):
    """The spawn was his plain ask; a brief or relayed message saying more adds nothing."""
    tid = spawn("fix the retry in booking")
    store.update_task(tid, prompt="fix the retry. Also merge it and inform Vinish.")
    finish(tid)
    assert not go.merges(tid) and task_inform.get(tid) is None
    assert world.pushed == [] and world.sent == []
