"""13 Nov: "how many Teams chats …? just the number", typed while a reply to a
colleague waited for his yes, was read as feedback on it. The brain answered
"1" — and the reply was already gone, refused before the brain ran. A draft is
now lost only when he says no or a revision replaces it. (Synthetic names.)"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import answers, loop, main, store

DRAFT = "booking and ap deploy to sit from develop; email-intake is still pending."


class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


@pytest.fixture
def waiting(monkeypatch):
    """A colleague's reply staged in a phone conversation; turns recorded, not run."""
    conv = store.create_conversation(model="claude_cli", workspace=None)
    conv["model"] = "claude_cli"
    loop.stage(conv["id"], {"type": "answer", "kind": "send", "what": DRAFT,
                            "to": "Ravi Menon", "channel": "teams", "who": "Ravi Menon",
                            "_shown": time.time()})
    prompts: list[str] = []

    def start(c, prompt, sink, channel):
        prompts.append(prompt)
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut
    monkeypatch.setattr(main, "_start_turn", start)
    monkeypatch.setattr(answers, "_enqueue", lambda item: queued.append(item))
    queued: list[dict] = []
    return conv, prompts, queued


def _say(conv, text):
    asyncio.run(main._dispatch(conv, text, _Sink(), "whatsapp"))


def _refused() -> bool:
    return bool(loop.rejected_again(DRAFT))


def test_a_question_that_revises_nothing_keeps_the_draft(waiting):
    conv, prompts, queued = waiting
    _say(conv, "live check: how many Teams chats had new messages since 9am? just the number")
    assert prompts and prompts[0].startswith("Arun did NOT approve")    # still read as feedback
    main._settle_feedback(conv["id"], None)                            # the turn staged nothing
    assert queued and queued[0]["what"] == DRAFT, "set aside to come up again"
    assert not _refused(), "its words may still go out"


def test_a_revision_replaces_it(waiting):
    conv, _, queued = waiting
    _say(conv, "make it shorter")
    main._settle_feedback(conv["id"], {"kind": "send", "what": "shorter", "to": "Ravi Menon"})
    assert not queued and _refused()


@pytest.mark.parametrize("said", ["no, drop it", "cancel that", "don't send this one",
                                  "nope not needed"])
def test_his_no_drops_it(waiting, said):
    conv, _, queued = waiting
    _say(conv, said)
    main._settle_feedback(conv["id"], None)
    assert not queued


def test_a_revision_with_a_no_in_front_replaces_it(waiting):
    conv, _, queued = waiting
    _say(conv, "no, ask him to use the develop branch")
    main._settle_feedback(conv["id"], {"kind": "send", "what": "use develop", "to": "Ravi Menon"})
    assert not queued


def test_settling_twice_or_with_nothing_held_does_nothing(waiting):
    conv, _, queued = waiting
    main._settle_feedback(conv["id"], None)
    assert not queued


def test_a_send_the_turn_started_acted_on_the_draft(waiting, monkeypatch):
    """13 Nov: the brain sent its revision straight out (to the wrong chat) and the
    draft was "kept — not about it" and offered again."""
    from app import teams_bridge
    conv, _, queued = waiting
    _say(conv, "tell him I am working on it, will update once done")
    monkeypatch.setattr(teams_bridge, "STARTED", [time.time() + 1])
    main._settle_feedback(conv["id"], None)
    assert not queued
