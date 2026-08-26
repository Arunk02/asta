"""Handing over code work must reach the task lane, not a 300s chat turn.

From the transcript that started this: Asta implementing a VTS ETA validation in
chat, narrating a dozen real steps, then

    RuntimeError: Copilot CLI turn timed out after 300s

Its own instruction already said "When Arun assigns work … delegate it as a
background task right away" and "Never plan or implement in chat yourself". It
implemented anyway. An instruction the model may or may not follow is not a
routing decision.
"""

from __future__ import annotations

import pytest

from app import work_intent


# --- what counts as handing over work ----------------------------------------

@pytest.mark.parametrize("text", [
    "implement the retry logic in booking",
    "Fix the vessel ETA validation",
    "please add a null check to the mapper",
    "refactor the booking mapper",
    "BEPTELIKOS-10159 add the ETA guard before port gate-in",
    "remove the dead flag from the config",
])
def test_these_are_work(text):
    assert work_intent.is_work_assignment(text), text


@pytest.mark.parametrize("text", [
    # Questions about code are not requests to change it.
    "how do I fix this",
    "why does the booking build fail",
    "what is the ci status of above PR",
    "explain how the vessel dates are stored",
    "who implemented this",
    # Question-shaped requests keep the old path rather than guessing.
    "can you implement the retry?",
    "would it be possible to add a retry",
    # Deliberation, not assignment.
    "how would you fix this",
    "do you think we should refactor the mapper",
    # Other verbs that have their own flows.
    "review PR 123 in booking",
    "trace booking 88271",
    "check the logs for errors",
    "read my chat with Vinish",
])
def test_these_are_not_work(text):
    assert not work_intent.is_work_assignment(text), text


@pytest.mark.parametrize("text", [
    "update me on the PR",
    "update me",
    "add me to the review",
    "fix a time for the call",
    "remove me from that thread",
    "extend the meeting by 30 mins",
    "drop the call",
    "delete that message",
    "update the ticket status to done",
    "change my status to busy",
    "reply to Vinish and add that I'll be late",
])
def test_other_flows_are_never_hijacked(text):
    """Every one of these leads with a listed work verb and none is code work.

    Found by probing rather than by review: the first version of this classifier
    routed all of them to code tasks, which would have turned "change my status
    to busy" into a spawned repo change. A work verb alone is nowhere near
    enough — the message has to be recognisably about code.
    """
    assert not work_intent.is_work_assignment(text), text


@pytest.mark.parametrize("text", [
    "implement it",
    "fix it",
    "add that",
])
def test_a_bare_verb_with_no_code_evidence_falls_through(text):
    """These probably ARE work, and still must not route.

    Nothing in them says "code", so routing would be a guess. They take the chat
    path — which can no longer edit files, so the model has to delegate from
    there anyway. Defence in depth is what makes the narrow classifier safe.
    """
    assert not work_intent.is_work_assignment(text), text


def test_naming_a_repo_counts_as_code_evidence():
    repos = ("telikos-booking-service", "telikos-email-service")
    assert work_intent.is_work_assignment(
        "fix the null check in telikos-booking-service", repos)
    # …but only when the verb is actually assigning work.
    assert not work_intent.is_work_assignment(
        "update me on telikos-booking-service", repos)


def test_a_ticket_key_overrides_the_other_flow_guard():
    """"comment on PROJ-1 and fix the NPE" is still code work."""
    assert work_intent.is_work_assignment(
        "BEPTELIKOS-10159 fix the NPE and update the status")


def test_the_bias_is_towards_missing_not_over_claiming():
    """A plan he did not ask for erodes trust; a missed route is merely the old,
    slower path. So anything ambiguous must fall through."""
    for text in ("maybe fix the mapper at some point?",
                 "should I fix this myself",
                 "is fixing this worth it"):
        assert not work_intent.is_work_assignment(text), text


def test_an_empty_or_essay_length_message_is_not_work():
    assert not work_intent.is_work_assignment("")
    assert not work_intent.is_work_assignment("implement " + ("x " * 400))


def test_the_title_carries_his_own_words_and_the_ticket():
    """He has to recognise it in a task list."""
    title = work_intent.title_for("add the ETA guard for BEPTELIKOS-10159")
    assert "BEPTELIKOS-10159" in title
    assert "ETA guard" in title
    assert len(title) <= 70


# --- the routing itself ------------------------------------------------------

class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        pass


def _dispatch(text, monkeypatch, *, workspace="booking", spawn=None, live=()):
    """Run the real dispatcher with the brain and the task engine stubbed."""
    import asyncio as _a
    from app import main, tasks, work_intent as wi

    started = {}

    def _no_turn(conv, user_text, sink, channel):
        started["chat_turn"] = user_text
        return "a-chat-turn"

    monkeypatch.setattr(main, "_start_turn", _no_turn)
    monkeypatch.setattr(main, "_workspace_repos", lambda ws: ("telikos-booking-service",))
    monkeypatch.setattr(tasks, "live_tasks_for", lambda cid: list(live))

    # Steering a live task is its own path and reaches a brain to classify the
    # message. Stubbed to "not about that task" so this stays a test of routing
    # versus not-routing. Without it the live-task case tripped conftest's
    # no-live-brains guard on CI — and passed locally, which is the worst kind of
    # difference to leave in a suite.
    async def _not_about_the_task(*a, **k):
        return False

    monkeypatch.setattr(main, "_route_to_task", _not_about_the_task)
    monkeypatch.setattr(tasks, "refinable_for", lambda cid: [])
    monkeypatch.setattr(tasks, "paused_tasks_for", lambda cid: [])
    monkeypatch.setattr(tasks, "link_task", lambda cid, tid: None)
    monkeypatch.setattr(tasks, "spawn", spawn or (
        lambda title, prompt, kind, ws, *a, **k: {"id": 42, "title": title}))

    conv = {"id": "c1", "workspace": workspace, "summary": "", "model": ""}
    sink = _Sink()
    out = _a.run(main._dispatch(conv, text, sink, "whatsapp"))
    return out, sink, started


def test_dispatch_sends_work_to_a_code_task_not_to_the_chat_turn(monkeypatch):
    """The whole point: this message must never reach a 300s chat turn."""
    seen = {}

    def spawn(title, prompt, kind, ws, *a, **k):
        seen.update({"title": title, "prompt": prompt, "kind": kind, "ws": ws})
        return {"id": 42, "title": title}

    out, sink, started = _dispatch(
        "implement the retry logic in the booking mapper", monkeypatch, spawn=spawn)

    assert seen.get("kind") == "code", f"not routed to a code task: {seen}"
    assert seen.get("ws") == "booking"
    assert "chat_turn" not in started, "it still went to the chat turn"
    assert out is None
    assert any("#42" in str(m.get("text", "")) for m in sink.sent), \
        "he was not told the task id"


def test_dispatch_leaves_a_question_to_the_chat_turn(monkeypatch):
    spawned = {"n": 0}

    def spawn(*a, **k):
        spawned["n"] += 1
        return {"id": 1, "title": "x"}

    out, _sink, started = _dispatch(
        "why does the booking build fail", monkeypatch, spawn=spawn)
    assert spawned["n"] == 0, "a question spawned a code task"
    assert started.get("chat_turn"), "the question was swallowed"


def test_dispatch_does_not_route_without_a_workspace(monkeypatch):
    """`tasks.code_cwd` refuses code work without one; routing into a refusal
    would be worse than not routing."""
    spawned = {"n": 0}

    def spawn(*a, **k):
        spawned["n"] += 1
        return {"id": 1, "title": "x"}

    out, _sink, started = _dispatch(
        "implement the retry logic in the mapper", monkeypatch,
        workspace="", spawn=spawn)
    assert spawned["n"] == 0
    assert started.get("chat_turn"), "the message was eaten"


def test_a_failed_spawn_still_answers_him(monkeypatch):
    """The message must never be lost: if the lane will not take it, chat does."""
    def boom(*a, **k):
        raise ValueError("workspace has no repos")

    out, _sink, started = _dispatch(
        "implement the retry logic in the mapper", monkeypatch, spawn=boom)
    assert started.get("chat_turn"), "a spawn failure swallowed his message"


def test_work_is_not_routed_while_a_task_is_already_live(monkeypatch):
    """A live task owns the conversation — steering it beats spawning a rival."""
    spawned = {"n": 0}

    def spawn(*a, **k):
        spawned["n"] += 1
        return {"id": 1, "title": "x"}

    out, _sink, _started = _dispatch(
        "implement the retry logic in the mapper", monkeypatch,
        spawn=spawn, live=(7,))
    assert spawned["n"] == 0, "spawned a second task while one was live"


# --- the chat brain cannot edit ----------------------------------------------

def test_the_chat_path_denies_the_edit_tool():
    """Defence in depth: routing catches the clear cases, this catches the rest.

    Only `edit` — bash, view and grep stay, because reading is most of what chat
    legitimately does and removing bash would break every Teams, git and log
    command.
    """
    import re
    from pathlib import Path

    from app import copilot_cli
    assert copilot_cli._deny_edit_in_chat() is True

    # Checked against the list it actually appends to, not by grepping for the
    # string. The first version of this test grepped — and passed while the code
    # said `argv += [...]` against a list named `cmd`, which raised
    # UnboundLocalError on every chat turn. The delivery tests caught that; this
    # one gave false confidence.
    src = Path("app/copilot_cli.py").read_text()
    m = re.search(r"\n    (\w+) = \[\n        \"copilot\",", src)
    assert m, "could not find the copilot command list"
    listname = m.group(1)
    deny = re.search(r"_deny_edit_in_chat\(\):\s*\n\s*(\w+) \+= \[\"--deny-tool\", \"edit\"\]", src)
    assert deny, "the chat path does not deny the edit tool"
    assert deny.group(1) == listname, (
        f"deny appends to {deny.group(1)!r} but the command list is {listname!r} — "
        f"this raises UnboundLocalError on every chat turn")


def test_task_runs_may_still_edit():
    """Implementing is a task's whole job — the denial is chat-only."""
    from pathlib import Path
    src = Path("app/copilot_cli.py").read_text()
    one_shot = src[src.index("async def one_shot"):]
    assert "--deny-tool" not in one_shot, \
        "the task path was barred from editing, which is its entire purpose"


def test_the_chat_edit_ban_is_reversible():
    import os
    from app import copilot_cli
    os.environ["ASTA_CHAT_MAY_EDIT"] = "1"
    try:
        assert copilot_cli._deny_edit_in_chat() is False
    finally:
        os.environ.pop("ASTA_CHAT_MAY_EDIT", None)
